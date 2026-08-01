# BMS — building management for Honeywell TC500A thermostats

An on-premises building management system for Honeywell TC500A commercial
thermostats over **BACnet/IP**. Polls thermostats, holds the building's intended
schedule in a database, reconciles that intent onto the devices, and exposes a
small web interface — including a phone-sized page a tenant can use to start the
air conditioning before they arrive.

Built and verified against real hardware: a TC500A-N running firmware
`01.01.16.00`.

> **Safety.** This software controls HVAC equipment in an occupied building. It
> is not life-safety rated and must not be relied on for freeze protection,
> smoke control, or anything an authority having jurisdiction inspects. Keep the
> thermostats' own limits and safeties configured and working.

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

## Corrections to the vendor documentation

The TC500A BACnet Integration Guide (31-00478) is wrong in several places that
matter. Verified on hardware:

| Guide says | Actually |
|---|---|
| Ch. 10: "thermostat does not support calendar object" | All 10 calendar objects work. `dateList` reads *and* writes, round-tripping exactly |
| Floating-date special events unsupported | `weekNDay` entries are accepted and honoured — "4th Thursday of November" works |
| Table 45: schedule object is named `EnumSchedule` | It is named `OccSchedule` |
| Holidays must be configured at the touchscreen | Fully configurable over BACnet |

Declaring the current day a holiday flips the device within ~3 seconds:
`schedule_state 0→1`, `effective_occupancy 1→2`, effective setpoints `68/76 →
55/85`.

Because floating rules are evaluated *by the device*, holidays never need
re-entering — no annual refresh, no three-year horizon problem.

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

> Always pin `bacnet.address`. A host with two NICs on one subnet will otherwise
> bind ambiguously and Who-Is can leave by the wrong interface.

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
- **AP client isolation must be off**, or nothing is reachable.

## First run

```bash
# Create the first account (blank password prompt generates a strong one)
.venv/bin/python -m bms.useradmin add-admin herm

.venv/bin/python -m bms --config config/devices.yaml
```

Then open <http://127.0.0.1:8237/>.

---

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

## The tenant page — `/t/{device_id}`

The one screen a tenant needs, designed for a phone held one-handed:

- Fixed duration buttons (1–4 hours), 56 px minimum touch targets. Nobody types
  "180" in a car park.
- Status in words — "Running now — 2h 47m left" — read from the device's own
  countdown, not from what we last asked for.
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

## Layout

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
  auth.py         scrypt passwords, server-side sessions, role checks
  api.py          HTTP surface
  tenant_page.py  the mobile bypass page
  useradmin.py    CLI for bootstrapping and recovery
tools/
  discover.py     dump every object on a device to JSON (commissioning aid)
  probe.py        read-only check of the points the app depends on
  write_test.py   which points accept writes; records, writes, reverts, verifies
  sim_tc500a.py   virtual TC500A, for developing with no hardware
  test_roles.py   authorisation matrix
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
anyone who can reach it. Put the thermostats on an isolated VLAN if you can. Do
not bind this application to a public interface — it refuses `0.0.0.0` on
startup. Reach it over Tailscale or WireGuard, and set `secure_cookies: true`
once it is served over HTTPS.

## Status

Working: discovery, polling, setpoints, occupancy override, bypass, weekly
schedules, groups, day overrides, holidays, dated exceptions, reconciliation,
auth and roles, audit log, tenant mobile page.

Not built yet: an operator UI beyond the status page and `/docs`; scheduled
(delayed-start) pre-cooling; per-holiday occupancy states on one device; lighting
control; access control integration.
