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
            for t in ("sessions", "conflicts", "discrepancy_photos",
                      "discrepancies", "inspection_items", "inspections",
                      "audit", "vehicles", "authorizations", "users"):
                conn.execute(f"DELETE FROM {t}")
            conn.commit()
        finally:
            conn.close()
        _, adm = req(self.base, "POST", "/api/setup",
                     {"username": "boss", "password": "vco-pass-1",
                      "display_name": "Boss", "unit": "18 ASOG"})
        self.assertEqual(adm.get("user", {}).get("role"), "vco")
        _, login = req(self.base, "POST", "/api/login",
                       {"username": "boss", "password": "vco-pass-1"})
        self.vco = login["token"]
        req(self.base, "POST", "/api/users",
            {"username": "vic", "password": "operator-pass-1",
             "display_name": "Vic Operator", "unit": "14 ASOS",
             "role": "operator"}, self.vco)
        _, lv = req(self.base, "POST", "/api/login",
                    {"username": "vic", "password": "operator-pass-1"})
        self.operator = lv["token"]

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

    def test_operator_cannot_create_or_edit_vehicles(self):
        code, _ = self.add_vehicle(self.operator)
        self.assertEqual(code, 403)
        code, v = self.add_vehicle(self.vco)
        self.assertEqual(code, 201)
        vid = v["vehicle"]["id"]
        code, _ = req(self.base, "PUT", f"/api/vehicles/{vid}",
                      {"status": "NMC"}, self.operator)
        self.assertEqual(code, 403)

    def test_vco_can_create_and_edit(self):
        code, v = self.add_vehicle(self.vco)
        self.assertEqual(code, 201)
        vid = v["vehicle"]["id"]
        code, r = req(self.base, "PUT", f"/api/vehicles/{vid}",
                      {"status": "PMC", "base_status": "FMC"}, self.vco)
        self.assertEqual(code, 200)
        self.assertEqual(r["vehicle"]["status"], "PMC")

    # ------------------------------------------------ math / derivation
    def test_mc_rate_and_on_hand_derivation(self):
        self.add_vehicle(self.vco, bumper="A-1", status="FMC")
        self.add_vehicle(self.vco, bumper="A-2", status="FMC")
        self.add_vehicle(self.vco, bumper="A-3", status="PMC")
        self.add_vehicle(self.vco, bumper="A-4", status="NMC")
        req(self.base, "POST", "/api/authorizations",
            {"unit": "14 ASOS", "vehicle_type": "JLTV", "authorized": 6},
            self.vco)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        st = next(u for u in fleet["units"] if u["unit"] == "14 ASOS")
        self.assertEqual(st["on_hand"], 4)          # derived from records
        self.assertEqual(st["authorizations"], 6)
        self.assertEqual(st["mc_rate"], 50.0)       # 2 FMC / 4 on hand
        grp = fleet["group"]                        # v1.1: separate entry
        self.assertEqual(grp["on_hand"], 4)         # group rollup
        self.assertEqual(grp["mc_rate"], 50.0)

    # ------------------------------------------------ audit
    def test_audit_entries_written(self):
        _, v = self.add_vehicle(self.vco)
        vid = v["vehicle"]["id"]
        req(self.base, "PUT", f"/api/vehicles/{vid}",
            {"notes": "needs tires", "base_status": "FMC"}, self.vco)
        _, log = req(self.base, "GET", "/api/audit", token=self.operator)
        actions = [(a["action"], a["field"]) for a in log["audit"]]
        self.assertIn(("create", ""), actions)
        self.assertIn(("update", "notes"), actions)
        note = next(a for a in log["audit"] if a["field"] == "notes")
        self.assertEqual(note["new_value"], "needs tires")
        self.assertEqual(note["actor"], "Boss")

    # ------------------------------------------------ sync conflict
    def test_conflicting_status_flagged_not_overwritten(self):
        _, v = self.add_vehicle(self.vco)  # status FMC
        vid = v["vehicle"]["id"]
        # someone else moves it FMC -> PMC first
        req(self.base, "PUT", f"/api/vehicles/{vid}",
            {"status": "PMC", "base_status": "FMC"}, self.vco)
        # an offline VCO, still seeing FMC, sets NMC and changes location
        code, r = req(self.base, "PUT", f"/api/vehicles/{vid}",
                      {"status": "NMC", "base_status": "FMC",
                       "location": "Field site"}, self.vco)
        self.assertEqual(code, 200)
        self.assertIn("conflict", r)
        self.assertEqual(r["vehicle"]["status"], "PMC")       # kept server's
        self.assertEqual(r["vehicle"]["location"], "Field site")  # merged
        _, cf = req(self.base, "GET", "/api/conflicts", token=self.vco)
        self.assertEqual(len(cf["conflicts"]), 1)
        self.assertEqual(cf["conflicts"][0]["attempted"], "NMC")
        _, log = req(self.base, "GET", "/api/audit", token=self.vco)
        self.assertTrue(any(a["is_conflict"] for a in log["audit"]))
        # VCO resolves by applying the attempted status
        cid = cf["conflicts"][0]["id"]
        code, _ = req(self.base, "POST", f"/api/conflicts/{cid}/resolve",
                      {"action": "apply"}, self.vco)
        self.assertEqual(code, 200)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        veh = next(x for x in fleet["vehicles"] if x["id"] == vid)
        self.assertEqual(veh["status"], "NMC")

    def test_demo_fleet_is_labeled_and_removable(self):
        code, _ = req(self.base, "POST", "/api/demo/load", {}, self.vco)
        self.assertEqual(code, 200)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        self.assertGreater(len(fleet["vehicles"]), 0)
        self.assertTrue(all(v["is_demo"] for v in fleet["vehicles"]))
        self.assertTrue(all("SAMPLE" in v["notes"]
                            for v in fleet["vehicles"]))
        req(self.base, "POST", "/api/demo/remove", {}, self.vco)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
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
    """Inherits ApiTests.setUp (fresh vco/operator per test)."""

    # ---- item 1: atomic first-run setup
    def test_parallel_setup_creates_exactly_one_vco(self):
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
            self.assertEqual(me["user"]["role"], "vco")
        finally:
            httpd.shutdown()

    # ---- item 2: numeric validation on insert AND update
    def test_numeric_fields_validated(self):
        for bad in ("abc", -5, 12.5, True):
            code, _ = self.add_vehicle(self.vco, mileage=bad)
            self.assertEqual(code, 400, f"mileage={bad!r}")
            code, _ = self.add_vehicle(self.vco, maint_due_miles=bad)
            self.assertEqual(code, 400, f"maint_due_miles={bad!r}")
        code, v = self.add_vehicle(self.vco, mileage="1200")
        self.assertEqual(code, 201)  # digit string coerced
        self.assertEqual(v["vehicle"]["mileage"], 1200)
        vid = v["vehicle"]["id"]
        code, _ = req(self.base, "PUT", f"/api/vehicles/{vid}",
                      {"mileage": -1}, self.vco)
        self.assertEqual(code, 400)
        code, _ = req(self.base, "PUT", f"/api/vehicles/{vid}",
                      {"maint_due_miles": 3.5}, self.vco)
        self.assertEqual(code, 400)

    # ---- item 3: group rollup sums PMC/NMC too
    def test_group_rollup_sums_all_statuses(self):
        self.add_vehicle(self.vco, unit="14 ASOS", bumper="G-1", status="PMC")
        self.add_vehicle(self.vco, unit="14 ASOS", bumper="G-2", status="NMC")
        self.add_vehicle(self.vco, unit="15 ASOS", bumper="G-3", status="PMC")
        self.add_vehicle(self.vco, unit="15 ASOS", bumper="G-4", status="FMC")
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        grp = fleet["group"]                        # v1.1: separate entry
        self.assertEqual(grp["on_hand"], 4)
        self.assertEqual(grp["pmc"], 2)
        self.assertEqual(grp["nmc"], 1)
        self.assertEqual(grp["fmc"], 1)
        self.assertEqual(grp["mc_rate"], 25.0)

    # ---- item 4: conflict resolve guards
    def _make_conflict(self):
        import uuid
        _, v = self.add_vehicle(self.vco,
                                bumper="CF-" + uuid.uuid4().hex[:8])
        vid = v["vehicle"]["id"]
        req(self.base, "PUT", f"/api/vehicles/{vid}",
            {"status": "PMC", "base_status": "FMC"}, self.vco)
        _, r = req(self.base, "PUT", f"/api/vehicles/{vid}",
                   {"status": "NMC", "base_status": "FMC"}, self.vco)
        return vid, r["conflict"]["id"]

    def test_conflict_resolve_guards(self):
        vid, cid = self._make_conflict()
        # bogus action -> 400 and no state change
        code, _ = req(self.base, "POST", f"/api/conflicts/{cid}/resolve",
                      {"action": "explode"}, self.vco)
        self.assertEqual(code, 400)
        _, cf = req(self.base, "GET", "/api/conflicts", token=self.vco)
        self.assertEqual(len(cf["conflicts"]), 1)
        # proper resolve works once…
        code, _ = req(self.base, "POST", f"/api/conflicts/{cid}/resolve",
                      {"action": "keep"}, self.vco)
        self.assertEqual(code, 200)
        # …and only once
        code, _ = req(self.base, "POST", f"/api/conflicts/{cid}/resolve",
                      {"action": "apply"}, self.vco)
        self.assertEqual(code, 409)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        veh = next(x for x in fleet["vehicles"] if x["id"] == vid)
        self.assertEqual(veh["status"], "PMC")  # keep won; re-resolve didn't apply

    # ---- item 5: demo-data hygiene
    def test_demo_never_overwrites_real_authorization(self):
        req(self.base, "POST", "/api/authorizations",
            {"unit": "14 ASOS", "vehicle_type": "JLTV", "authorized": 7},
            self.vco)
        req(self.base, "POST", "/api/demo/load", {}, self.vco)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        auth = next(a for a in fleet["authorizations"]
                    if a["unit"] == "14 ASOS" and a["vehicle_type"] == "JLTV")
        self.assertEqual(auth["authorized"], 7)   # real value untouched
        self.assertEqual(auth["is_demo"], 0)
        n_after_first = len(fleet["vehicles"])
        # idempotent: second load adds nothing
        req(self.base, "POST", "/api/demo/load", {}, self.vco)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        self.assertEqual(len(fleet["vehicles"]), n_after_first)
        # a real edit of a demo-flagged authorization clears its demo flag
        demo_auth = next(a for a in fleet["authorizations"] if a["is_demo"])
        req(self.base, "POST", "/api/authorizations",
            {"unit": demo_auth["unit"], "vehicle_type": demo_auth["vehicle_type"],
             "authorized": 9}, self.vco)
        # remove demo: real + real-edited authorizations survive
        req(self.base, "POST", "/api/demo/remove", {}, self.vco)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
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
        code, _ = self.add_vehicle(self.vco, unit="18 ASOG",
                                   bumper="HQ-0")
        self.assertEqual(code, 201)
        code, _ = self.add_vehicle(self.vco, unit="99 ASOS")
        self.assertEqual(code, 400)
        code, _ = self.add_vehicle(self.vco, bumper="  ")
        self.assertEqual(code, 400)
        code, _ = self.add_vehicle(self.vco, type="")
        self.assertEqual(code, 400)
        code, _ = self.add_vehicle(self.vco, status="BROKEN")
        self.assertEqual(code, 400)
        code, _ = self.add_vehicle(self.vco,
                                   maint_due_date="2026-13-40")
        self.assertEqual(code, 400)
        code, _ = self.add_vehicle(self.vco, maint_due_date="next week")
        self.assertEqual(code, 400)
        code, v = self.add_vehicle(self.vco, bumper="DUP-1")
        self.assertEqual(code, 201)
        code, _ = self.add_vehicle(self.vco, bumper="DUP-1")
        self.assertEqual(code, 409)  # dupe in same unit
        code, _ = self.add_vehicle(self.vco, unit="15 ASOS", bumper="DUP-1")
        self.assertEqual(code, 201)  # same bumper, other unit: fine
        # moving a vehicle onto an occupied bumper in the target unit
        _, v2 = self.add_vehicle(self.vco, unit="15 ASOS", bumper="MV-1")
        code, _ = req(self.base, "PUT", f"/api/vehicles/{v2['vehicle']['id']}",
                      {"unit": "14 ASOS", "bumper": "DUP-1"}, self.vco)
        self.assertEqual(code, 409)
        # authorizations (v1.1: 18 ASOG authorizations are valid too,
        # covered in V11Tests; a non-unit is not)
        for body, want in [
                ({"unit": "99 ASOS", "vehicle_type": "JLTV", "authorized": 3}, 400),
                ({"unit": "14 ASOS", "vehicle_type": "JLTV", "authorized": 3.5}, 400),
                ({"unit": "14 ASOS", "vehicle_type": "JLTV", "authorized": -1}, 400),
                ({"unit": "14 ASOS", "vehicle_type": "", "authorized": 3}, 400)]:
            code, _ = req(self.base, "POST", "/api/authorizations", body,
                          self.vco)
            self.assertEqual(code, want, body)
        # user affiliation must be one of the five units
        code, _ = req(self.base, "POST", "/api/users",
                      {"username": "zz", "password": "zz-pass-123",
                       "display_name": "Zed", "unit": "Narnia",
                       "role": "operator"}, self.vco)
        self.assertEqual(code, 400)

    # ---- item 7: no request dies without JSON; unknown ids 404
    def test_error_handling(self):
        code, body = req(self.base, "PUT", "/api/vehicles/abc",
                         {"status": "NMC"}, self.vco)
        self.assertEqual(code, 404)
        self.assertIn("error", body)
        code, body = req_raw(self.base, "POST", "/api/login", b"{not json")
        self.assertEqual(code, 400)
        self.assertIn("malformed", body["error"])
        code, body = req_raw(self.base, "POST", "/api/login",
                             b"x" * (1024 * 1024 + 10))
        self.assertEqual(code, 413)
        code, _ = req(self.base, "PUT", "/api/users/99999",
                      {"role": "vco"}, self.vco)
        self.assertEqual(code, 404)  # no ghost success
        code, _ = req(self.base, "DELETE", "/api/vehicles/99999",
                      token=self.vco)
        self.assertEqual(code, 404)
        code, _ = req(self.base, "POST", "/api/conflicts/99999/resolve",
                      {"action": "keep"}, self.vco)
        self.assertEqual(code, 404)

    # ---- item 8: session expiry / revocation / last-vco guard
    def test_session_revocation_and_last_vco(self):
        # role change revokes the target's sessions: promote vic to
        # VCO, and vic's pre-promotion token must die
        _, lv0 = req(self.base, "POST", "/api/login",
                     {"username": "vic", "password": "operator-pass-1"})
        code, _ = req(self.base, "PUT", "/api/users/2", {"role": "vco"},
                      self.vco)
        self.assertEqual(code, 200)
        code, _ = req(self.base, "GET", "/api/me", token=lv0["token"])
        self.assertEqual(code, 401)  # old token dead after role change
        # password reset revokes sessions too
        _, lv = req(self.base, "POST", "/api/login",
                    {"username": "vic", "password": "operator-pass-1"})
        code, _ = req(self.base, "PUT", f"/api/users/{2}",
                      {"password": "operator-pass-2"}, self.vco)
        self.assertEqual(code, 200)
        code, _ = req(self.base, "GET", "/api/me", token=lv["token"])
        self.assertEqual(code, 401)
        # demote vic back so boss is again the only VCO; then the last
        # VCO cannot be demoted (boss is the only vco)
        code, _ = req(self.base, "PUT", "/api/users/2",
                      {"role": "operator"}, self.vco)
        self.assertEqual(code, 200)
        _, users = req(self.base, "GET", "/api/users", token=self.vco)
        boss = next(u for u in users["users"] if u["username"] == "boss")
        code, _ = req(self.base, "PUT", f"/api/users/{boss['id']}",
                      {"role": "operator"}, self.vco)
        self.assertEqual(code, 400)
        # expired session is rejected
        conn = sky.connect(sky.Handler.db_path)
        conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
        conn.commit()
        conn.close()
        code, _ = req(self.base, "GET", "/api/me", token=self.vco)
        self.assertEqual(code, 401)

    # ---- item 9: audit correctness
    def test_audit_correctness(self):
        _, v = self.add_vehicle(self.vco, notes="orig")
        vid = v["vehicle"]["id"]
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        before = next(x for x in fleet["vehicles"] if x["id"] == vid)
        _, log = req(self.base, "GET", "/api/audit", token=self.operator)
        n_audit = len(log["audit"])
        # no-op PUT: no audit rows, updated_by/at untouched
        code, _ = req(self.base, "PUT", f"/api/vehicles/{vid}",
                      {"notes": "orig", "status": "FMC"}, self.vco)
        self.assertEqual(code, 200)
        _, log = req(self.base, "GET", "/api/audit", token=self.operator)
        self.assertEqual(len(log["audit"]), n_audit)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        after = next(x for x in fleet["vehicles"] if x["id"] == vid)
        self.assertEqual(after["updated_at"], before["updated_at"])
        # authorization audit carries real old -> new
        req(self.base, "POST", "/api/authorizations",
            {"unit": "14 ASOS", "vehicle_type": "HMMWV", "authorized": 5},
            self.vco)
        req(self.base, "POST", "/api/authorizations",
            {"unit": "14 ASOS", "vehicle_type": "HMMWV", "authorized": 8},
            self.vco)
        _, log = req(self.base, "GET", "/api/audit", token=self.operator)
        auth_rows = [a for a in log["audit"] if a["action"] == "authorization"]
        self.assertTrue(any(a["old_value"] == "5" and a["new_value"] == "8"
                            for a in auth_rows))
        # account update audit carries old -> new
        _, users = req(self.base, "GET", "/api/users", token=self.vco)
        vic = next(u for u in users["users"] if u["username"] == "vic")
        req(self.base, "PUT", f"/api/users/{vic['id']}",
            {"role": "vco"}, self.vco)
        # (vic's own session was revoked by that role change — use vco's)
        _, log = req(self.base, "GET", "/api/audit", token=self.vco)
        self.assertTrue(any(a["action"] == "account" and a["field"] == "role"
                            and a["old_value"] == "operator"
                            and a["new_value"] == "vco"
                            for a in log["audit"]))
        # deleting a vehicle closes its open conflicts (audited)
        vid2, cid2 = self._make_conflict()
        code, _ = req(self.base, "DELETE", f"/api/vehicles/{vid2}",
                      token=self.vco)
        self.assertEqual(code, 200)
        _, cf = req(self.base, "GET", "/api/conflicts", token=self.vco)
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
        self.add_vehicle(self.vco, bumper="CSV-1",
                         notes="=2+5 dangerous", location="@SUM(A1:A9)")
        code, body = req_raw(self.base, "GET", "/api/export.csv",
                             b"", token=self.operator)
        self.assertEqual(code, 200)
        text = body["_text"]
        self.assertIn("'=2+5 dangerous", text)
        self.assertIn("'@SUM(A1:A9)", text)

    # ---- item 11a: idempotent creates by client op UUID
    def test_create_dedup_by_op_id(self):
        body = {"unit": "14 ASOS", "bumper": "OP-1", "type": "JLTV",
                "status": "FMC", "op_id": "op-uuid-1234"}
        code, r1 = req(self.base, "POST", "/api/vehicles", body, self.vco)
        self.assertEqual(code, 201)
        code, r2 = req(self.base, "POST", "/api/vehicles", body, self.vco)
        self.assertEqual(code, 200)
        self.assertEqual(r1["vehicle"]["id"], r2["vehicle"]["id"])
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        self.assertEqual(
            len([v for v in fleet["vehicles"] if v["bumper"] == "OP-1"]), 1)

    # ---- item 16 (server half): snapshot carries includes_demo per unit
    def test_snapshot_includes_demo_flags(self):
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        self.assertFalse(any(u["includes_demo"] for u in fleet["units"]))
        req(self.base, "POST", "/api/demo/load", {}, self.vco)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
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
    """Inherits ApiTests.setUp (fresh vco/operator per test)."""

    def _db_count(self, sql, args=()):
        conn = sky.connect(sky.Handler.db_path)
        try:
            return conn.execute(sql, args).fetchone()[0]
        finally:
            conn.close()

    # ---- fix 1a: parallel identical creates -> exactly one wins
    def test_parallel_identical_creates_exactly_one(self):
        results = _parallel(10, lambda i: self.add_vehicle(
            self.vco, bumper="RACE-1"))
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
            self.vco, unit=units[i], bumper="MULTI-1"))
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
        _, v1 = self.add_vehicle(self.vco, unit="15 ASOS", bumper="MV-A")
        _, v2 = self.add_vehicle(self.vco, unit="19 ASOS", bumper="MV-B")
        ids = [v1["vehicle"]["id"], v2["vehicle"]["id"]]
        results = _parallel(2, lambda i: req(
            self.base, "PUT", f"/api/vehicles/{ids[i]}",
            {"unit": "14 ASOS", "bumper": "TGT-1"}, self.vco))
        codes = sorted(c for c, _ in results)
        self.assertEqual(codes, [200, 409], codes)
        self.assertEqual(self._db_count(
            "SELECT COUNT(*) FROM vehicles WHERE unit='14 ASOS'"
            " AND bumper='TGT-1'"), 1)

    # ---- fix 2: parallel resolves of one conflict -> resolve-once
    def _make_conflict2(self):
        _, v = self.add_vehicle(self.vco,
                                bumper="CF2-" + uuid.uuid4().hex[:8])
        vid = v["vehicle"]["id"]
        req(self.base, "PUT", f"/api/vehicles/{vid}",
            {"status": "PMC", "base_status": "FMC"}, self.vco)
        _, r = req(self.base, "PUT", f"/api/vehicles/{vid}",
                   {"status": "NMC", "base_status": "FMC"}, self.vco)
        return vid, r["conflict"]["id"]

    def test_parallel_conflict_resolve_exactly_once(self):
        vid, cid = self._make_conflict2()
        results = _parallel(10, lambda i: req(
            self.base, "POST", f"/api/conflicts/{cid}/resolve",
            {"action": "apply"}, self.vco))
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
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        veh = next(x for x in fleet["vehicles"] if x["id"] == vid)
        self.assertEqual(veh["status"], "NMC")

    # ---- fix 3: DELETE with a body, then reuse the SAME connection
    def test_delete_with_body_keeps_connection_usable(self):
        _, v = self.add_vehicle(self.vco, bumper="DEL-1")
        vid = v["vehicle"]["id"]
        port = int(self.base.rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            auth = {"Authorization": "Bearer " + self.vco,
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
                         headers={"Authorization": "Bearer " + self.operator})
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
            code, _ = self.add_vehicle(self.vco, unit="18 ASOG",
                                       bumper=bumper, status=status)
            self.assertEqual(code, 201)
        self.req(self.vco, "POST", "/api/authorizations",
                 {"unit": "18 ASOG", "vehicle_type": "JLTV", "authorized": 5})
        code, _ = self.add_vehicle(self.vco)  # 14 ASOS FMC default
        self.assertEqual(code, 201)
        _, snap = self.req(self.operator, "GET", "/api/fleet")
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
        code, _ = self.add_vehicle(self.vco, unit="18 ASOG", bumper="S-1")
        self.assertEqual(code, 201)
        code, _ = self.add_vehicle(self.vco, bumper="S-1")  # 14 ASOS ok
        self.assertEqual(code, 201)
        code, _ = self.add_vehicle(self.vco, unit="18 ASOG", bumper="S-1")
        self.assertEqual(code, 409)  # duplicate within ASOG
        # update an ASOG vehicle's unit to a squadron and back
        _, fleet = self.req(self.operator, "GET", "/api/fleet")
        hq = next(v for v in fleet["vehicles"] if v["unit"] == "18 ASOG")
        code, body = self.req(self.vco, "PUT", f"/api/vehicles/{hq['id']}",
                              {"unit": "20 ASOS"})
        self.assertEqual(code, 200, body)

    def test_authorizations_all_five_units(self):
        for unit in sky.UNIT_NAMES:
            code, body = self.req(
                self.vco, "POST", "/api/authorizations",
                {"unit": unit, "vehicle_type": "HMMWV", "authorized": 7})
            self.assertEqual(code, 200, (unit, body))
        code, _ = self.req(self.operator, "POST", "/api/authorizations",
                           {"unit": "18 ASOG", "vehicle_type": "X",
                            "authorized": 1})
        self.assertEqual(code, 403)  # operators stay read-only
        _, snap = self.req(self.operator, "GET", "/api/fleet")
        for unit in sky.UNIT_NAMES:
            entry = next(a for a in snap["authorizations"]
                         if a["unit"] == unit)
            self.assertEqual(entry["authorized"], 7)
        self.assertEqual(snap["group"]["authorizations"], 35)

    def test_utc_crud_membership_rules(self):
        _, v1 = self.add_vehicle(self.vco)                     # 14 ASOS
        _, v2 = self.add_vehicle(self.vco, bumper="B-2")
        code, other = self.add_vehicle(self.vco, unit="15 ASOS",
                                       bumper="C-1")
        self.assertEqual(code, 201)
        code, utc = self.mk_utc(self.vco)
        self.assertEqual(code, 201, utc)
        uid = utc["utc"]["id"]
        # operators cannot create or modify UTCs
        self.assertEqual(self.mk_utc(self.operator, code="ZZZ")[0], 403)
        # duplicate code in the same unit -> 409; same code elsewhere ok
        self.assertEqual(self.mk_utc(self.vco)[0], 409)
        self.assertEqual(self.mk_utc(self.vco, unit="15 ASOS")[0], 201)
        # membership: own unit ok, other unit 400, nonexistent 400
        code, _ = self.req(self.vco, "POST", f"/api/utcs/{uid}/vehicles",
                           {"vehicle_id": v1["vehicle"]["id"]})
        self.assertEqual(code, 200)
        code, _ = self.req(self.vco, "POST", f"/api/utcs/{uid}/vehicles",
                           {"vehicle_id": v2["vehicle"]["id"]})
        self.assertEqual(code, 200)
        code, body = self.req(self.vco, "POST",
                              f"/api/utcs/{uid}/vehicles",
                              {"vehicle_id": other["vehicle"]["id"]})
        self.assertEqual(code, 400, body)  # wrong unit
        code, _ = self.req(self.vco, "POST", f"/api/utcs/{uid}/vehicles",
                           {"vehicle_id": 999999})
        self.assertEqual(code, 400)
        code, _ = self.req(self.operator, "POST", f"/api/utcs/{uid}/vehicles",
                           {"vehicle_id": v1["vehicle"]["id"]})
        self.assertEqual(code, 403)
        # a vehicle may sit in MULTIPLE UTCs
        code, utc2 = self.mk_utc(self.vco, code="XFFC2")
        self.assertEqual(code, 201)
        code, _ = self.req(self.vco, "POST",
                           f"/api/utcs/{utc2['utc']['id']}/vehicles",
                           {"vehicle_id": v1["vehicle"]["id"]})
        self.assertEqual(code, 200)
        # snapshot carries UTCs + membership for offline viewing
        _, snap = self.req(self.operator, "GET", "/api/fleet")
        u = next(x for x in snap["utcs"] if x["id"] == uid)
        self.assertEqual(sorted(u["vehicle_ids"]),
                         sorted([v1["vehicle"]["id"], v2["vehicle"]["id"]]))
        # update code/title (audited)
        code, body = self.req(self.vco, "PUT", f"/api/utcs/{uid}",
                              {"code": "XFFC9", "title": "Renamed"})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["utc"]["code"], "XFFC9")
        # remove a member explicitly
        code, _ = self.req(self.vco, "DELETE",
                           f"/api/utcs/{uid}/vehicles/{v2['vehicle']['id']}")
        self.assertEqual(code, 200)
        # deleting a VEHICLE drops its memberships
        code, _ = self.req(self.vco, "DELETE", f"/api/vehicles/{v1['vehicle']['id']}")
        self.assertEqual(code, 200)
        _, snap = self.req(self.operator, "GET", "/api/fleet")
        u = next(x for x in snap["utcs"] if x["id"] == uid)
        self.assertEqual(u["vehicle_ids"], [])
        # deleting the UTC keeps the vehicles
        code, _ = self.req(self.vco, "DELETE", f"/api/utcs/{uid}")
        self.assertEqual(code, 200)
        _, snap = self.req(self.operator, "GET", "/api/fleet")
        self.assertFalse(any(x["id"] == uid for x in snap["utcs"]))
        self.assertTrue(any(v["bumper"] == "B-2" for v in snap["vehicles"]))
        acts = {a["action"] for a in audit_actions()}
        for want in ("utc-create", "utc-update", "utc-add-vehicle",
                     "utc-remove-vehicle", "utc-delete"):
            self.assertIn(want, acts)

    def test_utc_validation(self):
        code, _ = self.mk_utc(self.vco, unit="99 ASOS")
        self.assertEqual(code, 400)
        code, _ = self.mk_utc(self.vco, code="  ")
        self.assertEqual(code, 400)
        code, _ = self.mk_utc(self.vco, code="X" * 21)
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
        code, prev = self.preview(self.vco, "14 ASOS", "fleet.xlsx", raw)
        self.assertEqual(code, 200, prev)
        self.assertEqual(prev["will_import"], 3)
        self.assertEqual(prev["skipped"], 0)
        self.assertEqual(prev["columns"]["type"], "Vehicle Type")
        self.assertEqual(prev["columns"]["bumper"], "Asset Number")
        # status default is SHOWN in the preview, not silent
        self.assertTrue(all(r["status"] == "FMC" and r["status_defaulted"]
                            for r in prev["rows"]))
        rows = [r for r in prev["rows"] if r["ok"]]
        code, res = self.confirm(self.vco, "14 ASOS", "fleet.xlsx", rows)
        self.assertEqual(code, 201, res)
        self.assertEqual(res["created_count"], 3)
        _, fleet = self.req(self.operator, "GET", "/api/fleet")
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
        code, prev = self.preview(self.vco, "15 ASOS", "v.xlsx", raw)
        self.assertEqual(code, 200, prev)
        self.assertEqual(prev["unmapped_columns"], ["Color"])
        r = prev["rows"][0]
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["status"], r["status_defaulted"]), ("PMC", False))
        self.assertEqual(r["mileage"], 12500)          # numeric cell
        self.assertEqual(r["maint_due_date"], "2027-03-01")
        code, res = self.confirm(self.vco, "15 ASOS", "v.xlsx",
                                 [r])
        self.assertEqual(res["created_count"], 1)
        _, fleet = self.req(self.operator, "GET", "/api/fleet")
        v = next(x for x in fleet["vehicles"] if x["bumper"] == "V-1")
        self.assertEqual(v["notes"], "Needs tires")
        self.assertEqual(v["mileage"], 12500)

    def test_import_skips(self):
        code, _ = self.add_vehicle(self.vco, bumper="DUP-1",
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
        code, prev = self.preview(self.vco, "14 ASOS", "d.xlsx", raw)
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
        code, res = self.confirm(self.vco, "14 ASOS", "d.xlsx", rows)
        self.assertEqual(res["created_count"], 2)  # NEW-1 + BAD-1 only
        _, fleet = self.req(self.operator, "GET", "/api/fleet")
        orig = next(v for v in fleet["vehicles"] if v["bumper"] == "DUP-1")
        self.assertEqual(orig["type"], "OriginalType")  # NOT overwritten

    def test_import_bad_status_and_malformed_and_roles(self):
        raw = make_xlsx([
            ["Type", "Asset Number", "Status"],
            ["JLTV", "S-1", "BROKEN"],
        ])
        code, prev = self.preview(self.vco, "14 ASOS", "s.xlsx", raw)
        self.assertEqual(code, 200)
        self.assertFalse(prev["rows"][0]["ok"])
        self.assertIn("invalid status", prev["rows"][0]["reason"])
        # malformed xlsx -> clean 400, not a dropped request
        code, body = self.preview(self.vco, "14 ASOS", "bad.xlsx",
                                  b"this is not a zip file")
        self.assertEqual(code, 400, body)
        self.assertIn("error", body)
        # wrong extension -> 400
        code, _ = self.preview(self.vco, "14 ASOS", "fleet.txt", b"a,b")
        self.assertEqual(code, 400)
        # operators: 403 on BOTH steps
        code, _ = self.preview(self.operator, "14 ASOS", "f.xlsx", raw)
        self.assertEqual(code, 403)
        code, _ = self.confirm(self.operator, "14 ASOS", "f.xlsx",
                               [{"bumper": "X-1"}])
        self.assertEqual(code, 403)

    def test_import_confirm_revalidates(self):
        # the confirm step never trusts the preview: rows are validated
        # again and duplicates at confirm time are skipped
        rows = [{"bumper": "CF-1", "type": "JLTV", "status": "FMC"},
                {"bumper": "CF-1", "type": "JLTV", "status": "FMC"},
                {"bumper": "CF-2", "type": "JLTV", "status": "NOPE"},
                {"bumper": "", "type": "JLTV", "status": "FMC"}]
        code, res = self.confirm(self.vco, "19 ASOS", "c.xlsx", rows)
        self.assertEqual(code, 201, res)
        self.assertEqual(res["created_count"], 1)
        self.assertEqual(res["skipped_count"], 3)

    def test_import_asog_and_csv(self):
        raw = make_xlsx([
            ["Vehicle", "Asset #", "Location"],
            ["JLTV", "HQ-IMP-1", "HQ motor pool"],
        ])
        code, prev = self.preview(self.vco, "18 ASOG", "hq.xlsx", raw)
        self.assertEqual(code, 200, prev)
        self.assertEqual(prev["will_import"], 1)
        code, res = self.confirm(self.vco, "18 ASOG", "hq.xlsx",
                                 prev["rows"])
        self.assertEqual(res["created_count"], 1)
        _, snap = self.req(self.operator, "GET", "/api/fleet")
        asog = next(u for u in snap["units"] if u["unit"] == "18 ASOG")
        self.assertEqual(asog["on_hand"], 1)
        self.assertEqual(snap["group"]["on_hand"], 1)
        # CSV goes through the same mapping
        csv_raw = b"Type,Asset Number,Location\nForklift,CSV-1,Dock\n"
        code, prev = self.preview(self.vco, "20 ASOS", "f.csv", csv_raw)
        self.assertEqual(code, 200, prev)
        self.assertEqual(prev["will_import"], 1)
        code, res = self.confirm(self.vco, "20 ASOS", "f.csv",
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
                self.vco, bumper=f"TIE-{i}",
                status="FMC" if i == 0 else "NMC")
            self.assertEqual(code, 201)
        _, snap = self.req(self.operator, "GET", "/api/fleet")
        u14 = next(u for u in snap["units"] if u["unit"] == "14 ASOS")
        self.assertEqual(u14["mc_rate"], 6.3)
        self.assertEqual(snap["group"]["mc_rate"], 6.3)

    # ---- S3: UTC codes normalized to uppercase; uniqueness ignores case
    def test_utc_code_case_normalized_and_unique(self):
        code, body = self.req(self.vco, "POST", "/api/utcs",
                              {"unit": "14 ASOS", "code": "abc1",
                               "title": "lower first"})
        self.assertEqual(code, 201, body)
        self.assertEqual(body["utc"]["code"], "ABC1")
        for dupe in ("ABC1", "aBc1"):
            code, _ = self.req(self.vco, "POST", "/api/utcs",
                               {"unit": "14 ASOS", "code": dupe,
                                "title": "dupe"})
            self.assertEqual(code, 409, dupe)
        code, _ = self.req(self.vco, "POST", "/api/utcs",
                           {"unit": "15 ASOS", "code": "abc1",
                            "title": "same code, other unit"})
        self.assertEqual(code, 201)
        code, body = self.req(self.vco, "POST", "/api/utcs",
                              {"unit": "14 ASOS", "code": "xyz9",
                               "title": "second"})
        self.assertEqual(code, 201, body)
        uid2 = body["utc"]["id"]
        code, _ = self.req(self.vco, "PUT", f"/api/utcs/{uid2}",
                           {"code": "AbC1"})
        self.assertEqual(code, 409)  # rename onto a case-variant dupe
        code, body = self.req(self.vco, "PUT", f"/api/utcs/{uid2}",
                              {"code": "zz2"})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["utc"]["code"], "ZZ2")  # update normalizes
        _, snap = self.req(self.operator, "GET", "/api/fleet")
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
        code, body = self.preview(self.vco, "14 ASOS", "evil.xlsx", raw)
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
        code, prev = self.preview(self.vco, "14 ASOS", "wide.xlsx", raw)
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
        code, prev = self.preview(self.vco, "14 ASOS", "neg.xlsx", raw)
        self.assertEqual(code, 200, prev)
        r = prev["rows"][0]
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["type"], "Unknown")  # blank, NOT SENTINEL-LAST
        self.assertEqual(r["bumper"], "NEG-1")
        # positive out-of-range index: blank too (bumper -> row skipped)
        row = ('<row r="2"><c r="A2" t="s"><v>3</v></c>'
               '<c r="B2" t="s"><v>999</v></c></row>')
        raw = make_xlsx_raw(hdr + row, strings)
        code, prev = self.preview(self.vco, "14 ASOS", "oob.xlsx", raw)
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
        code, body = self.preview(self.vco, "14 ASOS", "crc.xlsx",
                                  bytes(raw))
        self.assertEqual(code, 400, body)
        self.assertIn("error", body)

    # ---- S4: the 413 message names the cap of the route hit
    def test_body_cap_message_names_route_cap(self):
        code, body = self.req(self.vco, "POST", "/api/import/preview",
                              {"unit": "14 ASOS", "filename": "big.xlsx",
                               "content_b64": "A" * 9_000_000})
        self.assertEqual(code, 413, body)
        self.assertIn("8 MB cap", body["error"])
        code, body = self.req(self.vco, "POST", "/api/vehicles",
                              {"unit": "14 ASOS", "bumper": "X",
                               "pad": "x" * 1_100_000})
        self.assertEqual(code, 413, body)
        self.assertIn("1 MB cap", body["error"])


class V111Tests(ApiTests):
    """v1.1.1: VCOs can DELETE an authorization row. Add existed,
    subtract-by-number (lowering the count) existed — deleting the row
    itself was the one gap."""

    def req(self, token, method, path, body=None):
        return req(self.base, method, path, body, token)

    def snap(self):
        _, snap = self.req(self.operator, "GET", "/api/fleet")
        return snap

    def mk_auth(self, token, unit="14 ASOS", typ="JLTV", n=10):
        code, body = self.req(token, "POST", "/api/authorizations",
                              {"unit": unit, "vehicle_type": typ,
                               "authorized": n})
        self.assertEqual(code, 200, body)
        return next(a for a in self.snap()["authorizations"]
                    if a["unit"] == unit and a["vehicle_type"] == typ)

    def test_vco_deletes_authorization_totals_and_audit(self):
        a14 = self.mk_auth(self.vco, "14 ASOS", "JLTV", 10)
        self.mk_auth(self.vco, "15 ASOS", "JLTV", 3)
        self.assertEqual(self.snap()["group"]["authorizations"], 13)
        code, body = self.req(self.vco, "DELETE",
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
        _, log = req(self.base, "GET", "/api/audit", token=self.operator)
        dels = [a for a in log["audit"]
                if a["action"] == "authorization"
                and "(deleted)" in a["detail"]]
        self.assertEqual(len(dels), 1)
        d = dels[0]
        self.assertEqual(d["actor"], "Boss")
        self.assertEqual(d["field"], "authorized")
        self.assertEqual(d["old_value"], "10")
        self.assertEqual(d["new_value"], "")
        self.assertIn("14 ASOS", d["detail"])
        self.assertIn("JLTV", d["detail"])

    def test_delete_roles_and_unknown_ids(self):
        a = self.mk_auth(self.vco)
        code, _ = self.req(self.operator, "DELETE",
                           f"/api/authorizations/{a['id']}")
        self.assertEqual(code, 403)  # operators are read-only
        code, _ = req(self.base, "DELETE",
                      f"/api/authorizations/{a['id']}")  # no token
        self.assertEqual(code, 401)
        code, _ = self.req(self.vco, "DELETE",
                           "/api/authorizations/999999")
        self.assertEqual(code, 404)  # unknown id
        code, _ = self.req(self.vco, "DELETE",
                           "/api/authorizations/not-an-id")
        self.assertEqual(code, 404)  # non-integer id
        code, _ = self.req(self.vco, "DELETE",
                           f"/api/authorizations/{a['id']}")
        self.assertEqual(code, 200)  # vco can too
        code, _ = self.req(self.vco, "DELETE",
                           f"/api/authorizations/{a['id']}")
        self.assertEqual(code, 404)  # second delete of the same id

    def test_delete_leaves_vehicles_and_on_hand_untouched(self):
        self.add_vehicle(self.vco)  # 14 ASOS JLTV
        self.add_vehicle(self.vco, bumper="A-2")
        a = self.mk_auth(self.vco, "14 ASOS", "JLTV", 6)
        code, _ = self.req(self.vco, "DELETE",
                           f"/api/authorizations/{a['id']}")
        self.assertEqual(code, 200)
        snap = self.snap()
        u14 = next(u for u in snap["units"] if u["unit"] == "14 ASOS")
        self.assertEqual(u14["on_hand"], 2)         # vehicles untouched
        self.assertEqual(u14["authorizations"], 0)  # only the line is gone
        self.assertEqual(len(snap["vehicles"]), 2)

    def test_vco_can_lower_authorization_by_number(self):
        # subtract-by-number, locked in: 10 -> 4 -> 0 as a VCO
        self.mk_auth(self.vco, "14 ASOS", "JLTV", 10)
        for n in (4, 0):
            code, body = self.req(self.vco, "POST", "/api/authorizations",
                                  {"unit": "14 ASOS", "vehicle_type": "JLTV",
                                   "authorized": n})
            self.assertEqual(code, 200, body)
            snap = self.snap()
            entry = next(a for a in snap["authorizations"]
                         if a["unit"] == "14 ASOS")
            self.assertEqual(entry["authorized"], n)
            self.assertEqual(snap["group"]["authorizations"], n)

    def test_demo_authorization_deletable_and_demo_remove_still_works(self):
        code, _ = req(self.base, "POST", "/api/demo/load", {}, self.vco)
        self.assertEqual(code, 200)
        snap = self.snap()
        demo_auths = [a for a in snap["authorizations"] if a["is_demo"]]
        self.assertTrue(demo_auths)
        victim = demo_auths[0]
        before = snap["group"]["authorizations"]
        code, _ = self.req(self.vco, "DELETE",
                           f"/api/authorizations/{victim['id']}")
        self.assertEqual(code, 200)  # demo rows delete like any other
        snap = self.snap()
        self.assertFalse(any(a["id"] == victim["id"]
                             for a in snap["authorizations"]))
        self.assertEqual(snap["group"]["authorizations"],
                         before - victim["authorized"])
        # the demo-remove flow still clears the rest cleanly afterwards
        code, _ = req(self.base, "POST", "/api/demo/remove", {}, self.vco)
        self.assertEqual(code, 200)
        snap = self.snap()
        self.assertEqual(snap["authorizations"], [])
        self.assertEqual(snap["vehicles"], [])


# ---------------------------------------------------------------- v1.2
def req_bytes(base, method, path, token=None):
    """Raw (non-JSON) request for the photo GET endpoints."""
    r = urllib.request.Request(base + path, method=method)
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, resp.headers.get_content_type(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get_content_type(), e.read()


def photo_url(mime, payload):
    return "data:" + mime + ";base64," + base64.b64encode(payload).decode()


class InspectionTests(ApiTests):
    """v1.2: AF Form 1800 operator inspections."""

    def mk_vehicle(self, token=None, **kw):
        code, v = self.add_vehicle(token or self.vco, **kw)
        self.assertEqual(code, 201, v)
        return v["vehicle"]

    def insp_body(self, vid, **kw):
        body = {"vehicle_id": vid, "month_year": "October 2026",
                "mileage_hours": 5000, "tire_front": "80",
                "tire_rear": "80",
                "items": [
                    {"item_no": 1, "applies": True, "result": "pass",
                     "comment": ""},
                    {"item_no": 2, "applies": True, "result": "fail",
                     "comment": "oil leak at filter"},
                    {"item_no": 3, "applies": False, "comment": ""},
                    {"item_no": "other", "text": "Winch cable fray",
                     "applies": True, "result": "fail",
                     "comment": "cable strands showing"}]}
        body.update(kw)
        return body

    def submit(self, token, body):
        return req(self.base, "POST", "/api/inspections", body, token)

    def test_operator_submits_inspection(self):
        v = self.mk_vehicle()
        code, r = self.submit(self.operator, self.insp_body(v["id"]))
        self.assertEqual(code, 201, r)
        insp = r["inspection"]
        self.assertEqual(insp["status"], "submitted")
        self.assertEqual(insp["operator"], "Vic Operator")
        self.assertEqual(insp["unit"], "14 ASOS")
        self.assertEqual(insp["bumper"], "A-1")
        self.assertEqual(len(r["items"]), 4)
        # numbered items store the official fixed text
        self.assertEqual(r["items"][0]["item_text"], sky.INSPECTION_ITEMS[0])
        # failed items become open discrepancies (numbered + other)
        self.assertEqual(len(r["discrepancies"]), 2)
        d2 = next(d for d in r["discrepancies"] if d["item_no"] == 2)
        self.assertEqual(d2["status"], "open")
        self.assertEqual(d2["description"], "oil leak at filter")
        self.assertEqual(d2["miles_hours"], 5000)
        dother = next(d for d in r["discrepancies"]
                      if d["item_no"] is None)
        self.assertEqual(dother["description"], "cable strands showing")
        # vehicle status is NEVER changed by an inspection
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        veh = next(x for x in fleet["vehicles"] if x["id"] == v["id"])
        self.assertEqual(veh["status"], "FMC")
        self.assertEqual(veh["mileage"], 5000)

    def test_vco_can_submit(self):
        v = self.mk_vehicle()
        code, _ = self.submit(self.vco, self.insp_body(v["id"]))
        self.assertEqual(code, 201)

    def test_vco_review_flow_and_gates(self):
        v = self.mk_vehicle()
        _, r = self.submit(self.operator, self.insp_body(v["id"]))
        iid = r["inspection"]["id"]
        disc = r["discrepancies"][0]["id"]
        code, _ = req(self.base, "POST", f"/api/inspections/{iid}/review",
                      {}, self.operator)
        self.assertEqual(code, 403)
        code, _ = req(self.base, "POST", f"/api/inspections/{iid}/review",
                      {})
        self.assertEqual(code, 401)  # unauthenticated review rejected
        code, _ = req(self.base, "POST", "/api/inspections/99999/review",
                      {}, self.vco)
        self.assertEqual(code, 404)
        code, r = req(self.base, "POST", f"/api/inspections/{iid}/review",
                      {"dispositions": [{"discrepancy_id": disc,
                                         "status": "corrected"}]}, self.vco)
        self.assertEqual(code, 200, r)
        self.assertEqual(r["inspection"]["status"], "reviewed")
        self.assertEqual(r["inspection"]["reviewed_by"], "Boss")
        code, _ = req(self.base, "POST", f"/api/inspections/{iid}/review",
                      {}, self.vco)
        self.assertEqual(code, 409)  # double review
        # disposition landed on the discrepancy
        _, full = req(self.base, "GET", f"/api/inspections/{iid}",
                      token=self.vco)
        d = next(x for x in full["discrepancies"] if x["id"] == disc)
        self.assertEqual(d["status"], "corrected")

    def test_review_validation_and_no_disposition_review(self):
        v = self.mk_vehicle()
        _, r = self.submit(self.operator, self.insp_body(v["id"]))
        iid = r["inspection"]["id"]
        disc = r["discrepancies"][0]["id"]
        code, _ = req(self.base, "POST", f"/api/inspections/{iid}/review",
                      {"dispositions": [{"discrepancy_id": disc,
                                         "status": "open"}]}, self.vco)
        self.assertEqual(code, 400)  # "open" is not a disposition
        code, _ = req(self.base, "POST", f"/api/inspections/{iid}/review",
                      {"dispositions": [{"discrepancy_id": 99999,
                                         "status": "corrected"}]}, self.vco)
        self.assertEqual(code, 400)  # foreign discrepancy
        code, r = req(self.base, "POST", f"/api/inspections/{iid}/review",
                      {}, self.vco)
        self.assertEqual(code, 200, r)  # review without dispositions is fine
        _, full = req(self.base, "GET", f"/api/inspections/{iid}",
                      token=self.vco)
        self.assertEqual(full["inspection"]["status"], "reviewed")
        self.assertEqual(full["discrepancies"][0]["status"], "open")

    def test_mileage_update_and_audit(self):
        v = self.mk_vehicle()  # mileage 100
        code, _ = self.submit(self.operator, self.insp_body(v["id"]))
        self.assertEqual(code, 201)
        _, log = req(self.base, "GET", "/api/audit", token=self.vco)
        acts = [(a["action"], a["field"], a["old_value"], a["new_value"])
                for a in log["audit"]]
        self.assertIn(("update", "mileage", "100", "5000"), acts)
        self.assertTrue(any(a[0] == "inspection" for a in acts))
        # no mileage given -> vehicle mileage untouched
        v2 = self.mk_vehicle(bumper="A-2")
        body = self.insp_body(v2["id"])
        del body["mileage_hours"]
        code, _ = self.submit(self.operator, body)
        self.assertEqual(code, 201)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        veh = next(x for x in fleet["vehicles"] if x["id"] == v2["id"])
        self.assertEqual(veh["mileage"], 100)

    def test_validation_400s(self):
        v = self.mk_vehicle()
        base = self.insp_body(v["id"])
        cases = []
        b = dict(base); b["items"] = []
        cases.append(b)  # empty items
        b = dict(base)
        b["items"] = [{"item_no": 36, "applies": True, "result": "pass"}]
        cases.append(b)  # item_no out of range
        b = dict(base)
        b["items"] = [{"item_no": 1, "applies": True}]
        cases.append(b)  # result missing when applies
        b = dict(base)
        b["items"] = [{"item_no": 1, "applies": True, "result": "ok"}]
        cases.append(b)  # bad result
        b = dict(base)
        b["items"] = [{"item_no": 1, "applies": True, "result": "pass"},
                      {"item_no": 1, "applies": True, "result": "pass"}]
        cases.append(b)  # duplicate numbered item
        b = dict(base)
        b["items"] = [{"item_no": "other", "text": "x" * 121,
                       "applies": True, "result": "pass"}]
        cases.append(b)  # other text too long
        b = dict(base)
        b["items"] = [{"item_no": "other", "text": f"O{i}",
                       "applies": True, "result": "pass"} for i in range(11)]
        cases.append(b)  # more than 10 others
        b = dict(base)
        b["items"] = [{"item_no": 1, "applies": True, "result": "pass",
                       "comment": "x" * 2001}]
        cases.append(b)  # comment too long
        b = dict(base); b["mileage_hours"] = -1
        cases.append(b)
        b = dict(base); del b["month_year"]
        cases.append(b)
        for body in cases:
            code, r = self.submit(self.operator, body)
            self.assertEqual(code, 400, body)
        # unauthenticated -> 401, unknown vehicle -> 404
        code, _ = req(self.base, "POST", "/api/inspections", base)
        self.assertEqual(code, 401)
        code, _ = self.submit(self.operator, self.insp_body(99999))
        self.assertEqual(code, 404)

    def test_list_filters_and_order(self):
        v1 = self.mk_vehicle(bumper="F-1")
        v2 = self.mk_vehicle(bumper="F-2", unit="15 ASOS")
        _, r1 = self.submit(self.operator, self.insp_body(v1["id"]))
        _, r2 = self.submit(self.operator, self.insp_body(v2["id"]))
        iid1 = r1["inspection"]["id"]
        req(self.base, "POST", f"/api/inspections/{iid1}/review", {},
            self.vco)
        _, lst = req(self.base, "GET", "/api/inspections", token=self.operator)
        self.assertEqual(len(lst["inspections"]), 2)
        # unreviewed first
        self.assertEqual(lst["inspections"][0]["status"], "submitted")
        _, lst = req(self.base, "GET", "/api/inspections?reviewed=false",
                     token=self.operator)
        self.assertEqual([i["id"] for i in lst["inspections"]],
                         [r2["inspection"]["id"]])
        _, lst = req(self.base, "GET", "/api/inspections?reviewed=true",
                     token=self.operator)
        self.assertEqual([i["id"] for i in lst["inspections"]], [iid1])
        _, lst = req(self.base, "GET",
                     f"/api/inspections?vehicle_id={v2['id']}",
                     token=self.operator)
        self.assertEqual(len(lst["inspections"]), 1)
        _, lst = req(self.base, "GET", "/api/inspections?unit=15%20ASOS",
                     token=self.operator)
        self.assertEqual(len(lst["inspections"]), 1)
        code, _ = req(self.base, "GET", "/api/inspections?reviewed=maybe",
                      token=self.operator)
        self.assertEqual(code, 400)
        code, _ = req(self.base, "GET", "/api/inspections/99999",
                      token=self.operator)
        self.assertEqual(code, 404)

    def test_detail_json_has_form_content(self):
        # the printable form is client-side (see frontend_check): the
        # server exposes the detail JSON the form renders from
        v = self.mk_vehicle()
        _, r = self.submit(self.operator, self.insp_body(v["id"]))
        iid = r["inspection"]["id"]
        _, full = req(self.base, "GET", f"/api/inspections/{iid}",
                      token=self.operator)
        self.assertEqual(full["inspection"]["bumper"], "A-1")
        self.assertEqual(full["inspection"]["month_year"], "October 2026")
        self.assertEqual(full["items"][1]["comment"], "oil leak at filter")
        self.assertEqual(full["discrepancies"][0]["description"],
                         "oil leak at filter")


class VcoGuardTests(ApiTests):
    """v1.2: the last VCO cannot be demoted."""

    def test_last_vco_guard(self):
        _, users = req(self.base, "GET", "/api/users", token=self.vco)
        boss = next(u for u in users["users"] if u["username"] == "boss")
        code, _ = req(self.base, "PUT", f"/api/users/{boss['id']}",
                      {"role": "operator"}, self.vco)
        self.assertEqual(code, 400)
        # create a second VCO, then demoting boss must succeed
        code, second = req(self.base, "POST", "/api/users",
                           {"username": "second", "password": "second-pass-1",
                            "display_name": "Second", "unit": "14 ASOS",
                            "role": "vco"}, self.vco)
        self.assertEqual(code, 201, second)
        # (creating a second VCO does not touch boss's session)
        code, _ = req(self.base, "PUT", f"/api/users/{boss['id']}",
                      {"role": "operator"}, self.vco)
        self.assertEqual(code, 200)


class LegacyMigrationTests(unittest.TestCase):
    """v1.2: databases created before the rename migrate on boot.
    v1.3: a pre-collapse 'editor' account boots as VCO, and the
    main-VCO backfill composes with it (the 'editor' literal below
    is legacy test data, by design)."""

    def test_legacy_roles_migrate_and_audit_text_untouched(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "legacy.db")
        conn = sky.connect(db)
        sky.init_db(conn)
        conn.execute(
            "INSERT INTO users(username, display_name, password_hash, unit,"
            " role, created_at) VALUES (?,?,?,?,?,?)",
            ("oldvic", "Old Vic", sky.hash_password("old-pass-123"),
             "14 ASOS", "viewer", "2026-01-01T00:00:00+00:00"))
        conn.execute(
            "INSERT INTO users(username, display_name, password_hash, unit,"
            " role, created_at) VALUES (?,?,?,?,?,?)",
            ("oldboss", "Old Boss", sky.hash_password("old-pass-123"),
             "18 ASOG", "admin", "2026-01-01T00:00:00+00:00"))
        # v1.3 legacy data: a pre-collapse editor account
        conn.execute(
            "INSERT INTO users(username, display_name, password_hash, unit,"
            " role, created_at) VALUES (?,?,?,?,?,?)",
            ("olded", "Old Ed", sky.hash_password("old-pass-123"),
             "15 ASOS", "editor", "2026-01-01T00:00:00+00:00"))
        conn.execute(
            "INSERT INTO audit(vehicle_id, actor, action, detail, created_at)"
            " VALUES (NULL, 'setup', 'account', 'created oldvic (viewer)',"
            " '2026-01-01T00:00:00+00:00')")
        conn.commit()
        sky.init_db(conn)  # second boot migrates
        rows = {r["username"]: r["role"] for r in conn.execute(
            "SELECT username, role FROM users")}
        self.assertEqual(rows["oldvic"], "operator")
        self.assertEqual(rows["oldboss"], "vco")
        self.assertEqual(rows["olded"], "vco")
        # main-VCO backfill composes with the migration: each unit's
        # earliest VCO becomes its main VCO
        mains = {r["username"]: r["is_main_vco"] for r in conn.execute(
            "SELECT username, is_main_vco FROM users")}
        self.assertEqual(mains["oldboss"], 1)
        self.assertEqual(mains["olded"], 1)
        self.assertEqual(mains["oldvic"], 0)
        detail = conn.execute(
            "SELECT detail FROM audit WHERE action='account'").fetchone()
        self.assertEqual(detail["detail"], "created oldvic (viewer)")
        conn.close()

    def test_main_backfill_keeps_an_existing_main(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "legacy2.db")
        conn = sky.connect(db)
        sky.init_db(conn)
        conn.execute(
            "INSERT INTO users(username, display_name, password_hash, unit,"
            " role, is_main_vco, created_at) VALUES (?,?,?,?,?,?,?)",
            ("later", "Later", sky.hash_password("old-pass-123"),
             "14 ASOS", "vco", 0, "2026-01-02T00:00:00+00:00"))
        conn.execute(
            "INSERT INTO users(username, display_name, password_hash, unit,"
            " role, is_main_vco, created_at) VALUES (?,?,?,?,?,?,?)",
            ("mainone", "Main One", sky.hash_password("old-pass-123"),
             "14 ASOS", "vco", 1, "2026-01-03T00:00:00+00:00"))
        conn.commit()
        sky.init_db(conn)  # backfill must NOT displace the existing main
        mains = {r["username"]: r["is_main_vco"] for r in conn.execute(
            "SELECT username, is_main_vco FROM users")}
        self.assertEqual(mains["mainone"], 1)
        self.assertEqual(mains["later"], 0)
        conn.close()

    def test_main_backfill_deduplicates_extra_mains(self):
        # S-C: a legacy/hand-edited DB with TWO mains in one unit
        # boots with exactly one — the earliest-created keeps the
        # badge, the additional mains are cleared.
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "legacy3.db")
        conn = sky.connect(db)
        sky.init_db(conn)
        conn.execute(
            "INSERT INTO users(username, display_name, password_hash, unit,"
            " role, is_main_vco, created_at) VALUES (?,?,?,?,?,?,?)",
            ("firstmain", "First Main", sky.hash_password("old-pass-123"),
             "14 ASOS", "vco", 1, "2026-01-02T00:00:00+00:00"))
        conn.execute(
            "INSERT INTO users(username, display_name, password_hash, unit,"
            " role, is_main_vco, created_at) VALUES (?,?,?,?,?,?,?)",
            ("secondmain", "Second Main", sky.hash_password("old-pass-123"),
             "14 ASOS", "vco", 1, "2026-01-03T00:00:00+00:00"))
        conn.execute(
            "INSERT INTO users(username, display_name, password_hash, unit,"
            " role, is_main_vco, created_at) VALUES (?,?,?,?,?,?,?)",
            ("plainop", "Plain Op", sky.hash_password("old-pass-123"),
             "14 ASOS", "operator", 1, "2026-01-04T00:00:00+00:00"))
        conn.commit()
        sky.init_db(conn)  # backfill deduplicates
        mains = {r["username"]: r["is_main_vco"] for r in conn.execute(
            "SELECT username, is_main_vco FROM users")}
        self.assertEqual(mains["firstmain"], 1)
        self.assertEqual(mains["secondmain"], 0)
        self.assertEqual(mains["plainop"], 0,
                         "a non-VCO never holds the badge")
        conn.close()


class VehiclePhotoTests(ApiTests):
    """v1.2: one photo per vehicle, operator or VCO."""

    def test_operator_attach_get_replace_remove(self):
        _, v = self.add_vehicle(self.vco)
        vid = v["vehicle"]["id"]
        # operator may attach (any signed-in role)
        code, r = req(self.base, "POST", f"/api/vehicles/{vid}/photo",
                      {"image": photo_url("image/jpeg", b"JPEG-ONE")},
                      self.operator)
        self.assertEqual(code, 200, r)
        self.assertTrue(r["photo"])
        status, ctype, raw = req_bytes(self.base, "GET",
                                       f"/api/vehicles/{vid}/photo",
                                       self.operator)
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/jpeg")
        self.assertEqual(raw, b"JPEG-ONE")
        # fleet payload carries the flag only, never bytes
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        veh = next(x for x in fleet["vehicles"] if x["id"] == vid)
        self.assertEqual(veh["photo"], 1)
        self.assertNotIn("photo_mime", veh)
        # replace (mime changes)
        code, _ = req(self.base, "POST", f"/api/vehicles/{vid}/photo",
                      {"image": photo_url("image/png", b"PNG-TWO")},
                      self.vco)
        self.assertEqual(code, 200)
        status, ctype, raw = req_bytes(self.base, "GET",
                                       f"/api/vehicles/{vid}/photo",
                                       self.operator)
        self.assertEqual(ctype, "image/png")
        self.assertEqual(raw, b"PNG-TWO")
        # audit rows for attach + replace
        _, log = req(self.base, "GET", "/api/audit", token=self.vco)
        photo_rows = [a for a in log["audit"] if a["action"] == "photo"]
        self.assertTrue(any("attached" in a["detail"] for a in photo_rows))
        self.assertTrue(any("replaced" in a["detail"] for a in photo_rows))
        # remove (operator may remove too)
        code, r = req(self.base, "DELETE", f"/api/vehicles/{vid}/photo",
                      token=self.operator)
        self.assertEqual(code, 200)
        status, _, _ = req_bytes(self.base, "GET",
                                 f"/api/vehicles/{vid}/photo", self.operator)
        self.assertEqual(status, 404)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        veh = next(x for x in fleet["vehicles"] if x["id"] == vid)
        self.assertEqual(veh["photo"], 0)

    def test_photo_gates_and_caps(self):
        _, v = self.add_vehicle(self.vco)
        vid = v["vehicle"]["id"]
        code, _ = req(self.base, "POST", f"/api/vehicles/{vid}/photo",
                      {"image": photo_url("image/jpeg", b"x")})
        self.assertEqual(code, 401)
        status, _, _ = req_bytes(self.base, "GET",
                                 f"/api/vehicles/{vid}/photo")
        self.assertEqual(status, 401)
        code, _ = req(self.base, "POST", "/api/vehicles/99999/photo",
                      {"image": photo_url("image/jpeg", b"x")}, self.operator)
        self.assertEqual(code, 404)
        code, _ = req(self.base, "POST", f"/api/vehicles/{vid}/photo",
                      {"image": "not-a-data-url"}, self.operator)
        self.assertEqual(code, 400)
        code, _ = req(self.base, "POST", f"/api/vehicles/{vid}/photo",
                      {"image": photo_url("image/bmp", b"x")}, self.operator)
        self.assertEqual(code, 400)
        # oversize: data URL past the 3 MB cap -> 413
        big = "data:image/jpeg;base64," + base64.b64encode(
            b"x" * 2400000).decode()
        self.assertGreater(len(big), 3 * 1024 * 1024)
        code, _ = req(self.base, "POST", f"/api/vehicles/{vid}/photo",
                      {"image": big}, self.operator)
        self.assertEqual(code, 413)


class DiscrepancyPhotoTests(ApiTests):
    """v1.2: up to 4 photos per discrepancy."""

    def mk_insp(self, bumper):
        _, v = self.add_vehicle(self.vco, bumper=bumper)
        body = {"vehicle_id": v["vehicle"]["id"],
                "month_year": "October 2026",
                "items": [{"item_no": 2, "applies": True, "result": "fail",
                           "comment": "leak"}]}
        code, r = req(self.base, "POST", "/api/inspections", body,
                      self.operator)
        self.assertEqual(code, 201, r)
        return r["inspection"]["id"], r["discrepancies"][0]["id"]

    def test_attach_list_get_and_cap(self):
        iid, did = self.mk_insp("DP-1")
        url = f"/api/inspections/{iid}/discrepancies/{did}/photos"
        for i in range(4):
            code, r = req(self.base, "POST", url,
                          {"image": photo_url("image/jpeg",
                                              f"P{i}".encode())},
                          self.operator)
            self.assertEqual(code, 201, r)
            self.assertIn("url", r)
        code, _ = req(self.base, "POST", url,
                      {"image": photo_url("image/jpeg", b"P4")},
                      self.operator)
        self.assertEqual(code, 409)  # 4-photo cap
        # detail JSON carries URLs/counts, never bytes
        _, full = req(self.base, "GET", f"/api/inspections/{iid}",
                      token=self.operator)
        disc = full["discrepancies"][0]
        self.assertEqual(len(disc["photos"]), 4)
        for p in disc["photos"]:
            self.assertTrue(p["url"].startswith(
                f"/api/inspections/{iid}/discrepancies/{did}/photos/"))
            status, ctype, raw = req_bytes(self.base, "GET", p["url"],
                                           self.operator)
            self.assertEqual(status, 200)
            self.assertEqual(ctype, "image/jpeg")
        _, log = req(self.base, "GET", "/api/audit", token=self.vco)
        self.assertTrue(any(a["action"] == "inspection-photo"
                            for a in log["audit"]))

    def test_photo_404s(self):
        iid, did = self.mk_insp("DP-1")
        code, _ = req(self.base, "POST",
                      f"/api/inspections/99999/discrepancies/{did}/photos",
                      {"image": photo_url("image/jpeg", b"x")}, self.operator)
        self.assertEqual(code, 404)
        code, _ = req(self.base, "POST",
                      f"/api/inspections/{iid}/discrepancies/99999/photos",
                      {"image": photo_url("image/jpeg", b"x")}, self.operator)
        self.assertEqual(code, 404)
        iid2, did2 = self.mk_insp("DP-2")
        code, _ = req(self.base, "POST",
                      f"/api/inspections/{iid}/discrepancies/{did2}/photos",
                      {"image": photo_url("image/jpeg", b"x")}, self.operator)
        self.assertEqual(code, 404)  # did2 belongs to iid2, not iid
        code, _ = req(self.base, "POST",
                      f"/api/inspections/{iid}/discrepancies/{did}/photos",
                      {"image": photo_url("image/jpeg", b"x")})
        self.assertEqual(code, 401)
        status, _, _ = req_bytes(
            self.base, "GET",
            f"/api/inspections/{iid}/discrepancies/{did}/photos/99999",
            self.operator)
        self.assertEqual(status, 404)


    def test_reviewed_inspection_photo_delete_allowed(self):
        # D-C (inherited v1.2 behavior, deliberate): attaching to a
        # reviewed inspection 409s, but DELETING a discrepancy photo
        # from a reviewed inspection is allowed — removal takes
        # nothing away from the closed record, and without it a
        # mistaken photo permanently occupied a cap slot.
        iid, did = self.mk_insp("DP-R")
        url = f"/api/inspections/{iid}/discrepancies/{did}/photos"
        code, r = req(self.base, "POST", url,
                      {"image": photo_url("image/jpeg", b"BEFORE")},
                      self.operator)
        self.assertEqual(code, 201, r)
        pid = r["photo_id"]
        code, _ = req(self.base, "POST", f"/api/inspections/{iid}/review",
                      {}, self.vco)
        self.assertEqual(code, 200)
        code, r = req(self.base, "POST", url,
                      {"image": photo_url("image/jpeg", b"AFTER")},
                      self.operator)
        self.assertEqual(code, 409, r)  # attach still refused
        code, _ = req(self.base, "DELETE", f"{url}/{pid}", None,
                      self.operator)
        self.assertEqual(code, 200)  # ...but delete is allowed
        _, full = req(self.base, "GET", f"/api/inspections/{iid}",
                      token=self.vco)
        self.assertEqual(len(full["discrepancies"][0]["photos"]), 0)


class RetentionTests(ApiTests):
    """v1.2: 1800 records purge 30 days after submission; the audit
    trail and vehicle photos survive."""

    def test_purge_on_list_read_keeps_audit_trail(self):
        from datetime import datetime, timezone, timedelta
        _, v = self.add_vehicle(self.vco)
        vid = v["vehicle"]["id"]
        # vehicle photo (fleet data) must survive any purge
        req(self.base, "POST", f"/api/vehicles/{vid}/photo",
            {"image": photo_url("image/jpeg", b"KEEP")}, self.operator)
        body = {"vehicle_id": vid, "month_year": "September 2026",
                "items": [{"item_no": 2, "applies": True, "result": "fail",
                           "comment": "old leak"}]}
        code, r = req(self.base, "POST", "/api/inspections", body,
                      self.operator)
        self.assertEqual(code, 201)
        iid = r["inspection"]["id"]
        did = r["discrepancies"][0]["id"]
        req(self.base, "POST",
            f"/api/inspections/{iid}/discrepancies/{did}/photos",
            {"image": photo_url("image/jpeg", b"OLD")}, self.operator)
        # backdate past 30 days
        old = (datetime.now(timezone.utc) -
               timedelta(days=31)).isoformat(timespec="seconds")
        conn = sky.connect(sky.Handler.db_path)
        conn.execute("UPDATE inspections SET created_at=? WHERE id=?",
                     (old, iid))
        conn.commit()
        conn.close()
        # a fresh inspection survives the same purge run
        _, r2 = req(self.base, "POST", "/api/inspections",
                    {"vehicle_id": vid, "month_year": "October 2026",
                     "items": [{"item_no": 1, "applies": True,
                                "result": "pass"}]}, self.operator)
        iid2 = r2["inspection"]["id"]
        # list read triggers the purge
        _, lst = req(self.base, "GET", "/api/inspections", token=self.vco)
        ids = [i["id"] for i in lst["inspections"]]
        self.assertNotIn(iid, ids)
        self.assertIn(iid2, ids)
        # items + discrepancies + photos are gone too
        conn = sky.connect(sky.Handler.db_path)
        n = conn.execute("SELECT COUNT(*) c FROM inspection_items"
                         " WHERE inspection_id=?", (iid,)).fetchone()["c"]
        self.assertEqual(n, 0)
        n = conn.execute("SELECT COUNT(*) c FROM discrepancies"
                         " WHERE inspection_id=?", (iid,)).fetchone()["c"]
        self.assertEqual(n, 0)
        n = conn.execute("SELECT COUNT(*) c FROM discrepancy_photos"
                         " WHERE discrepancy_id=?", (did,)).fetchone()["c"]
        self.assertEqual(n, 0)
        conn.close()
        # one purge audit row; the original submission row REMAINS
        _, log = req(self.base, "GET", "/api/audit", token=self.vco)
        self.assertTrue(any(a["action"] == "retention" and
                            "1 inspection(s) removed" in a["detail"]
                            for a in log["audit"]))
        self.assertTrue(any(a["action"] == "inspection" and
                            "September 2026" in a["detail"]
                            for a in log["audit"]))
        # ...and the vehicle photo is untouched by retention
        status, _, raw = req_bytes(self.base, "GET",
                                   f"/api/vehicles/{vid}/photo", self.operator)
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"KEEP")


class V12FixPassTests(ApiTests):
    """v1.2 fix pass: anchored routing, int64 guards, last-VCO race,
    discrepancy-photo delete, reviewed-409, detail-read purge,
    retention boundary + startup path."""

    def mk_insp(self, bumper="FX-1"):
        _, v = self.add_vehicle(self.vco, bumper=bumper)
        body = {"vehicle_id": v["vehicle"]["id"],
                "month_year": "October 2026",
                "items": [{"item_no": 2, "applies": True, "result": "fail",
                           "comment": "leak"}]}
        code, r = req(self.base, "POST", "/api/inspections", body,
                      self.operator)
        self.assertEqual(code, 201, r)
        return (v["vehicle"]["id"], r["inspection"]["id"],
                r["discrepancies"][0]["id"])

    def backdate(self, iid, days):
        from datetime import datetime, timezone, timedelta
        old = (datetime.now(timezone.utc) -
               timedelta(days=days)).isoformat(timespec="seconds")
        conn = sky.connect(sky.Handler.db_path)
        conn.execute("UPDATE inspections SET created_at=? WHERE id=?",
                     (old, iid))
        conn.commit()
        conn.close()

    def test_route_anchoring_photo_extra_404_and_vehicle_survives(self):
        # the live repro: DELETE /photo/extra used to fall past the
        # photo branch into generic vehicle-delete and DELETE THE
        # VEHICLE. Now it must 404 and change nothing.
        _, v = self.add_vehicle(self.vco)
        vid = v["vehicle"]["id"]
        req(self.base, "POST", f"/api/vehicles/{vid}/photo",
            {"image": photo_url("image/jpeg", b"KEEP")}, self.operator)
        code, _ = req(self.base, "DELETE",
                      f"/api/vehicles/{vid}/photo/extra", token=self.vco)
        self.assertEqual(code, 404)
        code, _ = req(self.base, "PUT",
                      f"/api/vehicles/{vid}/photo/extra",
                      {"status": "NMC"}, self.vco)
        self.assertEqual(code, 404)
        code, _ = req(self.base, "PUT", f"/api/vehicles/{vid}/extra",
                      {"status": "NMC"}, self.vco)
        self.assertEqual(code, 404)
        # vehicle + photo both untouched
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        veh = next(x for x in fleet["vehicles"] if x["id"] == vid)
        self.assertEqual(veh["status"], "FMC")
        self.assertEqual(veh["photo"], 1)
        status, _, raw = req_bytes(
            self.base, "GET", f"/api/vehicles/{vid}/photo", self.operator)
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"KEEP")
        # the legitimate suffixed routes still work
        code, _ = req(self.base, "DELETE", f"/api/vehicles/{vid}/photo",
                      token=self.operator)
        self.assertEqual(code, 200)
        # other single-id routes are anchored too
        code, a = req(self.base, "POST", "/api/authorizations",
                      {"unit": "14 ASOS", "vehicle_type": "JLTV",
                       "authorized": 3}, self.vco)
        self.assertEqual(code, 200, a)
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        auth = next(x for x in fleet["authorizations"]
                    if x["vehicle_type"] == "JLTV")
        code, _ = req(self.base, "DELETE",
                      f"/api/authorizations/{auth['id']}/extra",
                      token=self.vco)
        self.assertEqual(code, 404)
        code, _ = req(self.base, "DELETE",
                      f"/api/authorizations/{auth['id']}", token=self.vco)
        self.assertEqual(code, 200)

    def test_routing_nit_no_pid_photos_404(self):
        # GET .../discrepancies/{did}/photos with no photo id used to
        # fall through to the inspection-detail handler; now 404
        _, iid, did = self.mk_insp()
        code, _ = req(self.base, "GET",
                      f"/api/inspections/{iid}/discrepancies/{did}/photos",
                      token=self.operator)
        self.assertEqual(code, 404)
        # ...and the detail handler itself rejects trailing segments
        code, _ = req(self.base, "GET", f"/api/inspections/{iid}/extra",
                      token=self.operator)
        self.assertEqual(code, 404)

    def test_absurd_ids_never_500(self):
        huge = 2 ** 70
        _, v = self.add_vehicle(self.vco)
        vid = v["vehicle"]["id"]
        # create-inspection vehicle_id
        code, _ = req(self.base, "POST", "/api/inspections",
                      {"vehicle_id": huge, "month_year": "October 2026",
                       "items": [{"item_no": 1, "applies": True,
                                  "result": "pass"}]}, self.operator)
        self.assertIn(code, (400, 404))
        # list filter
        code, _ = req(self.base, "GET",
                      f"/api/inspections?vehicle_id={huge}",
                      token=self.operator)
        self.assertIn(code, (400, 404))
        # discrepancy-photo attach path ids
        _, iid, did = self.mk_insp()
        code, _ = req(self.base, "POST",
                      f"/api/inspections/{huge}/discrepancies/{did}/photos",
                      {"image": photo_url("image/jpeg", b"x")},
                      self.operator)
        self.assertIn(code, (400, 404))
        code, _ = req(self.base, "POST",
                      f"/api/inspections/{iid}/discrepancies/{huge}/photos",
                      {"image": photo_url("image/jpeg", b"x")},
                      self.operator)
        self.assertIn(code, (400, 404))
        # discrepancy-photo GET branch ids
        status, _, _ = req_bytes(
            self.base, "GET",
            f"/api/inspections/{iid}/discrepancies/{did}/photos/{huge}",
            self.operator)
        self.assertIn(status, (400, 404))
        # vehicle-photo GET branch id (already guarded via _path_id)
        status, _, _ = req_bytes(
            self.base, "GET", f"/api/vehicles/{huge}/photo", self.operator)
        self.assertIn(status, (400, 404))
        # the real records are untouched by all of the above
        _, fleet = req(self.base, "GET", "/api/fleet", token=self.operator)
        self.assertTrue(any(x["id"] == vid for x in fleet["vehicles"]))

    def test_last_vco_concurrent_demotions(self):
        # two VCOs total (boss + one other); two barrier-aligned
        # demotions must NOT both succeed — exactly one of the pair
        # stays VCO. NOTE: this guards the serialized re-check;
        # against the old check-before-transaction code it fails
        # whenever both requests' count reads land before either
        # write commits (the barrier makes that interleaving
        # near-certain, but a round is timing-shaped, not a proof
        # of the old bug).
        # Loser outcome depends on order: if the other demotion wins
        # first, the boss demotion sees count=1 and gets 400; if the
        # boss self-demotion wins first, the other request gets 400
        # from the serialized guard when it authenticated before the
        # session revocation landed, or 401 when it authenticated
        # after. All three are "refused"; neither may be 200, and
        # zero VCOs must never result.
        _, users = req(self.base, "GET", "/api/users", token=self.vco)
        boss = next(u for u in users["users"] if u["username"] == "boss")
        for rnd in range(3):
            _, other = req(self.base, "POST", "/api/users",
                           {"username": f"vco{rnd}",
                            "password": "vco-pass-1",
                            "display_name": f"VCO {rnd}",
                            "unit": "18 ASOG", "role": "vco"}, self.vco)
            other_id = other["user"]["id"]
            # fresh boss login per round (a demoted boss loses its
            # session to the role-change revocation)
            _, login = req(self.base, "POST", "/api/login",
                           {"username": "boss", "password": "vco-pass-1"})
            boss_tok = login["token"]
            barrier = threading.Barrier(2)
            results = {}

            def demote(key, uid):
                barrier.wait(timeout=10)
                code, _ = req(self.base, "PUT", f"/api/users/{uid}",
                              {"role": "operator"}, boss_tok)
                results[key] = code

            t1 = threading.Thread(target=demote, args=("boss", boss["id"]))
            t2 = threading.Thread(target=demote, args=("other", other_id))
            t1.start()
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)
            self.assertIn((results["boss"], results["other"]),
                          [(400, 200), (200, 400), (200, 401)],
                          f"round {rnd}: {results}")
            # exactly one of the pair is still a VCO: log in as the
            # survivor (boss token is revoked if boss was demoted)
            if results["boss"] == 200:
                _, lv = req(self.base, "POST", "/api/login",
                            {"username": f"vco{rnd}",
                             "password": "vco-pass-1"})
                surv_tok = lv["token"]
                expect = {"boss": "operator", f"vco{rnd}": "vco"}
            else:
                surv_tok = boss_tok
                expect = {"boss": "vco", f"vco{rnd}": "operator"}
            _, users = req(self.base, "GET", "/api/users", token=surv_tok)
            roles = {u["username"]: u["role"] for u in users["users"]}
            self.assertEqual(roles["boss"], expect["boss"])
            self.assertEqual(roles[f"vco{rnd}"], expect[f"vco{rnd}"])
            # reset for the next round: back to boss-only, so the
            # next round's fresh other is again the second VCO
            if results["boss"] == 200:
                # survivor is vco{rnd}: promote boss, demote survivor
                req(self.base, "PUT", f"/api/users/{boss['id']}",
                    {"role": "vco"}, surv_tok)
                _, login = req(self.base, "POST", "/api/login",
                               {"username": "boss",
                                "password": "vco-pass-1"})
                req(self.base, "PUT", f"/api/users/{other_id}",
                    {"role": "operator"}, login["token"])
            _, login = req(self.base, "POST", "/api/login",
                           {"username": "boss", "password": "vco-pass-1"})
            self.vco = login["token"]

    def test_last_vco_concurrent_removals(self):
        # mirror of the concurrent-demotion test, for DELETE: two
        # VCOs total (boss + one other); two barrier-aligned
        # removals must NOT both succeed — exactly one of the pair
        # survives. Loser outcome depends on order: if the other
        # removal wins first, the boss removal sees count=1 and gets
        # 400; if the boss self-removal wins first, the other request
        # gets 400 from the serialized guard when it authenticated
        # before the session revocation landed, or 401 when it
        # authenticated after. All three are "refused"; neither may
        # be 200, and zero VCOs must never result.
        _, users = req(self.base, "GET", "/api/users", token=self.vco)
        boss = next(u for u in users["users"] if u["username"] == "boss")
        boss_id = boss["id"]
        boss_tok = self.vco
        for rnd in range(3):
            _, other = req(self.base, "POST", "/api/users",
                           {"username": f"vcor{rnd}",
                            "password": "vco-pass-1",
                            "display_name": f"VCO R{rnd}",
                            "unit": "18 ASOG", "role": "vco"}, boss_tok)
            other_id = other["user"]["id"]
            barrier = threading.Barrier(2)
            results = {}

            def remove(key, uid):
                barrier.wait(timeout=10)
                code, _ = req(self.base, "DELETE", f"/api/users/{uid}",
                              None, boss_tok)
                results[key] = code

            t1 = threading.Thread(target=remove, args=("boss", boss_id))
            t2 = threading.Thread(target=remove, args=("other", other_id))
            t1.start()
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)
            self.assertIn((results["boss"], results["other"]),
                          [(400, 200), (200, 400), (200, 401)],
                          f"round {rnd}: {results}")
            # exactly one of the pair is still a VCO
            if results["boss"] == 200:
                _, lv = req(self.base, "POST", "/api/login",
                            {"username": f"vcor{rnd}",
                             "password": "vco-pass-1"})
                surv_tok = lv["token"]
                surv_id = other_id
            else:
                surv_tok = boss_tok
                surv_id = boss_id
            _, users = req(self.base, "GET", "/api/users", token=surv_tok)
            vcos = [u for u in users["users"] if u["role"] == "vco"]
            self.assertEqual(len(vcos), 1, f"round {rnd}: {vcos}")
            # reset for the next round: back to boss-only, so the
            # next round's fresh other is again the second VCO
            if results["boss"] == 200:
                # survivor is vcor{rnd}: re-create boss, then remove
                # the survivor
                _, nb = req(self.base, "POST", "/api/users",
                            {"username": "boss", "password": "vco-pass-1",
                             "display_name": "Boss", "unit": "18 ASOG",
                             "role": "vco"}, surv_tok)
                boss_id = nb["user"]["id"]
                _, login = req(self.base, "POST", "/api/login",
                               {"username": "boss", "password": "vco-pass-1"})
                boss_tok = login["token"]
                code, _ = req(self.base, "DELETE", f"/api/users/{surv_id}",
                              None, boss_tok)
                self.assertEqual(code, 200)
            # else boss survived and the other is gone: already boss-only

    def test_discrepancy_photo_delete(self):
        _, iid, did = self.mk_insp()
        url = f"/api/inspections/{iid}/discrepancies/{did}/photos"
        _, r1 = req(self.base, "POST", url,
                    {"image": photo_url("image/jpeg", b"P1")},
                    self.operator)
        _, r2 = req(self.base, "POST", url,
                    {"image": photo_url("image/jpeg", b"P2")},
                    self.operator)
        pid1, pid2 = r1["photo_id"], r2["photo_id"]
        # unauthenticated -> 401
        code, _ = req(self.base, "DELETE",
                      f"{url}/{pid1}")
        self.assertEqual(code, 401)
        # unknown photo -> 404
        code, _ = req(self.base, "DELETE", f"{url}/99999",
                      token=self.operator)
        self.assertEqual(code, 404)
        # delete one: detail shows one left, GET on it 404s
        code, _ = req(self.base, "DELETE", f"{url}/{pid1}",
                      token=self.operator)
        self.assertEqual(code, 200)
        _, full = req(self.base, "GET", f"/api/inspections/{iid}",
                      token=self.operator)
        self.assertEqual([p["id"] for p in
                          full["discrepancies"][0]["photos"]], [pid2])
        status, _, _ = req_bytes(self.base, "GET", f"{url}/{pid1}",
                                 self.operator)
        self.assertEqual(status, 404)
        # deleting again -> 404
        code, _ = req(self.base, "DELETE", f"{url}/{pid1}",
                      token=self.operator)
        self.assertEqual(code, 404)
        # the freed slot is usable again (cap is per live photo)
        for _ in range(3):
            code, _ = req(self.base, "POST", url,
                          {"image": photo_url("image/jpeg", b"PX")},
                          self.operator)
            self.assertEqual(code, 201)
        code, _ = req(self.base, "POST", url,
                      {"image": photo_url("image/jpeg", b"OVER")},
                      self.operator)
        self.assertEqual(code, 409)  # back at the 4-photo cap
        _, log = req(self.base, "GET", "/api/audit", token=self.vco)
        self.assertTrue(any(a["action"] == "inspection-photo" and
                            "removed" in a["detail"]
                            for a in log["audit"]))

    def test_photo_on_reviewed_inspection_409(self):
        _, iid, did = self.mk_insp()
        url = f"/api/inspections/{iid}/discrepancies/{did}/photos"
        code, _ = req(self.base, "POST", url,
                      {"image": photo_url("image/jpeg", b"BEFORE")},
                      self.operator)
        self.assertEqual(code, 201)
        code, _ = req(self.base, "POST", f"/api/inspections/{iid}/review",
                      {}, self.vco)
        self.assertEqual(code, 200)
        code, r = req(self.base, "POST", url,
                      {"image": photo_url("image/jpeg", b"AFTER")},
                      self.operator)
        self.assertEqual(code, 409, r)
        # the pre-review photo is still there; nothing new was added
        _, full = req(self.base, "GET", f"/api/inspections/{iid}",
                      token=self.operator)
        self.assertEqual(len(full["discrepancies"][0]["photos"]), 1)

    def test_detail_read_purges_expired(self):
        _, iid, did = self.mk_insp()
        self.backdate(iid, 31)
        # no list read in between: the detail GET itself must purge
        code, _ = req(self.base, "GET", f"/api/inspections/{iid}",
                      token=self.operator)
        self.assertEqual(code, 404)
        _, lst = req(self.base, "GET", "/api/inspections", token=self.vco)
        self.assertNotIn(iid, [i["id"] for i in lst["inspections"]])
        _, log = req(self.base, "GET", "/api/audit", token=self.vco)
        self.assertTrue(any(a["action"] == "retention" and
                            "1 inspection(s) removed" in a["detail"]
                            for a in log["audit"]))

    def test_reviewed_old_inspection_purged_with_photos(self):
        _, iid, did = self.mk_insp()
        url = f"/api/inspections/{iid}/discrepancies/{did}/photos"
        req(self.base, "POST", url,
            {"image": photo_url("image/jpeg", b"OLD")}, self.operator)
        req(self.base, "POST", f"/api/inspections/{iid}/review",
            {}, self.vco)
        self.backdate(iid, 31)
        _, lst = req(self.base, "GET", "/api/inspections", token=self.vco)
        self.assertNotIn(iid, [i["id"] for i in lst["inspections"]])
        conn = sky.connect(sky.Handler.db_path)
        n = conn.execute("SELECT COUNT(*) c FROM discrepancy_photos"
                         " WHERE discrepancy_id=?", (did,)).fetchone()["c"]
        self.assertEqual(n, 0)
        conn.close()
        # submission AND review audit rows remain
        _, log = req(self.base, "GET", "/api/audit", token=self.vco)
        acts = [a["action"] for a in log["audit"]]
        self.assertIn("inspection", acts)
        self.assertIn("inspection-review", acts)

    def test_retention_exact_boundary_and_startup_path(self):
        # EXACT boundary: cutoff is strict (created_at < now-30d), so
        # an inspection exactly 30 days old survives until older.
        # Wall-clock comparison is racy, so freeze the server module's
        # clock: the live server thread shares this process's module
        # object, so patching sky.datetime freezes it too.
        import datetime as _dt
        real_datetime = sky.datetime

        class FixedDatetime(real_datetime):
            _now = None

            @classmethod
            def now(cls, tz=None):
                return cls._now

        _, iid, _ = self.mk_insp()
        fixed = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
        exact = (fixed - _dt.timedelta(days=30)).isoformat(
            timespec="seconds")
        conn = sky.connect(sky.Handler.db_path)
        conn.execute("UPDATE inspections SET created_at=? WHERE id=?",
                     (exact, iid))
        conn.commit()
        conn.close()
        FixedDatetime._now = fixed
        sky.datetime = FixedDatetime
        try:
            _, lst = req(self.base, "GET", "/api/inspections",
                         token=self.vco)
            self.assertIn(iid, [i["id"] for i in lst["inspections"]],
                          "exactly-30-days-old must survive")
            FixedDatetime._now = fixed + _dt.timedelta(seconds=5)
            _, lst = req(self.base, "GET", "/api/inspections",
                         token=self.vco)
            self.assertNotIn(iid, [i["id"] for i in lst["inspections"]],
                             "older than 30 days must purge")
        finally:
            sky.datetime = real_datetime
        # STARTUP path: main() calls purge_expired_inspections(conn)
        # directly at boot — exercise that exact call, not the HTTP
        # list-read trigger
        _, iid2, _ = self.mk_insp(bumper="FX-2")
        self.backdate(iid2, 31)
        conn = sky.connect(sky.Handler.db_path)
        n = sky.purge_expired_inspections(conn)
        conn.close()
        self.assertEqual(n, 1)
        code, _ = req(self.base, "GET", f"/api/inspections/{iid2}",
                      token=self.operator)
        self.assertEqual(code, 404)


class RolesV13Tests(ApiTests):
    """v1.3: exactly two roles — operator (read + inspections + photos)
    and VCO (everything else). Server-side enforcement on every write."""

    def users(self):
        _, u = req(self.base, "GET", "/api/users", token=self.vco)
        return u["users"]

    def mk_user(self, username, role, unit="14 ASOS"):
        code, body = req(self.base, "POST", "/api/users",
                         {"username": username, "password": "user-pass-1",
                          "display_name": username, "unit": unit,
                          "role": role}, self.vco)
        self.assertEqual(code, 201, body)
        return body["user"]

    def mk_insp(self):
        _, v = self.add_vehicle(self.vco, bumper="M-1")
        body = {"vehicle_id": v["vehicle"]["id"],
                "month_year": "October 2026",
                "items": [{"item_no": 2, "applies": True, "result": "fail",
                           "comment": "leak"}]}
        code, r = req(self.base, "POST", "/api/inspections", body,
                      self.operator)
        self.assertEqual(code, 201, r)
        return r["inspection"]["id"], r["discrepancies"][0]["id"]

    def test_operator_denied_every_vco_write(self):
        _, v = self.add_vehicle(self.vco)
        vid = v["vehicle"]["id"]
        code, a = req(self.base, "POST", "/api/authorizations",
                      {"unit": "14 ASOS", "vehicle_type": "JLTV",
                       "authorized": 5}, self.vco)
        self.assertEqual(code, 200, a)
        auth_id = a["authorization"]["id"] if "authorization" in a \
            else next(x["id"] for x in
                      req(self.base, "GET", "/api/fleet",
                          token=self.vco)[1]["authorizations"]
                      if x["vehicle_type"] == "JLTV")
        code, u = req(self.base, "POST", "/api/utcs",
                      {"unit": "14 ASOS", "code": "XTEST1",
                       "title": "t"}, self.vco)
        self.assertEqual(code, 201, u)
        utc_id = u["utc"]["id"]
        code, _ = req(self.base, "POST", f"/api/utcs/{utc_id}/vehicles",
                      {"vehicle_id": vid}, self.vco)
        self.assertEqual(code, 200)
        iid, _ = self.mk_insp()
        # stale-status conflict for the resolve check (created as VCO)
        req(self.base, "PUT", f"/api/vehicles/{vid}",
            {"status": "PMC", "base_status": "FMC"}, self.vco)
        code, r = req(self.base, "PUT", f"/api/vehicles/{vid}",
                      {"status": "NMC", "base_status": "FMC"}, self.vco)
        self.assertIn("conflict", r)
        cid = r["conflict"]["id"]
        boss = next(x for x in self.users() if x["username"] == "boss")
        denied = [
            ("POST", "/api/vehicles",
             {"unit": "14 ASOS", "bumper": "OP-1", "type": "JLTV"}),
            ("PUT", f"/api/vehicles/{vid}", {"notes": "no"}),
            ("DELETE", f"/api/vehicles/{vid}", None),
            ("POST", "/api/authorizations",
             {"unit": "14 ASOS", "vehicle_type": "HMMWV",
              "authorized": 3}),
            ("DELETE", f"/api/authorizations/{auth_id}", None),
            ("POST", "/api/users",
             {"username": "nope", "password": "nope-pass-1",
              "display_name": "Nope", "unit": "14 ASOS",
              "role": "operator"}),
            ("GET", "/api/users", None),
            ("PUT", f"/api/users/{boss['id']}", {"unit": "15 ASOS"}),
            ("DELETE", f"/api/users/{boss['id']}", None),
            ("POST", f"/api/inspections/{iid}/review", {}),
            ("POST", "/api/utcs",
             {"unit": "14 ASOS", "code": "XTEST2", "title": "t"}),
            ("PUT", f"/api/utcs/{utc_id}",
             {"unit": "14 ASOS", "code": "XTEST1", "title": "renamed"}),
            ("POST", f"/api/utcs/{utc_id}/vehicles",
             {"vehicle_id": vid}),
            ("DELETE", f"/api/utcs/{utc_id}/vehicles/{vid}", None),
            ("DELETE", f"/api/utcs/{utc_id}", None),
            ("POST", "/api/import/preview",
             {"unit": "14 ASOS", "filename": "x.csv", "rows": []}),
            ("POST", "/api/import/confirm",
             {"unit": "14 ASOS", "filename": "x.csv", "rows": []}),
            ("POST", f"/api/conflicts/{cid}/resolve", {"action": "keep"}),
            ("POST", "/api/demo/load", {}),
            ("POST", "/api/demo/remove", {}),
        ]
        for method, path, body in denied:
            code, _ = req(self.base, method, path, body, self.operator)
            self.assertEqual(code, 403, (method, path))
        # cleanup: the VCO removes the UTC this test created, so its
        # membership row does not trip the next test's table wipe
        code, _ = req(self.base, "DELETE", f"/api/utcs/{utc_id}", None,
                      self.vco)
        self.assertEqual(code, 200)

    def test_operator_allowed_inspections_and_photos(self):
        _, v = self.add_vehicle(self.vco, bumper="P-1")
        vid = v["vehicle"]["id"]
        iid, did = self.mk_insp()
        self.assertEqual(iid and did and 1, 1)
        # vehicle photo: attach, replace, remove — all as operator
        code, _ = req(self.base, "POST", f"/api/vehicles/{vid}/photo",
                      {"image": photo_url("image/jpeg", b"OP-PHOTO")},
                      self.operator)
        self.assertEqual(code, 200)
        code, _ = req(self.base, "POST", f"/api/vehicles/{vid}/photo",
                      {"image": photo_url("image/png", b"OP-PHOTO-2")},
                      self.operator)
        self.assertEqual(code, 200)
        code, _ = req(self.base, "DELETE", f"/api/vehicles/{vid}/photo",
                      token=self.operator)
        self.assertEqual(code, 200)
        # discrepancy photo: attach + remove as operator
        url = f"/api/inspections/{iid}/discrepancies/{did}/photos"
        code, r = req(self.base, "POST", url,
                      {"image": photo_url("image/jpeg", b"DISC")},
                      self.operator)
        self.assertEqual(code, 201, r)
        code, _ = req(self.base, "DELETE",
                      url + "/" + str(r["photo_id"]), token=self.operator)
        self.assertEqual(code, 200)

    def test_vco_allowed_everything(self):
        _, v = self.add_vehicle(self.vco, bumper="V-1")
        vid = v["vehicle"]["id"]
        code, _ = req(self.base, "PUT", f"/api/vehicles/{vid}",
                      {"notes": "vco edit"}, self.vco)
        self.assertEqual(code, 200)
        code, a = req(self.base, "POST", "/api/authorizations",
                      {"unit": "14 ASOS", "vehicle_type": "JLTV",
                       "authorized": 5}, self.vco)
        self.assertEqual(code, 200, a)
        auth_id = next(x["id"] for x in
                       req(self.base, "GET", "/api/fleet",
                           token=self.vco)[1]["authorizations"]
                       if x["vehicle_type"] == "JLTV")
        code, _ = req(self.base, "DELETE",
                      f"/api/authorizations/{auth_id}", token=self.vco)
        self.assertEqual(code, 200)
        u = self.mk_user("tempvco", "vco")
        code, _ = req(self.base, "PUT", f"/api/users/{u['id']}",
                      {"role": "operator"}, self.vco)
        self.assertEqual(code, 200)
        code, _ = req(self.base, "DELETE", f"/api/users/{u['id']}",
                      token=self.vco)
        self.assertEqual(code, 200)
        iid, _ = self.mk_insp()
        code, _ = req(self.base, "POST", f"/api/inspections/{iid}/review",
                      {}, self.vco)
        self.assertEqual(code, 200)
        code, _ = req(self.base, "DELETE", f"/api/vehicles/{vid}",
                      token=self.vco)
        self.assertEqual(code, 200)

    def test_last_vco_cannot_be_removed(self):
        boss = next(x for x in self.users() if x["username"] == "boss")
        code, _ = req(self.base, "DELETE", f"/api/users/{boss['id']}",
                      token=self.vco)
        self.assertEqual(code, 400)
        code, _ = req(self.base, "PUT", f"/api/users/{boss['id']}",
                      {"role": "operator"}, self.vco)
        self.assertEqual(code, 400)

    def test_no_cap_on_vco_headcount(self):
        # personnel is fluid: four VCOs in one unit, all accepted
        for i in range(4):
            u = self.mk_user(f"vcap{i}", "vco", unit="15 ASOS")
            self.assertEqual(u["role"], "vco")
        vcos = [x for x in self.users()
                if x["role"] == "vco" and x["unit"] == "15 ASOS"]
        self.assertEqual(len(vcos), 4)

    def test_invalid_role_rejected(self):
        code, _ = req(self.base, "POST", "/api/users",
                      {"username": "bad", "password": "bad-pass-1",
                       "display_name": "Bad", "unit": "14 ASOS",
                       "role": "superuser"}, self.vco)
        self.assertEqual(code, 400)
        vic = next(x for x in self.users() if x["username"] == "vic")
        code, _ = req(self.base, "PUT", f"/api/users/{vic['id']}",
                      {"role": "superuser"}, self.vco)
        self.assertEqual(code, 400)


class MainVcoTests(ApiTests):
    """v1.3: the Unit VCO badge (is_main_vco) — one per unit,
    honorific only, auto-passing on removal/demotion."""

    def users(self):
        _, u = req(self.base, "GET", "/api/users", token=self.vco)
        return u["users"]

    def mk_user(self, username, role, unit):
        code, body = req(self.base, "POST", "/api/users",
                         {"username": username, "password": "user-pass-1",
                          "display_name": username, "unit": unit,
                          "role": role}, self.vco)
        self.assertEqual(code, 201, body)
        return body["user"]

    def test_first_vco_of_unit_becomes_main_automatically(self):
        boss = next(x for x in self.users() if x["username"] == "boss")
        self.assertEqual(boss["is_main_vco"], 1)  # setup VCO: main of 18 ASOG
        a = self.mk_user("maina", "vco", "14 ASOS")
        self.assertEqual(a["is_main_vco"], 1)
        b = self.mk_user("mainb", "vco", "14 ASOS")
        self.assertEqual(b["is_main_vco"], 0)
        op = self.mk_user("mainop", "operator", "14 ASOS")
        self.assertEqual(op["is_main_vco"], 0)

    def test_transfer_clears_first_and_is_audited(self):
        a = self.mk_user("tfera", "vco", "15 ASOS")
        b = self.mk_user("tferb", "vco", "15 ASOS")
        self.assertEqual(a["is_main_vco"], 1)
        code, _ = req(self.base, "PUT", f"/api/users/{b['id']}",
                      {"is_main_vco": True}, self.vco)
        self.assertEqual(code, 200)
        by = {x["username"]: x for x in self.users()}
        self.assertEqual(by["tfera"]["is_main_vco"], 0)
        self.assertEqual(by["tferb"]["is_main_vco"], 1)
        _, log = req(self.base, "GET", "/api/audit", token=self.vco)
        # both sides of the transfer are audited: the recipient's
        # own row plus the displaced main's row
        badge = [x for x in log["audit"]
                 if x["action"] == "account"
                 and x["field"] == "is_main_vco"]
        self.assertTrue(any(x["detail"] == "user tferb"
                            and x["old_value"] == "0"
                            and x["new_value"] == "1" for x in badge),
                        badge)
        self.assertTrue(any(x["detail"] == "user tfera"
                            and x["old_value"] == "1"
                            and x["new_value"] == "0" for x in badge),
                        badge)

    def test_explicit_false_on_main_auto_passes(self):
        # B1: explicit is_main_vco:false on a main clears the seat
        # AND auto-passes to the earliest-created remaining VCO —
        # it must not leave a unit with VCOs but no main.
        a = self.mk_user("falsea", "vco", "15 ASOS")
        b = self.mk_user("falseb", "vco", "15 ASOS")
        c = self.mk_user("falsec", "vco", "15 ASOS")
        self.assertEqual(a["is_main_vco"], 1)
        code, _ = req(self.base, "PUT", f"/api/users/{a['id']}",
                      {"is_main_vco": False}, self.vco)
        self.assertEqual(code, 200)
        by = {x["username"]: x for x in self.users()}
        self.assertEqual(by["falsea"]["is_main_vco"], 0)
        self.assertEqual(by["falseb"]["is_main_vco"], 1,
                         "earliest remaining VCO inherits")
        self.assertEqual(by["falsec"]["is_main_vco"], 0)
        _, log = req(self.base, "GET", "/api/audit", token=self.vco)
        self.assertTrue(any(x["action"] == "account"
                            and x["field"] == "is_main_vco"
                            and x["detail"] == "user falseb"
                            and x["new_value"] == "1"
                            for x in log["audit"]))

    def test_explicit_false_lone_main_clears(self):
        # with no remaining VCO the auto-pass simply clears the seat
        # (same as the demotion/removal path when none remains).
        a = self.mk_user("lonefalse", "vco", "19 ASOS")
        self.assertEqual(a["is_main_vco"], 1)
        code, _ = req(self.base, "PUT", f"/api/users/{a['id']}",
                      {"is_main_vco": False}, self.vco)
        self.assertEqual(code, 200)
        by = {x["username"]: x for x in self.users()}
        self.assertEqual(by["lonefalse"]["is_main_vco"], 0)
        snap = req(self.base, "GET", "/api/fleet", token=self.vco)[1]
        self.assertNotIn("19 ASOS", snap.get("main_vcos", {}))

    def test_create_operator_with_main_flag_400(self):
        # create-path badge consistency: PUT 400s on is_main_vco:true
        # for a non-VCO target, so create must too (not silently 201
        # with the flag dropped).
        code, r = req(self.base, "POST", "/api/users",
                      {"username": "flagop", "password": "user-pass-1",
                       "display_name": "Flag Op", "unit": "14 ASOS",
                       "role": "operator", "is_main_vco": True}, self.vco)
        self.assertEqual(code, 400, r)
        by = {x["username"]: x for x in self.users()}
        self.assertNotIn("flagop", by)

    def test_create_explicit_main_audits_displaced(self):
        a = self.mk_user("dispa", "vco", "15 ASOS")
        self.assertEqual(a["is_main_vco"], 1)
        code, b = req(self.base, "POST", "/api/users",
                      {"username": "dispb", "password": "user-pass-1",
                       "display_name": "dispb", "unit": "15 ASOS",
                       "role": "vco", "is_main_vco": True}, self.vco)
        self.assertEqual(code, 201, b)
        self.assertEqual(b["user"]["is_main_vco"], 1)
        by = {x["username"]: x for x in self.users()}
        self.assertEqual(by["dispa"]["is_main_vco"], 0)
        _, log = req(self.base, "GET", "/api/audit", token=self.vco)
        self.assertTrue(any(x["action"] == "account"
                            and x["field"] == "is_main_vco"
                            and x["detail"] == "user dispa"
                            and x["old_value"] == "1"
                            and x["new_value"] == "0"
                            for x in log["audit"]))

    def test_put_main_flag_on_operator_target_400(self):
        op = self.mk_user("putflagop", "operator", "14 ASOS")
        code, r = req(self.base, "PUT", f"/api/users/{op['id']}",
                      {"is_main_vco": True}, self.vco)
        self.assertEqual(code, 400, r)
        by = {x["username"]: x for x in self.users()}
        self.assertEqual(by["putflagop"]["is_main_vco"], 0)

    def test_transfer_is_vco_only(self):
        a = self.mk_user("tonly", "vco", "15 ASOS")
        b = self.mk_user("tonlyb", "vco", "15 ASOS")
        code, _ = req(self.base, "PUT", f"/api/users/{b['id']}",
                      {"is_main_vco": True}, self.operator)
        self.assertEqual(code, 403)
        by = {x["username"]: x for x in self.users()}
        self.assertEqual(by["tonly"]["is_main_vco"], 1)
        self.assertEqual(by["tonlyb"]["is_main_vco"], 0)

    def test_auto_pass_on_removal(self):
        a = self.mk_user("passa", "vco", "19 ASOS")
        b = self.mk_user("passb", "vco", "19 ASOS")
        self.assertEqual(a["is_main_vco"], 1)
        code, _ = req(self.base, "DELETE", f"/api/users/{a['id']}",
                      token=self.vco)
        self.assertEqual(code, 200)
        by = {x["username"]: x for x in self.users()}
        self.assertNotIn("passa", by)
        self.assertEqual(by["passb"]["is_main_vco"], 1)
        # the auto-pass recipient's badge gain is audited too
        _, log = req(self.base, "GET", "/api/audit", token=self.vco)
        self.assertTrue(any(x["action"] == "account"
                            and x["field"] == "is_main_vco"
                            and x["detail"] == "user passb"
                            and x["new_value"] == "1"
                            for x in log["audit"]))

    def test_auto_pass_on_demotion(self):
        a = self.mk_user("dema", "vco", "20 ASOS")
        b = self.mk_user("demb", "vco", "20 ASOS")
        code, _ = req(self.base, "PUT", f"/api/users/{a['id']}",
                      {"role": "operator"}, self.vco)
        self.assertEqual(code, 200)
        by = {x["username"]: x for x in self.users()}
        self.assertEqual(by["dema"]["role"], "operator")
        self.assertEqual(by["dema"]["is_main_vco"], 0)
        self.assertEqual(by["demb"]["is_main_vco"], 1)

    def test_auto_pass_clears_when_no_vco_remains(self):
        a = self.mk_user("lone", "vco", "19 ASOS")
        self.assertEqual(a["is_main_vco"], 1)
        code, _ = req(self.base, "DELETE", f"/api/users/{a['id']}",
                      token=self.vco)
        self.assertEqual(code, 200)
        snap = req(self.base, "GET", "/api/fleet", token=self.vco)[1]
        self.assertNotIn("19 ASOS", snap.get("main_vcos", {}))

    def test_badge_grants_no_extra_permissions(self):
        main = self.mk_user("badgeMain", "vco", "14 ASOS")
        plain = self.mk_user("badgePlain", "vco", "14 ASOS")
        self.assertEqual(main["is_main_vco"], 1)
        self.assertEqual(plain["is_main_vco"], 0)
        # both VCOs, badge or not, can write and review: log in as each
        for username in ("badgeMain", "badgePlain"):
            _, login = req(self.base, "POST", "/api/login",
                           {"username": username,
                            "password": "user-pass-1"})
            tok = login["token"]
            code, v = self.add_vehicle(tok, bumper="B-" + username)
            self.assertEqual(code, 201, v)
            body = {"vehicle_id": v["vehicle"]["id"],
                    "month_year": "October 2026",
                    "items": [{"item_no": 1, "applies": True,
                               "result": "pass"}]}
            code, r = req(self.base, "POST", "/api/inspections", body, tok)
            self.assertEqual(code, 201, r)
            code, _ = req(self.base, "POST",
                          f"/api/inspections/{r['inspection']['id']}/review",
                          {}, tok)
            self.assertEqual(code, 200)
        # and the operator is still denied regardless of badge state
        code, _ = self.add_vehicle(self.operator, bumper="B-OP")
        self.assertEqual(code, 403)
