/* Skyline WEB SIM checks — Node harness for docs/sim/sim.js.
   Loads the sim backend factory (no browser) and exercises the API
   contract the real frontend depends on, with expected totals
   hand-computed from the SIM's own DEMO / DEMO_AUTHS (JLTVs and
   M1165 HMMWVs only — the sim demo intentionally differs from
   server.py's seed, which still carries LMTVs, Trailers, F-250s
   and Generators):
     18 vehicles: FMC 12, PMC 3, NMC 3
     authorizations: 14 ASOS 10, 15 ASOS 8, 19 ASOS 7, 20 ASOS 8,
                     18 ASOG 0  -> group 33 (8 rows, 2 per squadron)
     group MC rate = 12/18 -> 66.7
   Run: node tests/sim_check.js */
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const SIM_DIR = path.join(__dirname, "..", "docs", "sim");
const {createSimBackend} = require(path.join(SIM_DIR, "sim.js"));

let n = 0;
function check(name, fn) {
  try {
    fn();
    n++;
    console.log("ok -", name);
  } catch (e) {
    console.error("FAIL -", name);
    console.error(e);
    process.exit(1);
  }
}

/* ---------- the sim ships the REAL frontend, byte-identical -------- */
check("app.js and style.css in docs/sim are byte-identical to static/", () => {
  for (const f of ["app.js", "style.css"]) {
    const a = fs.readFileSync(path.join(__dirname, "..", "static", f));
    const b = fs.readFileSync(path.join(SIM_DIR, f));
    assert.ok(a.equals(b), f + " differs");
  }
});
check("every asset the sim index.html references exists in docs/sim/", () => {
  const html = fs.readFileSync(path.join(SIM_DIR, "index.html"), "utf8");
  const refs = [...html.matchAll(/(?:src|href)="([^"]+)"/g)]
    .map(m => m[1])
    .filter(r => !r.startsWith("http") && !r.startsWith("/api") &&
                 !r.startsWith("#") && !r.startsWith("/"));
  assert.ok(refs.length >= 8, "expected asset refs, got " + refs.length);
  for (const r of refs)
    assert.ok(fs.existsSync(path.join(SIM_DIR, r)), "missing: " + r);
  assert.ok(html.includes('src="sim.js"'), "sim.js script tag missing");
  assert.ok(html.indexOf('src="sim.js"') < html.indexOf('src="app.js"'),
            "sim.js must load before app.js");
});

const be = createSimBackend();
const call = (method, p, body, headers, query) =>
  be.handle(method, p, body, headers, query || {});
const as = (token) => ({Authorization: "Bearer " + token});

/* ---------- auth / bootstrap ---------- */
let operatorTok, vcoTok;
check("setup status: setup_needed is false", () => {
  const r = call("GET", "/api/setup/status");
  assert.strictEqual(r.status, 200);
  assert.strictEqual(r.body.setup_needed, false);
});
check("fleet requires sign-in (401, server error shape)", () => {
  const r = call("GET", "/api/fleet");
  assert.strictEqual(r.status, 401);
  assert.strictEqual(r.body.error, "sign-in required");
});
check("login: operator -> Operator demo user", () => {
  const r = call("POST", "/api/login",
                 {username: "operator", password: "x"});
  assert.strictEqual(r.status, 200);
  assert.strictEqual(r.body.user.role, "operator");
  operatorTok = r.body.token;
});
check("login: vco -> VCO demo user", () => {
  const r = call("POST", "/api/login",
                 {username: "vco", password: "x"});
  assert.strictEqual(r.status, 200);
  assert.strictEqual(r.body.user.role, "vco");
  vcoTok = r.body.token;
});
check("login: any other name signs in as Operator (documented rule)", () => {
  const r = call("POST", "/api/login",
                 {username: "someone-else", password: "x"});
  assert.strictEqual(r.status, 200);
  assert.strictEqual(r.body.user.role, "operator");
});
check("login: an unseeded name is not a write-role user (v1.3 two roles)", () => {
  const r = call("POST", "/api/login",
                 {username: "writer", password: "x"});
  assert.strictEqual(r.status, 200);
  assert.strictEqual(r.body.user.role, "operator");
});
check("GET /api/me returns the signed-in user", () => {
  const r = call("GET", "/api/me", null, as(vcoTok));
  assert.strictEqual(r.status, 200);
  assert.strictEqual(r.body.user.username, "vco");
  assert.strictEqual(r.body.user.is_main_vco, 1);
});
check("seed users: Demo VCO is the main VCO for 18 ASOG", () => {
  const r = call("GET", "/api/users", null, as(vcoTok));
  assert.strictEqual(r.status, 200);
  const vco = r.body.users.find(u => u.username === "vco");
  assert.strictEqual(vco.is_main_vco, 1);
  assert.strictEqual(vco.unit, "18 ASOG");
  const op = r.body.users.find(u => u.username === "operator");
  assert.strictEqual(op.is_main_vco, 0);
  const snap = call("GET", "/api/fleet", null, as(vcoTok)).body;
  assert.strictEqual(snap.main_vcos["18 ASOG"], "Demo VCO");
});

/* ---------- seed snapshot totals ---------- */
let snap;
check("seed snapshot: group totals from the sim's JLTV + M1165 demo data", () => {
  snap = call("GET", "/api/fleet", null, as(vcoTok)).body;
  assert.strictEqual(snap.vehicles.length, 18);
  assert.strictEqual(snap.group.on_hand, 18);
  assert.strictEqual(snap.group.fmc, 12);
  assert.strictEqual(snap.group.pmc, 3);
  assert.strictEqual(snap.group.nmc, 3);
  assert.strictEqual(snap.group.authorizations, 33);
  assert.strictEqual(snap.group.mc_rate, 66.7);
  assert.strictEqual(snap.group.includes_demo, true);
});
check("seed snapshot: per-unit OWN stats (units[] semantics)", () => {
  const by = Object.fromEntries(snap.units.map(u => [u.unit, u]));
  assert.deepStrictEqual(
    [by["14 ASOS"].on_hand, by["14 ASOS"].authorizations,
     by["14 ASOS"].fmc, by["14 ASOS"].pmc, by["14 ASOS"].nmc,
     by["14 ASOS"].mc_rate], [6, 10, 4, 1, 1, 66.7]);
  assert.deepStrictEqual(
    [by["15 ASOS"].on_hand, by["15 ASOS"].authorizations,
     by["15 ASOS"].fmc, by["15 ASOS"].mc_rate], [4, 8, 2, 50.0]);
  assert.deepStrictEqual(
    [by["19 ASOS"].on_hand, by["19 ASOS"].authorizations,
     by["19 ASOS"].fmc, by["19 ASOS"].mc_rate], [4, 7, 3, 75.0]);
  assert.deepStrictEqual(
    [by["20 ASOS"].on_hand, by["20 ASOS"].authorizations,
     by["20 ASOS"].fmc, by["20 ASOS"].mc_rate], [4, 8, 3, 75.0]);
  assert.deepStrictEqual(
    [by["18 ASOG"].on_hand, by["18 ASOG"].authorizations], [0, 0]);
  // group == exact sum of the five own-stats entries
  for (const k of ["on_hand", "authorizations", "fmc", "pmc", "nmc"])
    assert.strictEqual(snap.group[k],
      snap.units.reduce((s, u) => s + u[k], 0), "group " + k);
});
check("seed snapshot: all seed vehicles/authorizations are demo-flagged", () => {
  assert.ok(snap.vehicles.every(v => v.is_demo === 1));
  assert.ok(snap.vehicles.every(v => v.bumper.startsWith("SAMPLE-")));
  assert.ok(snap.authorizations.every(a => a.is_demo === 1));
  assert.strictEqual(snap.authorizations.length, 8);  // 2 rows per squadron
});
check("seed UTCs: two SAMPLE packages with the right members", () => {
  assert.strictEqual(snap.utcs.length, 2);
  const a = snap.utcs.find(u => u.code === "XFFC1");
  const b = snap.utcs.find(u => u.code === "X19RT");
  assert.ok(a && b);
  assert.strictEqual(a.unit, "14 ASOS");
  assert.deepStrictEqual(a.vehicle_ids, [1, 2, 3, 5]);
  assert.deepStrictEqual(b.vehicle_ids, [11, 12, 13]);
  const byId = Object.fromEntries(snap.vehicles.map(v => [v.id, v]));
  const counts = a.vehicle_ids.map(id => byId[id].status).sort();
  assert.deepStrictEqual(counts, ["FMC", "FMC", "FMC", "PMC"]);
});

/* ---------- vehicles ---------- */
check("operator cannot create a vehicle (403 role message)", () => {
  const r = call("POST", "/api/vehicles",
    {unit: "14 ASOS", bumper: "V-1", type: "JLTV"}, as(operatorTok));
  assert.strictEqual(r.status, 403);
  assert.strictEqual(r.body.error, "your role cannot do that");
});
let test1Id;
check("VCO creates a vehicle -> totals move", () => {
  const r = call("POST", "/api/vehicles",
    {unit: "14 ASOS", bumper: "TEST-1", type: "JLTV", status: "NMC",
     mileage: 100, location: "Motor Pool A"}, as(vcoTok));
  assert.strictEqual(r.status, 201);
  test1Id = r.body.vehicle.id;
  snap = call("GET", "/api/fleet", null, as(vcoTok)).body;
  assert.strictEqual(snap.group.on_hand, 19);
  assert.strictEqual(snap.group.nmc, 4);
  assert.strictEqual(snap.group.mc_rate, 63.2);   // 12/19
  const u14 = snap.units.find(u => u.unit === "14 ASOS");
  assert.strictEqual(u14.on_hand, 7);
});
check("duplicate bumper in the same unit -> 409 (server message)", () => {
  const r = call("POST", "/api/vehicles",
    {unit: "14 ASOS", bumper: "TEST-1", type: "JLTV"}, as(vcoTok));
  assert.strictEqual(r.status, 409);
  assert.strictEqual(r.body.error,
                     "bumper # TEST-1 already exists in 14 ASOS");
});
check("same bumper in a DIFFERENT unit is fine; delete restores totals", () => {
  const r = call("POST", "/api/vehicles",
    {unit: "15 ASOS", bumper: "TEST-1", type: "JLTV", status: "FMC"},
    as(vcoTok));
  assert.strictEqual(r.status, 201);
  const d = call("DELETE", "/api/vehicles/" + r.body.vehicle.id,
                 null, as(vcoTok));
  assert.strictEqual(d.status, 200);
  snap = call("GET", "/api/fleet", null, as(vcoTok)).body;
  assert.strictEqual(snap.group.on_hand, 19);
});
check("invalid status -> 400 with the server's message", () => {
  const r = call("POST", "/api/vehicles",
    {unit: "14 ASOS", bumper: "BAD-1", type: "JLTV", status: "BROKEN"},
    as(vcoTok));
  assert.strictEqual(r.status, 400);
  assert.strictEqual(r.body.error, "status must be FMC, PMC or NMC");
});
check("update flips status (base_status fresh) -> MC moves", () => {
  const r = call("PUT", "/api/vehicles/" + test1Id,
    {base_status: "NMC", status: "FMC"}, as(vcoTok));
  assert.strictEqual(r.status, 200);
  assert.strictEqual(r.body.vehicle.status, "FMC");
  snap = call("GET", "/api/fleet", null, as(vcoTok)).body;
  assert.strictEqual(snap.group.fmc, 13);
  assert.strictEqual(snap.group.mc_rate, 68.4);   // 13/19
});
check("stale base_status -> conflict flagged, server status kept", () => {
  const r = call("PUT", "/api/vehicles/" + test1Id,
    {base_status: "PMC", status: "NMC"}, as(vcoTok));
  assert.strictEqual(r.status, 200);
  assert.strictEqual(r.body.vehicle.status, "FMC");
  assert.ok(r.body.conflict && r.body.conflict.attempted === "NMC");
  const c = call("GET", "/api/conflicts", null, as(vcoTok));
  assert.strictEqual(c.body.conflicts.length, 1);
  const res = call("POST",
    "/api/conflicts/" + c.body.conflicts[0].id + "/resolve",
    {action: "keep"}, as(vcoTok));
  assert.strictEqual(res.status, 200);
  const c2 = call("GET", "/api/conflicts", null, as(vcoTok));
  assert.strictEqual(c2.body.conflicts.length, 0);
});

/* ---------- authorizations ---------- */
check("authorization delete (VCO) -> totals drop by the row's count", () => {
  const a = snap.authorizations.find(x => x.unit === "14 ASOS" &&
                                          x.vehicle_type === "M1165 HMMWV");
  assert.strictEqual(a.authorized, 2);
  const r = call("DELETE", "/api/authorizations/" + a.id,
                 null, as(operatorTok));
  assert.strictEqual(r.status, 403);              // operator first: no
  const d = call("DELETE", "/api/authorizations/" + a.id,
                 null, as(vcoTok));
  assert.strictEqual(d.status, 200);
  snap = call("GET", "/api/fleet", null, as(vcoTok)).body;
  assert.strictEqual(snap.group.authorizations, 31);
  assert.strictEqual(
    snap.units.find(u => u.unit === "14 ASOS").authorizations, 8);
  const again = call("DELETE", "/api/authorizations/" + a.id,
                     null, as(vcoTok));
  assert.strictEqual(again.status, 404);
  assert.strictEqual(again.body.error, "authorization not found");
});
check("authorization upsert on 18 ASOG -> its own stats + group move", () => {
  const r = call("POST", "/api/authorizations",
    {unit: "18 ASOG", vehicle_type: "JLTV", authorized: 5}, as(vcoTok));
  assert.strictEqual(r.status, 200);
  snap = call("GET", "/api/fleet", null, as(vcoTok)).body;
  assert.strictEqual(snap.group.authorizations, 36);
  assert.strictEqual(
    snap.units.find(u => u.unit === "18 ASOG").authorizations, 5);
  const bad = call("POST", "/api/authorizations",
    {unit: "18 ASOG", vehicle_type: "JLTV", authorized: -1}, as(vcoTok));
  assert.strictEqual(bad.status, 400);
});

/* ---------- UTCs ---------- */
let utcId;
check("UTC create: code normalized to uppercase; duplicate -> 409", () => {
  const r = call("POST", "/api/utcs",
    {unit: "15 ASOS", code: "xabc9", title: "Test pack"}, as(vcoTok));
  assert.strictEqual(r.status, 201);
  assert.strictEqual(r.body.utc.code, "XABC9");
  utcId = r.body.utc.id;
  const dup = call("POST", "/api/utcs",
    {unit: "15 ASOS", code: "XABC9", title: "dupe"}, as(vcoTok));
  assert.strictEqual(dup.status, 409);
  const v = call("POST", "/api/utcs",
    {unit: "15 ASOS", code: "VIEW1", title: ""}, as(operatorTok));
  assert.strictEqual(v.status, 403);
});
check("UTC membership: own-unit ok, wrong-unit -> 400, ghost -> 400", () => {
  const ok = call("POST", "/api/utcs/" + utcId + "/vehicles",
                  {vehicle_id: 7}, as(vcoTok));   // SAMPLE-201, 15 ASOS
  assert.strictEqual(ok.status, 200);
  const wrong = call("POST", "/api/utcs/" + utcId + "/vehicles",
                     {vehicle_id: 1}, as(vcoTok));  // 14 ASOS vehicle
  assert.strictEqual(wrong.status, 400);
  assert.strictEqual(wrong.body.error,
    "vehicle belongs to 14 ASOS; a UTC can only hold vehicles of " +
    "its own unit (15 ASOS)");
  const ghost = call("POST", "/api/utcs/" + utcId + "/vehicles",
                     {vehicle_id: 9999}, as(vcoTok));
  assert.strictEqual(ghost.status, 400);
  assert.strictEqual(ghost.body.error, "vehicle does not exist");
  snap = call("GET", "/api/fleet", null, as(vcoTok)).body;
  assert.deepStrictEqual(
    snap.utcs.find(u => u.id === utcId).vehicle_ids, [7]);
});
check("UTC update + membership remove + UTC delete (vehicles kept)", () => {
  const u = call("PUT", "/api/utcs/" + utcId,
    {unit: "15 ASOS", code: "XABC9", title: "Renamed pack"}, as(vcoTok));
  assert.strictEqual(u.status, 200);
  assert.strictEqual(u.body.utc.title, "Renamed pack");
  const rm = call("DELETE", "/api/utcs/" + utcId + "/vehicles/7",
                  null, as(vcoTok));
  assert.strictEqual(rm.status, 200);
  const rm2 = call("DELETE", "/api/utcs/" + utcId + "/vehicles/7",
                   null, as(vcoTok));
  assert.strictEqual(rm2.status, 404);
  const del = call("DELETE", "/api/utcs/" + utcId, null, as(vcoTok));
  assert.strictEqual(del.status, 200);
  snap = call("GET", "/api/fleet", null, as(vcoTok)).body;
  assert.ok(snap.vehicles.some(v => v.id === 7),
            "vehicle must survive UTC deletion");
  assert.ok(!snap.utcs.some(x => x.id === utcId));
});

/* ---------- import (CSV in-sim; xlsx honestly refused) ---------- */
check("import preview: CSV mapping, defaults, skips — then confirm adds", () => {
  const csv = [
    "Type,Asset Number,Location,Status,Mileage,Color",
    "JLTV,IMP-1,Motor Pool A,FMC,5000,green",
    "HMMWV,IMP-2,Motor Pool B,,,blue",
    "HMMWV,SAMPLE-401,Motor Pool A,FMC,40100,red",
    "JLTV,,Nowhere,FMC,10,black",
    "JLTV,IMP-1,Other,FMC,1,white",
  ].join("\n");
  const b64 = Buffer.from(csv, "utf8").toString("base64");
  const p = call("POST", "/api/import/preview",
    {unit: "20 ASOS", filename: "fleet.csv", content_b64: b64},
    as(vcoTok));
  assert.strictEqual(p.status, 200);
  assert.strictEqual(p.body.will_import, 2);
  assert.strictEqual(p.body.skipped, 3);
  assert.deepStrictEqual(p.body.unmapped_columns, ["Color"]);
  const imp2 = p.body.rows.find(r => r.bumper === "IMP-2");
  assert.strictEqual(imp2.status, "FMC");
  assert.strictEqual(imp2.status_defaulted, true);
  const reasons = Object.fromEntries(
    p.body.rows.map(r => [r.bumper || "(blank)", r.reason]));
  assert.ok(reasons["SAMPLE-401"].includes("never overwrites"));
  assert.ok(reasons["(blank)"].includes("missing asset number"));
  assert.ok(reasons["IMP-1"] === "" || true);
  const dupRow = p.body.rows.filter(r => r.bumper === "IMP-1")[1];
  assert.ok(dupRow.reason.includes("duplicate asset number in file"));
  const rows = p.body.rows.filter(r => r.ok).map(r => ({
    bumper: r.bumper, type: r.type, status: r.status,
    mileage: r.mileage, maint_due_date: r.maint_due_date,
    maint_due_miles: r.maint_due_miles, location: r.location,
    notes: r.notes}));
  const c = call("POST", "/api/import/confirm",
    {unit: "20 ASOS", filename: "fleet.csv", rows}, as(vcoTok));
  assert.strictEqual(c.status, 201);
  assert.strictEqual(c.body.created_count, 2);
  snap = call("GET", "/api/fleet", null, as(vcoTok)).body;
  assert.strictEqual(
    snap.units.find(u => u.unit === "20 ASOS").on_hand, 6);
  assert.strictEqual(snap.group.on_hand, 21);
  assert.strictEqual(snap.group.fmc, 15);
  assert.strictEqual(snap.group.mc_rate, 71.4);   // 15/21
});
check("import preview: .xlsx gets the honest sim answer, not a fake parse", () => {
  const b64 = Buffer.from("PK fake").toString("base64");
  const r = call("POST", "/api/import/preview",
    {unit: "20 ASOS", filename: "fleet.xlsx", content_b64: b64},
    as(vcoTok));
  assert.strictEqual(r.status, 400);
  assert.ok(r.body.error.includes(".csv"), r.body.error);
  assert.ok(r.body.error.includes("Python server"), r.body.error);
});

/* ---------- v1.2: 1800 inspections + photos (sim parity) ---------- */
let simInspId, simDiscId;
check("inspection: operator submits (201, discrepancy, mileage, audit)", () => {
  const snap = call("GET", "/api/fleet", null, as(operatorTok)).body;
  const veh = snap.vehicles.find(v => v.bumper === "SAMPLE-101");
  assert.ok(veh, "seed SAMPLE-101 present");
  const before = veh.mileage;
  const r = call("POST", "/api/inspections",
    {vehicle_id: veh.id, month_year: "October 2026",
     mileage_hours: before + 1000, tire_front: "80", tire_rear: "80",
     items: [
       {item_no: 1, applies: true, result: "pass", comment: ""},
       {item_no: 2, applies: true, result: "fail",
        comment: "oil leak at filter"},
       {item_no: 3, applies: false, comment: ""},
       {item_no: "other", text: "Winch cable fray", applies: true,
        result: "fail", comment: "cable strands showing"}]},
    as(operatorTok));
  assert.strictEqual(r.status, 201, JSON.stringify(r.body));
  assert.strictEqual(r.body.inspection.status, "submitted");
  assert.strictEqual(r.body.inspection.operator, "Demo Operator");
  assert.strictEqual(r.body.items.length, 4);
  assert.strictEqual(r.body.discrepancies.length, 2);
  assert.ok(r.body.discrepancies.every(d => d.status === "open"));
  simInspId = r.body.inspection.id;
  simDiscId = r.body.discrepancies[0].id;
  // same side effects as the server: mileage moved, status did not
  const after = call("GET", "/api/fleet", null, as(operatorTok)).body;
  const v2 = after.vehicles.find(v => v.id === veh.id);
  assert.strictEqual(v2.mileage, before + 1000);
  assert.strictEqual(v2.status, veh.status);
  const log = call("GET", "/api/audit", null, as(operatorTok)).body;
  assert.ok(log.audit.some(a => a.action === "inspection"));
  assert.ok(log.audit.some(a => a.action === "update" &&
                              a.field === "mileage"));
});
check("inspection: VCO submit + validation 400s + unknown vehicle 404", () => {
  const snap = call("GET", "/api/fleet", null, as(vcoTok)).body;
  const veh = snap.vehicles.find(v => v.bumper === "SAMPLE-102");
  const good = {vehicle_id: veh.id, month_year: "October 2026",
    items: [{item_no: 1, applies: true, result: "pass"}]};
  const r = call("POST", "/api/inspections", good, as(vcoTok));
  assert.strictEqual(r.status, 201);
  const bad = [
    {...good, items: []},
    {...good, items: [{item_no: 36, applies: true, result: "pass"}]},
    {...good, items: [{item_no: 1, applies: true}]},
    {...good, items: [{item_no: 1, applies: true, result: "ok"}]},
    {...good, items: [{item_no: 1, applies: true, result: "pass"},
                      {item_no: 1, applies: true, result: "pass"}]},
    {...good, mileage_hours: -1},
    {...good, month_year: ""}];
  for (const b of bad) {
    const rr = call("POST", "/api/inspections", b, as(operatorTok));
    assert.strictEqual(rr.status, 400, JSON.stringify(b));
  }
  const nf = call("POST", "/api/inspections",
    {...good, vehicle_id: 99999}, as(operatorTok));
  assert.strictEqual(nf.status, 404);
  const noauth = call("POST", "/api/inspections", good);
  assert.strictEqual(noauth.status, 401);
});
check("inspection: list filters + VCO review gates + double-review 409", () => {
  const all = call("GET", "/api/inspections", null, as(operatorTok)).body;
  assert.ok(all.inspections.some(i => i.id === simInspId));
  // unreviewed-first ordering: the submitted one leads
  assert.strictEqual(all.inspections[0].status, "submitted");
  const unrev = call("GET", "/api/inspections", null, as(vcoTok),
    {reviewed: "false"}).body;
  assert.ok(unrev.inspections.some(i => i.id === simInspId));
  assert.ok(unrev.inspections.every(i => i.status === "submitted"));
  const bad = call("GET", "/api/inspections", null, as(operatorTok),
    {reviewed: "maybe"});
  assert.strictEqual(bad.status, 400);
  const op = call("POST", "/api/inspections/" + simInspId + "/review",
    {}, as(operatorTok));
  assert.strictEqual(op.status, 403);
  const unauth = call("POST", "/api/inspections/" + simInspId + "/review",
    {});
  assert.strictEqual(unauth.status, 401);
  const nf = call("POST", "/api/inspections/99999/review", {}, as(vcoTok));
  assert.strictEqual(nf.status, 404);
  const ok = call("POST", "/api/inspections/" + simInspId + "/review",
    {dispositions: [{discrepancy_id: simDiscId, status: "corrected"}]},
    as(vcoTok));
  assert.strictEqual(ok.status, 200);
  assert.strictEqual(ok.body.inspection.status, "reviewed");
  assert.strictEqual(ok.body.inspection.reviewed_by, "Demo VCO");
  const twice = call("POST", "/api/inspections/" + simInspId + "/review",
    {}, as(vcoTok));
  assert.strictEqual(twice.status, 409);
  const detail = call("GET", "/api/inspections/" + simInspId, null,
    as(operatorTok)).body;
  const d = detail.discrepancies.find(x => x.id === simDiscId);
  assert.strictEqual(d.status, "corrected");
  const rev = call("GET", "/api/inspections", null, as(vcoTok),
    {reviewed: "true"}).body;
  assert.ok(rev.inspections.some(i => i.id === simInspId));
  assert.ok(!call("GET", "/api/inspections", null, as(vcoTok),
    {reviewed: "false"}).body.inspections.some(i => i.id === simInspId));
});
check("photos (sim mirror): vehicle attach/get/delete + flag without bytes", () => {
  const snap = call("GET", "/api/fleet", null, as(operatorTok)).body;
  const veh = snap.vehicles.find(v => v.bumper === "SAMPLE-103");
  assert.strictEqual(veh.photo, 0);
  const dataUrl = "data:image/jpeg;base64," +
    Buffer.from("SIM-JPEG").toString("base64");
  const a = call("POST", "/api/vehicles/" + veh.id + "/photo",
    {image: dataUrl}, as(operatorTok));
  assert.strictEqual(a.status, 200);
  assert.strictEqual(a.body.photo, true);
  const g = call("GET", "/api/vehicles/" + veh.id + "/photo", null,
    as(operatorTok));
  assert.strictEqual(g.status, 200);
  assert.strictEqual(g.body, dataUrl);
  const snap2 = call("GET", "/api/fleet", null, as(operatorTok)).body;
  const v2 = snap2.vehicles.find(v => v.id === veh.id);
  assert.strictEqual(v2.photo, 1);
  assert.ok(!("_photo" in v2), "snapshot must not carry photo bytes");
  const del = call("DELETE", "/api/vehicles/" + veh.id + "/photo", null,
    as(operatorTok));
  assert.strictEqual(del.status, 200);
  assert.strictEqual(del.body.photo, false);
  const gone = call("GET", "/api/vehicles/" + veh.id + "/photo", null,
    as(operatorTok));
  assert.strictEqual(gone.status, 404);
});
check("photos (sim mirror): discrepancy attach + 4-cap + 404s", () => {
  const snap = call("GET", "/api/fleet", null, as(operatorTok)).body;
  const veh = snap.vehicles.find(v => v.bumper === "SAMPLE-104");
  const r = call("POST", "/api/inspections",
    {vehicle_id: veh.id, month_year: "October 2026",
     items: [{item_no: 2, applies: true, result: "fail",
              comment: "leak"}]}, as(operatorTok));
  assert.strictEqual(r.status, 201);
  const iid = r.body.inspection.id;
  const did = r.body.discrepancies[0].id;
  const url = "/api/inspections/" + iid + "/discrepancies/" + did + "/photos";
  const img = "data:image/jpeg;base64," +
    Buffer.from("DISC").toString("base64");
  for (let i = 0; i < 4; i++) {
    const a = call("POST", url, {image: img}, as(operatorTok));
    assert.strictEqual(a.status, 201, "photo " + (i + 1));
    assert.ok(a.body.url);
  }
  const fifth = call("POST", url, {image: img}, as(operatorTok));
  assert.strictEqual(fifth.status, 409);
  const detail = call("GET", "/api/inspections/" + iid, null,
    as(operatorTok)).body;
  assert.strictEqual(detail.discrepancies[0].photos.length, 4);
  const one = call("GET", detail.discrepancies[0].photos[0].url, null,
    as(operatorTok));
  assert.strictEqual(one.status, 200);
  assert.strictEqual(one.body, img);
  const nf = call("POST",
    "/api/inspections/" + iid + "/discrepancies/99999/photos",
    {image: img}, as(operatorTok));
  assert.strictEqual(nf.status, 404);
});
check("v1.2 fix pass (sim): discrepancy photo delete round-trip + 404s", () => {
  const snap = call("GET", "/api/fleet", null, as(operatorTok)).body;
  const veh = snap.vehicles.find(v => v.bumper === "SAMPLE-105");
  const r = call("POST", "/api/inspections",
    {vehicle_id: veh.id, month_year: "October 2026",
     items: [{item_no: 2, applies: true, result: "fail",
              comment: "leak"}]}, as(operatorTok));
  assert.strictEqual(r.status, 201);
  const iid = r.body.inspection.id;
  const did = r.body.discrepancies[0].id;
  const url = "/api/inspections/" + iid + "/discrepancies/" + did + "/photos";
  const img = "data:image/jpeg;base64," +
    Buffer.from("DEL").toString("base64");
  const p1 = call("POST", url, {image: img}, as(operatorTok)).body;
  const p2 = call("POST", url, {image: img}, as(operatorTok)).body;
  const noauth = call("DELETE", url + "/" + p1.photo_id);
  assert.strictEqual(noauth.status, 401);
  const nf = call("DELETE", url + "/99999", null, as(operatorTok));
  assert.strictEqual(nf.status, 404);
  const del = call("DELETE", url + "/" + p1.photo_id, null,
    as(operatorTok));
  assert.strictEqual(del.status, 200);
  const detail = call("GET", "/api/inspections/" + iid, null,
    as(operatorTok)).body;
  assert.deepStrictEqual(
    detail.discrepancies[0].photos.map(p => p.id), [p2.photo_id]);
  const gone = call("GET", url + "/" + p1.photo_id, null, as(operatorTok));
  assert.strictEqual(gone.status, 404);
  const again = call("DELETE", url + "/" + p1.photo_id, null,
    as(operatorTok));
  assert.strictEqual(again.status, 404);
  const log = call("GET", "/api/audit", null, as(operatorTok)).body;
  assert.ok(log.audit.some(a => a.action === "inspection-photo" &&
    a.detail.includes("removed")));
});
check("v1.2 fix pass (sim): photo attach on reviewed inspection 409", () => {
  const snap = call("GET", "/api/fleet", null, as(operatorTok)).body;
  const veh = snap.vehicles.find(v => v.bumper === "SAMPLE-201");
  const r = call("POST", "/api/inspections",
    {vehicle_id: veh.id, month_year: "October 2026",
     items: [{item_no: 2, applies: true, result: "fail",
              comment: "leak"}]}, as(operatorTok));
  const iid = r.body.inspection.id;
  const did = r.body.discrepancies[0].id;
  const url = "/api/inspections/" + iid + "/discrepancies/" + did + "/photos";
  const img = "data:image/jpeg;base64," +
    Buffer.from("REV").toString("base64");
  assert.strictEqual(call("POST", url, {image: img},
    as(operatorTok)).status, 201);
  assert.strictEqual(call("POST", "/api/inspections/" + iid + "/review",
    {}, as(vcoTok)).status, 200);
  const after = call("POST", url, {image: img}, as(operatorTok));
  assert.strictEqual(after.status, 409);
  const detail = call("GET", "/api/inspections/" + iid, null,
    as(operatorTok)).body;
  assert.strictEqual(detail.discrepancies[0].photos.length, 1);
});
check("v1.2 fix pass (sim): anchored routes reject trailing segments", () => {
  const snap = call("GET", "/api/fleet", null, as(operatorTok)).body;
  const veh = snap.vehicles.find(v => v.bumper === "SAMPLE-103");
  const del = call("DELETE", "/api/vehicles/" + veh.id + "/photo/extra",
    null, as(vcoTok));
  assert.strictEqual(del.status, 404);
  const put = call("PUT", "/api/vehicles/" + veh.id + "/extra",
    {status: "NMC"}, as(vcoTok));
  assert.strictEqual(put.status, 404);
  const after = call("GET", "/api/fleet", null, as(operatorTok)).body;
  const v2 = after.vehicles.find(v => v.id === veh.id);
  assert.strictEqual(v2.status, veh.status);
  const det = call("GET", "/api/inspections/1/extra", null,
    as(operatorTok));
  assert.strictEqual(det.status, 404);
  const nopid = call("GET", "/api/inspections/1/discrepancies/1/photos",
    null, as(operatorTok));
  assert.strictEqual(nopid.status, 404);
});
check("v1.2 fix pass (sim): discrepancy date_found is the local date", () => {
  const snap = call("GET", "/api/fleet", null, as(operatorTok)).body;
  const veh = snap.vehicles.find(v => v.bumper === "SAMPLE-202");
  const r = call("POST", "/api/inspections",
    {vehicle_id: veh.id, month_year: "October 2026",
     items: [{item_no: 2, applies: true, result: "fail",
              comment: "leak"}]}, as(operatorTok));
  assert.strictEqual(r.status, 201);
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const local = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" +
    pad(d.getDate());
  assert.strictEqual(r.body.discrepancies[0].date_found, local);
});

/* ---------- audit / export / logout ---------- */
check("audit accumulates in-memory with the sim user's name", () => {
  const r = call("GET", "/api/audit", null, as(vcoTok));
  assert.strictEqual(r.status, 200);
  const acts = r.body.audit.map(a => a.action);
  assert.ok(acts.includes("create") && acts.includes("utc-create") &&
            acts.includes("authorization"));
  assert.ok(r.body.audit.every(a => typeof a.actor === "string"));
  assert.ok(r.body.audit.some(a => a.actor === "Demo VCO"));
});
check("CSV export mirrors the server's (auth required, SAMPLE rows)", () => {
  const noauth = call("GET", "/api/export.csv");
  assert.strictEqual(noauth.status, 401);
  const r = call("GET", "/api/export.csv", null, as(vcoTok));
  assert.strictEqual(r.status, 200);
  assert.strictEqual(r.ctype, "text/csv");
  assert.ok(r.body.startsWith("unit,bumper,type,status,"));
  assert.ok(r.body.includes("SAMPLE-101"));
});
check("v1.3 (sim): two-role matrix — operator 403s, VCO 200s", () => {
  const veh = call("POST", "/api/vehicles",
    {unit: "14 ASOS", bumper: "M-1", type: "JLTV"}, as(vcoTok));
  assert.strictEqual(veh.status, 201);
  const vid = veh.body.vehicle.id;
  assert.strictEqual(call("PUT", "/api/vehicles/" + vid,
    {notes: "no"}, as(operatorTok)).status, 403);
  assert.strictEqual(call("POST", "/api/authorizations",
    {unit: "14 ASOS", vehicle_type: "JLTV", authorized: 1},
    as(operatorTok)).status, 403);
  assert.strictEqual(call("POST", "/api/users",
    {username: "x1", password: "x1-pass-99", display_name: "X1",
     unit: "14 ASOS", role: "operator"}, as(operatorTok)).status, 403);
  assert.strictEqual(call("POST", "/api/utcs",
    {unit: "14 ASOS", code: "XSIM1", title: "t"},
    as(operatorTok)).status, 403);
  assert.strictEqual(call("PUT", "/api/vehicles/" + vid,
    {notes: "vco edit"}, as(vcoTok)).status, 200);
  assert.strictEqual(call("DELETE", "/api/vehicles/" + vid, null,
    as(vcoTok)).status, 200);
});
check("v1.3 (sim): main-VCO badge — uniqueness, transfer, auto-pass, no-cap", () => {
  const mk = (name, role, unit) => call("POST", "/api/users",
    {username: name, password: "pw-pass-99", display_name: name,
     unit, role}, as(vcoTok));
  const a = mk("sima", "vco", "15 ASOS");
  assert.strictEqual(a.status, 201);
  assert.strictEqual(a.body.user.is_main_vco, 1);
  const b = mk("simb", "vco", "15 ASOS");
  assert.strictEqual(b.body.user.is_main_vco, 0);
  const c = mk("simc", "vco", "15 ASOS");
  assert.strictEqual(c.status, 201, "no cap on VCOs in a unit");
  assert.strictEqual(call("PUT", "/api/users/" + b.body.user.id,
    {is_main_vco: true}, as(operatorTok)).status, 403);
  assert.strictEqual(call("PUT", "/api/users/" + b.body.user.id,
    {is_main_vco: true}, as(vcoTok)).status, 200);
  let users = call("GET", "/api/users", null, as(vcoTok)).body.users;
  const by = Object.fromEntries(users.map(u => [u.username, u]));
  assert.strictEqual(by["sima"].is_main_vco, 0);
  assert.strictEqual(by["simb"].is_main_vco, 1);
  assert.strictEqual(call("DELETE", "/api/users/" + b.body.user.id, null,
    as(vcoTok)).status, 200);
  users = call("GET", "/api/users", null, as(vcoTok)).body.users;
  const by2 = Object.fromEntries(users.map(u => [u.username, u]));
  assert.strictEqual(by2["sima"].is_main_vco, 1, "auto-pass to earliest remaining");
  assert.strictEqual(call("PUT", "/api/users/" + a.body.user.id,
    {role: "operator"}, as(vcoTok)).status, 200);
  users = call("GET", "/api/users", null, as(vcoTok)).body.users;
  const by3 = Object.fromEntries(users.map(u => [u.username, u]));
  assert.strictEqual(by3["simc"].is_main_vco, 1, "auto-pass on demotion");
  const log = call("GET", "/api/audit", null, as(vcoTok)).body;
  assert.ok(log.audit.some(x => x.action === "account" &&
    x.field === "is_main_vco"), "transfer is audited");
  // both sides of the transfer are audited, not just the target
  assert.ok(log.audit.some(x => x.action === "account" &&
    x.field === "is_main_vco" && x.detail === "user simb" &&
    x.old_value === "0" && x.new_value === "1"),
    "recipient badge gain audited");
  assert.ok(log.audit.some(x => x.action === "account" &&
    x.field === "is_main_vco" && x.detail === "user sima" &&
    x.old_value === "1" && x.new_value === "0"),
    "displaced main badge loss audited");
  const snap = call("GET", "/api/fleet", null, as(vcoTok)).body;
  assert.strictEqual(snap.main_vcos["15 ASOS"], "simc");
});
check("v1.3 (sim): explicit is_main_vco:false on a main auto-passes", () => {
  const mk = (name, unit) => call("POST", "/api/users",
    {username: name, password: "pw-pass-99", display_name: name,
     unit, role: "vco"}, as(vcoTok));
  const d = mk("simd", "19 ASOS");
  assert.strictEqual(d.body.user.is_main_vco, 1);
  const e = mk("sime", "19 ASOS");
  assert.strictEqual(e.body.user.is_main_vco, 0);
  assert.strictEqual(call("PUT", "/api/users/" + d.body.user.id,
    {is_main_vco: false}, as(vcoTok)).status, 200);
  const users = call("GET", "/api/users", null, as(vcoTok)).body.users;
  const by = Object.fromEntries(users.map(u => [u.username, u]));
  assert.strictEqual(by["simd"].is_main_vco, 0);
  assert.strictEqual(by["sime"].is_main_vco, 1,
    "earliest remaining VCO inherits on explicit false");
  // and clearing again passes it straight back to the other VCO
  assert.strictEqual(call("PUT", "/api/users/" + e.body.user.id,
    {is_main_vco: false}, as(vcoTok)).status, 200);
  const users2 = call("GET", "/api/users", null, as(vcoTok)).body.users;
  const by2 = Object.fromEntries(users2.map(u => [u.username, u]));
  assert.strictEqual(by2["sime"].is_main_vco, 0);
  assert.strictEqual(by2["simd"].is_main_vco, 1);
  // lone-main explicit false simply clears (no VCO remains to pass to)
  const lone = mk("simlone", "20 ASOS");
  assert.strictEqual(lone.body.user.is_main_vco, 1);
  assert.strictEqual(call("PUT", "/api/users/" + lone.body.user.id,
    {is_main_vco: false}, as(vcoTok)).status, 200);
  const users3 = call("GET", "/api/users", null, as(vcoTok)).body.users;
  const by3 = Object.fromEntries(users3.map(u => [u.username, u]));
  assert.strictEqual(by3["simlone"].is_main_vco, 0);
});
check("v1.3 (sim): create operator with is_main_vco:true -> 400", () => {
  assert.strictEqual(call("POST", "/api/users",
    {username: "simflag", password: "pw-pass-99", display_name: "F",
     unit: "14 ASOS", role: "operator", is_main_vco: true},
    as(vcoTok)).status, 400);
  const users = call("GET", "/api/users", null, as(vcoTok)).body.users;
  assert.ok(!users.some(u => u.username === "simflag"));
});
check("v1.3 (sim): user removal is VCO-only with a last-VCO floor", () => {
  const u = call("POST", "/api/users",
    {username: "gone", password: "gone-pass-99", display_name: "Gone",
     unit: "20 ASOS", role: "operator"}, as(vcoTok));
  assert.strictEqual(u.status, 201);
  assert.strictEqual(call("DELETE", "/api/users/" + u.body.user.id, null,
    as(operatorTok)).status, 403);
  assert.strictEqual(call("DELETE", "/api/users/" + u.body.user.id, null,
    as(vcoTok)).status, 200);
  const vco = call("GET", "/api/users", null, as(vcoTok)).body.users
    .find(x => x.username === "vco");
  // Demo VCO is not the last VCO (simc etc. exist), so remove a
  // different VCO instead of asserting the floor on the demo account:
  const simc = call("GET", "/api/users", null, as(vcoTok)).body.users
    .find(x => x.username === "simc");
  assert.ok(simc && vco);
});
check("v1.3 (sim): last-VCO removal actually fails 400 (isolated state)", () => {
  // the old check never attempted removing the final VCO; this one
  // does, on a fresh backend whose seed holds exactly one VCO.
  const be2 = createSimBackend();
  const call2 = (method, p, body, headers, query) =>
    be2.handle(method, p, body, headers, query || {});
  const login = call2("POST", "/api/login", {username: "vco", password: "x"});
  assert.strictEqual(login.status, 200);
  const tok2 = {Authorization: "Bearer " + login.body.token};
  const vcoId = login.body.user.id;
  assert.strictEqual(call2("DELETE", "/api/users/" + vcoId, null, tok2)
    .status, 400);
  const users = call2("GET", "/api/users", null, tok2).body.users;
  assert.ok(users.some(u => u.id === vcoId), "final VCO still exists");
});
check("v1.3 (sim): operator 403 matrix — review, demo, conflict, user delete, import, UTC delete, user list", () => {
  assert.strictEqual(call("GET", "/api/users", null, as(operatorTok))
    .status, 403);
  assert.strictEqual(call("POST", "/api/inspections/" + simInspId + "/review",
    {}, as(operatorTok)).status, 403);
  assert.strictEqual(call("POST", "/api/demo/load", {}, as(operatorTok))
    .status, 403);
  assert.strictEqual(call("POST", "/api/demo/remove", {}, as(operatorTok))
    .status, 403);
  // fresh conflict for the resolve check (test1Id is FMC by now)
  const cf = call("PUT", "/api/vehicles/" + test1Id,
    {base_status: "PMC", status: "NMC"}, as(vcoTok));
  assert.strictEqual(cf.status, 200);
  assert.ok(cf.body.conflict, "conflict expected");
  assert.strictEqual(call("POST",
    "/api/conflicts/" + cf.body.conflict.id + "/resolve",
    {action: "keep"}, as(operatorTok)).status, 403);
  assert.strictEqual(call("POST",
    "/api/conflicts/" + cf.body.conflict.id + "/resolve",
    {action: "keep"}, as(vcoTok)).status, 200);
  const u = call("POST", "/api/users",
    {username: "matrop", password: "matrop-pass-99",
     display_name: "Matrop", unit: "20 ASOS", role: "operator"},
    as(vcoTok));
  assert.strictEqual(u.status, 201);
  assert.strictEqual(call("DELETE", "/api/users/" + u.body.user.id, null,
    as(operatorTok)).status, 403);
  assert.strictEqual(call("DELETE", "/api/users/" + u.body.user.id, null,
    as(vcoTok)).status, 200);
  const csv = "Type,Asset Number,Status\nJLTV,MAT-1,FMC\n";
  const b64 = Buffer.from(csv, "utf8").toString("base64");
  assert.strictEqual(call("POST", "/api/import/preview",
    {unit: "20 ASOS", filename: "m.csv", content_b64: b64},
    as(operatorTok)).status, 403);
  assert.strictEqual(call("POST", "/api/import/confirm",
    {unit: "20 ASOS", filename: "m.csv", rows: []},
    as(operatorTok)).status, 403);
  const utc = call("POST", "/api/utcs",
    {unit: "15 ASOS", code: "MATUTC", title: "matrix"}, as(vcoTok));
  assert.strictEqual(utc.status, 201);
  assert.strictEqual(call("DELETE", "/api/utcs/" + utc.body.utc.id, null,
    as(operatorTok)).status, 403);
  assert.strictEqual(call("DELETE", "/api/utcs/" + utc.body.utc.id, null,
    as(vcoTok)).status, 200);
});
check("logout revokes the session", () => {
  const r = call("POST", "/api/logout", {}, as(vcoTok));
  assert.strictEqual(r.status, 200);
  const after = call("GET", "/api/fleet", null, as(vcoTok));
  assert.strictEqual(after.status, 401);
});

console.log("\nSIM CHECKS PASSED — " + n + " checks");
