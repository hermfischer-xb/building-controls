"""Driver for an Interlogix TruPortal access control panel.

Interlogix closed at the end of 2020, so there is no vendor documentation and no
support. Everything here was established by reading the appliance's own AngularJS
client and its WSDL, then testing against live hardware. The findings that shaped
this module:

* **It speaks SOAP, not REST.** The widely-referenced PSTruPortal PowerShell
  module targets `/api/...`, which this firmware answers with an nginx 404. The
  real endpoint is `AcsWebservices.wsdl`, namespace `http://tempuri.org/ns1.xsd`,
  publishing 202 operations.

* **There is no session.** Every operation carries `UserName` and `Password`
  inline, so there is nothing to keep alive or renew -- at the cost of sending
  credentials on every call, which is why this must only run over TLS or a
  trusted network.

* **`ExecuteSystemActionMap` ignores an action's trigger conditions.** Proven
  twice: trigger 7 fires with its input inactive, and trigger 12 fires with no
  trigger group at all. Conditions govern only automatic firing. That is what
  lets a map be configured so it can *never* self-fire while remaining callable
  here -- which matters, because a maintenance action wired to a schedule
  condition will switch a building's lights on by itself every night.

* **Durations live on the device.** `CmdOutputPulseOn` carries a `TimeSpan`, and
  the panel holds the timer. Nothing here schedules an "off" command, so a
  gateway that dies mid-pulse cannot leave a building lit -- the same property
  that makes the thermostat bypass safe to expose to tenants.

Two request shapes exist and the difference is not decorative: most operations
take flat `UserName`/`Password`, while the SystemActionMap family nests them in
`ClientCredentials`. Sending the wrong one returns a fault with no clue why.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import escape

import httpx

log = logging.getLogger(__name__)

SERVICE_PATH = "/AcsWebservices.wsdl"
NAMESPACE = "http://tempuri.org/ns1.xsd"

_ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    '<soap:Body><{op} xmlns="{ns}">{payload}</{op}></soap:Body>'
    "</soap:Envelope>"
)

# The panel's TimeSpan enum, e.g. Tms10m. Converted so the UI can say "10 minutes"
# using the device's own configuration rather than a number duplicated in code.
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class TruPortalError(Exception):
    """A call did not complete. The message is safe to show an operator."""


def parse_timespan(value: str) -> int | None:
    """'Tms10m' -> 600. Returns None for TmsNone or anything unrecognised."""
    m = re.fullmatch(r"Tms(\d+)([smhd])", value or "")
    return int(m.group(1)) * _UNITS[m.group(2)] if m else None


def describe_duration(seconds: int | None) -> str:
    """Largest sensible unit, correctly pluralised.

    This wording goes on a button a tenant reads, so "1 seconds" or a seven-day
    action described as "168 hours" both undermine trust in a control that opens
    doors and switches lights.
    """
    if not seconds:
        return ""
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second")):
        if seconds % size == 0 and seconds >= size:
            count = seconds // size
            return f"{count} {unit}" + ("s" if count != 1 else "")
    return f"{seconds} seconds"


@dataclass(frozen=True)
class Door:
    id: int
    name: str
    grant_seconds: int


@dataclass(frozen=True)
class Output:
    id: int
    name: str
    enabled: bool


@dataclass(frozen=True)
class Trigger:
    id: int
    name: str
    enabled: bool
    duration_seconds: int | None
    outputs: tuple[int, ...]
    # A map with no conditions cannot fire itself, which is what we want for
    # anything the application invokes. Surfaced so the UI can warn if someone
    # later adds a condition that would make it run on its own.
    self_firing: bool

    @property
    def duration_text(self) -> str:
        return describe_duration(self.duration_seconds)


@dataclass
class LiveStatus:
    doors: dict[int, dict[str, Any]] = field(default_factory=dict)
    outputs: dict[int, int] = field(default_factory=dict)
    inputs: dict[int, int] = field(default_factory=dict)
    at: float = 0.0


class TruPortal:
    """Async client for one panel.

    Inventory (doors, outputs, action maps) is cached because it changes only
    when someone edits the panel; live status is read on demand. Nothing here
    writes configuration -- this drives the panel, it does not administer it.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify_tls: bool = False,
        timeout: float = 20.0,
        inventory_ttl: float = 300.0,
    ) -> None:
        base = host if host.startswith("http") else f"https://{host}"
        self._base = base
        self._user = username
        self._password = password
        # The appliance carries a self-signed certificate and, with the vendor
        # gone, there is no route to a trusted one.
        self._verify = verify_tls
        self._timeout = timeout
        self._inventory_ttl = inventory_ttl
        self._client: httpx.AsyncClient | None = None
        # One panel, one HTTP conversation. Serialised so a burst of tenant
        # requests cannot interleave mid-call.
        self._lock = asyncio.Lock()
        self._inventory: dict[str, Any] = {}
        self._inventory_at = 0.0

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._base, verify=self._verify, timeout=self._timeout
        )

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # --- transport --------------------------------------------------------------

    async def _call(self, operation: str, payload: str, action: str | None = None) -> str:
        if self._client is None:
            raise TruPortalError("client not started")
        body = _ENVELOPE.format(op=operation, ns=NAMESPACE, payload=payload)
        async with self._lock:
            try:
                res = await self._client.post(
                    SERVICE_PATH,
                    content=body.encode(),
                    headers={
                        "Content-Type": "text/xml; charset=utf-8",
                        "SOAPAction": action or operation,
                    },
                )
            except Exception as err:  # noqa: BLE001
                raise TruPortalError(f"{type(err).__name__}: {err}") from err

        text = res.text
        if res.status_code != 200:
            raise TruPortalError(f"HTTP {res.status_code}")
        if "<SOAP-ENV:Fault" in text or "<soap:Fault" in text:
            reason = re.search(r"<faultstring>([^<]*)</faultstring>", text)
            raise TruPortalError(reason.group(1) if reason else "SOAP fault")
        return text

    def _flat_credentials(self) -> str:
        """The shape most operations expect."""
        return (
            f"<UserName>{escape(self._user)}</UserName>"
            f"<Password>{escape(self._password)}</Password>"
        )

    def _nested_credentials(self) -> str:
        """The shape the SystemActionMap family expects instead."""
        return (
            "<ClientCredentials>"
            f"<User>{escape(self._user)}</User>"
            f"<Password>{escape(self._password)}</Password>"
            "</ClientCredentials>"
        )

    # --- inventory --------------------------------------------------------------

    async def inventory(self, refresh: bool = False) -> dict[str, Any]:
        if not refresh and self._inventory and (
            time.time() - self._inventory_at < self._inventory_ttl
        ):
            return self._inventory

        doors = await self._read_doors()
        outputs = await self._read_outputs()
        triggers = await self._read_triggers()
        self._inventory = {"doors": doors, "outputs": outputs, "triggers": triggers}
        self._inventory_at = time.time()
        return self._inventory

    async def _read_doors(self) -> list[Door]:
        payload = (
            self._flat_credentials()
            + "<count>100</count><offset>0</offset><revision>0</revision>"
        )
        text = " ".join((await self._call("GetAccessPoints", payload)).split())
        doors = []
        for blk in re.findall(r"<item><device>.*?(?=<item><device>|</items>|$)", text):
            dev_id = re.search(r"<devID>(\d+)</devID>", blk)
            name = re.search(r"<devName>([^<]*)</devName>", blk)
            grant = re.search(r"<grantAccessTime>(\d+)</grantAccessTime>", blk)
            if dev_id and name:
                doors.append(
                    Door(int(dev_id.group(1)), html.unescape(name.group(1)),
                         int(grant.group(1)) if grant else 5)
                )
        return doors

    async def _read_outputs(self) -> list[Output]:
        payload = (
            self._flat_credentials()
            + "<count>100</count><offset>0</offset><revision>0</revision>"
        )
        text = " ".join((await self._call("GetOutputs", payload)).split())
        outputs = []
        for blk in re.findall(r"<item><device>.*?(?=<item><device>|</items>|$)", text):
            dev_id = re.search(r"<devID>(\d+)</devID>", blk)
            name = re.search(r"<devName>([^<]*)</devName>", blk)
            disabled = re.search(r"<disable>(\d+)</disable>", blk)
            if dev_id and name:
                outputs.append(
                    Output(int(dev_id.group(1)), html.unescape(name.group(1)),
                           not (disabled and disabled.group(1) == "1"))
                )
        return outputs

    async def _read_triggers(self) -> list[Trigger]:
        payload = (
            "<GetSystemActionMapsRequest>"
            + self._nested_credentials()
            + "<RecordSelector><Count>100</Count><Offset>0</Offset>"
            "<Revision>0</Revision></RecordSelector>"
            "</GetSystemActionMapsRequest>"
        )
        text = " ".join((await self._call("GetSystemActionMaps", payload)).split())
        triggers = []
        for blk in re.findall(r"<SystemActionMap>(.*?)</SystemActionMap>", text):
            tid = re.search(r"<id>(\d+)</id>", blk)
            name = re.search(r"<Name>([^<]*)</Name>", blk)
            if not (tid and name):
                continue
            enabled = re.search(r"<Enabled>(\w+)</Enabled>", blk)
            spans = [parse_timespan(s) for s in re.findall(r"<TimeSpan>(\w+)</TimeSpan>", blk)]
            pulses = [
                int(m.group(1))
                for m in re.finditer(
                    r"<CommandType>CmdOutputPulseOn</CommandType><EntityID>(\d+)</EntityID>", blk
                )
            ]
            # Longest span wins: a map may mix a 1-second command with the real
            # pulse, and the pulse is the duration a person cares about.
            duration = max((s for s in spans if s), default=None)
            triggers.append(
                Trigger(
                    id=int(tid.group(1)),
                    name=html.unescape(name.group(1)),
                    enabled=bool(enabled and enabled.group(1) == "true"),
                    duration_seconds=duration,
                    outputs=tuple(pulses),
                    self_firing=bool(re.search(r"<QueryType>\w+</QueryType>", blk)),
                )
            )
        return triggers

    # --- live status ------------------------------------------------------------

    async def status(self) -> LiveStatus:
        text = await self._call("GetHardwareStatusAsJSON", self._flat_credentials())

        def section(tag: str) -> dict:
            m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
            if not m:
                return {}
            try:
                return json.loads(html.unescape(m.group(1)))
            except Exception:  # noqa: BLE001
                return {}

        doors = section("doorStatuses")
        return LiveStatus(
            doors={int(k): v for k, v in doors.items()},
            outputs={int(k): v.get("outputStatus", 0)
                     for k, v in section("outputStatuses").items()},
            inputs={int(k): v.get("inputStatus", 0)
                    for k, v in section("inputStatuses").items()},
            at=time.time(),
        )

    # --- commands ---------------------------------------------------------------

    async def grant_access(self, door_id: int) -> None:
        """Momentary unlock -- the same action a valid card presentation causes.

        The duration is the panel's own `grantAccessTime` for that door (5s on
        most, 30s on the garage gate). Deliberately not overridden: the door
        relocks itself whatever happens to this process, and a caller that could
        choose the duration could choose a long one.
        """
        payload = self._flat_credentials() + f"<doorID>{int(door_id)}</doorID>"
        await self._call("DoorGrantAccess", payload)

    async def execute_trigger(self, trigger_id: int) -> None:
        """Run an action map now, regardless of its trigger conditions.

        Conditions govern automatic firing only, so a map configured never to
        fire on its own is still callable here. That is the intended
        arrangement for anything this application invokes.
        """
        payload = (
            "<ExecuteSystemActionMapRequest>"
            + self._nested_credentials()
            + f"<id>{int(trigger_id)}</id><Edge>true</Edge>"
            "</ExecuteSystemActionMapRequest>"
        )
        await self._call("ExecuteSystemActionMap", payload)
