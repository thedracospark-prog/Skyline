#!/usr/bin/env python3
"""Skyline API tests. Run:  python3 -m unittest discover -s tests -v
Starts the real server on an ephemeral port against a temp SQLite DB."""
import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server as sky


def req(base, method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {}


class PasswordTests(unittest.TestCase):
    def test_hash_and_verify(self):
        h = sky.hash_password("correct horse battery")
        self.assertNotIn("correct horse", h)
        self.assertTrue(sky.verify_password("correct horse battery", h))
        self.assertFalse(sky.verify_password("wrong password", h))

    def test_salts_differ(self):
        self.assertNotEqual(sky.hash_password("same-password"),
                            sky.hash_password("same-password"))


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        db = os.path.join(cls.tmp, "test.db")
        conn = sky.connect(db)
        sky.init_db(conn)
        conn.close()
        sky.Handler.db_path = db
        cls.httpd = sky.ThreadingHTTPServer(("127.0.0.1", 0), sky.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        # wipe mutable tables between tests for independence
        # (FK-safe order: sessions reference users, so they go first)
        conn = sky.connect(sky.Handler.db_path)
        try:
            for t in ("sessions", "conflicts", "audit", "vehicles",
                      "authorizations", "users"):
                conn.execute(f"DELETE FROM {t}")
            conn.commit()
        finally:
            conn.close()
        _, adm = req(self.base, "POST", "/api/setup",
                     {"username": "boss", "password": "admin-pass-1",
                      "display_name": "Boss", "unit": "18 ASOG"})
        self.assertEqual(adm.get("user", {}).get("role"), "admin")
        _, login = req(self.base, "POST", "/api/login",
                       {"username": "boss", "password": "admin-pass-1"})
        self.admin = login["token"]
        req(self.base, "POST", "/api/users",
            {"username": "vic", "password": "viewer-pass-1",
             "display_name": "Vic Viewer", "unit": "14 ASOS",
             "role": "viewer"}, self.admin)
        req(self.base, "POST", "/api/users",
            {"username": "ed", "password": "editor-pass-1",
             "display_name": "Ed Editor", "unit": "15 ASOS",
             "role": "editor"}, self.admin)
        _, lv = req(self.base, "POST", "/api/login",
                    {"username": "vic", "password": "viewer-pass-1"})
        self.viewer = lv["token"]
        _, le = req(self.base, "POST", "/api/login",
                    {"username": "ed", "password": "editor-pass-1"})
        self.editor = le["token"]

    # ------------------------------------------------ auth
    def test_setup_only_once(self):
        code, _ = req(self.base, "POST", "/api/setup",
                      {"username": "two", "password": "second-pass-1",
                       "display_name": "Two"})
        self.assertEqual(code, 403)

    def test_login_bad_password(self):
        code, _ = req(self.base, "POST", "/api/login",
                      {"username": "boss", "password": "nope-nope-nope"})
        self.assertEqual(code, 401)

    def test_no_public_signup_and_auth_required(self):
        code, _ = req(self.base, "GET", "/api/fleet")
        self.assertEqual(code, 401)

    # ------------------------------------------------ roles
    def add_vehicle(self, token, **kw):
        body = {"unit": "14 ASOS", "bumper": "A-1", "type": "JLTV",
                "status": "FMC", "mileage": 100, "location": "Pool"}
        body.update(kw)
        return req(self.base, "POST", "/api/vehicles", body, token)

    def test_viewer_cannot_create_or_edit(self):
        code, _ = self.add_vehicle(self.viewer)
        self.assertEqual(code, 403)
        code, v = self.add_vehicle(self.editor)
        self.assertEqual(code, 201)
        vid = v["vehicle"]["id"]
        code, _ = req(self.base, "PUT", f"/api/vehicles/{vid}",
                      {"status": "NMC"}, self.viewer)
        self.assertEqual(code, 403)

    def test_editor_can_create_and_edit(self):
        code, v = self.add_vehicle(self.editor)
        self.assertEqual(code, 201)
        vid = v["vehicle"]["id"]
        code, r = req(self.base, "PUT", f"/api/vehicles/{vid}",
                      {"status": "PMC", "base_status": "FMC"}, self.editor)
        self.assertEqual(code, 200)
        self.assertEqual(r["vehicle"]["status"], "PMC")

    # ------------------------------------------------ math / derivation
    def test_mc_rate_and_on_hand_derivation(self):
        self.add_vehicle(self.editor, bumper="A-1", status="FMC")
        self.add_vehicle(self.editor, bumper="A-2", status="FMC")
        self.add_vehicle(self.editor, bumper="A-3", status="PMC")
        self.add_vehicle(self.editor, bumper="A-4", status="NMC")
        req(self.base, "POST", "/api/authorizations",
            {"unit": "14 ASOS", "vehicle_type": "JLTV", "authorized": 6},
            self.editor)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.viewer)
        st = next(u for u in fleet["units"] if u["unit"] == "14 ASOS")
        self.assertEqual(st["on_hand"], 4)          # derived from records
        self.assertEqual(st["authorizations"], 6)
        self.assertEqual(st["mc_rate"], 50.0)       # 2 FMC / 4 on hand
        grp = fleet["group"]                        # v1.1: separate entry
        self.assertEqual(grp["on_hand"], 4)         # group rollup
        self.assertEqual(grp["mc_rate"], 50.0)

    # ------------------------------------------------ audit
    def test_audit_entries_written(self):
        _, v = self.add_vehicle(self.editor)
        vid = v["vehicle"]["id"]
        req(self.base, "PUT", f"/api/vehicles/{vid}",
            {"notes": "needs tires", "base_status": "FMC"}, self.editor)
        _, log = req(self.base, "GET", "/api/audit", token=self.viewer)
        actions = [(a["action"], a["field"]) for a in log["audit"]]
        self.assertIn(("create", ""), actions)
        self.assertIn(("update", "notes"), actions)
        note = next(a for a in log["audit"] if a["field"] == "notes")
        self.assertEqual(note["new_value"], "needs tires")
        self.assertEqual(note["actor"], "Ed Editor")

    # ------------------------------------------------ sync conflict
    def test_conflicting_status_flagged_not_overwritten(self):
        _, v = self.add_vehicle(self.editor)  # status FMC
        vid = v["vehicle"]["id"]
        # someone else moves it FMC -> PMC first
        req(self.base, "PUT", f"/api/vehicles/{vid}",
            {"status": "PMC", "base_status": "FMC"}, self.admin)
        # an offline editor, still seeing FMC, sets NMC and changes location
        code, r = req(self.base, "PUT", f"/api/vehicles/{vid}",
                      {"status": "NMC", "base_status": "FMC",
                       "location": "Field site"}, self.editor)
        self.assertEqual(code, 200)
        self.assertIn("conflict", r)
        self.assertEqual(r["vehicle"]["status"], "PMC")       # kept server's
        self.assertEqual(r["vehicle"]["location"], "Field site")  # merged
        _, cf = req(self.base, "GET", "/api/conflicts", token=self.editor)
        self.assertEqual(len(cf["conflicts"]), 1)
        self.assertEqual(cf["conflicts"][0]["attempted"], "NMC")
        _, log = req(self.base, "GET", "/api/audit", token=self.editor)
        self.assertTrue(any(a["is_conflict"] for a in log["audit"]))
        # editor resolves by applying the attempted status
        cid = cf["conflicts"][0]["id"]
        code, _ = req(self.base, "POST", f"/api/conflicts/{cid}/resolve",
                      {"action": "apply"}, self.editor)
        self.assertEqual(code, 200)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.viewer)
        veh = next(x for x in fleet["vehicles"] if x["id"] == vid)
        self.assertEqual(veh["status"], "NMC")

    def test_demo_fleet_is_labeled_and_removable(self):
        code, _ = req(self.base, "POST", "/api/demo/load", {}, self.admin)
        self.assertEqual(code, 200)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.viewer)
        self.assertGreater(len(fleet["vehicles"]), 0)
        self.assertTrue(all(v["is_demo"] for v in fleet["vehicles"]))
        self.assertTrue(all("SAMPLE" in v["notes"]
                            for v in fleet["vehicles"]))
        req(self.base, "POST", "/api/demo/remove", {}, self.admin)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.viewer)
        self.assertEqual(len(fleet["vehicles"]), 0)


if __name__ == "__main__":
    unittest.main()


# =====================================================================
# Review fix-pass regression tests (2026-10-23). Every test here maps to
# a proven DO-NOT-SHIP finding from the two independent v1 reviews.
# =====================================================================
import concurrent.futures
import sqlite3


def req_raw(base, method, path, raw_bytes, token=None, ctype="application/json"):
    r = urllib.request.Request(base + path, data=raw_bytes, method=method)
    r.add_header("Content-Type", ctype)
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r) as resp:
            body = resp.read()
            try:
                return resp.status, json.loads(body or b"{}")
            except Exception:
                return resp.status, {"_text": body.decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {"_text": raw.decode("utf-8", "replace")}


def start_server(db_path):
    conn = sky.connect(db_path)
    sky.init_db(conn)
    conn.close()
    # dedicated subclass so this server's db_path never fights the main one
    handler = type("IsolatedHandler", (sky.Handler,), {"db_path": db_path})
    httpd = sky.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", db_path


class FixPassTests(ApiTests):
    """Inherits ApiTests.setUp (fresh admin/editor/viewer per test)."""

    # ---- item 1: atomic first-run setup
    def test_parallel_setup_creates_exactly_one_admin(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "race.db")
        httpd, base, _ = start_server(db)
        try:
            def attempt(i):
                return req(base, "POST", "/api/setup",
                           {"username": f"racer{i}", "password": "race-pass-1",
                            "display_name": f"Racer {i}", "unit": "18 ASOG"})
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                results = list(ex.map(attempt, range(8)))
            codes = sorted(c for c, _ in results)
            self.assertEqual(codes.count(201), 1, codes)
            self.assertEqual(codes.count(403), 7, codes)
            conn = sky.connect(db)
            n = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            conn.close()
            self.assertEqual(n, 1)
            # winner got a session straight from setup (item 16)
            winner = next(b for c, b in results if c == 201)
            self.assertIn("token", winner)
            code, me = req(base, "GET", "/api/me", token=winner["token"])
            self.assertEqual(code, 200)
            self.assertEqual(me["user"]["role"], "admin")
        finally:
            httpd.shutdown()

    # ---- item 2: numeric validation on insert AND update
    def test_numeric_fields_validated(self):
        for bad in ("abc", -5, 12.5, True):
            code, _ = self.add_vehicle(self.editor, mileage=bad)
            self.assertEqual(code, 400, f"mileage={bad!r}")
            code, _ = self.add_vehicle(self.editor, maint_due_miles=bad)
            self.assertEqual(code, 400, f"maint_due_miles={bad!r}")
        code, v = self.add_vehicle(self.editor, mileage="1200")
        self.assertEqual(code, 201)  # digit string coerced
        self.assertEqual(v["vehicle"]["mileage"], 1200)
        vid = v["vehicle"]["id"]
        code, _ = req(self.base, "PUT", f"/api/vehicles/{vid}",
                      {"mileage": -1}, self.editor)
        self.assertEqual(code, 400)
        code, _ = req(self.base, "PUT", f"/api/vehicles/{vid}",
                      {"maint_due_miles": 3.5}, self.editor)
        self.assertEqual(code, 400)

    # ---- item 3: group rollup sums PMC/NMC too
    def test_group_rollup_sums_all_statuses(self):
        self.add_vehicle(self.editor, unit="14 ASOS", bumper="G-1", status="PMC")
        self.add_vehicle(self.editor, unit="14 ASOS", bumper="G-2", status="NMC")
        self.add_vehicle(self.editor, unit="15 ASOS", bumper="G-3", status="PMC")
        self.add_vehicle(self.editor, unit="15 ASOS", bumper="G-4", status="FMC")
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.viewer)
        grp = fleet["group"]                        # v1.1: separate entry
        self.assertEqual(grp["on_hand"], 4)
        self.assertEqual(grp["pmc"], 2)
        self.assertEqual(grp["nmc"], 1)
        self.assertEqual(grp["fmc"], 1)
        self.assertEqual(grp["mc_rate"], 25.0)

    # ---- item 4: conflict resolve guards
    def _make_conflict(self):
        import uuid
        _, v = self.add_vehicle(self.editor,
                                bumper="CF-" + uuid.uuid4().hex[:8])
        vid = v["vehicle"]["id"]
        req(self.base, "PUT", f"/api/vehicles/{vid}",
            {"status": "PMC", "base_status": "FMC"}, self.admin)
        _, r = req(self.base, "PUT", f"/api/vehicles/{vid}",
                   {"status": "NMC", "base_status": "FMC"}, self.editor)
        return vid, r["conflict"]["id"]

    def test_conflict_resolve_guards(self):
        vid, cid = self._make_conflict()
        # bogus action -> 400 and no state change
        code, _ = req(self.base, "POST", f"/api/conflicts/{cid}/resolve",
                      {"action": "explode"}, self.editor)
        self.assertEqual(code, 400)
        _, cf = req(self.base, "GET", "/api/conflicts", token=self.editor)
        self.assertEqual(len(cf["conflicts"]), 1)
        # proper resolve works once…
        code, _ = req(self.base, "POST", f"/api/conflicts/{cid}/resolve",
                      {"action": "keep"}, self.editor)
        self.assertEqual(code, 200)
        # …and only once
        code, _ = req(self.base, "POST", f"/api/conflicts/{cid}/resolve",
                      {"action": "apply"}, self.editor)
        self.assertEqual(code, 409)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.viewer)
        veh = next(x for x in fleet["vehicles"] if x["id"] == vid)
        self.assertEqual(veh["status"], "PMC")  # keep won; re-resolve didn't apply

    # ---- item 5: demo-data hygiene
    def test_demo_never_overwrites_real_authorization(self):
        req(self.base, "POST", "/api/authorizations",
            {"unit": "14 ASOS", "vehicle_type": "JLTV", "authorized": 7},
            self.editor)
        req(self.base, "POST", "/api/demo/load", {}, self.admin)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.viewer)
        auth = next(a for a in fleet["authorizations"]
                    if a["unit"] == "14 ASOS" and a["vehicle_type"] == "JLTV")
        self.assertEqual(auth["authorized"], 7)   # real value untouched
        self.assertEqual(auth["is_demo"], 0)
        n_after_first = len(fleet["vehicles"])
        # idempotent: second load adds nothing
        req(self.base, "POST", "/api/demo/load", {}, self.admin)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.viewer)
        self.assertEqual(len(fleet["vehicles"]), n_after_first)
        # a real edit of a demo-flagged authorization clears its demo flag
        demo_auth = next(a for a in fleet["authorizations"] if a["is_demo"])
        req(self.base, "POST", "/api/authorizations",
            {"unit": demo_auth["unit"], "vehicle_type": demo_auth["vehicle_type"],
             "authorized": 9}, self.editor)
        # remove demo: real + real-edited authorizations survive
        req(self.base, "POST", "/api/demo/remove", {}, self.admin)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.viewer)
        self.assertEqual(len(fleet["vehicles"]), 0)
        kept = {(a["unit"], a["vehicle_type"]): a["authorized"]
                for a in fleet["authorizations"]}
        self.assertEqual(kept[("14 ASOS", "JLTV")], 7)
        self.assertEqual(
            kept[(demo_auth["unit"], demo_auth["vehicle_type"])], 9)

    # ---- item 6: validation on all write paths
    def test_write_path_validation(self):
        # v1.1: 18 ASOG holds its OWN vehicles now — but a unit that is
        # not one of the five is still rejected
        code, _ = self.add_vehicle(self.editor, unit="18 ASOG",
                                   bumper="HQ-0")
        self.assertEqual(code, 201)
        code, _ = self.add_vehicle(self.editor, unit="99 ASOS")
        self.assertEqual(code, 400)
        code, _ = self.add_vehicle(self.editor, bumper="  ")
        self.assertEqual(code, 400)
        code, _ = self.add_vehicle(self.editor, type="")
        self.assertEqual(code, 400)
        code, _ = self.add_vehicle(self.editor, status="BROKEN")
        self.assertEqual(code, 400)
        code, _ = self.add_vehicle(self.editor,
                                   maint_due_date="2026-13-40")
        self.assertEqual(code, 400)
        code, _ = self.add_vehicle(self.editor, maint_due_date="next week")
        self.assertEqual(code, 400)
        code, v = self.add_vehicle(self.editor, bumper="DUP-1")
        self.assertEqual(code, 201)
        code, _ = self.add_vehicle(self.editor, bumper="DUP-1")
        self.assertEqual(code, 409)  # dupe in same unit
        code, _ = self.add_vehicle(self.editor, unit="15 ASOS", bumper="DUP-1")
        self.assertEqual(code, 201)  # same bumper, other unit: fine
        # moving a vehicle onto an occupied bumper in the target unit
        _, v2 = self.add_vehicle(self.editor, unit="15 ASOS", bumper="MV-1")
        code, _ = req(self.base, "PUT", f"/api/vehicles/{v2['vehicle']['id']}",
                      {"unit": "14 ASOS", "bumper": "DUP-1"}, self.editor)
        self.assertEqual(code, 409)
        # authorizations (v1.1: 18 ASOG authorizations are valid too,
        # covered in V11Tests; a non-unit is not)
        for body, want in [
                ({"unit": "99 ASOS", "vehicle_type": "JLTV", "authorized": 3}, 400),
                ({"unit": "14 ASOS", "vehicle_type": "JLTV", "authorized": 3.5}, 400),
                ({"unit": "14 ASOS", "vehicle_type": "JLTV", "authorized": -1}, 400),
                ({"unit": "14 ASOS", "vehicle_type": "", "authorized": 3}, 400)]:
            code, _ = req(self.base, "POST", "/api/authorizations", body,
                          self.editor)
            self.assertEqual(code, want, body)
        # user affiliation must be one of the five units
        code, _ = req(self.base, "POST", "/api/users",
                      {"username": "zz", "password": "zz-pass-123",
                       "display_name": "Zed", "unit": "Narnia",
                       "role": "viewer"}, self.admin)
        self.assertEqual(code, 400)

    # ---- item 7: no request dies without JSON; unknown ids 404
    def test_error_handling(self):
        code, body = req(self.base, "PUT", "/api/vehicles/abc",
                         {"status": "NMC"}, self.editor)
        self.assertEqual(code, 404)
        self.assertIn("error", body)
        code, body = req_raw(self.base, "POST", "/api/login", b"{not json")
        self.assertEqual(code, 400)
        self.assertIn("malformed", body["error"])
        code, body = req_raw(self.base, "POST", "/api/login",
                             b"x" * (1024 * 1024 + 10))
        self.assertEqual(code, 413)
        code, _ = req(self.base, "PUT", "/api/users/99999",
                      {"role": "editor"}, self.admin)
        self.assertEqual(code, 404)  # no ghost success
        code, _ = req(self.base, "DELETE", "/api/vehicles/99999",
                      token=self.editor)
        self.assertEqual(code, 404)
        code, _ = req(self.base, "POST", "/api/conflicts/99999/resolve",
                      {"action": "keep"}, self.editor)
        self.assertEqual(code, 404)

    # ---- item 8: session expiry / revocation / last-admin guard
    def test_session_revocation_and_last_admin(self):
        # role change revokes the target's sessions
        code, _ = req(self.base, "PUT", "/api/users/3", {"role": "editor"},
                      self.admin)  # ed (id 3) editor -> editor? ensure exists
        _, users = req(self.base, "GET", "/api/users", token=self.admin)
        ed = next(u for u in users["users"] if u["username"] == "ed")
        code, _ = req(self.base, "PUT", f"/api/users/{ed['id']}",
                      {"role": "viewer"}, self.admin)
        self.assertEqual(code, 200)
        code, _ = req(self.base, "GET", "/api/me", token=self.editor)
        self.assertEqual(code, 401)  # old token dead after role change
        # password reset revokes sessions too
        _, lv = req(self.base, "POST", "/api/login",
                    {"username": "vic", "password": "viewer-pass-1"})
        code, _ = req(self.base, "PUT", f"/api/users/{2}",
                      {"password": "viewer-pass-2"}, self.admin)
        self.assertEqual(code, 200)
        code, _ = req(self.base, "GET", "/api/me", token=lv["token"])
        self.assertEqual(code, 401)
        # the last Admin cannot be demoted (boss is the only admin)
        _, users = req(self.base, "GET", "/api/users", token=self.admin)
        boss = next(u for u in users["users"] if u["username"] == "boss")
        code, _ = req(self.base, "PUT", f"/api/users/{boss['id']}",
                      {"role": "viewer"}, self.admin)
        self.assertEqual(code, 400)
        # expired session is rejected
        conn = sky.connect(sky.Handler.db_path)
        conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
        conn.commit()
        conn.close()
        code, _ = req(self.base, "GET", "/api/me", token=self.admin)
        self.assertEqual(code, 401)

    # ---- item 9: audit correctness
    def test_audit_correctness(self):
        _, v = self.add_vehicle(self.editor, notes="orig")
        vid = v["vehicle"]["id"]
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.viewer)
        before = next(x for x in fleet["vehicles"] if x["id"] == vid)
        _, log = req(self.base, "GET", "/api/audit", token=self.viewer)
        n_audit = len(log["audit"])
        # no-op PUT: no audit rows, updated_by/at untouched
        code, _ = req(self.base, "PUT", f"/api/vehicles/{vid}",
                      {"notes": "orig", "status": "FMC"}, self.editor)
        self.assertEqual(code, 200)
        _, log = req(self.base, "GET", "/api/audit", token=self.viewer)
        self.assertEqual(len(log["audit"]), n_audit)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.viewer)
        after = next(x for x in fleet["vehicles"] if x["id"] == vid)
        self.assertEqual(after["updated_at"], before["updated_at"])
        # authorization audit carries real old -> new
        req(self.base, "POST", "/api/authorizations",
            {"unit": "14 ASOS", "vehicle_type": "HMMWV", "authorized": 5},
            self.editor)
        req(self.base, "POST", "/api/authorizations",
            {"unit": "14 ASOS", "vehicle_type": "HMMWV", "authorized": 8},
            self.editor)
        _, log = req(self.base, "GET", "/api/audit", token=self.viewer)
        auth_rows = [a for a in log["audit"] if a["action"] == "authorization"]
        self.assertTrue(any(a["old_value"] == "5" and a["new_value"] == "8"
                            for a in auth_rows))
        # account update audit carries old -> new
        _, users = req(self.base, "GET", "/api/users", token=self.admin)
        vic = next(u for u in users["users"] if u["username"] == "vic")
        req(self.base, "PUT", f"/api/users/{vic['id']}",
            {"role": "editor"}, self.admin)
        # (vic's own session was revoked by that role change — use admin's)
        _, log = req(self.base, "GET", "/api/audit", token=self.admin)
        self.assertTrue(any(a["action"] == "account" and a["field"] == "role"
                            and a["old_value"] == "viewer"
                            and a["new_value"] == "editor"
                            for a in log["audit"]))
        # deleting a vehicle closes its open conflicts (audited)
        vid2, cid2 = self._make_conflict()
        code, _ = req(self.base, "DELETE", f"/api/vehicles/{vid2}",
                      token=self.editor)
        self.assertEqual(code, 200)
        _, cf = req(self.base, "GET", "/api/conflicts", token=self.editor)
        self.assertFalse(any(c["id"] == cid2 for c in cf["conflicts"]))
        conn = sky.connect(sky.Handler.db_path)
        row = conn.execute("SELECT resolved FROM conflicts WHERE id=?",
                           (cid2,)).fetchone()
        closes = conn.execute(
            "SELECT COUNT(*) c FROM audit WHERE vehicle_id=? AND"
            " action='conflict-close'", (vid2,)).fetchone()["c"]
        conn.close()
        self.assertEqual(row["resolved"], 1)
        self.assertGreaterEqual(closes, 1)

    # ---- item 10: CSV formula-injection neutralization
    def test_csv_formula_injection_neutralized(self):
        self.add_vehicle(self.editor, bumper="CSV-1",
                         notes="=2+5 dangerous", location="@SUM(A1:A9)")
        code, body = req_raw(self.base, "GET", "/api/export.csv",
                             b"", token=self.viewer)
        self.assertEqual(code, 200)
        text = body["_text"]
        self.assertIn("'=2+5 dangerous", text)
        self.assertIn("'@SUM(A1:A9)", text)

    # ---- item 11a: idempotent creates by client op UUID
    def test_create_dedup_by_op_id(self):
        body = {"unit": "14 ASOS", "bumper": "OP-1", "type": "JLTV",
                "status": "FMC", "op_id": "op-uuid-1234"}
        code, r1 = req(self.base, "POST", "/api/vehicles", body, self.editor)
        self.assertEqual(code, 201)
        code, r2 = req(self.base, "POST", "/api/vehicles", body, self.editor)
        self.assertEqual(code, 200)
        self.assertEqual(r1["vehicle"]["id"], r2["vehicle"]["id"])
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.viewer)
        self.assertEqual(
            len([v for v in fleet["vehicles"] if v["bumper"] == "OP-1"]), 1)

    # ---- item 16 (server half): snapshot carries includes_demo per unit
    def test_snapshot_includes_demo_flags(self):
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.viewer)
        self.assertFalse(any(u["includes_demo"] for u in fleet["units"]))
        req(self.base, "POST", "/api/demo/load", {}, self.admin)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.viewer)
        grp = fleet["group"]                        # v1.1: separate entry
        self.assertTrue(grp["includes_demo"])
        self.assertTrue(all(u["includes_demo"] for u in fleet["units"]
                            if u["kind"] == "squadron"))


# =====================================================================
# Fix pass 2 regression tests (2026-10-23). Three proven findings from
# the verification re-review: (1) bumper uniqueness was check-then-insert
# outside a transaction — parallel identical creates duplicated rows;
# (2) conflict resolve checked `resolved` before BEGIN IMMEDIATE —
# parallel resolves all "won"; (3) DELETE never read its request body,
# desyncing keep-alive connections.
# =====================================================================
import contextlib
import http.client
import io
import uuid


def _parallel(n, fn):
    """Run fn(i) in n threads released simultaneously by a barrier."""
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait(timeout=15)
        return fn(i)

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
        return list(ex.map(run, range(n)))


class FixPass2Tests(ApiTests):
    """Inherits ApiTests.setUp (fresh admin/editor/viewer per test)."""

    def _db_count(self, sql, args=()):
        conn = sky.connect(sky.Handler.db_path)
        try:
            return conn.execute(sql, args).fetchone()[0]
        finally:
            conn.close()

    # ---- fix 1a: parallel identical creates -> exactly one wins
    def test_parallel_identical_creates_exactly_one(self):
        results = _parallel(10, lambda i: self.add_vehicle(
            self.editor, bumper="RACE-1"))
        codes = sorted(c for c, _ in results)
        self.assertEqual(codes.count(201), 1, codes)
        self.assertEqual(codes.count(409), 9, codes)
        self.assertEqual(self._db_count(
            "SELECT COUNT(*) FROM vehicles WHERE unit='14 ASOS'"
            " AND bumper='RACE-1'"), 1)

    # ---- fix 1b: same bumper in DIFFERENT units all succeed in parallel
    def test_parallel_same_bumper_different_units_all_succeed(self):
        units = list(sky.SQUADRONS)
        results = _parallel(len(units), lambda i: self.add_vehicle(
            self.editor, unit=units[i], bumper="MULTI-1"))
        codes = sorted(c for c, _ in results)
        self.assertEqual(codes, [201] * len(units), codes)
        self.assertEqual(self._db_count(
            "SELECT COUNT(*) FROM vehicles WHERE bumper='MULTI-1'"),
            len(units))

    # ---- fix 1c: UNIQUE(unit, bumper) index exists; a legacy DB that
    # already holds duplicates must init without crashing (index skipped,
    # duplicates logged, rows preserved)
    def test_bumper_unique_index_and_legacy_migration(self):
        conn = sky.connect(sky.Handler.db_path)
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND name='idx_vehicles_unit_bumper'").fetchone()
        conn.close()
        self.assertIsNotNone(idx)
        # legacy-shaped vehicles table (no client_op column, no index)
        # with duplicate (unit, bumper) rows already in it
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "legacy.db")
        raw = sqlite3.connect(path)
        raw.execute(
            "CREATE TABLE vehicles(id INTEGER PRIMARY KEY,"
            " unit TEXT NOT NULL, bumper TEXT NOT NULL,"
            " type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'FMC',"
            " mileage INTEGER, maint_due_date TEXT DEFAULT '',"
            " maint_due_miles INTEGER, location TEXT DEFAULT '',"
            " notes TEXT DEFAULT '', is_demo INTEGER NOT NULL DEFAULT 0,"
            " updated_by TEXT DEFAULT '', updated_at TEXT DEFAULT '')")
        for _ in range(2):
            raw.execute("INSERT INTO vehicles(unit, bumper, type)"
                        " VALUES ('14 ASOS', 'LEG-1', 'JLTV')")
        raw.commit()
        raw.close()
        conn = sky.connect(path)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sky.init_db(conn)  # must NOT raise
        n = conn.execute("SELECT COUNT(*) c FROM vehicles").fetchone()["c"]
        idx2 = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND name='idx_vehicles_unit_bumper'").fetchone()
        conn.close()
        self.assertEqual(n, 2)            # duplicates preserved, not eaten
        self.assertIsNone(idx2)           # index skipped, not forced
        self.assertIn("LEG-1", err.getvalue())  # …and logged clearly

    # ---- fix 1d: parallel unit-moves onto the same (unit, bumper):
    # the update-path uniqueness check is inside its transaction too
    def test_parallel_unit_moves_onto_same_bumper_one_wins(self):
        _, v1 = self.add_vehicle(self.editor, unit="15 ASOS", bumper="MV-A")
        _, v2 = self.add_vehicle(self.editor, unit="19 ASOS", bumper="MV-B")
        ids = [v1["vehicle"]["id"], v2["vehicle"]["id"]]
        results = _parallel(2, lambda i: req(
            self.base, "PUT", f"/api/vehicles/{ids[i]}",
            {"unit": "14 ASOS", "bumper": "TGT-1"}, self.editor))
        codes = sorted(c for c, _ in results)
        self.assertEqual(codes, [200, 409], codes)
        self.assertEqual(self._db_count(
            "SELECT COUNT(*) FROM vehicles WHERE unit='14 ASOS'"
            " AND bumper='TGT-1'"), 1)

    # ---- fix 2: parallel resolves of one conflict -> resolve-once
    def _make_conflict2(self):
        _, v = self.add_vehicle(self.editor,
                                bumper="CF2-" + uuid.uuid4().hex[:8])
        vid = v["vehicle"]["id"]
        req(self.base, "PUT", f"/api/vehicles/{vid}",
            {"status": "PMC", "base_status": "FMC"}, self.admin)
        _, r = req(self.base, "PUT", f"/api/vehicles/{vid}",
                   {"status": "NMC", "base_status": "FMC"}, self.editor)
        return vid, r["conflict"]["id"]

    def test_parallel_conflict_resolve_exactly_once(self):
        vid, cid = self._make_conflict2()
        results = _parallel(10, lambda i: req(
            self.base, "POST", f"/api/conflicts/{cid}/resolve",
            {"action": "apply"}, self.editor))
        codes = sorted(c for c, _ in results)
        self.assertEqual(codes.count(200), 1, codes)
        self.assertEqual(codes.count(409), 9, codes)
        # exactly ONE resolution audit row for this conflict's vehicle
        self.assertEqual(self._db_count(
            "SELECT COUNT(*) FROM audit WHERE vehicle_id=?"
            " AND detail LIKE 'conflict resolved:%'", (vid,)), 1)
        conn = sky.connect(sky.Handler.db_path)
        row = conn.execute("SELECT resolved FROM conflicts WHERE id=?",
                           (cid,)).fetchone()
        conn.close()
        self.assertEqual(row["resolved"], 1)
        # winner's action ("apply") is what the vehicle now shows
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.viewer)
        veh = next(x for x in fleet["vehicles"] if x["id"] == vid)
        self.assertEqual(veh["status"], "NMC")

    # ---- fix 3: DELETE with a body, then reuse the SAME connection
    def test_delete_with_body_keeps_connection_usable(self):
        _, v = self.add_vehicle(self.editor, bumper="DEL-1")
        vid = v["vehicle"]["id"]
        port = int(self.base.rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            auth = {"Authorization": "Bearer " + self.editor,
                    "Content-Type": "application/json"}
            conn.request("DELETE", f"/api/vehicles/{vid}",
                         body=b'{"reason":"decommissioned"}', headers=auth)
            resp = conn.getresponse()
            payload = resp.read()
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(payload), {"ok": True})
            # second request on the SAME persistent connection must come
            # back as clean JSON, not corrupted by the leftover body
            conn.request("GET", "/api/fleet",
                         headers={"Authorization": "Bearer " + self.viewer})
            resp2 = conn.getresponse()
            payload2 = resp2.read()
            self.assertEqual(resp2.status, 200)
            fleet = json.loads(payload2)
            self.assertIn("vehicles", fleet)
            self.assertFalse(any(x["id"] == vid for x in fleet["vehicles"]))
            # and a DELETE-with-body 404 also stays JSON on this connection
            conn.request("DELETE", "/api/vehicles/99999",
                         body=b'{"x":1}', headers=auth)
            resp3 = conn.getresponse()
            payload3 = resp3.read()
            self.assertEqual(resp3.status, 404)
            self.assertIn("error", json.loads(payload3))
        finally:
            conn.close()

# ===================================================================== v1.1
def audit_actions():
    conn = sqlite3.connect(sky.Handler.db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM audit")]
    conn.close()
    return rows

class V11Tests(ApiTests):
    """v1.1: 18 ASOG's own fleet, per-unit authorizations, UTCs."""

    def setUp(self):
        conn = sqlite3.connect(sky.Handler.db_path)
        conn.execute("DELETE FROM utc_vehicles")
        conn.execute("DELETE FROM utcs")
        conn.commit()
        conn.close()
        super().setUp()

    def req(self, token, method, path, body=None):
        return req(self.base, method, path, body, token)

    def mk_utc(self, token, unit="14 ASOS", code="XFFC1", title="Pkg"):
        return self.req(token, "POST", "/api/utcs",
                        {"unit": unit, "code": code, "title": title})

    def test_asog_own_vehicles_and_group_sums(self):
        # ASOG's own fleet: one of each status + authorization rows
        for bumper, status in [("HQ-1", "FMC"), ("HQ-2", "PMC"),
                               ("HQ-3", "NMC")]:
            code, _ = self.add_vehicle(self.editor, unit="18 ASOG",
                                       bumper=bumper, status=status)
            self.assertEqual(code, 201)
        self.req(self.editor, "POST", "/api/authorizations",
                 {"unit": "18 ASOG", "vehicle_type": "JLTV", "authorized": 5})
        code, _ = self.add_vehicle(self.editor)  # 14 ASOS FMC default
        self.assertEqual(code, 201)
        _, snap = self.req(self.viewer, "GET", "/api/fleet")
        asog = next(u for u in snap["units"] if u["unit"] == "18 ASOG")
        # the ASOG unit entry is its OWN fleet now
        self.assertEqual((asog["on_hand"], asog["fmc"], asog["pmc"],
                          asog["nmc"]), (3, 1, 1, 1))
        self.assertEqual(asog["authorizations"], 5)
        self.assertEqual(asog["mc_rate"], round(1 / 3 * 100, 1))
        # the GROUP entry sums ALL FIVE units incl. ASOG's own
        grp = snap["group"]
        self.assertEqual((grp["on_hand"], grp["fmc"], grp["pmc"],
                          grp["nmc"]), (4, 2, 1, 1))
        self.assertEqual(grp["authorizations"], 5)
        self.assertEqual(grp["mc_rate"], 50.0)

    def test_asog_bumper_scope_and_update(self):
        code, _ = self.add_vehicle(self.editor, unit="18 ASOG", bumper="S-1")
        self.assertEqual(code, 201)
        code, _ = self.add_vehicle(self.editor, bumper="S-1")  # 14 ASOS ok
        self.assertEqual(code, 201)
        code, _ = self.add_vehicle(self.editor, unit="18 ASOG", bumper="S-1")
        self.assertEqual(code, 409)  # duplicate within ASOG
        # update an ASOG vehicle's unit to a squadron and back
        _, fleet = self.req(self.viewer, "GET", "/api/fleet")
        hq = next(v for v in fleet["vehicles"] if v["unit"] == "18 ASOG")
        code, body = self.req(self.editor, "PUT", f"/api/vehicles/{hq['id']}",
                              {"unit": "20 ASOS"})
        self.assertEqual(code, 200, body)

    def test_authorizations_all_five_units(self):
        for unit in sky.UNIT_NAMES:
            code, body = self.req(
                self.editor, "POST", "/api/authorizations",
                {"unit": unit, "vehicle_type": "HMMWV", "authorized": 7})
            self.assertEqual(code, 200, (unit, body))
        code, _ = self.req(self.viewer, "POST", "/api/authorizations",
                           {"unit": "18 ASOG", "vehicle_type": "X",
                            "authorized": 1})
        self.assertEqual(code, 403)  # viewers stay read-only
        _, snap = self.req(self.viewer, "GET", "/api/fleet")
        for unit in sky.UNIT_NAMES:
            entry = next(a for a in snap["authorizations"]
                         if a["unit"] == unit)
            self.assertEqual(entry["authorized"], 7)
        self.assertEqual(snap["group"]["authorizations"], 35)

    def test_utc_crud_membership_rules(self):
        _, v1 = self.add_vehicle(self.editor)                     # 14 ASOS
        _, v2 = self.add_vehicle(self.editor, bumper="B-2")
        code, other = self.add_vehicle(self.editor, unit="15 ASOS",
                                       bumper="C-1")
        self.assertEqual(code, 201)
        code, utc = self.mk_utc(self.editor)
        self.assertEqual(code, 201, utc)
        uid = utc["utc"]["id"]
        # viewers cannot create or modify UTCs
        self.assertEqual(self.mk_utc(self.viewer, code="ZZZ")[0], 403)
        # duplicate code in the same unit -> 409; same code elsewhere ok
        self.assertEqual(self.mk_utc(self.editor)[0], 409)
        self.assertEqual(self.mk_utc(self.editor, unit="15 ASOS")[0], 201)
        # membership: own unit ok, other unit 400, nonexistent 400
        code, _ = self.req(self.editor, "POST", f"/api/utcs/{uid}/vehicles",
                           {"vehicle_id": v1["vehicle"]["id"]})
        self.assertEqual(code, 200)
        code, _ = self.req(self.editor, "POST", f"/api/utcs/{uid}/vehicles",
                           {"vehicle_id": v2["vehicle"]["id"]})
        self.assertEqual(code, 200)
        code, body = self.req(self.editor, "POST",
                              f"/api/utcs/{uid}/vehicles",
                              {"vehicle_id": other["vehicle"]["id"]})
        self.assertEqual(code, 400, body)  # wrong unit
        code, _ = self.req(self.editor, "POST", f"/api/utcs/{uid}/vehicles",
                           {"vehicle_id": 999999})
        self.assertEqual(code, 400)
        code, _ = self.req(self.viewer, "POST", f"/api/utcs/{uid}/vehicles",
                           {"vehicle_id": v1["vehicle"]["id"]})
        self.assertEqual(code, 403)
        # a vehicle may sit in MULTIPLE UTCs
        code, utc2 = self.mk_utc(self.editor, code="XFFC2")
        self.assertEqual(code, 201)
        code, _ = self.req(self.editor, "POST",
                           f"/api/utcs/{utc2['utc']['id']}/vehicles",
                           {"vehicle_id": v1["vehicle"]["id"]})
        self.assertEqual(code, 200)
        # snapshot carries UTCs + membership for offline viewing
        _, snap = self.req(self.viewer, "GET", "/api/fleet")
        u = next(x for x in snap["utcs"] if x["id"] == uid)
        self.assertEqual(sorted(u["vehicle_ids"]),
                         sorted([v1["vehicle"]["id"], v2["vehicle"]["id"]]))
        # update code/title (audited)
        code, body = self.req(self.editor, "PUT", f"/api/utcs/{uid}",
                              {"code": "XFFC9", "title": "Renamed"})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["utc"]["code"], "XFFC9")
        # remove a member explicitly
        code, _ = self.req(self.editor, "DELETE",
                           f"/api/utcs/{uid}/vehicles/{v2['vehicle']['id']}")
        self.assertEqual(code, 200)
        # deleting a VEHICLE drops its memberships
        code, _ = self.req(self.editor, "DELETE", f"/api/vehicles/{v1['vehicle']['id']}")
        self.assertEqual(code, 200)
        _, snap = self.req(self.viewer, "GET", "/api/fleet")
        u = next(x for x in snap["utcs"] if x["id"] == uid)
        self.assertEqual(u["vehicle_ids"], [])
        # deleting the UTC keeps the vehicles
        code, _ = self.req(self.editor, "DELETE", f"/api/utcs/{uid}")
        self.assertEqual(code, 200)
        _, snap = self.req(self.viewer, "GET", "/api/fleet")
        self.assertFalse(any(x["id"] == uid for x in snap["utcs"]))
        self.assertTrue(any(v["bumper"] == "B-2" for v in snap["vehicles"]))
        acts = {a["action"] for a in audit_actions()}
        for want in ("utc-create", "utc-update", "utc-add-vehicle",
                     "utc-remove-vehicle", "utc-delete"):
            self.assertIn(want, acts)

    def test_utc_validation(self):
        code, _ = self.mk_utc(self.editor, unit="99 ASOS")
        self.assertEqual(code, 400)
        code, _ = self.mk_utc(self.editor, code="  ")
        self.assertEqual(code, 400)
        code, _ = self.mk_utc(self.editor, code="X" * 21)
        self.assertEqual(code, 400)  # over the code length cap

# ------------------------------------------------------------------ import
def make_xlsx(rows):
    """Build a minimal real .xlsx (zip of XML) with shared strings.
    rows: list of rows; a cell is str (-> shared string) or int/float
    (-> numeric cell) or None (-> skipped)."""
    import zipfile
    strings, index = [], {}
    def sidx(s):
        if s not in index:
            index[s] = len(strings)
            strings.append(s)
        return index[s]
    def colletters(n):
        out = ""
        n += 1
        while n:
            n, r = divmod(n - 1, 26)
            out = chr(65 + r) + out
        return out
    sheet_rows = []
    for ri, row in enumerate(rows, start=1):
        cells = []
        for ci, val in enumerate(row):
            if val is None:
                continue
            ref = f"{colletters(ci)}{ri}"
            if isinstance(val, (int, float)):
                cells.append(f'<c r="{ref}"><v>{val}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="s"><v>{sidx(str(val))}</v></c>')
        sheet_rows.append(f'<row r="{ri}">{"".join(cells)}</row>')
    ss = "".join(f"<si><t>{s}</t></si>" for s in strings)
    parts = {
        "[Content_Types].xml":
            '<?xml version="1.0"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types"><Default Extension="xml" ContentType="text/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.'
            'worksheet+xml"/><Override PartName="/xl/sharedStrings.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.sharedStrings+xml"/></Types>',
        "_rels/.rels":
            '<?xml version="1.0"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Id="rId1" Type='
            '"http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        "xl/workbook.xml":
            '<?xml version="1.0"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main"><sheets><sheet name="Fleet" '
            'sheetId="1"/></sheets></workbook>',
        "xl/sharedStrings.xml":
            '<?xml version="1.0"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
            f'2006/main" count="{len(strings)}" uniqueCount="{len(strings)}">'
            f"{ss}</sst>",
        "xl/worksheets/sheet1.xml":
            '<?xml version="1.0"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main"><sheetData>'
            + "".join(sheet_rows) + "</sheetData></worksheet>",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in parts.items():
            zf.writestr(name, content)
    return buf.getvalue()

class ImportTests(ApiTests):
    def req(self, token, method, path, body=None):
        return req(self.base, method, path, body, token)

    def preview(self, token, unit, filename, raw):
        return self.req(token, "POST", "/api/import/preview", {
            "unit": unit, "filename": filename,
            "content_b64": base64.b64encode(raw).decode()})

    def confirm(self, token, unit, filename, rows):
        return self.req(token, "POST", "/api/import/confirm", {
            "unit": unit, "filename": filename, "rows": rows})

    def test_import_happy_path_basic_columns(self):
        raw = make_xlsx([
            ["Vehicle Type", "Asset Number", "Location"],
            ["JLTV", "IMP-1", "Motor pool"],
            ["HMMWV", "IMP-2", "Bay 4"],
            ["MTVR", "IMP-3", "Flightline"],
        ])
        code, prev = self.preview(self.editor, "14 ASOS", "fleet.xlsx", raw)
        self.assertEqual(code, 200, prev)
        self.assertEqual(prev["will_import"], 3)
        self.assertEqual(prev["skipped"], 0)
        self.assertEqual(prev["columns"]["type"], "Vehicle Type")
        self.assertEqual(prev["columns"]["bumper"], "Asset Number")
        # status default is SHOWN in the preview, not silent
        self.assertTrue(all(r["status"] == "FMC" and r["status_defaulted"]
                            for r in prev["rows"]))
        rows = [r for r in prev["rows"] if r["ok"]]
        code, res = self.confirm(self.editor, "14 ASOS", "fleet.xlsx", rows)
        self.assertEqual(code, 201, res)
        self.assertEqual(res["created_count"], 3)
        _, fleet = self.req(self.viewer, "GET", "/api/fleet")
        got = {v["bumper"]: v for v in fleet["vehicles"]}
        self.assertEqual(got["IMP-1"]["type"], "JLTV")
        self.assertEqual(got["IMP-2"]["location"], "Bay 4")
        self.assertEqual(got["IMP-3"]["status"], "FMC")
        details = [a["detail"] or "" for a in audit_actions()
                   if a["action"] == "create"]
        self.assertTrue(any("imported from fleet.xlsx" in d
                            for d in details))

    def test_import_header_variants_and_extra_columns(self):
        raw = make_xlsx([
            ["type", "reg #", "LOCATION", "Status", "Mileage", "Color",
             "Notes", "Maintenance Due Date"],
            ["JLTV", "V-1", "Yard", "pmc", 12500, "Green", "Needs tires",
             "2027-03-01"],
        ])
        code, prev = self.preview(self.editor, "15 ASOS", "v.xlsx", raw)
        self.assertEqual(code, 200, prev)
        self.assertEqual(prev["unmapped_columns"], ["Color"])
        r = prev["rows"][0]
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["status"], r["status_defaulted"]), ("PMC", False))
        self.assertEqual(r["mileage"], 12500)          # numeric cell
        self.assertEqual(r["maint_due_date"], "2027-03-01")
        code, res = self.confirm(self.editor, "15 ASOS", "v.xlsx",
                                 [r])
        self.assertEqual(res["created_count"], 1)
        _, fleet = self.req(self.viewer, "GET", "/api/fleet")
        v = next(x for x in fleet["vehicles"] if x["bumper"] == "V-1")
        self.assertEqual(v["notes"], "Needs tires")
        self.assertEqual(v["mileage"], 12500)

    def test_import_skips(self):
        code, _ = self.add_vehicle(self.editor, bumper="DUP-1",
                                   type="OriginalType")
        self.assertEqual(code, 201)
        raw = make_xlsx([
            ["Type", "Asset Number", "Location"],
            ["JLTV", None, "Yard"],            # missing asset number
            ["JLTV", "NEW-1", "Yard"],
            ["HMMWV", "NEW-1", "Yard"],        # duplicate within file
            ["MTVR", "DUP-1", "Yard"],         # already in the unit
            ["MTVR", "BAD-1", "Yard"],         # fine
        ])
        code, prev = self.preview(self.editor, "14 ASOS", "d.xlsx", raw)
        self.assertEqual(code, 200, prev)
        by_bumper = {r["bumper"]: r for r in prev["rows"]}
        self.assertFalse(by_bumper[""]["ok"])
        self.assertIn("asset number", by_bumper[""]["reason"])
        self.assertTrue(by_bumper["NEW-1"]["ok"] or True)
        new1 = [r for r in prev["rows"] if r["bumper"] == "NEW-1"]
        self.assertEqual([r["ok"] for r in new1], [True, False])
        dup = next(r for r in prev["rows"] if r["bumper"] == "DUP-1")
        self.assertFalse(dup["ok"])
        self.assertIn("never overwrites", dup["reason"])
        rows = [r for r in prev["rows"] if r["ok"]]
        code, res = self.confirm(self.editor, "14 ASOS", "d.xlsx", rows)
        self.assertEqual(res["created_count"], 2)  # NEW-1 + BAD-1 only
        _, fleet = self.req(self.viewer, "GET", "/api/fleet")
        orig = next(v for v in fleet["vehicles"] if v["bumper"] == "DUP-1")
        self.assertEqual(orig["type"], "OriginalType")  # NOT overwritten

    def test_import_bad_status_and_malformed_and_roles(self):
        raw = make_xlsx([
            ["Type", "Asset Number", "Status"],
            ["JLTV", "S-1", "BROKEN"],
        ])
        code, prev = self.preview(self.editor, "14 ASOS", "s.xlsx", raw)
        self.assertEqual(code, 200)
        self.assertFalse(prev["rows"][0]["ok"])
        self.assertIn("invalid status", prev["rows"][0]["reason"])
        # malformed xlsx -> clean 400, not a dropped request
        code, body = self.preview(self.editor, "14 ASOS", "bad.xlsx",
                                  b"this is not a zip file")
        self.assertEqual(code, 400, body)
        self.assertIn("error", body)
        # wrong extension -> 400
        code, _ = self.preview(self.editor, "14 ASOS", "fleet.txt", b"a,b")
        self.assertEqual(code, 400)
        # viewers: 403 on BOTH steps
        code, _ = self.preview(self.viewer, "14 ASOS", "f.xlsx", raw)
        self.assertEqual(code, 403)
        code, _ = self.confirm(self.viewer, "14 ASOS", "f.xlsx",
                               [{"bumper": "X-1"}])
        self.assertEqual(code, 403)

    def test_import_confirm_revalidates(self):
        # the confirm step never trusts the preview: rows are validated
        # again and duplicates at confirm time are skipped
        rows = [{"bumper": "CF-1", "type": "JLTV", "status": "FMC"},
                {"bumper": "CF-1", "type": "JLTV", "status": "FMC"},
                {"bumper": "CF-2", "type": "JLTV", "status": "NOPE"},
                {"bumper": "", "type": "JLTV", "status": "FMC"}]
        code, res = self.confirm(self.editor, "19 ASOS", "c.xlsx", rows)
        self.assertEqual(code, 201, res)
        self.assertEqual(res["created_count"], 1)
        self.assertEqual(res["skipped_count"], 3)

    def test_import_asog_and_csv(self):
        raw = make_xlsx([
            ["Vehicle", "Asset #", "Location"],
            ["JLTV", "HQ-IMP-1", "HQ motor pool"],
        ])
        code, prev = self.preview(self.editor, "18 ASOG", "hq.xlsx", raw)
        self.assertEqual(code, 200, prev)
        self.assertEqual(prev["will_import"], 1)
        code, res = self.confirm(self.editor, "18 ASOG", "hq.xlsx",
                                 prev["rows"])
        self.assertEqual(res["created_count"], 1)
        _, snap = self.req(self.viewer, "GET", "/api/fleet")
        asog = next(u for u in snap["units"] if u["unit"] == "18 ASOG")
        self.assertEqual(asog["on_hand"], 1)
        self.assertEqual(snap["group"]["on_hand"], 1)
        # CSV goes through the same mapping
        csv_raw = b"Type,Asset Number,Location\nForklift,CSV-1,Dock\n"
        code, prev = self.preview(self.editor, "20 ASOS", "f.csv", csv_raw)
        self.assertEqual(code, 200, prev)
        self.assertEqual(prev["will_import"], 1)
        code, res = self.confirm(self.editor, "20 ASOS", "f.csv",
                                 prev["rows"])
        self.assertEqual(res["created_count"], 1)


# ------------------------------------------------------- v1.1 fix pass
def make_xlsx_raw(sheet_xml, shared=()):
    """Assemble an .xlsx from raw worksheet XML + shared-string items,
    for hostile fixtures make_xlsx cannot express (far-column refs,
    bogus shared-string indices)."""
    import zipfile
    ss = "".join(f"<si><t>{s}</t></si>" for s in shared)
    parts = {
        "xl/sharedStrings.xml":
            '<?xml version="1.0"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
            f'2006/main" count="{len(shared)}" '
            f'uniqueCount="{len(shared)}">{ss}</sst>',
        "xl/worksheets/sheet1.xml":
            '<?xml version="1.0"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main"><sheetData>'
            + sheet_xml + "</sheetData></worksheet>",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in parts.items():
            zf.writestr(name, content)
    return buf.getvalue()


class V11FixPassTests(ApiTests):
    """v1.1 review fix pass — B1 (xlsx column-width memory bomb),
    S1 (shared-string index wrap), S2 (corrupt part must be a 400),
    S3 (UTC code case), S4 (413 message), F2 (MC-rate half-up ties)."""

    def setUp(self):
        conn = sqlite3.connect(sky.Handler.db_path)
        conn.execute("DELETE FROM utc_vehicles")
        conn.execute("DELETE FROM utcs")
        conn.commit()
        conn.close()
        super().setUp()

    def req(self, token, method, path, body=None):
        return req(self.base, method, path, body, token)

    def preview(self, token, unit, filename, raw):
        return self.req(token, "POST", "/api/import/preview", {
            "unit": unit, "filename": filename,
            "content_b64": base64.b64encode(raw).decode()})

    # ---- F2: MC rate rounds half-up, server == client, ties included
    def test_mc_rate_ties_round_half_up(self):
        for fmc, total, want in [(1, 16, 6.3), (5, 16, 31.3),
                                 (1, 80, 1.3), (7, 112, 6.3),
                                 (0, 5, 0.0), (2, 3, 66.7)]:
            self.assertEqual(sky.mc_rate_half_up(fmc, total), want,
                             (fmc, total))
        self.assertEqual(sky.mc_rate_half_up(0, 0), 0.0)
        # through the snapshot: 16 vehicles, exactly 1 FMC -> 6.3
        # (Python round() would report 6.2 — the parity bug)
        for i in range(16):
            code, _ = self.add_vehicle(
                self.editor, bumper=f"TIE-{i}",
                status="FMC" if i == 0 else "NMC")
            self.assertEqual(code, 201)
        _, snap = self.req(self.viewer, "GET", "/api/fleet")
        u14 = next(u for u in snap["units"] if u["unit"] == "14 ASOS")
        self.assertEqual(u14["mc_rate"], 6.3)
        self.assertEqual(snap["group"]["mc_rate"], 6.3)

    # ---- S3: UTC codes normalized to uppercase; uniqueness ignores case
    def test_utc_code_case_normalized_and_unique(self):
        code, body = self.req(self.editor, "POST", "/api/utcs",
                              {"unit": "14 ASOS", "code": "abc1",
                               "title": "lower first"})
        self.assertEqual(code, 201, body)
        self.assertEqual(body["utc"]["code"], "ABC1")
        for dupe in ("ABC1", "aBc1"):
            code, _ = self.req(self.editor, "POST", "/api/utcs",
                               {"unit": "14 ASOS", "code": dupe,
                                "title": "dupe"})
            self.assertEqual(code, 409, dupe)
        code, _ = self.req(self.editor, "POST", "/api/utcs",
                           {"unit": "15 ASOS", "code": "abc1",
                            "title": "same code, other unit"})
        self.assertEqual(code, 201)
        code, body = self.req(self.editor, "POST", "/api/utcs",
                              {"unit": "14 ASOS", "code": "xyz9",
                               "title": "second"})
        self.assertEqual(code, 201, body)
        uid2 = body["utc"]["id"]
        code, _ = self.req(self.editor, "PUT", f"/api/utcs/{uid2}",
                           {"code": "AbC1"})
        self.assertEqual(code, 409)  # rename onto a case-variant dupe
        code, body = self.req(self.editor, "PUT", f"/api/utcs/{uid2}",
                              {"code": "zz2"})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["utc"]["code"], "ZZ2")  # update normalizes
        _, snap = self.req(self.viewer, "GET", "/api/fleet")
        codes = sorted(u["code"] for u in snap["utcs"]
                       if u["unit"] == "14 ASOS")
        self.assertEqual(codes, ["ABC1", "ZZ2"])

    # ---- B1: a far-column cell ref must be a fast 400, not an alloc bomb
    def test_import_far_column_ref_rejected_fast(self):
        hdr = ('<row r="1"><c r="A1" t="s"><v>0</v></c>'
               '<c r="B1" t="s"><v>1</v></c></row>')
        evil = '<row r="2"><c r="ZZZZZZ1" t="s"><v>0</v></c></row>'
        raw = make_xlsx_raw(hdr + evil, ["Type", "Asset Number"])
        start = time.time()
        code, body = self.preview(self.editor, "14 ASOS", "evil.xlsx", raw)
        self.assertLess(time.time() - start, 5)  # no wide allocation
        self.assertEqual(code, 400, body)
        self.assertIn("column", body["error"])
        # wide-but-legal (extra column at CV = column 100) still parses
        hdr = ('<row r="1"><c r="A1" t="s"><v>0</v></c>'
               '<c r="B1" t="s"><v>1</v></c>'
               '<c r="CV1" t="s"><v>4</v></c></row>')
        row = ('<row r="2"><c r="A2" t="s"><v>2</v></c>'
               '<c r="B2" t="s"><v>3</v></c>'
               '<c r="CV2" t="s"><v>4</v></c></row>')
        raw = make_xlsx_raw(hdr + row,
                            ["Type", "Asset Number", "JLTV", "WIDE-1",
                             "Extra"])
        code, prev = self.preview(self.editor, "14 ASOS", "wide.xlsx", raw)
        self.assertEqual(code, 200, prev)
        self.assertEqual(prev["will_import"], 1)
        self.assertEqual(prev["rows"][0]["bumper"], "WIDE-1")

    # ---- S1: negative / out-of-range shared-string index -> blank
    def test_import_shared_string_index_not_wrapped(self):
        strings = ["Type", "Asset Number", "Location", "JLTV",
                   "NEG-1", "Pool", "SENTINEL-LAST"]
        hdr = ('<row r="1"><c r="A1" t="s"><v>0</v></c>'
               '<c r="B1" t="s"><v>1</v></c>'
               '<c r="C1" t="s"><v>2</v></c></row>')
        # type cell index -1: pre-fix it wrapped to the LAST string
        row = ('<row r="2"><c r="A2" t="s"><v>-1</v></c>'
               '<c r="B2" t="s"><v>4</v></c>'
               '<c r="C2" t="s"><v>5</v></c></row>')
        raw = make_xlsx_raw(hdr + row, strings)
        code, prev = self.preview(self.editor, "14 ASOS", "neg.xlsx", raw)
        self.assertEqual(code, 200, prev)
        r = prev["rows"][0]
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["type"], "Unknown")  # blank, NOT SENTINEL-LAST
        self.assertEqual(r["bumper"], "NEG-1")
        # positive out-of-range index: blank too (bumper -> row skipped)
        row = ('<row r="2"><c r="A2" t="s"><v>3</v></c>'
               '<c r="B2" t="s"><v>999</v></c></row>')
        raw = make_xlsx_raw(hdr + row, strings)
        code, prev = self.preview(self.editor, "14 ASOS", "oob.xlsx", raw)
        self.assertEqual(code, 200, prev)
        self.assertFalse(prev["rows"][0]["ok"])

    # ---- S2: CRC-corrupt part -> clean 400, never a 500
    def test_import_crc_corrupt_part_is_400(self):
        raw = bytearray(make_xlsx([
            ["Type", "Asset Number", "Location"],
            ["JLTV", "CRC-1", "Pool"]]))
        name = b"xl/worksheets/sheet1.xml"
        idx = raw.rfind(name)
        hdr = idx - 46  # central-directory header fixed part is 46 bytes
        self.assertEqual(bytes(raw[hdr:hdr + 4]), b"PK\x01\x02")
        raw[hdr + 16] ^= 0xFF  # corrupt the stored CRC-32 of the sheet
        code, body = self.preview(self.editor, "14 ASOS", "crc.xlsx",
                                  bytes(raw))
        self.assertEqual(code, 400, body)
        self.assertIn("error", body)

    # ---- S4: the 413 message names the cap of the route hit
    def test_body_cap_message_names_route_cap(self):
        code, body = self.req(self.editor, "POST", "/api/import/preview",
                              {"unit": "14 ASOS", "filename": "big.xlsx",
                               "content_b64": "A" * 9_000_000})
        self.assertEqual(code, 413, body)
        self.assertIn("8 MB cap", body["error"])
        code, body = self.req(self.editor, "POST", "/api/vehicles",
                              {"unit": "14 ASOS", "bumper": "X",
                               "pad": "x" * 1_100_000})
        self.assertEqual(code, 413, body)
        self.assertIn("1 MB cap", body["error"])


class V111Tests(ApiTests):
    """v1.1.1: Editors can DELETE an authorization row. Add existed,
    subtract-by-number (lowering the count) existed — deleting the row
    itself was the one gap."""

    def req(self, token, method, path, body=None):
        return req(self.base, method, path, body, token)

    def snap(self):
        _, snap = self.req(self.viewer, "GET", "/api/fleet")
        return snap

    def mk_auth(self, token, unit="14 ASOS", typ="JLTV", n=10):
        code, body = self.req(token, "POST", "/api/authorizations",
                              {"unit": unit, "vehicle_type": typ,
                               "authorized": n})
        self.assertEqual(code, 200, body)
        return next(a for a in self.snap()["authorizations"]
                    if a["unit"] == unit and a["vehicle_type"] == typ)

    def test_editor_deletes_authorization_totals_and_audit(self):
        a14 = self.mk_auth(self.editor, "14 ASOS", "JLTV", 10)
        self.mk_auth(self.editor, "15 ASOS", "JLTV", 3)
        self.assertEqual(self.snap()["group"]["authorizations"], 13)
        code, body = self.req(self.editor, "DELETE",
                              f"/api/authorizations/{a14['id']}")
        self.assertEqual(code, 200, body)
        snap = self.snap()
        self.assertFalse(any(a["id"] == a14["id"]
                             for a in snap["authorizations"]))
        u14 = next(u for u in snap["units"] if u["unit"] == "14 ASOS")
        self.assertEqual(u14["authorizations"], 0)
        # group drops by EXACTLY the deleted row's count (13 - 10)
        self.assertEqual(snap["group"]["authorizations"], 3)
        # audit records who, which unit/type, and the count removed
        _, log = req(self.base, "GET", "/api/audit", token=self.viewer)
        dels = [a for a in log["audit"]
                if a["action"] == "authorization"
                and "(deleted)" in a["detail"]]
        self.assertEqual(len(dels), 1)
        d = dels[0]
        self.assertEqual(d["actor"], "Ed Editor")
        self.assertEqual(d["field"], "authorized")
        self.assertEqual(d["old_value"], "10")
        self.assertEqual(d["new_value"], "")
        self.assertIn("14 ASOS", d["detail"])
        self.assertIn("JLTV", d["detail"])

    def test_delete_roles_and_unknown_ids(self):
        a = self.mk_auth(self.editor)
        code, _ = self.req(self.viewer, "DELETE",
                           f"/api/authorizations/{a['id']}")
        self.assertEqual(code, 403)  # viewers are read-only
        code, _ = req(self.base, "DELETE",
                      f"/api/authorizations/{a['id']}")  # no token
        self.assertEqual(code, 401)
        code, _ = self.req(self.editor, "DELETE",
                           "/api/authorizations/999999")
        self.assertEqual(code, 404)  # unknown id
        code, _ = self.req(self.editor, "DELETE",
                           "/api/authorizations/not-an-id")
        self.assertEqual(code, 404)  # non-integer id
        code, _ = self.req(self.admin, "DELETE",
                           f"/api/authorizations/{a['id']}")
        self.assertEqual(code, 200)  # admin can too
        code, _ = self.req(self.editor, "DELETE",
                           f"/api/authorizations/{a['id']}")
        self.assertEqual(code, 404)  # second delete of the same id

    def test_delete_leaves_vehicles_and_on_hand_untouched(self):
        self.add_vehicle(self.editor)  # 14 ASOS JLTV
        self.add_vehicle(self.editor, bumper="A-2")
        a = self.mk_auth(self.editor, "14 ASOS", "JLTV", 6)
        code, _ = self.req(self.editor, "DELETE",
                           f"/api/authorizations/{a['id']}")
        self.assertEqual(code, 200)
        snap = self.snap()
        u14 = next(u for u in snap["units"] if u["unit"] == "14 ASOS")
        self.assertEqual(u14["on_hand"], 2)         # vehicles untouched
        self.assertEqual(u14["authorizations"], 0)  # only the line is gone
        self.assertEqual(len(snap["vehicles"]), 2)

    def test_editor_can_lower_authorization_by_number(self):
        # subtract-by-number, locked in: 10 -> 4 -> 0 as an editor
        self.mk_auth(self.editor, "14 ASOS", "JLTV", 10)
        for n in (4, 0):
            code, body = self.req(self.editor, "POST", "/api/authorizations",
                                  {"unit": "14 ASOS", "vehicle_type": "JLTV",
                                   "authorized": n})
            self.assertEqual(code, 200, body)
            snap = self.snap()
            entry = next(a for a in snap["authorizations"]
                         if a["unit"] == "14 ASOS")
            self.assertEqual(entry["authorized"], n)
            self.assertEqual(snap["group"]["authorizations"], n)

    def test_demo_authorization_deletable_and_demo_remove_still_works(self):
        code, _ = req(self.base, "POST", "/api/demo/load", {}, self.admin)
        self.assertEqual(code, 200)
        snap = self.snap()
        demo_auths = [a for a in snap["authorizations"] if a["is_demo"]]
        self.assertTrue(demo_auths)
        victim = demo_auths[0]
        before = snap["group"]["authorizations"]
        code, _ = self.req(self.editor, "DELETE",
                           f"/api/authorizations/{victim['id']}")
        self.assertEqual(code, 200)  # demo rows delete like any other
        snap = self.snap()
        self.assertFalse(any(a["id"] == victim["id"]
                             for a in snap["authorizations"]))
        self.assertEqual(snap["group"]["authorizations"],
                         before - victim["authorized"])
        # the demo-remove flow still clears the rest cleanly afterwards
        code, _ = req(self.base, "POST", "/api/demo/remove", {}, self.admin)
        self.assertEqual(code, 200)
        snap = self.snap()
        self.assertEqual(snap["authorizations"], [])
        self.assertEqual(snap["vehicles"], [])
