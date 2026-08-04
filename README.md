# building-controls

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)

On-premises building controls for a small commercial building. Today it manages
**Honeywell TC500A** thermostats over **BACnet/IP**: polling them, holding the
building's intended schedule in a database, reconciling that intent onto the
devices, and serving a small web interface — including a phone-sized page a
tenant can use to start the air conditioning before they arrive.

It is structured to grow into lighting and access control; see
[Roadmap](#roadmap).

Built and verified against real hardware: a **TC500A-N** on firmware
`01.01.16.00`. Along the way it documents several places where the vendor's own
integration guide is **wrong** — see
[Corrections to the vendor documentation](#corrections-to-the-vendor-documentation).

> [!WARNING]
> **This controls HVAC equipment in an occupied building.** It is not
> life-safety rated and must not be relied on for freeze protection, smoke
> control, or anything an authority having jurisdiction inspects. Keep the
> thermostats' own limits and safeties configured and working. See
> [Project status](#project-status) before deploying it anywhere that matters.

## Project status

**Early but working.** Every feature listed below has been exercised against a
real thermostat, not just unit-tested. It currently runs against a single-device
bench setup; it has not yet run a full building unattended for months.

Interfaces should be considered unstable — the HTTP API and the database schema
may change without a migration path until there is a tagged release.

---

## Why the database is the source of truth

Three facts, each established by testing against the hardware rather than read
from the manual:

- **The writable points have no BACnet priority array.** Setpoints,
  `ni_OccManCom` and the bypass points are plain Value objects; writes are
  last-writer-wins and a write priority is accepted and silently ignored. There
  is no protocol-level arbitration between this system and anything else on the
  network.
- **The thermostats do not support COV.** Nothing is pushed; every value is the
  result of the last poll. `read-property-multiple` is supported, which is what
  makes polling cheap (~19 ms for 16 points, versus ~197 ms read individually).
- **A write can be applied without being acknowledged.** A failed write response
  does *not* mean the write failed. The only way to know is to read back.

So the database holds what the building *should* do, the thermostats hold what
they *are* doing, and a reconciler closes the gap on a loop.

## Vendor documentation

Honeywell publishes both manuals. They are proprietary and not redistributed
here, so these link to Honeywell's own copies:

- [TC500A User Guide (31-00400M-11)](https://prod-edam.honeywell.com/content/dam/honeywell-edam/hbt/en-us/documents/manuals-and-guides/user-manuals/hon-ba-bms-TC500A-User-Guide-31-00400M-11.pdf?download=false)
- [TC500A BACnet Integration Guide (31-00478-06)](https://prod-edam.honeywell.com/content/dam/honeywell-edam/hbt/en-us/documents/manuals-and-guides/user-manuals/hon-ba-bms-TC500A-BACnet-Integration-Guide-31-00478-06.pdf?download=false)

Read the next section before trusting either of them on schedules and holidays.

## Corrections to the vendor documentation

**The BACnet Integration Guide is wrong about calendars and holidays**, and still
wrong in revision **31-00478-06**, the current published version as of August
2026. Each of these was verified by writing to a TC500A-N on firmware
`01.01.16.00` and reading the result back:

| The guide says | The hardware does |
|---|---|
| Ch. 10: *"Current implementation of thermostat does not support calendar object ... HMI is the option to configure holiday schedule"* | All 10 calendar objects work. `dateList` reads **and** writes, round-tripping exactly. Holidays are fully configurable over BACnet |
| Ch. 10: *"Thermostat does not support floating date type special events"* | `weekNDay` entries are accepted and honoured — "4th Thursday of November" works |
| Table 45: the schedule object is named `EnumSchedule` | It is named `OccSchedule`. The same document's proprietary-properties list also says `OccSchedule`, so it contradicts itself |
| `ni_BypassValue` is described as the "Bypass Value to enable Bypass Time" | It does **not** set the duration. `Cfg_Thermostat_BypOverrideTime` does. Write only `ni_BypassValue` and every bypass runs for whatever the config point holds, silently ignoring the request |
| `ni_OccManCom` is listed as the network occupancy override, with no preconditions | Inert until `Cfg_Thermostat_Override` is enabled. Without it the write succeeds, the point reads back the new value, and effective occupancy never moves |
| Ch. "List of all BACnet objects" includes 31 `Gui_*` points, among them `Gui_WiFiStatus`, `Gui_LEDStatus` and `Gui_ApplicationRevision` | **None of them exist.** The device advertises 770 objects and not one is `Gui_*`. There is no Wi-Fi signal strength, LED state or GUI diagnostic available over BACnet, whatever the point list says |

Two of those are the nastiest kind of bug: the write is accepted, the value
reads back correctly, and nothing happens. `no_BypassRemTime` is similar — it
reports the configured bypass period and was not observed decrementing, so this
project states it as a period rather than a live countdown.

Declaring the current day a holiday flips the device within ~3 seconds:
`schedule_state 0→1`, `effective_occupancy 1→2`, effective setpoints `68/76 →
55/85`.

The practical consequence is large. Because floating rules are evaluated *by the
device*, holidays are entered once as rules and never need refreshing — no annual
re-entry, no three-year horizon problem, and no need to touch 25 touchscreens.
Anyone following the guide would have built the far worse workaround.

`tools/write_test.py` reproduces all of this: it records each value, writes,
reads back, reverts, and verifies the revert.

---

## Requirements

- Python 3.12+ (developed on 3.14)
- A host on the **same subnet** as the thermostats — BACnet discovery is
  broadcast-based. If it must sit elsewhere, set `foreign_bbmd`.
- TC500A thermostats with **BACnet/IP enabled** (which is Wi-Fi only on the
  TC500A-N; it cannot do BACnet/IP and MS/TP at the same time).

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config/devices.example.yaml config/devices.yaml
```

Edit `config/devices.yaml`: set `bacnet.address` to **this host's** interface
with its prefix, and list your thermostats.

```yaml
bacnet:
  address: 192.168.1.10/24    # pin the interface explicitly
devices:
  - device_id: 301            # must match the thermostat's BACnet Device ID
    address: 192.168.1.101
    name: Room 301
    zone: floor-3             # tenants are granted access by zone
```

> Always pin `bacnet.address`. On a host with more than one interface — an office
> LAN plus a second adapter on the thermostat subnet is the usual arrangement —
> bacpypes3 otherwise binds whichever it picks, and broadcasts leave by the wrong
> one.

## Commissioning a thermostat

**Order matters.** A new unit upgrades its own firmware the first time it reaches
the internet, so give it internet *before* it goes onto the isolated control
network:

1. Join a normal internet Wi-Fi (**Local Router** mode) and let it self-upgrade.
   Check the version under **System Status → Device information**.
2. Switch to **BACnet IP** mode, set a unique Device ID and a static address.
3. Move it onto the control VLAN.

Once on an isolated VLAN it has no route to Honeywell's cloud and will not
receive further OTA updates. That is a deliberate trade — see *Networking* below.

## Configuring a thermostat

On the touchscreen: **Config → Connection → Wi-Fi** → enable → **BACnet IP** →
**BACnet Network Settings** → set a **unique Device ID**, Network Number and UDP
port (47808) → DHCP or static. Requires the installer passcode.

- Out of the box every thermostat reports device id `4194302`. Twenty-five units
  all reporting that will collide; the gateway warns at startup if the id does
  not match your config.
- Note the MAC from **System Status → Network Status** and make a DHCP
  reservation.
- Selecting BACnet IP disables the Honeywell Occupant app.
- Switching between IP and MS/TP reboots the device.
- **AP client isolation may stay on.** It blocks broadcast discovery, but the
  gateway is config-driven and every poll and write is unicast, so only
  `tools/discover.py` is affected — run it during commissioning, or use
  `--target <ip>` to unicast at a known address.

## First run

```bash
# Create the first account (blank password prompt generates a strong one)
.venv/bin/python -m bms.useradmin add-admin herm --no-change-required

.venv/bin/python -m bms --config config/devices.yaml
```

Then open <http://127.0.0.1:8237/>.

`--no-change-required` is right here and almost nowhere else: you are creating
your *own* account, so nobody else learns the password. Every other account is
created by someone who is not its owner — see below.

## Commissioning a building, not a thermostat

Twenty-five thermostats and their tenants are too many to hand-type without
transposing a digit, and a transposed digit is a device that silently never
answers. Both loaders validate the whole file before writing anything.

```bash
# Devices -> the `devices:` block for config/devices.yaml
.venv/bin/python tools/devices_from_csv.py thermostats.csv
```

Columns `device_id, address, name, zone, mac`. Rejects duplicate ids, duplicate
addresses, malformed IPs, and `4194302` — the unconfigured default, which two
units would report simultaneously. It prints rather than editing
`config/devices.yaml`, because that file is mostly comments recording traps that
cost real time to find and every YAML library here drops them on a round trip.

```bash
# Accounts -> the database
.venv/bin/python -m bms.useradmin import-csv tenants.csv --dry-run
.venv/bin/python -m bms.useradmin import-csv tenants.csv --passwords pw.csv
```

Columns `username, display_name, role, zones, password`; only `username` is
required. Blank password means one is generated. Re-running skips accounts that
already exist. `--passwords` writes a file created mode 600, not chmod'ed
afterwards, so there is no window in which it is readable.

Given `--config`, it warns when a tenant's zone matches no device — the failure
that otherwise presents as a working login onto an empty page. See
`tools/thermostats.example.csv` and `tools/tenants.example.csv`.

## Backups

```bash
deploy/backup.sh                    # -> /usr/local/var/backups/building-controls
```

Database, config, plist, and the git SHA that was running; 30 kept; restore
instructions written into every backup directory, and each one holds a single
standalone database file. To run it nightly, see *Install the launchd jobs* in
[deploy/DEPLOY.md](deploy/DEPLOY.md) — the plists carry `CHANGEME` placeholders
that are substituted at install time, and a job installed unedited fails before
it can write a log explaining why.

This protects against a bad edit, a botched upgrade, a corrupted database, and
"what did the schedule look like last week". It does **not** protect against the
disk failing, since the copies live on that disk — point Time Machine or an
`rsync` at the destination for that, bearing in mind it carries password hashes
and the access panel's credentials. The destination sits outside the repo so a
bad checkout cannot take the backups with it.

**The gateway does not need to be stopped**, because it uses `sqlite3 .backup`
rather than copying the file. That distinction is not cosmetic: measured against
a live gateway-style connection, a plain `cp` of `bms.db` produced a file in
which `app_user` did not exist as a table, because 292 KB of committed data was
still in `bms.db-wal` awaiting a checkpoint. Each run then verifies its own
output with `PRAGMA integrity_check` and a non-zero account count, so a run that
reports success has been checked rather than assumed.

---

## The web interface

Server-rendered pages with small islands of vanilla JavaScript — no build step,
no `node_modules`, nothing to bundle or deploy. For a handful of screens over 25
devices that would be more machinery than the problem needs, on a host expected
to run unattended.

| Page | Who | What |
|---|---|---|
| `/` | everyone | Dashboard: every visible device, temperatures, what the equipment is doing, outdoor conditions |
| `/ui/devices/{id}` | everyone | Setpoints, override, bypass, live weekly schedule, zone |
| `/ui/zones` | manager | Device-to-zone mapping and tenant access |
| `/ui/schedules` | manager | Schedule groups and device assignment |
| `/ui/holidays` | manager | Holiday rules, dated one-offs, work-through |
| `/ui/system` | manager | Reconcile status, clock drift, audit log |
| `/ui/users` | admin | Accounts and zone grants |
| `/t/{id}` | everyone | The mobile tenant page (below) |
| `/docs` | everyone | Interactive API explorer |

Pages read from the poll cache in process, but every *mutation* goes back through
the JSON API from the browser. Authorisation therefore lives in exactly one place
and the interface cannot grant something the API refuses; the role filtering in
the navigation only avoids dead ends.

The dashboard shows what each unit is actually doing, driven by **active stage
counts** rather than by the selected mode — a unit sitting in HEAT with nothing
energised is idle, and showing a flame for it would make every row look busy:

❄ cooling · 🔥 heating (and `aux` for backup heat) · 🍃 economizer · 💨 fan, which
spins while running and respects `prefers-reduced-motion`.

## Roles

| | tenant | manager | admin |
|---|:--:|:--:|:--:|
| View own zone, start bypass | ✅ | ✅ | ✅ |
| Setpoints, occupancy override | | ✅ | ✅ |
| Schedules, groups, holidays, exceptions | | ✅ | ✅ |
| Reconcile, audit log | | ✅ | ✅ |
| Create and remove users | | | ✅ |

Tenants are scoped to **zones**. A device outside a tenant's zones returns 404,
not 403 — they should not learn which other devices exist.

```bash
.venv/bin/python -m bms.useradmin add-tenant suite301 --zones floor-3
.venv/bin/python -m bms.useradmin add-manager bldgmgr
.venv/bin/python -m bms.useradmin list
```

Passwords use scrypt. Sessions are server-side and revocable; changing a password
invalidates them. Login is throttled per username *and* client address.

### Nobody else gets to know your password

Accounts are created by someone other than their owner, which means the creator
necessarily sees the first password. There is deliberately **no field to type one
into**: the server generates it, returns it once, and the account can do nothing
until its owner replaces it.

1. An admin creates the account. A 16-character one-time password appears once on
   the Users page, to be handed over however is convenient.
2. That password signs in and goes straight to *Choose your password*. Every
   other page redirects there, and every API call returns 403 with
   `password_change_required` — a forced change that can be navigated around is
   not a forced change.
3. Setting a new one clears the flag and revokes every session for the account,
   including any the admin might have opened. The browser doing the changing gets
   a fresh session so it is not signed out mid-task.

The change form asks for the current password and the new one **twice**. The
match is checked in the browser, because the server is sent a single value and
cannot compare two — a confirmation field is only meaningful where both are
visible.

Administrative resets work the same way. `PUT /users/{u}/password` takes **no
body** — it generates a fresh one-time password, returns it once, and marks the
account must-change, exactly as creation does. Setting the flag alone would not
have been enough: an admin who *chooses* the password still knows it, and a
human-chosen one may be weak or reused besides.

`--no-change-required` on `useradmin add-*` and `useradmin passwd` opts out, and
is correct only when the account is your own — `passwd` is the lockout recovery
path, where being told to change the password you just deliberately set is
absurd.

`tools/test_passwords.py` covers the lifecycle end to end — that the password is
generated rather than accepted from the caller, that it unlocks nothing but the
change form, and that it stops working once replaced.

## The tenant page — `/t/{device_id}`

The one screen a tenant needs, designed for a phone held one-handed:

- Fixed duration buttons (1–4 hours), 56 px minimum touch targets. Nobody types
  "180" in a car park.
- Status in words — "Running now — about 2h" — read back from the device, not
  from what we last asked for. Stated as a period rather than a countdown
  because `no_BypassRemTime` reports the configured duration and was not
  observed decrementing.
- Self-contained: no framework, no external assets, loads instantly on cellular.
- A failed write never claims failure, because it may still have been applied.

This is safe to expose to tenants without an approval workflow because the
**bypass timer expires on the device by itself**. The worst a stray tap costs is
a few hours of conditioning in a room they already have access to. The
non-expiring occupancy override is manager-only for exactly that reason.

## Schedules

Three layers, most specific winning:

```
schedule group   the tenant's normal week ("Standard weekday", "Six-day", …)
day override     that tenant's deviation on one weekday
exception        a dated one-off, applied by the device over both
```

Day-granularity overrides mean a group-wide change still reaches every day a
tenant has not specifically customised.

Holidays and dated exceptions **share one BACnet property**, so the reconciler
composes them together — priority 1 for one-offs, 2 for holidays, matching the
documented precedence of event over holiday over standard schedule.

```bash
curl -X POST localhost:8237/groups/seed-defaults        # 4 starting templates
curl -X POST localhost:8237/holidays/seed-us-federal    # 10 US federal holidays
curl -X POST localhost:8237/reconcile                   # push to devices now
```

The reconciler also runs every 5 minutes and is idempotent — it reports
`already_correct` and writes nothing when devices match intent.

## Zones

A zone groups thermostats for tenant access and for zone-scoped holidays.

**Config seeds a zone; the database owns it.** The split follows what changes and
who changes it — a device's BACnet id, address and MAC are facts about hardware,
but a zone changes when a tenant takes a different suite, and that is an
operational act a manager should perform at 4pm on a Friday rather than a YAML
edit and a restart. `/ui/zones` shows the mapping and moves devices between
zones; the change takes effect immediately for both access and scheduling.

A device has **exactly one zone by construction**: the override table is keyed on
device id so it can hold at most one, and a device without an override falls back
to the zone it was commissioned into.

Tenant grants are checkboxes of zones that exist, and the API validates
independently — a typo like `floor3` for `floor-3` is refused rather than
silently granting access to nothing.

## Holidays

Stored as **rules**, not dates, and scoped three ways:

| Scope | Example |
|---|---|
| `global` | The building closes on Christmas Day |
| `zone` | One tenant's company observes Good Friday |
| `device` | A single suite, for one-off cases |

Floating rules (`weekNDay`) are evaluated *by the thermostat*, so "4th Thursday
of November" is entered once and never refreshed.

When a tenant says they are working through a holiday, the **"Working…"** action
on that holiday creates a dated, zone-scoped exception which the device applies
*over* the holiday, leaving it intact for everyone else. Verified on hardware:
with a global holiday in force the device sat Unoccupied at 55/85, and the zone
exception moved it to Occupied at 68/76.

## Outdoor air

Only one thermostat needs a physical outdoor sensor. **The thermostats cannot
share it between themselves** — no COV, and they never originate reads, so there
is no mechanism by which one could push to another. `ni_OutdoorTemp` is a
writable input backed by a 600 second watchdog (`Cfg_NetOATFailDetDly`), and a
device fed by peers would not need one.

So the gateway reads the sensor and writes it to the rest:

```
Room 301 (sensor) ──read──> gateway ──write ni_OutdoorTemp──> every other room
```

All traffic stays server-to-device, so **wireless client isolation can remain
enabled** and no thermostat-to-thermostat access is required.

```yaml
outdoor_sensor_device_id: 301     # null until a sensor is fitted
outdoor_weather:                  # fallback, and cover before one is installed
  enabled: true
  zip_code: "91436"
```

Selecting BACnet/IP disables the thermostat's own internet path and an isolated
VLAN removes it entirely, so the zip-code outdoor temperature it would otherwise
display is gone — but the gateway is dual-homed and has internet, so it supplies
the same value through the same input. Open-Meteo needs no API key.

Precedence is **sensor first, weather second**: a physical sensor is the actual
air at this building and is what economizer decisions are made from, while a
failed sensor should cost accuracy rather than the whole signal. Every weather
failure is non-fatal — a service outage must never stall a reconcile.

> `65535` is the device's "no value" sentinel, not a temperature. Sharing it
> would set a 65,535°F outdoor temperature on every other unit, so an invalid
> reading is dropped rather than propagated.

## Clock sync

The thermostats have no time source on an isolated VLAN, and the vendor guide
says daylight saving cannot be written over BACnet. So the gateway is the clock:
every reconcile cycle it reads each device's clock, and pushes this host's
**local wall-clock time** whenever drift exceeds `max_clock_drift_seconds`.

Sending local rather than UTC time is deliberate — the device has no timezone
awareness (it reports `utcOffset 0` regardless), so DST transitions are computed
here and the device never needs to know about them.

```
changed: ['clock: drift +1499s -> -0s']
```

Because `TimeSynchronization` is an unconfirmed service with no acknowledgement,
the reconciler re-reads the clock afterwards and raises an error if the sync did
not take. Current drift per device is in `GET /reconcile`.

> If a thermostat has its own DST setting enabled at the touchscreen it may also
> self-adjust. The sync corrects any disagreement within one cycle, but expect a
> few minutes of wrong time at a transition unless you disable DST on the device.

---

## Repository layout

```
bms/
  bacnet.py       single owner of the BACnet socket; RPM reads, writes, schedules
  points.py       the ~18 points that matter, with real instance numbers
  poller.py       poll loop; sequential by design (one socket, one lock)
  cache.py        last-known state, with age and staleness as first-class ideas
  store.py        SQLite intent: holidays, groups, exceptions, users, audit
  schedules.py    group + override resolution, BACnet conversion
  holidays.py     fixed / range / floating rules -> calendar entries
  reconciler.py   pushes intent onto devices, detects drift
  zones.py        resolves a device's zone: config seeds it, the database owns it
  weather.py      public outdoor conditions, key-free, every failure non-fatal
  auth.py         scrypt passwords, server-side sessions, role checks
  api.py          HTTP surface
  ui/
    layout.py     shared shell, CSS, activity icons, escaping
    pages.py      dashboard, device, zones, schedules, holidays, system, users
    routes.py     page routes; render only, mutations go via the JSON API
  tenant_page.py  the mobile bypass page and the login form
  passkeys.py     WebAuthn registration and step-up verification
  passkey_js.py   the browser half, shared by both interfaces
  truportal.py    async SOAP driver for the access panel: doors and lighting
  useradmin.py    CLI for bootstrapping, recovery and bulk import
tools/
  discover.py     dump every object on a device to JSON (commissioning aid)
  probe.py        read-only check of the points the app depends on
  write_test.py   which points accept writes; records, writes, reverts, verifies
  sim_tc500a.py   virtual TC500A, for developing with no hardware
  test_roles.py   authorisation matrix
  test_proxy.py   login throttle keys on the real client, not the proxy
  test_passkeys.py  passkey security properties, against a software authenticator
  test_passwords.py one-time passwords: generated, forced change, single use
  devices_from_csv.py  a spreadsheet of thermostats -> the devices: block
  truportal_soap.py    raw SOAP calls, for exploring the panel
deploy/
  DEPLOY.md       the macOS traps, in the order they bite
  backup.sh       consistent snapshot of database + config; verifies its output
  *.plist         LaunchDaemons for the gateway and the nightly backup
```

## Developing without hardware

```bash
.venv/bin/python tools/sim_tc500a.py --address 192.168.1.10:47809 --instance 2001
```

Then add it to `devices.yaml`. The simulator mirrors the real object map,
including the quirk that the device writes an explicit `00:00` entry on closed
days rather than leaving a day empty.

> One caveat: bacpypes3's `match_date_range` compares raw tuples with no wildcard
> handling, so an all-wildcard `effectivePeriod` — which is what real hardware
> reports — never matches and schedule evaluation returns `None`. The simulator
> uses a wide concrete range instead. Read paths must still expect wildcards.

## Deployment notes

Nothing here survives a reboot yet. Before this is load-bearing:

- **FileVault**: if enabled, an unattended reboot leaves the disk locked and the
  machine never rejoins the network. Decide deliberately.
- `sudo pmset -a sleep 0 disablesleep 1 autorestart 1 standby 0 powernap 0`
- Run as **LaunchDaemons**, not LaunchAgents — daemons start without a login.
- Turn off automatic macOS updates, or an unattended upgrade stalls at a setup
  screen and takes the building offline.

**Networking.** BACnet has no authentication or encryption whatsoever, and the
TC500A honours `device-communication-control` and `reinitialize-device` from
anyone who can reach it. Anyone on the same network can reboot every thermostat
in the building.

Put the thermostats on their **own VLAN**, with a dedicated SSID mapped to it and
no inter-VLAN routing except to the gateway host. Note that a separate *IP
subnet* on a shared VLAN is not isolation — anything on that L2 can simply add an
address in your range and reach the devices. A MAC ACL controls which stations
may *associate*; it does not control who can reach them afterwards. Only the VLAN
boundary does that.

The gateway then sits dual-homed: one interface on the control VLAN (pinned via
`bacnet.address`), one on the normal LAN. Do not bind this application to a
public interface — it refuses `0.0.0.0` on startup.

**Remote access.** Tenants will not install a VPN client, so anything they use
needs a publicly trusted certificate. An outbound-only tunnel (Cloudflare Tunnel
or similar) suits this topology better than opening a port: the gateway bridges
to equipment that has no authentication of its own, so there should be nothing
inbound to attack. Once TLS terminates in front:

```yaml
secure_cookies: true                  # AFTER https works, never before
behind_proxy: true                    # only then is the client-IP header trusted
client_ip_header: cf-connecting-ip    # or x-forwarded-for
```

Set `secure_cookies` *after* HTTPS is working. Enabled on plain HTTP, the browser
accepts the cookie and never sends it back, so login appears to succeed and
silently does not stick.

`behind_proxy` matters more than it looks. Behind a proxy every request arrives
from `127.0.0.1`, so a login throttle keyed on that would put every failed login
in one bucket and let eight bad passwords lock out the whole building. The
client-IP header is trusted *only* when this is set — believing it
unconditionally would be worse than not having it, since a direct caller could
forge a fresh address per attempt and never be throttled. `tools/test_proxy.py`
asserts both directions.

An isolated VLAN needs no internet: clock sync comes from the gateway over
BACnet, and firmware is applied at commissioning time before the device moves
onto the control network.

## Hardware tested

| Device | Status |
|---|---|
| Honeywell TC500A-N, firmware `01.01.16.00` | ✅ verified end to end |
| Other TC500A variants (`-W`, MS/TP) | ❔ untested; MS/TP would need a different transport |
| Anything else | ❌ not supported yet |

`tools/discover.py` will dump any BACnet device's object map, so it is a
reasonable starting point for adapting this to other equipment.

### MS/TP over the old LonWorks wiring — ruled out at this building

**Not viable at 16400 Ventura.** The existing LonWorks cabling is home-run stars
with no terminators anywhere, confirmed by the person who supervised its
installation. RS-485 needs a single linear daisy chain; a star of spurs is the
one topology it cannot be talked into. Reusing that wire would mean pulling new
wire, at which point the wire is not being reused.

Multi-port RS-485 repeater hubs do exist, which turn each spur into its own
terminated segment. For sixteen home runs that is several industrial hubs, their
power supplies and the labour to land and terminate every leg — against a box of
spare access points that cost nothing and go in this afternoon. It is the wrong
trade here.

The rest of this section is kept for a future building, or in case someone
proposes this again.

Buildings converted from LonWorks often still have twisted pair running to every
thermostat, and the TC500A does speak BACnet MS/TP (Config → Connection → BACnet
MS/TP; it auto-detects baud). Before anyone plans around that, four things
determined by measurement rather than optimism:

- **`bacpypes3` has no MS/TP link layer.** Version 0.0.106 ships `ipv4`, `ipv6`
  and `sc` only. This gateway cannot drive an RS-485 segment directly, and that
  is not a small patch.
- **It does not need to.** A BACnet MS/TP-to-IP router puts the segment on the IP
  network, and `bacpypes3` addresses devices behind it natively:
  `Address("2:5")` parses as `RemoteStation 2:5`. So `address: "2:5"` in
  `devices.yaml` reaches MS/TP station 5 on network 2 **with no code changes** —
  the transport problem is solved by buying a router, not by writing one.
- **The TC500A-N cannot do both at once**, and its BACnet/IP is Wi-Fi only. Any
  unit moved to MS/TP leaves the wireless network entirely. A mixed fleet is
  fine — the gateway would simply have some devices addressed by IP and some by
  `network:mac`.
- **Polarity is the least of the wiring risks.** RS-485 is polarity sensitive and
  LonWorks FT-10 was not, but that is a consistency problem: get it backwards and
  the device is silent, so swap the pair. The risks that actually sink these
  conversions, in order:
  1. **Topology.** FT-10 is free topology — stars, T-taps and spurs are all legal.
     RS-485 requires a single linear daisy chain, 120 Ω at the two physical ends,
     and stubs of inches. Home runs to a panel will not work reliably no matter
     how carefully they are landed. **Trace the existing runs first** — this is
     the question that decides it, and at this building the answer was stars, so
     the other three points never got a chance to matter.
  2. **No signal common.** FT-10 is transformer-coupled two-wire, so no reference
     conductor was pulled. RS-485 needs receivers to stay inside a -7 V to +12 V
     common-mode window, usually satisfied through shared building ground, but
     not guaranteed across separate panels.
  3. **Unshielded pair beside contactors.** Workable, helped considerably by
     running the segment slower.

The honest summary: polarity is trivial, topology decides it, and the gateway
needs a router appliance either way. Here topology decided it — the answer is
more access points.

## Roadmap

The near-term work is a **driver abstraction** — extracting the TC500A-specific
code behind a device interface so the scheduling, holiday and reconciliation
layers become device-agnostic. Everything below depends on it.

- [ ] `drivers/` split, with the scheduling layer dealing in occupancy states
      rather than BACnet objects
- [ ] **Lighting**, via a BACnet/IP relay module. Choosing BACnet means lighting
      inherits the existing poller, schedule groups, holiday calendars,
      reconciler and roles almost for free
- [ ] **Access control**, via Axis A1210/A1610 network door controllers over the
      VAPIX HTTP API — chosen for a publicly documented API with no NDA or
      partner fee
- [ ] **Badge-driven comfort**: first badge-in on a weekend starts that suite's
      HVAC and lights; last badge-out plus a timeout releases them
- [ ] Delayed-start pre-cooling ("be cool by 2pm" rather than "start now")
- [ ] A devices page — adding or re-addressing a thermostat is still a config
      edit and a restart, which is defensible for hardware but not for much else
- [ ] Per-holiday occupancy states on one device (the device has ten calendar
      objects; only one is currently used)

Access control will integrate with a listed, purpose-built controller rather than
driving Wiegand and door relays directly. Door hardware on egress paths is
life-safety territory with UL 294 and NFPA 101 implications, and the controller
must keep working when this application is down.

## Contributing

Issues and pull requests are welcome, particularly:

- **Other BACnet thermostats.** The hard part of this project was establishing
  what the hardware actually does. A `tools/discover.py` dump from a different
  model is genuinely useful even without code.
- **Corrections.** If something here contradicts your hardware, say so — the
  vendor documentation already contradicts mine.

Running the tests needs a thermostat or the simulator plus a running gateway:

```bash
.venv/bin/python tools/sim_tc500a.py --address 192.168.1.10:47809 --instance 2001
.venv/bin/python -m bms --config config/devices.yaml

BMS_ADMIN=herm:secret BMS_MANAGER=bldgmgr:secret BMS_TENANT=suite301:secret \
    .venv/bin/python tools/test_roles.py
```

Never commit `config/devices.yaml`, `data/`, `tools/discovery.json`, or vendor
PDFs — all are gitignored, and all contain either credentials or a real
building's addressing.

## Security

BACnet is unauthenticated by design and this software controls physical
equipment. If you find a vulnerability, please report it privately via GitHub
Security Advisories rather than opening a public issue.

Known and accepted limitations, documented rather than hidden:

- The gateway trusts anything that can reach its port; it is intended to bind
  loopback behind a VPN, and refuses to bind `0.0.0.0`.
- There is no rate limiting on the API beyond login attempts.
- Session cookies are only `Secure` when `secure_cookies: true` is set, which
  requires serving over HTTPS.

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for attribution and
trademark information.

Honeywell, TC500A, Axis and Ruckus are trademarks of their respective owners.
This project is not affiliated with or endorsed by any of them. Vendor
documentation is proprietary and is deliberately not included in this
repository — only findings derived from testing hardware.
