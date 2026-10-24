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
