#!/usr/bin/env python3
"""Skyline — fleet management for 18 ASOG and its ASOS squadrons.

Python 3 standard library only: http.server + sqlite3. No dependencies.
Run:  python3 server.py [--port 8420] [--db skyline.db]
"""
import argparse
import base64
import binascii
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import traceback
import zipfile
import xml.etree.ElementTree as ET
from datetime import date as _date
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")

UNITS = [
    ("18 ASOG", "group"),
    ("14 ASOS", "squadron"),
    ("15 ASOS", "squadron"),
    ("19 ASOS", "squadron"),
    ("20 ASOS", "squadron"),
]
UNIT_NAMES = [n for n, _ in UNITS]
SQUADRONS = [n for n, k in UNITS if k == "squadron"]
ROLES = ("operator", "vco")
STATUSES = ("FMC", "PMC", "NMC")
# DAF Form 1800 (Operator's Inspection Guide and Trouble Report) — the
# official 35 checklist items, verbatim. Numbered items always store
# THIS text; a client-supplied text for a numbered item is ignored.
INSPECTION_ITEMS = (
    "Cleanliness/damage/missing items (interior/exterior); unusual noise or occurrence (during operation)",
    "Leaks/fluid levels (oil/coolant/hydraulic/transmission/brake fluid)",
    "Safety devices (wiring/lights/horn/warning devices/sirens/reflectors/mirrors/seatbelts)",
    "Batteries (fluid/damage/cleanliness/security); instruments/gauges (operation & readings)",
    "Drive belts (fraying/cracking/tension); pulley/motor (air/hydraulic/electrical)",
    "Steering/springs/shackles (free play/excessive wear)",
    "Exhaust system/spark arrestors (damage/leaks)",
    "Tires/wheels (lug nuts/cracks); tracks",
    "Heater/defroster/air-conditioner; auxiliary generators/auxiliary heaters",
    "Brakes (servicing/parking); clutches (operation); accelerator pedal (damage/worn)",
    "Windshield (wipers/washer fluid/cracks)",
    "Hydraulic hoses (damage/frayed); cylinders/valves (damage/leaks)",
    "Cold weather aids (ether/alcohol injector, battery heater, oil/engine heater, coolant heater, etc.)",
    "Power take-off (PTO)",
    "Winch/tow connections",
    "Air tanks (drain daily or after operation)",
    "Mast tines/rollerized tines",
    "Rollers/power conveyors/rails/ladders/catwalks/cargo bed",
    "Bed (K-load): top/half/lowest/roll/shift/yaw (check position)",
    "Lower lobe cab shift (operation)",
    "Emergency stand-by system",
    'Markings — check legibility ("WATCHSTEP" stencil, etc.)',
    "Chutes/augers/fan blades/wear shoes",
    "Drums/crowns/fair leads",
    "Booms/outriggers/basket/platform/turntable (check for cracks & damage)",
    "Brooms/sprockets/chains",
    "Blades/reels/sickle bars/flails",
    "Kettle/hoisting mechanism/agitators",
    "Sheaves/blocks/cables",
    "Mould boards/bowls/cutting edges/skids",
    "Pumps/piping/discharge valves (leaks, corrosion); spare bars",
    "Firefighting specialized equipment/safety devices",
    "Firefighting system operation & foam/water levels",
    "Auxiliary firefighting system",
    "Hose/reels/handlines/turrets",
)
INSPECTION_STATUSES = ("submitted", "reviewed")
DISCREPANCY_STATUSES = ("open", "corrected", "delayed", "disposition")
MAX_OTHER_ITEMS = 10
VEHICLE_FIELDS = ("unit", "bumper", "type", "status", "mileage",
                  "maint_due_date", "maint_due_miles", "location", "notes")
MAX_BODY = 1_048_576  # 1 MB request-body cap
SESSION_TTL_DAYS = 30

class BodyError(Exception):
    """Request-body problem that maps to a JSON 4xx response."""
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message

class ValidationError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status
        self.message = message

# ---------------------------------------------------------------- passwords
def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + "$" + dk.hex()

def verify_password(password, stored):
    try:
        salt_hex, dk_hex = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), 200_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def session_expiry_iso():
    return (datetime.now(timezone.utc) +
            timedelta(days=SESSION_TTL_DAYS)).isoformat(timespec="seconds")

# ---------------------------------------------------------------- validation
_INT_RE = re.compile(r"^\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def strict_nonneg_int(value, field, allow_none=True):
    """Accept a real int (never bool/float) or a digit string; coerce to int.
    Anything else — floats, negatives, junk — is a 400."""
    if value is None:
        if allow_none:
            return None
        raise ValidationError(f"{field} is required")
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be an integer >= 0")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and _INT_RE.match(value.strip()):
        n = int(value.strip())
    else:
        raise ValidationError(f"{field} must be an integer >= 0")
    if n < 0:
        raise ValidationError(f"{field} must be an integer >= 0")
    return n

def valid_due_date(value, field="maint_due_date"):
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValidationError(f"{field} must be empty or YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValidationError(f"{field} must be a real calendar date")
    return value

def _req_text(data, field):
    v = data.get(field)
    if v is None:
        return ""
    if not isinstance(v, str):
        v = str(v)
    return v.strip()

def validate_vehicle(data, partial=False):
    """Validate + coerce vehicle fields. Returns a clean dict containing
    only the fields that were supplied (all of them when partial=False).
    Raises ValidationError (400/409 handled by callers for uniqueness)."""
    clean = {}
    def present(f):
        return (f in data) if partial else True
    if present("unit"):
        unit = data.get("unit")
        if unit not in UNIT_NAMES:
            raise ValidationError(
                "unit must be one of the five units "
                "(18 ASOG, 14 ASOS, 15 ASOS, 19 ASOS, 20 ASOS)")
        clean["unit"] = unit
    if present("bumper"):
        bumper = _req_text(data, "bumper")
        if not bumper:
            raise ValidationError("bumper # is required (non-empty)")
        clean["bumper"] = bumper
    if present("type"):
        typ = _req_text(data, "type")
        if not typ:
            raise ValidationError("type is required (non-empty)")
        clean["type"] = typ
    if present("status"):
        status = data.get("status", "FMC") if not partial else data.get("status")
        if status not in STATUSES:
            raise ValidationError("status must be FMC, PMC or NMC")
        clean["status"] = status
    elif not partial:
        clean["status"] = "FMC"
    for f in ("mileage", "maint_due_miles"):
        if present(f):
            clean[f] = strict_nonneg_int(data.get(f), f)
    if present("maint_due_date"):
        clean["maint_due_date"] = valid_due_date(data.get("maint_due_date", ""))
    for f in ("location", "notes"):
        if present(f):
            v = data.get(f, "")
            clean[f] = "" if v is None else str(v)
    return clean

def validate_inspection_items(items):
    """Validate an inspection's item list. Returns cleaned rows:
    [{item_no (int|None), item_text, applies (bool), result (str|None),
      comment}]. Numbered items 1-35 always carry the official fixed
    text (a client-supplied text for one is ignored, never stored);
    custom 'other' items carry client text (<=120 chars, max 10).
    result is required when applies, and must be pass/fail."""
    if not isinstance(items, list) or not items:
        raise ValidationError("items must be a non-empty list")
    if len(items) > len(INSPECTION_ITEMS) + MAX_OTHER_ITEMS:
        raise ValidationError("too many items (35 numbered + 10 other max)")
    seen, others, clean = set(), 0, []
    for entry in items:
        if not isinstance(entry, dict):
            raise ValidationError("each item must be an object")
        item_no = entry.get("item_no")
        if isinstance(item_no, bool):
            raise ValidationError('item_no must be 1-35 or "other"')
        if isinstance(item_no, int):
            if not 1 <= item_no <= len(INSPECTION_ITEMS):
                raise ValidationError('item_no must be 1-35 or "other"')
            if item_no in seen:
                raise ValidationError(f"duplicate item {item_no}")
            seen.add(item_no)
            text = INSPECTION_ITEMS[item_no - 1]
        elif item_no == "other":
            others += 1
            if others > MAX_OTHER_ITEMS:
                raise ValidationError("at most 10 other items")
            text = entry.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValidationError("other items need text")
            text = text.strip()
            if len(text) > 120:
                raise ValidationError("other item text must be <= 120 chars")
            item_no = None
        else:
            raise ValidationError('item_no must be 1-35 or "other"')
        applies = entry.get("applies", True)
        if not isinstance(applies, bool):
            raise ValidationError("applies must be true or false")
        result = None
        if applies:
            result = entry.get("result")
            if result not in ("pass", "fail"):
                raise ValidationError(
                    "result must be pass or fail when the item applies")
        comment = entry.get("comment", "")
        if comment is None:
            comment = ""
        if not isinstance(comment, str):
            raise ValidationError("comment must be text")
        if len(comment) > 2000:
            raise ValidationError("comment must be <= 2000 chars")
        clean.append({"item_no": item_no, "item_text": text,
                      "applies": applies, "result": result,
                      "comment": comment.strip()})
    return clean

PHOTO_BODY_CAP = 3 * 1024 * 1024  # photo POST request cap: 3 MB
PHOTO_MIMES = ("image/jpeg", "image/png", "image/webp", "image/gif")

def parse_photo_data_url(data):
    """Validate a photo upload body: {image: "data:<mime>;base64,..."}.
    Returns (raw_bytes, mime). Anything else is a 400. The client
    downscales to max 1600 px JPEG 0.8 first, so typical uploads are
    200-500 KB; the 3 MB cap is the backstop, not the target."""
    if not isinstance(data, dict):
        raise ValidationError("photo body must be an object")
    url = data.get("image")
    if not isinstance(url, str) or not url.startswith("data:image/"):
        raise ValidationError("image must be a data URL (data:image/...)")
    try:
        header, b64 = url.split(",", 1)
    except ValueError:
        raise ValidationError("image data URL is malformed")
    mime = header[5:].split(";", 1)[0]
    if mime not in PHOTO_MIMES or ";base64" not in header:
        raise ValidationError("image must be a base64 JPEG, PNG, WebP or GIF")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError("image data is not valid base64")
    if not raw:
        raise ValidationError("image is empty")
    return raw, mime

def bumper_taken(conn, unit, bumper, exclude_id=None):
    q = "SELECT id FROM vehicles WHERE unit=? AND bumper=?"
    args = [unit, bumper]
    if exclude_id is not None:
        q += " AND id<>?"
        args.append(exclude_id)
    return conn.execute(q, args).fetchone() is not None

def csv_safe(value):
    """Neutralize spreadsheet formula injection: cells starting with
    = + - @ (after optional leading blanks) get a leading single quote."""
    s = "" if value is None else str(value)
    t = s.lstrip(" \t")
    if t[:1] in ("=", "+", "-", "@"):
        return "'" + s
    return s

# ---------------------------------------------------------------- database
def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn

def _ensure_column(conn, table, column, ddl):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
        unit TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT 'operator',
        is_main_vco INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions(
        token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL, expires_at TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS units(
        id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, kind TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS vehicles(
        id INTEGER PRIMARY KEY, unit TEXT NOT NULL, bumper TEXT NOT NULL,
        type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'FMC',
        mileage INTEGER, maint_due_date TEXT DEFAULT '',
        maint_due_miles INTEGER, location TEXT DEFAULT '', notes TEXT DEFAULT '',
        is_demo INTEGER NOT NULL DEFAULT 0, client_op TEXT DEFAULT '',
        updated_by TEXT DEFAULT '', updated_at TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS authorizations(
        id INTEGER PRIMARY KEY, unit TEXT NOT NULL, vehicle_type TEXT NOT NULL,
        authorized INTEGER NOT NULL DEFAULT 0, is_demo INTEGER NOT NULL DEFAULT 0,
        UNIQUE(unit, vehicle_type));
    CREATE TABLE IF NOT EXISTS audit(
        id INTEGER PRIMARY KEY, vehicle_id INTEGER, actor TEXT NOT NULL,
        action TEXT NOT NULL, field TEXT DEFAULT '', old_value TEXT DEFAULT '',
        new_value TEXT DEFAULT '', detail TEXT DEFAULT '',
        is_conflict INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS conflicts(
        id INTEGER PRIMARY KEY, vehicle_id INTEGER NOT NULL, actor TEXT NOT NULL,
        field TEXT NOT NULL, attempted TEXT DEFAULT '', current TEXT DEFAULT '',
        resolved INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS utcs(
        id INTEGER PRIMARY KEY, unit TEXT NOT NULL, code TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT '',
        UNIQUE(unit, code));
    CREATE TABLE IF NOT EXISTS utc_vehicles(
        utc_id INTEGER NOT NULL REFERENCES utcs(id),
        vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
        PRIMARY KEY(utc_id, vehicle_id));
    CREATE TABLE IF NOT EXISTS inspections(
        id INTEGER PRIMARY KEY, vehicle_id INTEGER NOT NULL,
        unit TEXT NOT NULL, bumper TEXT NOT NULL, operator TEXT NOT NULL,
        month_year TEXT NOT NULL, mileage_hours INTEGER,
        tire_front TEXT DEFAULT '', tire_rear TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'submitted',
        created_at TEXT NOT NULL, reviewed_by TEXT DEFAULT '',
        reviewed_at TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS inspection_items(
        id INTEGER PRIMARY KEY, inspection_id INTEGER NOT NULL,
        item_no INTEGER, item_text TEXT NOT NULL, applies INTEGER NOT NULL,
        result TEXT DEFAULT '', comment TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS discrepancies(
        id INTEGER PRIMARY KEY, inspection_id INTEGER NOT NULL,
        item_no INTEGER, description TEXT NOT NULL,
        date_found TEXT NOT NULL, miles_hours INTEGER,
        status TEXT NOT NULL DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS discrepancy_photos(
        id INTEGER PRIMARY KEY, discrepancy_id INTEGER NOT NULL,
        image BLOB NOT NULL, mime TEXT NOT NULL,
        created_at TEXT NOT NULL);
    """)
    # v1.3 role collapse: legacy stored roles migrate in place on boot.
    # Nothing else is touched — historical audit rows keep their old text.
    # (The 'editor' literal below is migration data for pre-v1.3 DBs.)
    _ensure_column(conn, "users", "is_main_vco", "INTEGER NOT NULL DEFAULT 0")
    conn.execute("UPDATE users SET role='operator' WHERE role='viewer'")
    conn.execute("UPDATE users SET role='vco' WHERE role='admin'")
    conn.execute("UPDATE users SET role='vco' WHERE role='editor'")
    # v1.3 main-VCO backfill: per unit, keep the earliest-created
    # main and clear any additional mains (a legacy/hand-edited DB
    # can hold two); a unit with VCOs but no main gets its
    # earliest-created VCO. Flags on non-VCOs are cleared too —
    # only a VCO may hold the badge.
    conn.execute("UPDATE users SET is_main_vco=0 WHERE role<>'vco'"
                 " AND is_main_vco=1")
    for _name, _kind in UNITS:
        _mains = conn.execute(
            "SELECT id FROM users WHERE role='vco' AND unit=?"
            " AND is_main_vco=1 ORDER BY id", (_name,)).fetchall()
        if _mains:
            for _extra in _mains[1:]:
                conn.execute("UPDATE users SET is_main_vco=0 WHERE id=?",
                             (_extra["id"],))
        else:
            _earliest = conn.execute(
                "SELECT id FROM users WHERE role='vco' AND unit=?"
                " ORDER BY id LIMIT 1", (_name,)).fetchone()
            if _earliest:
                conn.execute("UPDATE users SET is_main_vco=1 WHERE id=?",
                             (_earliest["id"],))
    # migrations for databases created by earlier builds
    _ensure_column(conn, "sessions", "expires_at", "TEXT DEFAULT ''")
    _ensure_column(conn, "vehicles", "client_op", "TEXT DEFAULT ''")
    _ensure_column(conn, "vehicles", "photo", "BLOB")
    _ensure_column(conn, "vehicles", "photo_mime", "TEXT DEFAULT ''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_vehicles_client_op"
                 " ON vehicles(client_op) WHERE client_op <> ''")
    _ensure_bumper_unique_index(conn)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.Error:
        pass
    for name, kind in UNITS:
        conn.execute("INSERT OR IGNORE INTO units(name, kind) VALUES (?,?)",
                     (name, kind))
    conn.commit()

def _ensure_bumper_unique_index(conn):
    """DB-level backstop for bumper uniqueness: UNIQUE(unit, bumper).

    Migration safety: a database created by an earlier build may already
    contain duplicate (unit, bumper) pairs, and CREATE UNIQUE INDEX over
    duplicate data fails. That must never crash startup — detect the
    duplicates, log them loudly, and continue WITHOUT the index (the
    transactional check-then-insert in the write paths still guards new
    writes). Once the duplicates are cleaned up, the next start creates
    the index."""
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS"
                     " idx_vehicles_unit_bumper ON vehicles(unit, bumper)")
    except sqlite3.Error as e:
        dups = []
        try:
            dups = conn.execute(
                "SELECT unit, bumper, COUNT(*) c FROM vehicles"
                " GROUP BY unit, bumper HAVING c > 1"
                " ORDER BY unit, bumper").fetchall()
        except sqlite3.Error:
            pass
        pairs = ", ".join(f"{r[0]}/{r[1]} x{r[2]}" for r in dups) or "unknown"
        print("SKYLINE WARNING: UNIQUE(unit, bumper) index NOT created "
              f"({e}). Existing duplicate vehicle bumper(s): {pairs}. "
              "Bumper uniqueness is enforced transactionally only until "
              "these duplicates are resolved; clean them up and restart "
              "to enable the database constraint.", file=sys.stderr)

@contextmanager
def immediate_tx(conn):
    """BEGIN IMMEDIATE … COMMIT/ROLLBACK for read-modify-write paths."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()

def audit(conn, vehicle_id, actor, action, field="", old="", new="",
          detail="", is_conflict=0):
    conn.execute(
        "INSERT INTO audit(vehicle_id, actor, action, field, old_value,"
        " new_value, detail, is_conflict, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (vehicle_id, actor, action, field,
         "" if old is None else str(old), "" if new is None else str(new),
         detail, is_conflict, now_iso()))

def close_open_conflicts(conn, vehicle_id, actor, why):
    """Mark a vehicle's open conflicts resolved; audit it. Returns count."""
    cur = conn.execute(
        "UPDATE conflicts SET resolved=1 WHERE vehicle_id=? AND resolved=0",
        (vehicle_id,))
    if cur.rowcount:
        audit(conn, vehicle_id, actor, "conflict-close",
              detail=f"{cur.rowcount} open conflict(s) closed: {why}")
    return cur.rowcount

RETENTION_DAYS = 30

def purge_expired_inspections(conn, actor="retention"):
    """1800 retention: an inspection record (inspection + items +
    discrepancies + discrepancy photos) lives 30 days from submission,
    then is purged. ONE audit row per run when anything was removed;
    the original submission/review audit rows REMAIN (accountability
    trail, no photos). Vehicle photos are fleet data and NEVER expire
    here. Returns the number purged."""
    cutoff = (datetime.now(timezone.utc) -
              timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    with immediate_tx(conn):
        rows = conn.execute(
            "SELECT id FROM inspections WHERE created_at < ?",
            (cutoff,)).fetchall()
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        q = ",".join("?" * len(ids))
        conn.execute(
            "DELETE FROM discrepancy_photos WHERE discrepancy_id IN "
            f"(SELECT id FROM discrepancies WHERE inspection_id IN ({q}))",
            ids)
        conn.execute(
            f"DELETE FROM discrepancies WHERE inspection_id IN ({q})", ids)
        conn.execute(
            f"DELETE FROM inspection_items WHERE inspection_id IN ({q})", ids)
        conn.execute(f"DELETE FROM inspections WHERE id IN ({q})", ids)
        audit(conn, None, actor, "retention",
              detail=f"retention purge: {len(rows)} inspection(s) removed")
        return len(rows)

# ------------------------------------------------------------- derivations
def mc_rate_half_up(fmc, on_hand):
    """MC rate (percent, 1 decimal) rounded HALF-UP — the rule the
    client uses (Math.round on the same fraction). Python's round()
    is banker's rounding and disagrees on exact ties (1/16 -> 6.2
    server vs 6.3 client), breaking server/client parity; Decimal
    division of the exact fraction keeps those ties exact.
    (Review finding F2.)"""
    if not on_hand:
        return 0.0
    rate = (Decimal(fmc * 100) / Decimal(on_hand)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP)
    return float(rate)

def unit_stats(conn, unit_name):
    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM vehicles WHERE unit=? GROUP BY status",
        (unit_name,)).fetchall()
    counts = {r["status"]: r["c"] for r in rows}
    on_hand = sum(counts.values())
    auth = conn.execute(
        "SELECT COALESCE(SUM(authorized),0) s FROM authorizations WHERE unit=?",
        (unit_name,)).fetchone()["s"]
    demo = conn.execute(
        "SELECT COUNT(*) c FROM vehicles WHERE unit=? AND is_demo=1",
        (unit_name,)).fetchone()["c"]
    fmc = counts.get("FMC", 0)
    mc_rate = mc_rate_half_up(fmc, on_hand)
    return {"unit": unit_name, "on_hand": on_hand, "authorizations": auth,
            "fmc": fmc, "pmc": counts.get("PMC", 0), "nmc": counts.get("NMC", 0),
            "mc_rate": mc_rate, "includes_demo": bool(demo)}


def _unit_has_main_vco(conn, unit):
    return conn.execute(
        "SELECT COUNT(*) c FROM users WHERE role='vco' AND unit=?"
        " AND is_main_vco=1", (unit,)).fetchone()["c"] > 0


def _set_main_vco(conn, unit, user_id):
    """Make user_id the unit's main VCO, clearing any other main in
    that unit. Enforced here (write path), never just in the UI.
    Honorific only: no permission check anywhere reads this flag.
    Returns the displaced main's id, or None. Call sites already
    check role/unit; the assertion hardens the helper itself."""
    _row = conn.execute("SELECT role, unit FROM users WHERE id=?",
                        (user_id,)).fetchone()
    assert _row is not None and _row["role"] == "vco" and \
        _row["unit"] == unit, \
        "_set_main_vco called on a non-VCO or wrong-unit user"
    _prev = conn.execute(
        "SELECT id FROM users WHERE unit=? AND is_main_vco=1 AND id<>?",
        (unit, user_id)).fetchone()
    conn.execute("UPDATE users SET is_main_vco=0 WHERE unit=? AND"
                 " is_main_vco=1", (unit,))
    conn.execute("UPDATE users SET is_main_vco=1 WHERE id=?", (user_id,))
    return _prev["id"] if _prev else None


def _pass_main_vco(conn, unit, exclude_id=None):
    """Auto-pass: earliest-created remaining VCO of the unit becomes
    main; if none remains, the badge simply clears (no main row left
    behind, since the departing user is already gone/demoted)."""
    row = conn.execute(
        "SELECT id FROM users WHERE role='vco' AND unit=? AND id<>?"
        " ORDER BY id LIMIT 1", (unit, exclude_id or -1)).fetchone()
    if row:
        _set_main_vco(conn, unit, row["id"])
        return row["id"]
    return None


def fleet_snapshot(conn):
    # Photo bytes NEVER ride the snapshot: only a presence flag, so
    # fleet payloads and offline snapshots stay light. The bytes come
    # back on demand from GET /api/vehicles/{id}/photo.
    vehicles = [dict(r) for r in conn.execute(
        "SELECT id, unit, bumper, type, status, mileage, maint_due_date,"
        " maint_due_miles, location, notes, is_demo, client_op,"
        " updated_by, updated_at, (photo IS NOT NULL) AS photo"
        " FROM vehicles ORDER BY unit, bumper")]
    auths = [dict(r) for r in conn.execute(
        "SELECT * FROM authorizations ORDER BY unit, vehicle_type")]
    units = []
    sums = {"on_hand": 0, "authorizations": 0, "fmc": 0, "pmc": 0, "nmc": 0}
    group_demo = False
    # v1.1 semantics: 18 ASOG has its OWN fleet too. Each entry in
    # "units" is that unit's OWN numbers (18 ASOG's entry included);
    # the separate "group" entry is the sum over ALL FIVE units, ASOG's
    # own included — every vehicle counted exactly once in its own
    # unit's stats and once in the group total.
    for name, kind in UNITS:
        st = unit_stats(conn, name)
        st["kind"] = kind
        units.append(st)
        for k in sums:
            sums[k] += st[k]
        group_demo = group_demo or st["includes_demo"]
    group = {"unit": "18 ASOG", "kind": "group", **sums,
             "includes_demo": group_demo,
             "mc_rate": mc_rate_half_up(sums["fmc"], sums["on_hand"])}
    main_vcos = {}
    for r in conn.execute(
            "SELECT unit, display_name FROM users WHERE role='vco'"
            " AND is_main_vco=1"):
        main_vcos[r["unit"]] = r["display_name"]
    return {"units": units, "group": group, "vehicles": vehicles,
            "authorizations": auths, "utcs": utc_snapshot(conn),
            "main_vcos": main_vcos,
            "generated_at": now_iso()}

# ------------------------------------------------------------- UTCs
def utc_snapshot(conn):
    """All UTCs with their member vehicle ids, for the fleet snapshot
    (so UTCs are viewable from the cached snapshot while offline)."""
    members = {}
    for r in conn.execute("SELECT utc_id, vehicle_id FROM utc_vehicles"
                          " ORDER BY vehicle_id"):
        members.setdefault(r["utc_id"], []).append(r["vehicle_id"])
    out = []
    for r in conn.execute("SELECT * FROM utcs ORDER BY unit, code"):
        u = dict(r)
        # codes are stored uppercase on write (v1.1 fix S3); normalize
        # here too so a legacy lowercase row displays consistently
        u["code"] = (u["code"] or "").upper()
        u["vehicle_ids"] = members.get(u["id"], [])
        out.append(u)
    return out

UTC_CODE_MAX = 20

def validate_utc(data, partial=False):
    """Validate + coerce UTC fields (unit / code / title)."""
    clean = {}
    def present(f):
        return (f in data) if partial else True
    if present("unit"):
        unit = data.get("unit")
        if unit not in UNIT_NAMES:
            raise ValidationError("unit must be one of the five units")
        clean["unit"] = unit
    if present("code"):
        code = _req_text(data, "code")
        if not code:
            raise ValidationError("code is required (non-empty)")
        # UTC designators are canonically uppercase: normalize on write
        # so uniqueness is effectively case-insensitive (v1.1 fix S3 —
        # "AbC1" and "aBc1" must not coexist in one unit)
        code = code.upper()
        if len(code) > UTC_CODE_MAX:
            raise ValidationError(
                f"code must be at most {UTC_CODE_MAX} characters")
        clean["code"] = code
    if present("title"):
        title = data.get("title", "")
        clean["title"] = "" if title is None else str(title).strip()
    elif not partial:
        clean["title"] = ""
    return clean

# ------------------------------------------------------------- Excel import
# Zero-dependency .xlsx reader (an .xlsx is a zip of XML) + CSV, feeding a
# two-step preview -> confirm import. Import is ADD-ONLY: duplicates are
# skipped and reported, never overwritten. Values only — cell formulas are
# never evaluated; the stored text is what a formula's cached result says.
IMPORT_MAX_FILE = 5 * 1024 * 1024        # decoded file cap: 5 MB
IMPORT_JSON_CAP = 8 * 1024 * 1024        # request cap (base64 overhead)
IMPORT_MAX_ZIP_ENTRIES = 100
IMPORT_MAX_UNCOMPRESSED = 20 * 1024 * 1024
IMPORT_MAX_ROWS = 5000
# Column cap per row (v1.1 fix B1). Excel's own maximum is 16,384, but
# a fleet sheet uses a handful of columns, and each parsed row is
# materialized as a list as wide as its widest cell ref: at Excel's max
# and 5,000 rows that is ~82M slots of pure attacker-controlled width.
# 512 columns is far beyond any real fleet sheet and bounds a row to
# 512 entries (~2.5M slots worst case across the whole row cap).
IMPORT_MAX_COLS = 512

_HEADER_ALIASES = {
    "type": "type", "vehicle type": "type", "vehicle": "type",
    "asset number": "bumper", "asset #": "bumper", "asset no": "bumper",
    "asset": "bumper", "reg number": "bumper", "reg #": "bumper",
    "registration": "bumper", "bumper": "bumper", "bumper #": "bumper",
    "id": "bumper",
    "location": "location",
    "status": "status", "readiness": "status",
    "mileage": "mileage", "miles": "mileage", "hours": "mileage",
    "mileage/hours": "mileage", "mileage / hours": "mileage",
    "maintenance due": "maint_due_date",
    "maintenance due date": "maint_due_date",
    "maint due": "maint_due_date", "maint due date": "maint_due_date",
    "due date": "maint_due_date",
    "due at miles": "maint_due_miles",
    "maintenance due miles": "maint_due_miles",
    "maint due miles": "maint_due_miles",
    "notes": "notes", "note": "notes", "remarks": "notes",
}

def _norm_header(h):
    return " ".join(str(h or "").strip().lower().split()).rstrip(":")

def _local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag

def _xlsx_rows(data):
    """Extract rows (lists of cell strings) from the first worksheet.

    Allocation audit (v1.1 fix B1): everything materialized here is
    bounded by a constant, never by attacker-controlled magnitudes —
    part count (IMPORT_MAX_ZIP_ENTRIES), declared uncompressed size
    summed over parts (IMPORT_MAX_UNCOMPRESSED; zipfile also enforces
    each part's declared size while reading, and a CRC mismatch is a
    clean 400 per fix S2), the shared-strings list and the XML trees
    (both proportional to those size-capped parts), the row count
    (IMPORT_MAX_ROWS), and each row's WIDTH (IMPORT_MAX_COLS — the
    check fires while parsing the cell ref, before the row list is
    built)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except zipfile.BadZipFile:
        raise ValidationError("not a valid .xlsx file (cannot open the zip)")
    if len(infos) > IMPORT_MAX_ZIP_ENTRIES:
        raise ValidationError("xlsx has too many parts — refusing")
    if sum(i.file_size for i in infos) > IMPORT_MAX_UNCOMPRESSED:
        raise ValidationError("xlsx expands too large — refusing")
    names = set(zf.namelist())

    def read_xml(name):
        # a corrupt part (bad CRC, truncated or encrypted data) must
        # surface as the documented clean 400, not a 500 (fix S2)
        try:
            raw = zf.read(name)
        except (zipfile.BadZipFile, RuntimeError, EOFError, OSError):
            raise ValidationError(
                f"corrupt .xlsx file (cannot read part {name})")
        if b"<!DOCTYPE" in raw:
            raise ValidationError("xlsx part has a DOCTYPE — refusing")
        try:
            return ET.fromstring(raw)
        except ET.ParseError:
            raise ValidationError(f"malformed XML in {name}")

    shared = []
    if "xl/sharedStrings.xml" in names:
        root = read_xml("xl/sharedStrings.xml")
        for si in root:
            if _local(si.tag) == "si":
                shared.append("".join(t.text or "" for t in si.iter()
                                      if _local(t.tag) == "t"))
    sheets = sorted(n for n in names
                    if n.startswith("xl/worksheets/") and n.endswith(".xml")
                    and "/_" not in n)
    if not sheets:
        raise ValidationError("no worksheet found in the .xlsx file")
    root = read_xml(sheets[0])
    rows = []
    for row_el in root.iter():
        if _local(row_el.tag) != "row":
            continue
        cells = {}
        max_col = -1
        for c in row_el:
            if _local(c.tag) != "c":
                continue
            ref = c.get("r") or ""
            letters = "".join(ch for ch in ref if ch.isalpha())
            if letters and not letters.isascii():
                # Real Excel refs are [A-Za-z]+[0-9]+. Non-ASCII
                # "letters" (e.g. ß, whose .upper() is the 2-char
                # "SS") would crash ord() below — reject cleanly
                # instead (malformed file -> 400, never 500).
                raise ValidationError(
                    "sheet cell reference has non-ASCII column "
                    "letters — refusing")
            col = 0
            for ch in letters:
                col = col * 26 + (ord(ch.upper()) - 64)
                if col > IMPORT_MAX_COLS:
                    # hostile ref (e.g. ZZZZZZ1 -> column ~321M): die
                    # HERE, while parsing the ref — never let the row
                    # materialize at that width (fix B1). The running
                    # value only grows per letter, so this bails within
                    # a few characters however long the ref string is.
                    raise ValidationError(
                        f"sheet references a column beyond "
                        f"{IMPORT_MAX_COLS} — refusing")
            col = col - 1 if letters else len(cells)
            if col >= IMPORT_MAX_COLS:
                raise ValidationError(
                    f"sheet row is wider than {IMPORT_MAX_COLS} "
                    f"columns — refusing")
            ctype = c.get("t") or ""
            v_text, inline = None, None
            for child in c:
                lt = _local(child.tag)
                if lt == "v":
                    v_text = child.text
                elif lt == "is":
                    inline = "".join(t.text or "" for t in child.iter()
                                     if _local(t.tag) == "t")
            if ctype == "s" and v_text is not None:
                try:
                    idx = int(float(v_text))
                except (ValueError, OverflowError):
                    idx = -1
                # range-checked explicitly (fix S1): a negative index
                # would silently wrap to the LAST shared string in
                # Python, smuggling an unrelated value into the cell.
                # Out-of-range -> blank cell; parsing continues.
                value = shared[idx] if 0 <= idx < len(shared) else ""
            elif inline is not None:
                value = inline
            elif v_text is not None:
                value = v_text  # number or cached formula result, as text
            else:
                value = ""
            cells[col] = value
            max_col = max(max_col, col)
        rows.append([cells.get(i, "") for i in range(max_col + 1)])
        if len(rows) > IMPORT_MAX_ROWS + 1:
            raise ValidationError(f"sheet has more than {IMPORT_MAX_ROWS} rows")
    return rows

def _csv_rows(data):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    try:
        rows = [r for r in csv.reader(io.StringIO(text))]
    except csv.Error as e:
        raise ValidationError(f"could not parse CSV: {e}")
    if len(rows) > IMPORT_MAX_ROWS + 1:
        raise ValidationError(f"file has more than {IMPORT_MAX_ROWS} rows")
    return rows

def extract_sheet_rows(filename, raw):
    """Rows from an uploaded file by extension. 400 on anything else."""
    lower = (filename or "").lower()
    if lower.endswith(".xlsx"):
        return _xlsx_rows(raw)
    if lower.endswith(".csv"):
        return _csv_rows(raw)
    raise ValidationError("only .xlsx or .csv files can be imported")

def _import_number(value, field_label):
    """Parse a spreadsheet number cell -> int or None. Raises ValueError."""
    if value is None or str(value).strip() == "":
        return None
    s = str(value).strip().replace(",", "")
    try:
        n = float(s)
    except ValueError:
        raise ValueError(f"invalid {field_label} '{value}'")
    if n < 0 or n != int(n):
        raise ValueError(f"invalid {field_label} '{value}'")
    return int(n)

def _import_date(value):
    """Normalize a date cell to YYYY-MM-DD ('' when blank). ValueError else.
    Accepts ISO dates, MM/DD/YYYY, and raw Excel serial numbers."""
    s = str(value or "").strip()
    if not s:
        return ""
    if _DATE_RE.match(s):
        return valid_due_date(s)
    try:
        serial = float(s)
        if serial == int(serial) and 20000 < serial < 80000:
            d = _date(1899, 12, 30) + timedelta(days=int(serial))
            return d.isoformat()
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"invalid maintenance due date '{value}'")

def build_import_preview(conn, unit, filename, raw):
    """Parse + validate every row for import into `unit`. Returns the
    preview dict the VCO confirms from. Nothing is written here."""
    rows = extract_sheet_rows(filename, raw)
    if not rows:
        raise ValidationError("the sheet is empty")
    headers_raw = [str(h or "") for h in rows[0]]
    mapping, unmapped, used = {}, [], set()
    for i, h in enumerate(headers_raw):
        field = _HEADER_ALIASES.get(_norm_header(h))
        if field and field not in used:
            mapping[i] = field
            used.add(field)
        elif h.strip():
            unmapped.append(h.strip())
    existing = {r["bumper"] for r in conn.execute(
        "SELECT bumper FROM vehicles WHERE unit=?", (unit,))}
    seen = {}
    out_rows = []
    for idx, raw_row in enumerate(rows[1:], start=2):
        if not any(str(c).strip() for c in raw_row):
            continue  # blank row — silently ignored
        vals = {}
        for col, field in mapping.items():
            vals[field] = str(raw_row[col]).strip() if col < len(raw_row) else ""
        bumper = vals.get("bumper", "")
        row = {"row": idx, "bumper": bumper,
               "type": vals.get("type") or "Unknown",
               "status": None, "status_defaulted": False,
               "mileage": None, "maint_due_date": "",
               "maint_due_miles": None,
               "location": vals.get("location", ""),
               "notes": vals.get("notes", ""),
               "ok": True, "reason": ""}
        def skip(reason):
            row["ok"] = False
            row["reason"] = reason
        status_raw = vals.get("status", "")
        if status_raw:
            if status_raw.upper() in STATUSES:
                row["status"] = status_raw.upper()
            else:
                skip(f"invalid status '{status_raw}' (must be FMC/PMC/NMC)")
        else:
            row["status"] = "FMC"
            row["status_defaulted"] = True
        try:
            row["mileage"] = _import_number(vals.get("mileage"), "mileage")
        except ValueError as e:
            skip(str(e))
        try:
            row["maint_due_miles"] = _import_number(
                vals.get("maint_due_miles"), "maintenance due miles")
        except ValueError as e:
            skip(str(e))
        try:
            row["maint_due_date"] = _import_date(vals.get("maint_due_date"))
        except ValueError as e:
            skip(str(e))
        if row["ok"]:
            if not bumper:
                skip("missing asset number — required")
            elif bumper in seen:
                skip(f"duplicate asset number in file "
                     f"(first seen on row {seen[bumper]})")
            elif bumper in existing:
                skip(f"already in {unit}'s fleet — import never overwrites")
        if bumper and bumper not in seen:
            seen[bumper] = idx
        out_rows.append(row)
    return {
        "unit": unit, "filename": filename,
        "columns": {mapping[i]: headers_raw[i] for i in mapping},
        "unmapped_columns": unmapped,
        "rows": out_rows,
        "will_import": sum(1 for r in out_rows if r["ok"]),
        "skipped": sum(1 for r in out_rows if not r["ok"]),
    }

# ------------------------------------------------------------- demo data
DEMO = [
    # unit, bumper, type, status, mileage, due-in-days, location
    ("14 ASOS", "SAMPLE-101", "JLTV", "FMC", 12400, 90, "Motor Pool A"),
    ("14 ASOS", "SAMPLE-102", "JLTV", "PMC", 23100, 12, "Motor Pool A"),
    ("14 ASOS", "SAMPLE-103", "HMMWV", "FMC", 45200, 60, "Motor Pool A"),
    ("14 ASOS", "SAMPLE-104", "HMMWV", "NMC", 51800, -5, "Maintenance Bay"),
    ("14 ASOS", "SAMPLE-105", "LMTV", "FMC", 18900, 45, "Motor Pool B"),
    ("14 ASOS", "SAMPLE-106", "Trailer", "FMC", 0, 120, "Motor Pool B"),
    ("15 ASOS", "SAMPLE-201", "JLTV", "FMC", 9800, 75, "Motor Pool A"),
    ("15 ASOS", "SAMPLE-202", "HMMWV", "FMC", 33400, 20, "Motor Pool A"),
    ("15 ASOS", "SAMPLE-203", "F-250 Truck", "PMC", 61200, 8, "Motor Pool A"),
    ("15 ASOS", "SAMPLE-204", "Generator", "NMC", 1450, -2, "Maintenance Bay"),
    ("19 ASOS", "SAMPLE-301", "JLTV", "FMC", 15200, 110, "Motor Pool A"),
    ("19 ASOS", "SAMPLE-302", "JLTV", "FMC", 16750, 55, "Motor Pool A"),
    ("19 ASOS", "SAMPLE-303", "LMTV", "PMC", 27400, 15, "Motor Pool B"),
    ("19 ASOS", "SAMPLE-304", "HMMWV", "FMC", 29800, 80, "Motor Pool B"),
    ("20 ASOS", "SAMPLE-401", "HMMWV", "FMC", 40100, 33, "Motor Pool A"),
    ("20 ASOS", "SAMPLE-402", "JLTV", "NMC", 19300, -9, "Maintenance Bay"),
    ("20 ASOS", "SAMPLE-403", "Trailer", "FMC", 0, 150, "Motor Pool A"),
    ("20 ASOS", "SAMPLE-404", "F-250 Truck", "FMC", 55700, 64, "Motor Pool A"),
]
DEMO_AUTHS = [
    ("14 ASOS", "JLTV", 3), ("14 ASOS", "HMMWV", 3), ("14 ASOS", "LMTV", 2),
    ("14 ASOS", "Trailer", 2),
    ("15 ASOS", "JLTV", 2), ("15 ASOS", "HMMWV", 2),
    ("15 ASOS", "F-250 Truck", 2), ("15 ASOS", "Generator", 2),
    ("19 ASOS", "JLTV", 3), ("19 ASOS", "LMTV", 2), ("19 ASOS", "HMMWV", 2),
    ("20 ASOS", "HMMWV", 3), ("20 ASOS", "JLTV", 2), ("20 ASOS", "Trailer", 1),
    ("20 ASOS", "F-250 Truck", 2),
]

def purge_demo(conn, actor):
    """Delete ONLY demo-flagged vehicles and demo-flagged authorizations.
    Caller owns the transaction. Returns number of demo vehicles removed."""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM vehicles WHERE is_demo=1")]
    for vid in ids:
        close_open_conflicts(conn, vid, actor, "demo vehicle removed")
        conn.execute("DELETE FROM utc_vehicles WHERE vehicle_id=?", (vid,))
    conn.execute("DELETE FROM vehicles WHERE is_demo=1")
    conn.execute("DELETE FROM authorizations WHERE is_demo=1")
    for vid in ids:
        audit(conn, vid, actor, "delete", detail="demo vehicle removed")
    return len(ids)

def load_demo(conn, actor):
    """Idempotent replace-clean load: any existing demo data is purged
    first, so a second load never duplicates. Real authorizations are
    NEVER overwritten — demo authorizations are insert-only-if-absent.
    Caller owns the transaction."""
    from datetime import date, timedelta
    purge_demo(conn, actor)
    for unit, bumper, typ, status, miles, due_days, loc in DEMO:
        if bumper_taken(conn, unit, bumper):
            continue  # a real vehicle already owns this bumper # — skip
        due = (date.today() + timedelta(days=due_days)).isoformat()
        cur = conn.execute(
            "INSERT INTO vehicles(unit, bumper, type, status, mileage,"
            " maint_due_date, maint_due_miles, location, notes, is_demo,"
            " updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
            (unit, bumper, typ, status, miles, due, None, loc,
             "SAMPLE DATA — demo vehicle, not a real fleet record.",
             actor, now_iso()))
        audit(conn, cur.lastrowid, actor, "create", detail="demo vehicle loaded")
    for unit, typ, n in DEMO_AUTHS:
        conn.execute(
            "INSERT OR IGNORE INTO authorizations(unit, vehicle_type,"
            " authorized, is_demo) VALUES (?,?,?,1)",
            (unit, typ, n))

def remove_demo(conn, actor):
    n = purge_demo(conn, actor)
    return n

# ---------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    db_path = "skyline.db"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    # ---- plumbing
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_bytes(self, code, raw, mime):
        """Raw image response for the photo GET endpoints."""
        try:
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json_body(self, max_bytes=MAX_BODY):
        """Parse the request body with a hard size cap checked BEFORE
        parsing. Raises BodyError -> JSON 4xx (never a dead request).
        Import routes pass a larger cap (they carry a base64 file)."""
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            return {}
        try:
            n = int(raw_len)
        except (TypeError, ValueError):
            raise BodyError(400, "invalid Content-Length")
        if n < 0:
            raise BodyError(400, "invalid Content-Length")
        if n > max_bytes:
            # drain a bounded amount so the connection stays well-formed
            remaining = min(n, max_bytes * 2)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            raise BodyError(413, f"request body too large "
                                 f"({max_bytes // (1024 * 1024)} MB cap)")
        if n == 0:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw or b"{}")
        except Exception:
            raise BodyError(400, "malformed JSON body")
        if not isinstance(data, dict):
            raise BodyError(400, "JSON body must be an object")
        return data

    def _discard_body(self):
        """DELETE handlers take no body, but a client may send one anyway.
        Unread body bytes would be parsed as the START OF THE NEXT REQUEST
        on this keep-alive connection, corrupting it (non-JSON response).
        Drain the body before responding. Returns None when the connection
        is clean, else (status, message) to answer with — in that case the
        connection is marked for close so leftover bytes can never desync
        a following request."""
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            return None
        try:
            n = int(raw_len)
        except (TypeError, ValueError):
            self.close_connection = True
            return (400, "invalid Content-Length")
        if n < 0:
            self.close_connection = True
            return (400, "invalid Content-Length")
        if n > MAX_BODY:
            self.close_connection = True
            return (413, f"request body too large "
                         f"({MAX_BODY // (1024 * 1024)} MB cap)")
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
        return None

    @staticmethod
    def _path_id(path, prefix):
        """Integer id after prefix, or None (callers answer 404)."""
        part = path[len(prefix):].split("/")[0]
        try:
            value = int(part)
        except (TypeError, ValueError):
            return None
        # Python ints are unbounded; SQLite INTEGER is int64. An id
        # beyond that range can never exist — answer 404 (None), not
        # a 500 from the SQLite bind (review S1, v1.1.1).
        if not (-(2 ** 63) <= value <= 2 ** 63 - 1):
            return None
        return value

    @staticmethod
    def _route_id(path, *shape):
        """Anchored route match: shape segments, with "?" marking an
        integer id slot (int64-guarded like _path_id). Returns the list
        of parsed ids, or None when the path does not match EXACTLY.
        Prefix-only matching used to let trailing segments fall through
        to the wrong handler — e.g. DELETE /api/vehicles/{id}/photo/
        extra missed the photo branch and hit generic vehicle-delete,
        DELETING THE VEHICLE (v1.2 fix pass, reviewer B1). Single-id
        routes use this; legitimate suffixed routes (vehicle /photo,
        inspection /discrepancies/{did}/photos/{pid}, utc /vehicles
        membership, conflicts /resolve) have their own shapes."""
        parts = path.strip("/").split("/")
        if len(parts) != len(shape):
            return None
        ids = []
        for part, want in zip(parts, shape):
            if want == "?":
                try:
                    value = int(part)
                except ValueError:
                    return None
                if not (-(2 ** 63) <= value <= 2 ** 63 - 1):
                    return None
                ids.append(value)
            elif part != want:
                return None
        return ids

    def _conn(self):
        return connect(self.db_path)

    def _user(self, conn):
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token:
            return None
        row = conn.execute(
            "SELECT u.*, s.expires_at AS _exp FROM sessions s"
            " JOIN users u ON u.id=s.user_id WHERE s.token=?",
            (token,)).fetchone()
        if not row:
            return None
        user = dict(row)
        exp = user.pop("_exp", "")
        if exp and exp < now_iso():  # session expired (30-day TTL)
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            conn.commit()
            return None
        return user

    def _require(self, conn, roles=None):
        user = self._user(conn)
        if not user:
            self._send(401, {"error": "sign-in required"})
            return None
        if roles and user["role"] not in roles:
            self._send(403, {"error": "your role cannot do that"})
            return None
        return user

    # ---- routing (every route ends in a JSON response, even on bugs)
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return self.api_get(parsed.path, parse_qs(parsed.query))
        return self.static_get(parsed.path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self.api_post(path)
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self.api_put(path)
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        err = self._discard_body()
        if err is not None:
            return self._send(err[0], {"error": err[1]})
        if path.startswith("/api/"):
            return self.api_delete(path)
        self._send(404, {"error": "not found"})

    def do_HEAD(self):
        self.do_GET()

    def static_get(self, path):
        if path == "/":
            path = "/index.html"
        full = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            # SPA fallback for app routes
            full = os.path.join(STATIC, "index.html")
        ctype = {".html": "text/html", ".js": "text/javascript",
                 ".css": "text/css", ".json": "application/json",
                 ".png": "image/png", ".svg": "image/svg+xml"}.get(
                     os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self._send(200, data, ctype)

    # ---- API: reads
    def api_get(self, path, query=None):
        conn = self._conn()
        try:
            if path == "/api/setup/status":
                n = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
                return self._send(200, {"setup_needed": n == 0})
            if path == "/api/export.csv":
                # v1.3: export is a READ of the shared fleet picture, so
                # both roles may use it (operator reads everything);
                # every WRITE below is role-gated.
                if not self._require(conn):
                    return
                snap = fleet_snapshot(conn)
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["unit", "bumper", "type", "status", "mileage_hours",
                            "maint_due_date", "maint_due_miles", "location",
                            "notes", "sample_data", "updated_by", "updated_at"])
                for v in snap["vehicles"]:
                    w.writerow([csv_safe(v["unit"]), csv_safe(v["bumper"]),
                                csv_safe(v["type"]), csv_safe(v["status"]),
                                v["mileage"], csv_safe(v["maint_due_date"]),
                                v["maint_due_miles"], csv_safe(v["location"]),
                                csv_safe(v["notes"]),
                                "SAMPLE" if v["is_demo"] else "",
                                csv_safe(v["updated_by"]),
                                csv_safe(v["updated_at"])])
                body = buf.getvalue().encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv")
                    self.send_header("Content-Disposition",
                                     'attachment; filename="skyline_fleet.csv"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            user = self._require(conn)
            if not user:
                return
            if path == "/api/me":
                return self._send(200, {"user": public_user(user)})
            if path == "/api/fleet":
                return self._send(200, fleet_snapshot(conn))
            if path == "/api/audit":
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM audit ORDER BY id DESC LIMIT 500")]
                return self._send(200, {"audit": rows})
            if path == "/api/conflicts":
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM conflicts WHERE resolved=0 ORDER BY id DESC")]
                return self._send(200, {"conflicts": rows})
            ids = self._route_id(path, "api", "vehicles", "?", "photo")
            if ids is not None:
                vid = ids[0]
                row = conn.execute(
                    "SELECT photo, photo_mime FROM vehicles WHERE id=?",
                    (vid,)).fetchone()
                if not row or row["photo"] is None:
                    return self._send(404, {"error": "vehicle photo not found"})
                return self._send_bytes(200, row["photo"],
                                        row["photo_mime"] or "image/jpeg")
            # GET /api/inspections/{iid}/discrepancies/{did}/photos/{pid}
            # (anchored: a missing pid no longer falls through to the
            # inspection-detail handler — v1.2 fix pass, routing nit)
            ids = self._route_id(path, "api", "inspections", "?",
                                 "discrepancies", "?", "photos", "?")
            if ids is not None:
                iid, did, pid = ids
                prow = conn.execute(
                    "SELECT image, mime FROM discrepancy_photos"
                    " WHERE id=? AND discrepancy_id=?",
                    (pid, did)).fetchone()
                if not prow:
                    return self._send(404, {"error": "photo not found"})
                disc = conn.execute(
                    "SELECT inspection_id FROM discrepancies WHERE id=?",
                    (did,)).fetchone()
                if not disc or disc["inspection_id"] != iid:
                    return self._send(404, {"error": "photo not found"})
                return self._send_bytes(200, prow["image"], prow["mime"])
            if path == "/api/inspections":
                return self._list_inspections(conn, query or {})
            ids = self._route_id(path, "api", "inspections", "?")
            if ids is not None:
                # retention runs on detail reads too (v1.2 fix pass):
                # a 31-day-old inspection must 404 here, not just on
                # the next list read
                purge_expired_inspections(conn)
                full = self._inspection_full(conn, ids[0])
                if not full:
                    return self._send(404, {"error": "inspection not found"})
                return self._send(200, full)
            if path == "/api/users":
                if user["role"] != "vco":
                    return self._send(403, {"error": "VCO only"})
                rows = [public_user(dict(r)) for r in
                        conn.execute("SELECT * FROM users ORDER BY username")]
                return self._send(200, {"users": rows})
            self._send(404, {"error": "not found"})
        except ValidationError as e:
            self._send(e.status, {"error": e.message})
        except Exception:
            traceback.print_exc()
            self._send(500, {"error": "internal error"})
        finally:
            conn.close()

    # ---- API: writes
    def api_post(self, path):
        conn = self._conn()
        try:
            if path.startswith("/api/import/"):
                cap = IMPORT_JSON_CAP
            elif "/photo" in path:
                cap = PHOTO_BODY_CAP
            else:
                cap = MAX_BODY
            data = self._json_body(cap)
            if path == "/api/setup":
                return self._setup(conn, data)
            if path == "/api/login":
                row = conn.execute("SELECT * FROM users WHERE username=?",
                                   (data.get("username", ""),)).fetchone()
                if not row or not verify_password(data.get("password", ""),
                                                  row["password_hash"]):
                    return self._send(401, {"error": "bad username or password"})
                token = secrets.token_urlsafe(32)
                conn.execute("INSERT INTO sessions(token, user_id, created_at,"
                             " expires_at) VALUES (?,?,?,?)",
                             (token, row["id"], now_iso(), session_expiry_iso()))
                conn.commit()
                return self._send(200, {"token": token,
                                        "user": public_user(dict(row))})
            if path == "/api/logout":
                auth = self.headers.get("Authorization", "")
                token = auth[7:] if auth.startswith("Bearer ") else ""
                conn.execute("DELETE FROM sessions WHERE token=?", (token,))
                conn.commit()
                return self._send(200, {"ok": True})
            user = self._require(conn)
            if not user:
                return
            if path == "/api/users":
                if user["role"] != "vco":
                    return self._send(403, {"error": "VCO only"})
                return self._create_user(conn, data,
                                         role=data.get("role", "operator"),
                                         actor=user["display_name"])
            if path == "/api/inspections":
                # operator or VCO may submit (both roles)
                return self._create_inspection(conn, data, user)
            if self._route_id(path, "api", "inspections", "?",
                              "review") is not None:
                if user["role"] != "vco":
                    return self._send(403, {"error": "VCO only"})
                return self._review_inspection(conn, path, data, user)
            if self._route_id(path, "api", "vehicles", "?",
                              "photo") is not None:
                # operator or VCO may attach a vehicle photo
                return self._attach_vehicle_photo(conn, path, data, user)
            if self._route_id(path, "api", "inspections", "?",
                              "discrepancies", "?", "photos") is not None:
                # operator or VCO may attach a discrepancy photo
                return self._attach_discrepancy_photo(conn, path, data, user)
            if path == "/api/vehicles":
                if not self._require_role(user, ("vco",)):
                    return
                return self._insert_vehicle(conn, data, user)
            if path == "/api/authorizations":
                if not self._require_role(user, ("vco",)):
                    return
                return self._upsert_authorization(conn, data, user)
            if path == "/api/utcs":
                if not self._require_role(user, ("vco",)):
                    return
                return self._create_utc(conn, data, user)
            if self._route_id(path, "api", "utcs", "?",
                              "vehicles") is not None:
                if not self._require_role(user, ("vco",)):
                    return
                return self._utc_add_vehicle(conn, path, data, user)
            if path == "/api/import/preview":
                if not self._require_role(user, ("vco",)):
                    return
                return self._import_preview(conn, data, user)
            if path == "/api/import/confirm":
                if not self._require_role(user, ("vco",)):
                    return
                return self._import_confirm(conn, data, user)
            if path == "/api/demo/load":
                if user["role"] != "vco":
                    return self._send(403, {"error": "VCO only"})
                with immediate_tx(conn):
                    load_demo(conn, user["display_name"])
                return self._send(200, {"ok": True})
            if path == "/api/demo/remove":
                if user["role"] != "vco":
                    return self._send(403, {"error": "VCO only"})
                with immediate_tx(conn):
                    n = remove_demo(conn, user["display_name"])
                return self._send(200, {"ok": True, "removed": n})
            if self._route_id(path, "api", "conflicts", "?",
                              "resolve") is not None:
                if not self._require_role(user, ("vco",)):
                    return
                return self._resolve_conflict(conn, path, data, user)
            self._send(404, {"error": "not found"})
        except BodyError as e:
            self._send(e.status, {"error": e.message})
        except ValidationError as e:
            self._send(e.status, {"error": e.message})
        except Exception:
            traceback.print_exc()
            self._send(500, {"error": "internal error"})
        finally:
            conn.close()

    def _setup(self, conn, data):
        """First-run setup — atomic: BEGIN IMMEDIATE serializes concurrent
        setup calls so exactly ONE VCO can ever be created here."""
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        display = (data.get("display_name") or username).strip()
        unit = data.get("unit", "18 ASOG")
        if not username or len(password) < 8:
            return self._send(400, {"error":
                "username required; password must be at least 8 characters"})
        if unit not in UNIT_NAMES:
            return self._send(400, {"error": "unit must be one of the five units"})
        with immediate_tx(conn):
            n = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            if n:
                return self._send(403, {"error": "setup already complete"})
            cur = conn.execute(
                "INSERT INTO users(username, display_name, password_hash, unit,"
                " role, is_main_vco, created_at) VALUES (?,?,?,?,?,?,?)",
                (username, display, hash_password(password), unit, "vco", 1,
                 now_iso()))
            uid = cur.lastrowid
            audit(conn, None, "setup", "account",
                  detail=f"created {username} (vco)")
            token = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions(token, user_id, created_at,"
                         " expires_at) VALUES (?,?,?,?)",
                         (token, uid, now_iso(), session_expiry_iso()))
            row = conn.execute("SELECT * FROM users WHERE id=?",
                               (uid,)).fetchone()
        # setup signs the new VCO in directly (session returned)
        return self._send(201, {"user": public_user(dict(row)), "token": token})

    def _resolve_conflict(self, conn, path, data, user):
        cid = self._path_id(path, "/api/conflicts/")
        if cid is None:
            return self._send(404, {"error": "conflict not found"})
        # Resolve-once requires the read + resolved-check INSIDE the
        # write transaction: checking before BEGIN IMMEDIATE let parallel
        # resolves all see resolved=0 and each write its own (contradictory)
        # resolution audit row. Re-checked under the write lock, exactly
        # one resolver wins; the rest get 409.
        with immediate_tx(conn):
            c = conn.execute("SELECT * FROM conflicts WHERE id=?",
                             (cid,)).fetchone()
            if not c:
                return self._send(404, {"error": "conflict not found"})
            action = data.get("action")
            if action not in ("apply", "keep"):
                return self._send(400, {"error":
                    'action must be "apply" or "keep"'})
            if c["resolved"]:
                return self._send(409, {"error": "conflict already resolved"})
            if action == "apply":
                conn.execute("UPDATE vehicles SET status=?, updated_by=?,"
                             " updated_at=? WHERE id=?",
                             (c["attempted"], user["display_name"],
                              now_iso(), c["vehicle_id"]))
                audit(conn, c["vehicle_id"], user["display_name"],
                      "update", field="status", old=c["current"],
                      new=c["attempted"], detail="conflict resolved: applied")
            else:
                audit(conn, c["vehicle_id"], user["display_name"],
                      "update", field="status", old=c["attempted"],
                      new=c["current"], detail="conflict resolved: kept current")
            conn.execute("UPDATE conflicts SET resolved=1 WHERE id=?", (cid,))
        return self._send(200, {"ok": True})

    def _upsert_authorization(self, conn, data, user):
        unit = data.get("unit")
        typ = _req_text(data, "vehicle_type")
        if unit not in UNIT_NAMES:
            raise ValidationError("unit must be one of the five units")
        if not typ:
            raise ValidationError("vehicle_type is required (non-empty)")
        authorized = strict_nonneg_int(data.get("authorized"), "authorized",
                                        allow_none=False)
        with immediate_tx(conn):
            old = conn.execute(
                "SELECT * FROM authorizations WHERE unit=? AND vehicle_type=?",
                (unit, typ)).fetchone()
            # a real edit also clears the demo flag on that authorization
            conn.execute(
                "INSERT INTO authorizations(unit, vehicle_type, authorized,"
                " is_demo) VALUES (?,?,?,0) ON CONFLICT(unit, vehicle_type)"
                " DO UPDATE SET authorized=excluded.authorized, is_demo=0",
                (unit, typ, authorized))
            if not old or old["authorized"] != authorized:
                audit(conn, None, user["display_name"], "authorization",
                      field="authorized",
                      old=("" if not old else old["authorized"]),
                      new=authorized, detail=f"{unit} / {typ}")
        return self._send(200, {"ok": True})

    def _delete_authorization(self, conn, path, user):
        """Remove an authorization row entirely (v1.1.1). The unit's
        vehicles of that type are NOT touched — only the allowance
        line goes away. Audited like every authorization change:
        old = the count that was removed, new = "" (gone)."""
        aid = self._path_id(path, "/api/authorizations/")
        if aid is None:
            return self._send(404, {"error": "authorization not found"})
        with immediate_tx(conn):
            row = conn.execute("SELECT * FROM authorizations WHERE id=?",
                               (aid,)).fetchone()
            if not row:
                return self._send(404, {"error": "authorization not found"})
            conn.execute("DELETE FROM authorizations WHERE id=?", (aid,))
            audit(conn, None, user["display_name"], "authorization",
                  field="authorized", old=row["authorized"], new="",
                  detail=f"{row['unit']} / {row['vehicle_type']} (deleted)")
        return self._send(200, {"ok": True})

    # ---- UTC handlers (VCO only; every mutation audited)
    def _create_utc(self, conn, data, user):
        clean = validate_utc(data, partial=False)
        with immediate_tx(conn):
            # UPPER() on the stored side too: a legacy lowercase row
            # must still collide with the normalized new code (S3)
            dupe = conn.execute(
                "SELECT id FROM utcs WHERE unit=? AND UPPER(code)=?",
                (clean["unit"], clean["code"])).fetchone()
            if dupe:
                return self._send(409, {"error":
                    f"UTC code {clean['code']} already exists "
                    f"in {clean['unit']}"})
            try:
                cur = conn.execute(
                    "INSERT INTO utcs(unit, code, title, created_by,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?)",
                    (clean["unit"], clean["code"], clean["title"],
                     user["display_name"], now_iso(), now_iso()))
            except sqlite3.IntegrityError:
                return self._send(409, {"error":
                    f"UTC code {clean['code']} already exists "
                    f"in {clean['unit']}"})
            audit(conn, None, user["display_name"], "utc-create",
                  detail=f"{clean['unit']} UTC {clean['code']}"
                         f" — {clean['title']}")
            row = conn.execute("SELECT * FROM utcs WHERE id=?",
                               (cur.lastrowid,)).fetchone()
        out = dict(row)
        out["vehicle_ids"] = []
        return self._send(201, {"utc": out})

    def _get_utc_or_404(self, conn, utc_id):
        if utc_id is None:
            return None
        return conn.execute("SELECT * FROM utcs WHERE id=?",
                            (utc_id,)).fetchone()

    def _update_utc(self, conn, path, data, user):
        utc_id = self._path_id(path, "/api/utcs/")
        if utc_id is None:
            return self._send(404, {"error": "UTC not found"})
        clean = validate_utc(data, partial=True)
        with immediate_tx(conn):
            row = self._get_utc_or_404(conn, utc_id)
            if not row:
                return self._send(404, {"error": "UTC not found"})
            row = dict(row)
            new_unit = clean.get("unit", row["unit"])
            new_code = clean.get("code", row["code"])
            if (new_unit, new_code) != (row["unit"], row["code"]):
                dupe = conn.execute(
                    "SELECT id FROM utcs WHERE unit=? AND UPPER(code)=?"
                    " AND id<>?", (new_unit, new_code, utc_id)).fetchone()
                if dupe:
                    return self._send(409, {"error":
                        f"UTC code {new_code} already exists in {new_unit}"})
            if new_unit != row["unit"]:
                members = conn.execute(
                    "SELECT COUNT(*) c FROM utc_vehicles WHERE utc_id=?",
                    (utc_id,)).fetchone()["c"]
                if members:
                    raise ValidationError(
                        "cannot move a UTC with assigned vehicles "
                        "to another unit")
            changed = False
            for f in ("unit", "code", "title"):
                if f in clean and clean[f] != row[f]:
                    conn.execute(f"UPDATE utcs SET {f}=? WHERE id=?",
                                 (clean[f], utc_id))
                    audit(conn, None, user["display_name"], "utc-update",
                          field=f, old=row[f], new=clean[f],
                          detail=f"UTC #{utc_id} ({row['unit']})")
                    changed = True
            if changed:
                conn.execute("UPDATE utcs SET updated_at=? WHERE id=?",
                             (now_iso(), utc_id))
            fresh = conn.execute("SELECT * FROM utcs WHERE id=?",
                                 (utc_id,)).fetchone()
        out = dict(fresh)
        out["vehicle_ids"] = [r["vehicle_id"] for r in conn.execute(
            "SELECT vehicle_id FROM utc_vehicles WHERE utc_id=?"
            " ORDER BY vehicle_id", (utc_id,))]
        return self._send(200, {"utc": out})

    def _delete_utc(self, conn, path, user):
        utc_id = self._path_id(path, "/api/utcs/")
        if utc_id is None:
            return self._send(404, {"error": "UTC not found"})
        with immediate_tx(conn):
            row = self._get_utc_or_404(conn, utc_id)
            if not row:
                return self._send(404, {"error": "UTC not found"})
            # deleting a UTC removes only the package — never vehicles
            conn.execute("DELETE FROM utc_vehicles WHERE utc_id=?",
                         (utc_id,))
            conn.execute("DELETE FROM utcs WHERE id=?", (utc_id,))
            audit(conn, None, user["display_name"], "utc-delete",
                  detail=f"{row['unit']} UTC {row['code']} deleted"
                         " (vehicles kept)")
        return self._send(200, {"ok": True})

    def _utc_add_vehicle(self, conn, path, data, user):
        utc_id = self._path_id(path, "/api/utcs/")
        if utc_id is None:
            return self._send(404, {"error": "UTC not found"})
        vehicle_id = data.get("vehicle_id")
        if isinstance(vehicle_id, bool) or \
                not isinstance(vehicle_id, (int, str)) or \
                (isinstance(vehicle_id, str)
                 and not vehicle_id.strip().isdigit()):
            raise ValidationError("vehicle_id must be an integer")
        vehicle_id = int(vehicle_id)
        if not (-(2 ** 63) <= vehicle_id <= 2 ** 63 - 1):
            raise ValidationError("vehicle does not exist")
        with immediate_tx(conn):
            utc = self._get_utc_or_404(conn, utc_id)
            if not utc:
                return self._send(404, {"error": "UTC not found"})
            veh = conn.execute("SELECT * FROM vehicles WHERE id=?",
                               (vehicle_id,)).fetchone()
            if not veh:
                raise ValidationError("vehicle does not exist")
            if veh["unit"] != utc["unit"]:
                raise ValidationError(
                    f"vehicle belongs to {veh['unit']}; a UTC can only "
                    f"hold vehicles of its own unit ({utc['unit']})")
            cur = conn.execute(
                "INSERT OR IGNORE INTO utc_vehicles(utc_id, vehicle_id)"
                " VALUES (?,?)", (utc_id, vehicle_id))
            if cur.rowcount:
                audit(conn, vehicle_id, user["display_name"],
                      "utc-add-vehicle",
                      detail=f"added to {utc['unit']} UTC {utc['code']}")
        return self._send(200, {"ok": True})

    def _utc_remove_vehicle(self, conn, path, user):
        parts = path.strip("/").split("/")  # api/utcs/{id}/vehicles/{vid}
        try:
            utc_id, vehicle_id = int(parts[2]), int(parts[4])
        except (IndexError, ValueError):
            return self._send(404, {"error": "UTC membership not found"})
        if not (-(2 ** 63) <= utc_id <= 2 ** 63 - 1) or \
                not (-(2 ** 63) <= vehicle_id <= 2 ** 63 - 1):
            return self._send(404, {"error": "UTC membership not found"})
        with immediate_tx(conn):
            utc = self._get_utc_or_404(conn, utc_id)
            if not utc:
                return self._send(404, {"error": "UTC not found"})
            cur = conn.execute(
                "DELETE FROM utc_vehicles WHERE utc_id=? AND vehicle_id=?",
                (utc_id, vehicle_id))
            if not cur.rowcount:
                return self._send(404, {"error":
                                        "vehicle is not in this UTC"})
            veh = conn.execute("SELECT bumper FROM vehicles WHERE id=?",
                               (vehicle_id,)).fetchone()
            audit(conn, vehicle_id, user["display_name"],
                  "utc-remove-vehicle",
                  detail=f"removed {veh['bumper'] if veh else vehicle_id}"
                         f" from {utc['unit']} UTC {utc['code']}")
        return self._send(200, {"ok": True})

    # ---- AF Form 1800 inspections (operator or VCO submits;
    #      VCO reviews; the vehicle FMC/PMC/NMC status is never changed
    #      by an inspection — that stays a VCO action)
    def _inspection_full(self, conn, iid):
        """Inspection + its items + discrepancies, or None."""
        row = conn.execute("SELECT * FROM inspections WHERE id=?",
                           (iid,)).fetchone()
        if not row:
            return None
        items = [dict(r) for r in conn.execute(
            "SELECT item_no, item_text, applies, result, comment"
            " FROM inspection_items WHERE inspection_id=? ORDER BY id",
            (iid,))]
        for it in items:
            it["applies"] = bool(it["applies"])
        discs = [dict(r) for r in conn.execute(
            "SELECT * FROM discrepancies WHERE inspection_id=? ORDER BY id",
            (iid,))]
        # detail JSON carries photo counts/URLs per discrepancy, NOT bytes
        for d in discs:
            d["photos"] = [{
                "id": p["id"],
                "url": f"/api/inspections/{iid}/discrepancies/{d['id']}"
                       f"/photos/{p['id']}"}
                for p in conn.execute(
                    "SELECT id FROM discrepancy_photos"
                    " WHERE discrepancy_id=? ORDER BY id", (d["id"],))]
        return {"inspection": dict(row), "items": items,
                "discrepancies": discs}

    def _create_inspection(self, conn, data, user):
        vehicle_id = data.get("vehicle_id")
        if isinstance(vehicle_id, bool) or \
                not isinstance(vehicle_id, (int, str)) or \
                (isinstance(vehicle_id, str)
                 and not vehicle_id.strip().isdigit()):
            raise ValidationError("vehicle_id must be an integer")
        vehicle_id = int(vehicle_id)
        # int64 guard (v1.2 fix pass): an absurd id must 404, never
        # 500 on the SQLite bind
        if not (-(2 ** 63) <= vehicle_id <= 2 ** 63 - 1):
            return self._send(404, {"error": "vehicle not found"})
        month_year = data.get("month_year")
        if not isinstance(month_year, str) or not month_year.strip():
            raise ValidationError("month_year is required")
        month_year = month_year.strip()
        if len(month_year) > 40:
            raise ValidationError("month_year must be <= 40 chars")
        mileage = strict_nonneg_int(data.get("mileage_hours"),
                                    "mileage_hours")
        tires = {}
        for f in ("tire_front", "tire_rear"):
            v = data.get(f)
            if v is None:
                tires[f] = ""
            elif not isinstance(v, str):
                raise ValidationError(f"{f} must be text")
            elif len(v.strip()) > 40:
                raise ValidationError(f"{f} must be <= 40 chars")
            else:
                tires[f] = v.strip()
        items = validate_inspection_items(data.get("items"))
        with immediate_tx(conn):
            veh = conn.execute("SELECT * FROM vehicles WHERE id=?",
                               (vehicle_id,)).fetchone()
            if not veh:
                return self._send(404, {"error": "vehicle not found"})
            veh = dict(veh)
            cur = conn.execute(
                "INSERT INTO inspections(vehicle_id, unit, bumper, operator,"
                " month_year, mileage_hours, tire_front, tire_rear, status,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (vehicle_id, veh["unit"], veh["bumper"],
                 user["display_name"], month_year, mileage,
                 tires["tire_front"], tires["tire_rear"], "submitted",
                 now_iso()))
            iid = cur.lastrowid
            failed = 0
            for it in items:
                conn.execute(
                    "INSERT INTO inspection_items(inspection_id, item_no,"
                    " item_text, applies, result, comment)"
                    " VALUES (?,?,?,?,?,?)",
                    (iid, it["item_no"], it["item_text"],
                     1 if it["applies"] else 0, it["result"] or "",
                     it["comment"]))
                if it["applies"] and it["result"] == "fail":
                    failed += 1
                    conn.execute(
                        "INSERT INTO discrepancies(inspection_id, item_no,"
                        " description, date_found, miles_hours, status)"
                        " VALUES (?,?,?,?,?,?)",
                        (iid, it["item_no"],
                         it["comment"] or "Failed — see comment",
                         _date.today().isoformat(), mileage, "open"))
            if mileage is not None:
                old = veh["mileage"]
                conn.execute("UPDATE vehicles SET mileage=?, updated_by=?,"
                             " updated_at=? WHERE id=?",
                             (mileage, user["display_name"], now_iso(),
                              vehicle_id))
                audit(conn, vehicle_id, user["display_name"], "update",
                      field="mileage", old=old, new=mileage,
                      detail="1800 inspection mileage/hours")
            audit(conn, vehicle_id, user["display_name"], "inspection",
                  detail=f"1800 inspection submitted ({month_year}) — "
                         f"{len(items)} items, {failed} "
                         f"{'discrepancy' if failed == 1 else 'discrepancies'}")
        return self._send(201, self._inspection_full(conn, iid))

    def _list_inspections(self, conn, query):
        # retention runs before every inspection list read, so the VCO
        # queue naturally shows only unpurged items
        purge_expired_inspections(conn)
        clauses, args = [], []
        vid = (query.get("vehicle_id") or [None])[0]
        if vid is not None:
            if not vid.isdigit():
                raise ValidationError("vehicle_id must be an integer")
            if not (-(2 ** 63) <= int(vid) <= 2 ** 63 - 1):
                # int64 guard (v1.2 fix pass): absurd filter id 400s,
                # never 500s on the SQLite bind
                raise ValidationError("vehicle_id is out of range")
            clauses.append("vehicle_id=?")
            args.append(int(vid))
        unit = (query.get("unit") or [None])[0]
        if unit is not None:
            if unit not in UNIT_NAMES:
                raise ValidationError("unit must be one of the five units")
            clauses.append("unit=?")
            args.append(unit)
        rev = (query.get("reviewed") or [None])[0]
        if rev is not None:
            if rev == "true":
                clauses.append("status='reviewed'")
            elif rev == "false":
                clauses.append("status='submitted'")
            else:
                raise ValidationError("reviewed must be true or false")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = [dict(r) for r in conn.execute(
            "SELECT inspections.*, "
            "(SELECT COUNT(*) FROM inspection_items WHERE "
            " inspection_items.inspection_id = inspections.id) AS item_count,"
            "(SELECT COUNT(*) FROM discrepancies WHERE "
            " discrepancies.inspection_id = inspections.id) AS fail_count"
            " FROM inspections" + where +
            " ORDER BY CASE WHEN status='submitted' THEN 0 ELSE 1 END,"
            " id DESC", args)]
        return self._send(200, {"inspections": rows})

    def _review_inspection(self, conn, path, data, user):
        iid = self._path_id(path, "/api/inspections/")
        if iid is None:
            return self._send(404, {"error": "inspection not found"})
        dispositions = data.get("dispositions", [])
        if dispositions is None:
            dispositions = []
        if not isinstance(dispositions, list):
            raise ValidationError("dispositions must be a list")
        clean_disp = []
        seen_disp = set()
        for d in dispositions:
            if not isinstance(d, dict):
                raise ValidationError("each disposition must be an object")
            did = d.get("discrepancy_id")
            if isinstance(did, bool) or not isinstance(did, int):
                raise ValidationError("discrepancy_id must be an integer")
            if not (-(2 ** 63) <= did <= 2 ** 63 - 1):
                raise ValidationError("discrepancy_id is out of range")
            if did in seen_disp:
                raise ValidationError(
                    "duplicate discrepancy in dispositions")
            seen_disp.add(did)
            st = d.get("status")
            if st not in ("corrected", "delayed", "disposition"):
                raise ValidationError(
                    "disposition status must be corrected, delayed or "
                    "disposition")
            clean_disp.append((did, st))
        with immediate_tx(conn):
            row = conn.execute("SELECT * FROM inspections WHERE id=?",
                               (iid,)).fetchone()
            if not row:
                return self._send(404, {"error": "inspection not found"})
            if row["status"] == "reviewed":
                return self._send(409, {"error": "inspection already reviewed"})
            for did, st in clean_disp:
                disc = conn.execute(
                    "SELECT * FROM discrepancies WHERE id=? AND "
                    "inspection_id=?", (did, iid)).fetchone()
                if not disc:
                    raise ValidationError(
                        f"discrepancy {did} does not belong to this "
                        "inspection")
                if disc["status"] != st:
                    conn.execute("UPDATE discrepancies SET status=? "
                                 "WHERE id=?", (st, did))
                    audit(conn, row["vehicle_id"], user["display_name"],
                          "inspection-review", field="discrepancy",
                          old=disc["status"], new=st,
                          detail=f"discrepancy {did}")
            conn.execute("UPDATE inspections SET status='reviewed',"
                         " reviewed_by=?, reviewed_at=? WHERE id=?",
                         (user["display_name"], now_iso(), iid))
            audit(conn, row["vehicle_id"], user["display_name"],
                  "inspection-review",
                  detail=f"1800 inspection reviewed ({row['month_year']})")
        full = self._inspection_full(conn, iid)
        return self._send(200, {"inspection": full["inspection"]})

    def _attach_vehicle_photo(self, conn, path, data, user):
        vid = self._path_id(path, "/api/vehicles/")
        if vid is None:
            return self._send(404, {"error": "vehicle not found"})
        raw, mime = parse_photo_data_url(data)
        with immediate_tx(conn):
            veh = conn.execute("SELECT photo FROM vehicles WHERE id=?",
                               (vid,)).fetchone()
            if not veh:
                return self._send(404, {"error": "vehicle not found"})
            had = veh["photo"] is not None
            conn.execute("UPDATE vehicles SET photo=?, photo_mime=?,"
                         " updated_by=?, updated_at=? WHERE id=?",
                         (raw, mime, user["display_name"], now_iso(), vid))
            audit(conn, vid, user["display_name"], "photo", field="photo",
                  old="present" if had else "", new="present",
                  detail="vehicle photo attached" if not had
                         else "vehicle photo replaced")
        return self._send(200, {"ok": True, "photo": True})

    def _attach_discrepancy_photo(self, conn, path, data, user):
        parts = path.strip("/").split("/")
        try:
            iid, did = int(parts[2]), int(parts[4])
        except (IndexError, ValueError):
            return self._send(404, {"error": "photo not found"})
        # int64 guard (v1.2 fix pass): absurd path ids 404, never 500
        if not (-(2 ** 63) <= iid <= 2 ** 63 - 1) or \
                not (-(2 ** 63) <= did <= 2 ** 63 - 1):
            return self._send(404, {"error": "photo not found"})
        raw, mime = parse_photo_data_url(data)
        with immediate_tx(conn):
            insp = conn.execute(
                "SELECT vehicle_id, status FROM inspections WHERE id=?",
                (iid,)).fetchone()
            if not insp:
                return self._send(404, {"error": "inspection not found"})
            # review closes the record (v1.2 fix pass): no new photos
            # on a reviewed inspection
            if insp["status"] == "reviewed":
                return self._send(409, {"error":
                                        "inspection already reviewed"})
            disc = conn.execute(
                "SELECT id FROM discrepancies WHERE id=? AND inspection_id=?",
                (did, iid)).fetchone()
            if not disc:
                return self._send(404, {"error": "discrepancy not found"})
            n = conn.execute(
                "SELECT COUNT(*) c FROM discrepancy_photos"
                " WHERE discrepancy_id=?", (did,)).fetchone()["c"]
            if n >= 4:
                return self._send(409, {"error":
                                        "discrepancy already has 4 photos"})
            cur = conn.execute(
                "INSERT INTO discrepancy_photos(discrepancy_id, image, mime,"
                " created_at) VALUES (?,?,?,?)",
                (did, raw, mime, now_iso()))
            pid = cur.lastrowid
            audit(conn, insp["vehicle_id"], user["display_name"],
                  "inspection-photo",
                  detail=f"photo added to discrepancy {did}"
                         f" (inspection {iid})")
        url = f"/api/inspections/{iid}/discrepancies/{did}/photos/{pid}"
        return self._send(201, {"photo_id": pid, "url": url})

    def _delete_discrepancy_photo(self, conn, path, user):
        """Remove one discrepancy photo (v1.2 fix pass): without this,
        a mistaken photo permanently occupied one of the 4 cap slots
        until the 30-day purge. Operator or VCO, audited, 404s
        matching the GET branch. Allowed on reviewed inspections too:
        removal takes nothing away from the closed record."""
        ids = self._route_id(path, "api", "inspections", "?",
                             "discrepancies", "?", "photos", "?")
        if ids is None:
            return self._send(404, {"error": "photo not found"})
        iid, did, pid = ids
        with immediate_tx(conn):
            prow = conn.execute(
                "SELECT id FROM discrepancy_photos"
                " WHERE id=? AND discrepancy_id=?",
                (pid, did)).fetchone()
            if not prow:
                return self._send(404, {"error": "photo not found"})
            disc = conn.execute(
                "SELECT inspection_id FROM discrepancies WHERE id=?",
                (did,)).fetchone()
            if not disc or disc["inspection_id"] != iid:
                return self._send(404, {"error": "photo not found"})
            insp = conn.execute(
                "SELECT vehicle_id FROM inspections WHERE id=?",
                (iid,)).fetchone()
            conn.execute("DELETE FROM discrepancy_photos WHERE id=?",
                         (pid,))
            audit(conn, insp["vehicle_id"] if insp else None,
                  user["display_name"], "inspection-photo",
                  detail=f"photo removed from discrepancy {did}"
                         f" (inspection {iid})")
        return self._send(200, {"ok": True})

    # ---- Excel import (two-step: preview, then confirm; VCO only)
    @staticmethod
    def _import_file_from(data):
        unit = data.get("unit")
        if unit not in UNIT_NAMES:
            raise ValidationError("unit must be one of the five units")
        filename = os.path.basename(str(data.get("filename") or ""))[:120]
        b64 = data.get("content_b64") or ""
        if not isinstance(b64, str) or not b64:
            raise ValidationError("no file content received")
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise ValidationError("file content is not valid base64")
        if len(raw) > IMPORT_MAX_FILE:
            raise ValidationError(
                f"file too large — import cap is "
                f"{IMPORT_MAX_FILE // (1024 * 1024)} MB")
        if not raw:
            raise ValidationError("the file is empty")
        return unit, filename, raw

    def _import_preview(self, conn, data, user):
        unit, filename, raw = self._import_file_from(data)
        preview = build_import_preview(conn, unit, filename, raw)
        return self._send(200, preview)

    def _import_confirm(self, conn, data, user):
        """Create the confirmed rows. The client sends back the rows it
        previewed as will-import; EVERY row is fully re-validated here
        (same validated insert path as a manual create) — the preview is
        never trusted. Duplicates that appear between preview and
        confirm are skipped, not overwritten."""
        unit = data.get("unit")
        if unit not in UNIT_NAMES:
            raise ValidationError("unit must be one of the five units")
        filename = os.path.basename(str(data.get("filename") or ""))[:120]
        rows = data.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValidationError("no rows to import")
        if len(rows) > IMPORT_MAX_ROWS:
            raise ValidationError(f"too many rows (cap {IMPORT_MAX_ROWS})")
        created, skipped = [], []
        with immediate_tx(conn):
            for r in rows:
                if not isinstance(r, dict):
                    skipped.append({"bumper": "", "reason": "bad row"})
                    continue
                payload = {"unit": unit, "bumper": r.get("bumper"),
                           "type": r.get("type") or "Unknown",
                           "status": r.get("status") or "FMC",
                           "mileage": r.get("mileage"),
                           "maint_due_date": r.get("maint_due_date") or "",
                           "maint_due_miles": r.get("maint_due_miles"),
                           "location": r.get("location") or "",
                           "notes": r.get("notes") or ""}
                try:
                    clean = validate_vehicle(payload, partial=False)
                except ValidationError as e:
                    skipped.append({"bumper": str(r.get("bumper") or ""),
                                    "reason": e.message})
                    continue
                if bumper_taken(conn, unit, clean["bumper"]):
                    skipped.append({"bumper": clean["bumper"],
                                    "reason": "already in this unit's "
                                              "fleet — not overwritten"})
                    continue
                cur = conn.execute(
                    "INSERT INTO vehicles(unit, bumper, type, status,"
                    " mileage, maint_due_date, maint_due_miles, location,"
                    " notes, updated_by, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (clean["unit"], clean["bumper"], clean["type"],
                     clean["status"], clean.get("mileage"),
                     clean.get("maint_due_date", ""),
                     clean.get("maint_due_miles"),
                     clean.get("location", ""), clean.get("notes", ""),
                     user["display_name"], now_iso()))
                audit(conn, cur.lastrowid, user["display_name"], "create",
                      detail=f"imported from {filename or 'spreadsheet'} "
                             f"into {unit}")
                created.append(clean["bumper"])
        return self._send(201, {"created": created, "skipped": skipped,
                                "created_count": len(created),
                                "skipped_count": len(skipped)})

    def _require_role(self, user, roles):
        if user["role"] not in roles:
            self._send(403, {"error": "your role cannot do that"})
            return False
        return True

    def _create_user(self, conn, data, role, actor):
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        display = (data.get("display_name") or username).strip()
        unit = data.get("unit", "")
        if not username or len(password) < 8:
            return self._send(400, {"error":
                "username required; password must be at least 8 characters"})
        if role not in ROLES:
            return self._send(400, {"error": "invalid role"})
        if unit not in UNIT_NAMES:
            return self._send(400, {"error": "unit must be one of the five units"})
        # v1.3 fix pass: create-path badge consistency — PUT 400s on
        # is_main_vco:true for a non-VCO target, so create must too
        # instead of silently ignoring the flag.
        if "is_main_vco" in data and \
                data["is_main_vco"] not in (True, False, 0, 1):
            return self._send(400, {"error":
                                    "is_main_vco must be true or false"})
        if data.get("is_main_vco") and role != "vco":
            return self._send(400, {"error":
                                    "only a VCO can be the unit VCO"})
        # the whole create (insert + badge + audit) runs in one
        # write transaction — previously the insert committed, then
        # the badge set committed separately, so a crash between the
        # two left a half-created account.
        with immediate_tx(conn):
            try:
                cur = conn.execute(
                    "INSERT INTO users(username, display_name, password_hash, unit,"
                    " role, is_main_vco, created_at) VALUES (?,?,?,?,?,?,?)",
                    (username, display, hash_password(password), unit, role, 0,
                     now_iso()))
            except sqlite3.IntegrityError:
                return self._send(409, {"error": "username already exists"})
            # v1.3 main-VCO default: the first VCO affiliated with a unit
            # becomes its main VCO automatically; an explicit is_main_vco
            # request transfers the badge (clearing the previous main).
            # No cap on VCO headcount: any number of VCOs may exist.
            is_main = 0
            displaced = None
            if role == "vco":
                if data.get("is_main_vco") or not _unit_has_main_vco(conn, unit):
                    displaced = _set_main_vco(conn, unit, cur.lastrowid)
                    is_main = 1
            detail = f"created {username} ({role})"
            if is_main:
                detail += " — Unit VCO"
            audit(conn, None, actor, "account", detail=detail)
            if displaced is not None:
                # badge side-effect, audited too: the displaced main
                # loses the badge even though the request named only
                # the new account.
                drow = conn.execute("SELECT username FROM users WHERE id=?",
                                    (displaced,)).fetchone()
                if drow:
                    audit(conn, None, actor, "account", field="is_main_vco",
                          old=1, new=0, detail=f"user {drow['username']}")
            row = conn.execute("SELECT * FROM users WHERE id=?",
                               (cur.lastrowid,)).fetchone()
        return self._send(201, {"user": public_user(dict(row))})

    def _insert_vehicle(self, conn, data, user):
        clean = validate_vehicle(data, partial=False)
        op_id = data.get("op_id") or data.get("client_op") or ""
        if not isinstance(op_id, str):
            op_id = str(op_id)
        # Uniqueness check + insert MUST be one write transaction:
        # check-then-insert outside BEGIN IMMEDIATE let parallel creates
        # all pass the check and insert duplicate (unit, bumper) rows.
        # The UNIQUE(unit, bumper) index is the DB-level backstop; the
        # IntegrityError arm below translates any index hit into the
        # same 409 / dedup answers.
        with immediate_tx(conn):
            if op_id:
                existing = conn.execute(
                    "SELECT * FROM vehicles WHERE client_op=?",
                    (op_id,)).fetchone()
                if existing:  # idempotent replay of a queued create
                    return self._send(200, {"vehicle": public_vehicle(existing),
                                            "deduplicated": True})
            if bumper_taken(conn, clean["unit"], clean["bumper"]):
                return self._send(409, {"error":
                    f"bumper # {clean['bumper']} already exists "
                    f"in {clean['unit']}"})
            try:
                cur = conn.execute(
                    "INSERT INTO vehicles(unit, bumper, type, status, mileage,"
                    " maint_due_date, maint_due_miles, location, notes,"
                    " client_op, updated_by, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (clean["unit"], clean["bumper"], clean["type"],
                     clean["status"], clean.get("mileage"),
                     clean.get("maint_due_date", ""),
                     clean.get("maint_due_miles"), clean.get("location", ""),
                     clean.get("notes", ""), op_id, user["display_name"],
                     now_iso()))
            except sqlite3.IntegrityError:
                if op_id:
                    existing = conn.execute(
                        "SELECT * FROM vehicles WHERE client_op=?",
                        (op_id,)).fetchone()
                    if existing:
                        return self._send(200, {"vehicle": public_vehicle(existing),
                                                "deduplicated": True})
                return self._send(409, {"error":
                    "bumper # already exists in unit"})
            audit(conn, cur.lastrowid, user["display_name"], "create",
                  detail=f"{clean['unit']} {clean['bumper']} ({clean['type']})")
            row = conn.execute("SELECT * FROM vehicles WHERE id=?",
                               (cur.lastrowid,)).fetchone()
        return self._send(201, {"vehicle": public_vehicle(row)})

    # ---- API: update / delete
    def api_put(self, path):
        conn = self._conn()
        try:
            data = self._json_body()
            user = self._require(conn)
            if not user:
                return
            ids = self._route_id(path, "api", "vehicles", "?")
            if ids is not None:
                if not self._require_role(user, ("vco",)):
                    return
                vid = ids[0]
                with immediate_tx(conn):
                    row = conn.execute("SELECT * FROM vehicles WHERE id=?",
                                       (vid,)).fetchone()
                    if not row:
                        return self._send(404, {"error": "vehicle not found"})
                    code, payload = self._update_vehicle(
                        conn, dict(row), data, user)
                return self._send(code, payload)
            if self._route_id(path, "api", "users", "?") is not None:
                if user["role"] != "vco":
                    return self._send(403, {"error": "VCO only"})
                return self._update_user(conn, path, data, user)
            if self._route_id(path, "api", "utcs", "?") is not None:
                if not self._require_role(user, ("vco",)):
                    return
                return self._update_utc(conn, path, data, user)
            self._send(404, {"error": "not found"})
        except BodyError as e:
            self._send(e.status, {"error": e.message})
        except ValidationError as e:
            self._send(e.status, {"error": e.message})
        except Exception:
            traceback.print_exc()
            self._send(500, {"error": "internal error"})
        finally:
            conn.close()

    def _update_user(self, conn, path, data, actor_user):
        uid = self._path_id(path, "/api/users/")
        if uid is None:
            return self._send(404, {"error": "user not found"})
        target = conn.execute("SELECT * FROM users WHERE id=?",
                              (uid,)).fetchone()
        if not target:
            return self._send(404, {"error": "user not found"})
        target = dict(target)
        updates, changes = {}, []  # changes: (field, old, new-for-audit)
        if "role" in data:
            if data["role"] not in ROLES:
                raise ValidationError("invalid role")
            if data["role"] != target["role"]:
                # the last-VCO guard itself is enforced inside the
                # write transaction below (fresh read under the lock);
                # checking here, before BEGIN IMMEDIATE, let two
                # concurrent demotions both read count=2 and both
                # proceed, leaving ZERO VCOs with no in-app path back
                # (v1.2 fix pass)
                updates["role"] = data["role"]
                changes.append(("role", target["role"], data["role"]))
        if "unit" in data:
            if data["unit"] not in UNIT_NAMES:
                raise ValidationError("unit must be one of the five units")
            if data["unit"] != target["unit"]:
                updates["unit"] = data["unit"]
                changes.append(("unit", target["unit"], data["unit"]))
        if "display_name" in data:
            dn = str(data["display_name"]).strip()
            if dn and dn != target["display_name"]:
                updates["display_name"] = dn
                changes.append(("display_name", target["display_name"], dn))
        if data.get("password"):
            if len(data["password"]) < 8:
                raise ValidationError("password too short (min 8)")
            updates["password_hash"] = hash_password(data["password"])
            changes.append(("password", "(set)", "(reset)"))
        # v1.3 main-VCO transfer: VCO-only (the route already gates),
        # audited, and enforced in this write path. Only a VCO can
        # hold the badge; the flag grants no permissions anywhere.
        main_requested = None
        if "is_main_vco" in data:
            if data["is_main_vco"] not in (True, False, 0, 1):
                raise ValidationError("is_main_vco must be true or false")
            main_requested = bool(data["is_main_vco"])
            final_role = updates.get("role", target["role"])
            if main_requested and final_role != "vco":
                raise ValidationError("only a VCO can be the unit VCO")
        if not updates and main_requested is None:
            return self._send(200, {"ok": True})  # no-op: no audit, no writes
        with immediate_tx(conn):
            # last-VCO guard, re-checked under BEGIN IMMEDIATE where
            # writers serialize: a concurrent demotion has already
            # committed by the time this transaction starts, so the
            # second demoter sees the true count and is refused.
            fresh = conn.execute("SELECT * FROM users WHERE id=?",
                                 (uid,)).fetchone()
            if not fresh:
                return self._send(404, {"error": "user not found"})
            fresh = dict(fresh)
            # stale-read guard: `updates`/`changes` were built from
            # the pre-transaction read; a concurrent writer may have
            # already applied the same change. Drop no-ops and re-base
            # audit old values on the fresh row, so the audit trail
            # never claims a change that did not happen.
            for _f in ("role", "unit", "display_name"):
                if _f in updates:
                    if fresh[_f] == updates[_f]:
                        del updates[_f]
                        changes = [c for c in changes if c[0] != _f]
                    else:
                        changes = [(f, fresh[_f] if f == _f else o, n)
                                   for (f, o, n) in changes]
            old_main = int(fresh["is_main_vco"] or 0)
            old_unit = fresh["unit"]
            if not updates and main_requested is not None and \
                    main_requested == bool(old_main):
                return self._send(200, {"ok": True})  # badge already as asked
            if "role" in updates and fresh["role"] == "vco":
                vcos = conn.execute(
                    "SELECT COUNT(*) c FROM users WHERE role='vco'"
                    ).fetchone()["c"]
                if vcos <= 1:
                    raise ValidationError(
                        "cannot demote the last VCO", status=400)
            if updates:
                sets = ", ".join(f"{k}=?" for k in updates)
                conn.execute(f"UPDATE users SET {sets} WHERE id=?",
                             (*updates.values(), uid))
            # credential/role changes revoke that user's sessions
            if "password_hash" in updates or "role" in updates:
                conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            new_row = conn.execute("SELECT * FROM users WHERE id=?",
                                   (uid,)).fetchone()
            new_role = new_row["role"]
            new_unit = new_row["unit"]
            # badge transitions, all inside this transaction.
            # badge_audits: (user_id, old, new) for OTHER users whose
            # badge moves as a side effect (displaced main, auto-pass
            # recipient) — the target's own badge change is audited
            # via `changes` below.
            badge_audits = []
            if old_main and (new_role != "vco" or new_unit != old_unit):
                # main VCO leaves the seat (demotion or unit move):
                # clear, then auto-pass to the earliest remaining VCO
                # of the OLD unit; if none remains it simply clears.
                conn.execute("UPDATE users SET is_main_vco=0 WHERE id=?",
                             (uid,))
                _new_main = _pass_main_vco(conn, old_unit, uid)
                if _new_main is not None:
                    badge_audits.append((_new_main, 0, 1))
            if new_role == "vco":
                if main_requested is True:
                    _displaced = _set_main_vco(conn, new_unit, uid)
                    if _displaced is not None:
                        badge_audits.append((_displaced, 1, 0))
                elif main_requested is False:
                    conn.execute("UPDATE users SET is_main_vco=0 WHERE id=?",
                                 (uid,))
                    # explicit false by a main auto-passes, exactly as
                    # demotion does — otherwise the unit is left with
                    # VCOs but no main.
                    if old_main and new_unit == old_unit:
                        _new_main = _pass_main_vco(conn, old_unit, uid)
                        if _new_main is not None:
                            badge_audits.append((_new_main, 0, 1))
                elif old_main and new_unit == old_unit:
                    pass  # main stays main in the same unit
                elif new_unit != old_unit or "role" in updates:
                    # promotion, or a main-less move: fill an empty
                    # seat, never displace an existing main
                    if not _unit_has_main_vco(conn, new_unit):
                        _set_main_vco(conn, new_unit, uid)
                    elif new_unit != old_unit:
                        conn.execute(
                            "UPDATE users SET is_main_vco=0 WHERE id=?",
                            (uid,))
            else:
                conn.execute("UPDATE users SET is_main_vco=0 WHERE id=?",
                             (uid,))
            final_main = conn.execute(
                "SELECT is_main_vco FROM users WHERE id=?",
                (uid,)).fetchone()["is_main_vco"]
            if int(final_main or 0) != old_main:
                changes.append(("is_main_vco", old_main,
                                int(final_main or 0)))
            for field, old, new in changes:
                audit(conn, None, actor_user["display_name"], "account",
                      field=field, old=old, new=new,
                      detail=f"user {target['username']}")
            for _bid, _o, _n in badge_audits:
                _brow = conn.execute("SELECT username FROM users WHERE id=?",
                                     (_bid,)).fetchone()
                if _brow:
                    audit(conn, None, actor_user["display_name"], "account",
                          field="is_main_vco", old=_o, new=_n,
                          detail=f"user {_brow['username']}")
        return self._send(200, {"ok": True})

    def _delete_user(self, conn, path, actor_user):
        """VCO-only user removal (v1.3: new endpoint — v1.2 had create
        and demote but no remove). Last-VCO guard inside the write
        transaction, like demotion; a removed main VCO auto-passes the
        badge to the earliest remaining VCO of that unit."""
        uid = self._path_id(path, "/api/users/")
        if uid is None:
            return self._send(404, {"error": "user not found"})
        target = conn.execute("SELECT * FROM users WHERE id=?",
                              (uid,)).fetchone()
        if not target:
            return self._send(404, {"error": "user not found"})
        target = dict(target)
        with immediate_tx(conn):
            fresh = conn.execute("SELECT * FROM users WHERE id=?",
                                 (uid,)).fetchone()
            if not fresh:
                return self._send(404, {"error": "user not found"})
            if fresh["role"] == "vco":
                vcos = conn.execute(
                    "SELECT COUNT(*) c FROM users WHERE role='vco'"
                    ).fetchone()["c"]
                if vcos <= 1:
                    raise ValidationError(
                        "cannot remove the last VCO", status=400)
            was_main = int(fresh["is_main_vco"] or 0)
            unit = fresh["unit"]
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM users WHERE id=?", (uid,))
            new_main = _pass_main_vco(conn, unit, uid) if was_main else None
            audit(conn, None, actor_user["display_name"], "account",
                  detail=f"removed user {target['username']}")
            if was_main:
                # badge side-effects, audited too: the removed main
                # loses the badge, and the auto-pass recipient gains it.
                audit(conn, None, actor_user["display_name"], "account",
                      field="is_main_vco", old=1, new=0,
                      detail=f"user {target['username']}")
                if new_main is not None:
                    brow = conn.execute(
                        "SELECT username FROM users WHERE id=?",
                        (new_main,)).fetchone()
                    if brow:
                        audit(conn, None, actor_user["display_name"],
                              "account", field="is_main_vco", old=0, new=1,
                              detail=f"user {brow['username']}")
        return self._send(200, {"ok": True})

    def _update_vehicle(self, conn, row, data, user):
        """Field-level merge inside the caller's transaction. A status
        change whose base_status no longer matches the server's current
        status is a sync conflict: other fields still merge, the status is
        NOT overwritten, a conflict + audit flag are written, and a VCO
        resolves it later. A true no-op writes nothing at all.
        Returns (status_code, payload)."""
        vid = row["id"]
        clean = validate_vehicle(data, partial=True)
        new_unit = clean.get("unit", row["unit"])
        new_bumper = clean.get("bumper", row["bumper"])
        if (new_unit, new_bumper) != (row["unit"], row["bumper"]) and \
                bumper_taken(conn, new_unit, new_bumper, exclude_id=vid):
            raise ValidationError(
                f"bumper # {new_bumper} already exists in {new_unit}",
                status=409)
        conflict = None
        changed = False
        # unit+bumper move atomically in ONE statement (the caller's
        # BEGIN IMMEDIATE already serialized us against other writers):
        # updating the two columns in separate statements would let a
        # UNIQUE(unit, bumper) index see a transient duplicate pair —
        # e.g. unit moved first while the old bumper still matches a
        # vehicle already in the target unit.
        if (new_unit, new_bumper) != (row["unit"], row["bumper"]):
            try:
                conn.execute("UPDATE vehicles SET unit=?, bumper=?"
                             " WHERE id=?", (new_unit, new_bumper, vid))
            except sqlite3.IntegrityError:
                raise ValidationError(
                    f"bumper # {new_bumper} already exists in {new_unit}",
                    status=409)
            for f in ("unit", "bumper"):
                if f in clean and row[f] != clean[f]:
                    audit(conn, vid, user["display_name"], "update",
                          field=f, old=row[f], new=clean[f])
                    row[f] = clean[f]
            changed = True
        # A vehicle that changed unit can no longer sit in UTCs of its
        # old unit (a UTC only holds its own unit's vehicles): drop any
        # memberships whose UTC belongs to a different unit now.
        cur_unit_row = conn.execute(
            "SELECT unit FROM vehicles WHERE id=?", (vid,)).fetchone()
        if cur_unit_row:
            cur = conn.execute(
                "DELETE FROM utc_vehicles WHERE vehicle_id=? AND utc_id IN"
                " (SELECT id FROM utcs WHERE unit<>?)",
                (vid, cur_unit_row["unit"]))
            if cur.rowcount:
                audit(conn, vid, user["display_name"], "utc-remove-vehicle",
                      detail="membership dropped: vehicle moved to "
                             f"{cur_unit_row['unit']}")
                changed = True
        for field in VEHICLE_FIELDS:
            if field not in clean:
                continue
            if field in ("unit", "bumper"):
                continue  # handled atomically above
            new = clean[field]
            if field == "status":
                base = data.get("base_status")
                if base is not None and base != row["status"] \
                        and new != row["status"]:
                    cur = conn.execute(
                        "INSERT INTO conflicts(vehicle_id, actor, field,"
                        " attempted, current, created_at) VALUES (?,?,?,?,?,?)",
                        (vid, user["display_name"], "status", new,
                         row["status"], now_iso()))
                    audit(conn, vid, user["display_name"], "conflict",
                          field="status", old=row["status"], new=new,
                          detail="offline sync conflict — server status kept",
                          is_conflict=1)
                    conflict = {"id": cur.lastrowid, "field": "status",
                                "attempted": new, "current": row["status"]}
                    changed = True
                    continue
            if row[field] != new:
                conn.execute(f"UPDATE vehicles SET {field}=? WHERE id=?",
                             (new, vid))
                audit(conn, vid, user["display_name"], "update", field=field,
                      old=row[field], new=new)
                row[field] = new
                changed = True
        if changed:
            conn.execute(
                "UPDATE vehicles SET updated_by=?, updated_at=? WHERE id=?",
                (user["display_name"], now_iso(), vid))
        fresh = conn.execute("SELECT * FROM vehicles WHERE id=?",
                             (vid,)).fetchone()
        out = {"vehicle": public_vehicle(fresh)}
        if conflict:
            out["conflict"] = conflict
        return 200, out

    def api_delete(self, path):
        conn = self._conn()
        try:
            user = self._require(conn)
            if not user:
                return
            ids = self._route_id(path, "api", "vehicles", "?", "photo")
            if ids is not None:
                # operator or VCO may remove a vehicle photo
                vid = ids[0]
                with immediate_tx(conn):
                    veh = conn.execute(
                        "SELECT photo FROM vehicles WHERE id=?",
                        (vid,)).fetchone()
                    if not veh:
                        return self._send(404, {"error": "vehicle not found"})
                    if veh["photo"] is not None:
                        conn.execute(
                            "UPDATE vehicles SET photo=NULL, photo_mime='',"
                            " updated_by=?, updated_at=? WHERE id=?",
                            (user["display_name"], now_iso(), vid))
                        audit(conn, vid, user["display_name"], "photo",
                              field="photo", old="present", new="",
                              detail="vehicle photo removed")
                return self._send(200, {"ok": True, "photo": False})
            if self._route_id(path, "api", "inspections", "?",
                              "discrepancies", "?", "photos",
                              "?") is not None:
                # operator or VCO may remove a discrepancy photo
                return self._delete_discrepancy_photo(conn, path, user)
            if self._route_id(path, "api", "users", "?") is not None:
                if user["role"] != "vco":
                    return self._send(403, {"error": "VCO only"})
                return self._delete_user(conn, path, user)
            ids = self._route_id(path, "api", "vehicles", "?")
            if ids is not None:
                if not self._require_role(user, ("vco",)):
                    return
                vid = ids[0]
                with immediate_tx(conn):
                    row = conn.execute("SELECT * FROM vehicles WHERE id=?",
                                       (vid,)).fetchone()
                    if not row:
                        return self._send(404, {"error": "vehicle not found"})
                    # deleting a vehicle drops its UTC memberships
                    conn.execute("DELETE FROM utc_vehicles"
                                 " WHERE vehicle_id=?", (vid,))
                    conn.execute("DELETE FROM vehicles WHERE id=?", (vid,))
                    audit(conn, vid, user["display_name"], "delete",
                          detail=f"{row['unit']} {row['bumper']}")
                    close_open_conflicts(conn, vid, user["display_name"],
                                         "vehicle deleted")
                return self._send(200, {"ok": True})
            if self._route_id(path, "api", "authorizations",
                              "?") is not None:
                if not self._require_role(user, ("vco",)):
                    return
                return self._delete_authorization(conn, path, user)
            if self._route_id(path, "api", "utcs", "?",
                              "vehicles", "?") is not None:
                if not self._require_role(user, ("vco",)):
                    return
                return self._utc_remove_vehicle(conn, path, user)
            if self._route_id(path, "api", "utcs", "?") is not None:
                if not self._require_role(user, ("vco",)):
                    return
                return self._delete_utc(conn, path, user)
            self._send(404, {"error": "not found"})
        except BodyError as e:
            self._send(e.status, {"error": e.message})
        except ValidationError as e:
            self._send(e.status, {"error": e.message})
        except Exception:
            traceback.print_exc()
            self._send(500, {"error": "internal error"})
        finally:
            conn.close()

def public_user(u):
    return {"id": u["id"], "username": u["username"],
            "display_name": u["display_name"], "unit": u["unit"],
            "role": u["role"],
            "is_main_vco": int(u.get("is_main_vco") or 0)}

def public_vehicle(v):
    """Vehicle dict safe for JSON: photo bytes never leave the server —
    only a presence flag (1/0)."""
    d = dict(v)
    d["photo"] = 1 if d.get("photo") is not None else 0
    d.pop("photo_mime", None)
    return d

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default=os.path.join(BASE, "skyline.db"))
    args = ap.parse_args()
    Handler.db_path = args.db
    conn = connect(args.db)
    init_db(conn)
    purged = purge_expired_inspections(conn)
    if purged:
        print(f"Skyline retention purge: {purged} inspection(s) removed")
    conn.close()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Skyline running at http://{args.host}:{args.port}  (db: {args.db})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
