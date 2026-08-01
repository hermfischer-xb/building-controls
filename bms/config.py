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


def load(path: str | Path) -> Config:
    return Config.model_validate(yaml.safe_load(Path(path).read_text()))
