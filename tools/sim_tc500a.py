#!/usr/bin/env python3
"""A virtual TC500A thermostat for offline development.

Exposes the subset of the real object map the BMS actually drives, using the
instance numbers from the Honeywell BACnet Integration Guide (31-00478). Run one
per simulated zone on its own UDP port and you can develop the poller, the
schedule reconciler and the web UI with no hardware on the bench.

    .venv/bin/python tools/sim_tc500a.py --address 192.168.1.10:47809 --instance 2001

Once tools/discovery.json exists from a real thermostat, prefer regenerating this
map from it -- the guide is known to disagree with the hardware in places.
"""

import asyncio
import random

from bacpypes3.app import Application
from bacpypes3.argparse import SimpleArgumentParser
from bacpypes3.local.analog import AnalogOutputObject, AnalogValueObject
from bacpypes3.local.binary import BinaryValueObject
from bacpypes3.local.multistate import MultiStateOutputObject, MultiStateValueObject
from bacpypes3.basetypes import DailySchedule, DateRange, TimeValue
from bacpypes3.local.schedule import ScheduleObject
from bacpypes3.primitivedata import Date, Time, Unsigned

# Occupancy enums differ between the schedule and the override points, which is a
# real and easy-to-miss quirk of this device:
#   EnumSchedule (Schedule 2): 0=Occupied, 1=Unoccupied, 3=Standby
#   ni_OccManCom (MSV 4):      1=Occupied, 2=Unoccupied, 3=Bypass, 4=Standby,
#                              5=No Override
OCC_OVERRIDE_STATES = ["Occupied", "Unoccupied", "Bypass", "Standby", "No Override"]
EFF_OCC_STATES = ["Occupied", "Unoccupied", "Bypass", "Standby", "No Override"]


def build_objects() -> list:
    """Objects and instance numbers per the integration guide Tables 1, 22 and 37."""
    return [
        # --- Sensor readings (Table 1, read-only) ---
        AnalogOutputObject(
            objectIdentifier="analog-output,18",
            objectName="no_SpaceTemp",
            description="Space temperature",
            presentValue=71.5,
            units="degreesFahrenheit",
        ),
        AnalogOutputObject(
            objectIdentifier="analog-output,19",
            objectName="no_SpaceHumidity",
            description="Space humidity",
            presentValue=44.0,
            units="percentRelativeHumidity",
        ),
        AnalogOutputObject(
            objectIdentifier="analog-output,5",
            objectName="no_EffSp",
            description="Effective setpoint",
            presentValue=72.0,
            units="degreesFahrenheit",
        ),
        AnalogOutputObject(
            objectIdentifier="analog-output,3",
            objectName="no_EffHeatSp",
            description="Effective heating setpoint",
            presentValue=68.0,
            units="degreesFahrenheit",
        ),
        AnalogOutputObject(
            objectIdentifier="analog-output,4",
            objectName="no_EffCoolSp",
            description="Effective cooling setpoint",
            presentValue=76.0,
            units="degreesFahrenheit",
        ),
        MultiStateOutputObject(
            objectIdentifier="multi-state-output,20",
            objectName="no_EffOccState",
            description="Effective occupancy state",
            presentValue=1,
            numberOfStates=len(EFF_OCC_STATES),
            stateText=EFF_OCC_STATES,
        ),
        # --- Occupancy setpoints (Table 22, writable) ---
        AnalogValueObject(
            objectIdentifier="analog-value,4",
            objectName="Cfg_Setpoints_OccCoolSp",
            description="Occupied cooling setpoint",
            presentValue=76.0,
            units="degreesFahrenheit",
        ),
        AnalogValueObject(
            objectIdentifier="analog-value,7",
            objectName="Cfg_Setpoints_OccHeatSp",
            description="Occupied heating setpoint",
            presentValue=68.0,
            units="degreesFahrenheit",
        ),
        AnalogValueObject(
            objectIdentifier="analog-value,5",
            objectName="Cfg_Setpoints_StbyCoolSp",
            description="Standby cooling setpoint",
            presentValue=80.0,
            units="degreesFahrenheit",
        ),
        AnalogValueObject(
            objectIdentifier="analog-value,8",
            objectName="Cfg_Setpoints_StbyHeatSp",
            description="Standby heating setpoint",
            presentValue=65.0,
            units="degreesFahrenheit",
        ),
        AnalogValueObject(
            objectIdentifier="analog-value,6",
            objectName="Cfg_Setpoints_UnOccCoolSp",
            description="Unoccupied cooling setpoint",
            presentValue=85.0,
            units="degreesFahrenheit",
        ),
        AnalogValueObject(
            objectIdentifier="analog-value,9",
            objectName="Cfg_Setpoints_UnOccHeatSp",
            description="Unoccupied heating setpoint",
            presentValue=55.0,
            units="degreesFahrenheit",
        ),
        # --- Outdoor air ---
        # 65535 is the device's "no value" sentinel, which is what a thermostat
        # with no outdoor sensor and no network write actually reports. Starting
        # here rather than at a plausible temperature means the simulator
        # exercises the "do not share garbage" path by default.
        AnalogOutputObject(
            objectIdentifier="analog-output,16",
            objectName="no_OaTemp",
            description="Outdoor air temperature",
            presentValue=65535.0,
            units="degreesFahrenheit",
        ),
        AnalogOutputObject(
            objectIdentifier="analog-output,17",
            objectName="no_OaHumidity",
            description="Outdoor air humidity",
            presentValue=65535.0,
            units="percentRelativeHumidity",
        ),
        AnalogValueObject(
            objectIdentifier="analog-value,89",
            objectName="ni_OutdoorTemp",
            description="Network outdoor temperature (point sharing)",
            presentValue=65535.0,
            units="degreesFahrenheit",
        ),
        AnalogValueObject(
            objectIdentifier="analog-value,194",
            objectName="ni_OutdoorHum",
            description="Network outdoor humidity (point sharing)",
            presentValue=65535.0,
            units="percentRelativeHumidity",
        ),
        # --- Occupancy override (Table 37, writable) ---
        MultiStateValueObject(
            objectIdentifier="multi-state-value,4",
            objectName="ni_OccManCom",
            description="Network occupancy manual override command",
            presentValue=5,
            numberOfStates=len(OCC_OVERRIDE_STATES),
            stateText=OCC_OVERRIDE_STATES,
        ),
        BinaryValueObject(
            objectIdentifier="binary-value,1",
            objectName="ni_BypassState",
            description="Enable bypass timer",
            presentValue="inactive",
        ),
        AnalogValueObject(
            objectIdentifier="analog-value,2",
            objectName="ni_BypassValue",
            description="Bypass time in minutes",
            presentValue=0.0,
            units="minutes",
        ),
        # --- Schedule. Instance 2 is the one that matters. Note the real device
        # names it OccSchedule, not EnumSchedule as the guide's Table 45 claims. ---
        ScheduleObject(
            objectIdentifier="schedule,2",
            objectName="OccSchedule",
            description="Occupancy schedule (0=Occupied, 1=Unoccupied, 3=Standby)",
            presentValue=Unsigned(0),
            # The real device reports an all-wildcard effectivePeriod (255,255,255,255),
            # but bacpypes3's match_date_range compares raw tuples and has no wildcard
            # handling, so a wildcard range never matches and eval() returns None. Use a
            # wide concrete range here so the simulated schedule actually evaluates.
            # Read code must still expect wildcards from real hardware.
            effectivePeriod=DateRange(
                startDate=Date((0, 1, 1, 255)),  # 1900-01-01
                endDate=Date((254, 12, 31, 255)),  # 2154-12-31
            ),
            # Shipped default, copied from a real TC500A: Mon-Fri occupied
            # 06:00-18:00. Note the device writes an explicit 00:00 Unoccupied entry
            # on weekends rather than leaving the day empty -- mirror that, an empty
            # daySchedule is not something this device ever produces.
            weeklySchedule=[
                DailySchedule(
                    daySchedule=[
                        TimeValue(time=Time("06:00:00"), value=Unsigned(0)),
                        TimeValue(time=Time("18:00:00"), value=Unsigned(1)),
                    ]
                    if day < 5
                    else [TimeValue(time=Time("00:00:00"), value=Unsigned(1))]
                )
                for day in range(7)
            ],
            exceptionSchedule=[],
            scheduleDefault=Unsigned(1),
            priorityForWriting=15,
        ),
    ]


# Network inputs the real device copies onto its corresponding output once a value
# arrives. Verified on hardware: writing ni_OutdoorTemp moved both no_OaTemp and
# the OaTemp_Display shown on the thermostat's own screen. Without this the
# simulator accepts a shared reading and then appears to ignore it, which looks
# exactly like a broken point-sharing implementation.
INPUT_TO_OUTPUT = (
    ("ni_OutdoorTemp", "no_OaTemp"),
    ("ni_OutdoorHum", "no_OaHumidity"),
)


async def drift(app: Application) -> None:
    """Nudge the space temperature, and mirror network inputs to their outputs."""
    space_temp = app.get_object_name("no_SpaceTemp")
    pairs = [
        (app.get_object_name(src), app.get_object_name(dst))
        for src, dst in INPUT_TO_OUTPUT
        if app.get_object_name(src) and app.get_object_name(dst)
    ]
    while True:
        await asyncio.sleep(5)
        space_temp.presentValue = round(
            min(78.0, max(66.0, space_temp.presentValue + random.uniform(-0.3, 0.3))), 1
        )
        for src, dst in pairs:
            if src.presentValue != dst.presentValue:
                dst.presentValue = src.presentValue


async def main() -> None:
    parser = SimpleArgumentParser(description=__doc__)
    args = parser.parse_args()

    app = Application.from_args(args)
    for obj in build_objects():
        app.add_object(obj)

    print(f"TC500A simulator: device {args.instance} on {args.address}")
    print(f"  {len(app.device_object.objectList)} objects; Ctrl-C to stop")

    try:
        await drift(app)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        app.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
