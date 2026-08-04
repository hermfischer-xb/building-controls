"""In-memory state cache.

The thermostats do not support COV, so nothing is pushed to us -- every value the
API serves is the result of the last poll. Callers therefore need to know how old
a reading is, which is why every entry carries a timestamp and staleness is a
first-class concept rather than something the UI guesses at.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeviceState:
    device_id: int
    name: str
    zone: str
    address: str
    values: dict[str, Any] = field(default_factory=dict)
    last_success: float | None = None
    last_attempt: float | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    # Round-trip time of the last successful poll, and a smoothed average of it.
    # The TC500A does not expose Wi-Fi signal strength over BACnet -- checked
    # against all 770 objects on firmware 01.01.16.00, and the only "Net" points
    # are fallbacks for stale network *inputs*, nothing about the radio. This is
    # the closest thing the gateway can measure, and for finding units that drop
    # out it is arguably the better number: it covers the whole path, including
    # retransmissions that leave RSSI looking healthy.
    last_poll_ms: float | None = None
    avg_poll_ms: float | None = None
    total_failures: int = 0
    total_polls: int = 0
    # Consecutive failures tolerated before the device is called offline.
    # Defaulted to 1 so a DeviceState built directly behaves as it always did;
    # the Cache overrides it from config for every device it registers.
    offline_after: int = 1

    @property
    def online(self) -> bool:
        """Reachable, allowing for the odd lost datagram.

        BACnet/IP is UDP over Wi-Fi here, where losing a packet is routine rather
        than exceptional. Treating one missed poll as an outage told a tenant
        their suite was unreachable, and wrote a transition into the log, for
        something that had already corrected itself by the next cycle.
        """
        return self.consecutive_failures < self.offline_after and self.last_success is not None

    @property
    def unstable(self) -> bool:
        """Missing polls but not yet offline -- the state worth seeing early."""
        return 0 < self.consecutive_failures < self.offline_after

    @property
    def failure_rate(self) -> float | None:
        """Share of attempts that failed, over the life of this process."""
        attempts = self.total_polls + self.total_failures
        return None if attempts == 0 else self.total_failures / attempts

    def age_seconds(self) -> float | None:
        return None if self.last_success is None else time.time() - self.last_success

    def to_dict(self, stale_after: float) -> dict[str, Any]:
        age = self.age_seconds()
        return {
            "device_id": self.device_id,
            "name": self.name,
            "zone": self.zone,
            "address": self.address,
            "online": self.online,
            "unstable": self.unstable,
            "stale": age is None or age > stale_after,
            "age_seconds": None if age is None else round(age, 1),
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "last_poll_ms": None if self.last_poll_ms is None else round(self.last_poll_ms),
            "avg_poll_ms": None if self.avg_poll_ms is None else round(self.avg_poll_ms),
            "failure_rate": None if self.failure_rate is None else round(self.failure_rate, 3),
            "total_polls": self.total_polls,
            "total_failures": self.total_failures,
            "values": self.values,
        }


class Cache:
    def __init__(self, stale_after: float, offline_after: int = 1) -> None:
        self._devices: dict[int, DeviceState] = {}
        self._stale_after = stale_after
        self._offline_after = offline_after

    def register(self, device_id: int, name: str, zone: str, address: str) -> None:
        self._devices[device_id] = DeviceState(
            device_id=device_id, name=name, zone=zone, address=address,
            offline_after=self._offline_after,
        )

    # Weight of each new sample in the smoothed average. 0.2 settles in roughly
    # 15 polls, so at a 30-second interval a device that degrades shows it within
    # about eight minutes -- responsive enough to catch an AP problem, slow enough
    # that one unlucky retransmission does not look like a fault.
    _EMA_ALPHA = 0.2

    def record_success(self, device_id: int, values: dict[str, Any],
                       elapsed_ms: float | None = None) -> None:
        state = self._devices[device_id]
        now = time.time()
        state.values = values
        state.last_success = now
        state.last_attempt = now
        state.last_error = None
        state.consecutive_failures = 0
        state.total_polls += 1
        if elapsed_ms is not None:
            state.last_poll_ms = elapsed_ms
            state.avg_poll_ms = (
                elapsed_ms if state.avg_poll_ms is None
                else state.avg_poll_ms * (1 - self._EMA_ALPHA) + elapsed_ms * self._EMA_ALPHA
            )

    def record_failure(self, device_id: int, error: str) -> None:
        state = self._devices[device_id]
        state.last_attempt = time.time()
        state.last_error = error
        state.consecutive_failures += 1
        state.total_failures += 1

    def apply_local_write(self, device_id: int, key: str, value: Any) -> None:
        """Reflect a write immediately so the UI does not show a stale value.

        The next poll overwrites this with the truth. If the device silently
        rejected the write the value will revert, which is the correct behaviour --
        we show what the device says, not what we wished for.
        """
        self._devices[device_id].values[key] = value

    def get(self, device_id: int) -> DeviceState | None:
        return self._devices.get(device_id)

    def all(self) -> list[DeviceState]:
        return list(self._devices.values())

    def to_dict(self) -> list[dict[str, Any]]:
        return [d.to_dict(self._stale_after) for d in self._devices.values()]
