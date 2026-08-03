# Handoff: laptop session → mini, 2026-08-03

Four commits pushed from the laptop since `6a5cdde` (the last thing the mini
pushed). None of it has run on the mini. **The gateway there is still running a
process that predates all of it**, including `6a5cdde` itself.

    git pull --ff-only
    sudo launchctl kickstart -k system/com.building-controls.gateway
    curl -s localhost:8237/health

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

### `HEAD` — bulk load and backups

See the two sections below.

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

    deploy/backup.sh                       # → /usr/local/var/backups/building-controls
    sudo cp deploy/com.building-controls.backup.plist /Library/LaunchDaemons/
    sudo launchctl bootstrap system /Library/LaunchDaemons/com.building-controls.backup.plist

Nightly at 03:15, 30 kept. Backs up the database, `config/devices.yaml`, the
gateway plist, and the git SHA that was running. Everything else on the machine
comes back from `git clone` and `pip install`.

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

Restore steps are written into every backup directory as `RESTORE`.

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

## Open right now: the backup job wrote no log

Herm installed the backup plist on the mini and
`/usr/local/var/log/building-controls-backup.log` was not created.

**That is not "it ran and said nothing".** `backup.sh` prints on every path,
including failure. No log means the job never started, so do not go looking for a
bug in the script first. Three causes, in order:

```bash
sudo launchctl print system/com.building-controls.backup   # "Could not find" = never bootstrapped
grep CHANGEME /Library/LaunchDaemons/com.building-controls.backup.plist
ls -ld /usr/local/var/log                                  # must be writable by UserName
```

Copying a plist does not install it, and `kickstart` on a service that was never
bootstrapped fails without creating anything — that is the most likely answer.

Prove the script itself works first, since it is the faster test and it is
meaningful in the foreground (unlike the gateway, whose failure mode only exists
under launchd):

```bash
deploy/backup.sh ~/backup-test && ls -R ~/backup-test
```

Note `/usr/local/var` is root-owned on a machine that never installed Homebrew,
which alone is enough to stop the job. The install steps in DEPLOY.md now create
and chown both directories first; the version Herm used did not say to, which is
my omission, not his mistake.

## Still open, in priority order

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
  evidence. The launchd failure was log file ownership.
