#!/usr/bin/env python3
"""Check the sampled history: what it records, what it refuses to invent.

The audit log records transitions -- the moments somebody changed something. It
cannot answer "was 326 warm because 321 turned the cooling down", because that
needs the temperature *between* the events. On 2026-08-05 Suite 321 held a +3F
cooling offset for nine hours and the only readings of neighbouring 326 in that
window were its two scheduled rollovers. Two points cannot show a relationship.

The properties that matter here are mostly about what must NOT be written:

* a sample every history interval, not every poll
* a device that failed this cycle is absent, not carried forward -- its stale
  cache values under a fresh timestamp would be a reading that never happened
* a point missing from a poll records as NULL, not as 0.0, because a zero offset
  and an unread offset mean opposite things
* everything in one sample shares one timestamp
* retention actually deletes

    .venv/bin/python tools/test_history.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from bms.poller import Poller
from bms.cache import Cache
from bms.store import Store

DEVICES = [
    types.SimpleNamespace(device_id=321, name="Suite 321", address="10.0.0.21", zone="floor-3"),
    types.SimpleNamespace(device_id=326, name="Suite 326", address="10.0.0.26", zone="floor-3"),
]


class Cfg:
    devices = DEVICES
    poll_interval_seconds = 30
    request_timeout_seconds = 7
    offline_after_failures = 3
    poll_concurrency = 1
    history_interval_seconds = 0.0     # per-test
    history_retention_days = 90.0


class Client:
    """Answers from a per-device script. A None entry means the unit is dark."""
    concurrency = 1

    def __init__(self):
        self.script = {}

    async def read_device_id(self, d):
        return d.device_id

    async def read_points_timed(self, d):
        values = self.script[d.device_id].pop(0)
        if values is None:
            from bms.bacnet import DeviceUnreachable
            raise DeviceUnreachable(f"{d.name}: dark")
        return values, 640.0


def reading(temp, sp=76.0, cool_adjust=0.0, status=1, drop=()):
    v = {
        "space_temp": temp, "effective_sp": sp,
        "effective_heat_sp": sp - 4, "effective_cool_sp": sp,
        "setpoint_status": status, "heat_adjust": 0.0, "cool_adjust": cool_adjust,
        "active_heat_stages": 0.0, "active_cool_stages": 1.0,
        "fan_running": True, "oa_temp": 88.0,
    }
    for key in drop:
        v.pop(key)
    return v


def build(cfg):
    store = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    cache = Cache(stale_after=90, offline_after=1)
    client = Client()
    poller = Poller(cfg, client, cache, types.SimpleNamespace(of=lambda d: "floor-3"), store)
    for d in DEVICES:
        cache.register(d.device_id, d.name, "floor-3", d.address)
    return store, cache, client, poller


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else f'   <- {detail}'}")
    return 0 if ok else 1


async def main() -> int:
    failures = 0

    # --- sampling rate: a sample per interval, not per poll --------------------
    print("--- the history interval governs, not the poll interval ---")
    cfg = Cfg()
    cfg.history_interval_seconds = 3600     # far longer than this test runs
    store, cache, client, poller = build(cfg)
    client.script = {321: [reading(69.0)] * 5, 326: [reading(77.0)] * 5}
    for _ in range(5):
        await poller.poll_once()
        poller._record_history()

    rows = store.readings()
    failures += check("5 polls produced 1 sample per device",
                      len(rows) == 2, f"got {len(rows)}")
    failures += check("and both carry the same timestamp",
                      len({r["ts"] for r in rows}) == 1)
    store.close()

    # --- a dark device is absent, not carried forward --------------------------
    print("\n--- a device that failed this cycle must not appear ---")
    cfg = Cfg()
    cfg.history_interval_seconds = 0.001    # sample every cycle
    store, cache, client, poller = build(cfg)
    client.script = {
        321: [reading(69.0), reading(70.0), reading(71.0)],
        326: [reading(77.0), None, reading(78.0)],   # dark on the middle cycle
    }
    for _ in range(3):
        await poller.poll_once()
        poller._record_history()
        time.sleep(0.002)

    got_321 = store.readings(device_id=321)
    got_326 = store.readings(device_id=326)
    failures += check("the healthy unit has all 3 samples",
                      len(got_321) == 3, f"got {len(got_321)}")
    failures += check("the dark one has 2, not a repeat of its last good value",
                      len(got_326) == 2, f"got {len(got_326)}")
    failures += check("and no sample invents a temperature it never read",
                      [r["space_temp"] for r in got_326] == [77.0, 78.0],
                      f"got {[r['space_temp'] for r in got_326]}")
    store.close()

    # --- an unread point is NULL, never 0 --------------------------------------
    print("\n--- a missing point records as NULL, not as zero ---")
    cfg = Cfg()
    cfg.history_interval_seconds = 0.001
    store, cache, client, poller = build(cfg)
    # read_points_timed omits a point that came back as an error; the offset here
    # was never read, which is not the same as an offset of zero.
    client.script = {
        321: [reading(69.0, cool_adjust=3.0, drop=("cool_adjust",))],
        326: [reading(77.0, cool_adjust=0.0)],
    }
    await poller.poll_once()
    poller._record_history()

    unread = store.readings(device_id=321)[0]["cool_adjust"]
    genuine = store.readings(device_id=326)[0]["cool_adjust"]
    failures += check("the unread offset is NULL", unread is None, f"got {unread!r}")
    failures += check("a genuine zero is still 0.0", genuine == 0.0, f"got {genuine!r}")
    store.close()

    # --- the column mapping actually lands -------------------------------------
    print("\n--- the renamed columns carry the right point ---")
    row = Store.reading_row(reading(69.0, sp=76.0))
    failures += check("heat_sp <- effective_heat_sp", row["heat_sp"] == 72.0, f"{row['heat_sp']}")
    failures += check("cool_sp <- effective_cool_sp", row["cool_sp"] == 76.0, f"{row['cool_sp']}")
    failures += check("cool_stages <- active_cool_stages", row["cool_stages"] == 1.0)
    failures += check("fan_running stores as an int, not a bool",
                      row["fan_running"] == 1 and not isinstance(row["fan_running"], bool))

    # --- retention deletes ------------------------------------------------------
    print("\n--- retention drops what is past the window ---")
    store = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    now = time.time()
    store.record_readings(now - 100 * 86400, [(321, reading(69.0))])   # 100 days old
    store.record_readings(now - 10 * 86400, [(321, reading(70.0))])    # 10 days old
    store.record_readings(now, [(321, reading(71.0))])
    dropped = store.prune_readings(90.0)
    left = store.readings(device_id=321)
    failures += check("the 100-day-old sample went", dropped == 1, f"dropped {dropped}")
    failures += check("the two inside the window stayed", len(left) == 2, f"got {len(left)}")
    failures += check("retention 0 is a no-op, not a purge",
                      store.prune_readings(0) == 0 and len(store.readings()) == 2)
    span = store.reading_span()
    failures += check("reading_span reports what is actually held",
                      span["samples"] == 2 and span["devices"] == 1, f"{span}")
    store.close()

    # --- disabled means disabled ------------------------------------------------
    print("\n--- history_interval_seconds: 0 records nothing ---")
    cfg = Cfg()
    cfg.history_interval_seconds = 0.0
    store, cache, client, poller = build(cfg)
    client.script = {321: [reading(69.0)] * 3, 326: [reading(77.0)] * 3}
    for _ in range(3):
        await poller.poll_once()
        poller._record_history()
    failures += check("no samples written", store.readings() == [])
    store.close()

    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
