#!/usr/bin/env python3
"""Capture every thermostat's live weekly schedule before anything overwrites it.

Assigning a device to a schedule group makes the reconciler push that group onto
the hardware, replacing whatever weekly schedule is on it now. Nothing in this
system has ever recorded what those schedules are -- the database holds intent,
and no intent has been expressed yet, so the devices are the only copy. Hours
somebody set at the wall two years ago exist nowhere else.

Read-only. Binds a spare port beside the running gateway, exactly like the other
probes here, so it neither restarts the daemon nor contends for its socket.

Writes JSON to the path given, and prints a table to read.

    .venv/bin/python tools/capture_schedules.py \
        data/schedule-backups/live-$(date +%Y%m%d-%H%M).json

Run it before assigning schedule groups, before changing a group every device
follows, and after any deliberate schedule change so the new state is the
recoverable one. `data/` is gitignored, so these backups stay on the machine that
made them -- which is correct, since they describe a real building's occupancy.

A device that fails is NOT in the file. The summary says so explicitly rather
than reporting a count that looks complete; a partial capture presented as a
backup is worse than no backup. Flaky units may need a second run -- Suite 231
took three attempts on 2026-08-05.
"""

from __future__ import annotations

import asyncio
import json
import sys

from bacpypes3.pdu import Address
from bacpypes3.app import Application

sys.path.insert(0, "/Users/Shared/building-controls")
from bms.bacnet import _Args, _describe  # noqa: E402
from bms.config import load  # noqa: E402
from bms.schedules import week_summary  # noqa: E402

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
OUT = sys.argv[1] if len(sys.argv) > 1 else "schedules-backup.json"


async def main() -> None:
    cfg = load("config/devices.yaml")
    app = Application.from_args(
        _Args(address="192.168.144.1/24:47809", instance=4000001,
              name="bms-probe", foreign=None, ttl=0)
    )

    captured, failed = {}, []
    for device in cfg.devices:
        try:
            weekly = await asyncio.wait_for(
                app.read_property(Address(device.address), "schedule,2", "weeklySchedule"),
                timeout=15,
            )
        except BaseException as err:  # bacpypes3 faults are outside Exception
            if isinstance(err, (KeyboardInterrupt, SystemExit)):
                raise
            print(f"{device.name:<12} FAILED  {_describe(err)}")
            failed.append(device.name)
            continue

        week = {
            day: [
                {"time": f"{tv.time[0]:02d}:{tv.time[1]:02d}",
                 "state": int(tv.value.get_value())}
                for tv in (entry.daySchedule or [])
            ]
            for day, entry in zip(DAYS, weekly)
        }
        captured[str(device.device_id)] = {
            "name": device.name, "address": device.address, "weekly": week,
        }
        # week_summary keys on 1..7, the shape the rest of the system uses.
        summary = week_summary({i + 1: week[d] for i, d in enumerate(DAYS)})
        print(f"{device.name:<12} {summary or '(no occupied period on any day)'}")

    with open(OUT, "w") as fh:
        json.dump(captured, fh, indent=2, sort_keys=True)

    print()
    print(f"captured {len(captured)} of {len(cfg.devices)} -> {OUT}")
    if failed:
        print(f"FAILED, and therefore NOT recoverable: {', '.join(failed)}")
        print("Do not assign a group to these until they have been captured.")

    app.close()


asyncio.run(main())
