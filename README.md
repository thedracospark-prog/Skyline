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

- **vco** — a VCO: everything — add/edit/delete vehicles, change
  or delete authorizations, build UTCs, import a CSV, accounts,
  demo fleet, and inspection review.
- **operator** — an Operator: the same fleet picture, read-only for
  vehicles, plus submitting 1800 inspections and attaching
  vehicle/discrepancy photos.
- Any other name also signs in as an Operator. Note: that
  fallback-login name squats the username — a later VCO user-create
  with that name 409s (username already exists).

**Worth trying:** flip a vehicle's status and watch its unit's MC rate
and fleet-health picture move · open a UTC and see its own health view ·
remove an authorization line from a unit screen · build your own UTC
from the demo vehicles · import a small CSV (Type / Asset Number /
Location columns) from a unit screen · submit an 1800 inspection for a
vehicle and review it as the VCO.

The sim demo fleet is **JLTVs and M1165 HMMWVs only** — the sim's demo
intentionally differs from the real server's demo seed (which still
carries LMTVs, Trailers, F-250s and Generators). The sim implements the
**full 1800 inspection feature** (submit, review, photos) against its
in-memory data, exactly like the real app; retention does not apply
there because a refresh resets everything anyway.

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

The shape of it: one computer on the network runs the server; every
other device just opens the address in a browser. No app store, nothing
to install from a store.

## Using Skyline on your devices

**Windows (host).** Install Python 3, then run `python3 server.py`.
On the host itself the app is at http://127.0.0.1:8420. For other
devices on the same network, run with `--host 0.0.0.0` and give them
http://&lt;host PC's LAN IP&gt;:8420. If other devices cannot reach it,
allow Python through Windows Firewall on private networks.

**Windows (client).** Any browser to the LAN address. Edge or Chrome
can install it as an app window.

**Android.** Chrome to the LAN address, then Install app / Add to Home
Screen. Camera photo capture works from the browser for vehicle and
discrepancy photos.

**iOS.** Safari ONLY for installing — Share → Add to Home Screen.
iOS does not offer installation from Chrome or Firefox. The app then
opens full-screen from the home-screen icon like any app. Sign in with
the account the VCO created. The camera/library photo picker works
from the browser for vehicle and discrepancy photos. iPhones/iPads
must be on the same network as the server host.

**No-server preview.** The public sim link at the top of this README
is the zero-install preview for any device — no server, no account
creation, nothing to install.

**What it does not do.** No push notifications, no background sync.
Offline behavior is exactly the snapshot + queued-edits model in the
Offline behavior section below. One caveat: the offline app shell
(service-worker cache) needs localhost or HTTPS — over a plain
http://LAN-IP address the browser will not cache the shell, so keep
the tab open on field devices that will lose signal, or serve the app
over HTTPS.

## First-run setup

1. Open Skyline with an empty database — the setup screen appears.
2. Create the first **VCO** account (username, display name, password of
   8+ characters).
3. Sign in as the VCO and create everyone else's accounts from the
   **VCO** screen. There is **no public sign-up** — every account is
   created by a VCO.

## Roles

| Role   | Can do |
|--------|--------|
| Operator | Sign in and read every unit's fleet, audit log and exports, **plus submit 1800 inspections** and attach vehicle/discrepancy photos. Nothing else: no vehicle edits, no authorizations, no user management, no inspection review. |
| VCO  | Everything: add/edit/delete vehicles, set **or delete** authorizations, build UTCs, import spreadsheets, resolve sync conflicts, create/edit/remove accounts, load/remove the demo fleet, and **review 1800 inspections** (the VCO review queue is the notification — there is no email in Skyline). |

**No cap on VCOs.** There is no ceiling on VCO headcount, per org or
per unit — personnel is fluid, and any number of VCOs may exist. The
only VCO-count guard is a floor, and it is **org-global, not
per-unit**: the **last VCO cannot be demoted or removed**, so an org
never ends up with zero VCOs. A single unit *can* be left with zero
VCOs (its main clears when its last VCO leaves); that is not a
dead end, because VCO rights are global — any VCO can create a VCO
for that unit, and the first one affiliated becomes its Unit VCO.

**User listing is VCO-only.** Operator "reads everything" covers the
shared fleet picture, audit log and exports — it does **not** extend
to `GET /api/users`, which stays VCO-only: the account list is
account enumeration, not fleet data, and it stays behind the VCO
gate deliberately.

**The one sanctioned operator-write-to-a-vehicle-row exception.**
Submitting an 1800 inspection with mileage/hours updates the vehicle
record's mileage immediately (audited as "1800 inspection
mileage/hours") — that is the designed v1.2 1800 flow, and the only
way an operator write touches a vehicle row. Status is never touched
by inspections: a submitted inspection leaves the vehicle's FMC/PMC/
NMC status exactly as it was.

**Unit VCO badge (★ Unit VCO).** One VCO per unit carries the gold-star
**Unit VCO** badge — shown next to their name in the VCO screen's user
list, in the user chip, and as a "Unit VCO" line on their unit's screen.
It is **honorific only**: all VCOs have identical rights, and no
permission check keys off the badge. The first VCO affiliated with a
unit becomes its Unit VCO automatically; any VCO can transfer the badge
to another VCO in the same unit (VCO-only, audited); if a Unit VCO is
demoted, removed, or clears the badge explicitly, the badge auto-passes
to the earliest-created remaining VCO of that unit, or clears if none
remains. Badge side-effects are audited too: the displaced main and
the auto-pass recipient each get their own `is_main_vco` audit row.

A user's unit affiliation is **attribution only** — it shows who they are
("updated by Davis, 18 ASOG"), never what they're allowed to see.

## The fleet picture

- **Home:** one wide 18 ASOG rollup tile (total on-hand / authorizations /
  group MC rate) over four squadron tiles, each with its own on-hand,
  authorizations and animated MC-rate bar.
- **MC rate = FMC ÷ on-hand × 100**, computed live from vehicle records.
- **On-hand is derived** from the vehicle records — it is never typed in.
  **Authorizations** are tracked per unit per vehicle type and are the only
  fleet totals a VCO sets by hand.
- **18 ASOG has its own fleet too (v1.1).** Vehicles and authorizations can
  belong to any of the five units, 18 ASOG included. A unit's stats are
  always that unit's **own** fleet; the **group** totals (the home rollup
  tile) sum **all five** units, ASOG's own included — every vehicle counts
  exactly once in its own unit and once in the group. The 18 ASOG screen
  shows its own fleet, its own visualization, and a clearly labeled group
  rollup summary.
- **Authorizations are edited on the unit screen (v1.1)** — each unit
  screen lists its vehicle types with authorized vs on-hand; VCOs can
  change a number inline or add a brand-new type (even with zero vehicles
  of that type). The VCO screen keeps its authorization management too.
- **Authorizations can also be deleted outright (v1.1.1)** — VCOs can
  **Remove** an authorization line entirely, from the unit screen or the
  VCO table. Removing a line never touches the unit's vehicles of that
  type (their on-hand counts are unaffected); to keep the line but zero
  it, just set the number to 0. Like vehicle deletes, removal is
  **online-only** (it is not queued offline) and is written to the audit
  log with the count that was removed.
- **Unit screens** have search, an FMC/PMC/NMC filter, a fleet health
  visualization, and vehicle cards. Tap a vehicle for its page: big status
  buttons, mileage/hours, maintenance due (date and/or miles), location,
  notes. Operators get a read-only version of the same page, except
  they can still submit an 1800 inspection and attach a vehicle photo
  from it.

## UTCs — deployment packages (v1.1)

A **UTC (Unit Type Code)** is a named deployment package belonging to one
unit — e.g. code `XFFC1`, "First 30-day rotation package". Each unit
screen has a **UTCs** section with a card per UTC (assigned count,
FMC/PMC/NMC, MC-rate bar) and a **New UTC** flow for VCOs. A UTC's own
screen renders the **same fleet-health visualization** for just its
vehicles, its stats, and its member list.

- A vehicle may belong to **multiple UTCs** at once.
- A vehicle may only join UTCs of **its own unit**.
- Deleting a vehicle removes its UTC memberships; deleting a UTC removes
  **only the package, never the vehicles**.
- UTC create/edit/membership is VCO-only and fully audited.
- **UTC changes are online-only in v1.1** (like vehicle deletes): viewing
  UTCs works offline from the cached fleet picture, and the UI says so
  when a change is attempted offline.

## AF Form 1800 inspections (v1.2)

Operators and VCOs can open a vehicle's page and
file an **1800 inspection** (the Operator's Inspection Guide and Trouble
Report): the official 35-item checklist, each item marked applies /
pass / fail with a per-item comment, plus up to 10 locally added
"other" items, mileage/hours, and front/rear tire pressures. On submit,
in one transaction: the inspection is saved against the vehicle, the
vehicle's mileage/hours update immediately (audited), every failed item
becomes an **open discrepancy**, and an inspection audit row is written.
Submitting **never changes the vehicle's FMC/PMC/NMC status** — that
stays a VCO action.

**VCO review.** The VCO sees an unreviewed-inspection count badge on
the nav and home screen, and an **Inspections** screen listing
unreviewed first — that queue **is** the notification to the org's VCO
(Skyline has no email). A VCO review marks the inspection reviewed and
can disposition each discrepancy as corrected, delayed, or disposition;
anything left untouched stays open. Reviewing an already-reviewed
inspection is rejected, and every review step is audited. Review
closes the record for new content: attaching a photo to a reviewed
inspection is rejected (a mistaken photo can still be removed).

**If the vehicle is later deleted, its inspections stay.** Deleting
a vehicle drops its UTC memberships but not its 1800 history — the
inspection rows carry the unit and bumper at filing time, so the VCO
queue and the printable form still open them. History outlives the
record.

**Printable form.** Every inspection has a **View / print form**
button: a client-side printable view of the full AF Form 1800 (header,
item table with applies/result/comment, discrepancy table, signature
lines) with a **Print / Save as PDF** button — print or save as PDF
from the browser and the document is yours to hand on. The form
includes the photos (below).

**Vehicle photos (fleet data).** Operators and VCOs can attach, replace
or remove **one photo per vehicle**, shown on the vehicle page only —
never on home tiles, unit list rows, or search results. The fleet JSON
carries only a photo yes/no flag, never the bytes, so fleet payloads
and offline snapshots stay light. Photos are downscaled in the browser
to max 1600 px JPEG before upload (uploads up to 3 MB; the usual 1 MB
body cap does not apply to photo posts).

**Discrepancy photos.** Up to **4 photos per discrepancy**, attached
from the form view after submission. They print inline in the form's
discrepancy section, so the saved PDF houses the photos.

**30-day retention — 1800 records only.** A completed 1800 record
(inspection + items + discrepancies + discrepancy photos) is kept
**30 days from submission, then purged** — at server startup and before
inspection list reads. Each purge run writes **one** audit row
("retention purge: N inspection(s) removed") so the deletion itself is
auditable, and the original submission/review audit rows **remain** as
the accountability trail. **Vehicle photos do not expire** — they are
fleet data and persist with the vehicle until edited or removed.

**Online-only.** Inspections and photos are online-only in the real
app (like vehicle deletes and UTC mutations): offline, the UI says so
plainly and nothing is queued. The sim is in-memory, so inspections and
photos work there with no server at all.

## Import vehicles from Excel (v1.1)

On any unit screen, VCOs get **Import from Excel**:

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
authorizations. From the **VCO** screen, **Load demo fleet** inserts a
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
  and lists the conflict under **Conflicts** for a VCO to resolve
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
