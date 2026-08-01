#!/usr/bin/env python3
"""Probe which points the TC500A actually accepts writes on, then put them back.

The integration guide's Chapter 10 claims calendar objects are unsupported and that
schedule properties should not be written over BACnet. Both claims decide how
holidays get implemented, so test them rather than trust them.

Every test records the current value, writes, reads back, reverts, and re-reads to
confirm the revert landed. Run against a bench unit.

    .venv/bin/python tools/write_test.py --address 192.168.1.10/24 --target 192.168.1.101
"""

import asyncio
import sys
import traceback

from bacpypes3.apdu import ErrorRejectAbortNack
from bacpypes3.app import Application
from bacpypes3.argparse import SimpleArgumentParser
from bacpypes3.basetypes import (
    CalendarEntry,
    DailySchedule,
    DateRange,
    SpecialEvent,
    SpecialEventPeriod,
    TimeValue,
)
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import Date, Null, Time, Unsigned

results: list[tuple[str, str, str]] = []


def record(name: str, verdict: str, detail: str = "") -> None:
    results.append((name, verdict, detail))
    print(f"  {verdict:12} {name}  {detail}")


async def read(app, addr, objid, prop):
    return await app.read_property(addr, objid, prop)


async def try_write(app, addr, objid, prop, value, priority=None):
    """Write and report the device's own words if it refuses."""
    try:
        await app.write_property(addr, objid, prop, value, priority=priority)
        return None
    except ErrorRejectAbortNack as err:
        return str(err)
    except Exception as err:  # noqa: BLE001
        return f"{type(err).__name__}: {err}"


async def roundtrip(app, addr, objid, prop, test_value, label, compare=None):
    """Write test_value, read back, revert to whatever was there, verify the revert."""
    try:
        original = await read(app, addr, objid, prop)
    except Exception as err:  # noqa: BLE001
        record(label, "UNREADABLE", str(err))
        return

    err = await try_write(app, addr, objid, prop, test_value)
    if err:
        record(label, "REJECTED", err)
        return

    readback = await read(app, addr, objid, prop)
    matched = compare(readback, test_value) if compare else (readback == test_value)

    # Always attempt to put it back, even if the readback disagreed.
    revert_err = await try_write(app, addr, objid, prop, original)
    reverted = await read(app, addr, objid, prop) if not revert_err else None

    if not matched:
        record(label, "ACCEPTED*", f"wrote {test_value!r}, read back {readback!r}")
    elif revert_err:
        record(label, "WRITABLE!", f"REVERT FAILED: {revert_err} -- left at {readback!r}")
    else:
        record(label, "WRITABLE", f"{original!r} -> {readback!r} -> {reverted!r}")


async def main() -> None:
    parser = SimpleArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="thermostat IP")
    args = parser.parse_args()

    app = Application.from_args(args)
    addr = Address(args.target)

    try:
        print("\n=== 1. setpoints (plain Analog Value, no priority array) ===")
        await roundtrip(
            app, addr, "analog-value,4", "presentValue", 74.0,
            "Cfg_Setpoints_OccCoolSp",
            compare=lambda a, b: abs(float(a) - float(b)) < 0.01,
        )

        print("\n=== 2. occupancy override ===")
        # 1 = Occ, 5 = NoOvrd per the device's own stateText
        await roundtrip(app, addr, "multi-state-value,4", "presentValue", 1, "ni_OccManCom -> Occ")

        print("\n=== 3. bypass timer ===")
        await roundtrip(app, addr, "analog-value,2", "presentValue", 60.0, "ni_BypassValue = 60min",
                        compare=lambda a, b: abs(float(a) - float(b)) < 0.01)
        await roundtrip(app, addr, "binary-value,1", "presentValue", 1, "ni_BypassState = enable",
                        compare=lambda a, b: int(a) == int(b))

        print("\n=== 4. does writing at a priority work on a non-commandable point? ===")
        err = await try_write(app, addr, "analog-value,4", "presentValue", 75.0, priority=8)
        if err:
            record("write AV4 @ priority 8", "REJECTED", err)
        else:
            back = await read(app, addr, "analog-value,4", "presentValue")
            await try_write(app, addr, "analog-value,4", "presentValue", 76.0)
            record("write AV4 @ priority 8", "ACCEPTED", f"read back {back!r} (priority likely ignored)")

        print("\n=== 5. calendar dateList -- the holiday question ===")
        # Christmas plus a two-day range, the shape a real holiday list needs.
        holiday_list = [
            CalendarEntry(date=Date((126, 12, 25, 255))),
            CalendarEntry(
                dateRange=DateRange(
                    startDate=Date((126, 11, 26, 255)), endDate=Date((126, 11, 27, 255))
                )
            ),
        ]
        await roundtrip(
            app, addr, "calendar,3", "dateList", holiday_list,
            "Calendar1.dateList (2 entries)",
            compare=lambda a, b: len(a) == len(b),
        )

        print("\n=== 6. schedule exceptionSchedule -- the other holiday route ===")
        exception = [
            SpecialEvent(
                period=SpecialEventPeriod(
                    calendarEntry=CalendarEntry(date=Date((126, 12, 25, 255)))
                ),
                listOfTimeValues=[TimeValue(time=Time("00:00:00"), value=Unsigned(1))],
                eventPriority=1,
            )
        ]
        await roundtrip(
            app, addr, "schedule,2", "exceptionSchedule", exception,
            "OccSchedule.exceptionSchedule",
            compare=lambda a, b: len(a) == len(b),
        )

        print("\n=== 7. schedule exceptionSchedule by calendar reference ===")
        by_ref = [
            SpecialEvent(
                period=SpecialEventPeriod(calendarReference="calendar,3"),
                listOfTimeValues=[TimeValue(time=Time("00:00:00"), value=Unsigned(1))],
                eventPriority=1,
            )
        ]
        await roundtrip(
            app, addr, "schedule,2", "exceptionSchedule", by_ref,
            "exceptionSchedule via calendarReference",
            compare=lambda a, b: len(a) == len(b),
        )

        print("\n=== 8. weeklySchedule (write the SAME value back -- acceptance only) ===")
        current = await read(app, addr, "schedule,2", "weeklySchedule")
        err = await try_write(app, addr, "schedule,2", "weeklySchedule", current)
        if err:
            record("OccSchedule.weeklySchedule", "REJECTED", err)
        else:
            record("OccSchedule.weeklySchedule", "WRITABLE", "identical value accepted, unchanged")

        print("\n\n================ SUMMARY ================")
        for name, verdict, detail in results:
            print(f"{verdict:12} {name}")
        print("\nACCEPTED* = write succeeded but read-back differed; check detail above.")

    except Exception:  # noqa: BLE001
        traceback.print_exc()
    finally:
        app.close()


if __name__ == "__main__":
    asyncio.run(main())
