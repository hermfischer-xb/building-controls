#!/usr/bin/env python3
"""Read-only probe of the points the BMS actually depends on.

Separate from discover.py because that dumps everything; this asks the specific
questions the design hinges on -- can we read holiday lists, what does the
occupancy chain look like, does the device claim COV.

    .venv/bin/python tools/probe.py --address 192.168.1.10/24 --target 192.168.1.101
"""

import asyncio
import sys

from bacpypes3.apdu import ErrorRejectAbortNack
from bacpypes3.app import Application
from bacpypes3.argparse import SimpleArgumentParser
from bacpypes3.pdu import Address

# The points the application actually drives, by role.
POINTS = {
    "occupancy chain": [
        ("multi-state-value,4", "ni_OccManCom (override command)"),
        ("multi-state-value,1", "ni_NetSchCurrentState"),
        ("multi-state-output,20", "no_EffOccState"),
        ("binary-value,1", "ni_BypassState"),
        ("analog-value,2", "ni_BypassValue"),
        ("analog-output,254", "schedule 2 target"),
        ("analog-output,255", "schedule 3 target"),
    ],
    "setpoints": [
        ("analog-value,4", "Cfg_Setpoints_OccCoolSp"),
        ("analog-value,7", "Cfg_Setpoints_OccHeatSp"),
        ("analog-value,5", "Cfg_Setpoints_StbyCoolSp"),
        ("analog-value,8", "Cfg_Setpoints_StbyHeatSp"),
        ("analog-value,6", "Cfg_Setpoints_UnOccCoolSp"),
        ("analog-value,9", "Cfg_Setpoints_UnOccHeatSp"),
    ],
    "readings": [
        ("analog-output,18", "no_SpaceTemp"),
        ("analog-output,5", "no_EffSp"),
        ("analog-output,3", "no_EffHeatSp"),
        ("analog-output,4", "no_EffCoolSp"),
    ],
}


async def read(app, addr, objid, prop, index=None):
    try:
        return await app.read_property(addr, objid, prop, array_index=index)
    except ErrorRejectAbortNack as err:
        return f"<{err}>"
    except Exception as err:  # noqa: BLE001
        return f"<error: {err}>"


async def main() -> None:
    parser = SimpleArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="thermostat IP, e.g. 192.168.1.101")
    args = parser.parse_args()

    app = Application.from_args(args)
    addr = Address(args.target)

    try:
        print("=== calendar dateList (holiday storage) ===")
        for inst in range(3, 13):
            objid = f"calendar,{inst}"
            name = await read(app, addr, objid, "objectName")
            date_list = await read(app, addr, objid, "dateList")
            print(f"  {objid:14} {str(name):12} dateList={date_list}")

        print("\n=== schedule object writability signals ===")
        for inst in (2, 3):
            objid = f"schedule,{inst}"
            for prop in ("objectName", "priorityForWriting", "outOfService", "reliability"):
                print(f"  {objid} {prop:22} {await read(app, addr, objid, prop)}")

        print("\n=== device: services and object types claimed ===")
        for prop in ("protocolServicesSupported", "protocolObjectTypesSupported"):
            print(f"  {prop}\n    {await read(app, addr, 'device,4194302', prop)}")

        for section, points in POINTS.items():
            print(f"\n=== {section} ===")
            for objid, label in points:
                pv = await read(app, addr, objid, "presentValue")
                name = await read(app, addr, objid, "objectName")
                print(f"  {objid:22} {str(name):32} = {pv}   ({label})")

    finally:
        app.close()


if __name__ == "__main__":
    asyncio.run(main())
