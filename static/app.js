/* Skyline — vanilla JS frontend. No build step, no libraries. */
"use strict";
const $ = (s, el=document) => el.querySelector(s);
const $$ = (s, el=document) => [...el.querySelectorAll(s)];
const UNITS = ["18 ASOG", "14 ASOS", "15 ASOS", "19 ASOS", "20 ASOS"];
const SQUADRONS = UNITS.slice(1);

let token = localStorage.getItem("skyline_token") || "";
let me = JSON.parse(localStorage.getItem("skyline_user") || "null");
let fleet = JSON.parse(localStorage.getItem("skyline_snapshot") || "null");
let queue = JSON.parse(localStorage.getItem("skyline_queue") || "[]");
let currentUnit = null, currentVehicle = null, statusFilter = "ALL";
let currentUtcId = null, importPreview = null;
let conflicts = [];
let lastSync = localStorage.getItem("skyline_last_sync") || "";
let syncFailed = false;      // last refresh attempt failed / was rejected
let conflictsError = "";     // last conflicts-load error (shown, not silent)
let replaying = false;       // replayQueue re-entrancy guard

function showNotice(msg, isError = true) {
  const n = $("#notice");
  if (!n) { if (isError) alert(msg); return; }
  n.textContent = msg;
  n.className = "notice " + (isError ? "err" : "ok");
  n.classList.remove("hidden");
  clearTimeout(showNotice._t);
  showNotice._t = setTimeout(() => n.classList.add("hidden"), 8000);
}

const canEdit = () => me && (me.role === "editor" || me.role === "vco");
const isVCO = () => me && me.role === "vco";

/* DAF Form 1800 — the official 35 checklist items, verbatim (mirrors
   server.py's INSPECTION_ITEMS; the server stores its own fixed text,
   so a drift here only affects the form's labels, never the record). */
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

/* ============================================================ API layer */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: {"Content-Type": "application/json",
              ...(token ? {Authorization: "Bearer " + token} : {}),
              ...(opts.headers || {})},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* csv etc. */ }
  if (!res.ok) throw Object.assign(new Error(data.error || res.statusText),
                                   {status: res.status, data});
  return data;
}

function saveQueue() {
  localStorage.setItem("skyline_queue", JSON.stringify(queue));
  renderSyncPill();
}
function saveSnapshot() {
  localStorage.setItem("skyline_snapshot", JSON.stringify(fleet));
}

function renderSyncPill() {
  const pill = $("#sync-pill");
  if (!pill) return;
  const when = lastSync ? " · last sync " + fmtTime(lastSync) : "";
  if (!navigator.onLine) {
    pill.className = "pill off";
    pill.textContent = `● Offline (cached)${queue.length ? ` — ${queue.length} queued` : ""}${when}`;
  } else if (replaying || queue.length) {
    pill.className = "pill warn";
    pill.textContent = `● Syncing${queue.length ? ` — ${queue.length} queued` : ""}`;
  } else if (syncFailed) {
    pill.className = "pill off";
    pill.textContent = `● Can't reach server — showing cached data${when}`;
  } else if (lastSync) {
    pill.className = "pill ok";
    pill.textContent = "● Synced " + fmtTime(lastSync);
  } else {
    pill.className = "pill warn";
    pill.textContent = "● Not synced yet";
  }
}
window.addEventListener("online", () => { renderSyncPill(); replayQueue(); });
window.addEventListener("offline", renderSyncPill);

/* ============================================================ offline queue
   Edits made without connectivity are applied to the local snapshot at
   once and queued. Every op carries a client op UUID; vehicle creates
   send it to the server, which dedups on it (idempotent replay). On
   reconnect ops replay in order; when a queued create lands, later ops
   that referenced its temp id are rewritten to the real id BEFORE they
   replay. Permanent 4xx rejections are dropped with a visible notice —
   never retried forever, never silent. Only transient failures
   (network / 5xx) stay queued.                                            */
function newOpId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return "op-" + Date.now() + "-" + Math.random().toString(36).slice(2, 10);
}
function makeOp(kind, payload) {
  return {opId: newOpId(), kind, payload,
          tempId: payload._tempId, id: payload._id};
}
const isPermanent = (e) => e && e.status >= 400 && e.status < 500;

async function execOp(op) {
  if (op.kind === "create") {
    const body = {...op.payload, op_id: op.opId};
    const r = await api("/api/vehicles", {method: "POST", body});
    const v = fleet.vehicles.find(v => String(v.id) === String(op.tempId));
    if (v) Object.assign(v, r.vehicle);
    return r.vehicle;
  }
  if (op.kind === "update") {
    const r = await api("/api/vehicles/" + op.id, {method: "PUT", body: op.payload});
    if (r.vehicle) {
      const v = fleet.vehicles.find(v => String(v.id) === String(r.vehicle.id));
      if (v) Object.assign(v, r.vehicle);
    }
    return r.vehicle;
  }
  if (op.kind === "authorization") {
    await api("/api/authorizations", {method: "POST", body: op.payload});
    return null;
  }
}

async function replayQueue() {
  if (replaying) return;                        // re-entrancy guard
  if (!queue.length || !navigator.onLine || !token) return;
  replaying = true;
  renderSyncPill();
  const dropped = [];
  try {
    while (queue.length) {
      const op = queue[0];
      try {
        const vehicle = await execOp(op);
        queue.shift();
        if (op.kind === "create" && vehicle) {
          // rewrite later ops that pointed at the temp id to the real id
          for (const later of queue) {
            if (String(later.id) === String(op.tempId)) later.id = vehicle.id;
            if (later.payload && String(later.payload._id) === String(op.tempId))
              later.payload._id = vehicle.id;
          }
          saveSnapshot();
        }
        saveQueue();                             // persist after every mutation
      } catch (e) {
        if (isPermanent(e)) {
          queue.shift();                         // drop + report, never wedge
          dropped.push(e.message || ("HTTP " + e.status));
          saveQueue();
        } else {
          break;                                 // transient: retry next time
        }
      }
    }
  } finally {
    replaying = false;
  }
  saveQueue(); saveSnapshot();
  if (dropped.length) {
    showNotice("Some offline edits were rejected by the server and dropped: " +
               dropped.join("; "));
    await refreshFleet(false);                   // resync to server truth
  } else if (!queue.length) {
    await refreshFleet(false);
  }
  if (me) render();                              // tiles/chips/viz/list agree
}

/* Send an op now (online) or queue it (offline / transient failure).
   Resolves to "accepted" (the server took it), "queued" (stored for
   the next replay), or "rejected" (permanent 4xx — the rejection
   notice has already been shown and the snapshot resynced to the
   server's truth). Callers that announce success MUST check this:
   announcing "saved" for a rejected op would be a lie (fix F1). */
async function queueOrSend(kind, payload, applyLocal) {
  const op = makeOp(kind, payload);
  applyLocal();            // optimistic local update, instant UI
  saveSnapshot(); render();
  if (navigator.onLine) {
    try {
      await sendOp(op);
      return "accepted";
    } catch (e) {
      if (isPermanent(e)) {
        showNotice("Server rejected that change: " + (e.message || e.status));
        await refreshFleet(false); if (me) render();
        return "rejected";
      }
      queue.push(op); saveQueue(); renderSyncPill();
      return "queued";
    }
  }
  queue.push(op);
  saveQueue();
  return "queued";
}
async function sendOp(op) {
  await execOp(op);
  saveSnapshot();
  await refreshFleet(false);
  if (me) render();
}

/* ============================================================ data */
async function refreshFleet(rerender = true) {
  try {
    fleet = await api("/api/fleet");
    lastSync = new Date().toISOString();
    localStorage.setItem("skyline_last_sync", lastSync);
    syncFailed = false;
    saveSnapshot();
    loadConflicts();
    loadInspectionBadge();
    if (rerender) render();
    renderSyncPill();
  } catch (e) {
    // failed or 401 refresh: pill must say so, cached data marked stale
    syncFailed = true;
    renderSyncPill();
    if (!fleet) throw e; // no cache and no network — nothing to show
    if (rerender) render();
  }
}
async function loadConflicts() {
  try {
    const r = await api("/api/conflicts");
    conflicts = r.conflicts;
    conflictsError = "";
    const b = $("#conflict-badge");
    b.textContent = conflicts.length;
    b.classList.toggle("hidden", !conflicts.length);
  } catch (e) {
    conflictsError = e.message || "couldn't load conflicts";
  }
}

/* Stats are DERIVED client-side from the vehicle list on every call —
   the same formula the server uses — so tiles, chips, the viz and the
   list can never contradict each other, online or off.
   v1.1 semantics: EVERY unit (18 ASOG included) has its OWN stats from
   its own vehicles/authorizations; the GROUP stats for 18 ASOG are the
   sums over ALL FIVE units (ASOG's own included, counted once). */
function ownStats(name) {
  const vehs = fleet.vehicles.filter(v => v.unit === name);
  const fmc = vehs.filter(v => v.status === "FMC").length;
  const auth = (fleet.authorizations || [])
    .filter(a => a.unit === name)
    .reduce((s, a) => s + (Number(a.authorized) || 0), 0);
  return {
    unit: name, kind: name === "18 ASOG" ? "group" : "squadron",
    on_hand: vehs.length, authorizations: auth, fmc,
    pmc: vehs.filter(v => v.status === "PMC").length,
    nmc: vehs.filter(v => v.status === "NMC").length,
    mc_rate: vehs.length ? Math.round(fmc / vehs.length * 1000) / 10 : 0.0,
    includes_demo: vehs.some(v => v.is_demo),
  };
}
function groupStats() {
  const g = {unit: "18 ASOG", kind: "group", on_hand: 0, authorizations: 0,
             fmc: 0, pmc: 0, nmc: 0, mc_rate: 0.0, includes_demo: false};
  for (const name of UNITS) {
    const st = ownStats(name);
    for (const k of ["on_hand", "authorizations", "fmc", "pmc", "nmc"])
      g[k] += st[k];
    g.includes_demo = g.includes_demo || st.includes_demo;
  }
  g.mc_rate = g.on_hand ? Math.round(g.fmc / g.on_hand * 1000) / 10 : 0.0;
  return g;
}
function deriveStats() {
  const per = {};
  for (const name of UNITS) per[name] = ownStats(name);
  per["18 ASOG"] = groupStats();   // the ASOG entry is the group picture
  return per;
}
function unitStats(name) {
  return deriveStats()[name];
}
function unitVehicles(name) {
  return fleet.vehicles.filter(v => v.unit === name);
}
function unitUtcs(name) {
  return (fleet.utcs || []).filter(u => u.unit === name);
}
function utcVehicles(utc) {
  const byId = new Map(fleet.vehicles.map(v => [v.id, v]));
  return (utc.vehicle_ids || []).map(id => byId.get(id)).filter(Boolean);
}
function statusCounts(vehs) {
  return {total: vehs.length,
          fmc: vehs.filter(v => v.status === "FMC").length,
          pmc: vehs.filter(v => v.status === "PMC").length,
          nmc: vehs.filter(v => v.status === "NMC").length};
}
function mcClass(rate, onHand) {
  if (!onHand) return "";
  return rate >= 90 ? "good" : rate >= 75 ? "mid" : "bad";
}

/* ============================================================ views */
let currentViewId = null;
function showView(id) {
  // leaving a photo view discards its images: revoke their blob URLs
  // so SPA navigation does not leak one per visit (v1.2 fix pass).
  // Same-view calls (re-renders) do NOT revoke — the photos are
  // still on screen.
  if (currentViewId === "view-inspection-form" &&
      id !== "view-inspection-form") {
    revokePhotoUrls(currentInspectionPhotoUrls);
    currentInspectionPhotoUrls = null;
  }
  if (currentViewId === "view-vehicle-detail" &&
      id !== "view-vehicle-detail") {
    revokeBlobUrl(currentVehiclePhotoUrl);
    currentVehiclePhotoUrl = null;
  }
  currentViewId = id;
  $$("main .view, #view-auth").forEach(v => v.classList.add("hidden"));
  $("#app").classList.toggle("hidden", id === "view-auth");
  const el = $("#" + id);
  el.classList.remove("hidden");
  el.style.animation = "none"; void el.offsetWidth; el.style.animation = "";
  window.scrollTo({top: 0});
}
function render() {
  if (!me) return showView("view-auth");
  $("#user-chip").innerHTML =
    `<b>${esc(me.display_name)}</b> · ${esc(me.unit || "—")} · ${esc(me.role)}`;
  $("#nav-vco").classList.toggle("hidden", !isVCO());
  $$(".editor-only").forEach(b => b.classList.toggle("hidden", !canEdit()));
  renderTiles();
  if (currentUtcId && !$("#view-utc").classList.contains("hidden")) renderUtc();
  else if (currentUnit) renderUnit();
  renderSyncPill();
}

/* ============================================================ home tiles */
const prevMc = {};  // unit -> last rendered MC rate (animate only on change)
function countUp(el, to) {
  const t0 = performance.now(), dur = 280;   // ~250–300 ms sweep
  (function step(t) {
    const p = Math.min(1, (t - t0) / dur);
    el.textContent = (to * (1 - Math.pow(1 - p, 3))).toFixed(1) + "%";
    if (p < 1) requestAnimationFrame(step);
  })(t0);
}
function tileHTML(st, cls) {
  const mc = st.on_hand ? null : "—";
  return `<button class="tile ${cls}" data-unit="${st.unit}">
    <div class="tname">${st.unit} ${st.includes_demo ? '<span class="sampletag">incl. SAMPLE data</span>' : ""}</div>
    <div class="tsub">${st.kind === "group" ? "Group rollup — all five units, incl. 18 ASOG's own fleet" : "Air Support Operations Squadron"}</div>
    <div class="stats">
      <div class="stat"><div class="num">${st.on_hand}</div><div class="lbl">On hand</div></div>
      <div class="stat"><div class="num">${st.authorizations}</div><div class="lbl">Authorizations</div></div>
      <div class="stat"><div class="num mc ${mcClass(st.mc_rate, st.on_hand)}" data-mc="${st.mc_rate}" data-unit-mc="${st.unit}">${mc ?? "0%"}</div><div class="lbl">MC rate</div></div>
    </div>
    <div class="mcbar"><div data-bar="${st.mc_rate}"></div></div>
    <div class="counts-line">FMC <b>${st.fmc}</b> · PMC <b>${st.pmc}</b> · NMC <b>${st.nmc}</b></div>
  </button>`;
}
function renderTiles() {
  const wrap = $("#tiles");
  const stats = deriveStats();
  const group = stats["18 ASOG"];
  const sqs = SQUADRONS.map(n => stats[n]);
  wrap.innerHTML = tileHTML(group, "") +
    `<div class="tile-grid">${sqs.map(s => tileHTML(s, "sq")).join("")}</div>`;
  requestAnimationFrame(() => {
    $$("[data-bar]", wrap).forEach(b => b.style.width = b.dataset.bar + "%");
    $$("[data-mc]", wrap).forEach(el => {
      const to = parseFloat(el.dataset.mc);
      const unit = el.dataset.unitMc;
      if (!unitStats(unit).on_hand) { el.textContent = "—"; return; }
      if (prevMc[unit] === to) { el.textContent = to.toFixed(1) + "%"; return; }
      prevMc[unit] = to;
      countUp(el, to);
    });
  });
  $$(".tile", wrap).forEach(t =>
    t.addEventListener("click", () => openUnit(t.dataset.unit)));
}

/* ============================================================ unit screen */
function openUnit(name) {
  currentUnit = name;
  statusFilter = "ALL";   // reset the status filter when switching units
  $$("#status-filter button").forEach(o =>
    o.classList.toggle("on", o.dataset.f === "ALL"));
  showView("view-unit");
  renderUnit();
}
function renderUnit() {
  const name = currentUnit;
  const st = ownStats(name);           // this unit's OWN fleet (v1.1)
  const vehs = unitVehicles(name);
  $("#unit-title").textContent = name === "18 ASOG"
    ? "18 ASOG — its own fleet" : name;
  $("#unit-stats").innerHTML = `
    <div class="chip">On hand <b>${st.on_hand}</b></div>
    <div class="chip">Authorizations <b>${st.authorizations}</b></div>
    <div class="chip">MC rate <b class="mc ${mcClass(st.mc_rate, st.on_hand)}">${st.on_hand ? st.mc_rate + "%" : "—"}</b></div>
    <div class="chip">FMC <b>${st.fmc}</b> · PMC <b>${st.pmc}</b> · NMC <b>${st.nmc}</b></div>
    ${st.includes_demo ? '<div class="chip"><span class="sampletag">incl. SAMPLE data</span></div>' : ""}`;
  // 18 ASOG additionally shows the GROUP rollup (all five units)
  const gs = $("#unit-group-summary");
  if (name === "18 ASOG") {
    const g = groupStats();
    gs.classList.remove("hidden");
    gs.innerHTML = `<h3>Group rollup — all five units (incl. 18 ASOG's own)</h3>
      <div class="statrow">
        <div class="chip">Group on hand <b>${g.on_hand}</b></div>
        <div class="chip">Group authorizations <b>${g.authorizations}</b></div>
        <div class="chip">Group MC rate <b class="mc ${mcClass(g.mc_rate, g.on_hand)}">${g.on_hand ? g.mc_rate + "%" : "—"}</b></div>
        <div class="chip">FMC <b>${g.fmc}</b> · PMC <b>${g.pmc}</b> · NMC <b>${g.nmc}</b></div>
        ${g.includes_demo ? '<div class="chip"><span class="sampletag">incl. SAMPLE data</span></div>' : ""}
      </div>`;
  } else {
    gs.classList.add("hidden");
    gs.innerHTML = "";
  }
  renderViz(name, vehs);
  // type datalist
  const types = [...new Set(fleet.vehicles.map(v => v.type))].sort();
  $("#type-list").innerHTML = types.map(t => `<option value="${esc(t)}">`).join("");
  renderVehicleList();
  renderAuthSection();
  renderUtcSection();
}

/* ------------------------------------------- per-unit authorizations */
function renderAuthSection() {
  const wrap = $("#unit-auth-list");
  if (!wrap) return;
  const name = currentUnit;
  const editable = canEdit();
  const types = new Set();
  (fleet.authorizations || []).filter(a => a.unit === name)
    .forEach(a => types.add(a.vehicle_type));
  unitVehicles(name).forEach(v => types.add(v.type));
  const rows = [...types].sort().map(t => {
    const a = (fleet.authorizations || []).find(a =>
      a.unit === name && a.vehicle_type === t);
    const oh = unitVehicles(name).filter(v => v.type === t).length;
    return {type: t, authorized: a ? a.authorized : 0, on_hand: oh,
            is_demo: a ? !!a.is_demo : false, id: a ? a.id : null};
  });
  wrap.innerHTML = (rows.length ? `<table>
    <tr><th>Type</th><th>Authorized</th><th>On hand</th>${editable ? "<th></th><th></th>" : ""}</tr>
    ${rows.map(r => `<tr>
      <td><b>${esc(r.type)}</b>${r.is_demo ? ' <span class="sampletag">SAMPLE</span>' : ""}</td>
      <td>${editable
        ? `<input type="number" min="0" class="auth-num" aria-label="Authorized count for ${esc(r.type)}"
             data-auth-type="${esc(r.type)}" value="${esc(r.authorized)}">`
        : `<b>${esc(r.authorized)}</b>`}</td>
      <td>${r.on_hand}</td>
      ${editable ? `<td><button class="btn" data-auth-save="${esc(r.type)}">Save</button></td>
      <td>${r.id ? `<button class="btn danger" data-auth-remove="${esc(r.type)}">Remove</button>` : ""}</td>` : ""}
    </tr>`).join("")}</table>`
    : '<p class="hint">No authorizations yet for this unit.</p>');
  if (!editable) return;
  $$("[data-auth-save]", wrap).forEach(b => b.addEventListener("click", async () => {
    const type = b.dataset.authSave;
    const input = $$(".auth-num", wrap)
      .find(i => i.dataset.authType === type);
    const n = Number(input ? input.value : "");
    if (!Number.isInteger(n) || n < 0) {
      showNotice("Authorized count must be a whole number, 0 or more.");
      return;
    }
    const payload = {unit: name, vehicle_type: type, authorized: n};
    const result = await queueOrSend("authorization", payload, () => {
      const ex = fleet.authorizations.find(a =>
        a.unit === name && a.vehicle_type === type);
      if (ex) { ex.authorized = n; ex.is_demo = 0; }
      else fleet.authorizations.push({id: 0, is_demo: 0, ...payload});
    });
    // only claim success when the op was accepted or queued — on a
    // rejection queueOrSend already showed the server's answer, and
    // overwriting it with "saved" would lie about the data (fix F1)
    if (result === "accepted")
      showNotice(`Authorization saved: ${name} ${type} = ${n}.`, false);
    else if (result === "queued")
      showNotice(`Authorization queued — will sync when online: ${name} ${type} = ${n}.`, false);
  }));
  // Remove = delete the authorization row entirely (v1.1.1). Online-
  // only like vehicle delete: never queued, failures say so plainly.
  $$("[data-auth-remove]", wrap).forEach(b => b.addEventListener("click", async () => {
    const type = b.dataset.authRemove;
    const a = (fleet.authorizations || []).find(x =>
      x.unit === name && x.vehicle_type === type);
    if (!a || !a.id) return;
    if (!confirm(`Remove the authorization for ${name} / ${type} (currently ${a.authorized})?\n\n` +
        `This deletes the authorization line entirely. The unit's ${type} vehicles are NOT affected.\n` +
        `Softer option: set the number to 0 and Save — the line stays, at zero.`)) return;
    try {
      await api("/api/authorizations/" + a.id, {method: "DELETE"});
    } catch (e) {
      showNotice("Remove failed (needs connection): " + (e.message || e.status));
      return;
    }
    fleet.authorizations = fleet.authorizations.filter(x => x.id !== a.id);
    render();
    showNotice(`Authorization removed: ${name} ${type}.`, false);
  }));
}

/* ------------------------------------------------------------ UTCs */
function renderUtcSection() {
  const wrap = $("#utc-list");
  if (!wrap) return;
  const utcs = unitUtcs(currentUnit);
  if (!utcs.length) {
    wrap.innerHTML = '<p class="hint">No UTCs yet for this unit. ' +
      (canEdit() ? "Build one with “New UTC”." : "") + "</p>";
    return;
  }
  wrap.innerHTML = `<div class="utc-grid">${utcs.map(u => {
    const vehs = utcVehicles(u);
    const c = statusCounts(vehs);
    const mc = c.total ? Math.round(c.fmc / c.total * 1000) / 10 : null;
    return `<button class="utc-card" data-utc="${u.id}">
      <div class="ucode">${esc(u.code)}</div>
      <div class="utitle">${esc(u.title || "")}</div>
      <div class="ucounts">Assigned <b>${c.total}</b> · FMC <b>${c.fmc}</b> · PMC <b>${c.pmc}</b> · NMC <b>${c.nmc}</b></div>
      <div class="mcbar"><div style="width:${mc ?? 0}%"></div></div>
      <div class="ucounts">MC rate <b class="mc ${mcClass(mc ?? 0, c.total)}">${mc === null ? "—" : mc + "%"}</b></div>
    </button>`;
  }).join("")}</div>`;
  $$("[data-utc]", wrap).forEach(b => b.addEventListener("click", () =>
    openUtc(Number(b.dataset.utc))));
}

function dueInfo(v) {
  if (!v.maint_due_date) return null;
  const days = Math.ceil((new Date(v.maint_due_date + "T12:00:00") - Date.now()) / 864e5);
  return {days, overdue: days < 0, soon: days <= 30};
}

function renderVehicleList() {
  const q = ($("#veh-search").value || "").toLowerCase();
  let vehs = unitVehicles(currentUnit);
  if (currentUnit === "18 ASOG" && statusFilter === "ALL" && !q) { /* all */ }
  vehs = vehs.filter(v =>
    (statusFilter === "ALL" || v.status === statusFilter) &&
    (!q || [v.bumper, v.type, v.location, v.unit, v.notes]
      .join(" ").toLowerCase().includes(q)));
  const list = $("#vehicle-list");
  if (!vehs.length) {
    list.innerHTML = `<div class="card hint">No vehicles match. ` +
      (fleet.vehicles.length ? "" : "The fleet is empty — a VCO can load the demo fleet from the VCO screen.") + `</div>`;
    return;
  }
  list.innerHTML = vehs.map(v => {
    const d = dueInfo(v);
    return `<button class="vcard ${v.status}" data-vid="${v.id}">
      <div class="top"><span class="bumper">${esc(v.bumper)} ${v.is_demo ? '<span class="sampletag">SAMPLE</span>' : ""}</span>
      <span class="st ${v.status}">${v.status}</span></div>
      <div class="meta">${esc(v.type)} · ${esc(v.unit)} · ${esc(v.location || "—")} ·
        ${esc(v.mileage ?? "—")} mi/hrs
        ${d ? ` · maint due ${esc(v.maint_due_date)} ${d.overdue ? '<span class="duetag overdue">(OVERDUE)</span>' : d.soon ? '<span class="duetag">(soon)</span>' : ""}` : ""}
        <br>Updated by ${esc(v.updated_by || "—")}${v.updated_at ? " · " + fmtTime(v.updated_at) : ""}</div>
    </button>`;
  }).join("");
  $$(".vcard", list).forEach(c => c.addEventListener("click", () => {
    const v = fleet.vehicles.find(x => String(x.id) === c.dataset.vid);
    openVehicle(v);
  }));
}

/* ============================================================ UTC detail */
function openUtc(id) {
  currentUtcId = id;
  showView("view-utc");
  renderUtc();
}
function currentUtc() {
  return (fleet.utcs || []).find(u => u.id === currentUtcId) || null;
}
function renderUtc() {
  const u = currentUtc();
  if (!u) { if (currentUnit) openUnit(currentUnit); return; }
  const editable = canEdit();
  const vehs = utcVehicles(u);
  const c = statusCounts(vehs);
  const mc = c.total ? Math.round(c.fmc / c.total * 1000) / 10 : null;
  $("#utc-title").textContent = `${u.code} — ${u.title || "UTC"} · ${u.unit}`;
  $("#utc-stats").innerHTML = `
    <div class="chip">Assigned <b>${c.total}</b></div>
    <div class="chip">FMC <b>${c.fmc}</b></div>
    <div class="chip">PMC <b>${c.pmc}</b></div>
    <div class="chip">NMC <b>${c.nmc}</b></div>
    <div class="chip">MC rate <b class="mc ${mcClass(mc ?? 0, c.total)}">${mc === null ? "—" : mc + "%"}</b></div>`;
  renderVizInto($("#utc-viz-wrap"), vehs,
    "No vehicles assigned yet — this UTC's skyline is dark.");
  // member list
  const members = $("#utc-members");
  members.innerHTML = vehs.length ? vehs.map(v => `
    <div class="vcard ${v.status}" style="cursor:default">
      <div class="top"><span class="bumper">${esc(v.bumper)} ${v.is_demo ? '<span class="sampletag">SAMPLE</span>' : ""}</span>
      <span class="st ${v.status}">${v.status}</span></div>
      <div class="meta">${esc(v.type)} · ${esc(v.location || "—")}</div>
      <div class="row" style="margin-top:8px">
        <button class="btn" data-open-veh="${v.id}">Open vehicle</button>
        ${editable ? `<button class="btn danger" data-utc-remove="${v.id}">Remove from UTC</button>` : ""}
      </div>
    </div>`).join("")
    : '<p class="hint">No vehicles assigned yet.</p>';
  $$("[data-open-veh]", members).forEach(b => b.addEventListener("click", () => {
    const v = fleet.vehicles.find(x => String(x.id) === b.dataset.openVeh);
    if (v) openVehicle(v);
  }));
  if (editable) {
    $$("[data-utc-remove]", members).forEach(b => b.addEventListener("click", async () => {
      if (!navigator.onLine) {
        showNotice("UTC changes need a connection — viewing works offline, changes don't (v1.1).");
        return;
      }
      try {
        await api(`/api/utcs/${u.id}/vehicles/${b.dataset.utcRemove}`,
                  {method: "DELETE"});
        await refreshFleet(false); renderUtc();
        showNotice("Vehicle removed from the UTC.", false);
      } catch (e) { showNotice("Remove failed: " + (e.message || e.status)); }
    }));
  }
  // add-vehicles checklist (this unit's vehicles not yet members)
  const picker = $("#utc-add-list");
  const memberIds = new Set((u.vehicle_ids || []).map(String));
  const candidates = unitVehicles(u.unit).filter(v => !memberIds.has(String(v.id)));
  picker.innerHTML = editable
    ? (candidates.length ? candidates.map(v => `
        <label class="checkrow"><input type="checkbox" data-utc-add="${v.id}">
          <span><b>${esc(v.bumper)}</b> · ${esc(v.type)} · ${v.status}</span></label>`).join("")
      : '<p class="hint">Every vehicle in this unit is already in the UTC.</p>')
    : "";
  $("#utc-editor-tools").classList.toggle("hidden", !editable);
  const codeIn = $("#utc-edit-code"), titleIn = $("#utc-edit-title");
  if (codeIn) codeIn.value = u.code;
  if (titleIn) titleIn.value = u.title || "";
}

/* ============================================================ Excel import
   Two-step: pick a file -> server parses and returns a PREVIEW (nothing
   is written) -> confirm -> the server re-validates and creates the
   will-import rows. Add-only: duplicates are skipped, never overwritten. */
function readFileB64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] || "");
    r.onerror = () => reject(new Error("couldn't read the file"));
    r.readAsDataURL(file);
  });
}
async function startImport(file) {
  if (!navigator.onLine) {
    showNotice("Import needs a connection — the server parses and validates the file.");
    return;
  }
  if (file.size > 5 * 1024 * 1024) {
    showNotice("That file is over the 5 MB import cap.");
    return;
  }
  let preview;
  try {
    const content_b64 = await readFileB64(file);
    preview = await api("/api/import/preview", {method: "POST", body: {
      unit: currentUnit, filename: file.name, content_b64}});
  } catch (e) {
    showNotice("Import preview failed: " + (e.message || e.status));
    return;
  }
  importPreview = preview;
  renderImportPreview();
}
function renderImportPreview() {
  const card = $("#import-preview");
  const p = importPreview;
  if (!p) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  card.innerHTML = `
    <h3>Import preview — ${esc(p.filename)} → ${esc(p.unit)}</h3>
    <p class="hint">${p.will_import} vehicle(s) will be imported · ${p.skipped} skipped.
    Nothing has been added yet — check the rows, then confirm.
    Import is add-only: duplicates are skipped, never overwritten.</p>
    ${p.unmapped_columns && p.unmapped_columns.length
      ? `<p class="hint">Unmapped columns (ignored): ${p.unmapped_columns.map(esc).join(", ")}</p>` : ""}
    <table><tr><th>Row</th><th>Asset #</th><th>Type</th><th>Status</th>
      <th>Location</th><th>Mi/Hrs</th><th></th></tr>
    ${p.rows.map(r => `<tr>
      <td>${r.row}</td><td><b>${esc(r.bumper || "—")}</b></td><td>${esc(r.type)}</td>
      <td>${r.status}${r.status_defaulted ? ' <span class="hint">(default)</span>' : ""}</td>
      <td>${esc(r.location || "")}</td><td>${esc(r.mileage ?? "")}</td>
      <td>${r.ok ? '<span style="color:var(--fmc)">will import</span>'
                  : `<span style="color:var(--nmc)">skipped — ${esc(r.reason)}</span>`}</td>
    </tr>`).join("")}</table>
    <div class="row" style="margin-top:10px">
      <button class="btn primary" id="import-confirm" ${p.will_import ? "" : "disabled"}>
        Confirm import (${p.will_import})</button>
      <button class="btn ghost" id="import-cancel">Cancel</button>
      <span id="import-msg" class="hint"></span>
    </div>`;
  $("#import-cancel").addEventListener("click", () => {
    importPreview = null; renderImportPreview();
  });
  $("#import-confirm").addEventListener("click", async () => {
    const rows = p.rows.filter(r => r.ok).map(r => ({
      bumper: r.bumper, type: r.type, status: r.status,
      mileage: r.mileage, maint_due_date: r.maint_due_date,
      maint_due_miles: r.maint_due_miles, location: r.location,
      notes: r.notes}));
    try {
      const res = await api("/api/import/confirm", {method: "POST", body: {
        unit: p.unit, filename: p.filename, rows}});
      importPreview = null; renderImportPreview();
      await refreshFleet(false); render();
      showNotice(`Imported ${res.created_count} vehicle(s) into ${p.unit}` +
        (res.skipped_count ? ` · ${res.skipped_count} skipped at confirm (duplicates).` : "") + ".", false);
    } catch (e) {
      showNotice("Import failed: " + (e.message || e.status));
    }
  });
  card.scrollIntoView({behavior: "smooth", block: "start"});
}

/* ================================================== fleet health visualization
   Renders the unit's fleet as a night city skyline: one building per
   vehicle, districts by type. Implemented with inline SVG, CSS
   animations only — fast with 100+ vehicles, never blocks input.     */
const TYPE_HEIGHT = {"LMTV": 150, "JLTV": 122, "MTV": 140, "HMMWV": 96,
  "F-250 Truck": 80, "Truck": 84, "Generator": 54, "Trailer": 46};
function seeded(n) { const x = Math.sin(n * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); }
function vkey(v) { // stable numeric key even for offline temp ids ("tmp-…")
  let h = 0; const s = String(v.id);
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 9973;
  return h + 1;
}

function renderViz(unitName, vehs) {
  renderVizInto($("#viz-wrap"), vehs,
    "The skyline is dark — no vehicles on hand yet.");
}
/* The same visualization renders a unit's fleet OR a UTC's assigned
   vehicles — only the vehicle set (and empty text) differs. */
function renderVizInto(wrap, vehs, emptyMsg) {
  if (!wrap) return;
  if (!vehs.length) {
    wrap.innerHTML = `<div class="viz-empty">${esc(emptyMsg || "No vehicles to show yet.")}</div>`;
    return;
  }
  const H = 270, GROUND = 232;
  // districts by type, first-seen order
  const districts = [];
  for (const v of vehs) {
    let d = districts.find(d => d.type === v.type);
    if (!d) { d = {type: v.type, items: []}; districts.push(d); }
    d.items.push(v);
  }
  districts.forEach(d => d.items.sort((a, b) => a.bumper.localeCompare(b.bumper)));
  const total = vehs.length;
  const gap = 3;
  // Layout FIRST (buildings may narrow to fit), then size the canvas to
  // the layout — every vehicle renders, nothing falls off-canvas, and
  // very large fleets scroll horizontally instead of truncating.
  const bw = Math.max(9, Math.min(46, (920 - 40) / total - gap));
  let x = 20;
  const placed = [];   // {v, x, dMid}
  for (const d of districts) {
    const dStart = x;
    for (const v of d.items) { placed.push({v, x, d}); x += bw + gap; }
    d.mid = (dStart + x - gap) / 2;
    x += 10;
  }
  const W = Math.max(920, Math.ceil(x + 20));
  let parts = [];
  // sky: stars + moon
  let stars = "";
  for (let i = 0; i < 60; i++) {
    stars += `<circle cx="${(seeded(i) * W).toFixed(0)}" cy="${(seeded(i + 99) * 110).toFixed(0)}" r="${(seeded(i + 7) * 1.2 + .4).toFixed(1)}" fill="#cfc6ee" class="twinkle" style="animation-delay:${(seeded(i + 3) * 4).toFixed(1)}s"/>`;
  }
  parts.push(`<rect width="${W}" height="${H}" fill="url(#skygrad)"/>
    ${stars}
    <circle cx="${W - 78}" cy="42" r="19" fill="#f4ead0"/>
    <circle cx="${W - 86}" cy="37" r="16" fill="#0a0817" opacity=".16"/>
    <circle cx="${W - 78}" cy="42" r="30" fill="none" stroke="#f4ead0" stroke-opacity=".18" stroke-width="6"/>`);
  for (const d of districts) {
    for (const p of placed.filter(p => p.d === d)) {
      const v = p.v, bx = p.x;
      const hBase = TYPE_HEIGHT[v.type] || 88;
      const h = hBase * (0.85 + seeded(vkey(v) * 3 + 1) * 0.3);
      const y = GROUND - h;
      const cols = Math.max(2, Math.floor(bw / 10));
      const rows = Math.max(2, Math.floor(h / 16));
      const nWin = cols * rows;
      let lit = 0;
      if (v.status === "FMC") lit = nWin;
      else if (v.status === "PMC") lit = Math.floor(nWin / 2);
      let wins = "";
      for (let i = 0; i < nWin; i++) {
        const cx = bx + 5 + (i % cols) * ((bw - 8) / Math.max(1, cols - 1 || 1));
        const cy = y + 9 + Math.floor(i / cols) * 14;
        if (cy > GROUND - 8) break;
        const isLit = i < lit && seeded(vkey(v) * 31 + i * 7) > 0.14;
        const colr = seeded(vkey(v) * 13 + i) > 0.45 ? "#ffd58a" : "#b78cff";
        const flick = v.status === "PMC" && isLit && seeded(i + v.id) > .7 ? ' class="flicker"' : "";
        wins += `<rect x="${cx.toFixed(1)}" y="${cy.toFixed(1)}" width="4.6" height="6.4" rx="1" fill="${isLit ? colr : "#241d3f"}"${flick}/>`;
      }
      const due = dueInfo(v);
      const crane = due && due.soon ? `
        <g stroke="#ffb54d" stroke-width="2">
          <line x1="${bx + bw - 3}" y1="${y}" x2="${bx + bw - 3}" y2="${y - 20}"/>
          <line x1="${bx + bw - 3}" y1="${y - 20}" x2="${bx + 6}" y2="${y - 20}"/>
          <line x1="${bx + 12}" y1="${y - 20}" x2="${bx + 12}" y2="${y - 9}"/>
          <rect x="${bx + 9}" y="${y - 9}" width="6" height="5" fill="#ffb54d" stroke="none"/>
        </g>` : "";
      const beacon = v.status === "NMC" ?
        `<circle cx="${bx + bw / 2}" cy="${y - 5}" r="3.6" fill="#ff5d6c" class="beacon"/>` : "";
      parts.push(`<g class="bldg" data-vid="${esc(v.id)}" tabindex="0" role="button" aria-label="${esc(v.bumper)} ${esc(v.type)} ${v.status}">
        <rect class="body" x="${bx}" y="${y}" width="${bw}" height="${h}" rx="2.5" fill="#171331" stroke="#332a5e"/>
        ${wins}${crane}${beacon}
        <title>${esc(v.bumper)} — ${esc(v.type)} — ${v.status}</title></g>`);
    }
    parts.push(`<text class="district-lbl" x="${d.mid}" y="${GROUND + 18}" text-anchor="middle">${esc(d.type.toUpperCase())}</text>`);
  }
  parts.push(`<rect y="${GROUND}" width="${W}" height="${H - GROUND}" fill="#0a0817"/>
              <line x1="0" y1="${GROUND}" x2="${W}" y2="${GROUND}" stroke="#332a5e"/>`);
  const fmc = vehs.filter(v => v.status === "FMC").length;
  const pmc = vehs.filter(v => v.status === "PMC").length;
  const nmc = vehs.filter(v => v.status === "NMC").length;
  wrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Fleet health visualization"${W > 920 ? ` style="width:${W}px;max-width:none"` : ""}>
    <defs><linearGradient id="skygrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#060512"/><stop offset="1" stop-color="#141034"/>
    </linearGradient></defs>${parts.join("")}</svg>
    <div class="viz-caption">${total} vehicle${total === 1 ? "" : "s"} — FMC ${fmc} · PMC ${pmc} · NMC ${nmc} · tap a building to open it</div>`;
  const activate = (g) => {
    $$(".bldg", wrap).forEach(o => o.classList.remove("sel"));
    g.classList.add("sel");
    const v = vehs.find(v => String(v.id) === g.dataset.vid);
    if (v) openVehicle(v);
  };
  $$(".bldg", wrap).forEach(g => {
    g.addEventListener("click", () => activate(g));
    g.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(g); }
    });
  });
}

/* ============================================================ vehicle page */
function openVehicle(v) {
  if (!v) return;
  currentVehicle = v;
  showView("view-vehicle");
  const editable = canEdit();
  const d = dueInfo(v);
  $("#vehicle-detail").innerHTML = `
    <div class="card" id="vcard-detail">
      <div class="top" style="display:flex;justify-content:space-between">
        <span class="bumper" style="font-size:1.4rem;font-weight:800">${esc(v.bumper)}
          ${v.is_demo ? '<span class="sampletag">SAMPLE</span>' : ""}</span>
        <span class="st ${v.status}">${v.status}</span>
      </div>
      <p class="meta" style="color:var(--dim)">${esc(v.type)} · ${esc(v.unit)} ·
        last updated by ${esc(v.updated_by || "—")}${v.updated_at ? " · " + fmtTime(v.updated_at) : ""}</p>

      <div class="field"><div class="lbl">Status ${editable ? "" : "(read-only)"}</div>
      <div class="statusbtns">
        ${["FMC", "PMC", "NMC"].map(s =>
          `<button data-status="${s}" class="${v.status === s ? "sel-" + s : ""}" ${editable ? "" : "disabled"}>${s}</button>`).join("")}
      </div></div>

      <div class="grid2">
        <div class="field"><div class="lbl">Mileage / hours</div>
          <input id="vf-mileage" type="number" aria-label="Mileage or hours" value="${esc(v.mileage ?? "")}" ${editable ? "" : "disabled"}></div>
        <div class="field"><div class="lbl">Location</div>
          <input id="vf-location" aria-label="Location" value="${esc(v.location || "")}" ${editable ? "" : "disabled"}></div>
        <div class="field"><div class="lbl">Maintenance due (date) ${d && d.overdue ? '<span class="duetag overdue">OVERDUE</span>' : d && d.soon ? '<span class="duetag">due soon</span>' : ""}</div>
          <input id="vf-due" type="date" aria-label="Maintenance due date" value="${esc(v.maint_due_date || "")}" ${editable ? "" : "disabled"}></div>
        <div class="field"><div class="lbl">Maintenance due at miles (optional)</div>
          <input id="vf-due-miles" type="number" aria-label="Maintenance due at miles" value="${esc(v.maint_due_miles ?? "")}" ${editable ? "" : "disabled"}></div>
        <div class="field"><div class="lbl">Type</div>
          <input id="vf-type" aria-label="Vehicle type" value="${esc(v.type)}" list="type-list" ${editable ? "" : "disabled"}></div>
        <div class="field"><div class="lbl">Unit</div>
          <select id="vf-unit" aria-label="Unit" ${editable ? "" : "disabled"}>
            ${UNITS.map(u => `<option ${u === v.unit ? "selected" : ""}>${u}</option>`).join("")}
          </select></div>
      </div>
      <div class="field"><div class="lbl">Notes</div>
        <input id="vf-notes" aria-label="Notes" value="${esc(v.notes || "")}" ${editable ? "" : "disabled"}></div>
      <div class="field"><div class="lbl">Vehicle photo</div>
        <div id="vf-photo-wrap">
          ${v.photo ? `<img id="vf-photo-img" alt="Vehicle photo" style="max-width:100%;border-radius:12px">`
                    : `<span class="hint">No photo yet.</span>`}
        </div>
        <div class="row" style="margin-top:8px">
          <input type="file" id="vf-photo-file" accept="image/*" capture="environment" aria-label="Vehicle photo file">
          <button class="btn" id="vf-photo-upload">Attach photo</button>
          ${v.photo ? `<button class="btn danger" id="vf-photo-remove">Remove photo</button>` : ""}
        </div>
        <span id="vf-photo-msg" class="hint"></span>
      </div>
      <div class="row">
        <button class="btn" id="vf-inspection">New 1800 inspection</button>
        <span class="hint">Operator's Inspection Guide — submits to the VCO review queue. Online-only.</span>
      </div>
      ${editable ? `<div class="row">
        <button class="btn primary" id="vf-save">Save changes</button>
        <button class="btn danger" id="vf-delete">Delete vehicle</button>
        <span id="vf-msg" class="hint"></span></div>` : ""}
    </div>`;
  $("#vf-inspection").addEventListener("click", () => openNewInspection(v));
  // vehicle photo: every signed-in role, online-only (never queued)
  // the re-render above discarded the previous photo <img>: its blob
  // URL is unreferenced now and safe to revoke (v1.2 fix pass)
  revokeBlobUrl(currentVehiclePhotoUrl);
  currentVehiclePhotoUrl = null;
  if (v.photo) {
    const seq = ++vehiclePhotoSeq;
    fetchPhoto("/api/vehicles/" + v.id + "/photo").then(src => {
      // stale fetch (a newer render superseded this one): drop it
      if (seq !== vehiclePhotoSeq || currentVehicle !== v) {
        revokeBlobUrl(src); return;
      }
      const img = $("#vf-photo-img");
      if (!img) { revokeBlobUrl(src); return; }
      if (src) {
        revokeBlobUrl(currentVehiclePhotoUrl);
        currentVehiclePhotoUrl = src;
        img.src = src;
      }
      else img.outerHTML = `<span class="hint">Vehicle photo needs a connection to load.</span>`;
    });
  }
  $("#vf-photo-upload").addEventListener("click", async () => {
    const file = $("#vf-photo-file").files[0];
    if (!file) { showNotice("Choose a photo first."); return; }
    if (!navigator.onLine) { showNotice("Vehicle photos are online-only — connect to attach. Nothing was queued."); return; }
    $("#vf-photo-msg").textContent = "Processing…";
    let dataUrl = await downscaleImage(file);
    if (!dataUrl) {
      try { dataUrl = await readFileAsDataURL(file); }
      catch (e) { showNotice("Could not read that photo file."); return; }
    }
    // 3 MB client check on BOTH paths (v1.2 fix pass): the downscaled
    // result used to skip it, leaving only the server 413 backstop
    if (dataUrl.length > 3145728) { showNotice("That photo is too large (3 MB cap) — pick a smaller one."); $("#vf-photo-msg").textContent = ""; return; }
    try {
      await api("/api/vehicles/" + v.id + "/photo", {method: "POST", body: {image: dataUrl}});
      showNotice("Vehicle photo attached.", false);
      v.photo = 1;
      const f = fleet.vehicles.find(x => x.id === v.id);
      if (f) f.photo = 1;
      openVehicle(v);
    } catch (e) { showNotice("Photo upload failed: " + (e.message || e.status)); }
  });
  const rmBtn = $("#vf-photo-remove");
  if (rmBtn) rmBtn.addEventListener("click", async () => {
    if (!navigator.onLine) { showNotice("Vehicle photos are online-only — connect to remove."); return; }
    if (!confirm("Remove this vehicle's photo?")) return;
    try {
      await api("/api/vehicles/" + v.id + "/photo", {method: "DELETE"});
      showNotice("Vehicle photo removed.", false);
      v.photo = 0;
      const f = fleet.vehicles.find(x => x.id === v.id);
      if (f) f.photo = 0;
      openVehicle(v);
    } catch (e) { showNotice("Photo remove failed: " + (e.message || e.status)); }
  });
  if (!editable) return;
  let newStatus = v.status;
  $$("#vehicle-detail [data-status]").forEach(b => b.addEventListener("click", () => {
    newStatus = b.dataset.status;
    $$("#vehicle-detail [data-status]").forEach(o =>
      o.className = o.dataset.status === newStatus ? "sel-" + newStatus : "");
  }));
  $("#vf-save").addEventListener("click", async () => {
    const payload = {
      _id: v.id, base_status: v.status, status: newStatus,
      mileage: numOrNull($("#vf-mileage").value),
      location: $("#vf-location").value, maint_due_date: $("#vf-due").value,
      maint_due_miles: numOrNull($("#vf-due-miles").value),
      type: $("#vf-type").value, unit: $("#vf-unit").value,
      notes: $("#vf-notes").value,
    };
    const old = {...v};
    const result = await queueOrSend("update", payload, () => Object.assign(v, {
      status: newStatus, mileage: payload.mileage, location: payload.location,
      maint_due_date: payload.maint_due_date, maint_due_miles: payload.maint_due_miles,
      type: payload.type, unit: payload.unit, notes: payload.notes,
      updated_by: me.display_name}));
    $("#vf-msg").textContent = result === "accepted" ? "Saved."
      : result === "queued" ? "Queued — will sync when online."
      : "Not saved — the server rejected that change.";
    const card = $("#vcard-detail");
    if (newStatus !== old.status) card.classList.add("flash");  // status flash
    card.style.animation = "none"; void card.offsetWidth;
    setTimeout(() => {
      openUnit(old.unit);
      if (newStatus !== old.status) {
        const vc = $(`.vcard[data-vid="${v.id}"]`);
        if (vc) vc.classList.add("flash");
      }
    }, 350);
  });
  $("#vf-delete").addEventListener("click", async () => {
    if (!confirm(`Delete ${v.bumper}? This writes an audit entry.`)) return;
    try { await api("/api/vehicles/" + v.id, {method: "DELETE"}); }
    catch (e) { alert("Delete failed (needs connection): " + e.message); return; }
    await refreshFleet(false);
    openUnit(currentUnit);
  });
}

/* ============================================================ audit/conflicts */
async function renderAudit() {
  let r;
  try {
    r = await api("/api/audit");
  } catch (e) {
    $("#audit-list").innerHTML =
      `<div class="card hint" style="color:var(--nmc)">Couldn't load the audit log — ${esc(e.message || "network error")}. Your connection may be down; try again when you're back online.</div>`;
    return;
  }
  $("#audit-list").innerHTML = r.audit.map(a => `
    <div class="arow ${a.is_conflict ? "conflictrow" : ""}">
      <b>${esc(a.actor)}</b> ${esc(a.action)}${a.field ? ` · ${esc(a.field)}` : ""}
      ${a.old_value || a.new_value ? `: <s>${esc(a.old_value)}</s> → <b>${esc(a.new_value)}</b>` : ""}
      ${a.detail ? ` — ${esc(a.detail)}` : ""}
      ${a.is_conflict ? ' <span class="sampletag" style="background:#5c2230;color:#ff5d6c">CONFLICT</span>' : ""}
      <div class="when">${fmtTime(a.created_at)}${a.vehicle_id ? " · vehicle #" + a.vehicle_id : ""}</div>
    </div>`).join("") || '<p class="hint">No audit entries yet.</p>';
}
function renderConflicts() {
  const vehById = id => fleet.vehicles.find(v => v.id === id);
  if (conflictsError) {
    $("#conflict-list").innerHTML =
      `<div class="card hint" style="color:var(--nmc)">Couldn't load conflicts — ${esc(conflictsError)}. Try again when you're back online.</div>`;
    return;
  }
  $("#conflict-list").innerHTML = conflicts.map(c => {
    const v = vehById(c.vehicle_id);
    return `<div class="card">
      <b>${v ? esc(v.bumper) + " (" + esc(v.unit) + ")" : "vehicle #" + c.vehicle_id}</b> —
      ${esc(c.actor)} tried to set status to <b>${esc(c.attempted)}</b> from an offline copy;
      current status is <b>${esc(c.current)}</b>.
      <div class="when" style="color:var(--dim)">${fmtTime(c.created_at)}</div>
      ${canEdit() ? `<div class="row" style="margin-top:10px">
        <button class="btn" data-resolve="keep" data-cid="${c.id}">Keep ${esc(c.current)}</button>
        <button class="btn primary" data-resolve="apply" data-cid="${c.id}">Apply ${esc(c.attempted)}</button>
      </div>` : '<p class="hint">An Editor must resolve this.</p>'}
    </div>`;
  }).join("") || '<p class="hint">No open conflicts. 🎉</p>';
  $$("[data-resolve]").forEach(b => b.addEventListener("click", async () => {
    try {
      await api(`/api/conflicts/${b.dataset.cid}/resolve`,
                {method: "POST", body: {action: b.dataset.resolve}});
      showNotice("Conflict resolved.", false);
    } catch (e) {
      showNotice("Couldn't resolve that conflict: " + (e.message || e.status));
      return;
    }
    await loadConflicts(); await refreshFleet(false);
    renderConflicts();
  }));
}

/* ============================================================ VCO */
async function renderVCO() {
  const unitOpts = UNITS.map(u => `<option>${u}</option>`).join("");
  $("#uf-unit").innerHTML = unitOpts;
  $("#af-unit").innerHTML = UNITS.map(u => `<option>${u}</option>`).join("");
  const r = await api("/api/users").catch(() => ({users: []}));
  $("#user-list").innerHTML = `<table><tr><th>User</th><th>Unit</th><th>Role</th></tr>` +
    r.users.map(u => `<tr><td><b>${esc(u.display_name)}</b> <span style="color:var(--dim)">@${esc(u.username)}</span></td>
      <td>${esc(u.unit || "—")}</td><td>
      <select class="inline" data-role-for="${u.id}">
        ${[["operator", "Operator — read all + 1800 inspections"],
            ["editor", "Editor — update any unit"],
            ["vco", "VCO — everything + inspection review"]].map(([role, label]) =>
          `<option value="${role}" ${role === u.role ? "selected" : ""}>${label}</option>`).join("")}
      </select></td></tr>`).join("") + `</table>`;
  $$("[data-role-for]").forEach(s => s.addEventListener("change", async () => {
    try {
      await api("/api/users/" + s.dataset.roleFor,
                {method: "PUT", body: {role: s.value}});
      showNotice("Role updated.", false);
    } catch (e) {
      showNotice("Role change failed: " + (e.message || e.status));
      renderVCO();  // revert the select to the server's truth
    }
  }));
  $("#auth-list").innerHTML = `<table><tr><th>Unit</th><th>Type</th><th>Authorized</th><th>On hand</th><th></th></tr>` +
    fleet.authorizations.map(a => {
      const oh = fleet.vehicles.filter(v => v.unit === a.unit && v.type === a.vehicle_type).length;
      return `<tr><td>${esc(a.unit)}</td><td>${esc(a.vehicle_type)}${a.is_demo ? ' <span class="sampletag">SAMPLE</span>' : ""}</td><td>${a.authorized}</td><td>${oh}</td><td><button class="btn danger" data-auth-del="${a.id}">Remove</button></td></tr>`;
    }).join("") + `</table>`;
  // same delete as the unit screen (v1.1.1): online-only, confirmed,
  // vehicles of the type are never touched
  $$("[data-auth-del]").forEach(b => b.addEventListener("click", async () => {
    const a = fleet.authorizations.find(x => x.id === Number(b.dataset.authDel));
    if (!a) return;
    if (!confirm(`Remove the authorization for ${a.unit} / ${a.vehicle_type} (currently ${a.authorized})?\n\n` +
        `This deletes the authorization line entirely. The unit's ${a.vehicle_type} vehicles are NOT affected.\n` +
        `Softer option: set the number to 0 instead — the line stays, at zero.`)) return;
    try {
      await api("/api/authorizations/" + a.id, {method: "DELETE"});
    } catch (e) {
      showNotice("Remove failed (needs connection): " + (e.message || e.status));
      return;
    }
    fleet.authorizations = fleet.authorizations.filter(x => x.id !== a.id);
    showNotice(`Authorization removed: ${a.unit} ${a.vehicle_type}.`, false);
    renderVCO();
  }));
}

/* ============================================================ print rollup */
function renderPrint() {
  const stats = deriveStats();
  const rows = UNITS.map(n => ownStats(n));   // per-unit own fleets + group line
  const group = stats["18 ASOG"];
  $("#print-area").innerHTML = `
    <h1>Skyline — Fleet Rollup</h1>
    <p>Generated ${new Date().toLocaleString()} · On hand ${group.on_hand} ·
    Authorizations ${group.authorizations} · Group MC rate ${group.on_hand ? group.mc_rate + "%" : "—"}
    ${group.includes_demo ? " · <b>incl. SAMPLE data</b>" : ""}</p>
    ${rows.map(st => `
      <h2>${st.unit} — on hand ${st.on_hand} / auth ${st.authorizations} · MC ${st.on_hand ? st.mc_rate + "%" : "—"}${st.includes_demo ? " · incl. SAMPLE data" : ""}</h2>
      <table><tr><th>Bumper</th><th>Type</th><th>Status</th><th>Mi/Hrs</th><th>Maint due</th><th>Location</th></tr>
      ${unitVehicles(st.unit).map(v => `<tr><td>${esc(v.bumper)}${v.is_demo ? " (SAMPLE)" : ""}</td>
        <td>${esc(v.type)}</td><td>${v.status}</td><td>${v.mileage ?? ""}</td>
        <td>${esc(v.maint_due_date || "")}</td><td>${esc(v.location || "")}</td></tr>`).join("")}
      </table>`).join("")}`;
}

/* ============================================================ AF Form 1800 inspections
   Submitting is open to every signed-in role (operators included);
   review is VCO-only. Inspections are ONLINE-ONLY in the real app
   (never queued): the UI says so plainly when offline, the same
   honesty pattern as vehicle deletes and UTC mutations. The web sim
   runs the same code against its in-memory backend, so inspections
   work there too. The printable form is rendered CLIENT-SIDE from the
   detail JSON (inspectionFormHTML) — no server HTML page — which is
   what makes sim parity possible with the shared app.js. */
let inspVehicle = null, inspList = [], currentInspectionFull = null;
let currentInspectionPhotoUrls = null;
let currentVehiclePhotoUrl = null, vehiclePhotoSeq = 0;
let inspFilter = {unit: "ALL", reviewed: "ALL"};

/* Fetch a photo endpoint with auth and return a usable <img> src:
   real server -> image bytes -> blob URL; web sim -> data URL text
   returned as-is. Null when unavailable (offline, missing, error). */
async function fetchPhoto(url) {
  let res;
  try {
    res = await fetch(url, {headers: {Authorization: "Bearer " + token}});
  } catch (e) { return null; }
  if (!res.ok) return null;
  const ct = res.headers.get("Content-Type") || "";
  if (ct.startsWith("image/")) {
    const b = await res.blob();
    return URL.createObjectURL(b);
  }
  const t = await res.text();
  return t.startsWith("data:image/") ? t : null;
}
/* Revoke a blob URL only (sim data URLs pass through untouched), and
   only once nothing displays it: revoking a displayed photo's URL
   breaks the image, so revocation happens on replace (a new photo
   set loads) or on leaving the view, never while on screen
   (v1.2 fix pass). */
function revokeBlobUrl(u) {
  if (typeof u === "string" && u.startsWith("blob:")) {
    try { URL.revokeObjectURL(u); } catch (e) { /* already revoked */ }
  }
}
function revokePhotoUrls(urls) {
  if (!urls) return;
  revokeBlobUrl(urls.vehicle);
  for (const list of Object.values(urls.byDiscrepancy || {}))
    for (const u of list) revokeBlobUrl(u);
}
function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(new Error("could not read file"));
    r.readAsDataURL(file);
  });
}
/* Downscale to max 1600 px (longest side), JPEG 0.8. Resolves null on
   any failure so the caller can fall back to the original file. */
function downscaleImage(file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      try {
        const MAX = 1600;
        const scale = Math.min(1, MAX / Math.max(img.width, img.height));
        const w = Math.max(1, Math.round(img.width * scale));
        const h = Math.max(1, Math.round(img.height * scale));
        const c = document.createElement("canvas");
        c.width = w; c.height = h;
        c.getContext("2d").drawImage(img, 0, 0, w, h);
        c.toBlob((blob) => {
          if (!blob) return resolve(null);
          readFileAsDataURL(blob).then(resolve, () => resolve(null));
        }, "image/jpeg", 0.8);
      } catch (e) { resolve(null); }
    };
    img.onerror = () => { URL.revokeObjectURL(url); resolve(null); };
    img.src = url;
  });
}

/* Pure: the printable 1800 form from a detail payload
   {inspection, items, discrepancies}. Used on-screen AND for print
   (via #print-area + window.print()), so both stay identical. */
function inspectionFormHTML(full, photoUrls = {vehicle: null, byDiscrepancy: {}}) {
  const insp = full.inspection;
  const items = full.items || [];
  const discs = full.discrepancies || [];
  return `
    <h1>AF Form 1800 — Operator's Inspection Guide and Trouble Report</h1>
    ${photoUrls.vehicle ? `<div><img src="${esc(photoUrls.vehicle)}" alt="Vehicle photo" style="max-width:280px;border-radius:12px"></div>` : ""}
    <p><b>Unit:</b> ${esc(insp.unit)} · <b>Vehicle:</b> ${esc(insp.bumper)} ·
    <b>Month / Year:</b> ${esc(insp.month_year)} · <b>Operator:</b> ${esc(insp.operator)}</p>
    <p><b>Submitted:</b> ${esc(fmtTime(insp.created_at))} ·
    <b>Mileage / hours:</b> ${esc(insp.mileage_hours ?? "—")} ·
    <b>Tire pressure front:</b> ${esc(insp.tire_front || "—")} ·
    <b>rear:</b> ${esc(insp.tire_rear || "—")} ·
    <b>Status:</b> ${esc(insp.status)}${insp.reviewed_by ? ` · <b>Reviewed by:</b> ${esc(insp.reviewed_by)} · ${esc(fmtTime(insp.reviewed_at))}` : ""}</p>
    <h2>Inspection items (${items.length} · ${discs.length} discrepanc${discs.length === 1 ? "y" : "ies"})</h2>
    <table><tr><th>#</th><th>Item</th><th>Applies</th><th>Result</th><th>Comment</th></tr>
    ${items.map(it => `<tr><td>${it.item_no ?? "other"}</td><td>${esc(it.item_text)}</td>
      <td>${it.applies ? "Yes" : "No"}</td><td>${it.applies ? esc(it.result || "") : "—"}</td>
      <td>${esc(it.comment || "")}</td></tr>`).join("")}</table>
    <h2>Discrepancies</h2>
    ${discs.length ? `<table><tr><th>Item</th><th>Discrepancy</th><th>Date found</th><th>Miles / hours</th><th>Status</th><th>Photos</th></tr>
    ${discs.map(d => {
      const urls = (photoUrls.byDiscrepancy[d.id] || []);
      return `<tr><td>${d.item_no ?? "other"}</td><td>${esc(d.description)}</td>
      <td>${esc(d.date_found)}</td><td>${esc(d.miles_hours ?? "")}</td><td>${esc(d.status)}</td>
      <td>${urls.length ? urls.map(u => `<img src="${esc(u)}" alt="Discrepancy photo" style="max-width:120px;border-radius:8px;margin:2px">`).join("") : "—"}</td></tr>`;
    }).join("")}</table>`
      : "<p>None — no discrepancies reported.</p>"}
    <div class="siglines">
      <p>Operator signature: ______________________________ &nbsp; Date: ______________</p>
      <p>VCO signature: ______________________________ &nbsp; Date: ______________</p>
    </div>
    <p class="hint">Printed from Skyline. The operator's signature signifies accomplishment of the checks; the VCO signature records review.</p>`;
}

function openNewInspection(v) {
  inspVehicle = v;
  showView("view-new-inspection");
  renderNewInspection(v);
}

function renderNewInspection(v) {
  const monthDefault = new Date().toLocaleString("en-US", {month: "long", year: "numeric"});
  $("#new-inspection-body").innerHTML = `
    <div class="card">
      <h3>${esc(v.bumper)} · ${esc(v.type)} · ${esc(v.unit)}</h3>
      <p class="hint">AF Form 1800 — Operator's Inspection Guide and Trouble Report.
      Check the items that apply; mark pass/fail and add a comment per item.
      Failed items become discrepancies for the VCO review queue. Submitting
      updates this vehicle's mileage/hours immediately; it never changes the
      FMC/PMC/NMC status — that stays an editor/VCO action.
      Inspections are online-only.</p>
      <div class="grid2">
        <label>Month / Year <input id="insp-month" value="${esc(monthDefault)}" aria-label="Month and year"></label>
        <label>Mileage / hours <input id="insp-mileage" type="number" min="0" value="${esc(v.mileage ?? "")}" aria-label="Mileage or hours"></label>
        <label>Tire pressure — front <input id="insp-tire-front" aria-label="Tire pressure front"></label>
        <label>Tire pressure — rear <input id="insp-tire-rear" aria-label="Tire pressure rear"></label>
      </div>
    </div>
    <div class="card">
      <h3>Checklist — 35 items</h3>
      <div id="insp-items">
      ${INSPECTION_ITEMS.map((text, i) => {
        const n = i + 1;
        return `<div class="insp-row">
          <div class="insp-num">${n}</div>
          <div class="insp-text">${esc(text)}</div>
          <label class="insp-applies"><input type="checkbox" id="insp-applies-${n}" checked> Applies</label>
          <div class="insp-result">
            <label><input type="radio" name="insp-result-${n}" value="pass" checked> Pass</label>
            <label><input type="radio" name="insp-result-${n}" value="fail"> Fail</label>
          </div>
          <input id="insp-comment-${n}" class="insp-comment" placeholder="Comment (add detail when failed)" aria-label="Comment for item ${n}">
        </div>`;
      }).join("")}
      </div>
      <div id="insp-others"></div>
      <div class="row" style="margin-top:10px">
        <button class="btn" id="insp-add-other">+ Add other item</button>
        <span class="hint">Up to 10 locally added items (the form's "other" rows).</span>
      </div>
      <div class="row" style="margin-top:14px">
        <button class="btn primary" id="insp-submit">Submit 1800 inspection</button>
        <span id="insp-msg" class="hint"></span>
      </div>
    </div>`;
  let otherCount = 0;
  $("#insp-add-other").addEventListener("click", () => {
    if (otherCount >= 10) { showNotice("At most 10 other items."); return; }
    otherCount++;
    const k = otherCount;
    $("#insp-others").innerHTML += `<div class="insp-row insp-other-row">
      <div class="insp-num">+</div>
      <input class="insp-other-text" placeholder="Other item description (max 120 chars)" aria-label="Other item description">
      <div class="insp-result">
        <label><input type="radio" name="insp-other-result-${k}" value="pass" checked> Pass</label>
        <label><input type="radio" name="insp-other-result-${k}" value="fail"> Fail</label>
      </div>
      <input class="insp-other-comment" placeholder="Comment" aria-label="Comment for other item">
    </div>`;
  });
  $("#insp-submit").addEventListener("click", async () => {
    if (!navigator.onLine) {
      showNotice("Inspections are online-only — connect to submit. Nothing was queued.");
      return;
    }
    const monthYear = $("#insp-month").value.trim();
    if (!monthYear) { showNotice("Month / Year is required."); return; }
    const items = [];
    for (let n = 1; n <= INSPECTION_ITEMS.length; n++) {
      const applies = $("#insp-applies-" + n).checked;
      const checked = $$(`input[name="insp-result-${n}"]:checked`);
      items.push({item_no: n, applies,
        ...(applies ? {result: checked.length ? checked[0].value : "pass"} : {}),
        comment: $("#insp-comment-" + n).value});
    }
    $$(".insp-other-row").forEach(row => {
      const text = row.querySelector(".insp-other-text").value.trim();
      if (!text) return;  // blank other rows are skipped, not submitted
      const checked = row.querySelectorAll('input[type="radio"]:checked');
      items.push({item_no: "other", text, applies: true,
        result: checked.length ? checked[0].value : "pass",
        comment: row.querySelector(".insp-other-comment").value});
    });
    try {
      await api("/api/inspections", {method: "POST", body: {
        vehicle_id: inspVehicle.id, month_year: monthYear,
        mileage_hours: numOrNull($("#insp-mileage").value),
        tire_front: $("#insp-tire-front").value.trim(),
        tire_rear: $("#insp-tire-rear").value.trim(), items}});
      showNotice(`1800 inspection submitted for ${inspVehicle.bumper} — the VCO review queue has it now.`, false);
      await loadInspectionBadge();
      showView("view-inspections");
      renderInspections();
    } catch (e) {
      showNotice("Submit failed: " + (e.message || e.status));
    }
  });
}

async function renderInspections() {
  const wrap = $("#inspection-list");
  if (!navigator.onLine) {
    wrap.innerHTML = `<div class="card hint">Inspections are online-only — connect to view or submit. (The fleet picture itself still works offline from the cached snapshot.)</div>`;
    return;
  }
  try {
    const r = await api("/api/inspections");
    inspList = r.inspections || [];
    applyInspFilters();
  } catch (e) {
    wrap.innerHTML = `<div class="card hint" style="color:var(--nmc)">Couldn't load inspections — ${esc(e.message || "network error")}.</div>`;
  }
}
function applyInspFilters() {
  let list = inspList;
  if (inspFilter.unit !== "ALL") list = list.filter(i => i.unit === inspFilter.unit);
  if (inspFilter.reviewed === "UNREVIEWED") list = list.filter(i => i.status === "submitted");
  renderInspectionsList(list);
}
function renderInspectionsList(list) {
  const wrap = $("#inspection-list");
  if (!list.length) {
    wrap.innerHTML = `<div class="card hint">No inspections match these filters.</div>`;
    return;
  }
  wrap.innerHTML = list.map(insp => `
    <div class="card">
      <div class="top" style="display:flex;justify-content:space-between">
        <span><b>${esc(insp.bumper)}</b> · ${esc(insp.unit)}</span>
        <span class="st ${insp.status === "reviewed" ? "FMC" : "PMC"}">${esc(insp.status)}</span>
      </div>
      <p class="meta" style="color:var(--dim)">${esc(insp.month_year)} · by ${esc(insp.operator)} · ${esc(fmtTime(insp.created_at))} ·
        ${insp.item_count ?? "?"} items · ${insp.fail_count ?? "?"} discrepanc${(insp.fail_count ?? 0) === 1 ? "y" : "ies"}${insp.reviewed_by ? ` · reviewed by ${esc(insp.reviewed_by)}` : ""}</p>
      <div class="row">
        <button class="btn" data-insp-form="${insp.id}">View / print form</button>
        ${isVCO() && insp.status === "submitted" ? `<button class="btn primary" data-insp-review="${insp.id}">Review</button>` : ""}
      </div>
    </div>`).join("");
  $$("[data-insp-form]").forEach(b => b.addEventListener("click", () => openInspectionForm(Number(b.dataset.inspForm))));
  $$("[data-insp-review]").forEach(b => b.addEventListener("click", async () => {
    try {
      const full = await api("/api/inspections/" + b.dataset.inspReview);
      renderReviewForm(full);
    } catch (e) {
      showNotice("Couldn't open that inspection: " + (e.message || e.status));
    }
  }));
}
function renderReviewForm(full) {
  const insp = full.inspection;
  const discs = full.discrepancies || [];
  $("#inspection-review").innerHTML = `
    <div class="card">
      <h3>Review — ${esc(insp.bumper)} · ${esc(insp.month_year)}</h3>
      <p class="hint">Set each discrepancy's disposition, then submit the review.
      Anything left as "Leave open" stays open. This action is VCO-only.</p>
      ${discs.length ? discs.map(d => `<div class="row" style="margin-bottom:8px">
        <span style="flex:1">${esc(d.description)} <span style="color:var(--dim)">(item ${d.item_no ?? "other"})</span></span>
        <select class="inline" data-disc-id="${d.id}">
          <option value="">Leave open</option>
          <option value="corrected">Corrected</option>
          <option value="delayed">Delayed</option>
          <option value="disposition">Disposition</option>
        </select></div>`).join("")
        : "<p>No discrepancies — nothing to disposition.</p>"}
      <div class="row" style="margin-top:12px">
        <button class="btn primary" id="insp-review-submit">Submit review</button>
        <span id="insp-review-msg" class="hint"></span>
      </div>
    </div>`;
  $("#insp-review-submit").addEventListener("click", async () => {
    const dispositions = $$("#inspection-review [data-disc-id]")
      .filter(s => s.value)
      .map(s => ({discrepancy_id: Number(s.dataset.discId), status: s.value}));
    try {
      await api("/api/inspections/" + insp.id + "/review",
                {method: "POST", body: {dispositions}});
      showNotice(`Inspection for ${insp.bumper} reviewed.`, false);
      $("#inspection-review").innerHTML = "";
      await loadInspectionBadge();
      renderInspections();
    } catch (e) {
      showNotice("Review failed: " + (e.message || e.status));
    }
  });
}
async function openInspectionForm(id) {
  try {
    const full = await api("/api/inspections/" + id);
    const photoUrls = await loadInspectionPhotos(full);
    // replace: the old set is no longer displayed once the form
    // below re-renders, so revoke it now (v1.2 fix pass)
    revokePhotoUrls(currentInspectionPhotoUrls);
    currentInspectionFull = full;
    currentInspectionPhotoUrls = photoUrls;
    $("#inspection-form-body").innerHTML = inspectionFormHTML(full, photoUrls);
    renderDiscPhotoTools(full);
    showView("view-inspection-form");
  } catch (e) {
    showNotice("Couldn't open that inspection: " + (e.message || e.status));
  }
}
/* Photos load on demand when the form view renders — never embedded
   in the fleet snapshot or the inspection JSON (URLs/counts only). */
async function loadInspectionPhotos(full) {
  const out = {vehicle: null, byDiscrepancy: {}};
  const veh = (fleet.vehicles || []).find(x => x.id === full.inspection.vehicle_id);
  if (veh && veh.photo)
    out.vehicle = await fetchPhoto("/api/vehicles/" + veh.id + "/photo");
  for (const d of (full.discrepancies || [])) {
    const urls = [];
    for (const p of (d.photos || [])) {
      const src = await fetchPhoto(p.url);
      if (src) urls.push(src);
    }
    out.byDiscrepancy[d.id] = urls;
  }
  return out;
}
/* Attach controls live OUTSIDE the printable document (a separate div
   in the form view), so Print / Save-as-PDF never captures buttons
   or file inputs — only the form + its photos. */
function renderDiscPhotoTools(full) {
  const wrap = $("#inspection-photo-tools");
  const discs = full.discrepancies || [];
  if (!discs.length) { wrap.innerHTML = ""; return; }
  wrap.innerHTML = `<div class="card"><h3>Discrepancy photos</h3>
    <p class="hint">Attach up to 4 photos per discrepancy (any signed-in role).
    Photos print with the form. Uploads are online-only.</p>
    ${discs.map(d => {
      const n = (d.photos || []).length;
      return `<div class="row" style="margin-bottom:8px">
        <span style="flex:1">${esc(d.description)} <span style="color:var(--dim)">(${n}/4 photos)</span></span>
        <input type="file" accept="image/*" capture="environment" data-disc-file="${d.id}" aria-label="Photo for discrepancy ${d.id}">
        <button class="btn" data-disc-upload="${d.id}" ${n >= 4 ? "disabled" : ""}>Attach photo</button>
      </div>
      ${(d.photos || []).map((p, i) => `<div class="row" style="margin-bottom:8px">
        <span style="flex:1">Photo ${i + 1} <span style="color:var(--dim)">(discrepancy ${d.id})</span></span>
        <button class="btn danger" data-disc-remove="${p.id}" data-disc-id="${d.id}">Remove photo</button>
      </div>`).join("")}`;
    }).join("")}</div>`;
  $$("[data-disc-upload]").forEach(b => b.addEventListener("click", async () => {
    const did = b.dataset.discUpload;
    const input = $(`[data-disc-file="${did}"]`);
    const file = input && input.files[0];
    if (!file) { showNotice("Choose a photo first."); return; }
    if (!navigator.onLine) { showNotice("Discrepancy photos are online-only — connect to attach."); return; }
    let dataUrl = await downscaleImage(file);
    if (!dataUrl) {
      try { dataUrl = await readFileAsDataURL(file); }
      catch (e) { showNotice("Could not read that photo file."); return; }
    }
    // 3 MB client check on both paths, same as vehicle photos
    if (dataUrl.length > 3145728) { showNotice("That photo is too large (3 MB cap)."); return; }
    try {
      await api(`/api/inspections/${full.inspection.id}/discrepancies/${did}/photos`,
                {method: "POST", body: {image: dataUrl}});
      showNotice("Discrepancy photo attached.", false);
      openInspectionForm(full.inspection.id);
    } catch (e) { showNotice("Photo upload failed: " + (e.message || e.status)); }
  }));
  $$("[data-disc-remove]").forEach(b => b.addEventListener("click", async () => {
    const did = b.dataset.discId, pid = b.dataset.discRemove;
    if (!navigator.onLine) { showNotice("Discrepancy photos are online-only — connect to remove."); return; }
    if (!confirm("Remove this photo?")) return;
    try {
      await api(`/api/inspections/${full.inspection.id}/discrepancies/${did}/photos/${pid}`,
                {method: "DELETE"});
      showNotice("Discrepancy photo removed.", false);
      openInspectionForm(full.inspection.id);
    } catch (e) { showNotice("Photo remove failed: " + (e.message || e.status)); }
  }));
}
function printInspectionForm() {
  if (!currentInspectionFull) return;
  $("#print-area").innerHTML = inspectionFormHTML(
    currentInspectionFull,
    currentInspectionPhotoUrls || {vehicle: null, byDiscrepancy: {}});
  window.print();
}
/* The VCO's notification for inspections: the unreviewed count badge
   on nav + home. Only the VCO sees it; a failed load hides it rather
   than showing a stale count. */
async function loadInspectionBadge() {
  const badge = $("#inspection-badge");
  const banner = $("#home-insp-banner");
  if (!isVCO()) {
    badge.classList.add("hidden");
    banner.classList.add("hidden");
    return;
  }
  try {
    const r = await api("/api/inspections?reviewed=false");
    const n = (r.inspections || []).length;
    badge.textContent = n;
    badge.classList.toggle("hidden", n === 0);
    banner.innerHTML = n ? `<button class="btn" id="home-insp-go">${n} inspection${n === 1 ? "" : "s"} awaiting review →</button>` : "";
    banner.classList.toggle("hidden", n === 0);
    const go = $("#home-insp-go");
    if (go) go.addEventListener("click", () => {
      showView("view-inspections"); renderInspections();
    });
  } catch (e) {
    badge.classList.add("hidden");
    banner.classList.add("hidden");
  }
}

/* ============================================================ helpers */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}
function fmtTime(iso) {
  try { return new Date(iso).toLocaleString(); } catch (e) { return iso; }
}
function numOrNull(v) { return v === "" || v == null ? null : Number(v); }

/* ============================================================ wiring */
function wire() {
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("#auth-error").textContent = "";
    try {
      const r = await api("/api/login", {method: "POST", body: {
        username: $("#li-username").value.trim(),
        password: $("#li-password").value}});
      token = r.token; me = r.user;
      localStorage.setItem("skyline_token", token);
      localStorage.setItem("skyline_user", JSON.stringify(me));
      $("#app").classList.remove("hidden");
      await refreshFleet(false); render(); showView("view-home");
      replayQueue();
    } catch (err) { $("#auth-error").textContent = err.message; }
  });
  $("#setup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("#auth-error").textContent = "";
    try {
      const r = await api("/api/setup", {method: "POST", body: {
        username: $("#su-username").value.trim(),
        display_name: $("#su-display").value.trim(),
        password: $("#su-password").value, unit: "18 ASOG"}});
      if (r.token && r.user) {
        // setup returns a session: the new VCO is signed in directly
        token = r.token; me = r.user;
        localStorage.setItem("skyline_token", token);
        localStorage.setItem("skyline_user", JSON.stringify(me));
        $("#app").classList.remove("hidden");
        await refreshFleet(false); render(); showView("view-home");
      } else {
        $("#setup-form").classList.add("hidden");
        $("#login-form").classList.remove("hidden");
        $("#auth-error").textContent = "VCO created — sign in.";
      }
    } catch (err) { $("#auth-error").textContent = err.message; }
  });
  async function doLogout(discardQueue) {
    if (queue.length && !discardQueue) {
      // never silently destroy unsynced work
      showNotice(`You have ${queue.length} unsynced edit(s). Signing out now ` +
                 `would lose them — sync first, or use "Discard edits & sign out".`);
      $("#btn-logout-discard").classList.remove("hidden");
      return;
    }
    try { await api("/api/logout", {method: "POST"}); } catch (e) {}
    token = ""; me = null; fleet = null; queue = [];
    localStorage.removeItem("skyline_token"); localStorage.removeItem("skyline_user");
    localStorage.removeItem("skyline_snapshot"); localStorage.removeItem("skyline_queue");
    localStorage.removeItem("skyline_last_sync");
    location.reload();
  }
  $("#btn-logout").addEventListener("click", () => doLogout(false));
  $("#btn-logout-discard").addEventListener("click", () => doLogout(true));
  $("#btn-home").addEventListener("click", () => { currentUnit = null;
    renderTiles(); showView("view-home"); });
  $$("[data-back]").forEach(b => b.addEventListener("click", () => {
    currentUnit = null; renderTiles(); showView("view-home"); }));
  $$("[data-nav]").forEach(b => b.addEventListener("click", async () => {
    const v = b.dataset.nav;
    if (v === "audit") { showView("view-audit"); renderAudit(); }
    if (v === "conflicts") { await loadConflicts(); showView("view-conflicts"); renderConflicts(); }
    if (v === "vco") { showView("view-vco"); renderVCO(); }
    if (v === "inspections") { showView("view-inspections"); renderInspections(); }
  }));
  $("#veh-back").addEventListener("click", () => openUnit(currentUnit || "18 ASOG"));
  $("#veh-search").addEventListener("input", renderVehicleList);
  $$("#status-filter button").forEach(b => b.addEventListener("click", () => {
    statusFilter = b.dataset.f;
    $$("#status-filter button").forEach(o => o.classList.toggle("on", o === b));
    renderVehicleList();
  }));
  $("#btn-add-vehicle").addEventListener("click", () => {
    // unit picker over all five units, preselected to the unit whose
    // screen you're on (v1.1: 18 ASOG holds its own vehicles too)
    const sel = $("#av-unit");
    sel.innerHTML = UNITS.map(u =>
      `<option ${u === currentUnit ? "selected" : ""}>${u}</option>`).join("");
    $("#add-form").classList.toggle("hidden");
  });
  $("#av-cancel").addEventListener("click", () =>
    $("#add-form").classList.add("hidden"));
  $("#add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const unit = $("#av-unit").value;
    if (!unit) { showNotice("Pick which squadron this vehicle belongs to."); return; }
    const tempId = "tmp-" + Date.now();
    const payload = {_tempId: tempId, unit,
      bumper: $("#av-bumper").value.trim(), type: $("#av-type").value.trim(),
      status: "FMC", mileage: numOrNull($("#av-mileage").value),
      location: $("#av-location").value, maint_due_date: $("#av-due").value,
      maint_due_miles: numOrNull($("#av-due-miles").value),
      notes: $("#av-notes").value};
    const local = {...payload, id: tempId, is_demo: 0,
      updated_by: me.display_name, updated_at: new Date().toISOString()};
    delete local._tempId;
    await queueOrSend("create", payload, () => fleet.vehicles.push(local));
    $("#add-form").classList.add("hidden");
    e.target.reset();
  });
  $("#user-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/users", {method: "POST", body: {
        username: $("#uf-username").value.trim(),
        display_name: $("#uf-display").value.trim(),
        password: $("#uf-password").value,
        unit: $("#uf-unit").value, role: $("#uf-role").value}});
      $("#uf-msg").textContent = "Account created.";
      e.target.reset(); renderVCO();
    } catch (err) { $("#uf-msg").textContent = err.message; }
  });
  $("#auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {unit: $("#af-unit").value, vehicle_type: $("#af-type").value.trim(),
      authorized: Number($("#af-count").value)};
    await queueOrSend("authorization", payload, () => {
      const ex = fleet.authorizations.find(a =>
        a.unit === payload.unit && a.vehicle_type === payload.vehicle_type);
      if (ex) ex.authorized = payload.authorized;
      else fleet.authorizations.push({id: 0, is_demo: 0, ...payload});
    });
    renderVCO();
  });
  $("#btn-demo-load").addEventListener("click", async () => {
    try {
      await api("/api/demo/load", {method: "POST"});
      await refreshFleet();
      $("#demo-msg").textContent = "Demo fleet loaded (all vehicles labeled SAMPLE).";
    } catch (e) {
      $("#demo-msg").textContent = "Demo load failed: " + (e.message || e.status);
      showNotice("Demo load failed: " + (e.message || e.status));
    }
  });
  $("#btn-demo-remove").addEventListener("click", async () => {
    try {
      await api("/api/demo/remove", {method: "POST"});
      await refreshFleet();
      $("#demo-msg").textContent = "Demo fleet removed.";
    } catch (e) {
      $("#demo-msg").textContent = "Demo remove failed: " + (e.message || e.status);
      showNotice("Demo remove failed: " + (e.message || e.status));
    }
  });
  $("#btn-print").addEventListener("click", () => { renderPrint(); window.print(); });
  $("#insp-filter-unit").addEventListener("change", (e) => {
    inspFilter.unit = e.target.value; applyInspFilters();
  });
  $("#insp-filter-reviewed").addEventListener("change", (e) => {
    inspFilter.reviewed = e.target.value; applyInspFilters();
  });
  $("#btn-insp-print").addEventListener("click", printInspectionForm);
  $("#insp-form-back").addEventListener("click", () => {
    showView("view-inspections"); renderInspections();
  });
  $("#insp-new-back").addEventListener("click", () => {
    if (inspVehicle) openVehicle(inspVehicle);
    else { currentUnit = null; renderTiles(); showView("view-home"); }
  });
  $("#btn-csv").addEventListener("click", async (e) => {
    e.preventDefault();
    try {
      const r = await fetch("/api/export.csv",
                            {headers: {Authorization: "Bearer " + token}});
      if (!r.ok) throw new Error("export failed (HTTP " + r.status + ")");
      const b = await r.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(b); a.download = "skyline_fleet.csv"; a.click();
    } catch (err) {
      showNotice("CSV export failed: " + err.message);
    }
  });

  $("#unit-auth-add").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {unit: currentUnit,
                     vehicle_type: $("#uaa-type").value.trim(),
                     authorized: Number($("#uaa-count").value)};
    if (!payload.vehicle_type || !Number.isInteger(payload.authorized)
        || payload.authorized < 0) {
      showNotice("Enter a type name and a whole-number authorized count (0 or more).");
      return;
    }
    const result = await queueOrSend("authorization", payload, () => {
      const ex = fleet.authorizations.find(a =>
        a.unit === payload.unit && a.vehicle_type === payload.vehicle_type);
      if (ex) { ex.authorized = payload.authorized; ex.is_demo = 0; }
      else fleet.authorizations.push({id: 0, is_demo: 0, ...payload});
    });
    if (result === "rejected") return;  // rejection already shown; keep the typed values (fix F1)
    e.target.reset();
    showNotice(result === "queued"
      ? `Authorization queued — will sync when online: ${payload.unit} ${payload.vehicle_type} = ${payload.authorized}.`
      : `Authorization added: ${payload.unit} ${payload.vehicle_type} = ${payload.authorized}.`, false);
  });

  /* ---- v1.1: UTC + import wiring (UTC mutations are online-only) */
  const utcOnlineOnly = () => {
    if (navigator.onLine) return true;
    showNotice("UTC changes need a connection — viewing works offline, changes don't (v1.1).");
    return false;
  };
  $("#btn-new-utc").addEventListener("click", () =>
    $("#utc-form").classList.toggle("hidden"));
  $("#utc-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!utcOnlineOnly()) return;
    try {
      const res = await api("/api/utcs", {method: "POST", body: {
        unit: currentUnit, code: $("#utc-code").value,
        title: $("#utc-title-input").value}});
      $("#utc-form").classList.add("hidden");
      e.target.reset();
      await refreshFleet(false); render();
      openUtc(res.utc.id);
    } catch (err) { showNotice("UTC create failed: " + (err.message || err.status)); }
  });
  $("#btn-import").addEventListener("click", () => $("#import-file").click());
  $("#import-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) await startImport(file);
  });
  $("#utc-back").addEventListener("click", () => {
    currentUtcId = null;
    openUnit(currentUnit);
  });
  $("#utc-save").addEventListener("click", async () => {
    const u = currentUtc();
    if (!u || !utcOnlineOnly()) return;
    try {
      await api(`/api/utcs/${u.id}`, {method: "PUT", body: {
        unit: u.unit, code: $("#utc-edit-code").value,
        title: $("#utc-edit-title").value}});
      await refreshFleet(false); renderUtc();
      showNotice("UTC saved.", false);
    } catch (e) { showNotice("UTC save failed: " + (e.message || e.status)); }
  });
  $("#utc-add-confirm").addEventListener("click", async () => {
    const u = currentUtc();
    if (!u || !utcOnlineOnly()) return;
    const picked = $$("#utc-add-list input[data-utc-add]:checked")
      .map(i => Number(i.dataset.utcAdd));
    if (!picked.length) { showNotice("Tick at least one vehicle to add."); return; }
    try {
      for (const vid of picked)
        await api(`/api/utcs/${u.id}/vehicles`, {method: "POST",
                                                 body: {vehicle_id: vid}});
      await refreshFleet(false); renderUtc();
      showNotice(`${picked.length} vehicle(s) added to ${u.code}.`, false);
    } catch (e) { showNotice("Add failed: " + (e.message || e.status)); }
  });
  $("#utc-delete").addEventListener("click", async () => {
    const u = currentUtc();
    if (!u || !utcOnlineOnly()) return;
    if (!confirm(`Delete UTC ${u.code}? The vehicles themselves are NOT deleted — only the package.`)) return;
    try {
      await api(`/api/utcs/${u.id}`, {method: "DELETE"});
      currentUtcId = null;
      await refreshFleet(false);
      openUnit(currentUnit);
      showNotice("UTC deleted. Its vehicles were kept.", false);
    } catch (e) { showNotice("Delete failed: " + (e.message || e.status)); }
  });
}

/* ============================================================ boot */
(async function boot() {
  wire();
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
  renderSyncPill();
  try {
    const st = await api("/api/setup/status");
    if (st.setup_needed) {
      $("#setup-form").classList.remove("hidden");
      $("#login-form").classList.add("hidden");
    }
  } catch (e) { /* offline first load: fall through to cached session */ }
  if (token && me) {
    $("#app").classList.remove("hidden");
    showView("view-home");
    if (fleet) render();
    refreshFleet(false).then(() => { render(); replayQueue(); })
      .catch(() => {});
  } else {
    showView("view-auth");
  }
})();
