"""Push stored intent onto devices and report where they disagree.

Runs on a slow interval rather than on every change, because devices drop off
Wi-Fi, get changed at the touchscreen, and occasionally apply a write without
acknowledging it. Converging repeatedly is more reliable than trusting any single
write to have landed.

Holidays are applied as one calendar object plus one schedule exception that
*references* it. That indirection matters: adding a holiday is then a single write
to the calendar, and the schedule itself is written once and left alone.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .bacnet import BacnetClient, DeviceUnreachable, holiday_event
from .config import Config, DeviceConfig
from .holidays import to_calendar_entries
from .points import BY_KEY, SETPOINT_LIMITS, ScheduleState, is_valid
from .schedules import (
    PRIORITY_HOLIDAY,
    bacnet_to_week,
    exception_to_bacnet,
    resolve_week,
    week_summary,
    week_to_bacnet,
)
from .store import Store
from .weather import WeatherSource
from .zones import Zones

log = logging.getLogger(__name__)

# Holidays live in the first calendar object. The device exposes ten; using one
# keeps the schedule's exception list to a single entry, and nothing so far needs
# per-holiday granularity on the device side.
HOLIDAY_CALENDAR = "calendar,3"
SCHEDULE_OBJID = "schedule,2"


@dataclass
class DeviceResult:
    device_id: int
    name: str
    changed: list[str] = field(default_factory=list)
    already_correct: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "ok": self.ok,
            "changed": self.changed,
            "already_correct": self.already_correct,
            "errors": self.errors,
        }


class Reconciler:
    def __init__(self, cfg: Config, client: BacnetClient, store: Store,
                 zones: Zones) -> None:
        self._cfg = cfg
        self._zones = zones
        self._client = client
        self._store = store
        self._task: asyncio.Task | None = None
        self._last_run: float | None = None
        self._last_result: list[dict[str, Any]] = []
        # device_id -> seconds the device clock is ahead of this host
        self._drift: dict[int, float] = {}
        # Last outdoor reading and where it came from, for the UI.
        self._outdoor: dict[str, Any] | None = None

        w = cfg.outdoor_weather
        self._weather = (
            WeatherSource(zip_code=w.zip_code, country=w.country,
                          latitude=w.latitude, longitude=w.longitude)
            if w.enabled else None
        )

    async def start(self, interval_seconds: float = 300.0) -> None:
        self._task = asyncio.create_task(self._run(interval_seconds), name="reconciler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self, interval: float) -> None:
        while True:
            try:
                await self.reconcile_all(actor="reconciler")
            except Exception:  # noqa: BLE001 - the loop must outlive any single failure
                log.exception("reconcile cycle failed")
            await asyncio.sleep(interval)

    @property
    def outdoor(self) -> dict[str, Any] | None:
        return self._outdoor

    @property
    def status(self) -> dict[str, Any]:
        return {
            "last_run": self._last_run,
            "age_seconds": None if self._last_run is None else round(time.time() - self._last_run, 1),
            "devices": self._last_result,
            "clock_drift_seconds": {str(k): round(v, 1) for k, v in self._drift.items()},
            "outdoor": self._outdoor,
        }

    async def _outdoor_reading(self) -> tuple[float, float | None, str, int | None] | None:
        """Where outdoor conditions come from, best source first.

        A physical sensor beats a regional forecast: it is the actual air at this
        building, and economizer decisions are made from it. Public weather is the
        fallback, which matters because the thermostat's own zip-code lookup is
        unavailable once BACnet/IP is selected and the VLAN is isolated.

        Returns (temperature, humidity, source label, device to skip) or None.
        """
        source_id = self._cfg.outdoor_sensor_device_id
        if source_id is not None:
            source = next((d for d in self._cfg.devices if d.device_id == source_id), None)
            if source is None:
                log.warning("outdoor_sensor_device_id %s is not in the inventory", source_id)
            else:
                try:
                    values = await self._client.read_points(source)
                    temp, humidity = values.get("oa_temp"), values.get("oa_humidity")
                    if is_valid(temp):
                        return (
                            float(temp),
                            float(humidity) if is_valid(humidity) else None,
                            f"sensor at {source.name}",
                            source_id,
                        )
                    # Falling through to weather rather than giving up: a failed
                    # sensor should degrade to a regional value, not to nothing.
                    log.info("%s has no valid outdoor reading; trying weather", source.name)
                except DeviceUnreachable as err:
                    log.warning("outdoor sensor device unreachable: %s", err)

        if self._weather is not None:
            conditions = await self._weather.current()
            if conditions is not None:
                return (
                    conditions.temperature_f,
                    conditions.humidity_pct,
                    f"weather for {conditions.place}",
                    None,
                )
        return None

    async def _share_outdoor_air(self, actor: str) -> str | None:
        """Write the outdoor reading into every thermostat.

        The thermostats cannot do this between themselves -- no COV, and they
        never originate reads -- so the gateway is the only thing that can. The
        device is built for it: ni_OutdoorTemp is a writable input backed by a
        600 second watchdog, which is why this runs on the reconcile interval
        rather than once at startup.
        """
        reading = await self._outdoor_reading()
        if reading is None:
            self._outdoor = None
            return None

        temp, humidity, label, skip_id = reading
        shared = 0
        for device in self._cfg.devices:
            # The device with the physical sensor already knows; writing to it
            # would overwrite its own measurement with a copy of itself.
            if skip_id is not None and device.device_id == skip_id:
                continue
            try:
                await self._client.write_point(device, BY_KEY["oa_temp_in"], float(temp))
                if humidity is not None:
                    await self._client.write_point(
                        device, BY_KEY["oa_humidity_in"], float(humidity)
                    )
                shared += 1
            except DeviceUnreachable as err:
                log.warning("could not share outdoor air to %s: %s", device.name, err)

        self._outdoor = {
            "temperature_f": round(float(temp), 1),
            "humidity_pct": round(humidity, 1) if humidity is not None else None,
            "source": label,
            "devices": shared,
            "at": time.time(),
        }
        if shared:
            self._store.log(actor, "reconcile.outdoor_air", label,
                            {"temp": round(float(temp), 1), "devices": shared})
            return f"outdoor {float(temp):.1f}F from {label} -> {shared} device(s)"
        return None

    async def reconcile_all(self, actor: str = "system") -> list[DeviceResult]:
        outdoor = await self._share_outdoor_air(actor)
        if outdoor:
            log.info("reconcile: %s", outdoor)
        results = [await self.reconcile_device(d, actor) for d in self._cfg.devices]
        self._last_run = time.time()
        self._last_result = [r.to_dict() for r in results]

        failed = [r.name for r in results if not r.ok]
        changed = sum(len(r.changed) for r in results)
        if failed:
            log.warning("reconcile: %d change(s), failures on %s", changed, ", ".join(failed))
        elif changed:
            log.info("reconcile: %d change(s) across %d device(s)", changed, len(results))
        return results

    async def reconcile_device(self, device: DeviceConfig, actor: str = "system") -> DeviceResult:
        result = DeviceResult(device_id=device.device_id, name=device.name)
        # Clock first: a wrong clock makes every schedule below it wrong too.
        await self._reconcile_clock(device, result, actor)
        await self._reconcile_weekly(device, result, actor)
        await self._reconcile_holidays(device, result, actor)
        await self._reconcile_setpoints(device, result, actor)
        return result

    async def _reconcile_clock(
        self, device: DeviceConfig, result: DeviceResult, actor: str
    ) -> None:
        """Keep the device clock on this host's local wall time.

        Checked every cycle rather than on a daily timer, because drift is cheap
        to measure (two reads) and a schedule running on a wrong clock is the kind
        of fault nobody notices until a tenant complains the heat came on late.

        Pushing *local* time deliberately: the device has no timezone awareness
        (it reports utcOffset 0 regardless), and the guide says daylight saving
        cannot be written over BACnet. Sending wall-clock time this host computed
        means DST transitions are handled here and the device never needs to know.
        """
        if not self._cfg.time_sync_enabled:
            return

        before = await self._client.read_device_time(device)
        if before is None:
            result.errors.append("could not read device clock")
            return

        now = dt.datetime.now()
        drift = (before - now).total_seconds()
        self._drift[device.device_id] = drift

        if abs(drift) <= self._cfg.max_clock_drift_seconds:
            result.already_correct.append(f"clock (drift {drift:+.0f}s)")
            return

        await self._client.sync_time(device, now)
        # TimeSynchronization is unconfirmed, so the only proof is a re-read.
        await asyncio.sleep(2)
        after = await self._client.read_device_time(device)
        if after is None:
            result.errors.append("clock sync sent but device clock unreadable afterwards")
            return

        new_drift = (after - dt.datetime.now()).total_seconds()
        self._drift[device.device_id] = new_drift

        if abs(new_drift) <= self._cfg.max_clock_drift_seconds:
            result.changed.append(f"clock: drift {drift:+.0f}s -> {new_drift:+.0f}s")
            self._store.log(actor, "reconcile.clock", device.name,
                            {"before": round(drift, 1), "after": round(new_drift, 1)})
        else:
            # Refused or ignored. Worth an error rather than silence: it means the
            # schedules on this device are running against the wrong time.
            result.errors.append(
                f"clock sync did not take: still {new_drift:+.0f}s off"
            )
            self._store.log(actor, "reconcile.clock", device.name,
                            {"before": round(drift, 1), "after": round(new_drift, 1)},
                            outcome="error")

    async def _reconcile_weekly(
        self, device: DeviceConfig, result: DeviceResult, actor: str
    ) -> None:
        """Write the device's resolved weekly pattern (group + day overrides)."""
        group_id = self._store.group_for_device(device.device_id)
        if group_id is None:
            # No group assigned means the device keeps whatever it has. Silently
            # rewriting an unassigned thermostat would be worse than leaving it.
            return

        week = resolve_week(
            self._store.group_week(group_id), self._store.day_overrides(device.device_id)
        )
        try:
            desired = week_to_bacnet(week)
        except ValueError as err:
            result.errors.append(f"weekly schedule invalid: {err}")
            return

        try:
            current_raw = await self._client.read_weekly_schedule(device, SCHEDULE_OBJID)
        except Exception as err:  # noqa: BLE001
            result.errors.append(f"read weekly schedule: {err}")
            return

        if bacnet_to_week(current_raw) == {d: week[d] for d in range(1, 8)}:
            result.already_correct.append("weekly schedule")
            return

        try:
            await self._client.write_weekly_schedule(device, SCHEDULE_OBJID, desired)
            result.changed.append(f"weekly schedule -> {week_summary(week)}")
            self._store.log(actor, "reconcile.weekly", device.name, {"summary": week_summary(week)})
        except Exception as err:  # noqa: BLE001
            result.errors.append(f"write weekly schedule: {err}")
            self._store.log(
                actor, "reconcile.weekly", device.name, {"error": str(err)}, outcome="unknown"
            )

    async def _reconcile_holidays(
        self, device: DeviceConfig, result: DeviceResult, actor: str
    ) -> None:
        zone = self._zones.of(device)
        holidays = self._store.holidays(device_id=device.device_id, zone=zone)
        try:
            desired = to_calendar_entries(holidays)
        except ValueError as err:
            # A malformed rule must not stop the rest of the device reconciling.
            result.errors.append(f"holiday rules invalid: {err}")
            log.error("%s: %s", device.name, err)
            return

        try:
            current = await self._client.read_calendar(device, HOLIDAY_CALENDAR)
        except (DeviceUnreachable, Exception) as err:  # noqa: BLE001
            result.errors.append(f"read calendar: {err}")
            return

        # Compare on the wire representation so a semantically identical list does
        # not look like drift every cycle.
        desired_repr = [e.dict_contents() for e in desired]
        if _same(current, desired_repr):
            result.already_correct.append(f"holidays ({len(desired)})")
        else:
            try:
                await self._client.write_calendar(device, HOLIDAY_CALENDAR, desired)
                result.changed.append(f"holidays: {len(current)} -> {len(desired)} entries")
                self._store.log(
                    actor, "reconcile.holidays", device.name,
                    {"entries": len(desired), "names": [h.name for h in holidays]},
                )
            except Exception as err:  # noqa: BLE001
                result.errors.append(f"write calendar: {err}")
                self._store.log(
                    actor, "reconcile.holidays", device.name, {"error": str(err)}, outcome="error"
                )
                return

        await self._reconcile_exceptions(device, result, actor, holidays, bool(desired))

    async def _reconcile_exceptions(
        self, device: DeviceConfig, result: DeviceResult, actor: str,
        holidays: list, has_holidays: bool,
    ) -> None:
        """Compose the exception list from holidays *and* dated one-offs.

        These two share one BACnet property. Writing either in isolation destroys
        the other, so they are always built and written together.
        """
        events = []

        if has_holidays:
            state = holidays[0].state if holidays else int(ScheduleState.UNOCCUPIED)
            events.append(holiday_event(HOLIDAY_CALENDAR, state, priority=PRIORITY_HOLIDAY))

        # Only current and future one-offs; expired ones would waste slots on the
        # device while changing nothing.
        today = dt.date.today().isoformat()
        for exception in self._store.exceptions_for(device.device_id, self._zones.of(device), on_or_after=today):
            try:
                events.append(exception_to_bacnet(exception))
            except ValueError as err:
                result.errors.append(f"exception {exception['name']!r}: {err}")

        try:
            await self._client.write_exception_schedule(device, SCHEDULE_OBJID, events)
            result.already_correct.append(f"exceptions ({len(events)})")
        except Exception as err:  # noqa: BLE001
            result.errors.append(f"write exception schedule: {err}")

    async def _reconcile_setpoints(
        self, device: DeviceConfig, result: DeviceResult, actor: str
    ) -> None:
        intended = self._store.setpoints_for(device.device_id, self._zones.of(device))
        if not intended:
            return

        try:
            actual = await self._client.read_points(device)
        except DeviceUnreachable as err:
            result.errors.append(f"read points: {err}")
            return

        for key, want in intended.items():
            point = BY_KEY.get(key)
            if point is None or not point.writable:
                result.errors.append(f"{key} is not a writable point")
                continue

            lo, hi = SETPOINT_LIMITS.get(key, (float("-inf"), float("inf")))
            want = min(hi, max(lo, float(want)))

            have = actual.get(key)
            if have is not None and abs(float(have) - want) < 0.1:
                result.already_correct.append(key)
                continue

            try:
                await self._client.write_point(device, point, want)
                result.changed.append(f"{key}: {have} -> {want}")
                self._store.log(actor, "reconcile.setpoint", device.name, {key: want})
            except DeviceUnreachable as err:
                # The write may still have landed; the next cycle will tell us.
                result.errors.append(f"{key}: {err}")
                self._store.log(
                    actor, "reconcile.setpoint", device.name,
                    {key: want, "error": str(err)}, outcome="unknown",
                )


def _same(current: list[dict], desired: list[dict]) -> bool:
    """Order-insensitive comparison of calendar entry representations."""
    if len(current) != len(desired):
        return False
    remaining = list(desired)
    for entry in current:
        match = next((d for d in remaining if _norm(d) == _norm(entry)), None)
        if match is None:
            return False
        remaining.remove(match)
    return True


def _norm(entry: Any) -> str:
    """Stable string form, since values arrive as tuples or lists depending on path."""
    if isinstance(entry, dict):
        return "{" + ",".join(f"{k}:{_norm(v)}" for k, v in sorted(entry.items())) + "}"
    if isinstance(entry, (list, tuple)):
        return "[" + ",".join(_norm(v) for v in entry) + "]"
    return str(entry)
