# Handoff: laptop session → mini, 2026-08-03

Both machines now push here. `git log --oneline 6a5cdde..` is the authoritative
list — a count written into this sentence went stale twice, so it is no longer
written down.

**State of the mini as of the last exchange:**

- Repo pulled — it is at `fbb1927` or later.
- Backup job installed, bootstrapped and verified: `last exit code = 0`, log
  written, first snapshot in `/usr/local/var/backups/building-controls`.
- **The gateway HAS now been restarted** (2026-08-03 18:14, superseding the
  warning that used to sit here). Everything under *What changed* is live.

The `code version` line in a backup log is the *repo's* SHA, not the running
process's — do not read it as proof the gateway restarted.

---

## Added from the mini, 2026-08-03 evening

**The fleet went from 1 thermostat to 16.** Fifteen were added to
`config/devices.yaml` — gitignored, so this is the only place that side of the
work is visible. All follow the −100 convention (room 326 → `192.168.144.226`);
zones split 11 × floor-3, 5 × floor-2. `devices_from_csv.py` earned its keep: it
caught a duplicate address where 303 had been transcribed as `.205`, a copy of
the row above it, and a duplicate MAC between 305 and 307.

**`poll_interval_seconds` raised 10 → 30, and `bms/poller.py`'s docstring is
wrong.** It claims "a measured 19ms per device leaves a 25-device cycle around
half a second". Measured here with 16 real thermostats over Wi-Fi: **~640 ms per
device**, so a cycle is ~10.2 s. At the old interval the loop never slept — it
finished a cycle and immediately began the next, logging a warning every time,
with zero headroom for the 5 s a non-answering device costs. 19 ms was a wired
bench figure and is 33× off for this building.

`stale_after` is 3 × the interval, so a dead device is now flagged after 90 s
instead of 30 s. Acceptable: a suite's temperature does not move meaningfully in
that time, and `settle_and_refresh` re-reads immediately after any command.

**Two changes this implies, deliberately NOT made on the mini** so they would
not ride along with a production restart — please do them on the laptop:

1. Correct that docstring in `bms/poller.py`. The 19 ms claim is the assumption
   that made a 10 s interval look safe.
2. Carry the same note into `config/devices.example.yaml`, so the next building
   does not start from a bench number.

**Suite 207 will not keep a BACnet Device ID — swap deferred.** It reports the
factory default `4194302` and reverts on exit, with the installer passcode
entered, both for `207` and for an unrelated in-range value (`111307`) — so it
is the unit's storage, not a validation rule. Its twin Suite 314 shares the OUI
`04:7b:cb` and accepted its id correctly, so this is not a model-wide fault.

Left as `207` in the config deliberately. Polling is unaffected: reads address
the point objects by IP and never name the device object, which is why it shows
online. What silently does not work is `read_device_time` (`bacnet.py:185`),
which addresses `device,207`; the failure is swallowed as "clock is optional".
**Do not "fix" this by putting `4194302` in the config** — it would silence the
startup warning, make `/t/4194302` the tenant URL, and hide a real fault. It is
also a collision waiting to happen: one unit on the default is a mismatch, a
second is genuinely ambiguous. Check this before commissioning any further
thermostat. Full reasoning sits in a comment beside the 207 entry in the mini's
`config/devices.yaml`, which git will never show you.

---

## Answered from the laptop, 2026-08-03 night

**Both requested changes are done.**

1. `bms/poller.py`'s docstring no longer claims 19 ms. It now carries your
   measured ~640 ms per device, says a 25-device fleet is therefore ~16 s not
   half a second, and explains *why* the bench number was 33x optimistic — it
   timed the read alone, on wire, against one unit.
2. `config/devices.example.yaml` ships `poll_interval_seconds: 30` with the same
   reasoning, so the next building starts from a real figure.

**One thing not asked for, because your report implied it.** You noted the
overrun warning fired every cycle. It had no guard, unlike the offline-device
path four lines below it, which already logs the transition rather than the
state — at a 10 s interval that was six identical lines a minute, indefinitely,
burying everything else. It now logs the first overrun and every 20th, reports
devices and per-device milliseconds, recommends a concrete interval, and logs
recovery when the loop comes back inside its budget.

Checked against your numbers rather than invented: 16 devices x 640 ms
recommends exactly `30`, the value you independently chose. 25 x 640 ms also
gives 30.

**Correction on Suite 207 — the clock failure is *not* silent.** `read_device_time`
returning `None` is swallowed, as you said, but `_reconcile_clock` turns that into
`result.errors.append("could not read device clock")`, and `pages.py:729` renders
any device with errors as a red `errors` chip on the System page with the message
beside it. So 207 should be sitting there visibly failing after every reconcile,
and `_verify_inventory` logs an id-mismatch warning for it at every startup.

That matters for how it gets triaged: it is a standing visible fault, not a hidden
one, so it does not need instrumenting — it needs the unit fixed or swapped.
Worth watching for the reason you gave: these thermostats evaluate schedules
**on-device**, so a clock nobody can set eventually means occupancy starting at
the wrong hour, in a suite, with no other symptom.

Everything else in your note I have left exactly as you set it, including 207
staying `207` in the config.

---

## What changed

### `6b742f6` — logout is audited; the role test no longer asserts on a fiction

`login.ok`, `login.failed` and `login.throttled` were recorded and nothing marked
a session *ending*. With door unlock live, a log that cannot answer "was that
session still open?" is a gap. `/logout` now resolves the session before
revoking it, so the row names who left.

`tools/test_roles.py` hardcoded device `301` and zone `floor-3`. Device 301
exists on neither machine — it came from a worked example in a config comment and
got repeated as though it were hardware. It now discovers the device from the
tenant account's own `/devices` and compares visible zones against what `/me`
reports. `BMS_DEVICE` overrides. **This test should now actually run on the
mini**, which it never could before:

    BMS_ADMIN=herm:... BMS_MANAGER=...:... BMS_TENANT=herm-326:... \
      .venv/bin/python tools/test_roles.py

### `7f044ac` — doors and lights on the dashboard

The reported bug: signing in **without** a bookmarked `/t/{device}` link left you
on `/` with no door or light buttons at all. They only ever existed on the tenant
page. Whether you could get into the building depended on which URL you arrived
at.

`/` now carries the same buttons, built by `access_buttons()` in `bms/api.py` —
the same helper `/t/{device}` uses, so the two cannot drift into offering
different doors to the same person. Authorisation is unchanged.

Three things that fell out of it, all fixed here, all worth knowing before you
touch this code:

- The dashboard reloads itself every 15s. That would have torn down the Face ID
  sheet mid-unlock. The reload now waits on `window.__busy`.
- Building the light labels needs the panel, and the SOAP client waits 20s per
  call across three calls. An unreachable panel could have held `/` for a minute,
  sixty times an hour. Bounded by `ACCESS_UI_BUDGET = 3.0`; past that the buttons
  draw without their durations. A trigger the panel *actively denies* having is
  still hidden — that is a config error, not an outage, and the two cases are
  deliberately distinguished.
- Tenants' device links now go to `/t/{device}` instead of the operator detail
  page.

### `7241a22` — verification window 120s → 90s

Herm's reasoning, which is the right frame: the window is the exposure. Someone
holding a phone taken from its owner **cannot re-verify** — that needs the
owner's face. So the number is precisely how long a stolen unlocked phone can
open doors.

90 covers the arrival it exists for (open the garage gate, park, walk to the
door — about 45 seconds at this building) with double the room. Falling outside
costs one extra face scan.

Deliberately **not** extended by use. Refreshing on each unlock would cover an
arbitrarily slow walk, but would also let someone who took the phone inside the
window hold it open indefinitely by opening a door every minute. Do not "improve"
this into a sliding window.

### `110d0f2` — bulk CSV loading, backups, this document

See the two sections below.

### `ec6054a` `18baa3f` — installing the launchd jobs

Both driven by a real failure on the mini; see *Resolved* below. The install
procedure changed: **the plists are now substituted with `sed`, not hand-edited.**
Anything you remember about editing `CHANGEME` by hand is superseded by *Install
the launchd jobs* in `deploy/DEPLOY.md`.

### `aac1412` — each backup is one standalone file

The first real run left `bms.db-wal` and `bms.db-shm` in the backup directory,
beside a `RESTORE` file warning that stray WAL files corrupt databases. No data
was at risk — the `-wal` was empty and `bms.db` verified complete without it —
but shipping the files your own instructions warn about is a trap for whoever is
restoring under pressure. The copy is now collapsed with `journal_mode=DELETE`,
**against the copy, never the source**: switching the live database's journal
mode would take a write lock on the gateway mid-poll.

`integrity_check` runs *after* the collapse, so what is verified is the artifact
that would actually be restored.

### `fbb1927` — what the backup does not cover

Same disk as the data. Covers a bad edit, a botched upgrade, a corrupt database,
"what did the schedule look like last week". Does not cover the disk failing or
the machine being stolen.

---

## Bulk loading 25 thermostats and their tenants

Two separate things, because they live in different places: **devices are
config** (`config/devices.yaml`), **users are database** (`data/bms.db`).

### Devices

    .venv/bin/python tools/devices_from_csv.py thermostats.csv

Columns: `device_id, address, name, zone, mac`. It validates the whole sheet
before printing anything — duplicate ids, duplicate addresses, malformed IPs, and
specifically `4194302`, which is what an unconfigured TC500A reports and which
would collide silently if two of them arrived that way.

It prints YAML to stdout rather than editing `config/devices.yaml`. That is
deliberate: the config file is mostly comments recording traps that cost real
time to find, and PyYAML drops every one of them on a round trip. Paste the block
in. Verified that the generated block loads through `bms.config.load`.

### Users

    .venv/bin/python -m bms.useradmin import-csv tenants.csv --dry-run
    .venv/bin/python -m bms.useradmin import-csv tenants.csv --passwords /tmp/pw.csv

Columns: `username, display_name, role, zones, password`. Only `username` is
required; role defaults to `tenant`; a blank password means one is generated.
Zones are space- or semicolon-separated (not comma — a spreadsheet would have
split that into another column).

Validates the entire file before writing anything, because finding the typo on
row 19 after rows 1–18 already exist is horrible to unpick by hand. Re-running is
safe: existing usernames are skipped, not modified.

`--passwords` writes a file created mode 600 by `os.open`, not chmod'ed
afterwards — there is no window where it is readable. Hand them out and delete it.

**The check worth caring about**: with `--config`, it warns when a tenant's zone
matches no device. That is exactly the failure that made `herm-326` sign in to an
empty page — the account was fine, the zone name did not match anything.

---

## Backups

**Already installed and working on the mini** — nightly at 03:15, 30 kept. To run
one by hand, or to a different destination:

    deploy/backup.sh                       # → /usr/local/var/backups/building-controls
    deploy/backup.sh ~/backup-test         # anywhere this account can write

Backs up the database, `config/devices.yaml`, the gateway plist, and the git SHA
that was running. Everything else on the machine comes back from `git clone` and
`pip install`. Each backup directory holds exactly one database file, complete
and standalone.

**The gateway does not need to be stopped.** It uses `sqlite3 .backup`, which
snapshots a live database consistently. This is not a stylistic preference —
measured on a live gateway-style connection, a plain `cp` of `bms.db` produced a
file where `app_user` **did not exist as a table**, because 292 KB of committed
data was still sitting in `bms.db-wal` awaiting a checkpoint. A naive `cp` backup
of this system is worthless. Do not "simplify" it back to `cp`.

Each run verifies its own copy — `PRAGMA integrity_check` plus a non-zero user
count — and fails loudly rather than reporting success on a bad file. Retention
only deletes directories containing a `RESTORE` file, so a mistyped destination
cannot `rm -rf` something the script did not create. Written for bash 3.2, which
is what macOS ships; no `mapfile`, no arrays.

Restore steps are written into every backup directory as `RESTORE`. A restore was
tested on the laptop, not merely read: the copy opened through `bms.store.Store`
with accounts and zones intact, 4 schedule groups, 10 holiday rules, 594 audit
entries, and it accepted writes.

Not disaster recovery — see `fbb1927` above. Pointing Time Machine or an `rsync`
at the destination would close that, remembering it carries password hashes and
the access panel's credentials.

---

## The bench database: nothing to transfer

Herm asked whether the schedules preloaded on the laptop need moving to the mini.
Checked, and they do not:

- The four schedule groups (Standard weekday, Extended evening, Six-day,
  Seven-day) in the laptop DB are **byte-identical to `DEFAULT_GROUPS` in
  `bms/schedules.py`**. Nothing was customised on the bench.
- The ten holidays are exactly the US federal seed set.

So the mini reproduces all of it with two authenticated calls, no file copy:

    POST /groups/seed-defaults
    POST /holidays/seed-us-federal

Both skip what already exists. If the mini's DB already has them, this is a
no-op. If real per-tenant schedules ever need moving between machines later, the
mechanism is a backup restore, not an export tool — that is what `backup.sh` is.

---

## Resolved on 2026-08-03: the backup job that wrote no log

Diagnosed and fixed; recorded because the symptom is misleading and will recur on
the next machine.

The plist was bootstrapped with its `CHANGEME` placeholders still in place.
`launchctl print` showed `username = CHANGEME` and `last exit code = 78:
EX_CONFIG`: launchd rejected the job at **user lookup, before opening
StandardOutPath**, so the failure had nowhere to be written. Silence, not an
error message. It was also in `properties = penalty box`, which `kickstart` does
not clear — only `bootout` + `bootstrap` does.

`deploy/backup.sh` itself was fine and had already produced a good backup when
run by hand. That is the faster test and it comes first:

```bash
deploy/backup.sh ~/backup-test && ls -R ~/backup-test
```

DEPLOY.md now has **Install the launchd jobs**, which substitutes both plists
with `sed` instead of asking anyone to hand-edit them, and **If a job never runs
and writes no log**, a table mapping each `launchctl print` signature to its
cause. Two substitutions are needed, not one: the repo path contains the literal
`CHANGEME`, so a single `s/CHANGEME/$(whoami)/` yields
`/Users/hermf/building-controls` on this machine, whose repo is at
`/Users/Shared/building-controls`.

The mini's repo is at `/Users/Shared/building-controls` — outside TCC, per the
`~/Documents` trap documented in DEPLOY.md. Do not assume a home-directory path.

## Settled: weak-signal drop-outs are a Wi-Fi coverage job

Some units drop out on weak signal. Two things were considered and one is closed.

**Wi-Fi RSSI is not readable over BACnet.** All 770 objects on firmware
`01.01.16.00` were checked — no RSSI, nothing in the decibel unit family, and
every object whose name contains "Net" is fallback behaviour for stale network
*inputs*. The vendor's point list does contain `Gui_WiFiStatus`, along with 30
other `Gui_*` points, and the device exposes **zero** of them. Added to the
corrections table in the README.

So the gateway measures round-trip instead: per-device poll timing, a smoothed
average and lifetime failure counts, ranked worst-first under **Link quality** on
the System page. Bands come from this building's numbers — ~640 ms is the norm,
marginal at ~1200, weak at ~2000 or 5% failures.

**MS/TP over the old LonWorks wiring is ruled out.** The cabling is home-run
stars with no terminators, confirmed by the person who supervised installing it.
RS-485 needs a linear daisy chain; that is the one topology it cannot be talked
into. Do not revisit this — the reasoning and the three other blockers are in the
README, including that `bacpypes3` 0.0.106 has no MS/TP link layer at all.

**The answer is more access points**, of which Herm has spares. Worth knowing for
whoever helps: the TC500A is a 2.4 GHz client, so plan channels 1/6/11 and turn
transmit power *down* — high power everywhere makes clients cling to a distant AP
instead of roaming to the near one, which produces exactly this symptom. The Link
quality table gives a before/after measurement per suite.

### Immediate software mitigation, while the AP work waits

AP changes are weeks away, so two things were done in software. **Both need edits
to the mini's `config/devices.yaml`, which is gitignored — new defaults do not
reach it while the old keys are present.**

```yaml
bacnet:
  apdu_timeout_ms: 1500      # add
  apdu_retries: 3            # add
request_timeout_seconds: 7   # was 5
offline_after_failures: 3    # add
```

**1. BACnet's retries were silently disabled.** Confirmed services retry at the
application layer — `apduTimeout` 3000 ms, `numberOfApduRetries` 3, so four
transmissions across twelve seconds. `request_timeout_seconds` is an outer bound
wrapped around that whole cycle, and at 5 it cut the cycle off during the second
attempt. Measured against a dark address: 5 s ends at 5.00 s with our own
`TimeoutError`; 15 s ends at 12.01 s with BACnet's `no-response`. Attempts three
and four never happened — the ones most likely to get through on a lossy link.

Raising the outer timeout alone would make a dead device block the sequential
poll loop for twelve seconds. The fix is to shorten each attempt instead: 1500 ms
is still generous against a ~640 ms fleet norm, and four attempts fit in six
seconds. Verified end to end — the new settings give up at 6.01 s with
`no-response`, meaning the full cycle ran. The gateway warns at startup if the
outer timeout is below the budget.

**2. A single lost datagram was being reported as an outage.** This is the bigger
one. `DeviceState.online` was `consecutive_failures == 0`, so *one* missed poll
marked a suite offline, told its tenant it was unreachable, and wrote a "went
offline" line. Over UDP on Wi-Fi that is a routine event.

**This reframes the overnight counts below.** Those 7, 6, 5, 4, 2, 1 figures are
mostly single missed reads that recovered 30 seconds later, not outages. The
floor-2 concentration still stands — it is the same measurement across all units —
but the absolute numbers overstate how bad it is.

`offline_after_failures: 3` is about 90 seconds at a 30 s interval. Every failure
is still counted, so the Link quality table keeps its diagnostic value, and a
device now shows `missed 1` / `missed 2` before going offline — the early warning
the old behaviour buried.

Expect the log to go much quieter after this. That is the point, and it is not
evidence the radio problem is fixed.

### The overnight data says floor 2, not one bad thermostat

Recovered on the mini from `/usr/local/var/log/building-controls.log`, 22:00
Aug 3 → 06:14 Aug 4, an empty building with nobody's laptops or phones competing.
Offline *transitions* per unit:

```
7  Suite 205   floor-2      4  Suite 231   floor-2
6  Suite 221   floor-2      2  Suite 207   floor-2
5  Suite 339   floor-3      1  Suite 340   floor-3
```

**4 of 5 floor-2 units dropped overnight, against 2 of 11 on floor 3.** Suite 339
looked like the outlier on a single ping test; it is third, and the real pattern
is by floor.

Drops continued at a steady 2–5 per hour with the building empty, which rules out
contention from people and their devices.

So the obvious first experiment — pull 339 off its subbase and look for metal in
the wall — is the wrong one. **Start with the floor-2 access point and the 2.4 GHz
channel plan.** Neither is fixed by swapping a thermostat.

Note the per-device counters in the Link quality table reset on restart, so for
anything spanning a restart the log is the better source: it records transitions,
which survive.

## Still open, in priority order

0. ~~Restart the gateway~~ — **done 2026-08-03 18:14.** The second half is not:
   **confirm on a phone that `/` shows the door and light buttons for both
   accounts.** That fix has only ever been tested against a stubbed panel, never
   real hardware. The access log cannot settle it — `/` returns 200 on every
   15-second auto-reload whether or not `access_buttons()` produced anything,
   and no `POST /doors` or `POST /lighting` has come from `/` since the restart.
   Someone has to look at the screen.

   For contrast, what *has* been confirmed on real hardware, from `/t/326`: door
   unlock end to end (passkey registration, Face ID, panel relock), the explicit
   10-minute lighting button, and the lights an unlock turns on as a side effect
   — each verified physically at the building and against the audit table, not
   inferred from a 200.

1. **Cloudflare dashboard.** The **rate-limit rule on `/login` is set** — Herm
   did it 2026-08-04. Worth confirming once from a phone on cellular, since a
   rule that matches nothing looks identical to a rule that works: reload the
   login page and submit a wrong password a dozen times inside ten seconds, and
   expect Cloudflare's block page rather than the app's "Incorrect username or
   password". If the app's message keeps coming back, check the rule is scoped to
   the hostname and that `/login` is the path Cloudflare sees.

   Still open here: **Access on `/ui/*`** and **SSL Full (strict)**. Certificate
   Transparency publishes every hostname that gets a certificate, so scanners
   find these within hours, and door unlock is live behind this one.

   Note the in-process `LoginThrottle` remains the only per-account limit, it
   still resets on every restart, and it deliberately does not catch one password
   sprayed across many usernames — that case is exactly what the new edge rule
   covers, which is why the two are complementary rather than redundant.
2. **Rotate credentials.** The TruPortal service account password was exposed in
   a chat transcript; the bench operator account on the panel should be deleted;
   the seeded test account passwords are still the obvious ones. Herm has the
   specifics — they are deliberately not written down in a public repo.
3. **The access panel is reachable from the internet** and its password sits
   cleartext in `config/devices.yaml` (mode 600, gitignored). The VLAN work would
   fix both. Addresses are in the local config, not here.
4. **No unit tests.** `tools/test_*.py` drive a live server. `test_passkeys.py` is
   the exception and is genuinely self-contained (13 checks, no hardware).
5. Comments in the mini's own `config/devices.yaml` still describe a machine that
   does not exist — `4194302`, "two NICs on 192.168.1.0/24". Fixed in
   `devices.example.yaml`; the mini's copy is gitignored so it needs doing by
   hand. Comments only.

## Things I got wrong before, so you do not repeat them

- **There is no device 301.** It is `326` on the mini. 301 came from a worked
  example in a config comment and I repeated it as fact, which cost Herm a trip
  through a test that could never have passed.
- **NAT loopback was a red herring** I asserted as a "strongest suspicion" on no
  evidence. That earlier gateway failure was log file ownership.
- **The repo on the mini is at `/Users/Shared/building-controls`**, not under a
  home directory. I have assumed otherwise more than once.
- Shipping install steps that omit a prerequisite is the recurring theme in all
  of the above: the log directory for the gateway, then the log directory *again*
  for the backup, then the placeholder substitution. When adding a launchd job,
  write the `mkdir`/`chown`/`sed` into the documented procedure rather than
  trusting whoever runs it to remember.

## How to keep this document honest

It has been wrong twice by drifting rather than by being incorrect when written:
a commit count that stopped matching, and a "nothing has run on the mini" that
stayed after things had. If you push from the mini, update the header block —
commit count, and which of the two machines is actually running what.
