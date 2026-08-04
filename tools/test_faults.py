#!/usr/bin/env python3
"""Every BACnet fault must be converted at the client boundary, and no loop may die of one.

Written after the reconciler stopped for four hours on 2026-08-04 with nothing in
the log. bacpypes3's error classes derive from BaseException rather than
Exception, so `except Exception` does not catch them. `read_calendar` was the one
client method that forgot to convert, and the raw AbortPDU went through the
handler around the call, through the reconcile loop's own guard, and killed the
task. The guard that should have logged it could not catch it either, so the only
symptom was a failed shutdown at the next restart.

Nothing in the existing suites could see this: they exercise the API and the
rendered pages, and a dead background task serves a perfectly healthy dashboard.

Three properties, in the order the failure travelled:

  1. the fault classes really are outside Exception  -- the premise
  2. every client method converts them to DeviceUnreachable  -- the boundary
  3. the loops and shutdown survive one that escapes anyway  -- the net
"""

import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bacpypes3.apdu import AbortPDU, ErrorRejectAbortNack  # noqa: E402

from bms.bacnet import BACNET_FAULTS, BacnetClient, DeviceUnreachable  # noqa: E402
from bms.config import BacnetConfig, DeviceConfig  # noqa: E402
from bms.points import POINTS  # noqa: E402

failures = 0


def check(ok: bool, label: str) -> None:
    global failures
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


DEVICE = DeviceConfig(device_id=301, address="192.168.144.201", name="Suite 301", zone="floor-3")


def a_fault() -> AbortPDU:
    """What a thermostat that does not answer produces once retries are enabled."""
    return AbortPDU(reason=0)


class DeadApp:
    """Stands in for the bacpypes3 Application, with every device dark."""

    def __init__(self) -> None:
        self.device_object = type("D", (), {"apduTimeout": 0, "numberOfApduRetries": 0})()

    async def read_property(self, *a, **k):
        raise a_fault()

    async def write_property(self, *a, **k):
        raise a_fault()

    async def read_property_multiple(self, *a, **k):
        raise a_fault()

    def request(self, *a, **k):
        raise a_fault()

    def close(self) -> None:
        pass


def dead_client() -> BacnetClient:
    client = BacnetClient(BacnetConfig(address="192.168.144.1/24", device_id=4000000), 0.2)
    client._app = DeadApp()
    return client


async def main() -> int:
    print("\n--- the premise: these are not Exceptions ---")
    check(not issubclass(ErrorRejectAbortNack, Exception),
          "ErrorRejectAbortNack is outside Exception, so `except Exception` misses it")
    check(issubclass(AbortPDU, ErrorRejectAbortNack),
          "AbortPDU derives from it, so BACNET_FAULTS does catch it")
    check(ErrorRejectAbortNack in BACNET_FAULTS,
          "BACNET_FAULTS names it")

    print("\n--- the boundary: every method converts, none leaks a raw fault ---")
    # The bug was one method in this list being absent from it. Anything that
    # talks to a device and can be reached from a long-lived loop belongs here.
    writable = next(p for p in POINTS if p.writable)
    calls = {
        "read_points": lambda c: c.read_points(DEVICE),
        "read_points_timed": lambda c: c.read_points_timed(DEVICE),
        "write_point": lambda c: c.write_point(DEVICE, writable, 70.0),
        "read_calendar": lambda c: c.read_calendar(DEVICE, "calendar,1"),
        "write_calendar": lambda c: c.write_calendar(DEVICE, "calendar,1", []),
        "read_weekly_schedule": lambda c: c.read_weekly_schedule(DEVICE, "schedule,1"),
        "write_weekly_schedule": lambda c: c.write_weekly_schedule(DEVICE, "schedule,1", []),
        "write_exception_schedule": lambda c: c.write_exception_schedule(DEVICE, "schedule,1", []),
        # Unconfirmed, so it has no protocol error to report -- but the send
        # itself can fail with OSError, and its caller in _reconcile_clock runs
        # inside the reconcile loop. This test is what found it unguarded.
        "sync_time": lambda c: c.sync_time(DEVICE, dt.datetime.now()),
    }
    for name, call in calls.items():
        client = dead_client()
        try:
            await call(client)
            check(False, f"{name} swallowed a dead device instead of raising")
        except DeviceUnreachable:
            check(True, f"{name} -> DeviceUnreachable")
        except BACNET_FAULTS as err:
            check(False, f"{name} leaked a raw {type(err).__name__} -- unconverted boundary")

    print("\n--- deliberately best-effort: these must not raise at all ---")
    for name, call in (
        ("read_device_time", lambda c: c.read_device_time(DEVICE)),
        ("read_device_id", lambda c: c.read_device_id(DEVICE)),
    ):
        client = dead_client()
        try:
            await call(client)
            check(True, f"{name} stays quiet when the device is dark")
        except BaseException as err:  # noqa: BLE001 - the whole point is that nothing escapes
            check(False, f"{name} raised {type(err).__name__}")

    print("\n--- the net: a loop guard must outlive a fault that escapes anyway ---")
    # Simulates a boundary someone forgets to convert in future. The guard has to
    # catch it, because the alternative is a task that dies in silence.
    survived = {"cycles": 0}

    async def leaky_cycle() -> None:
        survived["cycles"] += 1
        raise a_fault()

    class FakeReconciler:
        """The real _run body, exercised without a live BACnet stack."""

        async def run(self, iterations: int) -> None:
            for _ in range(iterations):
                try:
                    await leaky_cycle()
                except (*BACNET_FAULTS, Exception):
                    pass

    await FakeReconciler().run(3)
    check(survived["cycles"] == 3, "the loop ran all 3 cycles despite a raw fault each time")

    # And the same guard must still let cancellation through, or stop() hangs.
    cancelled = {"ok": False}

    async def cancellable() -> None:
        while True:
            try:
                await asyncio.sleep(3600)
            except (*BACNET_FAULTS, Exception):
                pass

    task = asyncio.create_task(cancellable())
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        cancelled["ok"] = True
    check(cancelled["ok"], "and CancelledError still passes through it")

    print("\n--- shutdown: one failing step must not skip the rest ---")
    order: list[str] = []

    async def ok_step(name: str) -> None:
        order.append(name)

    async def bad_step() -> None:
        order.append("bad")
        raise a_fault()

    closed = {"db": False}
    for name, close in (("a", lambda: ok_step("a")), ("bad", bad_step), ("b", lambda: ok_step("b"))):
        try:
            await close()
        except (*BACNET_FAULTS, Exception):
            pass
    try:
        closed["db"] = True
    except Exception:  # noqa: BLE001
        pass
    check(order == ["a", "bad", "b"], "every step ran, including the one behind the failure")
    check(closed["db"], "and the database still got closed")

    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
