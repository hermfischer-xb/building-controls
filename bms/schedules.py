"""Resolve stored schedule intent into what a device should actually hold.

Three layers, most specific winning:

    schedule group  ->  the tenant's normal week
    day override    ->  that tenant's deviation on a particular weekday
    exception       ->  a dated one-off, which the device applies over both

Only the first two become the BACnet weeklySchedule. Exceptions and holidays are a
separate property (exceptionSchedule) and are composed in reconciler.py, because
both feed the same list and neither may clobber the other.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from bacpypes3.basetypes import (
    CalendarEntry,
    DailySchedule,
    DateRange,
    SpecialEvent,
    SpecialEventPeriod,
    TimeValue,
)
from bacpypes3.primitivedata import Date, Time, Unsigned

from .points import ScheduleState

# BACnet weeklySchedule is indexed Monday..Sunday, matching date.isoweekday().
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Event priority decides what wins when a date matches several exceptions. The
# TC500A user guide states events override holidays, which override the standard
# schedule, so dated one-offs get the stronger (numerically lower) priority.
PRIORITY_ONE_OFF = 1
PRIORITY_HOLIDAY = 2

# A day the tenant is never in. The device does not accept an empty daySchedule
# cleanly, and its own factory default writes an explicit midnight entry, so match
# that rather than inventing a representation the hardware does not use.
CLOSED_DAY: list[dict] = [{"time": "00:00", "state": int(ScheduleState.UNOCCUPIED)}]


def parse_time(text: str) -> Time:
    """Accept 'HH:MM' or 'HH:MM:SS'."""
    parts = [int(p) for p in text.split(":")]
    while len(parts) < 3:
        parts.append(0)
    hour, minute, second = parts[:3]
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError(f"invalid time {text!r}")
    return Time((hour, minute, second, 0))


def validate_transitions(transitions: list[dict]) -> list[dict]:
    """Check a day's transitions and return them in time order.

    Rejects duplicates and unknown states early: a bad schedule that reaches the
    thermostat is far more annoying to diagnose than one refused at the API.
    """
    if not transitions:
        raise ValueError("a day needs at least one transition")

    seen: set[tuple[int, int]] = set()
    normalised = []
    for t in transitions:
        if "time" not in t or "state" not in t:
            raise ValueError(f"transition needs 'time' and 'state': {t!r}")
        parsed = parse_time(str(t["time"]))
        key = (parsed[0], parsed[1])
        if key in seen:
            raise ValueError(f"duplicate transition at {t['time']}")
        seen.add(key)

        state = int(t["state"])
        if state not in (int(s) for s in ScheduleState):
            raise ValueError(
                f"state {state} invalid; use 0 (Occupied), 1 (Unoccupied) or 3 (Standby)"
            )
        normalised.append({"time": f"{parsed[0]:02d}:{parsed[1]:02d}", "state": state})

    normalised.sort(key=lambda t: t["time"])
    if len(normalised) > 8:
        raise ValueError(f"{len(normalised)} transitions in one day; the device accepts 8")
    return normalised


def resolve_week(
    group_week: dict[int, list[dict]], overrides: dict[int, list[dict]]
) -> dict[int, list[dict]]:
    """Combine a group's week with a device's per-day overrides."""
    return {
        day: overrides.get(day, group_week.get(day, CLOSED_DAY)) for day in range(1, 8)
    }


def week_to_bacnet(week: dict[int, list[dict]]) -> list[DailySchedule]:
    return [
        DailySchedule(
            daySchedule=[
                TimeValue(time=parse_time(t["time"]), value=Unsigned(int(t["state"])))
                for t in week.get(day, CLOSED_DAY)
            ]
        )
        for day in range(1, 8)
    ]


def normalise_week(week: dict[int, list[dict]], schedule_default: int | None) -> dict[int, list[dict]]:
    """Give every empty day the meaning the device assigns it.

    A day with no entries is not a closed day. BACnet says `Schedule_Default`
    applies for the whole of it, so the same empty Saturday is closed on a unit
    whose default is Unoccupied and open all day on one whose default is
    Occupied.

    This project writes closed days explicitly as `CLOSED_DAY`, so a device that
    expresses the same thing by leaving the day empty compared as different and
    was rewritten once for no reason. Harmless while every unit defaults to
    Unoccupied -- and silently wrong the day one does not, because the reconciler
    would read a fully occupied Saturday as closed and leave it alone.

    `schedule_default` of None means it could not be read: the day is left empty
    rather than guessed at, so a comparison fails safe towards writing the intent
    rather than towards believing the device already matches.
    """
    if schedule_default is None:
        return week
    filled = [{"time": "00:00", "state": int(schedule_default)}]
    return {day: (entries if entries else list(filled)) for day, entries in week.items()}


def bacnet_to_week(weekly: Any) -> dict[int, list[dict]]:
    """Decode what a device currently holds, for drift comparison and display."""
    out: dict[int, list[dict]] = {}
    for index, entry in enumerate(weekly, start=1):
        out[index] = [
            {
                "time": f"{tv.time[0]:02d}:{tv.time[1]:02d}",
                "state": int(tv.value.get_value()),
            }
            for tv in (entry.daySchedule or [])
        ]
    return out


def exception_to_bacnet(exception: dict) -> SpecialEvent:
    """Turn a dated one-off into a BACnet special event."""
    start = dt.date.fromisoformat(exception["start_date"])
    end_raw = exception.get("end_date")

    if end_raw:
        end = dt.date.fromisoformat(end_raw)
        if end < start:
            raise ValueError(
                f"exception {exception.get('name')!r}: end_date {end} precedes start {start}"
            )
        entry = CalendarEntry(
            dateRange=DateRange(
                startDate=Date((start.year - 1900, start.month, start.day, 255)),
                endDate=Date((end.year - 1900, end.month, end.day, 255)),
            )
        )
    else:
        entry = CalendarEntry(date=Date((start.year - 1900, start.month, start.day, 255)))

    return SpecialEvent(
        period=SpecialEventPeriod(calendarEntry=entry),
        listOfTimeValues=[
            TimeValue(time=parse_time(t["time"]), value=Unsigned(int(t["state"])))
            for t in validate_transitions(exception["transitions"])
        ],
        eventPriority=PRIORITY_ONE_OFF,
    )


def week_summary(week: dict[int, list[dict]]) -> str:
    """One-line human form, e.g. 'Mon-Fri 06:00-18:00, Sat 08:00-13:00'."""
    labels = {
        int(ScheduleState.OCCUPIED): "occupied",
        int(ScheduleState.UNOCCUPIED): "unoccupied",
        int(ScheduleState.STANDBY): "standby",
    }
    parts = []
    for day in range(1, 8):
        transitions = week.get(day, [])
        occupied = [t for t in transitions if t["state"] == int(ScheduleState.OCCUPIED)]
        if not occupied:
            continue
        start = occupied[0]["time"]
        after = [t for t in transitions if t["time"] > start]
        end = after[0]["time"] if after else "24:00"
        parts.append(f"{DAYS[day - 1].capitalize()} {start}-{end}")
    return ", ".join(parts) if parts else "never occupied"


# Starting templates covering the patterns described for this building.
def _weekday_week(start: str, end: str, saturday: tuple[str, str] | None = None,
                  sunday: tuple[str, str] | None = None) -> dict[int, list[dict]]:
    occ, unocc = int(ScheduleState.OCCUPIED), int(ScheduleState.UNOCCUPIED)
    week = {
        day: [{"time": start, "state": occ}, {"time": end, "state": unocc}]
        for day in range(1, 6)
    }
    week[6] = (
        [{"time": saturday[0], "state": occ}, {"time": saturday[1], "state": unocc}]
        if saturday else list(CLOSED_DAY)
    )
    week[7] = (
        [{"time": sunday[0], "state": occ}, {"time": sunday[1], "state": unocc}]
        if sunday else list(CLOSED_DAY)
    )
    return week


DEFAULT_GROUPS: tuple[dict, ...] = (
    {
        "name": "Standard weekday",
        "description": "Mon-Fri 06:00-18:00, closed weekends",
        "week": _weekday_week("06:00", "18:00"),
    },
    {
        "name": "Extended evening",
        "description": "Mon-Fri 06:00-21:00, closed weekends",
        "week": _weekday_week("06:00", "21:00"),
    },
    {
        "name": "Six-day",
        "description": "Mon-Fri 06:00-18:00 plus Sat 08:00-13:00",
        "week": _weekday_week("06:00", "18:00", saturday=("08:00", "13:00")),
    },
    {
        "name": "Seven-day",
        "description": "Every day 07:00-19:00",
        "week": _weekday_week("07:00", "19:00", saturday=("07:00", "19:00"),
                              sunday=("07:00", "19:00")),
    },
)
