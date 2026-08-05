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
    return {"setpoint_status": status, "heat_adjust": heat, "cool_adjust": cool,
            "adjust": 0.0, "effective_sp": sp, "space_temp": temp}

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
    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0

sys.exit(asyncio.run(main()))
