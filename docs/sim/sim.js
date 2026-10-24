/* Skyline WEB SIM — a simulated backend that runs entirely in the
   browser, so the REAL Skyline frontend (app.js, byte-identical to
   the shipped app) can be tried with no server at all.

   How it works: this file loads BEFORE app.js and replaces
   window.fetch. Any request to a path starting with /api/ is answered
   from an in-memory simulation of server.py's API — same JSON
   shapes, same status codes, same error strings, same validation
   rules. Everything else falls through to the real fetch.

   State is pure in-memory: edits work for the session, a page
   refresh resets to the seeded SAMPLE demo fleet.

   Honesty rules (standing): the seed data is the app's own SAMPLE
   demo fleet; a persistent on-screen pill says this is a sim; and
   Excel (.xlsx) import is NOT faked — parsing .xlsx is the Python
   server's job, so the sim answers import previews for .xlsx with a
   plain explanation and accepts .csv instead.

   Node note: the backend factory is exported via module.exports
   when available (used by tests/sim_check.js); browser behavior is
   unchanged. */
(function () {
"use strict";

/* ============================================================ constants
   Mirrored from server.py — keep in sync if the server changes. */
const UNITS = [
  ["18 ASOG", "group"],
  ["14 ASOS", "squadron"],
  ["15 ASOS", "squadron"],
  ["19 ASOS", "squadron"],
  ["20 ASOS", "squadron"],
];
const UNIT_NAMES = UNITS.map(u => u[0]);
const STATUSES = ["FMC", "PMC", "NMC"];
const ROLES = ["viewer", "editor", "admin"];
const VEHICLE_FIELDS = ["unit", "bumper", "type", "status", "mileage",
  "maint_due_date", "maint_due_miles", "location", "notes"];
const UTC_CODE_MAX = 20;
const IMPORT_MAX_FILE = 5 * 1024 * 1024;
const IMPORT_MAX_ROWS = 5000;

const HEADER_ALIASES = {
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
};

/* Demo fleet — copied 1:1 from server.py's DEMO / DEMO_AUTHS.
   due-in-days is relative to "today", exactly like the server's
   loader (date.today() + timedelta(days=due_days)). */
const DEMO = [
  ["14 ASOS", "SAMPLE-101", "JLTV", "FMC", 12400, 90, "Motor Pool A"],
  ["14 ASOS", "SAMPLE-102", "JLTV", "PMC", 23100, 12, "Motor Pool A"],
  ["14 ASOS", "SAMPLE-103", "HMMWV", "FMC", 45200, 60, "Motor Pool A"],
  ["14 ASOS", "SAMPLE-104", "HMMWV", "NMC", 51800, -5, "Maintenance Bay"],
  ["14 ASOS", "SAMPLE-105", "LMTV", "FMC", 18900, 45, "Motor Pool B"],
  ["14 ASOS", "SAMPLE-106", "Trailer", "FMC", 0, 120, "Motor Pool B"],
  ["15 ASOS", "SAMPLE-201", "JLTV", "FMC", 9800, 75, "Motor Pool A"],
  ["15 ASOS", "SAMPLE-202", "HMMWV", "FMC", 33400, 20, "Motor Pool A"],
  ["15 ASOS", "SAMPLE-203", "F-250 Truck", "PMC", 61200, 8, "Motor Pool A"],
  ["15 ASOS", "SAMPLE-204", "Generator", "NMC", 1450, -2, "Maintenance Bay"],
  ["19 ASOS", "SAMPLE-301", "JLTV", "FMC", 15200, 110, "Motor Pool A"],
  ["19 ASOS", "SAMPLE-302", "JLTV", "FMC", 16750, 55, "Motor Pool A"],
  ["19 ASOS", "SAMPLE-303", "LMTV", "PMC", 27400, 15, "Motor Pool B"],
  ["19 ASOS", "SAMPLE-304", "HMMWV", "FMC", 29800, 80, "Motor Pool B"],
  ["20 ASOS", "SAMPLE-401", "HMMWV", "FMC", 40100, 33, "Motor Pool A"],
  ["20 ASOS", "SAMPLE-402", "JLTV", "NMC", 19300, -9, "Maintenance Bay"],
  ["20 ASOS", "SAMPLE-403", "Trailer", "FMC", 0, 150, "Motor Pool A"],
  ["20 ASOS", "SAMPLE-404", "F-250 Truck", "FMC", 55700, 64, "Motor Pool A"],
];
const DEMO_AUTHS = [
  ["14 ASOS", "JLTV", 3], ["14 ASOS", "HMMWV", 3], ["14 ASOS", "LMTV", 2],
  ["14 ASOS", "Trailer", 2],
  ["15 ASOS", "JLTV", 2], ["15 ASOS", "HMMWV", 2],
  ["15 ASOS", "F-250 Truck", 2], ["15 ASOS", "Generator", 2],
  ["19 ASOS", "JLTV", 3], ["19 ASOS", "LMTV", 2], ["19 ASOS", "HMMWV", 2],
  ["20 ASOS", "HMMWV", 3], ["20 ASOS", "JLTV", 2], ["20 ASOS", "Trailer", 1],
  ["20 ASOS", "F-250 Truck", 2],
];
/* Two pre-built SAMPLE UTCs so UTC health shows the moment a visitor
   opens a unit (bumpers refer to the demo fleet above). */
const SEED_UTCS = [
  {unit: "14 ASOS", code: "XFFC1",
   title: "SAMPLE — Deployment package (demo)",
   bumpers: ["SAMPLE-101", "SAMPLE-102", "SAMPLE-103", "SAMPLE-105"]},
  {unit: "19 ASOS", code: "X19RT",
   title: "SAMPLE — Ready team package (demo)",
   bumpers: ["SAMPLE-301", "SAMPLE-302", "SAMPLE-303"]},
];

/* ============================================================ helpers */
class SimError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
    this.message = message;
  }
}
const bad = (msg) => new SimError(400, msg);

function nowIso() {
  return new Date().toISOString().slice(0, 19) + "+00:00";
}
function localDatePlus(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  const p = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
}
/* MC rate rounded HALF-UP to 1 decimal — server.py's
   mc_rate_half_up (Decimal) and the client's own formula agree. */
function mcRateHalfUp(fmc, onHand) {
  if (!onHand) return 0.0;
  return Math.round(fmc * 1000 / onHand) / 10;
}
const INT_RE = /^\d+$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function strictNonnegInt(value, field, allowNone = true) {
  if (value === null || value === undefined) {
    if (allowNone) return null;
    throw bad(field + " is required");
  }
  let n;
  if (typeof value === "boolean") {
    throw bad(field + " must be an integer >= 0");
  } else if (typeof value === "number") {
    if (!Number.isInteger(value)) throw bad(field + " must be an integer >= 0");
    n = value;
  } else if (typeof value === "string" && INT_RE.test(value.trim())) {
    n = parseInt(value.trim(), 10);
  } else {
    throw bad(field + " must be an integer >= 0");
  }
  if (n < 0) throw bad(field + " must be an integer >= 0");
  return n;
}
function isRealDate(s) {
  const [y, m, d] = s.split("-").map(Number);
  if (m < 1 || m > 12 || d < 1) return false;
  const dim = [31, (y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0)) ? 29 : 28,
    31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1];
  return d <= dim;
}
function validDueDate(value, field = "maint_due_date") {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value !== "string" || !DATE_RE.test(value))
    throw bad(field + " must be empty or YYYY-MM-DD");
  if (!isRealDate(value))
    throw bad(field + " must be a real calendar date");
  return value;
}
function reqText(data, field) {
  const v = data[field];
  if (v === null || v === undefined) return "";
  return (typeof v === "string" ? v : String(v)).trim();
}

/* validate_vehicle — mirror of server.py (partial=False: all fields) */
function validateVehicle(data, partial = false) {
  const clean = {};
  const present = (f) => partial ? (f in data) : true;
  if (present("unit")) {
    if (!UNIT_NAMES.includes(data.unit))
      throw bad("unit must be one of the five units " +
                "(18 ASOG, 14 ASOS, 15 ASOS, 19 ASOS, 20 ASOS)");
    clean.unit = data.unit;
  }
  if (present("bumper")) {
    const bumper = reqText(data, "bumper");
    if (!bumper) throw bad("bumper # is required (non-empty)");
    clean.bumper = bumper;
  }
  if (present("type")) {
    const typ = reqText(data, "type");
    if (!typ) throw bad("type is required (non-empty)");
    clean.type = typ;
  }
  if (present("status")) {
    const status = partial ? data.status
                           : (data.status === undefined ? "FMC" : data.status);
    if (!STATUSES.includes(status))
      throw bad("status must be FMC, PMC or NMC");
    clean.status = status;
  } else if (!partial) {
    clean.status = "FMC";
  }
  for (const f of ["mileage", "maint_due_miles"]) {
    if (present(f)) clean[f] = strictNonnegInt(data[f], f);
  }
  if (present("maint_due_date"))
    clean.maint_due_date = validDueDate(
      data.maint_due_date === undefined ? "" : data.maint_due_date);
  for (const f of ["location", "notes"]) {
    if (present(f)) {
      const v = data[f];
      clean[f] = (v === null || v === undefined) ? "" : String(v);
    }
  }
  return clean;
}

/* validate_utc — mirror of server.py */
function validateUtc(data, partial = false) {
  const clean = {};
  const present = (f) => partial ? (f in data) : true;
  if (present("unit")) {
    if (!UNIT_NAMES.includes(data.unit))
      throw bad("unit must be one of the five units");
    clean.unit = data.unit;
  }
  if (present("code")) {
    let code = reqText(data, "code");
    if (!code) throw bad("code is required (non-empty)");
    code = code.toUpperCase();
    if (code.length > UTC_CODE_MAX)
      throw bad("code must be at most " + UTC_CODE_MAX + " characters");
    clean.code = code;
  }
  if (present("title")) {
    const t = data.title;
    clean.title = (t === null || t === undefined) ? "" : String(t).trim();
  } else if (!partial) {
    clean.title = "";
  }
  return clean;
}

/* spreadsheet formula-injection guard — mirror of csv_safe */
function csvSafe(value) {
  const s = value === null || value === undefined ? "" : String(value);
  const t = s.replace(/^[ \t]+/, "");
  return ["=", "+", "-", "@"].includes(t[0]) ? "'" + s : s;
}
function csvCell(value) {
  const s = value === null || value === undefined ? "" : String(value);
  return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

/* ============================================================ CSV parsing
   (the sim parses CSV client-side; .xlsx stays the Python server's
   job — see the import preview handler) */
function parseCsv(text) {
  // Mirrors Python's csv module: a quote is special ONLY as the very
  // first character of a field; anywhere else it is literal text.
  const rows = [];
  let row = [], cell = "", inQ = false, atCellStart = true;
  const src = String(text);
  for (let i = 0; i < src.length; i++) {
    const ch = src[i];
    if (inQ) {
      if (ch === '"') {
        if (src[i + 1] === '"') { cell += '"'; i++; }
        else inQ = false;
      } else cell += ch;
    } else if (ch === '"' && atCellStart) {
      inQ = true; atCellStart = false;
    } else if (ch === ",") {
      row.push(cell); cell = ""; atCellStart = true;
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && src[i + 1] === "\n") i++;
      row.push(cell); cell = ""; atCellStart = true;
      rows.push(row); row = [];
    } else { cell += ch; atCellStart = false; }
  }
  if (cell !== "" || row.length) { row.push(cell); rows.push(row); }
  return rows;
}
function decodeFileBytes(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  try {
    return new TextDecoder("utf-8", {fatal: true}).decode(bytes)
      .replace(/^﻿/, "");
  } catch (e) {
    return new TextDecoder("iso-8859-1").decode(bytes);
  }
}
function normHeader(h) {
  return String(h || "").trim().toLowerCase().split(/\s+/).join(" ")
    .replace(/:$/, "");
}
function importNumber(value, label) {
  if (value === null || value === undefined || String(value).trim() === "")
    return null;
  const s = String(value).trim().replace(/,/g, "");
  if (!/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(s))
    throw new Error("invalid " + label + " '" + value + "'");
  const n = Number(s);
  if (Number.isNaN(n) || n < 0 || n !== Math.floor(n))
    throw new Error("invalid " + label + " '" + value + "'");
  return n;
}
function importDate(value) {
  const s = String(value === null || value === undefined ? "" : value).trim();
  if (!s) return "";
  if (DATE_RE.test(s)) {
    // Server parity: an ISO-shaped but impossible date (2026-02-30)
    // fails the WHOLE preview with a 400 there (its ValidationError
    // escapes the per-row catch). Tag the error so buildImportPreview
    // rethrows it instead of skipping just this row.
    try { return validDueDate(s); }
    catch (e) { e.wholePreview = true; throw e; }
  }
  if (/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/.test(s)) {
    const serial = Number(s);
    if (Number.isInteger(serial) && serial > 20000 && serial < 80000) {
      const d = new Date(Date.UTC(1899, 11, 30) + serial * 864e5);
      const p = (n) => String(n).padStart(2, "0");
      return d.getUTCFullYear() + "-" + p(d.getUTCMonth() + 1) + "-" +
             p(d.getUTCDate());
    }
  }
  const m = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (m) {
    const out = m[3] + "-" + m[1].padStart(2, "0") + "-" + m[2].padStart(2, "0");
    if (isRealDate(out)) return out;
  }
  const m2 = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{2})$/);
  if (m2) {
    const out = "20" + m2[3] + "-" + m2[1].padStart(2, "0") + "-" +
      m2[2].padStart(2, "0");
    if (isRealDate(out)) return out;
  }
  throw new Error("invalid maintenance due date '" + value + "'");
}

/* ============================================================ the backend */
function createSimBackend() {
  const state = {
    users: [],            // {id, username, display_name, unit, role, _pw}
    sessions: new Map(),  // token -> user id
    vehicles: [],
    authorizations: [],
    utcs: [],
    utcVehicles: new Set(), // "utcId:vehicleId"
    auditLog: [],
    conflicts: [],
    nextUserId: 1, nextVehicleId: 1, nextAuthId: 1,
    nextUtcId: 1, nextAuditId: 1, nextConflictId: 1,
    tokenSeq: 0,
  };

  const publicUser = (u) => ({id: u.id, username: u.username,
    display_name: u.display_name, unit: u.unit, role: u.role});
  const findUser = (username) =>
    state.users.find(u => u.username === username) || null;
  const userById = (id) => state.users.find(u => u.id === id) || null;

  function audit(vehicleId, actor, action, field = "", old = "", nu = "",
                 detail = "", isConflict = 0) {
    state.auditLog.push({
      id: state.nextAuditId++, vehicle_id: vehicleId, actor, action,
      field, old_value: old === null || old === undefined ? "" : String(old),
      new_value: nu === null || nu === undefined ? "" : String(nu),
      detail, is_conflict: isConflict, created_at: nowIso(),
    });
  }
  function closeOpenConflicts(vehicleId, actor, why) {
    let n = 0;
    for (const c of state.conflicts)
      if (c.vehicle_id === vehicleId && !c.resolved) { c.resolved = 1; n++; }
    if (n) audit(vehicleId, actor, "conflict-close",
                 "", "", "", n + " open conflict(s) closed: " + why);
    return n;
  }
  const bumperTaken = (unit, bumper, excludeId = null) =>
    state.vehicles.some(v => v.unit === unit && v.bumper === bumper &&
                             v.id !== excludeId);

  /* ---- demo fleet (server.py's load_demo / purge_demo) ---- */
  function purgeDemo(actor) {
    const ids = state.vehicles.filter(v => v.is_demo).map(v => v.id);
    for (const vid of ids) {
      closeOpenConflicts(vid, actor, "demo vehicle removed");
      for (const key of [...state.utcVehicles])
        if (Number(key.split(":")[1]) === vid) state.utcVehicles.delete(key);
    }
    state.vehicles = state.vehicles.filter(v => !v.is_demo);
    state.authorizations = state.authorizations.filter(a => !a.is_demo);
    for (const vid of ids)
      audit(vid, actor, "delete", "", "", "", "demo vehicle removed");
    return ids.length;
  }
  function loadDemo(actor) {
    purgeDemo(actor);
    for (const [unit, bumper, typ, status, miles, dueDays, loc] of DEMO) {
      if (bumperTaken(unit, bumper)) continue;
      const v = {
        id: state.nextVehicleId++, unit, bumper, type: typ, status,
        mileage: miles, maint_due_date: localDatePlus(dueDays),
        maint_due_miles: null, location: loc,
        notes: "SAMPLE DATA — demo vehicle, not a real fleet record.",
        is_demo: 1, client_op: "", updated_by: actor,
        updated_at: nowIso(),
      };
      state.vehicles.push(v);
      audit(v.id, actor, "create", "", "", "", "demo vehicle loaded");
    }
    for (const [unit, typ, n] of DEMO_AUTHS) {
      if (!state.authorizations.some(a => a.unit === unit &&
                                          a.vehicle_type === typ))
        state.authorizations.push({id: state.nextAuthId++, unit,
          vehicle_type: typ, authorized: n, is_demo: 1});
    }
  }

  /* ---- seed: the demo fleet + two SAMPLE UTCs + the two sim users */
  state.users.push(
    {id: state.nextUserId++, username: "editor",
     display_name: "Demo Editor", unit: "18 ASOG", role: "editor",
     _pw: null},
    {id: state.nextUserId++, username: "viewer",
     display_name: "Demo Viewer", unit: "14 ASOS", role: "viewer",
     _pw: null});
  loadDemo("Skyline Sim");
  for (const s of SEED_UTCS) {
    const u = {id: state.nextUtcId++, unit: s.unit, code: s.code,
      title: s.title, created_by: "Skyline Sim",
      created_at: nowIso(), updated_at: nowIso()};
    state.utcs.push(u);
    for (const bumper of s.bumpers) {
      const v = state.vehicles.find(x => x.unit === s.unit &&
                                         x.bumper === bumper);
      if (v) state.utcVehicles.add(u.id + ":" + v.id);
    }
    audit(null, "Skyline Sim", "utc-create", "", "", "",
          s.unit + " UTC " + s.code + " — " + s.title);
  }

  /* ---- snapshot (fleet_snapshot / unit_stats / utc_snapshot) ---- */
  function unitStats(name) {
    const vehs = state.vehicles.filter(v => v.unit === name);
    const count = (s) => vehs.filter(v => v.status === s).length;
    const auth = state.authorizations.filter(a => a.unit === name)
      .reduce((sum, a) => sum + a.authorized, 0);
    const fmc = count("FMC");
    return {unit: name, on_hand: vehs.length, authorizations: auth,
      fmc, pmc: count("PMC"), nmc: count("NMC"),
      mc_rate: mcRateHalfUp(fmc, vehs.length),
      includes_demo: vehs.some(v => v.is_demo)};
  }
  function snapshot() {
    const byUnitBumper = (a, b) =>
      a.unit < b.unit ? -1 : a.unit > b.unit ? 1 :
      a.bumper < b.bumper ? -1 : a.bumper > b.bumper ? 1 : 0;
    const byUnitType = (a, b) =>
      a.unit < b.unit ? -1 : a.unit > b.unit ? 1 :
      a.vehicle_type < b.vehicle_type ? -1 :
      a.vehicle_type > b.vehicle_type ? 1 : 0;
    const vehicles = [...state.vehicles].sort(byUnitBumper)
      .map(v => ({...v}));
    const auths = [...state.authorizations].sort(byUnitType)
      .map(a => ({...a}));
    const units = [];
    const sums = {on_hand: 0, authorizations: 0, fmc: 0, pmc: 0, nmc: 0};
    let groupDemo = false;
    for (const [name, kind] of UNITS) {
      const st = unitStats(name);
      st.kind = kind;
      units.push(st);
      for (const k of Object.keys(sums)) sums[k] += st[k];
      groupDemo = groupDemo || st.includes_demo;
    }
    const group = {unit: "18 ASOG", kind: "group", ...sums,
      includes_demo: groupDemo,
      mc_rate: mcRateHalfUp(sums.fmc, sums.on_hand)};
    const utcs = [...state.utcs]
      .sort((a, b) => a.unit < b.unit ? -1 : a.unit > b.unit ? 1 :
                      a.code < b.code ? -1 : a.code > b.code ? 1 : 0)
      .map(u => ({...u, code: (u.code || "").toUpperCase(),
        vehicle_ids: [...state.utcVehicles]
          .filter(k => Number(k.split(":")[0]) === u.id)
          .map(k => Number(k.split(":")[1])).sort((x, y) => x - y)}));
    return {units, group, vehicles, authorizations: auths, utcs,
            generated_at: nowIso()};
  }

  /* ---- auth plumbing (mirror of _user / _require / _require_role) */
  function currentUser(headers) {
    const auth = (headers && (headers.Authorization ||
                              headers.authorization)) || "";
    const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
    if (!token || !state.sessions.has(token)) return null;
    return userById(state.sessions.get(token));
  }
  function requireUser(headers) {
    const user = currentUser(headers);
    if (!user) throw new SimError(401, "sign-in required");
    return user;
  }
  function requireRole(user, roles) {
    if (!roles.includes(user.role))
      throw new SimError(403, "your role cannot do that");
  }
  function pathId(path, prefix) {
    const part = path.slice(prefix.length).split("/")[0];
    if (!/^-?\d+$/.test(part)) return null;
    const value = Number(part);
    if (!Number.isSafeInteger(value)) return null;
    return value;
  }

  /* ======================================================== handlers */
  function setup(data) {
    const username = String(data.username || "").trim();
    const password = data.password || "";
    const display = String(data.display_name || username).trim();
    const unit = data.unit === undefined ? "18 ASOG" : data.unit;
    if (!username || String(password).length < 8)
      throw bad("username required; password must be at least 8 characters");
    if (!UNIT_NAMES.includes(unit))
      throw bad("unit must be one of the five units");
    // the sim is pre-seeded with accounts, so setup is always complete
    throw new SimError(403, "setup already complete");
  }
  function login(data) {
    const username = String(data.username || "").trim();
    const password = data.password || "";
    let user = findUser(username);
    if (user) {
      // accounts created inside the sim check their password; the two
      // seeded demo accounts accept any password (documented on the
      // login screen and in the README)
      if (user._pw !== null && user._pw !== password)
        throw new SimError(401, "bad username or password");
    } else {
      // predictable demo rule: "viewer" is the Viewer (seeded above);
      // any other new name signs in as an Editor demo user
      user = {id: state.nextUserId++, username,
              display_name: username || "Demo Editor",
              unit: "18 ASOG", role: "editor", _pw: null};
      state.users.push(user);
    }
    const token = "sim-" + (++state.tokenSeq) + "-" +
      Math.random().toString(36).slice(2) + Date.now().toString(36);
    state.sessions.set(token, user.id);
    return {status: 200, body: {token, user: publicUser(user)}};
  }
  function createUser(data, actor) {
    const username = String(data.username || "").trim();
    const password = data.password || "";
    const display = String(data.display_name || username).trim();
    const unit = data.unit === undefined ? "" : data.unit;
    const role = data.role === undefined ? "viewer" : data.role;
    if (!username || String(password).length < 8)
      throw bad("username required; password must be at least 8 characters");
    if (!ROLES.includes(role)) throw bad("invalid role");
    if (!UNIT_NAMES.includes(unit))
      throw bad("unit must be one of the five units");
    if (findUser(username))
      throw new SimError(409, "username already exists");
    const user = {id: state.nextUserId++, username, display_name: display,
                  unit, role, _pw: String(password)};
    state.users.push(user);
    audit(null, actor, "account", "", "", "",
          "created " + username + " (" + role + ")");
    return {status: 201, body: {user: publicUser(user)}};
  }
  function updateUser(path, data, actorUser) {
    const uid = pathId(path, "/api/users/");
    if (uid === null) throw new SimError(404, "user not found");
    const target = userById(uid);
    if (!target) throw new SimError(404, "user not found");
    const updates = {};   // field -> [old, new]
    if ("role" in data) {
      if (!ROLES.includes(data.role)) throw bad("invalid role");
      if (data.role !== target.role) {
        if (target.role === "admin" &&
            state.users.filter(u => u.role === "admin").length <= 1)
          throw bad("cannot demote the last Admin");
        updates.role = [target.role, data.role];
      }
    }
    if ("unit" in data) {
      if (!UNIT_NAMES.includes(data.unit))
        throw bad("unit must be one of the five units");
      if (data.unit !== target.unit) updates.unit = [target.unit, data.unit];
    }
    if ("display_name" in data) {
      const dn = String(data.display_name).trim();
      if (dn && dn !== target.display_name)
        updates.display_name = [target.display_name, dn];
    }
    let newPw = null;
    if (data.password) {
      if (String(data.password).length < 8)
        throw bad("password too short (min 8)");
      newPw = String(data.password);
      updates.password = ["(set)", "(reset)"];
    }
    if (!Object.keys(updates).length && !newPw)
      return {status: 200, body: {ok: true}};
    for (const [field, [oldV, newV]] of Object.entries(updates)) {
      if (field === "password") target._pw = newPw;
      else target[field] = newV;
      audit(null, actorUser.display_name, "account", field, oldV, newV,
            "user " + target.username);
    }
    if ("password" in updates || "role" in updates)
      for (const [tok, id] of [...state.sessions])
        if (id === uid) state.sessions.delete(tok);
    return {status: 200, body: {ok: true}};
  }

  function insertVehicle(data, user) {
    const clean = validateVehicle(data, false);
    let opId = data.op_id || data.client_op || "";
    if (typeof opId !== "string") opId = String(opId);
    if (opId) {
      const existing = state.vehicles.find(v => v.client_op === opId);
      if (existing)
        return {status: 200, body: {vehicle: {...existing},
                                    deduplicated: true}};
    }
    if (bumperTaken(clean.unit, clean.bumper))
      throw new SimError(409, "bumper # " + clean.bumper +
                         " already exists in " + clean.unit);
    const v = {
      id: state.nextVehicleId++, unit: clean.unit, bumper: clean.bumper,
      type: clean.type, status: clean.status,
      mileage: clean.mileage !== undefined ? clean.mileage : null,
      maint_due_date: clean.maint_due_date || "",
      maint_due_miles: clean.maint_due_miles !== undefined
        ? clean.maint_due_miles : null,
      location: clean.location || "", notes: clean.notes || "",
      is_demo: 0, client_op: opId,
      updated_by: user.display_name, updated_at: nowIso(),
    };
    state.vehicles.push(v);
    audit(v.id, user.display_name, "create", "", "", "",
          clean.unit + " " + clean.bumper + " (" + clean.type + ")");
    return {status: 201, body: {vehicle: {...v}}};
  }
  function updateVehicle(path, data, user) {
    const vid = pathId(path, "/api/vehicles/");
    if (vid === null) throw new SimError(404, "vehicle not found");
    const row = state.vehicles.find(v => v.id === vid);
    if (!row) throw new SimError(404, "vehicle not found");
    const clean = validateVehicle(data, true);
    const newUnit = clean.unit !== undefined ? clean.unit : row.unit;
    const newBumper = clean.bumper !== undefined ? clean.bumper : row.bumper;
    if ((newUnit !== row.unit || newBumper !== row.bumper) &&
        bumperTaken(newUnit, newBumper, vid))
      throw new SimError(409, "bumper # " + newBumper +
                          " already exists in " + newUnit);
    let conflict = null;
    let changed = false;
    if (newUnit !== row.unit || newBumper !== row.bumper) {
      for (const f of ["unit", "bumper"]) {
        if (f in clean && row[f] !== clean[f])
          audit(vid, user.display_name, "update", f, row[f], clean[f]);
      }
      row.unit = newUnit;
      row.bumper = newBumper;
      changed = true;
    }
    // a vehicle that changed unit drops memberships in other units' UTCs
    let dropped = 0;
    for (const key of [...state.utcVehicles]) {
      const [utcId, vehId] = key.split(":").map(Number);
      if (vehId !== vid) continue;
      const utc = state.utcs.find(u => u.id === utcId);
      if (utc && utc.unit !== row.unit) {
        state.utcVehicles.delete(key);
        dropped++;
      }
    }
    if (dropped) {
      audit(vid, user.display_name, "utc-remove-vehicle", "", "", "",
            "membership dropped: vehicle moved to " + row.unit);
      changed = true;
    }
    for (const field of VEHICLE_FIELDS) {
      if (!(field in clean) || field === "unit" || field === "bumper")
        continue;
      const nu = clean[field];
      if (field === "status") {
        const base = data.base_status;
        if (base !== null && base !== undefined && base !== row.status &&
            nu !== row.status) {
          const c = {id: state.nextConflictId++, vehicle_id: vid,
            actor: user.display_name, field: "status", attempted: nu,
            current: row.status, resolved: 0, created_at: nowIso()};
          state.conflicts.push(c);
          audit(vid, user.display_name, "conflict", "status", row.status,
                nu, "offline sync conflict — server status kept", 1);
          conflict = {id: c.id, field: "status", attempted: nu,
                      current: row.status};
          changed = true;
          continue;
        }
      }
      if (row[field] !== nu) {
        const old = row[field];
        row[field] = nu;
        audit(vid, user.display_name, "update", field, old, nu);
        changed = true;
      }
    }
    if (changed) {
      row.updated_by = user.display_name;
      row.updated_at = nowIso();
    }
    const out = {vehicle: {...row}};
    if (conflict) out.conflict = conflict;
    return {status: 200, body: out};
  }

  function deleteVehicle(path, user) {
    const vid = pathId(path, "/api/vehicles/");
    if (vid === null) throw new SimError(404, "vehicle not found");
    const row = state.vehicles.find(v => v.id === vid);
    if (!row) throw new SimError(404, "vehicle not found");
    for (const key of [...state.utcVehicles])
      if (Number(key.split(":")[1]) === vid) state.utcVehicles.delete(key);
    state.vehicles = state.vehicles.filter(v => v.id !== vid);
    audit(vid, user.display_name, "delete", "", "", "",
          row.unit + " " + row.bumper);
    closeOpenConflicts(vid, user.display_name, "vehicle deleted");
    return {status: 200, body: {ok: true}};
  }

  function upsertAuthorization(data, user) {
    const unit = data.unit;
    const typ = reqText(data, "vehicle_type");
    if (!UNIT_NAMES.includes(unit))
      throw bad("unit must be one of the five units");
    if (!typ) throw bad("vehicle_type is required (non-empty)");
    const authorized = strictNonnegInt(data.authorized, "authorized", false);
    const old = state.authorizations.find(a => a.unit === unit &&
                                               a.vehicle_type === typ);
    const prev = old ? old.authorized : null;
    if (old) {
      old.authorized = authorized;
      old.is_demo = 0;   // a real edit clears the demo flag (server parity)
    } else {
      state.authorizations.push({id: state.nextAuthId++, unit,
        vehicle_type: typ, authorized, is_demo: 0});
    }
    if (!old || prev !== authorized)
      audit(null, user.display_name, "authorization", "authorized",
            old ? prev : "", authorized, unit + " / " + typ);
    return {status: 200, body: {ok: true}};
  }
  function deleteAuthorization(path, user) {
    const aid = pathId(path, "/api/authorizations/");
    if (aid === null)
      throw new SimError(404, "authorization not found");
    const row = state.authorizations.find(a => a.id === aid);
    if (!row) throw new SimError(404, "authorization not found");
    state.authorizations = state.authorizations.filter(a => a.id !== aid);
    audit(null, user.display_name, "authorization", "authorized",
          row.authorized, "",
          row.unit + " / " + row.vehicle_type + " (deleted)");
    return {status: 200, body: {ok: true}};
  }

  /* ---- UTCs ---- */
  const getUtc = (id) => state.utcs.find(u => u.id === id) || null;
  const utcOut = (u) => ({...u, vehicle_ids: [...state.utcVehicles]
    .filter(k => Number(k.split(":")[0]) === u.id)
    .map(k => Number(k.split(":")[1])).sort((a, b) => a - b)});
  function createUtc(data, user) {
    const clean = validateUtc(data, false);
    const dupe = state.utcs.some(u => u.unit === clean.unit &&
      (u.code || "").toUpperCase() === clean.code);
    if (dupe)
      throw new SimError(409, "UTC code " + clean.code +
                         " already exists in " + clean.unit);
    const u = {id: state.nextUtcId++, unit: clean.unit, code: clean.code,
      title: clean.title, created_by: user.display_name,
      created_at: nowIso(), updated_at: nowIso()};
    state.utcs.push(u);
    audit(null, user.display_name, "utc-create", "", "", "",
          clean.unit + " UTC " + clean.code + " — " + clean.title);
    const out = {...u, vehicle_ids: []};
    return {status: 201, body: {utc: out}};
  }
  function updateUtc(path, data, user) {
    const utcId = pathId(path, "/api/utcs/");
    if (utcId === null) throw new SimError(404, "UTC not found");
    const clean = validateUtc(data, true);
    const row = getUtc(utcId);
    if (!row) throw new SimError(404, "UTC not found");
    const newUnit = clean.unit !== undefined ? clean.unit : row.unit;
    const newCode = clean.code !== undefined ? clean.code : row.code;
    if (newUnit !== row.unit || newCode !== row.code) {
      const dupe = state.utcs.some(u => u.id !== utcId &&
        u.unit === newUnit && (u.code || "").toUpperCase() === newCode);
      if (dupe)
        throw new SimError(409, "UTC code " + newCode +
                           " already exists in " + newUnit);
    }
    if (newUnit !== row.unit) {
      const members = [...state.utcVehicles]
        .filter(k => Number(k.split(":")[0]) === utcId).length;
      if (members)
        throw bad("cannot move a UTC with assigned vehicles to another unit");
    }
    let changed = false;
    for (const f of ["unit", "code", "title"]) {
      if (f in clean && clean[f] !== row[f]) {
        audit(null, user.display_name, "utc-update", f, row[f], clean[f],
              "UTC #" + utcId + " (" + row.unit + ")");
        row[f] = clean[f];
        changed = true;
      }
    }
    if (changed) row.updated_at = nowIso();
    return {status: 200, body: {utc: utcOut(row)}};
  }
  function deleteUtc(path, user) {
    const utcId = pathId(path, "/api/utcs/");
    if (utcId === null) throw new SimError(404, "UTC not found");
    const row = getUtc(utcId);
    if (!row) throw new SimError(404, "UTC not found");
    for (const key of [...state.utcVehicles])
      if (Number(key.split(":")[0]) === utcId) state.utcVehicles.delete(key);
    state.utcs = state.utcs.filter(u => u.id !== utcId);
    audit(null, user.display_name, "utc-delete", "", "", "",
          row.unit + " UTC " + row.code + " deleted (vehicles kept)");
    return {status: 200, body: {ok: true}};
  }
  function utcAddVehicle(path, data, user) {
    const utcId = pathId(path, "/api/utcs/");
    if (utcId === null) throw new SimError(404, "UTC not found");
    let vehicleId = data.vehicle_id;
    if (typeof vehicleId === "boolean" ||
        !(typeof vehicleId === "number" || typeof vehicleId === "string") ||
        (typeof vehicleId === "string" && !/^\d+$/.test(vehicleId.trim())))
      throw bad("vehicle_id must be an integer");
    vehicleId = Number(vehicleId);
    if (!Number.isSafeInteger(vehicleId))
      throw bad("vehicle does not exist");
    const utc = getUtc(utcId);
    if (!utc) throw new SimError(404, "UTC not found");
    const veh = state.vehicles.find(v => v.id === vehicleId);
    if (!veh) throw bad("vehicle does not exist");
    if (veh.unit !== utc.unit)
      throw bad("vehicle belongs to " + veh.unit + "; a UTC can only " +
                "hold vehicles of its own unit (" + utc.unit + ")");
    const key = utcId + ":" + vehicleId;
    if (!state.utcVehicles.has(key)) {
      state.utcVehicles.add(key);
      audit(vehicleId, user.display_name, "utc-add-vehicle", "", "", "",
            "added to " + utc.unit + " UTC " + utc.code);
    }
    return {status: 200, body: {ok: true}};
  }
  function utcRemoveVehicle(path, user) {
    const parts = path.replace(/^\/+|\/+$/g, "").split("/");
    let utcId, vehicleId;
    try {
      utcId = Number(parts[2]);
      vehicleId = Number(parts[4]);
      if (!Number.isInteger(utcId) || !Number.isInteger(vehicleId) ||
          String(parts[2]).trim() === "" || String(parts[4]).trim() === "")
        throw new Error("bad");
    } catch (e) {
      throw new SimError(404, "UTC membership not found");
    }
    if (!Number.isSafeInteger(utcId) || !Number.isSafeInteger(vehicleId))
      throw new SimError(404, "UTC membership not found");
    const utc = getUtc(utcId);
    if (!utc) throw new SimError(404, "UTC not found");
    const key = utcId + ":" + vehicleId;
    if (!state.utcVehicles.has(key))
      throw new SimError(404, "vehicle is not in this UTC");
    state.utcVehicles.delete(key);
    const veh = state.vehicles.find(v => v.id === vehicleId);
    audit(vehicleId, user.display_name, "utc-remove-vehicle", "", "", "",
          "removed " + (veh ? veh.bumper : vehicleId) + " from " +
          utc.unit + " UTC " + utc.code);
    return {status: 200, body: {ok: true}};
  }

  /* ---- conflicts ---- */
  function resolveConflict(path, data, user) {
    const cid = pathId(path, "/api/conflicts/");
    if (cid === null) throw new SimError(404, "conflict not found");
    const c = state.conflicts.find(x => x.id === cid);
    if (!c) throw new SimError(404, "conflict not found");
    const action = data.action;
    if (action !== "apply" && action !== "keep")
      throw bad('action must be "apply" or "keep"');
    if (c.resolved) throw new SimError(409, "conflict already resolved");
    if (action === "apply") {
      const v = state.vehicles.find(x => x.id === c.vehicle_id);
      if (v) {
        v.status = c.attempted;
        v.updated_by = user.display_name;
        v.updated_at = nowIso();
      }
      audit(c.vehicle_id, user.display_name, "update", "status",
            c.current, c.attempted, "conflict resolved: applied");
    } else {
      audit(c.vehicle_id, user.display_name, "update", "status",
            c.attempted, c.current, "conflict resolved: kept current");
    }
    c.resolved = 1;
    return {status: 200, body: {ok: true}};
  }

  /* ---- Excel/CSV import ---- */
  function importFileFrom(data) {
    const unit = data.unit;
    if (!UNIT_NAMES.includes(unit))
      throw bad("unit must be one of the five units");
    const filename = String(data.filename || "").split(/[\\/]/).pop()
      .slice(0, 120);
    const b64 = data.content_b64 || "";
    if (typeof b64 !== "string" || !b64)
      throw bad("no file content received");
    if (!/^[A-Za-z0-9+/]*={0,2}$/.test(b64) || b64.length % 4 !== 0)
      throw bad("file content is not valid base64");
    let rawLen;
    try {
      rawLen = atob(b64).length;
    } catch (e) {
      throw bad("file content is not valid base64");
    }
    if (rawLen > IMPORT_MAX_FILE)
      throw bad("file too large — import cap is " +
                (IMPORT_MAX_FILE / (1024 * 1024)) + " MB");
    if (!rawLen) throw bad("the file is empty");
    return {unit, filename, b64};
  }
  function buildImportPreview(unit, filename, rows) {
    if (!rows.length) throw bad("the sheet is empty");
    const headersRaw = rows[0].map(h => String(h || ""));
    const mapping = {}, unmapped = [], used = new Set();
    headersRaw.forEach((h, i) => {
      const field = HEADER_ALIASES[normHeader(h)];
      if (field && !used.has(field)) {
        mapping[i] = field;
        used.add(field);
      } else if (h.trim()) {
        unmapped.push(h.trim());
      }
    });
    const existing = new Set(state.vehicles.filter(v => v.unit === unit)
      .map(v => v.bumper));
    const seen = {};
    const outRows = [];
    rows.slice(1).forEach((rawRow, i) => {
      const idx = i + 2;
      if (!rawRow.some(c => String(c).trim() !== "")) return;  // blank row
      const vals = {};
      for (const [col, field] of Object.entries(mapping))
        vals[field] = Number(col) < rawRow.length
          ? String(rawRow[Number(col)]).trim() : "";
      const bumper = vals.bumper || "";
      const row = {row: idx, bumper, type: vals.type || "Unknown",
        status: null, status_defaulted: false, mileage: null,
        maint_due_date: "", maint_due_miles: null,
        location: vals.location || "", notes: vals.notes || "",
        ok: true, reason: ""};
      const skip = (reason) => { row.ok = false; row.reason = reason; };
      const statusRaw = vals.status || "";
      if (statusRaw) {
        if (STATUSES.includes(statusRaw.toUpperCase()))
          row.status = statusRaw.toUpperCase();
        else skip("invalid status '" + statusRaw + "' (must be FMC/PMC/NMC)");
      } else {
        row.status = "FMC";
        row.status_defaulted = true;
      }
      try { row.mileage = importNumber(vals.mileage, "mileage"); }
      catch (e) { skip(e.message); }
      try {
        row.maint_due_miles = importNumber(vals.maint_due_miles,
                                           "maintenance due miles");
      } catch (e) { skip(e.message); }
      try { row.maint_due_date = importDate(vals.maint_due_date); }
      catch (e) { if (e.wholePreview) throw e; skip(e.message); }
      if (row.ok) {
        if (!bumper) skip("missing asset number — required");
        else if (bumper in seen)
          skip("duplicate asset number in file (first seen on row " +
               seen[bumper] + ")");
        else if (existing.has(bumper))
          skip("already in " + unit + "'s fleet — import never overwrites");
      }
      if (bumper && !(bumper in seen)) seen[bumper] = idx;
      outRows.push(row);
    });
    const columns = {};
    for (const [i, field] of Object.entries(mapping))
      columns[field] = headersRaw[Number(i)];
    return {unit, filename, columns, unmapped_columns: unmapped,
      rows: outRows,
      will_import: outRows.filter(r => r.ok).length,
      skipped: outRows.filter(r => !r.ok).length};
  }
  function importPreview(data) {
    const {unit, filename, b64} = importFileFrom(data);
    const lower = (filename || "").toLowerCase();
    if (lower.endsWith(".xlsx"))
      // honest, not faked: parsing .xlsx is the Python server's job
      throw bad("Excel (.xlsx) parsing runs on the Python server in " +
                "the real app — this web sim accepts .csv files. " +
                "Save the sheet as CSV and import that instead.");
    if (!lower.endsWith(".csv"))
      throw bad("only .xlsx or .csv files can be imported");
    const rows = parseCsv(decodeFileBytes(b64));
    if (rows.length > IMPORT_MAX_ROWS + 1)
      throw bad("file has more than " + IMPORT_MAX_ROWS + " rows");
    return {status: 200, body: buildImportPreview(unit, filename, rows)};
  }
  function importConfirm(data, user) {
    const unit = data.unit;
    if (!UNIT_NAMES.includes(unit))
      throw bad("unit must be one of the five units");
    const filename = String(data.filename || "").split(/[\\/]/).pop()
      .slice(0, 120);
    const rows = data.rows;
    if (!Array.isArray(rows) || !rows.length)
      throw bad("no rows to import");
    if (rows.length > IMPORT_MAX_ROWS)
      throw bad("too many rows (cap " + IMPORT_MAX_ROWS + ")");
    const created = [], skipped = [];
    for (const r of rows) {
      if (!r || typeof r !== "object" || Array.isArray(r)) {
        skipped.push({bumper: "", reason: "bad row"});
        continue;
      }
      const payload = {unit, bumper: r.bumper,
        type: r.type || "Unknown", status: r.status || "FMC",
        mileage: r.mileage, maint_due_date: r.maint_due_date || "",
        maint_due_miles: r.maint_due_miles,
        location: r.location || "", notes: r.notes || ""};
      let clean;
      try {
        clean = validateVehicle(payload, false);
      } catch (e) {
        skipped.push({bumper: String(r.bumper || ""), reason: e.message});
        continue;
      }
      if (bumperTaken(unit, clean.bumper)) {
        skipped.push({bumper: clean.bumper,
          reason: "already in this unit's fleet — not overwritten"});
        continue;
      }
      const v = {
        id: state.nextVehicleId++, unit: clean.unit, bumper: clean.bumper,
        type: clean.type, status: clean.status,
        mileage: clean.mileage !== undefined ? clean.mileage : null,
        maint_due_date: clean.maint_due_date || "",
        maint_due_miles: clean.maint_due_miles !== undefined
          ? clean.maint_due_miles : null,
        location: clean.location || "", notes: clean.notes || "",
        is_demo: 0, client_op: "",
        updated_by: user.display_name, updated_at: nowIso(),
      };
      state.vehicles.push(v);
      audit(v.id, user.display_name, "create", "", "", "",
            "imported from " + (filename || "spreadsheet") +
            " into " + unit);
      created.push(clean.bumper);
    }
    return {status: 201, body: {created, skipped,
      created_count: created.length, skipped_count: skipped.length}};
  }

  /* ---- CSV export (GET /api/export.csv) ---- */
  function exportCsv() {
    const snap = snapshot();
    const header = ["unit", "bumper", "type", "status", "mileage_hours",
      "maint_due_date", "maint_due_miles", "location", "notes",
      "sample_data", "updated_by", "updated_at"];
    const lines = [header.join(",")];
    for (const v of snap.vehicles)
      lines.push([csvSafe(v.unit), csvSafe(v.bumper), csvSafe(v.type),
        csvSafe(v.status), v.mileage, csvSafe(v.maint_due_date),
        v.maint_due_miles, csvSafe(v.location), csvSafe(v.notes),
        v.is_demo ? "SAMPLE" : "", csvSafe(v.updated_by),
        csvSafe(v.updated_at)].map(csvCell).join(","));
    return {status: 200, body: lines.join("\r\n") + "\r\n",
            ctype: "text/csv",
            headers: {"Content-Disposition":
                      'attachment; filename="skyline_fleet.csv"'}};
  }

  /* ======================================================== router
     Mirrors server.py's dispatch order: public routes first, then
     sign-in required for everything else, then per-route role gates. */
  function handle(method, path, body, headers) {
    try {
      body = body || {};
      if (method === "GET") {
        if (path === "/api/setup/status")
          return {status: 200, body: {setup_needed: false}};
        if (path === "/api/export.csv") {
          requireUser(headers);
          return exportCsv();
        }
        const user = requireUser(headers);
        if (path === "/api/me")
          return {status: 200, body: {user: publicUser(user)}};
        if (path === "/api/fleet")
          return {status: 200, body: snapshot()};
        if (path === "/api/audit")
          return {status: 200, body: {audit: [...state.auditLog]
            .sort((a, b) => b.id - a.id).slice(0, 500).map(a => ({...a}))}};
        if (path === "/api/conflicts")
          return {status: 200, body: {conflicts: state.conflicts
            .filter(c => !c.resolved)
            .sort((a, b) => b.id - a.id).map(c => ({...c}))}};
        if (path === "/api/users") {
          if (user.role !== "admin")
            throw new SimError(403, "admin only");
          return {status: 200, body: {users: [...state.users]
            .sort((a, b) => a.username < b.username ? -1 : 1)
            .map(publicUser)}};
        }
        throw new SimError(404, "not found");
      }
      if (method === "POST") {
        if (path === "/api/setup") return setup(body);
        if (path === "/api/login") return login(body);
        if (path === "/api/logout") {
          const auth = (headers && (headers.Authorization ||
                                    headers.authorization)) || "";
          const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
          state.sessions.delete(token);
          return {status: 200, body: {ok: true}};
        }
        const user = requireUser(headers);
        if (path === "/api/users") {
          if (user.role !== "admin")
            throw new SimError(403, "admin only");
          return createUser(body, user.display_name);
        }
        if (path === "/api/vehicles") {
          requireRole(user, ["editor", "admin"]);
          return insertVehicle(body, user);
        }
        if (path === "/api/authorizations") {
          requireRole(user, ["editor", "admin"]);
          return upsertAuthorization(body, user);
        }
        if (path === "/api/utcs") {
          requireRole(user, ["editor", "admin"]);
          return createUtc(body, user);
        }
        if (path.startsWith("/api/utcs/") && path.endsWith("/vehicles")) {
          requireRole(user, ["editor", "admin"]);
          return utcAddVehicle(path, body, user);
        }
        if (path === "/api/import/preview") {
          requireRole(user, ["editor", "admin"]);
          return importPreview(body);
        }
        if (path === "/api/import/confirm") {
          requireRole(user, ["editor", "admin"]);
          return importConfirm(body, user);
        }
        if (path === "/api/demo/load") {
          if (user.role !== "admin")
            throw new SimError(403, "admin only");
          // reset/reseed: demo data returns to the shipped seed; any
          // real (non-demo) records the session created are kept,
          // exactly like the server's loader
          loadDemo(user.display_name);
          return {status: 200, body: {ok: true}};
        }
        if (path === "/api/demo/remove") {
          if (user.role !== "admin")
            throw new SimError(403, "admin only");
          const n = purgeDemo(user.display_name);
          return {status: 200, body: {ok: true, removed: n}};
        }
        if (path.startsWith("/api/conflicts/") &&
            path.endsWith("/resolve")) {
          requireRole(user, ["editor", "admin"]);
          return resolveConflict(path, body, user);
        }
        throw new SimError(404, "not found");
      }
      if (method === "PUT") {
        const user = requireUser(headers);
        if (path.startsWith("/api/vehicles/")) {
          requireRole(user, ["editor", "admin"]);
          return updateVehicle(path, body, user);
        }
        if (path.startsWith("/api/users/")) {
          if (user.role !== "admin")
            throw new SimError(403, "admin only");
          return updateUser(path, body, user);
        }
        if (path.startsWith("/api/utcs/")) {
          requireRole(user, ["editor", "admin"]);
          return updateUtc(path, body, user);
        }
        throw new SimError(404, "not found");
      }
      if (method === "DELETE") {
        const user = requireUser(headers);
        if (path.startsWith("/api/vehicles/")) {
          requireRole(user, ["editor", "admin"]);
          return deleteVehicle(path, user);
        }
        if (path.startsWith("/api/authorizations/")) {
          requireRole(user, ["editor", "admin"]);
          return deleteAuthorization(path, user);
        }
        if (path.startsWith("/api/utcs/") && path.includes("/vehicles/")) {
          requireRole(user, ["editor", "admin"]);
          return utcRemoveVehicle(path, user);
        }
        if (path.startsWith("/api/utcs/")) {
          requireRole(user, ["editor", "admin"]);
          return deleteUtc(path, user);
        }
        throw new SimError(404, "not found");
      }
      throw new SimError(404, "not found");
    } catch (e) {
      if (e instanceof SimError)
        return {status: e.status, body: {error: e.message}};
      throw e;
    }
  }

  return {handle, state, snapshot};
}

/* ============================================================ exports
   (Node test harness only; ignored by browsers) */
if (typeof module !== "undefined" && module.exports) {
  module.exports = {createSimBackend, parseCsv, DEMO, DEMO_AUTHS,
                    SEED_UTCS, UNIT_NAMES};
}

/* ============================================================ browser shim */
if (typeof window !== "undefined" && typeof document !== "undefined") {
  const backend = createSimBackend();

  /* The sim starts clean on every load: any leftover real-app (or
     earlier sim) session/snapshot under this origin must not leak in —
     a refresh resetting to the seed is the documented behavior. */
  try {
    for (const k of ["skyline_token", "skyline_user", "skyline_snapshot",
                     "skyline_queue", "skyline_last_sync"])
      window.localStorage.removeItem(k);
  } catch (e) { /* private mode etc. — the sim still runs */ }

  /* Neutralize the service worker: the real app registers /sw.js to
     cache its shell; the sim must never be cached by (or register) a
     service worker. app.js guards with `if ("serviceWorker" in
     navigator)`, so replacing the object with a stub whose register()
     resolves harmlessly is enough — and unregister anything a
     previous visit to the real app left on this origin. */
  try {
    const swStub = {
      register: () => Promise.resolve({}),
      getRegistrations: () => Promise.resolve([]),
      getRegistration: () => Promise.resolve(undefined),
      addEventListener: () => {},
      removeEventListener: () => {},
      ready: Promise.resolve({}),
      controller: null,
    };
    if ("serviceWorker" in navigator &&
        navigator.serviceWorker &&
        typeof navigator.serviceWorker.getRegistrations === "function") {
      navigator.serviceWorker.getRegistrations()
        .then(regs => regs.forEach(r => r.unregister()))
        .catch(() => {});
    }
    Object.defineProperty(navigator, "serviceWorker",
                          {value: swStub, configurable: true});
  } catch (e) { /* if the stub can't be installed, /sw.js simply
                   404s under the sim path and register() is caught
                   by app.js's own .catch(() => {}) */ }

  /* fetch interception: /api/* -> simulated backend, else real. */
  const realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const rawUrl = typeof input === "string" ? input
      : (input && input.url) || "";
    let path = rawUrl;
    try { path = new URL(rawUrl, window.location.href).pathname; }
    catch (e) { /* keep raw */ }
    if (!path.startsWith("/api/")) return realFetch(input, init);
    const method = ((init && init.method) ||
                    (input && input.method) || "GET").toUpperCase();
    const headers = {};
    const srcHeaders = (init && init.headers) ||
                       (input && input.headers) || {};
    if (typeof Headers !== "undefined" && srcHeaders instanceof Headers) {
      srcHeaders.forEach((v, k) => { headers[k] = v; });
    } else {
      for (const [k, v] of Object.entries(srcHeaders)) headers[k] = v;
    }
    let bodyObj = {};
    const rawBody = init && init.body;
    if (typeof rawBody === "string" && rawBody) {
      try { bodyObj = JSON.parse(rawBody); } catch (e) { bodyObj = {}; }
    }
    let result;
    try {
      result = backend.handle(method, path, bodyObj, headers);
    } catch (e) {
      result = {status: 500, body: {error: "internal error"}};
    }
    const isText = typeof result.body === "string";
    return Promise.resolve(new Response(
      isText ? result.body : JSON.stringify(result.body),
      {status: result.status,
       headers: {"Content-Type": result.ctype || "application/json",
                 ...(result.headers || {})}}));
  };

  /* Visible sim labeling + login hints (injected; app.js untouched). */
  const injectSimUi = () => {
    if (!document.getElementById("sim-pill")) {
      const pill = document.createElement("div");
      pill.id = "sim-pill";
      pill.textContent = "WEB SIM — simulated demo data, runs entirely " +
        "in your browser · refresh resets";
      pill.setAttribute("style",
        "position:fixed;left:50%;transform:translateX(-50%);bottom:10px;" +
        "z-index:9999;background:#241a4d;color:#e6defc;" +
        "border:1px solid #7c5cff;border-radius:999px;" +
        "padding:6px 14px;font:600 12px/1.3 system-ui,sans-serif;" +
        "box-shadow:0 4px 18px rgba(0,0,0,.45);pointer-events:none;" +
        "max-width:92vw;text-align:center");
      document.body.appendChild(pill);
    }
    const card = document.querySelector(".auth-card");
    const loginForm = document.getElementById("login-form");
    if (card && loginForm && !document.getElementById("sim-login-note")) {
      const note = document.createElement("p");
      note.id = "sim-login-note";
      note.className = "hint";
      note.setAttribute("style",
        "border:1px dashed #7c5cff;border-radius:10px;padding:8px 10px");
      note.innerHTML = "<b>Web sim logins</b> — any password works:<br>" +
        "<b>editor</b> = Editor (can change things) · " +
        "<b>viewer</b> = Viewer (read-only)<br>" +
        "Any other name also signs in as an Editor.";
      card.insertBefore(note, loginForm);
      const u = document.getElementById("li-username");
      const p = document.getElementById("li-password");
      if (u && !u.value) u.value = "editor";
      if (p && !p.value) p.value = "demo";
    }
  };
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", injectSimUi);
  else injectSimUi();
}
})();
