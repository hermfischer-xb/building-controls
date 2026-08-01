"""Outdoor conditions from a public weather service.

Exists because of a specific gap. Selecting BACnet/IP mode disables the
thermostat's own internet path, and an isolated control VLAN removes it entirely,
so the "outdoor temperature by zip code" the device would otherwise show is gone.
But the *gateway* is dual-homed and does have internet -- so it can fetch the
value and write it into the same ni_OutdoorTemp input a physical sensor would
feed. The thermostats stay isolated and still know the weather.

Open-Meteo needs no API key and no registration, which matters for something that
has to keep working unattended for years without anyone remembering to renew a
credential. Zip codes are resolved through Zippopotam, also key-free.

Every failure here is non-fatal and returns None. A weather service being down
must never stall a reconcile cycle or stop schedules being applied -- the
thermostats have their own fail-detect timers for exactly this.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

GEOCODE_URL = "https://api.zippopotam.us/{country}/{postcode}"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo publishes on a 15 minute interval, so polling faster only adds load
# without adding information.
CACHE_SECONDS = 600.0
TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class Conditions:
    temperature_f: float
    humidity_pct: float | None
    observed_at: str
    place: str


class WeatherSource:
    def __init__(
        self,
        zip_code: str | None = None,
        country: str = "us",
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> None:
        self._zip = zip_code
        self._country = country
        self._lat = latitude
        self._lon = longitude
        self._place = ""
        self._cached: Conditions | None = None
        self._fetched_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._zip) or (self._lat is not None and self._lon is not None)

    @property
    def place(self) -> str:
        return self._place or (self._zip or "")

    async def _resolve(self, client: httpx.AsyncClient) -> bool:
        """Turn a postcode into coordinates. Done once and remembered."""
        if self._lat is not None and self._lon is not None:
            return True
        if not self._zip:
            return False

        url = GEOCODE_URL.format(country=self._country, postcode=self._zip)
        try:
            res = await client.get(url)
            res.raise_for_status()
            place = res.json()["places"][0]
        except Exception as err:  # noqa: BLE001 - any failure means "no location"
            log.warning("could not resolve postcode %s: %s", self._zip, err)
            return False

        self._lat = float(place["latitude"])
        self._lon = float(place["longitude"])
        self._place = f"{place['place name']}, {place.get('state abbreviation', '')}".strip(", ")
        log.info("weather location %s -> %.3f, %.3f (%s)",
                 self._zip, self._lat, self._lon, self._place)
        return True

    async def current(self) -> Conditions | None:
        """Current conditions, cached. Returns None if unavailable for any reason."""
        if not self.configured:
            return None
        if self._cached and (time.time() - self._fetched_at) < CACHE_SECONDS:
            return self._cached

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                if not await self._resolve(client):
                    return None
                res = await client.get(
                    FORECAST_URL,
                    params={
                        "latitude": self._lat,
                        "longitude": self._lon,
                        "current": "temperature_2m,relative_humidity_2m",
                        "temperature_unit": "fahrenheit",
                    },
                )
                res.raise_for_status()
                current = res.json()["current"]
        except Exception as err:  # noqa: BLE001 - never fatal, see module docstring
            log.warning("weather lookup failed: %s", err)
            return None

        temperature = current.get("temperature_2m")
        if temperature is None:
            return None

        self._cached = Conditions(
            temperature_f=float(temperature),
            humidity_pct=(
                float(current["relative_humidity_2m"])
                if current.get("relative_humidity_2m") is not None
                else None
            ),
            observed_at=str(current.get("time", "")),
            place=self._place or (self._zip or ""),
        )
        self._fetched_at = time.time()
        return self._cached
