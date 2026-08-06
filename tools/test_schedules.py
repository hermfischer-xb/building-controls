#!/usr/bin/env python3
"""Check how a day with no schedule entries is interpreted.

An empty `daySchedule` is not a closed day. BACnet says `Schedule_Default`
applies for the whole of it, so the same empty Saturday is closed on a unit whose
default is Unoccupied and **open all day** on one whose default is Occupied.

This project writes closed days explicitly, as a `00:00 -> Unoccupied` entry, so
a device expressing the same thing by leaving the day empty compared as different
and got rewritten once for nothing. That was found on Suite 205, whose weekend
days are genuinely empty while every other unit carries the explicit form.

Cosmetic on this fleet, because every unit checked defaults to Unoccupied. The
case that matters is the one nobody has met yet: a unit defaulting to Occupied,
where an empty Saturday means the suite conditions all weekend and the old
comparison would have read it as closed and reported no drift.

    .venv/bin/python tools/test_schedules.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bms.points import ScheduleState  # noqa: E402
from bms.schedules import CLOSED_DAY, normalise_week  # noqa: E402

OPEN = [{"time": "06:00", "state": int(ScheduleState.OCCUPIED)},
        {"time": "18:00", "state": int(ScheduleState.UNOCCUPIED)}]

# Suite 205's actual shape: weekdays populated, Saturday and Sunday empty.
DEVICE_WEEK = {1: OPEN, 2: OPEN, 3: OPEN, 4: OPEN, 5: OPEN, 6: [], 7: []}
INTENT = {1: OPEN, 2: OPEN, 3: OPEN, 4: OPEN, 5: OPEN, 6: CLOSED_DAY, 7: CLOSED_DAY}

results: list[bool] = []


def check(ok: bool, label: str) -> None:
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


def main() -> int:
    print("=== an empty day means whatever Schedule_Default says ===")

    closed = normalise_week(dict(DEVICE_WEEK), int(ScheduleState.UNOCCUPIED))
    check(closed == INTENT,
          "default Unoccupied: an empty Saturday equals an explicit closed day, "
          "so no pointless rewrite")

    occupied = normalise_week(dict(DEVICE_WEEK), int(ScheduleState.OCCUPIED))
    check(occupied != INTENT,
          "default Occupied: an empty Saturday is open all day and DOES differ")
    check(occupied[6] == [{"time": "00:00", "state": int(ScheduleState.OCCUPIED)}],
          "and it reads as occupied rather than as closed")

    unknown = normalise_week(dict(DEVICE_WEEK), None)
    check(unknown != INTENT,
          "unreadable default: left empty rather than guessed, so the comparison "
          "fails towards writing intent")

    print("\n=== days that already carry entries are untouched ===")
    for default in (int(ScheduleState.OCCUPIED), int(ScheduleState.UNOCCUPIED), None):
        out = normalise_week(dict(DEVICE_WEEK), default)
        check(out[1] == OPEN, f"weekday unchanged with default={default}")

    print("\n=== a fully explicit week is unaffected whatever the default ===")
    explicit = dict(INTENT)
    for default in (0, 1, 3, None):
        check(normalise_week(dict(explicit), default) == INTENT,
              f"already explicit, default={default}: identical")

    failures = results.count(False)
    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'} "
          f"({len(results)} checks)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
