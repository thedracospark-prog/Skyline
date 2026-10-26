# Skyline v1 — Build Notes

Built 2026-10-23. Runnable, tested, no dependencies beyond Python 3.

## Architecture

- **Backend:** `server.py` — Python 3 standard library only
  (`http.server.ThreadingHTTPServer` + `sqlite3`). One file holds routing,
  auth, derivations, demo data and the API. No pip installs, no build step.
- **Database:** SQLite (`skyline.db`, created on first run). Tables: users,
  sessions, units (seeded: 18 ASOG + 14/15/19/20 ASOS), vehicles,
  authorizations, audit, conflicts.
- **Frontend:** vanilla JS SPA in `static/` (index.html / app.js / style.css),
  installable PWA (manifest + service worker). The app icon master was
  copied to `assets/` and the PWA icon sizes (512/192/maskable/apple-touch/
  favicon) were generated from it with PIL into `static/icons/`.
- **Auth:** individual accounts, no public signup; first-run setup creates
  the initial Admin. Passwords: PBKDF2-HMAC-SHA256, 200k iterations,
  per-user salt. Bearer session tokens (revoked on sign-out).
  Roles enforced server-side on every write: viewer (read-only),
  editor (vehicle + authorization writes, conflict resolution), admin
  (accounts + demo data). Unit affiliation is attribution only.
- **Derivations (server-side, single source of truth):** on-hand counted
  from vehicle records; MC rate = FMC ÷ on-hand × 100; 18 ASOG stats are
  the rollup of the four squadrons. Authorizations are the only hand-set
  totals (per unit per vehicle type).
- **Audit:** every create/update/delete, authorization change, account
  change and conflict event writes an audit row (actor, field, old → new,
  timestamp). Vehicle create/update also stamps updated_by / updated_at.
- **Offline / sync:** service worker caches the shell; the last fleet
  snapshot and an edit queue live in localStorage; the header pill shows
  offline / queued / synced state. Offline edits apply locally and replay
  on reconnect. Status updates carry the client's `base_status`: if the
  server's status has moved, the status is NOT overwritten — other fields
  merge, a conflict row + flagged audit entry are written, and an Editor
  resolves it in the Conflicts screen (keep current / apply attempted).
- **Exports:** `GET /api/export.csv` (full fleet CSV, SAMPLE column marks
  demo rows) and a print-CSS rollup view for PDF-via-print.
- **Screens:** login/setup, home tiles (ASOG rollup + 4 squadron squares
  with count-up MC% and sweeping rate bars), unit screen (fleet health
  visualization, search, status filter, vehicle cards), vehicle page
  (big status buttons; read-only for viewers), audit log, conflicts,
  admin (accounts, authorizations, demo load/remove). ~250 ms view/status
  transitions; animations are CSS/JS-side only and never gate saving.
- **Demo data:** starts empty. Admin-only load/remove; 18 vehicles across
  the squadrons, every one bumper-prefixed `SAMPLE-` with SAMPLE notes,
  plus matching sample authorizations (flagged `is_demo` and removed with
  the demo fleet).

## Test results

- `python3 -m unittest discover -s tests` — **11 tests, all passing**
  (5.8 s). Coverage: password hashing/verification + salt uniqueness;
  setup-only-once; bad-login rejection; auth required (no public access);
  viewer cannot create/edit (403) while editor can; MC-rate math (2 FMC /
  4 on-hand = 50.0%) and on-hand derivation + group rollup; audit entries
  written for create and field updates with actor attribution; sync
  conflict — conflicting offline status is flagged, server status kept,
  other fields merged, resolution applies the attempted status; demo
  fleet is SAMPLE-labeled and fully removable.
- **Live smoke test** (real `server.py`, temp DB, curl): shell + sw.js +
  manifest + icons all HTTP 200; setup → login → add vehicle → edit to
  NMC with notes → fleet endpoint returned correct derived stats → audit
  showed 4 entries (account, create, status, notes) → CSV export returned
  the vehicle row. All passed.
- **Frontend checks:** `node --check` passes on app.js; manifest parses as
  JSON. The unit-screen fleet health visualization was exercised headlessly
  in Node with 121 vehicles (incl. an offline temp-id vehicle): all
  buildings rendered, status markers and district groupings present, no
  NaN geometry, empty-fleet state renders.
- One bug found and fixed during testing: the test setUp deleted tables in
  FK-unsafe order, leaving a locked DB; fixed in the test harness (the
  server code was not at fault — a standalone subprocess repro was clean).

## Limitations (honest)

- **No passkeys/biometrics yet.** The `cryptography` package is not
  available in this environment, so — as specified — v1 ships strong
  password auth and documents the WebAuthn/passkey step in the README
  instead of faking it. Biometric unlock after first sign-in remains the
  first production upgrade; it also needs a public HTTPS origin.
- **Not hosted.** Nothing was published or deployed; it runs locally/LAN
  via the single command in the README. Production hosting (always-on
  host + HTTPS + DB backups) is an ops step for Draco.
- Sessions do not expire yet and there is no login rate limiting — noted
  in the README hardening list.
- Offline queue covers vehicle creates/updates and authorizations;
  deletes still require a connection (the UI says so when one fails).
- The frontend was verified by syntax checks and a headless render
  harness, not by clicking through a real browser — first visual
  confirmation happens when Draco opens it.

## Review fix pass (2026-10-23)

Two independent reviews of v1 returned DO-NOT-SHIP. Every proven finding
was fixed, with a regression test per blocker. Nothing was published or
hosted. This section supersedes the "sessions do not expire yet" line in
Limitations above.

### Backend (`server.py`)

1. **Atomic first-run setup** — `/api/setup` now runs inside
   `BEGIN IMMEDIATE`; the user-count check and admin insert are one
   transaction. Regression: 8 parallel setup calls create exactly one
   admin (one 201, seven 403, one user row).
2. **Numeric validation/coercion** — `mileage` / `maint_due_miles` must
   be integers ≥ 0 on INSERT and UPDATE (real ints or digit strings,
   coerced; floats/bools/negatives/junk → 400). Frontend sinks for both
   fields are also escaped (`esc(...)`) as defense in depth.
3. **Group rollup sums** — 18 ASOG now sums squadron on-hand,
   authorizations, FMC, PMC **and NMC**; group MC is computed from the
   sums. Regression asserts group PMC=2/NMC=1 on a mixed fleet.
4. **Conflict resolve guards** — action must be exactly `apply`/`keep`
   (else 400, no state change); an already-resolved conflict → 409.
   Regression covers bogus action, resolve-once, and re-resolve.
5. **Demo-data hygiene** — demo authorizations are insert-only-if-absent
   (`INSERT OR IGNORE`): a real authorization is never overwritten. Any
   real edit of a demo-flagged authorization clears its `is_demo` flag.
   `remove_demo` deletes only rows still flagged demo. `load_demo` is
   replace-clean (purges demo data first), so a second load duplicates
   nothing. Regression walks the whole lifecycle.
6. **Validation on all write paths** — vehicles: unit must be one of the
   four squadrons (18 ASOG rollup-only), bumper non-empty and unique per
   unit (409, including on unit moves), status ∈ FMC/PMC/NMC, type
   non-empty. Authorizations: squadron-only, `authorized` a strict
   integer ≥ 0. `maint_due_date` empty or a real YYYY-MM-DD date. User
   affiliation must be one of the five units.
7. **Global error handling** — malformed JSON → 400 (distinct message),
   bodies capped at 1 MB checked before parsing → 413, non-integer ids
   → 404 JSON, unknown user/vehicle/conflict ids → 404 (user update no
   longer ghost-succeeds), and every API route is wrapped so unexpected
   errors return a JSON 500 instead of a dead request.
8. **Sessions** — 30-day expiry (`expires_at`, checked on every auth;
   sessions minted before this change have no expiry recorded and are
   honored until they sign out). Password resets and role changes
   revoke that user's sessions. The last Admin cannot be demoted,
   including self-demotion → 400.
9. **Audit correctness** — a no-op PUT writes no audit rows and leaves
   `updated_by`/`updated_at` untouched (update path only writes when a
   value actually changed). Authorization changes log real old → new;
   account updates log per-field old → new. Deleting a vehicle closes
   its open conflicts and audits the closure (`conflict-close`).
10. **DB robustness / CSV** — `busy_timeout = 5000` on every connection
    (plus WAL); read-modify-write paths (setup, vehicle update, user
    update, conflict resolve, authorization upsert, vehicle delete,
    demo load/remove) run under `BEGIN IMMEDIATE`. CSV export
    neutralizes formula injection: cells starting with `= + - @` are
    prefixed with a single quote.

### Frontend (`static/`)

11. **Offline queue rewrite** — every op carries a client op UUID;
    creates send it as `op_id` and the server dedups on it
    (`vehicles.client_op`, unique partial index), so replays are
    idempotent. When a queued create lands, later queued ops referencing
    its temp id are rewritten to the real id before replay. Replay has a
    re-entrancy guard, persists the queue after every mutation, drops
    permanent 4xx with a visible notice (and resyncs), and retries only
    transient/network/5xx. Sign-out is blocked with a warning while
    unsynced edits exist, with a deliberate "Discard edits & sign out"
    second step.
12. **Stats consistency** — unit/group stats are derived client-side
    from the vehicle list on every render (`deriveStats()`, same formula
    as the server incl. summed PMC/NMC); the UI no longer reads the
    server's per-unit stats block. Re-render happens after `sendOp` and
    after `replayQueue` completes.
13. **Sync pill honesty** — "Synced" (with the last-sync time) shows
    only after a successful fetch/sync; other states are Syncing,
    Offline (cached), and "Can't reach server — showing cached data".
    A failed or 401 refresh flips the pill instead of rendering silently.
14. **Fleet health viz at scale** — layout is computed first and the SVG
    canvas grows to fit: buildings narrow (min 9 px) and the wrap
    scrolls horizontally past 920 px, so all vehicles render at 100+
    (verified at 150: max building end 1847 ≤ canvas 1880). A caption
    shows the vehicle count and status mix; buildings are
    keyboard-focusable/activatable (tabindex, role, Enter/Space).
15. **Silent-failure cluster** — role change, demo load/remove, conflict
    resolve, audit load, conflicts load, and CSV export all have
    try/catch + visible feedback (notice bar / inline message / error
    state). CSV checks `response.ok` before downloading; a failed audit
    fetch shows an error card, not "No audit entries yet."
16. **Should-fixes** — Add Vehicle from the 18 ASOG rollup now has a
    required unit picker with no silent default (and the vehicle-page
    unit select offers squadrons only). Service worker bumped to
    `skyline-shell-v2`, network-first for app files so new versions are
    picked up, cache as offline fallback. MC sweep/count-up shortened
    to ~280–300 ms and only animates when the value changed (bar CSS
    transition likewise 0.3 s); the status-flash class is applied on
    status change (detail card + list card). Zero-vehicle units show
    MC "—". Server stats carry `includes_demo` per unit and tiles,
    chips, and the print rollup show an "incl. SAMPLE data" badge.
    Form inputs gained `aria-label`s. The status filter resets when
    switching units. First-run setup returns a session and the new
    Admin is signed in directly.

### Verification

- `python3 -m unittest discover -s tests` — **32 tests, all passing**
  (18.3 s): the original 11, plus 10 new fix-pass regression tests in
  `FixPassTests` (which also re-runs the inherited API suite against
  the fixed code). Coverage includes the parallel setup race,
  demo-over-real-authorization protection, group PMC/NMC sums,
  conflict re-resolve, session revocation/expiry, last-admin guard,
  no-op audit, CSV injection, and op-UUID dedup.
- `node tests/frontend_check.js` — **17 checks, all passing**: the real
  `app.js` queue/stats/viz/pill logic driven in Node (DOM/localStorage
  stubs) against a live server — offline create + temp-id update drains
  cleanly with exactly one server vehicle, op-UUID replay dedup,
  derived group stats, 150-building viz with no off-canvas truncation,
  and honest pill states.
- `node --check` passes on `app.js`, `sw.js`, and the harness.
- **Live smoke test** (real `server.py` from the CLI, temp DB, HTTP):
  all static assets 200 and SW reports `skyline-shell-v2`; 8 parallel
  setup calls → one admin, and the setup token authenticates
  immediately; a real authorization (14 ASOS/JLTV = 7) survives demo
  load and demo remove, second load idempotent (18 → 18 vehicles);
  group rollup on a mixed fleet reads FMC 1 / PMC 2 / NMC 1 / MC 25.0;
  a queued-style create with `op_id` replays to the same vehicle id
  (dedup) and a follow-up update lands on the real id. **All passed.**

### Not fixed / unchanged

- Nothing in this list was left unfixed. Two deliberate notes:
  pre-existing sessions (minted before this pass) have no recorded
  expiry and remain valid until sign-out — all new sessions carry the
  30-day TTL. Passkeys/biometrics remain the documented production
  upgrade (see Limitations); the password + session layer they will
  sit on is what this pass hardened.

## Fix pass 2 (2026-10-23)

A verification re-review of the fix pass confirmed 13/15 claims but
proved two concurrency blockers and one should-fix with live probes.
All three are fixed here, each with barrier-synchronized regression
tests. Nothing published or hosted.

### Fixes (`server.py`)

1. **Bumper uniqueness is now concurrency-safe (was BLOCKING).**
   `_insert_vehicle` ran its uniqueness check-then-insert outside any
   transaction, so parallel identical creates all passed the check and
   inserted duplicate (unit, bumper) rows with 201s. The op-id dedup
   check, bumper check, insert, audit and re-read now run inside the
   existing `immediate_tx` (`BEGIN IMMEDIATE`) mechanism; any
   `IntegrityError` from the new DB constraint still translates to the
   same 409 / dedup answers. The unit-move uniqueness check on update
   already ran inside `immediate_tx` at the `api_put` level, and
   `_update_vehicle` now moves unit+bumper in ONE UPDATE statement so
   the new unique index never sees a transient duplicate pair (unit
   changed first while the old bumper still collides in the target
   unit). Backstop: a `UNIQUE(unit, bumper)` index
   (`idx_vehicles_unit_bumper`, created in `init_db`).
   **Migration story:** if a database from an earlier build already
   contains duplicate (unit, bumper) pairs, index creation fails by
   design — `_ensure_bumper_unique_index` catches that, logs a loud
   `SKYLINE WARNING` to stderr naming every duplicate pair (e.g.
   `14 ASOS/LEG-1 x2`), and startup continues WITHOUT the index (the
   transactional enforcement still applies); cleaning the duplicates
   and restarting enables the constraint. Existing rows are never
   touched by the migration.
2. **Conflict resolve is resolve-once under concurrency (was BLOCKING).**
   `_resolve_conflict` read the conflict and checked `resolved` BEFORE
   entering `immediate_tx`, so parallel resolves all saw `resolved=0`,
   all returned 200, and each wrote its own (contradictory) resolution
   audit row. The fetch, action validation, and resolved-check now all
   happen INSIDE the write transaction: exactly one resolver wins
   (200), the rest get 409, exactly one resolution audit row is written,
   and the vehicle's final status is the winner's action.
3. **DELETE with a request body no longer desyncs keep-alive (should-fix).**
   `do_DELETE`/`api_delete` never read the body, so its bytes were
   parsed as the start of the next request on the persistent connection,
   corrupting it (non-JSON response). `do_DELETE` now drains the body
   first (`_discard_body`): a well-formed body up to the 1 MB cap is
   fully read and discarded before responding, so every response stays
   JSON and the connection stays usable; an invalid or oversize
   Content-Length is answered 400/413 with the connection marked for
   close so leftover bytes can never desync a following request.

### Verification

- `python3 -m unittest discover -s tests` — **47 tests, all passing**
  (29.2 s): the previous 32, the 9 inherited `ApiTests` re-run under
  the new class, and **6 new fix-pass-2 regression tests** in
  `FixPass2Tests`:
  - 10 barrier-synchronized parallel identical creates → one 201,
    nine 409, exactly one vehicle row;
  - parallel creates with the same bumper in all four different
    squadrons → all 201;
  - `idx_vehicles_unit_bumper` exists on a fresh DB, and a legacy DB
    pre-loaded with duplicate (unit, bumper) rows inits without
    crashing — rows preserved, index skipped, duplicates named in
    the stderr warning;
  - parallel unit-moves of two vehicles onto the same target
    (unit, bumper) → one 200, one 409, one row;
  - 10 barrier-synchronized parallel resolves of one conflict → one
    200, nine 409, exactly one `conflict resolved:` audit row, vehicle
    status equals the winner's applied status;
  - DELETE with a body → 200 JSON, followed by a GET and a
    DELETE-with-body 404 on the SAME `http.client` persistent
    connection — all clean JSON.
- `node tests/frontend_check.js` — **17 checks, all passing**
  (unchanged frontend; re-run to confirm no regression).
- `node --check` passes on `app.js`, `sw.js`, and the harness.
- **Live smoke test** (real `server.py` from the CLI, temp DBs,
  `/tmp/smoke_fixpass2.py`) — **12/12 checks passed**: 10 parallel
  creates → one 201 / nine 409 / one row; conflict built, 10 parallel
  resolves → one 200 / nine 409 / one resolution audit row / status
  NMC applied; DELETE with body → 200 JSON and the same connection
  then serves a clean fleet JSON with the vehicle gone; and a server
  booted against a legacy DB containing duplicate bumpers serves
  normally while logging the warning naming the duplicate pair.

## v1.1 (2026-10-23) — ASOG's own fleet, per-unit authorizations, UTCs, Excel import, banner branding

Draco's five requests after reviewing v1. All v1 guarantees preserved
(roles, audit-on-every-change, offline queue semantics, concurrency
fixes); v1 tests updated only where the new spec intentionally changes
behavior (ASOG vehicle/authorization writes now succeed; the snapshot's
group sums moved to a dedicated `group` entry — see below).

### 1. 18 ASOG has its own vehicles
- `validate_vehicle` and authorization upserts now accept any of the five
  units (was: squadrons only). All other validation unchanged (bumper
  non-empty + unique per unit, status enum, numeric/date checks).
- **Snapshot semantics:** `units[]` entries are now each unit's OWN stats
  (18 ASOG's entry = ASOG's own fleet); a new top-level **`group`** entry
  carries the rollup = sums over ALL FIVE units incl. ASOG's own, with
  the group `includes_demo` OR and group MC rate. (In v1 the ASOG
  `units[]` entry WAS the group sums — that conflation is gone.)
- Client mirrors it exactly: `ownStats(name)` per unit, `groupStats()`
  for the rollup; `deriveStats()["18 ASOG"]` stays the group (home
  tiles unchanged), `unitVehicles("18 ASOG")` returns ASOG's own only.
  Frontend harness asserts client stats == live server snapshot for
  both the group entry and every unit entry.
- ASOG screen: own stats chips, own vehicle list + viz (its OWN fleet),
  an Add Vehicle flow defaulting to 18 ASOG, plus a labeled "Group
  rollup — all five units" summary card. Vehicle form + add picker now
  list all five units.

### 2. Authorizations editable on the unit screen
- New **Authorizations** section per unit screen: every type the unit
  has an authorization row OR vehicles for, with authorized vs on-hand;
  Editors get inline number + Save and an **Add type** form (works with
  zero vehicles of that type). Viewers read-only. All saves go through
  the existing `POST /api/authorizations` (validated, audited, and they
  queue offline like before — authorization ops were already queueable).
  Admin-screen authorization management untouched; its unit dropdown
  now includes 18 ASOG.

### 3. UTCs (Unit Type Codes)
- Schema: `utcs(id, unit, code, title, created_by, created_at,
  updated_at, UNIQUE(unit, code))`, `utc_vehicles(utc_id, vehicle_id)`
  with FKs. Code: non-empty, ≤ 20 chars (spec said "~12" — 20 chosen as
  the hard cap so longer real-world designators aren't rejected; stated
  here rather than silently).
- API (Editors only, all audited: `utc-create`, `utc-update`,
  `utc-delete`, `utc-add-vehicle`, `utc-remove-vehicle`):
  `POST /api/utcs`, `PUT/DELETE /api/utcs/{id}`,
  `POST /api/utcs/{id}/vehicles`, `DELETE /api/utcs/{id}/vehicles/{vid}`.
  Duplicate code per unit → 409; membership vehicle must exist and
  belong to the UTC's unit → 400. A vehicle may join **multiple** UTCs.
  Deleting a vehicle deletes its memberships in the same transaction;
  deleting a UTC deletes only the package. Moving a vehicle to another
  unit drops memberships whose UTC belongs to a different unit
  (audited). `purge_demo` removes demo UTC memberships with the demo
  vehicles.
- Snapshot includes `utcs` (with `vehicle_ids`), so UTC viewing works
  **offline**; UTC **mutations are online-only** in v1.1 — the UI says
  so plainly when attempted offline (same honesty pattern as vehicle
  deletes). UTC ops are deliberately NOT queued: membership semantics
  against a stale snapshot would be guesswork.
- UI: UTC cards on each unit screen (code, title, assigned count,
  FMC/PMC/NMC mini-counts, MC bar); UTC detail screen reuses the SAME
  visualization via `renderVizInto(wrap, vehicles, emptyMsg)`
  (parameterized from the old `renderViz`), correct at 120 vehicles
  (harness-checked); member list, Editor checklist add-picker,
  remove-member, edit code/title, delete-with-confirm; Viewers
  read-only; empty UTC renders an intentional empty state.
  CSS viz selectors moved from `#viz-wrap` to a shared `.vizwrap` class.

### 4. Excel vehicle import
- Zero new dependencies: minimal `.xlsx` reader on `zipfile` +
  `xml.etree` (sharedStrings + first worksheet, inline strings, numeric
  cells; values only — formulas never evaluated; DOCTYPE parts
  rejected), `.csv` through the same mapping. Caps: 5 MB file (request
  cap 8 MB for base64), 100 zip entries, 20 MB uncompressed, 5,000 rows.
  Malformed files → clean JSON 400.
- Two-step, stateless-ish: `POST /api/import/preview` (Editors only)
  parses + validates and writes NOTHING; the client holds the preview;
  `POST /api/import/confirm` **re-validates every row server-side**
  through the same `validate_vehicle` insert path — duplicates that
  appear between preview and confirm are skipped, never overwritten.
  Audit rows say `imported from <filename> into <unit>`.
- Mapping + rules documented in the README (header alias table,
  add-only rule, asset-number required, status default FMC *shown* in
  the preview, blank type → `Unknown`, unmapped columns reported).
  Dates accept ISO, MM/DD/YYYY, and raw Excel serials.
- Tests build real `.xlsx` fixtures programmatically (zipfile + XML,
  no openpyxl): happy path, header variants + extra columns, missing
  asset # skipped, in-file + existing duplicates skipped (existing row
  proven unmodified), bad status flagged, malformed 400, wrong
  extension 400, viewer 403 on both steps, confirm re-validation,
  import audit detail, ASOG import, CSV path.

### 5. Banner branding (Draco, mid-build)
- Top bar rebuilt as a 3-column grid (`1fr auto 1fr`): the Cooper icon
  (48 px, was 34) + "Skyline" (1.6 rem, weight 800) sit in the center
  track — truly centered at every width; nav lives in the left track,
  sync pill / user chip / sign-out in the right. At ≤ 760 px the grid
  collapses to one column: brand centered on its own row (42 px icon),
  nav and controls centered below it, user chip hidden for space.
  Nav buttons now have `min-height: 44px` tap targets.
- Login screen matches: icon 132 → 168 px, app name 1.7 → 2.3 rem / 800.
- Layout verified by construction + CSS review and a 390 px-width
  composition check in the smoke script (grid tracks + media query
  present, no fixed widths in the bar). No real-browser click-through
  exists in this environment — same honest limit as v1.
- Service-worker shell cache bumped to `skyline-shell-v3`.

### Verification (v1.1)
- `python3 -m unittest discover -s tests` — **76 tests, all passing**
  (47 v1 + 29 new: `V11Tests` — ASOG CRUD/group parity incl. PMC/NMC,
  authorizations for all five units, UTC CRUD + membership rules +
  audit; `ImportTests` — 6 import tests; both classes also re-run the
  inherited `ApiTests` set).
- `node tests/frontend_check.js` — **30 checks, all passing** (17 v1 +
  13 new: live-snapshot parity for group + all five unit entries, ASOG
  own vs group semantics, UTC viz exact-set / caption / empty / 120
  buildings).
- `node --check` on `app.js`, `sw.js`, harness — pass.
- **Live smoke** (fresh server from the CLI, `/tmp/smoke_v11.py`) —
  **26/26 checks passed**: ASOG vehicle create + own/group stats,
  per-unit authorizations (+ viewer 403), UTC create/dupe-409/
  wrong-unit-400/membership-in-snapshot/delete-keeps-vehicles, Excel
  import preview (FMC default shown, missing-asset skipped) → confirm
  → vehicles landed + import audit rows, and the banner composition
  (centered-brand markup, 3-column grid, ≤760 px collapse, 48 px icon /
  1.6 rem name, ≥ 44 px nav targets, enlarged login branding,
  `skyline-shell-v3`).

### v1.1 verification addendum (2026-10-23)

The verification re-review returned **SHIP** (all 7 verified live) with
one new minor finding: a cell ref with Unicode-expanding letters
(ß → "SS" under .upper(), ligatures) crashed ord() → 500 instead of
the clean 400. Fixed in `_xlsx_rows`: non-ASCII column letters are
rejected with ValidationError before the column computation. Probed:
ß and ﬀ refs → ValidationError; legit sheets and the B1 attack file
unaffected.

### v1.1 review fix pass (2026-10-23) — B1, S1–S4, F1, F2

Two independent reviews of v1.1 returned SHIP-AFTER-FIXES. Fixes:

- **B1 (BLOCKING) — xlsx sparse-column memory exhaustion.**
  `_xlsx_rows` materialized each row as a list as wide as its widest
  cell ref, so a ~300-byte sheet with one cell at `ZZZZZZ1` (column
  ≈ 321M) forced a multi-GB allocation. Fix: new cap
  `IMPORT_MAX_COLS = 512` (Excel's own max is 16,384, but a fleet
  sheet uses a handful of columns, and 512 bounds a full 5,000-row
  sheet to ~2.5M materialized cells worst case). The running column
  value is checked *while parsing the ref letters* — it only grows
  per letter, so a hostile ref is rejected within a few characters,
  before any wide list exists; the no-ref fallback position is
  checked too. Rejection is a clean JSON 400. The same function was
  audited for other input-sized allocations: part count, summed
  declared uncompressed size, shared-strings list, XML trees, and
  row count were already constant-capped; row width was the only
  unbounded dimension. Regression test + live probe: the attack
  file (690 bytes as built by the fixture) returns 400 in ~0.00 s
  with server RSS flat (28 MB → 28 MB); a legal sheet with a column
  at CV (100) parses normally.
- **S1 — shared-string index wrap.** A cell `t="s"` with `<v>-1</v>`
  indexed `shared[-1]` — the LAST shared string — silently smuggling
  an unrelated value into the import. The index is now range-checked
  (`0 <= idx < len(shared)`); out-of-range (negative, too large, or
  non-numeric) yields a blank cell and parsing continues, so the row
  surfaces in the preview with the usual blank-field defaults/skips
  instead of wrong data. Regression test asserts the type comes out
  "Unknown", never the sentinel last string.
- **S2 — corrupt xlsx parts returned 500.** The `BadZipFile` guard
  wrapped only the `ZipFile()` constructor; `zf.read()` failures
  (e.g. a bad CRC on a part) fell into the catch-all. `read_xml` now
  catches `BadZipFile` / `RuntimeError` / `EOFError` / `OSError`
  from the read and raises the documented clean JSON 400
  ("corrupt .xlsx file"). Regression test corrupts the sheet part's
  stored CRC-32 in the central directory.
- **S3 — UTC code uniqueness was case-sensitive.** Codes are now
  normalized to uppercase in `validate_utc` (create and update —
  designators are canonically uppercase), the duplicate checks in
  create/update compare `UPPER(code)` so a legacy lowercase row in
  an existing DB still collides, and `utc_snapshot` uppercases on
  read so legacy rows display consistently. Tests: create "abc1" →
  stored "ABC1"; "ABC1"/"aBc1" in the same unit → 409; same code in
  another unit → 201; rename onto a case-variant → 409; rename to
  "zz2" → stored "ZZ2".
- **S4 — 413 message hardcoded "1 MB cap".** `_json_body` now names
  the cap it was actually called with (import routes correctly say
  "8 MB cap"); `_discard_body` computes from `MAX_BODY` the same way.
  Regression test asserts both messages on both routes.
- **F1 — lying "saved" notice on rejected authorization saves.**
  `queueOrSend` now resolves to `"accepted"` / `"queued"` /
  `"rejected"` (permanent 4xx — its rejection notice + resync are
  unchanged). The unit-screen authorization Save handler only claims
  "saved" on accepted and says "queued — will sync when online" on
  queued; on rejected it shows nothing, leaving the server's
  rejection notice standing. The add-authorization handler likewise
  (and keeps the typed values on rejection instead of resetting the
  form). The vehicle-edit save message uses the same status
  ("Saved." / "Queued — will sync when online." / "Not saved — the
  server rejected that change."). The vehicle-create and admin
  authorization callers claim no success text and are unchanged.
  Harness checks assert all three resolutions against a live server
  (valid op → accepted; authorized = −3 → rejected; offline →
  queued). The two fixed handlers live past the harness's wiring
  cut, so their branching is covered by the queueOrSend contract
  checks + code inspection.
- **F2 — MC-rate rounding parity.** The server used Python `round()`
  (banker's) while the client uses `Math.round` (half-up), so exact
  ties disagreed (1/16: server 6.2 vs client 6.3). Both sides now
  round half-up: new server helper `mc_rate_half_up` divides the
  exact fraction with `Decimal` and quantizes `ROUND_HALF_UP`
  (used by `unit_stats` and the group entry); the client's existing
  formula was verified half-up (1/16 → 6.3, 5/16 → 31.3, 1/80 →
  1.3 in Node). Python tests assert the ties at the helper and
  through the snapshot; the frontend harness asserts the client
  ties and live parity against a server-seeded 16-vehicle/1-FMC
  fleet (its item-11 duplicate assertion was rescoped to the
  14 ASOS vehicles, since the seed now shares the server).

#### Verification (fix pass)
- `python3 -m unittest discover -s tests` — **91 tests, all passing**
  (76 + 6 new in `V11FixPassTests`, which also re-runs the inherited
  `ApiTests` set; ~56 s).
- `node tests/frontend_check.js` — **38 checks, all passing** (30 +
  8 new: 3 client tie cases, server tie value, live tie parity,
  3 queueOrSend status resolutions).
- `node --check` on `app.js`, `sw.js`, harness — pass.
- **Live smoke** (fresh server from the CLI, `/tmp/smoke_v11fix.py`)
  — **10/10 checks passed**: B1 attack → 400 in ~0.00 s with server
  RSS flat at 28 MB; CRC-corrupt part → 400; negative shared-string
  index → blank type; legit import preview+confirm still lands;
  UTC lowercase create → "ABC1" + case-variant dupe → 409;
  import-route 413 says "8 MB cap"; live snapshot tie 1/16 → 6.3.

## v1.1.1 (2026-10-23) — Editors can delete authorizations

Draco: "editors [must be able to] add and subtract vehicles and
authorizations too" + "there must also be the ability to delete
authorizations." Audit of v1.1 found add/subtract already complete
everywhere (vehicle create/update/delete, authorization create and
lowering — including to 0) **except one gap**: an authorization row
could never be deleted outright. v1.1.1 closes exactly that gap.

### Server (`server.py`)
- `DELETE /api/authorizations/<id>` in `api_delete`, handled by
  `_delete_authorization`: editor/admin only (viewer → 403,
  unauthenticated → 401), unknown or non-integer id → 404. The row is
  deleted inside `immediate_tx`, and the audit entry mirrors the
  `_upsert_authorization` pattern — action `authorization`,
  field `authorized`, old = the removed count, new = "",
  detail `"<unit> / <type> (deleted)"`, actor = the deleter.
- Snapshot math unchanged in code and exact in effect: the deleted
  row simply stops contributing to unit and group authorization
  totals (proven in tests to the row's exact count).
- Demo (`is_demo`) authorization rows delete the same way;
  `/api/demo/remove` afterwards still clears the remaining demo data.

### Client (`static/app.js`)
- Unit-screen Authorizations: rows backed by a real authorization
  record get a **Remove** button (Editors only; types that exist only
  because vehicles carry them have no line to remove). The confirm
  spells it out — the line is deleted entirely, vehicles of the type
  are NOT affected, and setting the number to 0 is the softer option.
- Online-only, following the vehicle-delete pattern: a failed DELETE
  surfaces "Remove failed (needs connection)" and nothing is queued.
  On success the row is filtered from local state and the app
  re-renders, so unit chips, home tiles and group totals drop
  immediately; the success notice fires only after the server accepts
  (same no-lies discipline as the F1 fix).
- Admin-screen authorizations table gets the same Remove control
  (Admin-screen gating unchanged).
- Service-worker cache bumped `skyline-shell-v3` → `v4`.

### Verification (v1.1.1)
- `python3 -m unittest discover -s tests` — **105 tests, all passing**
  (91 + 14 from `V111Tests`: 5 new — editor delete with totals +
  audit, role/unknown/double-delete, vehicles untouched,
  lower-by-number 10 → 4 → 0, demo-row delete + demo-remove
  consistency — plus its 9 inherited `ApiTests` re-runs).
- `node tests/frontend_check.js` — **49 checks, all passing** (38 +
  11 new: Remove renders for an editor, absent for a viewer, Save
  control unaffected; a live DELETE drops the derived unit total to
  0 and the group total by exactly the removed count; the server
  snapshot confirms the row is gone).
- `node --check` on `app.js`, `sw.js`, harness — pass.
- **Live smoke** (fresh CLI-booted server, temp DB): setup → create
  authorization → delete it as an editor → snapshot totals and the
  audit entry verified end to end.

### v1.1.1 review addendum (2026-10-23)

Independent review verdict: **SHIP** (~110 probes: roles, totals
recomputed independently, 8-way parallel delete race → one 200 / one
audit row, delete-vs-upsert races, demo interplay, XSS). One
SHOULD-FIX taken before publish — **S1:** an id larger than SQLite's
int64 range (e.g. 23 digits) overflowed the bind in the shared
`_path_id` helper and returned 500 instead of 404 on the new endpoint
(and, inherited, on the vehicle/UTC deletes). Fixed in `_path_id`
(out-of-range → None → callers answer 404) plus the two UTC
membership paths that parse ids themselves. Probed live post-fix:
oversized ids on authorizations/vehicles/UTCs → 404; normal and
double deletes unchanged. The reviewer's S2 (lenient path tail) is
accepted by design: routes are prefix-matched deliberately because
the nested `/api/utcs/<id>/vehicles/<vid>` membership path needs it,
and every handler re-validates the row before acting.

## Web sim (docs/sim/) — 2026-10-23

The GitHub Pages sim at https://thedracospark-prog.github.io/Skyline/sim/
(Pages serves the repo's /docs folder), linked from the README's
"▶ Try the web sim" section. Same pattern as the Shepherd / Pack Out
sims: **shim, don't fork**.

- `docs/sim/` holds byte-identical copies of `static/`'s `app.js` and
  `style.css` (verified by md5 and asserted in tests) plus
  `index.html`, `manifest.json`, `icons/`. The sim copy of
  `index.html` differs only by: the title gains "— Web Sim", asset
  paths are made relative (root-absolute paths would 404 under the
  /Skyline/sim/ subpath), and one added `<script src="sim.js">` tag
  before `app.js`. `sw.js` is deliberately NOT copied.
- `docs/sim/sim.js` is the only new code. Loaded before `app.js`, it
  replaces `window.fetch`: `/api/*` requests are answered by an
  in-memory mirror of server.py's API (same JSON shapes, status
  codes, error strings, validation rules, snapshot semantics —
  units[] = own stats, group = exact sum, half-up MC rate); all other
  requests fall through to the real fetch. It also stubs
  `navigator.serviceWorker` (the real SW must never cache the sim;
  app.js's guarded register() gets a harmless resolving stub) and
  clears any `skyline_*` localStorage keys on load, so a refresh
  deterministically resets to the seed.
- Seed = server.py's demo fleet verbatim (18 SAMPLE vehicles + 15
  SAMPLE authorizations; due dates computed as today + due-days like
  the server loader) plus two pre-built SAMPLE UTCs (XFFC1 / 14 ASOS,
  X19RT / 19 ASOS) so UTC health shows immediately. Parity-proven:
  the sim's seed snapshot was diffed against a live server's
  post-demo-load snapshot — units, group, vehicles and
  authorizations identical (group: 18 on hand, 33 authorizations,
  FMC 12 / PMC 3 / NMC 3, MC 66.7).
- Sim logins: `editor` (any password) = Editor demo user, `viewer`
  (any password) = Viewer; any other name signs in as an Editor.
  Injected login note + persistent "WEB SIM" pill come from sim.js —
  app.js is never edited. State is in-memory only; refresh resets.
- Import: CSV is parsed client-side through server.py's exact
  column-mapping/preview/confirm rules. `.xlsx` returns an honest
  400 ("parsing runs on the Python server… the sim accepts .csv") —
  never fake-parsed.
- Checks: `node tests/sim_check.js` — 29 checks (byte-identity of
  app.js/style.css, asset existence, seed totals, role gates,
  bumper 409, status-conflict mirror, authorization delete totals,
  UTC 409/400 rules, CSV import preview/confirm, xlsx honesty,
  audit, CSV export, logout). All passing.

**Refreshing the sim when the app changes:** re-copy `static/`
(app.js, style.css, manifest.json, icons/, index.html) into
`docs/sim/`, re-apply the three index.html tweaks above (title,
relative paths, sim.js tag), and if server.py's API or demo data
changed, mirror it in `sim.js`. Re-run `tests/sim_check.js` and the
snapshot parity diff before uploading.

### Web sim review addendum (2026-10-23)

Independent review verdict: **SHIP** — byte-identity of the sim's
app.js/style.css verified, interception airtight (no /api/ path can
fall through to the network), seed snapshot re-diffed against a live
server with zero differences, 38 independent probes passed. Two
SHOULD-FIX parity nits in the sim's CSV import were fixed post-review:
(1) an ISO-shaped but impossible date (2026-02-30) now fails the
whole preview with the server's exact 400, matching the server (whose
ValidationError escapes its per-row catch), while non-ISO junk dates
still skip per-row; (2) the sim's CSV lexer now matches Python's csv
semantics — a quote is special only as the first character of a
field, literal anywhere else. Both probed live; harness still 29/29.

## v1.2 (2026-10-26) — Role rename (operator/VCO), AF Form 1800 inspections, photos, 30-day retention, sim parity, device guide

### 1. Role rename: viewer → operator, admin → VCO
- Server: `ROLES = ("operator", "editor", "vco")`. Every role check,
  error string ("admin only" → "VCO only"), first-run setup (creates
  the initial VCO), and the last-admin demotion guard (now last-VCO
  guard) updated. Powers: operator = old viewer powers (read
  everything) PLUS submitting inspections; editor unchanged; VCO = old
  admin powers (accounts, demo load/remove) PLUS inspection review.
- Migration: `init_db` maps legacy stored roles (viewer → operator,
  admin → vco) for existing databases; no other data touched;
  historical audit rows keep their old text (verified by a dedicated
  legacy-migration test that seeds viewer/admin rows + an old audit
  row, re-runs `init_db`, and asserts the roles moved while the audit
  detail did not).
- Frontend: setup screen "Create the first VCO account", nav, VCO
  screen (`view-vco`), role dropdown options/descriptions, read-only
  notices for operators; internal identifiers renamed consistently
  (`isAdmin` → `isVCO`, `view-admin` → `view-vco`).
- Sim: ROLES, demo logins, role gates, error strings mirrored; a `vco`
  seed user (Demo VCO, 18 ASOG) was added because the sim's setup is
  pre-seeded/403, so no other path creates a VCO in the sim.
- Service-worker shell cache bumped v4 → v5.

### 2. AF Form 1800 operator inspections
- Tables (created in `init_db`): `inspections` (id, vehicle_id, unit,
  bumper, operator, month_year, mileage_hours, tire_front, tire_rear,
  status ∈ {submitted, reviewed}, created_at, reviewed_by,
  reviewed_at), `inspection_items` (inspection_id, item_no, item_text,
  applies, result, comment), `discrepancies` (inspection_id, item_no,
  description, date_found, miles_hours, status ∈ {open, corrected,
  delayed, disposition}).
- The 35 items are the official DAF 1800 list, stored server-side as
  fixed text (client-supplied text for numbered items is ignored, so
  label drift cannot corrupt records); "other" items: text ≤ 120 chars,
  max 10; applies bool; result pass/fail required when applies;
  comment ≤ 2000 chars. All violations → 400.
- POST /api/inspections (any signed-in role, incl. operator): one
  `immediate_tx` inserts the inspection + items + an open discrepancy
  per failed item (description = comment or "Failed — see comment");
  a supplied mileage_hours updates the vehicle mileage with an audited
  old→new entry; an "inspection" audit row is written. Vehicle
  FMC/PMC/NMC status is NEVER changed by an inspection.
- GET /api/inspections (filters vehicle_id, unit, reviewed; unreviewed
  first, then id DESC) and GET /api/inspections/{id} (inspection +
  items + discrepancies) — any signed-in role, no-silos visibility.
  `do_GET` now passes the query string through to `api_get` (it was
  previously dropped).
- POST /api/inspections/{id}/review — VCO only (operator/editor → 403,
  unknown → 404, already reviewed → 409). Shape validation runs
  BEFORE the 409 check, matching the server's error precedence.
  Optional dispositions [{discrepancy_id, status}] apply the real
  statuses; per-discrepancy audit rows + a review audit row.
- VCO notification is in-app (no email/SMTP in Skyline, none invented):
  unreviewed-count badge on nav/home + an Inspections screen listing
  unreviewed first. That queue IS the notification.
- FORM VIEW GOES CLIENT-SIDE (spec redirect, 2026-10-26 — supersedes
  the original server-HTML form plan; no server HTML form page was
  ever built, so nothing was torn out): `inspectionFormHTML` in
  app.js fetches the detail JSON and renders the full form (header,
  item table, discrepancy table, signature lines) with print CSS and
  a Print / Save-as-PDF button. The server exposes JSON only. Because
  app.js is byte-identical between the real app and the sim, the sim
  gets the form view for free.
- Inspections are ONLINE-ONLY in the real app (same honesty pattern as
  vehicle deletes and UTC mutations); the UI says so plainly offline
  and nothing is queued.

### 3. Photos
- Vehicle photo (fleet data): one per vehicle, any signed-in role may
  attach/replace/remove. Storage: `vehicles.photo` BLOB + `photo_mime`
  (added via `_ensure_column` migration). Endpoints: POST
  /api/vehicles/{id}/photo {image: data URL}, DELETE (any role,
  idempotent, audits only when a photo actually existed), GET (raw
  image bytes). Fleet JSON carries only a `photo` 1/0 presence flag —
  never bytes — via `public_vehicle()` at all four vehicle return
  sites. Client downscales (canvas) to max 1600 px JPEG 0.8 before
  upload, falling back to the original file within the cap.
- Discrepancy photos: table `discrepancy_photos` (id, discrepancy_id,
  image BLOB, mime, created_at); POST
  /api/inspections/{id}/discrepancies/{did}/photos (any role, audited;
  404 on unknown inspection/discrepancy or a discrepancy belonging to
  another inspection; 409 at 4 photos), GET
  /api/inspections/{id}/discrepancies/{did}/photos/{pid}. Detail JSON
  includes photo [{id, url}] per discrepancy, never bytes.
- Body caps: photo POST endpoints accept up to 3 MB (413 beyond);
  the global 1 MB cap still applies to everything else. Photo routes
  are dispatched BEFORE the generic vehicle/inspection routes that
  would otherwise swallow them (and the DELETE photo branch lives in
  `api_delete`, not `api_put` — a misplacement during the build was
  caught and fixed before verification).
- Printable form renders photos: vehicle photo as a small thumb in
  the header, discrepancy photos inline in the discrepancy section
  (thumbnails that print), loaded on demand via the GET endpoints.
- VISIBILITY (spec clarification, 2026-10-26): the vehicle photo lives
  ONLY inside the vehicle detail view (and the inspection form reached
  from it). No thumbnails on home tiles, unit list rows, or search
  results; the unit list stays text-only. Locked in by frontend_check
  assertions: list + tile output contains no `<img>` even when
  vehicles carry `photo: 1`.

### 4. Retention — 30 days for 1800 records only
- A completed 1800 record (inspection + items + discrepancies +
  discrepancy photos) is purged 30 days after submission. Purge runs
  at server startup (`main()`, after `init_db`) and before inspection
  list reads (`_list_inspections` calls it first).
- Each purge run writes ONE audit row ("retention purge: N
  inspection(s) removed"); the original submission/review audit rows
  REMAIN as the accountability trail. The VCO queue naturally shows
  only unpurged items.
- VEHICLE PHOTOS DO NOT EXPIRE: they are fleet data, persist with the
  vehicle until edited/removed, and are never touched by the purge
  (verified by test: a vehicle photo survives a purge run).
- Sim: retention is N/A — in-memory data resets on refresh — and is
  documented as such; the sim mirrors the photo endpoints with data
  URLs instead of blobs.

### 5. Sim parity + sim demo divergence (SUPERSEDES the Web sim section)
- This section supersedes the "Web sim" section's statement that the
  sim seed is server.py's demo fleet verbatim: **the sim demo is now
  JLTVs + M1165 HMMWVs ONLY** (spec change, sim scope only;
  server.py's demo seed is untouched and still carries LMTVs,
  Trailers, F-250s and Generators). Sim rows were RETYPED, not
  renumbered — same 18 SAMPLE-xxx bumpers, units, statuses, mileages,
  due-days and locations, same seeded UTC/conflict references.
- `docs/sim/sim.js` implements the complete inspection backend
  in-memory (same validation, same side effects: mileage update on
  the in-memory vehicle, audit row, discrepancy rows for failed
  items, VCO-only review with the same 403/404/409 semantics) plus
  the photo endpoints (vehicle `_photo` data URL field, discrepancy
  photo data URLs, 4-cap 409). The VCO pending-inspection badge works
  in the sim from unreviewed in-memory inspections. The browser fetch
  shim now parses the query string into a plain object and passes it
  as a 5th arg to `backend.handle` (previously it stripped to
  `pathname` only, which would have broken `?reviewed=false`); the
  sim_check 4-arg calls keep working via the default.
- DEMO_AUTHS rewritten to the new mix: 14 ASOS JLTV 8 + M1165 2 (= 10),
  15 ASOS JLTV 5 + M1165 3 (= 8), 19 ASOS JLTV 4 + M1165 3 (= 7),
  20 ASOS JLTV 5 + M1165 3 (= 8), 18 ASOG 0, group 33 — per-unit totals
  deliberately kept identical to the old mix so downstream totals
  survive; only the type split changed. Row count changed 15 → 8
  (2 rows per squadron): every hand-computed expectation in
  sim_check.js was recomputed from the NEW sim DEMO (seed totals
  18 / FMC 12 / PMC 3 / NMC 3 / MC 66.7 unchanged; authorization
  rows 8; the authorization-delete test now targets 14 ASOS
  "M1165 HMMWV" authorized 2, downstream group 31 / 14 ASOS 8
  unchanged).

### 6. README device guide (spec addition, 2026-10-26)
- The README's one-line "open the address in any browser" guidance
  was replaced with a "Using Skyline on your devices" section: one
  host runs the server, every other device just opens the address in
  a browser, no app store; Windows host (Python 3, `python3
  server.py`, 127.0.0.1:8420 locally, `--host 0.0.0.0` + LAN IP for
  other devices, Windows Firewall private-network allowance);
  Windows client (any browser; Edge/Chrome install as an app window);
  Android (Chrome → Install app / Add to Home Screen; camera capture
  works for vehicle + discrepancy photos); iOS (Safari ONLY for
  installing — Share → Add to Home Screen, not offered from Chrome
  or Firefox; full-screen from the home-screen icon; sign in with the
  VCO-created account; camera/library picker works; same network as
  the host or use the public sim preview).
- Honest limits kept: no push notifications, no background sync;
  offline stays exactly the snapshot + queued-edits model, with the
  added caveat that the service-worker shell cache needs localhost or
  HTTPS — over a plain http://LAN-IP address the browser will not
  cache the shell, so field devices that will lose signal should keep
  the tab open or be served over HTTPS.
- To make the iOS full-screen claim true across iOS versions,
  index.html gained `mobile-web-app-capable` /
  `apple-mobile-web-app-capable` / status-bar-style meta tags
  (manifest already declares display: standalone); the sim re-copy
  carries them.

### Verification (v1.2)
- `python3 -m unittest discover -s tests` — **165 tests, OK**
  (100.2 s; baseline 105 + the new v1.2 classes, each ApiTests
  subclass also re-running the inherited suite). The run is slow on
  this VM because every setUp does five PBKDF2-HMAC-SHA256 (200k)
  hashes — an earlier "stall" scare during this build turned out to
  be exactly that slowness (faulthandler dump caught a server thread
  mid-`verify_password`, not a deadlock).
- `node tests/frontend_check.js` — **61 checks, all passing**
  (49 baseline + 12 v1.2: role gating ×3, 35-row form ×2, list Review
  gating ×3, printable-form content/escaping/photos ×2, photo
  visibility lock ×2).
- `node tests/sim_check.js` — **35 checks, all passing** (29 baseline
  + vco login + 3 inspection + 2 photo checks).
- `node --check` passes on app.js, sw.js, sim.js, and both harnesses.
- Sim app.js/style.css md5-verified byte-identical to static/
  (app.js 20d244f55b7e2b93225132a9b7e68238, style.css
  d965396160dfb9915d20f45d216d0bf5); sim index.html diff vs
  static/index.html is exactly the three tweaks (title, relative
  paths, sim.js tag).
- Live smoke (fresh server, temp DB): setup VCO → create vehicle →
  submit inspection as operator (failed item + comment) → vehicle
  mileage updated 100 → 5000 with status untouched (FMC) → audit
  rows present → VCO queue shows it pending → form-view JSON renders
  items/comments/discrepancies → vehicle photo attach/get +
  discrepancy photo attach → VCO review (corrected) → double review
  409. **10/10 passed.**
- Legacy-DB probe (pre-seeded viewer/admin users, real server boot):
  roles migrate to operator/vco, old VCO signs in. **Passed.**
- Two bugs found and fixed during verification (both in this build's
  new code, neither pre-existing): (1) `api_get` had no
  `ValidationError` → 400 mapping (api_post/put/delete already had
  it), so bad inspection-list filters returned 500 instead of 400 —
  fixed by adding the except clause; (2) a wrong-argument `req_bytes`
  call in `test_photo_404s` (missing `"GET"`, so the path string
  became the method) produced a `RemoteDisconnected` — test-only bug,
  the server was correct throughout.
- Non-blocking noise: the suite tail shows `ResourceWarning:
  unclosed socket` lines from the test servers (same pattern as
  baseline runs); exit code is 0 regardless.

### Fix pass (v1.2, reviewer-driven)
Two independent reviewers returned SHIP-AFTER-FIXES with no blocking
spec deviations. Fixes applied (server + sim mirror where the sim has
the route):
- **Anchored routing (B1).** Single-id routes used to match by prefix
  only, so DELETE /api/vehicles/{id}/photo/extra fell past the photo
  branch into generic vehicle-delete and DELETED THE VEHICLE
  (live-reproduced), and PUT to a suffixed path became a vehicle
  update. All single-id routes in api_get/api_post/api_put/api_delete
  (and the sim's handle()) now match exact shapes via `_route_id`
  (server) / `routeIds` (sim); trailing segments 404. Legitimate
  suffixed routes (vehicle /photo, inspection
  /discrepancies/{did}/photos/{pid}, utc /vehicles membership,
  conflicts /resolve) keep working. Regression tests prove the
  /photo/extra repro now 404s with the vehicle and its photo intact.
  The GET .../discrepancies/{did}/photos route with no photo id used
  to fall through to the inspection-detail handler; anchoring makes
  it 404 (routing nit, fixed as a natural consequence).
- **int64 guards (B2/F1).** Absurd integer ids (2**70) returned 500
  (OverflowError on the SQLite bind) on the new endpoints. The
  codebase's existing int64 range check (as `_path_id` already does)
  is now applied in `_create_inspection`'s vehicle_id (404),
  `_attach_discrepancy_photo`'s path ids (404), the
  `_list_inspections` vehicle_id filter (400), the api_get photo
  branch ids (404, via the anchored shapes), and review disposition
  ids (400). Absurd ids return 400/404, never 500.
- **Last-VCO race (F2).** In `_update_user`, the SELECT COUNT(*)
  WHERE role='vco' check ran BEFORE immediate_tx, so two concurrent
  demotions of two different VCOs could both read count=2 and both
  proceed, leaving ZERO VCOs — with no in-app path back (setup is
  blocked once users exist; promotion requires a VCO caller). The
  count check now runs INSIDE the write transaction with a fresh
  target read, where BEGIN IMMEDIATE serializes writers; the second
  demoter sees the true count and is refused. A barrier-aligned
  concurrency test (3 rounds, boss + one other VCO) proves exactly
  one demotion succeeds per round.
- **Discrepancy-photo delete (F3).** There was no way to remove a
  discrepancy photo, so a mistaken photo permanently occupied one of
  the 4 cap slots until the 30-day purge. Added DELETE
  /api/inspections/{id}/discrepancies/{did}/photos/{pid} (any
  signed-in role, audited, 404s matching the GET branch), mirrored
  in the sim, with a remove control per photo in the form view's
  discrepancy photo tools. Deletion is allowed on reviewed
  inspections too: removal takes nothing away from the closed
  record (contrast the attach rule below).
- **Photos on reviewed inspections (S3).** Attaching a discrepancy
  photo to a REVIEWED inspection now returns 409 on server and sim:
  review closes the record for new content. (Deletion, above, stays
  allowed.)
- **Purge on detail read (S1).** Purge triggers were startup + list
  reads only, so a 31-day-old inspection still returned 200 on a
  direct detail GET until the next list read. Detail reads now purge
  first, so expired records 404 there as well.
- **Retention tests (S2/F4).** Added: purge of a REVIEWED inspection
  older than 30 days (with its photos; submission + review audit rows
  remain), the exactly-30-day boundary (cutoff is strict
  created_at < now-30d, so exactly-30-days-old survives until older
  — proven with a frozen module clock, since wall-clock comparison
  is racy), and the startup purge path (the exact
  purge_expired_inspections call main() makes at boot).
- **Client 3 MB check (S5).** The client-side 3 MB length check now
  applies to the downscaled upload path too (vehicle photos; the
  discrepancy path got the same fix) — previously only the fallback
  path checked, leaving the server 413 as the only backstop.
- **Blob URL revocation (S6).** fetchPhoto blob URLs are now revoked
  when replaced (new photo set loads, new vehicle render) or when
  the photo view unloads, via revokeBlobUrl/revokePhotoUrls; sim
  data URLs pass through untouched, and stale vehicle-photo fetches
  are dropped (sequence guard) instead of assigning to a newer
  render. Revocation never runs while a photo is on screen.
- **Sim date parity (S7).** Sim discrepancy date_found used the UTC
  date (nowIso slice); it now uses the local date (localDatePlus(0))
  like the server's `_date.today()`.

### Deliberate design rules restated (documented, not changed)
- **Inspections survive vehicle deletion BY DESIGN.** Deleting a
  vehicle drops its UTC memberships but NOT its inspections: the
  inspection rows carry snapshot unit/bumper data, and the audit
  philosophy is that history outlives the record. An inspection
  whose vehicle is gone is an orphaned-but-readable record, not an
  error — the VCO queue and form view still open it.
- **Inspection mileage may move backward.** A submitted
  mileage_hours overwrites the vehicle mileage with an audited
  old→new entry even when lower than current, consistent with the
  existing vehicle-update behavior; no monotonic guard was
  specified, and none was added.
- **Purge cutoff is strict.** Exactly-30-days-old survives until
  older (created_at < now-30d); see the boundary test above.
- **No action taken (reviewer items considered and declined):**
  review-precedence order (deliberate, documented); the pre-existing
  static-path prefix note (not this build, no such sibling exists);
  magic-byte mime sniffing (the data-URL whitelist + image/* serving
  is sufficient).

## v1.3 (2026-10-26) — Two roles only (operator/VCO) + Unit VCO badge, no VCO cap

Roles collapse from three to exactly two, enforced server-side on
every write (the frontend hides/disables controls to match but is
never relied on alone):

- **ROLES = ("operator", "vco")** in `server.py` and `ROLES` in
  `docs/sim/sim.js`. Every gate that was `("editor", "vco")` is now
  `("vco",)` only (13 gates server-side, 13 in the sim): vehicle
  create/edit/delete, authorization upsert/delete, UTC
  create/edit/delete/membership, import preview/confirm, conflict
  resolve. User management (create/demote/remove), demo load/remove
  and inspection review were already VCO-only and stay so.
- **Operator:** reads everything (the shared fleet picture, audit,
  exports) and writes ONLY inspections and photos — 1800 submit,
  vehicle photo attach/replace/remove, discrepancy photo
  attach/remove. Everything else is 403, including conflict resolve,
  UTCs, import and demo data.
- **VCO:** all rights, including review (operator review is 403).
  First-run setup still creates the first VCO.
- **Legacy migration on boot (composes with the v1.2 renames):**
  `editor -> vco` added alongside `admin -> vco` and
  `viewer -> operator`. Historical audit rows keep their old text;
  only stored user roles migrate. A migrated editor who is the
  earliest-created VCO of their unit becomes that unit's Unit VCO
  via the backfill below.
- **Exports stay a read:** `GET /api/export.csv` remains open to both
  roles — it is the shared fleet picture as a CSV, and the operator
  denial list is vehicle edits, authorizations, user management and
  review. (Deliberate call; see the judgment list at the end.)
- **NEW endpoint (v1.2 had no user removal):** `DELETE /api/users/{id}`,
  VCO-only, with the last-VCO guard inside the write transaction
  (400 if the target is the final VCO), session revocation, and an
  audit row. The VCO screen exposes it as a Remove button. This was
  needed because the v1.3 spec tests operator-403 on user
  create/demote/remove — remove previously had no endpoint at all.

### Unit VCO badge (honorific only, one per unit)

- **Flag:** `users.is_main_vco` (INTEGER 0/1; new tables carry it,
  legacy DBs gain it via `_ensure_column`). Exposed as
  `is_main_vco` on `public_user` and as `main_vcos: {unit:
  display_name}` on the fleet snapshot (server + sim), so every role
  can see it on the unit screen.
- **Uniqueness enforced in the write path, never just the UI:**
  `_set_main_vco` (server) / `setMainVco` (sim) clears any other main
  in the unit before setting the new one, inside the same
  transaction as the user write.
- **Default:** the first VCO affiliated with a unit becomes its main
  VCO automatically on creation (and on promotion to VCO when the
  unit has no main). The setup VCO is main for its unit.
- **Transfer:** `PUT /api/users/{id}` with `{"is_main_vco": true}` —
  VCO-only, audited with field `is_main_vco`, and rejected for a
  non-VCO target. The VCO screen shows a "★ Make unit VCO" button
  for non-main VCOs.
- **Auto-pass:** demoting or removing a main VCO passes the badge to
  the earliest-created remaining VCO of that unit (lowest id); if
  none remains it clears. A main VCO moving units passes the badge
  in the old unit and fills the new unit's seat only if empty.
- **Honorific only:** no permission check anywhere reads
  `is_main_vco` — main and non-main VCOs have identical rights
  (proven by a dedicated test).
- **Display:** gold star + "Unit VCO" (`.main-vco`, gold on the
  dark/purple theme) next to the name in the VCO screen's user list,
  in the header user chip for the signed-in main VCO, and as a
  "★ Unit VCO" chip on the unit screen. The sim's Demo VCO is main
  for 18 ASOG.
- **Boot migration:** for each unit that has VCOs but no main VCO,
  the earliest-created VCO of that unit becomes main. Backfill runs
  after the role migration, so a migrated editor can become main.
  An existing main is never displaced by the backfill.
- **Sim mirror:** all of the above in `docs/sim/sim.js` (helpers,
  create/update/delete user, snapshot `main_vcos`, seed). Sim login
  rule changed with the collapse: `vco` / `operator` are the seeded
  demo users and any other name now signs in as an **Operator**
  (least privilege; the old "any other name = Editor" rule is gone).

### No cap on VCOs

There is no ceiling on VCO headcount anywhere — no max-VCO
validation, no UI limit, no test asserting a cap. The ONLY VCO-count
guard is the floor: demoting or removing the last VCO fails (400),
in the transaction, for both demote and remove paths.

### Frontend (static/, mirrored byte-identical into docs/sim/)

- `canEdit()` is now VCO-only; the `editor-only` CSS class is
  renamed `vco-only` and `utc-editor-tools` is renamed
  `utc-vco-tools` (the word "editor" is gone from the frontend
  entirely). Vehicle edit/delete, authorization edit/delete, UTC
  tools, import and user management are hidden from operators;
  operators still see the full fleet picture and reach inspection
  submit + both photo flows.
- VCO screen: role select offers operator/VCO only; user rows show
  the ★ Unit VCO badge, a transfer button, and a Remove button.

### Tests

- `python3 -m unittest discover -s tests` — **215 tests, OK**
  (v1.2: 183). New: 6 matrix tests + 7 badge tests + 1 main-backfill
  test = 14 new methods; the two new `ApiTests` subclasses also
  re-run its 9 base tests each (18), matching the existing subclass
  pattern. Coverage: operator 403 on vehicle create/edit/delete,
  authorization create/delete, user create/demote/remove,
  inspection review, UTC create, conflict resolve, demo load/remove;
  operator 200 on inspection submit + vehicle photo
  attach/replace/remove + discrepancy photo attach/remove; VCO 200
  on all of the above; last-VCO demote AND remove both fail; no-cap
  (4 VCOs in one unit, all 201); badge uniqueness, transfer
  (VCO-only, audited), auto-pass on removal and on demotion,
  auto-pass clearing when no VCO remains, badge-never-affects-
  permissions, legacy editor boots as VCO (+ becomes unit main),
  backfill keeps an existing main.
- `node tests/frontend_check.js` — **64 checks, all passing**
  (v1.2: 63; +2 badge/transfer renders, −1 retired write-role
  assertion). Bootstrap user is now a VCO (Walt), not an editor.
- `node tests/sim_check.js` — **43 checks, all passing**
  (v1.2: 39; + seed main-VCO check, unseeded-name rule, two-role
  matrix, badge matrix, user-removal floor).
- `node --check` passes on app.js, sw.js, sim.js, both harnesses.
- Sim app.js/style.css md5-verified byte-identical to static/
  (app.js 58cabfbb7053582fccacd968980dd35e, style.css
  6053921150141728a344a6bf1bafbbf4); sim index.html diff vs
  static/index.html is exactly the three tweaks (title, relative
  paths, sim.js tag — 17 diff lines, same shape as v1.2).
- Live smoke (fresh server, temp DB): setup VCO (main) → operator
  403s (vehicle/auth/user) → VCO vehicle create → operator
  inspection submit + vehicle photo + discrepancy photo attach/
  remove → operator review 403 → VCO review 200 → badge transfer
  clears first → removal auto-passes → VCO vehicle delete →
  fleet `main_vcos` shows the setup VCO. **21/21 passed.**
- Migration probe (legacy DB with a pre-collapse editor + viewer,
  real boot): editor boots as VCO and becomes its unit's main;
  viewer boots as operator. **Passed.**
- One bug found and fixed during verification (this build's new
  code): `_delete_user` read `fresh.get(...)` on a sqlite3.Row
  (no `.get`), 500ing user removal — fixed to row indexing before
  the final run. One test-only bug: an int `photo_id`
  concatenated into a URL string — fixed with `str()`.

### Deliberate judgment calls (spec ambiguities, conservative where it matters)

- **Export stays open to operators** (read, not write; see above).
  If the parent wants export VCO-only, it is a one-line gate change
  in `api_get` + sim `handle()` and two test updates.
- **User removal is a new endpoint** (see above) — v1.2 had no
  DELETE /api/users, so the spec's "operator 403 on user remove"
  could not be tested without one.
- **Any-VCO transfer, not same-unit-actor transfer:** "any VCO can
  transfer the badge to another VCO in the same unit" is implemented
  as: any VCO caller may move the badge between two VCOs of one
  unit (the badge's unit is what is scoped, not the caller's unit —
  consistent with Skyline's attribution-only unit affiliation).
- **Explicit `is_main_vco: false` on a main VCO clears AND
  auto-passes** (v1.3 fix pass, B1): clearing the seat calls the same
  auto-pass as removal/demotion, so the earliest-created remaining
  VCO of the unit inherits; only when no VCO remains does the seat
  simply clear. The earlier v1.3 wording ("clears without auto-pass,
  refills on the next create/promote/transfer") was wrong and is
  superseded by this.
- **"Editor" as a live role is gone everywhere; the word remains
  only where history or migration requires it:** the server
  migration literal `WHERE role='editor'` (required to migrate old
  DBs), the test legacy-migration data (the allowed exception),
  BUILD_NOTES dated history sections (v1–v1.2 entries describe the
  roles as they existed then, as the v1.2 rename did for
  viewer/admin), and this section's own explanation of what changed.
  Server gates, frontend, sim, README and all other tests are
  editor-free as a current designation. README and BUILD_NOTES
  version notes were otherwise updated; no version string file
  exists in this repo (version lives in these notes + README roles
  section), so no separate bump file was changed.
- **Kept intact, verified by the unchanged suites:** 30-day 1800
  retention + purge, photo rules (one vehicle photo, detail view
  only, no thumbnails; 4 discrepancy photos; 3 MB cap;
  reviewed-attach 409; discrepancy-photo delete on reviewed
  inspections), offline queued edits + online-only inspections/
  photos, and all v1.2 fixes (route anchoring, int64 guards,
  VCO-guard-in-transaction, blob URL revocation, sim local date).

### Fix pass (v1.3, reviewer-driven, 2026-10-26)

Two independent reviewers reported on the v1.3 build; the
consolidated list below was applied to server.py + the sim mirror
each (except where noted), with the docs/sim parity re-verified
after every static change. No behavior was changed for the
documented-only items (D-A–D-C).

**Blocking**
- **B1 — Explicit `is_main_vco: false` on a main auto-passes.**
  `server.py _update_user` and `sim.js updateUser` cleared the seat
  with no `_pass_main_vco` call, leaving a unit with VCOs but no
  main. Now an explicit false by a main clears, then auto-passes to
  the earliest-created remaining VCO of that unit — exactly as the
  demotion path does. (Only when no VCO remains does the seat
  simply clear.) Test asserts the earliest-created remaining VCO
  inherits, server + sim. The v1.3 "refill" sentence above is
  corrected to match.

**Should-fix (fixed)**
- **S-A — Sim last-VCO floor test was hollow.** The old sim check
  never attempted removing the final VCO. A new check on isolated
  sim state actually DELETEs the seed's sole VCO and expects 400,
  then confirms the VCO still exists.
- **S-B — Badge side-effects are now audited.** Transfer used to
  audit only the target. Now the displaced main (1→0) and the
  auto-pass recipient (0→1) each get their own `is_main_vco` audit
  row, inside the same transaction, in `_update_user`,
  `_delete_user`, `_create_user` (server) and `updateUser`,
  `deleteUser`, `createUser` (sim).
- **S-C — Boot backfill deduplicates.** `init_db` used to fill only
  zero-main units. Now, per unit, the earliest-created main is kept
  and any additional mains are cleared in the same pass; flags on
  non-VCOs are cleared too (only a VCO may hold the badge).
- **S-D — `_create_user` is one write transaction.** Insert + badge
  + audit now run inside a single `immediate_tx` instead of three
  separate commits.
- **S-E — Frontend VCO view guarded.** The `data-nav="vco"` handler
  now checks `isVCO()` before rendering, and the `#add-form`,
  `#user-form`, `#auth-form`, `#utc-form` submit handlers early-
  return on `!canEdit()`. Defense-in-depth only — the server still
  403s every VCO write. `frontend_check.js` asserts the guards as
  source text (the wiring section is cut from its child run).
- **S-F — Test gaps closed.** Server: operator GET `/api/users` →
  403; operator import preview/confirm and UTC create/update/
  delete/add-member/remove-member → 403 (added to the denied
  matrix); PUT `is_main_vco: true` on an operator target → 400;
  concurrent user removal (barrier-aligned, mirroring the
  concurrent-demotion test). Sim: operator-403 matrix for review,
  demo load/remove, conflict resolve, user delete, import preview/
  confirm, UTC delete, user list.
- **S-G — `GET /api/users` VCO-only exception documented** in README
  + here (see below). The endpoint was NOT opened.
- **S-H — Create-path badge flag consistency.** POST `/api/users`
  with `role=operator` + `is_main_vco: true` used to 201 with the
  flag silently dropped; it now 400s, like PUT. (Sim mirrors it.)

**Should-fix (documented, behavior deliberately unchanged)**
- **D-A — Last-VCO floor stays ORG-GLOBAL.** No per-unit floor was
  implemented. The floor prevents an org ending up with zero VCOs;
  a single unit CAN be left with zero VCOs, and any VCO (rights are
  global) can create a VCO for that unit. Documented in README +
  here.
- **D-B — Operator inspection submit updating vehicle mileage
  stays.** Designed v1.2 1800 flow: mileage/hours updates the vehicle
  record immediately, audited as "1800 inspection mileage/hours";
  status is never touched by inspections. This is the one sanctioned
  operator-write-to-a-vehicle-row exception. Documented in README +
  here.
- **D-C — Operator discrepancy-photo DELETE on reviewed inspections
  stays.** Deliberate v1.2 decision: reviewed-attach → 409,
  reviewed-delete allowed (removal takes nothing away from the
  closed record; without it a mistaken photo permanently occupied a
  cap slot until the 30-day purge). New test covers reviewed-delete
  behavior. Documented here as inherited v1.2 behavior.

**Nits**
- **N-A — Helper hardening.** `_set_main_vco` / `setMainVco` now
  assert the target is a VCO of that unit (all call sites already
  checked) and `_set_main_vco` returns the displaced main's id for
  the S-B audit rows.
- **N-B — Prose.** "notification to the org admin" → "org VCO" in
  `static/index.html` and `docs/sim/index.html` (identical edit, so
  the sim index diff stays exactly the three tweaks).
- **N-C — Sim comment.** `docs/sim/sim.js` seed section notes no
  boot backfill is needed because the seed is always clean.
- **N-D — Sim empty-username edge tightened to 400** (was: fallback
  login created a blank-username Operator); the sim note now also
  records fallback-login name squatting (a later VCO user-create
  with that name 409s).
- **N-E — `_update_user` stale-read audit noise fixed, not just
  documented.** Field changes are rebuilt from the fresh row read
  inside the transaction: changes a concurrent writer already
  applied are dropped as no-ops, and audit old values are re-based
  on the fresh row; the badge no-op return moved inside the
  transaction for the same reason. (The pre-transaction fast path
  for "no change requested at all" remains.)

**Deliberately left unchanged beyond the D-items:** the UTC
click handlers (`#utc-save`, `#utc-add-confirm`, `#utc-delete`) got
no `canEdit()` guard — S-E specified the submit handlers + nav
handler only, and the server 403s those clicks; the create-path
fill-empty-seat rule (explicit false on create still fills an empty
seat) is pre-existing v1.3 behavior the fix list did not ask to
change.

**Verification (fix pass, 2026-10-26):**
- `python3 -m unittest discover -s tests` — **223 tests, OK**
  (v1.3: 215; +8: explicit-false auto-pass, lone-main clear,
  create-flag 400, create-displaced audit, PUT-flag-on-operator
  400, concurrent removal, backfill dedup, reviewed-delete).
- `node tests/sim_check.js` — **47 checks, all passing** (v1.3: 43;
  +4: sim explicit-false auto-pass, sim create-flag 400, isolated
  last-VCO floor, sim operator-403 matrix).
- `node tests/frontend_check.js` — **64 checks, all passing**
  (count unchanged; the new S-E guard assertions run parent-side
  and fail the run if a guard is missing).
- `node --check` passes on app.js, sw.js, sim.js, both harnesses.
- Sim app.js/style.css md5-verified byte-identical to static/
  (app.js c980142604c15701bae86698e837cfe0, style.css unchanged
  6053921150141728a344a6bf1bafbbf4); sim index.html diff vs
  static/index.html is exactly the three tweaks (17 diff lines,
  same shape as v1.2/v1.3).
- Live smoke (fresh server, temp DB, never the tree DB): setup VCO
  (main) → operator 403s (users GET/create, import preview, UTC
  create) → transfer → B1 explicit-false auto-pass → removal
  auto-pass → last-VCO demote/remove both 400, org left with
  exactly one VCO. **13/13 passed.**
- Migration probe (legacy DB with a pre-collapse editor + viewer +
  a duplicated-main unit, real boot): editor boots as VCO and
  becomes its unit's main; viewer boots as operator; earliest
  duplicate main kept, additional main cleared, non-VCO flag
  cleared. **6/6 passed.**
