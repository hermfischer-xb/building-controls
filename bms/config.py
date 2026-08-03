"""Gateway configuration.

The device inventory is config, not discovery. Who-Is is useful for commissioning
but a production poll loop should know exactly what it expects to find, so a
thermostat that drops off the network is an alarm rather than a silently shorter
device list.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


class DeviceConfig(BaseModel):
    device_id: int = Field(description="BACnet device object instance, unique per unit")
    address: str = Field(description="IP or IP:port of the thermostat")
    name: str = Field(description="human label, e.g. 'Room 301'")
    zone: str = Field(default="default", description="groups devices for tenant permissions")
    mac: str | None = Field(
        default=None,
        description="Wi-Fi MAC, recorded so the DHCP reservation can be rebuilt from "
        "this file rather than from the router's UI",
    )

    @field_validator("device_id")
    @classmethod
    def _sane_device_id(cls, v: int) -> int:
        if not 0 <= v <= 4194303:
            raise ValueError(f"device_id {v} outside BACnet range 0-4194303")
        if v >= 4194302:
            raise ValueError(
                f"device_id {v} is the unconfigured default -- set a unique ID on the "
                "thermostat under Config > Connection > BACnet IP"
            )
        return v


class BacnetConfig(BaseModel):
    # Always pin the interface. A host with two NICs on the same subnet will
    # otherwise bind ambiguously and Who-Is can leave by the wrong one.
    address: str = Field(description="local interface with prefix, e.g. 192.168.1.10/24")
    device_id: int = Field(default=4000000, description="this gateway's own BACnet device id")
    name: str = Field(default="bms-gateway")
    foreign_bbmd: str | None = Field(
        default=None, description="BBMD address if the gateway is not on the thermostats' subnet"
    )
    foreign_ttl: int = Field(default=30)


class OutdoorWeatherConfig(BaseModel):
    """Public weather as an outdoor-temperature source.

    Selecting BACnet/IP disables the thermostat's own internet path, and an
    isolated VLAN removes it entirely, so its built-in zip-code outdoor
    temperature is unavailable. The gateway has internet and can supply the same
    value through the network input a physical sensor would use.
    """

    enabled: bool = Field(default=False)
    zip_code: str | None = Field(default=None, description="e.g. '91436'")
    country: str = Field(default="us", description="ISO country code for postcode lookup")
    latitude: float | None = Field(default=None, description="use instead of a postcode")
    longitude: float | None = Field(default=None)


class TruPortalDoorConfig(BaseModel):
    id: int = Field(description="devID from the panel, not the door's position")
    name: str = Field(description="what a tenant should see, e.g. 'Front entrance'")
    zones: list[str] = Field(
        default_factory=lambda: ["*"],
        description="zones whose tenants may unlock it. '*' means any tenant, which is "
        "correct for a shared entrance and wrong for a door into one suite. An empty "
        "list means no tenant may -- managers and admins are never zone-scoped, so the "
        "door stays available to them.",
    )


class TruPortalTriggerConfig(BaseModel):
    id: int = Field(description="action map id from the panel")
    zone: str = Field(default="*", description="zone this lights, or '*' for the building")
    role: str = Field(
        default="manager",
        description="lowest role that may fire it: tenant | manager | admin. Mapped "
        "explicitly rather than inferred from the action's name, so renaming an action "
        "in TruPortal cannot quietly hand tenants a six-hour trigger.",
    )


class TruPortalConfig(BaseModel):
    """An Interlogix TruPortal panel. Absent or disabled, none of it is exposed."""

    enabled: bool = Field(default=False)
    host: str = Field(default="", description="IP or hostname; https is assumed")
    username: str = Field(default="")
    password: str = Field(default="")
    verify_tls: bool = Field(
        default=False,
        description="the panel carries a self-signed certificate and the vendor closed in "
        "2020, so there is no path to a trusted one",
    )
    doors: list[TruPortalDoorConfig] = Field(default_factory=list)
    lighting_triggers: list[TruPortalTriggerConfig] = Field(default_factory=list)


class Config(BaseModel):
    bacnet: BacnetConfig
    devices: list[DeviceConfig]
    poll_interval_seconds: float = Field(default=10.0, ge=1.0)
    request_timeout_seconds: float = Field(default=5.0)
    api_host: str = Field(
        default="127.0.0.1",
        description="bind address. Never 0.0.0.0 on a host with a public interface.",
    )
    api_port: int = Field(default=8080)
    secure_cookies: bool = Field(
        default=False,
        description="set true once served over HTTPS; keeps the session cookie off plain HTTP. "
        "Setting it while still on plain HTTP silently breaks login -- the browser accepts "
        "the cookie and then never sends it back.",
    )
    behind_proxy: bool = Field(
        default=False,
        description="true when a reverse proxy or tunnel terminates TLS in front of this app. "
        "Only then is the client-IP header trusted -- believing it unconditionally would let "
        "anyone forge an address and walk past the login throttle.",
    )
    client_ip_header: str = Field(
        default="cf-connecting-ip",
        description="header carrying the real client address. 'cf-connecting-ip' for Cloudflare "
        "Tunnel, 'x-forwarded-for' for Caddy or nginx. Ignored unless behind_proxy is true.",
    )
    db_path: str = Field(default="data/bms.db")
    time_sync_enabled: bool = Field(
        default=True,
        description="push this host's local wall-clock time to the thermostats. Their only "
        "other time source is the Honeywell cloud, which an isolated VLAN cannot reach.",
    )
    max_clock_drift_seconds: float = Field(
        default=30.0, ge=1.0, description="resync once a device clock is this far out"
    )
    reconcile_interval_seconds: float = Field(default=300.0, ge=10.0)
    public_origin: str = Field(
        default="",
        description="the exact https origin users reach this on, e.g. "
        "'https://controls.16400ventura.com'. WebAuthn binds credentials to it, so a "
        "mismatch makes every passkey fail. Empty disables passkeys entirely, which is "
        "correct on plain HTTP -- browsers refuse the API outside a secure context.",
    )
    truportal: TruPortalConfig = Field(default_factory=TruPortalConfig)
    outdoor_weather: OutdoorWeatherConfig = Field(default_factory=OutdoorWeatherConfig)
    outdoor_sensor_device_id: int | None = Field(
        default=None,
        description="device with a physical outdoor sensor. Its reading is shared to every "
        "other device by the gateway, because the thermostats cannot exchange it themselves. "
        "Leave null if no sensor is fitted.",
    )

    @field_validator("devices")
    @classmethod
    def _unique(cls, devices: list[DeviceConfig]) -> list[DeviceConfig]:
        for field in ("device_id", "address"):
            seen: dict[object, str] = {}
            for d in devices:
                value = getattr(d, field)
                if value in seen:
                    raise ValueError(
                        f"duplicate {field} {value!r}: {seen[value]!r} and {d.name!r}"
                    )
                seen[value] = d.name
        return devices


def check_permissions(path: str | Path) -> str | None:
    """Warn if the config is readable by anyone but its owner.

    This file holds the access panel's credentials, which the daemon must be
    able to read on every call -- so they cannot be hashed, and any scheme that
    lets the daemon recover them lets anyone with the same access recover them
    too. Filesystem permissions are therefore the actual protection, not a
    formality, and `/Users/Shared` is world-readable by default.

    Returns a message to log, or None when the file is already restricted.
    """
    p = Path(path)
    try:
        mode = p.stat().st_mode & 0o777
    except OSError:
        return None
    if mode & 0o077:
        return (
            f"{p} is mode {mode:03o} and holds credentials in clear text; "
            f"restrict it with: chmod 600 {p}"
        )
    return None


def load(path: str | Path) -> Config:
    return Config.model_validate(yaml.safe_load(Path(path).read_text()))
