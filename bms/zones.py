"""Resolving which zone a device belongs to.

Two sources, deliberately: `config/devices.yaml` states the zone a device was
commissioned into, and the database overrides it from then on.

The split follows what changes and who changes it. A device's BACnet id, address
and MAC are facts about hardware, they belong in config, and editing them implies
a service restart anyway. A zone is organisational -- it changes when a tenant
takes a different suite -- and a building manager should be able to do that from
the UI at 4pm on a Friday without touching YAML.

Everything that needs a device's zone goes through `Zones` so the two sources can
never be read inconsistently. Reading `DeviceConfig.zone` directly is a bug: it
sees the commissioned value and misses every move since.
"""

from __future__ import annotations

from typing import Iterable

from .config import DeviceConfig
from .store import Store


class Zones:
    def __init__(self, devices: Iterable[DeviceConfig], store: Store) -> None:
        self._defaults = {d.device_id: d.zone for d in devices}
        self._store = store

    def of(self, device: DeviceConfig | int) -> str:
        """The zone a device is in now."""
        device_id = device if isinstance(device, int) else device.device_id
        override = self._store.device_zone(device_id)
        if override:
            return override
        if isinstance(device, DeviceConfig):
            return device.zone
        return self._defaults.get(device_id, "default")

    def set(self, device_id: int, zone: str, actor: str = "system") -> None:
        zone = zone.strip()
        if not zone:
            raise ValueError("zone cannot be empty")
        self._store.set_device_zone(device_id, zone, actor=actor)

    def known(self) -> list[str]:
        """Every zone in use, for populating pickers.

        Includes configured defaults as well as current assignments, so a zone
        does not vanish from the list the moment its last device moves out of it.
        """
        return sorted({*self._defaults.values(), *self._store.device_zones().values()})

    def map(self) -> dict[int, str]:
        overrides = self._store.device_zones()
        return {did: overrides.get(did, default) for did, default in self._defaults.items()}
