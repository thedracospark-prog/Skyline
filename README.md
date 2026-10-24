# Skyline — Fleet Management

**Skyline** is a shared fleet management web app for the 18th Air Support
Operations Group (18 ASOG) and its squadrons — 14 ASOS, 15 ASOS, 19 ASOS and
20 ASOS. It is named for the Nissan Skyline GT-R, Paul Walker's halo car in
*Fast & Furious*.

Every signed-in member sees **every** unit's fleet — there are no silos. One
shared picture: who has what, what's mission capable, what's deadlined, and
what maintenance is coming due.

Skyline is a web-first, installable PWA: one link works on any phone, no app
store. It runs on the Python 3 standard library alone (no pip installs, no
build step) with a SQLite database, so it is easy to copy and run anywhere.

## ▶ Try the web sim

**https://thedracospark-prog.github.io/Skyline/sim/**

The web sim is the **real Skyline app** — the same frontend that ships in
this repo — running against simulated data entirely in your browser. No
server, no account creation, nothing to install. It starts loaded with
the app's SAMPLE demo fleet and two sample UTCs so you can see it alive
immediately.

**Logins** (any password works):

- **editor** — an Editor: add/edit/delete vehicles, change
  authorizations, build UTCs, import a CSV.
- **viewer** — a Viewer: the same fleet picture, read-only.
- Any other name also signs in as an Editor.

**Worth trying:** flip a vehicle's status and watch its unit's MC rate
and fleet-health picture move · open a UTC and see its own health view ·
remove an authorization line from a unit screen · build your own UTC
from the demo vehicles · import a small CSV (Type / Asset Number /
Location columns) from a unit screen.

Honest notes: the sim holds **demo SAMPLE data only** — nothing you do
there is real or saved; **a page refresh resets it** to the demo fleet.
Excel (.xlsx) import is the one thing the sim won't fake: parsing Excel
is the Python server's job, so in the sim the import accepts **.csv**
(the real app takes both).

## Run it

```bash
cd skyline
python3 server.py            # http://127.0.0.1:8420
python3 server.py --port 9000 --host 0.0.0.0   # reachable on your LAN
```

Then open the address in any browser. On a phone, use "Add to Home Screen"
to install it as an app.

## First-run setup

1. Open Skyline with an empty database — the setup screen appears.
2. Create the first **Admin** account (username, display name, password of
   8+ characters).
3. Sign in as the Admin and create everyone else's accounts from the
   **Admin** screen. There is **no public sign-up** — every account is
   created by an Admin.

## Roles

| Role   | Can do |
|--------|--------|
| Viewer | Sign in and read every unit's fleet, audit log and exports. |
| Editor | Everything a Viewer can, plus add/edit/delete vehicles, set **or delete** authorizations, resolve sync conflicts. (SEL + whoever the Admin designates.) |
| Admin  | Everything, plus create/edit accounts and load/remove the demo fleet. |

A user's unit affiliation is **attribution only** — it shows who they are
("updated by Davis, 18 ASOG"), never what they're allowed to see.

## The fleet picture

- **Home:** one wide 18 ASOG rollup tile (total on-hand / authorizations /
  group MC rate) over four squadron tiles, each with its own on-hand,
  authorizations and animated MC-rate bar.
- **MC rate = FMC ÷ on-hand × 100**, computed live from vehicle records.
- **On-hand is derived** from the vehicle records — it is never typed in.
  **Authorizations** are tracked per unit per vehicle type and are the only
  fleet totals an Editor/Admin sets by hand.
- **18 ASOG has its own fleet too (v1.1).** Vehicles and authorizations can
  belong to any of the five units, 18 ASOG included. A unit's stats are
  always that unit's **own** fleet; the **group** totals (the home rollup
  tile) sum **all five** units, ASOG's own included — every vehicle counts
  exactly once in its own unit and once in the group. The 18 ASOG screen
  shows its own fleet, its own visualization, and a clearly labeled group
  rollup summary.
- **Authorizations are edited on the unit screen (v1.1)** — each unit
  screen lists its vehicle types with authorized vs on-hand; Editors can
  change a number inline or add a brand-new type (even with zero vehicles
  of that type). The Admin screen keeps its authorization management too.
- **Authorizations can also be deleted outright (v1.1.1)** — Editors can
  **Remove** an authorization line entirely, from the unit screen or the
  Admin table. Removing a line never touches the unit's vehicles of that
  type (their on-hand counts are unaffected); to keep the line but zero
  it, just set the number to 0. Like vehicle deletes, removal is
  **online-only** (it is not queued offline) and is written to the audit
  log with the count that was removed.
- **Unit screens** have search, an FMC/PMC/NMC filter, a fleet health
  visualization, and vehicle cards. Tap a vehicle for its page: big status
  buttons, mileage/hours, maintenance due (date and/or miles), location,
  notes. Viewers get a read-only version of the same page.

## UTCs — deployment packages (v1.1)

A **UTC (Unit Type Code)** is a named deployment package belonging to one
unit — e.g. code `XFFC1`, "First 30-day rotation package". Each unit
screen has a **UTCs** section with a card per UTC (assigned count,
FMC/PMC/NMC, MC-rate bar) and a **New UTC** flow for Editors. A UTC's own
screen renders the **same fleet-health visualization** for just its
vehicles, its stats, and its member list.

- A vehicle may belong to **multiple UTCs** at once.
- A vehicle may only join UTCs of **its own unit**.
- Deleting a vehicle removes its UTC memberships; deleting a UTC removes
  **only the package, never the vehicles**.
- UTC create/edit/membership is Editors-only and fully audited.
- **UTC changes are online-only in v1.1** (like vehicle deletes): viewing
  UTCs works offline from the cached fleet picture, and the UI says so
  when a change is attempted offline.

## Import vehicles from Excel (v1.1)

On any unit screen, Editors get **Import from Excel**:

1. Pick a `.xlsx` (or `.csv`) file — the server parses it and returns a
   **preview**: every row marked *will import* or *skipped* with the
   reason. Nothing is written yet.
2. Check the preview, then **Confirm**. The server re-validates every row
   (it never trusts the preview) and creates the vehicles in that unit,
   with audit entries marked as imported, including the source filename.

Column mapping (headers matched case-insensitively, trimmed):

| Field | Accepted headers |
|-------|------------------|
| Vehicle type | `type`, `vehicle type`, `vehicle` |
| Asset number → bumper # | `asset number`, `asset #`, `reg number`, `reg #`, `registration`, `bumper`, `bumper #`, `id` |
| Location | `location` |
| Status | `status` (FMC/PMC/NMC; **defaults to FMC and the preview shows the default**) |
| Mileage / hours | `mileage`, `miles`, `hours`, `mileage/hours` |
| Maintenance due date | `maintenance due`, `maintenance due date`, `maint due date`, `due date` |
| Maintenance due miles | `due at miles`, `maintenance due miles`, `maint due miles` |
| Notes | `notes`, `note`, `remarks` |

Rules: the asset number is **required** (rows without one are skipped
with the reason shown); **import is add-only** — duplicate asset numbers,
in the file or already in the unit, are skipped and reported, never
overwritten; a blank type becomes `Unknown` (shown in the preview);
unmapped columns are listed in the preview, not silently guessed. File
cap **5 MB**, max 5,000 rows; formulas are never evaluated (stored values
only). Malformed files get a clean error, never a dropped request.
- **Every change is audited** — who, what field, old → new, when — including
  account changes and conflict events. The audit log is under **Audit**.
- **Exports:** **CSV** downloads the full fleet; **Rollup** opens a clean
  printable view (use the browser's Print → Save as PDF for a PDF).

## Demo data

The app starts **empty**: the five units exist, with zero vehicles and no
authorizations. From the **Admin** screen, **Load demo fleet** inserts a
sample fleet across the squadrons so you can see Skyline alive. Every demo
vehicle's bumper number starts with `SAMPLE-` and its notes say it is
sample data. **Remove demo fleet** deletes only the sample vehicles and
their sample authorizations.

## Offline behavior

- A service worker caches the app shell, so Skyline opens with no signal.
- The last synced fleet snapshot is cached on the device, so the fleet
  picture is readable offline.
- Edits made offline apply on the device immediately and are **queued**;
  the header pill shows `Offline — N queued` and flips to `Synced` after
  the queue replays on reconnect.
- **Conflict rule:** edits to *different* fields merge automatically. If a
  vehicle's *status* changed on the server while you were editing an older
  copy offline, your status change does **not** silently overwrite it. The
  server keeps its current status, writes a flagged entry to the audit log,
  and lists the conflict under **Conflicts** for an Editor to resolve
  (keep current, or apply the attempted status).

## Passwords & sessions

Passwords are stored as PBKDF2-HMAC-SHA256 hashes (200,000 iterations,
per-user salt) — never in plain text. Sign-in issues a random session
token; sign-out revokes it.

## Honest next steps for production

- **Hosting:** v1 runs anywhere Python 3 runs. For real nationwide use, put
  it on one always-on host (a small VPS or a unit server), run it behind
  HTTPS (e.g. Caddy/nginx or a Cloudflare front), and back up `skyline.db`
  on a schedule. Nothing here costs money by itself; the host is the only
  real-world requirement.
- **Passkey / biometric sign-in:** the locked design calls for Face ID /
  fingerprint unlock after first sign-in. This build implements strong
  password auth and session tokens; **biometrics are not faked**. Adding
  real passkeys (WebAuthn) needs a public HTTPS origin and a WebAuthn
  verification library (the Python `cryptography` package) — that is the
  first production upgrade to schedule.
- **Hardening for production:** add session expiry, login rate limiting,
  and an off-site backup of the database before wider rollout.

## Layout

```
server.py            backend (stdlib http.server + SQLite) + all API logic
static/              the PWA: index.html, app.js, style.css, sw.js,
                     manifest.json, icons/
tests/test_skyline.py  unittest suite (auth, roles, MC math, audit, conflicts)
assets/              original app icon master (1600×1600)
skyline.db           created on first run (your data — back it up)
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```
