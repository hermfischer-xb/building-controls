# Handoff: laptop session → mini, 2026-08-03

Eight commits pushed from the laptop since `6a5cdde` (the last thing the mini
pushed).

**State of the mini as of the last exchange:**

- Repo pulled — it is at `fbb1927` or later.
- Backup job installed, bootstrapped and verified: `last exit code = 0`, log
  written, first snapshot in `/usr/local/var/backups/building-controls`.
- **The gateway has NOT been restarted.** It is still running a process that
  predates `6a5cdde`, so none of the code below is live yet. Until this runs, the
  dashboard has no door buttons and the verification window is still 120s:

```bash
sudo launchctl kickstart -k system/com.building-controls.gateway
curl -sS localhost:8237/health
```

The `code version` line in a backup log is the *repo's* SHA, not the running
process's — do not read it as proof the gateway restarted.

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

## Still open, in priority order

0. **Restart the gateway** (top of this document). Everything under *What
   changed* is inert until then, including the reported missing door buttons.
   Then confirm on a phone that `/` shows them for both accounts — that fix has
   only ever been tested against a stubbed panel, never real hardware.

1. **Cloudflare dashboard.** Access on `/ui/*`, a rate-limit rule on `/login`,
   SSL Full (strict). Certificate Transparency publishes every hostname that gets
   a certificate, so scanners find these within hours — and door unlock is now
   live behind this one. The only rate limiting today is `LoginThrottle`, which
   is in-process and dies with every restart. **This is the highest-value
   remaining item and it is not code.**
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
