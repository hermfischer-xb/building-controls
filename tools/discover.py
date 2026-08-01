#!/usr/bin/env python3
"""Discover TC500A thermostats on the BACnet/IP network and dump their real object map.

The Honeywell integration guide contradicts itself in places (Calendar objects are
listed in Table 44 but Chapter 10 says they are unsupported; the device object
"claims unsupported objects and services"). This tool records what the hardware
actually exposes so we can build against that instead of the PDF.

Usage:
    .venv/bin/python tools/discover.py --address 192.168.1.10/24
    .venv/bin/python tools/discover.py --address 192.168.1.10/24 --device 2001
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bacpypes3.apdu import ErrorRejectAbortNack
from bacpypes3.app import Application
from bacpypes3.argparse import SimpleArgumentParser
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import ObjectIdentifier

# Read for every object we find. Missing properties are expected and skipped.
OBJECT_PROPERTIES = [
    "objectName",
    "presentValue",
    "description",
    "units",
    "statusFlags",
    "outOfService",
    "priorityArray",
    "relinquishDefault",
    "numberOfStates",
    "stateText",
    "inactiveText",
    "activeText",
]

# Schedule objects carry the weekly/exception schedules the whole app depends on.
SCHEDULE_PROPERTIES = [
    "objectName",
    "presentValue",
    "effectivePeriod",
    "weeklySchedule",
    "exceptionSchedule",
    "scheduleDefault",
    "listOfObjectPropertyReferences",
    "priorityForWriting",
]

DEVICE_PROPERTIES = [
    "objectName",
    "vendorName",
    "vendorIdentifier",
    "modelName",
    "firmwareRevision",
    "applicationSoftwareVersion",
    "protocolVersion",
    "protocolRevision",
    "protocolServicesSupported",
    "protocolObjectTypesSupported",
    "maxApduLengthAccepted",
    "segmentationSupported",
    "apduTimeout",
    "databaseRevision",
    "localDate",
    "localTime",
    "utcOffset",
    "daylightSavingsStatus",
]


def encode(value: Any) -> Any:
    """Make a bacpypes3 value JSON-serializable without losing detail."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if hasattr(value, "dict_contents"):
        try:
            return encode(value.dict_contents())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    return str(value)


async def read_one(app: Application, addr: Address, objid: Any, prop: str) -> Any:
    """Read a single property, returning None if the device doesn't have it."""
    try:
        return await app.read_property(addr, objid, prop)
    except ErrorRejectAbortNack:
        return None
    except Exception as err:  # noqa: BLE001 - we want the reason recorded, not raised
        return f"<error: {err}>"


async def dump_object(app: Application, addr: Address, objid: ObjectIdentifier) -> dict:
    obj_type = str(objid[0])
    props = SCHEDULE_PROPERTIES if obj_type == "schedule" else OBJECT_PROPERTIES

    record: dict[str, Any] = {
        "objectIdentifier": str(objid),
        "objectType": obj_type,
        "instance": objid[1],
    }
    for prop in props:
        value = await read_one(app, addr, objid, prop)
        if value is not None:
            record[prop] = encode(value)
    return record


async def dump_device(app: Application, addr: Address, device_id: int) -> dict:
    devid = ObjectIdentifier(f"device,{device_id}")
    print(f"\n=== device {device_id} @ {addr} ===", file=sys.stderr)

    device: dict[str, Any] = {"deviceIdentifier": device_id, "address": str(addr)}
    for prop in DEVICE_PROPERTIES:
        value = await read_one(app, addr, devid, prop)
        if value is not None:
            device[prop] = encode(value)

    object_list = await read_one(app, addr, devid, "objectList")
    if not isinstance(object_list, list):
        print("  ! could not read objectList", file=sys.stderr)
        device["objects"] = []
        return device

    print(f"  objectList: {len(object_list)} objects", file=sys.stderr)
    objects = []
    for index, objid in enumerate(object_list, 1):
        record = await dump_object(app, addr, objid)
        objects.append(record)
        name = record.get("objectName", "?")
        value = record.get("presentValue", "")
        print(f"  [{index}/{len(object_list)}] {objid} {name} = {value}", file=sys.stderr)

    device["objects"] = objects
    return device


async def main() -> None:
    # SimpleArgumentParser supplies --address, --instance, --network and the
    # --foreign/--ttl pair we need if this host is not on the thermostats' subnet.
    parser = SimpleArgumentParser(description=__doc__)
    parser.add_argument(
        "--device",
        type=int,
        action="append",
        help="only dump this device id (repeatable); default is every device found",
    )
    parser.add_argument("--timeout", type=int, default=5, help="Who-Is wait in seconds")
    parser.add_argument(
        "--target",
        help="unicast Who-Is to this address instead of broadcasting, e.g. "
        "192.168.1.50 -- use when the AP or switch blocks directed broadcasts",
    )
    parser.add_argument(
        "--out",
        default="tools/discovery.json",
        help="where to write the dump",
    )
    args = parser.parse_args()

    app = Application.from_args(args)

    try:
        if args.target:
            print(f"unicast Who-Is to {args.target} (waiting {args.timeout}s) ...", file=sys.stderr)
            i_ams = await app.who_is(address=Address(args.target), timeout=args.timeout)
        else:
            print(f"broadcasting Who-Is (waiting {args.timeout}s) ...", file=sys.stderr)
            i_ams = await app.who_is(timeout=args.timeout)

        found = []
        for i_am in i_ams:
            device_id = i_am.iAmDeviceIdentifier[1]
            found.append((device_id, i_am.pduSource))
            print(f"  found device {device_id} at {i_am.pduSource}", file=sys.stderr)

        if not found:
            print(
                "\nNo devices answered. Check that this host is on the same subnet/SSID as\n"
                "the thermostat, that AP client isolation is OFF, and that the thermostat\n"
                "shows BACnet IP (not MS/TP) under Config > Connection.",
                file=sys.stderr,
            )
            return

        wanted = set(args.device or [])
        devices = [
            await dump_device(app, addr, device_id)
            for device_id, addr in sorted(found)
            if not wanted or device_id in wanted
        ]

        out = {
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "devices": devices,
        }
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, indent=2))
        print(f"\nwrote {out_path} ({len(devices)} device(s))", file=sys.stderr)

    finally:
        app.close()


if __name__ == "__main__":
    asyncio.run(main())
