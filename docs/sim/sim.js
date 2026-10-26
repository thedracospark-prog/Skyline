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
const ROLES = ["operator", "editor", "vco"];
/* DAF Form 1800 — the official 35 checklist items, verbatim (mirrors
   server.py's INSPECTION_ITEMS). */
const INSPECTION_ITEMS = [
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
];
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

/* Demo fleet — SIM ONLY: JLTVs and M1165 HMMWVs, nothing else.
   This intentionally differs from server.py's DEMO / DEMO_AUTHS (the
   real server's demo seed is unchanged and still carries LMTVs,
   Trailers, F-250s and Generators). Rows are RETYPED, not renumbered:
   same bumper numbers, same unit spread, same statuses, same order —
   so the seeded UTCs (which reference demo bumpers) and every
   hand-computed seed expectation keep resolving. due-in-days is
   relative to "today", exactly like the server's loader
   (date.today() + timedelta(days=due_days)). */
const DEMO = [
  ["14 ASOS", "SAMPLE-101", "JLTV", "FMC", 12400, 90, "Motor Pool A"],
  ["14 ASOS", "SAMPLE-102", "JLTV", "PMC", 23100, 12, "Motor Pool A"],
  ["14 ASOS", "SAMPLE-103", "M1165 HMMWV", "FMC", 45200, 60, "Motor Pool A"],
  ["14 ASOS", "SAMPLE-104", "M1165 HMMWV", "NMC", 51800, -5, "Maintenance Bay"],
  ["14 ASOS", "SAMPLE-105", "JLTV", "FMC", 18900, 45, "Motor Pool B"],
  ["14 ASOS", "SAMPLE-106", "M1165 HMMWV", "FMC", 0, 120, "Motor Pool B"],
  ["15 ASOS", "SAMPLE-201", "JLTV", "FMC", 9800, 75, "Motor Pool A"],
  ["15 ASOS", "SAMPLE-202", "M1165 HMMWV", "FMC", 33400, 20, "Motor Pool A"],
  ["15 ASOS", "SAMPLE-203", "JLTV", "PMC", 61200, 8, "Motor Pool A"],
  ["15 ASOS", "SAMPLE-204", "M1165 HMMWV", "NMC", 1450, -2, "Maintenance Bay"],
  ["19 ASOS", "SAMPLE-301", "JLTV", "FMC", 15200, 110, "Motor Pool A"],
  ["19 ASOS", "SAMPLE-302", "JLTV", "FMC", 16750, 55, "Motor Pool A"],
  ["19 ASOS", "SAMPLE-303", "M1165 HMMWV", "PMC", 27400, 15, "Motor Pool B"],
  ["19 ASOS", "SAMPLE-304", "M1165 HMMWV", "FMC", 29800, 80, "Motor Pool B"],
  ["20 ASOS", "SAMPLE-401", "M1165 HMMWV", "FMC", 40100, 33, "Motor Pool A"],
  ["20 ASOS", "SAMPLE-402", "JLTV", "NMC", 19300, -9, "Maintenance Bay"],
  ["20 ASOS", "SAMPLE-403", "M1165 HMMWV", "FMC", 0, 150, "Motor Pool A"],
  ["20 ASOS", "SAMPLE-404", "JLTV", "FMC", 55700, 64, "Motor Pool A"],
];
/* Per-unit authorization totals match the old mix (14:10, 15:8, 19:7,
   20:8, ASOG:0, group 33) — only the type split changed, so the
   auth-vs-on-hand viz stays in the same shape it had. */
const DEMO_AUTHS = [
  ["14 ASOS", "JLTV", 8], ["14 ASOS", "M1165 HMMWV", 2],
  ["15 ASOS", "JLTV", 5], ["15 ASOS", "M1165 HMMWV", 3],
  ["19 ASOS", "JLTV", 4], ["19 ASOS", "M1165 HMMWV", 3],
  ["20 ASOS", "JLTV", 5], ["20 ASOS", "M1165 HMMWV", 3],
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
    inspections: [], inspItems: [], discrepancies: [], discPhotos: [],
    nextUserId: 1, nextVehicleId: 1, nextAuthId: 1,
    nextUtcId: 1, nextAuditId: 1, nextConflictId: 1,
    nextInspId: 1, nextDiscId: 1, nextDiscPhotoId: 1,
    tokenSeq: 0,
  };

  const publicUser = (u) => ({id: u.id, username: u.username,
    display_name: u.display_name, unit: u.unit, role: u.role});
  /* Vehicle dict safe for JSON: the in-memory photo data URL (_photo)
     never leaves the backend — only a presence flag, like the server. */
  const publicVehicle = (v) => {
    const {_photo, ...rest} = v;
    return {...rest, photo: _photo ? 1 : 0};
  };
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
    {id: state.nextUserId++, username: "operator",
     display_name: "Demo Operator", unit: "14 ASOS", role: "operator",
     _pw: null},
    {id: state.nextUserId++, username: "vco",
     display_name: "Demo VCO", unit: "18 ASOG", role: "vco",
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
      .map(v => publicVehicle(v));
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
  /* Anchored route match (server.py _route_id mirror): shape segments
     with "?" marking an integer id slot. Returns the parsed ids, or
     null when the path does not match EXACTLY — prefix-only matching
     used to let trailing segments fall through to the wrong handler
     (DELETE /api/vehicles/{id}/photo/extra deleted the vehicle). */
  function routeIds(path, ...shape) {
    const parts = path.replace(/^\/|\/$/g, "").split("/");
    if (parts.length !== shape.length) return null;
    const ids = [];
    for (let i = 0; i < shape.length; i++) {
      if (shape[i] === "?") {
        if (!/^-?\d+$/.test(parts[i])) return null;
        const value = Number(parts[i]);
        if (!Number.isSafeInteger(value)) return null;
        ids.push(value);
      } else if (parts[i] !== shape[i]) {
        return null;
      }
    }
    return ids;
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
      // predictable demo rule: "operator" is the Operator (seeded above);
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
    const role = data.role === undefined ? "operator" : data.role;
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
        if (target.role === "vco" &&
            state.users.filter(u => u.role === "vco").length <= 1)
          throw bad("cannot demote the last VCO");
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
        return {status: 200, body: {vehicle: publicVehicle(existing),
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
    return {status: 201, body: {vehicle: publicVehicle(v)}};
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
    const out = {vehicle: publicVehicle(row)};
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

  /* ======================================================== inspections
     Full in-memory mirror of server.py's 1800 endpoints (same
     validation, same side effects: mileage update, audit rows,
     discrepancy rows for failed items, VCO-only review with the same
     403/404/409 semantics). Retention is N/A here: sim state resets
     on refresh, so nothing ever ages to 30 days. */
  function validateInspectionItems(items) {
    if (!Array.isArray(items) || !items.length)
      throw bad("items must be a non-empty list");
    if (items.length > 45)
      throw bad("too many items (35 numbered + 10 other max)");
    const seen = new Set();
    let others = 0;
    const clean = [];
    for (const entry of items) {
      if (!entry || typeof entry !== "object" || Array.isArray(entry))
        throw bad("each item must be an object");
      let itemNo = entry.item_no;
      let text;
      if (typeof itemNo === "boolean")
        throw bad('item_no must be 1-35 or "other"');
      if (typeof itemNo === "number") {
        if (!Number.isInteger(itemNo) || itemNo < 1 || itemNo > 35)
          throw bad('item_no must be 1-35 or "other"');
        if (seen.has(itemNo)) throw bad("duplicate item " + itemNo);
        seen.add(itemNo);
        text = INSPECTION_ITEMS[itemNo - 1];
      } else if (itemNo === "other") {
        if (++others > 10) throw bad("at most 10 other items");
        text = typeof entry.text === "string" ? entry.text.trim() : "";
        if (!text) throw bad("other items need text");
        if (text.length > 120)
          throw bad("other item text must be <= 120 chars");
        itemNo = null;
      } else {
        throw bad('item_no must be 1-35 or "other"');
      }
      const applies = entry.applies === undefined ? true : entry.applies;
      if (typeof applies !== "boolean")
        throw bad("applies must be true or false");
      let result = null;
      if (applies) {
        if (entry.result !== "pass" && entry.result !== "fail")
          throw bad("result must be pass or fail when the item applies");
        result = entry.result;
      }
      let comment = entry.comment === null || entry.comment === undefined
        ? "" : entry.comment;
      if (typeof comment !== "string") throw bad("comment must be text");
      if (comment.length > 2000)
        throw bad("comment must be <= 2000 chars");
      clean.push({item_no: itemNo, item_text: text, applies,
                  result, comment: comment.trim()});
    }
    return clean;
  }
  function parsePhotoDataUrl(data) {
    if (!data || typeof data !== "object")
      throw bad("photo body must be an object");
    const url = data.image;
    if (typeof url !== "string" || !url.startsWith("data:image/"))
      throw bad("image must be a data URL (data:image/...)");
    const comma = url.indexOf(",");
    if (comma < 0) throw bad("image data URL is malformed");
    const header = url.slice(0, comma);
    const mime = header.slice(5).split(";", 1)[0];
    if (!["image/jpeg", "image/png", "image/webp", "image/gif"]
          .includes(mime) || !header.includes(";base64"))
      throw bad("image must be a base64 JPEG, PNG, WebP or GIF");
    const b64 = url.slice(comma + 1);
    if (!b64) throw bad("image is empty");
    if (url.length > 3 * 1024 * 1024)
      throw new SimError(413, "request body too large (3 MB cap)");
    try { atob(b64); }
    catch (e) { throw bad("image data is not valid base64"); }
    return {dataUrl: url, mime};
  }
  function inspectionFull(iid) {
    const insp = state.inspections.find(x => x.id === iid);
    if (!insp) return null;
    const items = state.inspItems.filter(x => x.inspection_id === iid)
      .map(x => ({item_no: x.item_no, item_text: x.item_text,
                  applies: x.applies, result: x.result,
                  comment: x.comment}));
    const discs = state.discrepancies.filter(x => x.inspection_id === iid)
      .map(d => ({...d, photos: state.discPhotos
        .filter(p => p.discrepancy_id === d.id)
        .map(p => ({id: p.id,
          url: "/api/inspections/" + iid + "/discrepancies/" + d.id +
               "/photos/" + p.id}))}));
    return {inspection: {...insp}, items, discrepancies: discs};
  }
  function createInspection(body, user) {
    let vehicleId = body.vehicle_id;
    if (typeof vehicleId === "string" && INT_RE.test(vehicleId.trim()))
      vehicleId = parseInt(vehicleId.trim(), 10);
    if (typeof vehicleId !== "number" || !Number.isInteger(vehicleId))
      throw bad("vehicle_id must be an integer");
    const monthYear = typeof body.month_year === "string"
      ? body.month_year.trim() : "";
    if (!monthYear) throw bad("month_year is required");
    if (monthYear.length > 40) throw bad("month_year must be <= 40 chars");
    const mileage = strictNonnegInt(body.mileage_hours, "mileage_hours");
    const tires = {};
    for (const f of ["tire_front", "tire_rear"]) {
      const v = body[f];
      if (v === null || v === undefined) tires[f] = "";
      else if (typeof v !== "string") throw bad(f + " must be text");
      else if (v.trim().length > 40) throw bad(f + " must be <= 40 chars");
      else tires[f] = v.trim();
    }
    const items = validateInspectionItems(body.items);
    const veh = state.vehicles.find(v => v.id === vehicleId);
    if (!veh) throw new SimError(404, "vehicle not found");
    const insp = {id: state.nextInspId++, vehicle_id: vehicleId,
      unit: veh.unit, bumper: veh.bumper, operator: user.display_name,
      month_year: monthYear, mileage_hours: mileage,
      tire_front: tires.tire_front, tire_rear: tires.tire_rear,
      status: "submitted", created_at: nowIso(),
      reviewed_by: "", reviewed_at: ""};
    state.inspections.push(insp);
    let failed = 0;
    for (const it of items) {
      state.inspItems.push({inspection_id: insp.id, ...it});
      if (it.applies && it.result === "fail") {
        failed++;
        state.discrepancies.push({id: state.nextDiscId++,
          inspection_id: insp.id, item_no: it.item_no,
          description: it.comment || "Failed — see comment",
          date_found: localDatePlus(0), miles_hours: mileage,
          status: "open"});
      }
    }
    if (mileage !== null) {
      const old = veh.mileage;
      veh.mileage = mileage;
      veh.updated_by = user.display_name;
      veh.updated_at = nowIso();
      audit(vehicleId, user.display_name, "update", "mileage", old, mileage,
            "1800 inspection mileage/hours");
    }
    audit(vehicleId, user.display_name, "inspection", "", "", "",
          "1800 inspection submitted (" + monthYear + ") — " +
          items.length + " items, " + failed + " " +
          (failed === 1 ? "discrepancy" : "discrepancies"));
    return {status: 201, body: inspectionFull(insp.id)};
  }
  function listInspections(query) {
    let rows = [...state.inspections];
    if (query.vehicle_id !== undefined) {
      if (!INT_RE.test(String(query.vehicle_id)))
        throw bad("vehicle_id must be an integer");
      rows = rows.filter(r => r.vehicle_id === parseInt(query.vehicle_id, 10));
    }
    if (query.unit !== undefined) {
      if (!UNIT_NAMES.includes(query.unit))
        throw bad("unit must be one of the five units");
      rows = rows.filter(r => r.unit === query.unit);
    }
    if (query.reviewed !== undefined) {
      if (query.reviewed === "true")
        rows = rows.filter(r => r.status === "reviewed");
      else if (query.reviewed === "false")
        rows = rows.filter(r => r.status === "submitted");
      else throw bad("reviewed must be true or false");
    }
    rows.sort((a, b) =>
      ((a.status === "submitted" ? 0 : 1) - (b.status === "submitted" ? 0 : 1)) ||
      (b.id - a.id));
    const withCounts = rows.map(r => ({...r,
      item_count: state.inspItems.filter(x => x.inspection_id === r.id).length,
      fail_count: state.discrepancies.filter(x => x.inspection_id === r.id).length}));
    return {status: 200, body: {inspections: withCounts}};
  }
  function reviewInspection(path, body, user) {
    const iid = pathId(path, "/api/inspections/");
    if (iid === null) throw new SimError(404, "inspection not found");
    // shape validation first (server: 400 beats 409 for bad shapes)
    let dispositions = body.dispositions === null ||
      body.dispositions === undefined ? [] : body.dispositions;
    if (!Array.isArray(dispositions))
      throw bad("dispositions must be a list");
    const clean = [];
    const seen = new Set();
    for (const d of dispositions) {
      if (!d || typeof d !== "object")
        throw bad("each disposition must be an object");
      if (typeof d.discrepancy_id !== "number" ||
          !Number.isInteger(d.discrepancy_id))
        throw bad("discrepancy_id must be an integer");
      if (seen.has(d.discrepancy_id))
        throw bad("duplicate discrepancy in dispositions");
      seen.add(d.discrepancy_id);
      if (!["corrected", "delayed", "disposition"].includes(d.status))
        throw bad("disposition status must be corrected, delayed or "
                  + "disposition");
      clean.push(d);
    }
    const insp = state.inspections.find(x => x.id === iid);
    if (!insp) throw new SimError(404, "inspection not found");
    if (insp.status === "reviewed")
      throw new SimError(409, "inspection already reviewed");
    for (const d of clean) {
      const disc = state.discrepancies.find(x => x.id === d.discrepancy_id &&
                                                 x.inspection_id === iid);
      if (!disc)
        throw bad("discrepancy " + d.discrepancy_id +
                  " does not belong to this inspection");
      if (disc.status !== d.status) {
        const old = disc.status;
        disc.status = d.status;
        audit(insp.vehicle_id, user.display_name, "inspection-review",
              "discrepancy", old, d.status,
              "discrepancy " + d.discrepancy_id);
      }
    }
    insp.status = "reviewed";
    insp.reviewed_by = user.display_name;
    insp.reviewed_at = nowIso();
    audit(insp.vehicle_id, user.display_name, "inspection-review", "", "", "",
          "1800 inspection reviewed (" + insp.month_year + ")");
    return {status: 200, body: {inspection: {...insp}}};
  }
  function attachVehiclePhoto(path, body, user) {
    const vid = pathId(path, "/api/vehicles/");
    if (vid === null) throw new SimError(404, "vehicle not found");
    const veh = state.vehicles.find(v => v.id === vid);
    if (!veh) throw new SimError(404, "vehicle not found");
    const {dataUrl} = parsePhotoDataUrl(body);
    const had = !!veh._photo;
    veh._photo = dataUrl;
    veh.updated_by = user.display_name;
    veh.updated_at = nowIso();
    audit(vid, user.display_name, "photo", "photo",
          had ? "present" : "", "present",
          had ? "vehicle photo replaced" : "vehicle photo attached");
    return {status: 200, body: {ok: true, photo: true}};
  }
  function removeVehiclePhoto(path, user) {
    const vid = pathId(path, "/api/vehicles/");
    if (vid === null) throw new SimError(404, "vehicle not found");
    const veh = state.vehicles.find(v => v.id === vid);
    if (!veh) throw new SimError(404, "vehicle not found");
    if (veh._photo) {
      delete veh._photo;
      veh.updated_by = user.display_name;
      veh.updated_at = nowIso();
      audit(vid, user.display_name, "photo", "photo", "present", "",
            "vehicle photo removed");
    }
    return {status: 200, body: {ok: true, photo: false}};
  }
  function getVehiclePhoto(path) {
    const vid = pathId(path, "/api/vehicles/");
    const veh = vid === null ? null
      : state.vehicles.find(v => v.id === vid);
    if (!veh || !veh._photo)
      throw new SimError(404, "vehicle photo not found");
    return {status: 200, body: veh._photo, ctype: "text/plain"};
  }
  function attachDiscPhoto(path, body, user) {
    const parts = path.replace(/^\/|\/$/g, "").split("/");
    const iid = Number(parts[2]), did = Number(parts[4]);
    if (!Number.isInteger(iid) || !Number.isInteger(did))
      throw new SimError(404, "photo not found");
    const {dataUrl} = parsePhotoDataUrl(body);
    const insp = state.inspections.find(x => x.id === iid);
    if (!insp) throw new SimError(404, "inspection not found");
    // review closes the record (v1.2 fix pass): no new photos
    // on a reviewed inspection
    if (insp.status === "reviewed")
      throw new SimError(409, "inspection already reviewed");
    const disc = state.discrepancies.find(x => x.id === did &&
                                               x.inspection_id === iid);
    if (!disc) throw new SimError(404, "discrepancy not found");
    if (state.discPhotos.filter(p => p.discrepancy_id === did).length >= 4)
      throw new SimError(409, "discrepancy already has 4 photos");
    const photo = {id: state.nextDiscPhotoId++, discrepancy_id: did, dataUrl};
    state.discPhotos.push(photo);
    audit(insp.vehicle_id, user.display_name, "inspection-photo", "", "", "",
          "photo added to discrepancy " + did + " (inspection " + iid + ")");
    return {status: 201, body: {photo_id: photo.id,
      url: "/api/inspections/" + iid + "/discrepancies/" + did +
           "/photos/" + photo.id}};
  }
  function deleteDiscPhoto(path, user) {
    const ids = routeIds(path, "api", "inspections", "?",
                         "discrepancies", "?", "photos", "?");
    if (!ids) throw new SimError(404, "photo not found");
    const [iid, did, pid] = ids;
    const idx = state.discPhotos.findIndex(p => p.id === pid &&
                                                p.discrepancy_id === did);
    if (idx < 0) throw new SimError(404, "photo not found");
    const disc = state.discrepancies.find(x => x.id === did);
    if (!disc || disc.inspection_id !== iid)
      throw new SimError(404, "photo not found");
    const insp = state.inspections.find(x => x.id === iid);
    state.discPhotos.splice(idx, 1);
    audit(insp ? insp.vehicle_id : null, user.display_name,
          "inspection-photo", "", "", "",
          "photo removed from discrepancy " + did +
          " (inspection " + iid + ")");
    return {status: 200, body: {ok: true}};
  }
  function getDiscPhoto(path) {
    const parts = path.replace(/^\/|\/$/g, "").split("/");
    const iid = Number(parts[2]), did = Number(parts[4]),
      pid = Number(parts[6]);
    const photo = state.discPhotos.find(p => p.id === pid &&
                                             p.discrepancy_id === did);
    const disc = state.discrepancies.find(x => x.id === did);
    if (!photo || !disc || disc.inspection_id !== iid)
      throw new SimError(404, "photo not found");
    return {status: 200, body: photo.dataUrl, ctype: "text/plain"};
  }

  /* ======================================================== router
     Mirrors server.py's dispatch order: public routes first, then
     sign-in required for everything else, then per-route role gates. */
  function handle(method, path, body, headers, query = {}) {
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
        if (routeIds(path, "api", "vehicles", "?", "photo"))
          return getVehiclePhoto(path);
        if (routeIds(path, "api", "inspections", "?",
                     "discrepancies", "?", "photos", "?"))
          return getDiscPhoto(path);
        if (path === "/api/inspections")
          return listInspections(query);
        {
          const ids = routeIds(path, "api", "inspections", "?");
          if (ids) {
            const full = inspectionFull(ids[0]);
            if (!full) throw new SimError(404, "inspection not found");
            return {status: 200, body: full};
          }
        }
        if (path === "/api/users") {
          if (user.role !== "vco")
            throw new SimError(403, "VCO only");
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
          if (user.role !== "vco")
            throw new SimError(403, "VCO only");
          return createUser(body, user.display_name);
        }
        if (path === "/api/inspections")
          return createInspection(body, user);
        if (routeIds(path, "api", "inspections", "?", "review")) {
          if (user.role !== "vco")
            throw new SimError(403, "VCO only");
          return reviewInspection(path, body, user);
        }
        if (routeIds(path, "api", "vehicles", "?", "photo"))
          return attachVehiclePhoto(path, body, user);
        if (routeIds(path, "api", "inspections", "?",
                     "discrepancies", "?", "photos"))
          return attachDiscPhoto(path, body, user);
        if (path === "/api/vehicles") {
          requireRole(user, ["editor", "vco"]);
          return insertVehicle(body, user);
        }
        if (path === "/api/authorizations") {
          requireRole(user, ["editor", "vco"]);
          return upsertAuthorization(body, user);
        }
        if (path === "/api/utcs") {
          requireRole(user, ["editor", "vco"]);
          return createUtc(body, user);
        }
        if (routeIds(path, "api", "utcs", "?", "vehicles")) {
          requireRole(user, ["editor", "vco"]);
          return utcAddVehicle(path, body, user);
        }
        if (path === "/api/import/preview") {
          requireRole(user, ["editor", "vco"]);
          return importPreview(body);
        }
        if (path === "/api/import/confirm") {
          requireRole(user, ["editor", "vco"]);
          return importConfirm(body, user);
        }
        if (path === "/api/demo/load") {
          if (user.role !== "vco")
            throw new SimError(403, "VCO only");
          // reset/reseed: demo data returns to the shipped seed; any
          // real (non-demo) records the session created are kept,
          // exactly like the server's loader
          loadDemo(user.display_name);
          return {status: 200, body: {ok: true}};
        }
        if (path === "/api/demo/remove") {
          if (user.role !== "vco")
            throw new SimError(403, "VCO only");
          const n = purgeDemo(user.display_name);
          return {status: 200, body: {ok: true, removed: n}};
        }
        if (routeIds(path, "api", "conflicts", "?", "resolve")) {
          requireRole(user, ["editor", "vco"]);
          return resolveConflict(path, body, user);
        }
        throw new SimError(404, "not found");
      }
      if (method === "PUT") {
        const user = requireUser(headers);
        if (routeIds(path, "api", "vehicles", "?")) {
          requireRole(user, ["editor", "vco"]);
          return updateVehicle(path, body, user);
        }
        if (routeIds(path, "api", "users", "?")) {
          if (user.role !== "vco")
            throw new SimError(403, "VCO only");
          return updateUser(path, body, user);
        }
        if (routeIds(path, "api", "utcs", "?")) {
          requireRole(user, ["editor", "vco"]);
          return updateUtc(path, body, user);
        }
        throw new SimError(404, "not found");
      }
      if (method === "DELETE") {
        const user = requireUser(headers);
        if (routeIds(path, "api", "vehicles", "?", "photo"))
          return removeVehiclePhoto(path, user);
        if (routeIds(path, "api", "inspections", "?",
                     "discrepancies", "?", "photos", "?"))
          return deleteDiscPhoto(path, user);
        if (routeIds(path, "api", "vehicles", "?")) {
          requireRole(user, ["editor", "vco"]);
          return deleteVehicle(path, user);
        }
        if (routeIds(path, "api", "authorizations", "?")) {
          requireRole(user, ["editor", "vco"]);
          return deleteAuthorization(path, user);
        }
        if (routeIds(path, "api", "utcs", "?", "vehicles", "?")) {
          requireRole(user, ["editor", "vco"]);
          return utcRemoveVehicle(path, user);
        }
        if (routeIds(path, "api", "utcs", "?")) {
          requireRole(user, ["editor", "vco"]);
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
    const query = {};
    try {
      const u = new URL(rawUrl, window.location.href);
      path = u.pathname;
      u.searchParams.forEach((v, k) => { query[k] = v; });
    } catch (e) { /* keep raw */ }
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
      result = backend.handle(method, path, bodyObj, headers, query);
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
        "<b>operator</b> = Operator (read-only + 1800 inspections) · " +
        "<b>vco</b> = VCO (accounts + inspection review)<br>" +
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
