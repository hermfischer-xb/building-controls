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

import re
from typing import Iterable

from .config import DeviceConfig
from .store import Store


class Zones:
    def __init__(self, devices: Iterable[DeviceConfig], store: Store,
                 extra_zones: Iterable[str] = ()) -> None:
        self._defaults = {d.device_id: d.zone for d in devices}
        self._store = store
        # Zones that exist in the building without a thermostat in them.
        #
        # A zone is not only a set of thermostats. Lighting triggers and doors
        # are configured against zones too, so once each suite became its own
        # zone, `floor-3` stopped having any device in it while remaining the
        # thing that switches the floor-3 corridor lights. Without this, granting
        # a tenant their floor for the lighting button is rejected as a typo.
        self._extra = {z.strip() for z in extra_zones if z and z.strip() not in ("", "*")}

    def of(self, device: DeviceConfig | int) -> str:
        """The zone a device is in now."""
        device_id = device if isinstance(device, int) else device.device_id
        override = self._store.device_zone(device_id)
        if override:
            return override
        if isinstance(device, DeviceConfig):
            return device.zone
        return self._defaults.get(device_id, "default")

    # Letters, digits, spaces, hyphens and underscores. Deliberately narrow:
    # zone names are typed into pickers and compared as strings, so punctuation
    # and stray whitespace only create names that look identical and are not.
    _VALID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")

    def set(self, device_id: int, zone: str, actor: str = "system") -> None:
        zone = zone.strip()
        if not zone:
            raise ValueError("zone cannot be empty")
        if not self._VALID.match(zone):
            raise ValueError(
                "zone may contain letters, numbers, spaces, hyphens and underscores, "
                "and must start with a letter or number"
            )
        self._store.set_device_zone(device_id, zone, actor=actor)

    def validate_grant(self, requested: Iterable[str]) -> list[str]:
        """Check zones being granted to a tenant, rejecting ones that do not exist.

        A grant for a zone that exists nowhere is not an error to warn about
        later -- it silently gives the tenant access to nothing, and the first
        anyone hears of it is a complaint that the app shows an empty dashboard.
        Better to refuse the typo at the point it is made.

        "Exists" means any zone this building uses, not any zone with a
        thermostat in it. A tenant is normally granted two: their own suite,
        which carries their thermostat, and their floor, which carries no device
        at all and is what the corridor lighting trigger matches on.
        """
        known = set(self.known())
        cleaned, unknown = [], []
        for zone in requested:
            zone = zone.strip()
            if not zone:
                continue
            (cleaned if zone in known else unknown).append(zone)
        if unknown:
            raise ValueError(
                f"unknown zone(s): {', '.join(sorted(unknown))}. "
                f"Existing zones are: {', '.join(sorted(known)) or '(none yet)'}"
            )
        return sorted(set(cleaned))

    def known(self) -> list[str]:
        """Every zone in use, for populating pickers.

        Includes configured defaults as well as current assignments, so a zone
        does not vanish from the list the moment its last device moves out of it,
        and the zones that only lighting or doors reference -- a floor with no
        thermostat of its own is still somewhere a tenant can be granted.
        """
        return sorted({
            *self._defaults.values(),
            *self._store.device_zones().values(),
            *self._extra,
        })

    def map(self) -> dict[int, str]:
        overrides = self._store.device_zones()
        return {did: overrides.get(did, default) for did, default in self._defaults.items()}
