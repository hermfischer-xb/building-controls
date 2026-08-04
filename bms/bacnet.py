"""The single owner of the BACnet socket.

Only one process on a host can bind UDP 47808 and be the BACnet device, which is
why the gateway is its own service rather than something the web app does inline.

Everything here is unicast by address. Who-Is discovery lives in tools/discover.py
and is a commissioning aid, not part of the running system.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from bacpypes3.apdu import ErrorRejectAbortNack, TimeSynchronizationRequest
from bacpypes3.app import Application
from bacpypes3.basetypes import (
    CalendarEntry,
    DateTime,
    DateRange,
    SpecialEvent,
    SpecialEventPeriod,
    TimeValue,
)
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import Date, Null, Time, Unsigned

from .config import BacnetConfig, DeviceConfig
from .points import POINTS, BY_OBJID, Point

log = logging.getLogger(__name__)


# bacpypes3 raises protocol errors that derive from BaseException, NOT Exception.
# `except Exception` around a BACnet call therefore does not catch a device saying
# "unknown-object" -- the error sails past every handler and kills the task. This
# module converts them at the boundary so nothing above has to know, and so an
# `except Exception` elsewhere behaves the way its author expected.
BACNET_FAULTS = (ErrorRejectAbortNack, asyncio.TimeoutError, OSError)


class DeviceUnreachable(Exception):
    """Raised when a request to a device did not complete.

    For a write this means the *acknowledgement* failed, which is not the same as
    the write failing. A TC500A will sometimes apply a value and then not ack it,
    so callers must treat this as "outcome unknown" and let the next poll
    establish the truth. Never assume the old value is still in place.
    """


def _describe(err: Exception) -> str:
    """Build a useful message.

    bacpypes3's reject/abort exceptions frequently stringify to empty, which
    produces log lines like "Room 301 (192.168.1.101): " that say nothing.
    """
    text = str(err).strip()
    if text:
        return f"{type(err).__name__}: {text}"
    for attr in ("errorCode", "reason", "apduAbortRejectReason"):
        value = getattr(err, attr, None)
        if value is not None:
            return f"{type(err).__name__}: {value}"
    return f"{type(err).__name__} (no detail)"


def _unwrap(value: Any) -> Any:
    """Turn a bacpypes3 value into something JSON can carry.

    Schedule present values arrive wrapped in AnyAtomic and enumerated values as
    objects with an int form, so unwrap before anything else sees them.
    """
    if hasattr(value, "get_value"):
        value = value.get_value()
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    if isinstance(value, Unsigned):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


class BacnetClient:
    def __init__(self, cfg: BacnetConfig, request_timeout: float = 5.0) -> None:
        self._cfg = cfg
        self._timeout = request_timeout
        self._app: Application | None = None
        # BACnet/IP is one UDP socket. Serialise requests so a burst of writes
        # from the API cannot interleave with a poll and confuse the responses.
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        args = _Args(
            address=self._cfg.address,
            instance=self._cfg.device_id,
            name=self._cfg.name,
            foreign=self._cfg.foreign_bbmd,
            ttl=self._cfg.foreign_ttl,
        )
        self._app = Application.from_args(args)

        # bacpypes3 reads these off our own device object when it builds each
        # transaction's state machine, so setting them here governs every request.
        # The stack ships 3000 ms x 3 retries -- four transmissions across twelve
        # seconds, which is tuned for slow MS/TP segments. Over Wi-Fi it means a
        # lost datagram is not retried until long after the outer timeout has
        # given up, so no retry ever completed and the retry mechanism was
        # effectively off.
        device_object = self._app.device_object
        device_object.apduTimeout = self._cfg.apdu_timeout_ms
        device_object.numberOfApduRetries = self._cfg.apdu_retries

        budget = self._cfg.retry_budget_seconds
        if budget > self._timeout:
            # Worth saying out loud rather than leaving as arithmetic in a config
            # comment: this is the exact misconfiguration that silently disabled
            # retries, and it looks like nothing at all from the outside.
            log.warning(
                "request_timeout_seconds=%.1f is below the %.1fs retry budget "
                "(%d ms x %d attempts) -- requests will be cut off part-way through "
                "the retry cycle, so late attempts never happen",
                self._timeout, budget, self._cfg.apdu_timeout_ms, self._cfg.apdu_retries + 1,
            )

        log.info(
            "BACnet bound to %s as device %d (up to %d attempts over %.1fs per request)",
            self._cfg.address, self._cfg.device_id, self._cfg.apdu_retries + 1, budget,
        )

    async def stop(self) -> None:
        if self._app:
            self._app.close()
            self._app = None

    @property
    def app(self) -> Application:
        if self._app is None:
            raise RuntimeError("BacnetClient.start() not called")
        return self._app

    async def read_points(self, device: DeviceConfig) -> dict[str, Any]:
        """Read every polled point in a single read-property-multiple request.

        Measured at ~19ms for 16 points against a real TC500A, versus ~197ms doing
        them individually, so this is what keeps a 25-device poll under a second.
        """
        parameter_list: list[Any] = []
        for point in POINTS:
            parameter_list += [point.objid, ["presentValue"]]

        async with self._lock:
            try:
                result = await asyncio.wait_for(
                    self.app.read_property_multiple(Address(device.address), parameter_list),
                    timeout=self._timeout,
                )
            except BACNET_FAULTS as err:
                raise DeviceUnreachable(f"{device.name} ({device.address}): {_describe(err)}") from err

        values: dict[str, Any] = {}
        for objid, _prop, _index, value in result:
            point = BY_OBJID.get(str(objid))
            if point is None:
                continue
            if isinstance(value, ErrorRejectAbortNack):
                log.warning("%s %s: %s", device.name, point.key, value)
                continue
            values[point.key] = _unwrap(value)
        return values

    async def write_point(self, device: DeviceConfig, point: Point, value: Any) -> None:
        """Write a single point.

        Note there is no priority argument. Every writable point on this device is
        a plain Value object with no priorityArray -- writes are last-writer-wins
        and a priority would be accepted and ignored. Arbitration is the
        application database's job, not BACnet's.
        """
        if not point.writable:
            raise ValueError(f"{point.key} is read-only")

        async with self._lock:
            try:
                await asyncio.wait_for(
                    self.app.write_property(
                        Address(device.address), point.objid, "presentValue", value
                    ),
                    timeout=self._timeout,
                )
            except BACNET_FAULTS as err:
                raise DeviceUnreachable(f"{device.name} ({device.address}): {_describe(err)}") from err

    # --- clock ------------------------------------------------------------------
    #
    # These thermostats have no reachable time source on an isolated VLAN: the
    # cloud path is unavailable and the guide says daylight saving cannot be
    # written over BACnet. Pushing local wall-clock time from this host solves
    # both -- the host knows the timezone and its DST rules, so the device never
    # has to.

    async def read_device_time(self, device: DeviceConfig) -> dt.datetime | None:
        """Read the device clock, or None if it cannot be read or is unset."""
        async with self._lock:
            try:
                date = await asyncio.wait_for(
                    self.app.read_property(
                        Address(device.address), f"device,{device.device_id}", "localDate"
                    ),
                    timeout=self._timeout,
                )
                clock = await asyncio.wait_for(
                    self.app.read_property(
                        Address(device.address), f"device,{device.device_id}", "localTime"
                    ),
                    timeout=self._timeout,
                )
            except (*BACNET_FAULTS, Exception):  # never fatal -- clock is optional
                return None

        try:
            # BACnet years are offset from 1900; 255 in any field means unspecified.
            if 255 in (date[0], date[1], date[2]) or 255 in (clock[0], clock[1]):
                return None
            return dt.datetime(
                date[0] + 1900, date[1], date[2], clock[0], clock[1], min(clock[2], 59)
            )
        except (TypeError, ValueError):
            return None

    async def sync_time(self, device: DeviceConfig, now: dt.datetime | None = None) -> None:
        """Push local wall-clock time.

        TimeSynchronization is an unconfirmed service, so there is no
        acknowledgement to wait on and no error to catch -- the only way to know
        it worked is to read the clock back afterwards, which the reconciler does.
        """
        now = now or dt.datetime.now()
        request = TimeSynchronizationRequest(
            time=DateTime(
                date=Date((now.year - 1900, now.month, now.day, now.isoweekday())),
                time=Time((now.hour, now.minute, now.second, 0)),
            )
        )
        request.pduDestination = Address(device.address)
        async with self._lock:
            self.app.request(request)

    async def read_device_id(self, device: DeviceConfig) -> int | None:
        """Read the device's own object identifier, to catch inventory mistakes.

        Tries the configured instance first and only then the 4194303 wildcard.
        The wildcard is what finds a device whose id is *not* what config claims --
        which is the whole point of this check -- but not every implementation
        answers it, so a device that rejects it must not look unreachable.
        """
        for objid in (f"device,{device.device_id}", "device,4194303"):
            async with self._lock:
                try:
                    oid = await asyncio.wait_for(
                        self.app.read_property(
                            Address(device.address), objid, "objectIdentifier"
                        ),
                        timeout=self._timeout,
                    )
                    return int(oid[1])
                except (*BACNET_FAULTS, Exception):  # best-effort identification only
                    continue
        return None

    # --- schedules and holidays -------------------------------------------------
    #
    # Contrary to the integration guide's Chapter 10, calendar objects are fully
    # supported: dateList accepts specific dates, ranges and floating weekNDay
    # rules, and the device honours a referenced calendar within ~3 seconds.

    async def read_calendar(self, device: DeviceConfig, objid: str) -> list[dict]:
        async with self._lock:
            entries = await asyncio.wait_for(
                self.app.read_property(Address(device.address), objid, "dateList"),
                timeout=self._timeout,
            )
        return [e.dict_contents() for e in entries]

    async def write_calendar(
        self, device: DeviceConfig, objid: str, entries: list[CalendarEntry]
    ) -> None:
        async with self._lock:
            try:
                try:
                    await asyncio.wait_for(
                        self.app.write_property(Address(device.address), objid, "dateList", entries),
                        timeout=self._timeout,
                    )
                except BACNET_FAULTS as err:
                    raise DeviceUnreachable(
                        f"{device.name} ({device.address}): {_describe(err)}"
                    ) from err
            except BACNET_FAULTS as err:
                raise DeviceUnreachable(
                    f"{device.name} ({device.address}): {_describe(err)}"
                ) from err

    async def read_weekly_schedule(self, device: DeviceConfig, objid: str) -> Any:
        async with self._lock:
            try:
                return await asyncio.wait_for(
                    self.app.read_property(Address(device.address), objid, "weeklySchedule"),
                    timeout=self._timeout,
                )
            except BACNET_FAULTS as err:
                raise DeviceUnreachable(
                    f"{device.name} ({device.address}): {_describe(err)}"
                ) from err

    async def write_weekly_schedule(
        self, device: DeviceConfig, objid: str, week: list[Any]
    ) -> None:
        """Write all seven days at once.

        The property is an array of seven DailySchedules and the device expects the
        whole thing; writing a single day by array index is not something this
        firmware was tested against, so always send the full week.
        """
        async with self._lock:
            try:
                await asyncio.wait_for(
                    self.app.write_property(
                        Address(device.address), objid, "weeklySchedule", week
                    ),
                    timeout=self._timeout,
                )
            except BACNET_FAULTS as err:
                raise DeviceUnreachable(
                    f"{device.name} ({device.address}): {_describe(err)}"
                ) from err

    async def write_exception_schedule(
        self, device: DeviceConfig, objid: str, events: list[SpecialEvent]
    ) -> None:
        async with self._lock:
            try:
                await asyncio.wait_for(
                    self.app.write_property(
                        Address(device.address), objid, "exceptionSchedule", events
                    ),
                    timeout=self._timeout,
                )
            except BACNET_FAULTS as err:
                raise DeviceUnreachable(
                    f"{device.name} ({device.address}): {_describe(err)}"
                ) from err


def holiday_event(calendar_objid: str, state: int, priority: int = 1) -> SpecialEvent:
    """A schedule exception that applies `state` all day on the referenced calendar.

    Referencing a calendar rather than inlining dates means adding a holiday is one
    write to the calendar, and the schedule never has to be touched again.
    """
    return SpecialEvent(
        period=SpecialEventPeriod(calendarReference=calendar_objid),
        listOfTimeValues=[TimeValue(time=Time("00:00:00"), value=Unsigned(state))],
        eventPriority=priority,
    )


def fixed_date(year: int, month: int, day: int) -> CalendarEntry:
    """A specific date. BACnet years are offset from 1900; 255 means 'any weekday'."""
    return CalendarEntry(date=Date((year - 1900, month, day, 255)))


def date_range(start: tuple[int, int, int], end: tuple[int, int, int]) -> CalendarEntry:
    return CalendarEntry(
        dateRange=DateRange(
            startDate=Date((start[0] - 1900, start[1], start[2], 255)),
            endDate=Date((end[0] - 1900, end[1], end[2], 255)),
        )
    )


def floating_date(month: int, week_of_month: int, day_of_week: int) -> CalendarEntry:
    """A recurring rule, e.g. floating_date(11, 4, 4) == 4th Thursday of November.

    week_of_month 5 means 'last'. day_of_week is 1=Monday .. 7=Sunday.
    Verified accepted and honoured by TC500A firmware 01.01.16.00.
    """
    from bacpypes3.basetypes import WeekNDay

    return CalendarEntry(weekNDay=WeekNDay(bytes([month, week_of_month, day_of_week])))


class _Args:
    """Duck-types the namespace Application.from_args expects."""

    def __init__(self, address: str, instance: int, name: str, foreign, ttl: int) -> None:
        self.address = address
        self.instance = instance
        self.name = name
        self.network = 0
        self.vendoridentifier = 999
        self.foreign = foreign
        self.ttl = ttl
        self.bbmd = None
