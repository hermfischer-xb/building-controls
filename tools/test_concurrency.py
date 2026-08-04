#!/usr/bin/env python3
"""Model the poll cycle against `poll_concurrency`, with no hardware.

Answers the question the setting exists for: how much does one slow device cost
the devices behind it? The client here is a stand-in whose timings come from this
building's own measurements -- 640 ms for a healthy read, the full 6 s retry
budget for a dark one -- so the table is a model, only as good as those inputs,
not a bench result.

Everything runs at 1/20th scale so the suite finishes in seconds; the printed
figures are scaled back up. Ratios are what the setting turns on, and those are
unaffected.

It also guards the property that makes the Link quality table trustworthy:
**avg_poll_ms must not move with concurrency.** If it does, time queued behind
other devices is being recorded as a slow radio link, which is exactly backwards
-- and it is an easy mistake to make, because the obvious place to start the
timer is in the poller rather than inside the client's gate.

    .venv/bin/python tools/test_concurrency.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bms.bacnet import DeviceUnreachable  # noqa: E402
from bms.cache import Cache  # noqa: E402
from bms.poller import Poller  # noqa: E402

SCALE = 20.0          # run 20x faster than the building
HEALTHY_MS = 640.0    # measured fleet norm over Wi-Fi
DARK_S = 6.0          # apdu_timeout_ms 1500 x 4 attempts

DEVICES = [
    types.SimpleNamespace(device_id=300 + i, name=f"Sim {i}",
                          address=f"10.0.0.{i}", zone="lab")
    for i in range(16)
]


class Cfg:
    devices = DEVICES
    poll_interval_seconds = 30
    request_timeout_seconds = 7
    offline_after_failures = 3

    def __init__(self, concurrency: int) -> None:
        self.poll_concurrency = concurrency


class FakeClient:
    """Applies the same per-device lock and global bound the real client does."""

    def __init__(self, concurrency: int, dark: set[int]) -> None:
        self._gate = asyncio.Semaphore(concurrency)
        self._locks: dict[str, asyncio.Lock] = {}
        self._dark = dark
        self.peak = 0
        self._inflight = 0

    async def read_points_timed(self, device):
        lock = self._locks.setdefault(device.address, asyncio.Lock())
        async with lock:
            async with self._gate:
                self._inflight += 1
                self.peak = max(self.peak, self._inflight)
                started = time.perf_counter()
                try:
                    if device.device_id in self._dark:
                        await asyncio.sleep(DARK_S / SCALE)
                        raise DeviceUnreachable(f"{device.name}: no-response")
                    await asyncio.sleep(HEALTHY_MS / 1000 / SCALE)
                    return {"space_temp": 72}, (time.perf_counter() - started) * 1000 * SCALE
                finally:
                    self._inflight -= 1


async def cycle(concurrency: int, dark: set[int] | None = None):
    cfg = Cfg(concurrency)
    cache = Cache(stale_after=90, offline_after=3)
    client = FakeClient(concurrency, dark or set())
    poller = Poller(cfg, client, cache, types.SimpleNamespace(of=lambda d: d.zone))
    for d in DEVICES:
        cache.register(d.device_id, d.name, "lab", d.address)

    started = time.perf_counter()
    await poller.poll_once()
    elapsed = (time.perf_counter() - started) * SCALE

    samples = [c.avg_poll_ms for c in cache.all() if c.avg_poll_ms is not None]
    return elapsed, client.peak, (sum(samples) / len(samples) if samples else 0.0)


async def main() -> int:
    print(f"16 devices at {HEALTHY_MS:.0f} ms; dark devices burn the full "
          f"{DARK_S:.0f} s retry budget")
    print(f"(modelled at {SCALE:.0f}x speed, figures scaled back to real time)\n")
    print(f"{'concurrency':>11}  {'no dark':>9}  {'1 dark':>9}  {'4 dark':>9}")
    for c in (1, 3, 4, 8):
        a, _, _ = await cycle(c)
        b, _, _ = await cycle(c, {301})
        d, _, _ = await cycle(c, {301, 305, 309, 313})
        print(f"{c:>11}  {a:>8.1f}s  {b:>8.1f}s  {d:>8.1f}s")

    print("\nThe 30 s poll window is the line that matters in that last column.\n")

    failures = 0

    def check(ok: bool, label: str) -> None:
        nonlocal failures
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")

    # Timing must measure the device, never the queue.
    for c in (1, 4, 8):
        _, peak, avg = await cycle(c, {301})
        check(abs(avg - HEALTHY_MS) < 40,
              f"concurrency {c}: avg_poll_ms {avg:.0f}, queue wait excluded "
              f"(want ~{HEALTHY_MS:.0f})")
        check(peak <= c, f"concurrency {c}: peak {peak} in flight, within bound")

    # The default must not have changed anyone's behaviour.
    one, peak, _ = await cycle(1)
    check(peak == 1, "the default of 1 keeps exactly one request in flight")
    check(abs(one - len(DEVICES) * HEALTHY_MS / 1000) < 1.5,
          "and the cycle is still the sum of every device")

    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
