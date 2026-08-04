"""The poll loop.

Devices are polled sequentially rather than concurrently, on purpose: there is one
UDP socket and one lock in BacnetClient, so concurrency here would only queue up
behind that lock while making failure attribution harder.

**Budget a cycle from measured round-trips, not from the RPM figure.** This file
used to claim 19 ms per device, and therefore half a second for 25 devices, which
made a 10-second interval look generous. 19 ms was the read itself, measured on a
wired bench against one thermostat. Against 16 real units on the building's Wi-Fi
it is **~640 ms per device** — 33x more — so that same fleet takes ~10.2 s and
25 will take ~16 s. The difference is the wireless path and the thermostats'
own response latency, neither of which appears in a bench number.

At a 10-second interval the loop therefore never slept: it finished a cycle and
started the next immediately, logging an overrun every time, with no headroom for
the ~5 s a non-answering device costs. The building now runs
`poll_interval_seconds: 30`.

Two consequences worth holding on to:

- A cycle that overruns does not accumulate lag -- `_run` sleeps for whatever is
  left of the interval and no less than zero -- but it does mean the loop is
  saturated, and the warning it logs is the signal to raise the interval.
- `Cache(stale_after=poll_interval * 3)` scales with this, so a 30 s interval
  flags a dead device after 90 s rather than 30 s. That is an accepted trade: a
  suite's temperature does not move meaningfully in a minute, and any command
  re-reads immediately through `settle_and_refresh` rather than waiting for the
  next cycle.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .bacnet import BacnetClient, DeviceUnreachable
from .cache import Cache
from .config import Config
from .zones import Zones

log = logging.getLogger(__name__)


class Poller:
    def __init__(self, cfg: Config, client: BacnetClient, cache: Cache,
                 zones: Zones) -> None:
        self._cfg = cfg
        self._client = client
        self._cache = cache
        self._zones = zones
        self._task: asyncio.Task | None = None
        self._cycle = 0
        self._overruns = 0

    async def start(self) -> None:
        for d in self._cfg.devices:
            self._cache.register(d.device_id, d.name, self._zones.of(d), d.address)
        await self._verify_inventory()
        self._task = asyncio.create_task(self._run(), name="poller")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _verify_inventory(self) -> None:
        """Warn when a device's real BACnet id disagrees with the config.

        A fleet left on the factory default all reports 4194302, which is exactly
        the mistake worth catching at startup rather than at 2am.
        """
        for d in self._cfg.devices:
            actual = await self._client.read_device_id(d)
            if actual is None:
                log.warning("%s (%s) did not answer during inventory check", d.name, d.address)
            elif actual != d.device_id:
                log.warning(
                    "%s (%s) reports BACnet device id %d but config says %d%s",
                    d.name,
                    d.address,
                    actual,
                    d.device_id,
                    " -- still on the unconfigured factory default"
                    if actual >= 4194302
                    else "",
                )

    async def _run(self) -> None:
        while True:
            started = time.perf_counter()
            await self.poll_once()
            elapsed = time.perf_counter() - started
            self._cycle += 1

            if elapsed > self._cfg.poll_interval_seconds:
                self._overruns += 1
                # Same rule the offline-device path follows: log the condition,
                # not every cycle. A saturated loop is a standing state, and at a
                # 10s interval it produced six identical lines a minute forever,
                # which buries everything else in the log.
                if self._overruns == 1 or self._overruns % 20 == 0:
                    count = len(self._cfg.devices) or 1
                    log.warning(
                        "poll cycle took %.1fs, longer than the %.1fs interval "
                        "(%d devices, %.0f ms each) -- raise poll_interval_seconds "
                        "to at least %d [%d consecutive]",
                        elapsed,
                        self._cfg.poll_interval_seconds,
                        count,
                        elapsed / count * 1000,
                        # Round up to the next 10s with room for a timeout or two.
                        int((elapsed + 2 * self._cfg.request_timeout_seconds) / 10 + 1) * 10,
                        self._overruns,
                    )
            elif self._overruns:
                log.info("poll cycle back within its interval after %d overrun(s)",
                         self._overruns)
                self._overruns = 0

            await asyncio.sleep(max(0.0, self._cfg.poll_interval_seconds - elapsed))

    async def poll_once(self) -> None:
        for device in self._cfg.devices:
            try:
                values = await self._client.read_points(device)
                self._cache.record_success(device.device_id, values)
            except DeviceUnreachable as err:
                state = self._cache.get(device.device_id)
                # Log the transition, not every cycle, or an offline thermostat
                # produces a log line every 10 seconds forever.
                if state and state.consecutive_failures == 0:
                    log.warning("%s went offline: %s", device.name, err)
                self._cache.record_failure(device.device_id, str(err))
            except Exception:  # noqa: BLE001 - one bad device must not kill the loop
                log.exception("unexpected error polling %s", device.name)
                self._cache.record_failure(device.device_id, "internal error")
