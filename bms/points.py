"""The TC500A points the BMS actually drives.

Object instances are taken from a real TC500A-N (firmware 01.01.16.00), not from
the integration guide -- the guide disagrees with the hardware in several places.
See tools/discovery.json for the full 770-object dump.

Two enum quirks worth knowing before reading further:

* The schedule uses 0=Occupied, 1=Unoccupied, 3=Standby.
* The occupancy points use a *different, 1-based* enum: 1=Occ, 2=UnOcc, 3=Bypass,
  4=StandBy, 5=NoOvrd. These are not interchangeable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class ScheduleState(IntEnum):
    """Values carried by the Schedule object and its exception entries."""

    OCCUPIED = 0
    UNOCCUPIED = 1
    STANDBY = 3


class OccupancyState(IntEnum):
    """Values used by ni_OccManCom and no_EffOccState (device's own stateText)."""

    OCCUPIED = 1
    UNOCCUPIED = 2
    BYPASS = 3
    STANDBY = 4
    NO_OVERRIDE = 5


@dataclass(frozen=True)
class Point:
    key: str
    objid: str
    writable: bool
    units: str | None = None
    enum: type[IntEnum] | None = None
    description: str = ""


# Polled every cycle. Keep this list tight -- the device has 770 objects and we
# want the whole poll in one RPM request.
POINTS: tuple[Point, ...] = (
    # --- readings ---
    Point("space_temp", "analog-output,18", False, "degF", None, "Space temperature"),
    Point("space_humidity", "analog-output,19", False, "%RH", None, "Space humidity"),
    Point("effective_sp", "analog-output,5", False, "degF", None, "Effective setpoint"),
    Point("effective_heat_sp", "analog-output,3", False, "degF", None, "Effective heating setpoint"),
    Point("effective_cool_sp", "analog-output,4", False, "degF", None, "Effective cooling setpoint"),
    Point(
        "effective_occupancy",
        "multi-state-output,20",
        False,
        None,
        OccupancyState,
        "Occupancy state the thermostat is actually acting on",
    ),
    # --- setpoints, writable ---
    Point("occ_cool_sp", "analog-value,4", True, "degF", None, "Occupied cooling setpoint"),
    Point("occ_heat_sp", "analog-value,7", True, "degF", None, "Occupied heating setpoint"),
    Point("standby_cool_sp", "analog-value,5", True, "degF", None, "Standby cooling setpoint"),
    Point("standby_heat_sp", "analog-value,8", True, "degF", None, "Standby heating setpoint"),
    Point("unocc_cool_sp", "analog-value,6", True, "degF", None, "Unoccupied cooling setpoint"),
    Point("unocc_heat_sp", "analog-value,9", True, "degF", None, "Unoccupied heating setpoint"),
    # --- occupancy override, writable ---
    Point(
        "occupancy_override",
        "multi-state-value,4",
        True,
        None,
        OccupancyState,
        "ni_OccManCom -- forces occupancy until set back to NO_OVERRIDE",
    ),
    Point("bypass_enable", "binary-value,1", True, None, None, "ni_BypassState"),
    Point("bypass_minutes", "analog-value,2", True, "minutes", None, "ni_BypassValue"),
    # Read-back of what the device is actually doing with the bypass, as opposed
    # to what we last asked for. The remaining-time countdown is what a tenant
    # sees ("occupied for another 2h 47m"), so it has to come from the device.
    Point("bypass_active", "binary-output,1", False, None, None, "no_BypassState"),
    Point(
        "bypass_remaining_minutes",
        "analog-output,65",
        False,
        "minutes",
        None,
        "no_BypassRemTime -- counts down while bypass is running",
    ),
    # --- schedule ---
    Point(
        "schedule_state",
        "schedule,2",
        False,
        None,
        ScheduleState,
        "OccSchedule present value (0=Occ, 1=UnOcc, 3=Standby)",
    ),
)

BY_KEY: dict[str, Point] = {p.key: p for p in POINTS}
BY_OBJID: dict[str, Point] = {p.objid: p for p in POINTS}

# Not polled -- read and written on demand, because they carry large constructed
# values and only change when someone edits a schedule.
SCHEDULE_OBJID = "schedule,2"
CALENDAR_OBJIDS = tuple(f"calendar,{i}" for i in range(3, 13))

# Setpoint guard rails. The device accepts 40-99F but letting a tenant set 40F is
# how you freeze pipes; the API clamps to these before writing.
SETPOINT_LIMITS: dict[str, tuple[float, float]] = {
    "occ_cool_sp": (65.0, 85.0),
    "occ_heat_sp": (60.0, 78.0),
    "standby_cool_sp": (68.0, 88.0),
    "standby_heat_sp": (55.0, 75.0),
    "unocc_cool_sp": (75.0, 95.0),
    "unocc_heat_sp": (50.0, 68.0),
}
