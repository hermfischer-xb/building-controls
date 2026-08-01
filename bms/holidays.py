"""Translate stored holiday rules into BACnet calendar entries.

The TC500A supports all three BACnet calendar entry shapes -- specific date, date
range, and weekNDay recurrence -- verified by writing each to real hardware and
reading it back unchanged. That means a floating holiday like "4th Thursday of
November" is expressed once as a rule the device evaluates itself, rather than
being expanded into concrete dates that need refreshing every year.

Chapter 10 of the vendor integration guide states calendar objects are
unsupported. It is wrong; see tools/write_test.py.
"""

from __future__ import annotations

import calendar as _calendar
import datetime as dt

from bacpypes3.basetypes import CalendarEntry, DateRange, WeekNDay
from bacpypes3.primitivedata import Date

from .store import Holiday

# BACnet encodes "any" as 255 in a date field, and years as an offset from 1900.
ANY = 255
YEAR_EPOCH = 1900


def _date(year: int | None, month: int, day: int) -> Date:
    return Date(((year - YEAR_EPOCH) if year else ANY, month, day, ANY))


def to_calendar_entry(h: Holiday) -> CalendarEntry:
    """Build the BACnet representation of one holiday rule."""
    if h.rule_type == "fixed":
        if h.month is None or h.day is None:
            raise ValueError(f"holiday {h.id} ({h.name}): fixed rule needs month and day")
        return CalendarEntry(date=_date(h.year, h.month, h.day))

    if h.rule_type == "range":
        if None in (h.month, h.day, h.end_month, h.end_day):
            raise ValueError(f"holiday {h.id} ({h.name}): range rule needs start and end")
        return CalendarEntry(
            dateRange=DateRange(
                startDate=_date(h.year, h.month, h.day),
                endDate=_date(h.year, h.end_month, h.end_day),
            )
        )

    if h.rule_type == "floating":
        if None in (h.month, h.week_of_month, h.day_of_week):
            raise ValueError(
                f"holiday {h.id} ({h.name}): floating rule needs month, "
                "week_of_month and day_of_week"
            )
        if not 1 <= h.week_of_month <= 5:
            raise ValueError(
                f"holiday {h.id} ({h.name}): week_of_month must be 1-5 (5 means last)"
            )
        if not 1 <= h.day_of_week <= 7:
            raise ValueError(f"holiday {h.id} ({h.name}): day_of_week must be 1-7 (1=Mon)")
        return CalendarEntry(
            weekNDay=WeekNDay(bytes([h.month, h.week_of_month, h.day_of_week]))
        )

    raise ValueError(f"holiday {h.id} ({h.name}): unknown rule_type {h.rule_type!r}")


def to_calendar_entries(holidays: list[Holiday]) -> list[CalendarEntry]:
    return [to_calendar_entry(h) for h in holidays]


def occurrences(h: Holiday, year: int) -> list[dt.date]:
    """Resolve a rule to concrete dates in `year`.

    The device does its own evaluation, so this exists purely so the UI can show
    "Thanksgiving -- Thu 26 Nov 2026" instead of "month 11, week 4, day 4", and so
    tests can assert a rule means what its author intended.
    """
    if h.rule_type == "fixed":
        if h.year and h.year != year:
            return []
        return [dt.date(year, h.month, h.day)]

    if h.rule_type == "range":
        if h.year and h.year != year:
            return []
        start = dt.date(year, h.month, h.day)
        end = dt.date(year, h.end_month, h.end_day)
        if end < start:  # a range spanning the new year
            end = dt.date(year + 1, h.end_month, h.end_day)
        return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]

    if h.rule_type == "floating":
        # BACnet day_of_week is 1=Mon..7=Sun, matching date.isoweekday().
        matching = [
            dt.date(year, h.month, d)
            for d in range(1, _calendar.monthrange(year, h.month)[1] + 1)
            if dt.date(year, h.month, d).isoweekday() == h.day_of_week
        ]
        if not matching:
            return []
        index = -1 if h.week_of_month == 5 else h.week_of_month - 1
        return [matching[index]] if -len(matching) <= index < len(matching) else []

    return []


def describe(h: Holiday, year: int | None = None) -> str:
    """Human-readable form for the UI and audit log."""
    year = year or dt.date.today().year
    dates = occurrences(h, year)
    if not dates:
        return h.name
    if len(dates) == 1:
        return f"{h.name} — {dates[0]:%a %d %b %Y}"
    return f"{h.name} — {dates[0]:%a %d %b} to {dates[-1]:%a %d %b %Y}"


# Reasonable starting set for a US commercial building. Floating rules are used
# where the real holiday floats, so they never need re-entering.
US_FEDERAL_DEFAULTS: tuple[dict, ...] = (
    {"name": "New Year's Day", "rule_type": "fixed", "month": 1, "day": 1},
    {"name": "Martin Luther King Jr. Day", "rule_type": "floating",
     "month": 1, "week_of_month": 3, "day_of_week": 1},
    {"name": "Presidents' Day", "rule_type": "floating",
     "month": 2, "week_of_month": 3, "day_of_week": 1},
    {"name": "Memorial Day", "rule_type": "floating",
     "month": 5, "week_of_month": 5, "day_of_week": 1},
    {"name": "Juneteenth", "rule_type": "fixed", "month": 6, "day": 19},
    {"name": "Independence Day", "rule_type": "fixed", "month": 7, "day": 4},
    {"name": "Labor Day", "rule_type": "floating",
     "month": 9, "week_of_month": 1, "day_of_week": 1},
    {"name": "Thanksgiving", "rule_type": "floating",
     "month": 11, "week_of_month": 4, "day_of_week": 4},
    {"name": "Day after Thanksgiving", "rule_type": "floating",
     "month": 11, "week_of_month": 4, "day_of_week": 5},
    {"name": "Christmas Day", "rule_type": "fixed", "month": 12, "day": 25},
)
