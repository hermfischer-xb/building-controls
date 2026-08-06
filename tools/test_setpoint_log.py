#!/usr/bin/env python3
"""Check that an occupant's temporary setpoint change reaches the audit log.

The poll cache is in memory and point-in-time. It shows that a suite is nudged
right now and loses the fact when the occupant clears it or the schedule rolls
over -- so without this, nothing in the system would ever record that somebody
turned a suite up on a Tuesday afternoon.

The sequence below is the real one: on 2026-08-04 Suite 314 was found sitting at
`cool_adjust = -1.0` with `no_SetpointSts` reporting Temporary, while its
occupant was in. This replays that, then what happens next.

Transitions only, deliberately. Thirty-five values every thirty seconds is a
time-series problem wanting a table of its own; the question a manager actually
asks is who has been fiddling with the heating, and that is answered by the few
moments the answer changes.

    .venv/bin/python tools/test_setpoint_log.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from bms.poller import Poller
from bms.cache import Cache
from bms.store import Store

DEV = types.SimpleNamespace(device_id=314, name="Suite 314", address="10.0.0.14", zone="floor-3")

class Cfg:
    devices = [DEV]
    poll_interval_seconds = 30
    request_timeout_seconds = 7
    offline_after_failures = 3
    poll_concurrency = 1

class Client:
    concurrency = 1
    def __init__(s): s.script = []
    async def read_device_id(s, d): return d.device_id
    async def read_points_timed(s, d): return s.script.pop(0), 640.0

def reading(status, heat=0.0, cool=0.0, sp=76.0, temp=74.0):
    v = {"heat_adjust": heat, "cool_adjust": cool,
         "adjust": 0.0, "effective_sp": sp, "space_temp": temp}
    # status=None models a poll that succeeded with this one object missing.
    if status is not None:
        v["setpoint_status"] = status
    return v


async def dropped_point_forges_nothing() -> int:
    """A lost point must not read as an occupant standing at the thermostat.

    `read_points_timed` does not fail a whole poll when one object comes back as
    an error -- it warns and leaves the key out -- so a *successful* poll can
    land without `setpoint_status`. The poll after that one then had nothing to
    compare against, called it a transition, and wrote a row attributing it to
    an occupant.

    Nobody touches the thermostat anywhere in this sequence. The correct number
    of audit rows is zero.
    """
    store = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    cache = Cache(stale_after=90, offline_after=3)
    client = Client()
    p = Poller(Cfg(), client, cache, types.SimpleNamespace(of=lambda d: "floor-3"), store)
    cache.register(314, "Suite 314", "floor-3", "10.0.0.14")

    client.script = [
        reading(2),      # baseline
        reading(2),      # steady
        reading(None),   # multi-state-output,7 errored; the rest of the poll is fine
        reading(2),      # back, same value it has held throughout
        reading(2),      # steady
    ]
    for _ in range(len(client.script)):
        await p.poll_once()

    rows = [r for r in store.recent_audit(20) if r["action"].startswith("setpoint")]
    ok = not rows
    print(f"  {'ok  ' if ok else 'FAIL'} a dropped point forges no occupant event"
          + ("" if ok else f"   <- wrote {[r['action'] for r in rows]}"))

    # The real transition still has to survive the guard: the point coming back
    # must not swallow an adjustment that happens after it.
    client.script = [reading(3, cool=-2.0)]
    await p.poll_once()
    rows = [r for r in store.recent_audit(20) if r["action"].startswith("setpoint")]
    ok2 = [r["action"] for r in rows] == ["setpoint.temporary"]
    print(f"  {'ok  ' if ok2 else 'FAIL'} and a genuine adjustment after it is still recorded"
          + ("" if ok2 else f"   <- got {[r['action'] for r in rows]}"))

    store.close()
    return (not ok) + (not ok2)


async def main():
    db = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    store = Store(db)
    cache = Cache(stale_after=90, offline_after=3)
    client = Client()
    p = Poller(Cfg(), client, cache, types.SimpleNamespace(of=lambda d: "floor-3"), store)
    cache.register(314, "Suite 314", "floor-3", "10.0.0.14")

    # Exactly the sequence the mini saw, then what happens next.
    client.script = [
        reading(3, cool=-1.0),          # restart finds it already nudged
        reading(3, cool=-1.0),          # steady, should log nothing
        reading(3, cool=-3.0),          # occupant pushes it further
        reading(3, cool=-3.0),          # steady again
        reading(2),                     # schedule rolls over to Unoccupied
        reading(2),                     # steady
        reading(3, heat=2.0),           # nudged again, heating side this time
    ]
    for _ in range(len(client.script)):
        await p.poll_once()

    expected = [
        "setpoint.temporary.observed",   # found already nudged after a restart
        "setpoint.temporary.changed",    # occupant pushes it further
        "setpoint.scheduled",            # schedule rolls over
        "setpoint.temporary",            # nudged again
    ]
    print(f"{'ACTION':34} {'TARGET':11} DETAIL")
    rows = [r for r in store.recent_audit(20) if r['action'].startswith('setpoint')]
    for r in rows[::-1]:
        print(f"{r['action']:34} {r['target'] or '':11} {r['detail']}")
    print(f"\n7 polls -> {len(rows)} audit rows "
          f"(only the moments the answer changed)")

    got = [r["action"] for r in rows[::-1]]
    failures = 0
    for want, have in zip(expected, got + [None] * len(expected)):
        ok = want == have
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {want:30} {'' if ok else f'got {have}'}")
    if len(got) != len(expected):
        failures += 1
        print(f"  FAIL expected {len(expected)} rows, got {len(got)} -- "
              f"a steady reading must not log")

    # The offsets have to travel with the event, or the row says something moved
    # without saying to what.
    first = rows[-1]["detail"]
    ok = isinstance(first, dict) and first.get("cool_adjust") == -1.0
    failures += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} the offset is recorded with the event")

    store.close()

    print("\n--- and what must NOT be written ---")
    failures += await dropped_point_forges_nothing()

    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0

sys.exit(asyncio.run(main()))
