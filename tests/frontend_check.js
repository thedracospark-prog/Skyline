#!/usr/bin/env node
/* Skyline frontend regression checks (review fix pass, items 11–14).
   Runs the REAL static/app.js logic (everything before the wiring
   section) in Node with minimal DOM/localStorage stubs, against a live
   server.py on a temp DB. Run:  node tests/frontend_check.js        */
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");

const ROOT = path.join(__dirname, "..");
const PORT = 18700 + Math.floor(Math.random() * 200);
const BASE = `http://127.0.0.1:${PORT}`;

const STUBS = `
const BASE = ${JSON.stringify(BASE)};
const _origFetch = globalThis.fetch;
globalThis.fetch = (url, opts) =>
  _origFetch(typeof url === "string" && url.startsWith("/") ? BASE + url : url, opts);
class LS { constructor(){ this.m = new Map(); }
  getItem(k){ return this.m.has(k) ? this.m.get(k) : null; }
  setItem(k, v){ this.m.set(k, String(v)); }
  removeItem(k){ this.m.delete(k); } }
const _ls = new LS();
_ls.setItem("skyline_token", __TOKEN__);
_ls.setItem("skyline_user", __USER__);
globalThis.localStorage = _ls;
const _els = new Map();
function makeEl(){ return { style:{}, dataset:{}, textContent:"", innerHTML:"",
  value:"", className:"",
  classList:{ _s:new Set(), add(c){ this._s.add(c); },
    remove(c){ this._s.delete(c); },
    toggle(c, f){ if (f === undefined) f = !this._s.has(c);
      f ? this._s.add(c) : this._s.delete(c); },
    contains(c){ return this._s.has(c); } },
  addEventListener(){}, querySelectorAll(){ return []; },
  querySelector(){ return makeEl(); }, appendChild(){} }; }
globalThis.document = {
  querySelector(sel){ if (!_els.has(sel)) _els.set(sel, makeEl());
    return _els.get(sel); },
  querySelectorAll(){ return []; },
  createElement(){ return makeEl(); } };
globalThis.window = { addEventListener(){}, scrollTo(){} };
globalThis.requestAnimationFrame = (cb) => { cb(performance.now() + 10000); return 0; };
const _nav = { onLine: true };
Object.defineProperty(globalThis, "navigator",
  { value: _nav, configurable: true, writable: true });
globalThis.alert = (m) => { console.log("ALERT:", m); };
`;

const DRIVER = `
;(async () => {
  const assert = (cond, msg) => {
    if (!cond) throw new Error("ASSERT FAILED: " + msg);
    console.log("ok - " + msg);
  };
  fleet = { vehicles: [], authorizations: [], units: [] };
  me = { display_name: "Ed Editor", username: "ed", unit: "15 ASOS",
         role: "editor" };

  /* ---- item 11: offline create + temp-id update drains cleanly ---- */
  _nav.onLine = false;
  const payload = { _tempId: "tmp-1", unit: "14 ASOS", bumper: "Q-1",
    type: "JLTV", status: "FMC", mileage: 10, location: "Pool",
    maint_due_date: "", maint_due_miles: null, notes: "" };
  await queueOrSend("create", payload, () => fleet.vehicles.push({
    id: "tmp-1", unit: "14 ASOS", bumper: "Q-1", type: "JLTV",
    status: "FMC", mileage: 10, location: "Pool", maint_due_date: "",
    maint_due_miles: null, notes: "", is_demo: 0,
    updated_by: "Ed Editor", updated_at: "" }));
  assert(queue.length === 1 && queue[0].opId, "offline create queued with op UUID");
  await queueOrSend("update",
    { _id: "tmp-1", base_status: "FMC", status: "NMC", notes: "deadlined" },
    () => { const v = fleet.vehicles[0]; v.status = "NMC"; v.notes = "deadlined"; });
  assert(queue.length === 2, "offline temp-id update queued behind create");
  _nav.onLine = true;
  await replayQueue();
  assert(queue.length === 0, "queue drained after replay (temp-id update did not wedge)");
  const snap = await (await _origFetch(BASE + "/api/fleet",
    { headers: { Authorization: "Bearer " + token } })).json();
  assert(snap.vehicles.filter(v => v.unit === "14 ASOS").length === 1,
         "server has exactly one 14 ASOS vehicle (create not duplicated)");
  assert(snap.vehicles[0].status === "NMC" && snap.vehicles[0].notes === "deadlined",
         "temp-id update landed on the real vehicle id");
  assert(String(fleet.vehicles[0].id) === String(snap.vehicles[0].id),
         "local snapshot id rewritten from temp to real");

  /* ---- item 11a: replaying the same create op is deduped server-side - */
  const dupOp = { opId: queue.length ? "x" : undefined, kind: "create",
    tempId: "tmp-dup", payload: { _tempId: "tmp-dup", unit: "14 ASOS",
    bumper: "Q-2", type: "JLTV", status: "FMC" } };
  // reuse the opId the first create carried by re-sending via execOp with
  // a fixed UUID twice:
  dupOp.opId = "fixed-uuid-for-dedup-test";
  fleet.vehicles.push({ id: "tmp-dup", unit: "14 ASOS", bumper: "Q-2",
    type: "JLTV", status: "FMC", is_demo: 0 });
  await execOp(dupOp);
  const tmpVehicle = fleet.vehicles.find(v => v.bumper === "Q-2");
  tmpVehicle.id = "tmp-dup";  // simulate a second replay attempt
  await execOp(dupOp);
  const snap2 = await (await _origFetch(BASE + "/api/fleet",
    { headers: { Authorization: "Bearer " + token } })).json();
  assert(snap2.vehicles.filter(v => v.bumper === "Q-2").length === 1,
         "same op UUID replayed twice creates exactly one vehicle");

  /* ---- item 12: client-derived stats mirror the server formula ------ */
  fleet.vehicles = [
    { unit: "14 ASOS", status: "FMC", is_demo: 0 },
    { unit: "14 ASOS", status: "PMC", is_demo: 0 },
    { unit: "14 ASOS", status: "NMC", is_demo: 1 },
    { unit: "15 ASOS", status: "PMC", is_demo: 0 },
    { unit: "15 ASOS", status: "FMC", is_demo: 0 },
  ];
  fleet.authorizations = [
    { unit: "14 ASOS", vehicle_type: "JLTV", authorized: 6, is_demo: 0 },
    { unit: "15 ASOS", vehicle_type: "JLTV", authorized: 4, is_demo: 0 },
  ];
  const g = unitStats("18 ASOG");
  assert(g.on_hand === 5 && g.fmc === 2 && g.pmc === 2 && g.nmc === 1,
         "group stats are squadron sums incl. PMC/NMC");
  assert(g.mc_rate === 40.0 && g.authorizations === 10,
         "group MC + authorizations derived from sums");
  assert(g.includes_demo === true, "group SAMPLE flag derived from demo vehicles");
  const s14 = unitStats("14 ASOS");
  assert(s14.mc_rate === 33.3, "unit MC = FMC / on-hand");

  /* ---- item 14: viz renders ALL vehicles at 150, on-canvas ----------- */
  const vehs = [];
  const sts = ["FMC", "PMC", "NMC"];
  for (let i = 0; i < 150; i++) vehs.push({
    id: i + 1, unit: "14 ASOS", bumper: "V-" + i,
    type: ["JLTV", "HMMWV", "LMTV", "Trailer"][i % 4],
    status: sts[i % 3], maint_due_date: "", is_demo: 0 });
  renderViz("14 ASOG", vehs);
  const html = document.querySelector("#viz-wrap").innerHTML;
  const n = (html.match(/class="bldg"/g) || []).length;
  assert(n === 150, "viz renders all 150 buildings (got " + n + ")");
  const W = Number((html.match(/viewBox="0 0 (\\d+) /) || [])[1]);
  let maxEnd = 0;
  for (const m of html.matchAll(/<rect class="body" x="([\\d.]+)"[^>]*?width="([\\d.]+)"/g))
    maxEnd = Math.max(maxEnd, parseFloat(m[1]) + parseFloat(m[2]));
  assert(maxEnd <= W, "no building off-canvas (max end " + maxEnd + " <= " + W + ")");
  assert(html.includes("150 vehicles"), "viz caption shows the vehicle count");
  assert(html.includes('tabindex="0"'), "buildings are keyboard-focusable");

  /* ---- v1.1: deriveStats parity vs the LIVE server snapshot ---------- */
  const snap3 = await (await _origFetch(BASE + "/api/fleet",
    { headers: { Authorization: "Bearer " + token } })).json();
  fleet.vehicles = snap3.vehicles;
  fleet.authorizations = snap3.authorizations;
  fleet.utcs = snap3.utcs;
  const cg = deriveStats()["18 ASOG"], sg = snap3.group;
  assert(cg.on_hand === sg.on_hand && cg.fmc === sg.fmc &&
         cg.pmc === sg.pmc && cg.nmc === sg.nmc &&
         cg.authorizations === sg.authorizations &&
         cg.mc_rate === sg.mc_rate,
         "client group stats == server group entry (live snapshot)");
  for (const su of snap3.units) {
    const co = ownStats(su.unit);
    assert(co.on_hand === su.on_hand && co.fmc === su.fmc &&
           co.pmc === su.pmc && co.nmc === su.nmc &&
           co.mc_rate === su.mc_rate,
           "client own stats == server unit entry for " + su.unit);
  }

  /* ---- v1.1: ASOG own fleet counts in own stats AND the group -------- */
  fleet.vehicles = [
    { id: 1, unit: "18 ASOG", bumper: "HQ-1", type: "JLTV", status: "FMC", is_demo: 0 },
    { id: 2, unit: "18 ASOG", bumper: "HQ-2", type: "JLTV", status: "PMC", is_demo: 0 },
    { id: 3, unit: "18 ASOG", bumper: "HQ-3", type: "HMMWV", status: "NMC", is_demo: 0 },
    { id: 4, unit: "14 ASOS", bumper: "A-1", type: "JLTV", status: "FMC", is_demo: 0 },
  ];
  fleet.authorizations = [
    { unit: "18 ASOG", vehicle_type: "JLTV", authorized: 5, is_demo: 0 },
    { unit: "14 ASOS", vehicle_type: "JLTV", authorized: 6, is_demo: 0 },
  ];
  const asogOwn = ownStats("18 ASOG");
  assert(asogOwn.on_hand === 3 && asogOwn.fmc === 1 && asogOwn.pmc === 1 &&
         asogOwn.nmc === 1 && asogOwn.authorizations === 5,
         "ASOG unit stats are its OWN fleet (v1.1)");
  const grp2 = unitStats("18 ASOG");
  assert(grp2.on_hand === 4 && grp2.fmc === 2 && grp2.pmc === 1 &&
         grp2.nmc === 1 && grp2.authorizations === 11,
         "group = all five units incl. ASOG's own, counted once");
  assert(unitVehicles("18 ASOG").length === 3,
         "ASOG unit vehicle list is its own fleet only");

  /* ---- v1.1: UTC viz renders the UTC's set (empty + large) ----------- */
  fleet.utcs = [{ id: 77, unit: "18 ASOG", code: "XFFC1", title: "Pkg",
                  vehicle_ids: [1, 3] }];
  const utcWrap = document.querySelector("#utc-viz-wrap");
  renderVizInto(utcWrap, utcVehicles(fleet.utcs[0]),
                "No vehicles assigned yet");
  let uhtml = utcWrap.innerHTML;
  assert((uhtml.match(/class="bldg"/g) || []).length === 2,
         "UTC viz renders exactly the UTC's vehicles");
  assert(uhtml.includes("2 vehicles"), "UTC viz caption counts its vehicles");
  renderVizInto(utcWrap, [], "No vehicles assigned yet — dark.");
  assert(utcWrap.innerHTML.includes("viz-empty") &&
         utcWrap.innerHTML.includes("No vehicles assigned yet"),
         "empty UTC renders the intentional empty state");
  const big = [];
  const sts3 = ["FMC", "PMC", "NMC"];
  for (let i = 0; i < 120; i++) big.push({
    id: 1000 + i, unit: "18 ASOG", bumper: "B-" + i, type: "JLTV",
    status: sts3[i % 3], maint_due_date: "", is_demo: 0 });
  renderVizInto(utcWrap, big);
  assert((utcWrap.innerHTML.match(/class="bldg"/g) || []).length === 120,
         "UTC viz renders all 120 buildings");

  /* ---- F2: MC-rate ties round half-up, client == server ------------- */
  const tieFleet = (fmc, total) => {
    const vs = [];
    for (let i = 0; i < total; i++) vs.push({ unit: "20 ASOS",
      status: i < fmc ? "FMC" : "NMC", is_demo: 0 });
    return vs;
  };
  fleet.authorizations = [];
  fleet.vehicles = tieFleet(1, 16);
  assert(ownStats("20 ASOS").mc_rate === 6.3,
         "client MC tie 1/16 rounds half-up to 6.3");
  fleet.vehicles = tieFleet(5, 16);
  assert(ownStats("20 ASOS").mc_rate === 31.3,
         "client MC tie 5/16 rounds half-up to 31.3");
  fleet.vehicles = tieFleet(1, 80);
  assert(ownStats("20 ASOS").mc_rate === 1.3,
         "client MC tie 1/80 rounds half-up to 1.3");
  // live parity on a real tie: the bootstrap seeded 19 ASOS with
  // 16 vehicles, exactly 1 FMC — server snapshot (half-up) must be 6.3
  // and the client derivation must match it exactly
  const snapT = await (await _origFetch(BASE + "/api/fleet",
    { headers: { Authorization: "Bearer " + token } })).json();
  const s19 = snapT.units.find(u => u.unit === "19 ASOS");
  assert(s19 && s19.on_hand === 16 && s19.mc_rate === 6.3,
         "server snapshot tie 1/16 is 6.3 (half-up, not banker's 6.2)");
  fleet.vehicles = snapT.vehicles; fleet.authorizations = snapT.authorizations;
  assert(ownStats("19 ASOS").mc_rate === s19.mc_rate,
         "client == server on a live tie fleet");

  /* ---- F1: queueOrSend reports accepted / queued / rejected --------- */
  _nav.onLine = true;
  const okRes = await queueOrSend("authorization",
    { unit: "14 ASOS", vehicle_type: "JLTV", authorized: 7 }, () => {});
  assert(okRes === "accepted",
         "queueOrSend resolves 'accepted' when the server takes the op");
  const badRes = await queueOrSend("authorization",
    { unit: "14 ASOS", vehicle_type: "JLTV", authorized: -3 }, () => {});
  assert(badRes === "rejected",
         "queueOrSend resolves 'rejected' on a permanent server refusal");
  _nav.onLine = false;
  const qRes = await queueOrSend("authorization",
    { unit: "14 ASOS", vehicle_type: "HMMWV", authorized: 2 }, () => {});
  assert(qRes === "queued", "queueOrSend resolves 'queued' while offline");
  queue = []; saveQueue();
  _nav.onLine = true;

  /* ---- v1.1.1: authorization Remove control + live delete ----------- */
  // seed a real authorization on the live server, then load it locally
  const [ac] = [await _origFetch(BASE + "/api/authorizations", {
    method: "POST",
    headers: { Authorization: "Bearer " + token,
               "Content-Type": "application/json" },
    body: JSON.stringify({ unit: "15 ASOS", vehicle_type: "LMTV",
                           authorized: 9 }) })];
  assert(ac.status === 200, "seed authorization created on live server");
  const snapA = await (await _origFetch(BASE + "/api/fleet",
    { headers: { Authorization: "Bearer " + token } })).json();
  fleet.vehicles = snapA.vehicles;
  fleet.authorizations = snapA.authorizations;
  const seeded = fleet.authorizations.find(a =>
    a.unit === "15 ASOS" && a.vehicle_type === "LMTV");
  assert(seeded && seeded.id > 0 && seeded.authorized === 9,
         "seeded authorization present locally with a real id");
  assert(ownStats("15 ASOS").authorizations === 9,
         "derived unit total includes the seeded authorization");
  currentUnit = "15 ASOS";
  me.role = "editor";
  renderAuthSection();
  const edHtml = document.querySelector("#unit-auth-list").innerHTML;
  assert(edHtml.includes('data-auth-remove="LMTV"'),
         "unit-screen Remove control renders for an editor");
  assert(edHtml.includes('data-auth-save="LMTV"'),
         "unit-screen Save control still renders for an editor");
  me.role = "operator";
  renderAuthSection();
  const vwHtml = document.querySelector("#unit-auth-list").innerHTML;
  assert(!vwHtml.includes("data-auth-remove"),
         "no Remove control for an operator");
  assert(!vwHtml.includes("data-auth-save"),
         "no Save control for an operator");
  me.role = "editor";
  // the real endpoint, then the handler's exact local-state step
  // (filter the row out of fleet.authorizations) must drop the totals
  const del = await _origFetch(BASE + "/api/authorizations/" + seeded.id, {
    method: "DELETE", headers: { Authorization: "Bearer " + token } });
  assert(del.status === 200, "live DELETE /api/authorizations/<id> is 200");
  fleet.authorizations = fleet.authorizations.filter(x => x.id !== seeded.id);
  assert(ownStats("15 ASOS").authorizations === 0,
         "derived unit total drops to 0 after the delete");
  assert(unitStats("18 ASOG").authorizations ===
         snapA.group.authorizations - 9,
         "derived group total drops by exactly the removed count");
  const snapB = await (await _origFetch(BASE + "/api/fleet",
    { headers: { Authorization: "Bearer " + token } })).json();
  assert(!snapB.authorizations.some(a => a.id === seeded.id),
         "server snapshot no longer contains the deleted row");

  /* ---- item 13: sync pill states are honest -------------------------- */
  const pill = document.querySelector("#sync-pill");
  queue = []; syncFailed = true; lastSync = "";
  _nav.onLine = true; renderSyncPill();
  assert(pill.textContent.includes("Can't reach server"),
         "failed refresh changes the pill (no silent 'Synced')");
  syncFailed = false; lastSync = new Date().toISOString(); renderSyncPill();
  assert(pill.textContent.includes("Synced") &&
         !pill.textContent.includes("Can't reach"),
         "synced pill (with last-sync time) shows after a successful sync");

  /* ---- v1.2: role gating, 1800 form, photo visibility lock --------- */
  me.role = "operator";
  assert(!canEdit() && !isVCO(), "operator cannot edit and is not VCO");
  me.role = "editor";
  assert(canEdit() && !isVCO(), "editor can edit but is not VCO");
  me.role = "vco";
  assert(canEdit() && isVCO(), "VCO can edit and passes the VCO gate");

  // new-inspection form: exactly the 35 official items
  renderNewInspection({ id: 1, bumper: "A-1", type: "JLTV",
    unit: "14 ASOS", mileage: 100 });
  const niHtml = document.querySelector("#new-inspection-body").innerHTML;
  assert((niHtml.match(/class="insp-row"/g) || []).length === 35,
         "new-inspection form renders 35 item rows");
  assert(niHtml.includes("VCO review queue"),
         "new-inspection form names the VCO review queue");

  // inspection list: Review button is VCO-only and only when submitted
  me.role = "vco";
  renderInspectionsList([
    { id: 2, bumper: "B-2", unit: "15 ASOS", status: "reviewed",
      month_year: "October 2026", operator: "Ed Editor",
      created_at: "2026-10-26T10:00:00+00:00", item_count: 4,
      fail_count: 1, reviewed_by: "Boss" },
    { id: 1, bumper: "A-1", unit: "14 ASOS", status: "submitted",
      month_year: "October 2026", operator: "Vic Operator",
      created_at: "2026-10-26T09:00:00+00:00", item_count: 4,
      fail_count: 2 }]);
  const ilHtml = document.querySelector("#inspection-list").innerHTML;
  assert(ilHtml.includes('data-insp-review="1"'),
         "Review button renders for a VCO on a submitted inspection");
  assert(!ilHtml.includes('data-insp-review="2"'),
         "no Review button on an already-reviewed inspection");
  me.role = "operator";
  renderInspectionsList([
    { id: 1, bumper: "A-1", unit: "14 ASOS", status: "submitted",
      month_year: "October 2026", operator: "Vic Operator",
      created_at: "2026-10-26T09:00:00+00:00", item_count: 4,
      fail_count: 2 }]);
  assert(!document.querySelector("#inspection-list").innerHTML
           .includes("data-insp-review"),
         "no Review button for an operator");

  // printable form: items/comments/discrepancies + photo thumbs, escaped
  const full = { inspection: { id: 1, vehicle_id: 7, unit: "14 ASOS",
      bumper: "A-1", operator: "Vic Operator", month_year: "October 2026",
      mileage_hours: 5000, tire_front: "80", tire_rear: "80",
      status: "submitted", created_at: "2026-10-26T09:00:00+00:00",
      reviewed_by: "", reviewed_at: "" },
    items: [{ item_no: 1, item_text: INSPECTION_ITEMS[0], applies: true,
              result: "pass", comment: "" },
            { item_no: 2, item_text: INSPECTION_ITEMS[1], applies: true,
              result: "fail", comment: "<script>alert(1)</script> leak" }],
    discrepancies: [{ id: 9, inspection_id: 1, item_no: 2,
      description: "oil leak <b>at filter</b>", date_found: "2026-10-26",
      miles_hours: 5000, status: "open",
      photos: [{ id: 3, url: "/api/inspections/1/discrepancies/2/photos/3" }] }] };
  const formHtml = inspectionFormHTML(full,
    { vehicle: "data:image/jpeg;base64,AAA",
      byDiscrepancy: { 9: ["data:image/jpeg;base64,BBB"] } });
  assert(formHtml.includes("AF Form 1800") && formHtml.includes("A-1") &&
         formHtml.includes("oil leak") &&
         formHtml.includes("data:image/jpeg;base64,AAA") &&
         formHtml.includes("data:image/jpeg;base64,BBB"),
         "printable form renders header, items, discrepancies, both photo thumbs");
  assert(!formHtml.includes("<script>alert(1)</script>") &&
         !formHtml.includes("<b>at filter</b>"),
         "printable form escapes user text");

  // v1.2 fix pass: discrepancy photo tools render a remove control
  // per existing photo (plus the attach control)
  renderDiscPhotoTools(full);
  const toolsHtml = document.querySelector("#inspection-photo-tools").innerHTML;
  assert(toolsHtml.includes('data-disc-remove="3"') &&
         toolsHtml.includes('data-disc-id="9"'),
         "discrepancy photo tools render a remove control per photo");
  assert(toolsHtml.includes('data-disc-upload="9"'),
         "discrepancy photo tools keep the attach control");

  // photo visibility lock: list rows + tiles never render <img>, even
  // when the vehicle carries a photo (detail view only)
  fleet.vehicles = [{ id: 1, unit: "14 ASOS", bumper: "A-1", type: "JLTV",
    status: "FMC", mileage: 100, location: "Pool", maint_due_date: "",
    maint_due_miles: null, notes: "", is_demo: 0, photo: 1,
    updated_by: "Ed Editor", updated_at: "" }];
  fleet.authorizations = [];
  currentUnit = "14 ASOS"; statusFilter = "ALL";
  document.querySelector("#veh-search").value = "";
  renderVehicleList();
  const listHtml = document.querySelector("#vehicle-list").innerHTML;
  assert(listHtml.includes("A-1") && !listHtml.includes("<img"),
         "vehicle list row shows the bumper but no photo (detail view only)");
  const tile = tileHTML({ unit: "14 ASOS", kind: "sq", on_hand: 1,
    authorizations: 0, mc_rate: 100, fmc: 1, pmc: 0, nmc: 0,
    includes_demo: false }, "sq");
  assert(!tile.includes("<img"), "home tile renders no photo");

  console.log("FRONTEND CHECKS PASSED");
})().catch((e) => { console.error(e); process.exit(1); });
`;

async function waitUp(tries = 60) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(BASE + "/api/setup/status");
      if (r.ok) return;
    } catch (e) { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error("server did not come up");
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "skyline-fe-"));
  const db = path.join(tmp, "fe.db");
  const srv = spawn("python3", [path.join(ROOT, "server.py"),
    "--port", String(PORT), "--db", db], { stdio: "ignore" });
  try {
    await waitUp();
    const j = async (method, p, body, tok) => {
      const r = await fetch(BASE + p, { method,
        headers: { "Content-Type": "application/json",
                   ...(tok ? { Authorization: "Bearer " + tok } : {}) },
        body: body ? JSON.stringify(body) : undefined });
      return [r.status, await r.json().catch(() => ({}))];
    };
    const [, setup] = await j("POST", "/api/setup",
      { username: "boss", password: "vco-pass-1", display_name: "Boss",
        unit: "18 ASOG" });
    const vco = setup.token;
    await j("POST", "/api/users",
      { username: "ed", password: "editor-pass-1", display_name: "Ed Editor",
        unit: "15 ASOS", role: "editor" }, vco);
    const [, login] = await j("POST", "/api/login",
      { username: "ed", password: "editor-pass-1" });
    // seed a live MC-rate TIE for the F2 parity check: 19 ASOS gets
    // 16 vehicles, exactly 1 FMC (1/16 = 6.25% — banker's would say
    // 6.2, half-up says 6.3; server and client must agree on 6.3)
    for (let i = 0; i < 16; i++) {
      const [vc] = await j("POST", "/api/vehicles",
        { unit: "19 ASOS", bumper: "TIE-" + i, type: "JLTV",
          status: i === 0 ? "FMC" : "NMC" }, vco);
      if (vc !== 201) throw new Error("tie seed failed: HTTP " + vc);
    }
    const user = { id: 2, username: "ed", display_name: "Ed Editor",
                   unit: "15 ASOS", role: "editor" };

    let src = fs.readFileSync(path.join(ROOT, "static", "app.js"), "utf8");
    const cut = src.indexOf("/* ============================================================ wiring */");
    if (cut < 0) throw new Error("wiring marker not found in app.js");
    src = src.slice(0, cut);
    const combined = STUBS
      .replace("__TOKEN__", JSON.stringify(login.token))
      .replace("__USER__", JSON.stringify(JSON.stringify(user)))
      + "\n" + src + "\n" + DRIVER;
    const file = path.join(tmp, "combined.js");
    fs.writeFileSync(file, combined);
    const child = spawn("node", [file], { stdio: "inherit" });
    const code = await new Promise((res) => child.on("exit", res));
    process.exitCode = code;
  } finally {
    srv.kill();
  }
})();
