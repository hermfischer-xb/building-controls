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


class SetpointStatus(IntEnum):
    """Which setpoint the thermostat is currently working to.

    Read from `no_SetpointSts`, whose own stateText on firmware 01.01.16.00 is
    ["Occ", "UnOcc", "Temporary", "StandBy"] -- BACnet multi-state values are
    1-based, so these are the device's own labels rather than an interpretation.

    TEMPORARY is the interesting one: it means somebody has adjusted the setpoint
    at the thermostat itself, using the slider on its home screen, and that
    adjustment stands until the next scheduled change or the override times out.
    It is the only way to tell a suite running to its schedule from one somebody
    has quietly nudged.
    """

    OCCUPIED = 1
    UNOCCUPIED = 2
    TEMPORARY = 3
    STANDBY = 4


class TempMode(IntEnum):
    """no_EffTempMode -- which mode the thermostat is in.

    This is not the same as "currently running": a unit can sit in HEAT mode with
    no stages energised. The stage counts say what the equipment is actually
    doing; this says what it would do if it called for something.
    """

    COOL = 1
    REHEAT = 2
    HEAT = 3
    EMERGENCY_HEAT = 4
    OFF = 5


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
        "setpoint_status",
        "multi-state-output,7",
        False,
        None,
        SetpointStatus,
        "no_SetpointSts -- Occ/UnOcc/Temporary/StandBy. TEMPORARY means someone "
        "moved the slider on the thermostat itself",
    ),
    Point(
        "effective_occupancy",
        "multi-state-output,20",
        False,
        None,
        OccupancyState,
        "Occupancy state the thermostat is actually acting on",
    ),
    # --- what the equipment is doing right now ---
    # Stage counts rather than the mode, because that is the difference between
    # "set to heat" and "heating". A dashboard that shows the mode makes every
    # idle unit look like it is running.
    Point(
        "temp_mode", "multi-state-output,6", False, None, TempMode,
        "no_EffTempMode -- selected mode, not necessarily running",
    ),
    Point(
        "active_heat_stages", "analog-output,7", False, "stages", None,
        "no_ActiveHeatStages -- heat stages energised now",
    ),
    Point(
        "active_cool_stages", "analog-output,11", False, "stages", None,
        "no_ActiveCoolStages -- cool stages energised now",
    ),
    Point(
        "active_aux_heat_stages", "analog-output,8", False, "stages", None,
        "no_ActiveAuxHeatStages -- backup/auxiliary heat",
    ),
    Point(
        "fan_running", "binary-output,19", False, None, None,
        "no_FanStart -- supply fan commanded on",
    ),
    Point(
        "economizer_enabled", "binary-output,22", False, None, None,
        "no_EconEn -- economizer providing free cooling",
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
    # Two gating points the integration guide does not connect to anything, both
    # established by testing. Without them the obvious writes are accepted and
    # then quietly ignored:
    #
    #   ni_BypassValue does NOT set the bypass duration -- this config point does.
    #   Writing 60 to ni_BypassValue and starting a bypass still runs for whatever
    #   this holds (180 by default).
    Point(
        "bypass_duration_cfg", "analog-value,10", True, "minutes", None,
        "Cfg_Thermostat_BypOverrideTime -- the actual bypass duration",
    ),
    #   ni_OccManCom does nothing at all until this is enabled. With it off the
    #   point reads back the value written while effective occupancy never moves.
    Point(
        "network_override_enable", "binary-value,135", True, None, None,
        "Cfg_Thermostat_Override -- gates whether ni_OccManCom is honoured",
    ),
    #   The clamp on the thermostat's own slider. Ships at 30 deltaF, which is no
    #   limit in practice -- it lets an occupant standing at the wall ask for 38
    #   or 106. Narrowing this is the only way to bound what someone can do
    #   locally, because the adjustment is made on the device and there is no
    #   network point to intercept it.
    Point(
        "occupant_adjust_limit", "analog-value,102", True, "deltaF", None,
        "Cfg_Thermostat_TempOffSpLimit -- how far the slider may move the setpoint",
    ),
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
    # --- outdoor air ---
    #
    # Only one thermostat needs a physical outdoor sensor. The rest receive the
    # value from the gateway, which is how this device is designed to work: it
    # ships a watchdog (Cfg_NetOATFailDetDly, 600s) for exactly this, and there is
    # no peer-to-peer mechanism it could use instead -- it has no COV support and
    # never originates reads. So sharing stays server-to-device and needs no
    # thermostat-to-thermostat network access.
    #
    # 65535 is the device's "no value" sentinel, not a temperature.
    Point("oa_temp", "analog-output,16", False, "degF", None, "no_OaTemp -- resulting outdoor temp"),
    Point("oa_humidity", "analog-output,17", False, "%RH", None, "no_OaHumidity"),
    Point(
        "oa_temp_in", "analog-value,89", True, "degF", None,
        "ni_OutdoorTemp -- write here to share a sensor from another device",
    ),
    Point(
        "oa_humidity_in", "analog-value,194", True, "%RH", None,
        "ni_OutdoorHum -- companion to oa_temp_in",
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

# The TC500A reports 65535 for an analog point it has no value for. Treating it
# as a reading gives you a 65535 degree outdoor temperature and, worse, shares it
# with every other thermostat.
NO_VALUE = 65535.0


def is_valid(value) -> bool:
    return isinstance(value, (int, float)) and abs(float(value) - NO_VALUE) > 0.5


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
