"""The poll loop.

Devices are polled sequentially rather than concurrently, on purpose: there is one
UDP socket and one lock in BacnetClient, so concurrency here would only queue up
behind that lock while making failure attribution harder. A measured 19ms per
device leaves a 25-device cycle around half a second, which is far inside a
10-second interval.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .bacnet import BacnetClient, DeviceUnreachable
from .cache import Cache
from .config import Config

log = logging.getLogger(__name__)


class Poller:
    def __init__(self, cfg: Config, client: BacnetClient, cache: Cache) -> None:
        self._cfg = cfg
        self._client = client
        self._cache = cache
        self._task: asyncio.Task | None = None
        self._cycle = 0

    async def start(self) -> None:
        for d in self._cfg.devices:
            self._cache.register(d.device_id, d.name, d.zone, d.address)
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
                log.warning(
                    "poll cycle took %.1fs, longer than the %.1fs interval",
                    elapsed,
                    self._cfg.poll_interval_seconds,
                )
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
