"""HTTP surface over the cache.

Reads are served from the poll cache and never touch the wire, so the API stays
fast and a slow or offline thermostat cannot stall a page load. Writes go straight
through, because a write the user cannot see land is worse than a slow one.

There is deliberately no authentication here yet -- this is the gateway, bound to
loopback. Auth, roles and the tenant request workflow belong in the application
layer in front of it.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .auth import COOKIE_NAME, SESSION_TTL_SECONDS, AuthStore, LoginThrottle, User
from .bacnet import BacnetClient, DeviceUnreachable
from .cache import Cache
from .config import Config
from .holidays import US_FEDERAL_DEFAULTS, describe, occurrences
from .points import BY_KEY, POINTS, SETPOINT_LIMITS, OccupancyState
from .poller import Poller
from .schedules import DAYS, DEFAULT_GROUPS, resolve_week, validate_transitions, week_summary
from .reconciler import Reconciler
from .store import Store
from .tenant_page import render as render_tenant
from .tenant_page import render_login

log = logging.getLogger(__name__)


class WriteRequest(BaseModel):
    value: float | int | bool = Field(description="value to write")


class OverrideRequest(BaseModel):
    state: str = Field(description="one of: OCCUPIED, UNOCCUPIED, BYPASS, STANDBY, NO_OVERRIDE")


class BypassRequest(BaseModel):
    minutes: int = Field(ge=0, le=1080, description="device caps bypass at 1080 minutes")


class DayRequest(BaseModel):
    transitions: list[dict] = Field(
        description='e.g. [{"time":"06:00","state":0},{"time":"18:00","state":1}]'
    )


class ExceptionRequest(BaseModel):
    name: str
    start_date: str = Field(description="ISO yyyy-mm-dd")
    end_date: str | None = Field(default=None, description="null means a single day")
    transitions: list[dict]
    scope: str = Field(default="global", description="device | zone | global")
    scope_ref: str = Field(default="*")


class UserRequest(BaseModel):
    username: str
    password: str = Field(min_length=8)
    role: str = Field(description="admin | manager | tenant")
    display_name: str = ""
    zones: list[str] = Field(default_factory=list,
                             description="zones a tenant may act on; ignored for manager/admin")


class PasswordRequest(BaseModel):
    password: str = Field(min_length=8)


class ZonesRequest(BaseModel):
    zones: list[str]


class HolidayRequest(BaseModel):
    name: str
    rule_type: str = Field(description="fixed | range | floating")
    year: int | None = Field(default=None, description="null means every year")
    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = Field(default=None, ge=1, le=31)
    end_month: int | None = Field(default=None, ge=1, le=12)
    end_day: int | None = Field(default=None, ge=1, le=31)
    week_of_month: int | None = Field(default=None, ge=1, le=5, description="5 means last")
    day_of_week: int | None = Field(default=None, ge=1, le=7, description="1=Mon..7=Sun")
    state: int = Field(default=1, description="0=Occupied, 1=Unoccupied, 3=Standby")
    zone: str = Field(default="*", description="'*' applies to every zone")


def create_app(cfg: Config, db_path: str = "data/bms.db") -> FastAPI:
    client = BacnetClient(cfg.bacnet, cfg.request_timeout_seconds)
    cache = Cache(stale_after=cfg.poll_interval_seconds * 3)
    poller = Poller(cfg, client, cache)
    store = Store(db_path)
    reconciler = Reconciler(cfg, client, store)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await client.start()
        await poller.start()
        await reconciler.start(cfg.reconcile_interval_seconds)
        try:
            yield
        finally:
            await reconciler.stop()
            await poller.stop()
            await client.stop()
            store.close()

    app = FastAPI(title="BMS Gateway", version="0.1.0", lifespan=lifespan)
    auth = AuthStore(store)
    throttle = LoginThrottle()

    def _device(device_id: int):
        for d in cfg.devices:
            if d.device_id == device_id:
                return d
        raise HTTPException(404, f"no device {device_id} in inventory")

    # --- authentication ---------------------------------------------------------

    def current_user(request: Request) -> User:
        token = request.cookies.get(COOKIE_NAME)
        user = auth.resolve_session(token) if token else None
        if user is None:
            raise HTTPException(401, "sign in required")
        return user

    def require(role: str):
        """Dependency factory: caller must hold `role` or higher."""

        def dependency(user: User = Depends(current_user)) -> User:
            if not user.at_least(role):
                raise HTTPException(403, f"{role} role required")
            return user

        return dependency

    def require_device(device_id: int, user: User) -> Any:
        """Resolve a device and confirm this user is allowed to touch it."""
        device = _device(device_id)
        if not user.may_access_zone(device.zone):
            # 404 rather than 403: a tenant has no business learning which other
            # device ids exist.
            raise HTTPException(404, f"no device {device_id} in inventory")
        return device

    def _same_origin(request: Request) -> bool:
        """Reject cross-site state changes.

        SameSite=Lax already blocks the cross-site POST case in current browsers;
        this is the belt to that braces, and costs nothing.
        """
        origin = request.headers.get("origin")
        if origin is None:
            return True  # non-browser client (curl, scripts)
        return origin.rstrip("/") == str(request.base_url).rstrip("/")

    @app.middleware("http")
    async def guard_mutations(request: Request, call_next):
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and not _same_origin(request):
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_form(next: str = "/") -> str:
        return render_login(next_url=next)

    @app.post("/login", include_in_schema=False)
    async def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        next: str = Form("/"),
    ):
        client = request.client.host if request.client else "unknown"
        if throttle.blocked(username, client):
            store.log(username, "login.throttled", client, outcome="error")
            return HTMLResponse(
                render_login("Too many attempts. Wait a few minutes and try again.", next),
                status_code=429,
            )

        user = auth.authenticate(username, password)
        if user is None:
            throttle.record_failure(username, client)
            store.log(username, "login.failed", client, outcome="error")
            # Never say which half was wrong.
            return HTMLResponse(
                render_login("Incorrect username or password.", next), status_code=401
            )

        throttle.clear(username, client)
        token = auth.create_session(user, request.headers.get("user-agent"))
        store.log(user.username, "login.ok", client)

        # Only allow relative redirects, or this becomes an open redirect.
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            COOKIE_NAME, token,
            httponly=True, samesite="lax", secure=cfg.secure_cookies,
            max_age=SESSION_TTL_SECONDS, path="/",
        )
        return response

    @app.post("/logout", include_in_schema=False)
    async def logout(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if token:
            auth.revoke_session(token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/me")
    async def me(user: User = Depends(current_user)) -> dict[str, Any]:
        return user.to_dict()

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> str:
        """Orientation page.

        This is the gateway, not the building UI -- there is no operator interface
        yet. Landing on a bare 404 gives no clue what is running or where to look,
        so point at the interactive docs and the endpoints that exist.
        """
        def cell(value: Any, suffix: str = "", places: int = 1) -> str:
            if value is None:
                return "—"
            if isinstance(value, (int, float)):
                return f"{value:.{places}f}{suffix}"
            return str(value)

        rows = "".join(
            "<tr>"
            f"<td>{d.name}</td><td>{d.device_id}</td><td>{d.address}</td>"
            f"<td class='{'ok' if d.online else 'bad'}'>{'online' if d.online else 'OFFLINE'}</td>"
            f"<td>{cell(d.age_seconds(), 's ago', 0)}</td>"
            f"<td>{cell(d.values.get('space_temp'), ' °F')}</td>"
            f"<td>{cell(d.values.get('effective_heat_sp'), '', 0)}"
            f" / {cell(d.values.get('effective_cool_sp'), '', 0)}</td>"
            "</tr>"
            for d in cache.all()
        )
        return f"""<!doctype html>
<meta charset="utf-8"><title>BMS Gateway</title>
<style>
 body{{font:14px/1.5 -apple-system,BlinkMacSystemFont,sans-serif;margin:2rem auto;max-width:52rem;padding:0 1rem}}
 code{{background:#8881;padding:.1em .35em;border-radius:3px}}
 table{{border-collapse:collapse;width:100%;margin:1rem 0}}
 th,td{{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #8883}}
 .ok{{color:#137333}} .bad{{color:#c5221f;font-weight:600}}
 a{{color:#1a73e8}}
 @media(prefers-color-scheme:dark){{body{{background:#111;color:#eee}} .ok{{color:#81c995}} .bad{{color:#f28b82}} a{{color:#8ab4f8}}}}
</style>
<h1>BMS Gateway</h1>
<p>BACnet/IP gateway for {len(cfg.devices)} TC500A thermostat(s).
   This is the machine interface — there is no operator UI yet.</p>
<p><strong><a href="/docs">→ Interactive API docs (/docs)</a></strong></p>
<table>
 <tr><th>Name</th><th>Device</th><th>Address</th><th>State</th><th>Polled</th>
     <th>Space temp</th><th>Eff. heat/cool</th></tr>
 {rows}
</table>
<h3>Endpoints</h3>
<ul>
 <li><code>GET /devices</code> — live state of every thermostat</li>
 <li><code>GET /devices/{{id}}/schedule</code> — weekly schedule</li>
 <li><code>POST /devices/{{id}}/override</code> — force occupancy</li>
 <li><code>POST /devices/{{id}}/bypass</code> — timed occupancy bypass</li>
 <li><code>GET /holidays</code> — holiday rules</li>
 <li><code>POST /reconcile</code> — push intent to devices now</li>
 <li><code>GET /audit</code> — change log</li>
 <li><code>GET /health</code> — poll status</li>
</ul>
"""

    @app.get("/t/{device_id}", include_in_schema=False)
    async def tenant(request: Request, device_id: int):
        """Mobile bypass page — the one screen a tenant ever needs.

        Deliberately a separate, tiny page rather than a route in an operator UI:
        the audience, the device and the interaction are all different. Sends an
        unauthenticated visitor to the login form rather than a bare 401, because
        this is a page a person opens from a bookmark, not an API call.
        """
        token = request.cookies.get(COOKIE_NAME)
        user = auth.resolve_session(token) if token else None
        if user is None:
            return RedirectResponse(f"/login?next=/t/{device_id}", status_code=303)

        device = require_device(device_id, user)
        state = cache.get(device_id)
        return HTMLResponse(render_tenant(
            device_id,
            device.name,
            state.to_dict(cfg.poll_interval_seconds * 3) if state else {},
        ))

    @app.get("/health")
    async def health() -> dict[str, Any]:
        devices = cache.all()
        online = sum(1 for d in devices if d.online)
        return {
            "status": "ok" if online == len(devices) else "degraded",
            "devices_total": len(devices),
            "devices_online": online,
            "poll_interval_seconds": cfg.poll_interval_seconds,
        }

    @app.get("/points")
    async def points(user: User = Depends(current_user)) -> list[dict[str, Any]]:
        """The point map, so a UI can render without hardcoding instance numbers."""
        return [
            {
                "key": p.key,
                "objid": p.objid,
                "writable": p.writable,
                "units": p.units,
                "description": p.description,
                "enum": {e.name: e.value for e in p.enum} if p.enum else None,
                "limits": SETPOINT_LIMITS.get(p.key),
            }
            for p in POINTS
        ]

    @app.get("/devices")
    async def devices(user: User = Depends(current_user)) -> list[dict[str, Any]]:
        # Tenants only ever see their own zones.
        return [d for d in cache.to_dict() if user.may_access_zone(d["zone"])]

    @app.get("/devices/{device_id}")
    async def device(device_id: int, user: User = Depends(current_user)) -> dict[str, Any]:
        require_device(device_id, user)
        state = cache.get(device_id)
        if state is None:
            raise HTTPException(404, f"no device {device_id}")
        return state.to_dict(cfg.poll_interval_seconds * 3)

    @app.post("/devices/{device_id}/points/{key}")
    async def write_point(device_id: int, key: str, body: WriteRequest,
                          user: User = Depends(require("manager"))) -> dict[str, Any]:
        device = require_device(device_id, user)
        point = BY_KEY.get(key)
        if point is None:
            raise HTTPException(404, f"unknown point {key!r}")
        if not point.writable:
            raise HTTPException(400, f"{key} is read-only")

        value: Any = body.value
        # Clamp rather than reject: a tenant nudging a setpoint should not get an
        # error page, and unbounded setpoints are how pipes freeze.
        if key in SETPOINT_LIMITS:
            lo, hi = SETPOINT_LIMITS[key]
            clamped = min(hi, max(lo, float(value)))
            if clamped != float(value):
                log.info("clamped %s %s -> %s for %s", key, value, clamped, device.name)
            value = clamped

        try:
            await client.write_point(device, point, value)
        except DeviceUnreachable as err:
            # The device sometimes applies a value and then fails to acknowledge
            # it, so do not claim the write was rejected. Say the outcome is
            # unknown and let the next poll report what actually happened.
            raise HTTPException(
                503,
                {
                    "error": str(err),
                    "outcome": "unknown",
                    "detail": "the write may or may not have been applied; "
                    "poll state to confirm",
                },
            ) from err

        cache.apply_local_write(device_id, key, value)
        return {"device_id": device_id, "point": key, "written": value}

    @app.post("/devices/{device_id}/override")
    async def override(device_id: int, body: OverrideRequest,
                       user: User = Depends(require("manager"))) -> dict[str, Any]:
        """Force occupancy. Set NO_OVERRIDE to hand control back to the schedule.

        Manager-only: unlike bypass this does not expire, so a stray tap would
        leave a floor conditioned indefinitely.
        """
        device = require_device(device_id, user)
        try:
            state = OccupancyState[body.state.upper()]
        except KeyError:
            raise HTTPException(
                400, f"state must be one of {[e.name for e in OccupancyState]}"
            ) from None

        try:
            await client.write_point(device, BY_KEY["occupancy_override"], int(state))
        except DeviceUnreachable as err:
            raise HTTPException(503, str(err)) from err

        cache.apply_local_write(device_id, "occupancy_override", int(state))
        return {"device_id": device_id, "override": state.name}

    @app.post("/devices/{device_id}/bypass")
    async def bypass(device_id: int, body: BypassRequest,
                     user: User = Depends(current_user)) -> dict[str, Any]:
        """Timed occupancy bypass -- the 'I'm here now' path for a tenant.

        Order matters: write the duration before enabling, or the device starts a
        timer with whatever value was there previously.

        Open to tenants because it expires by itself: the worst a stray tap costs
        is a few hours of conditioning in a room they already have access to.
        """
        device = require_device(device_id, user)
        try:
            await client.write_point(device, BY_KEY["bypass_minutes"], float(body.minutes))
            await client.write_point(
                device, BY_KEY["bypass_enable"], 1 if body.minutes > 0 else 0
            )
        except DeviceUnreachable as err:
            raise HTTPException(503, str(err)) from err

        cache.apply_local_write(device_id, "bypass_minutes", float(body.minutes))
        cache.apply_local_write(device_id, "bypass_enable", 1 if body.minutes > 0 else 0)
        return {"device_id": device_id, "bypass_minutes": body.minutes}

    @app.get("/devices/{device_id}/schedule")
    async def schedule(device_id: int, user: User = Depends(current_user)) -> dict[str, Any]:
        device = require_device(device_id, user)
        try:
            weekly = await client.read_weekly_schedule(device, "schedule,2")
        except Exception as err:  # noqa: BLE001
            raise HTTPException(503, str(err)) from err

        days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        return {
            "device_id": device_id,
            "weekly": {
                day: [
                    {
                        "time": f"{tv.time[0]:02d}:{tv.time[1]:02d}",
                        "state": int(tv.value.get_value()),
                    }
                    for tv in (entry.daySchedule or [])
                ]
                for day, entry in zip(days, weekly)
            },
        }

    @app.get("/devices/{device_id}/calendars")
    async def calendars(device_id: int,
                        user: User = Depends(require("manager"))) -> dict[str, Any]:
        """Holiday lists. Chapter 10 of the vendor guide claims these are
        unsupported; they work, including floating weekNDay rules."""
        device = _device(device_id)
        out = {}
        for i in range(3, 13):
            objid = f"calendar,{i}"
            try:
                out[objid] = await client.read_calendar(device, objid)
            except Exception as err:  # noqa: BLE001
                out[objid] = {"error": str(err)}
        return {"device_id": device_id, "calendars": out}

    # --- intent: holidays -------------------------------------------------------

    @app.get("/holidays")
    async def list_holidays(zone: str | None = None, year: int | None = None,
                            user: User = Depends(current_user)) -> list[dict]:
        from datetime import date

        year = year or date.today().year
        return [
            {
                **h.to_dict(),
                "description": describe(h, year),
                "dates": [str(d) for d in occurrences(h, year)],
            }
            for h in store.holidays(zone=zone, enabled_only=False)
        ]

    @app.post("/holidays", status_code=201)
    async def add_holiday(body: HolidayRequest,
                          user: User = Depends(require("manager"))) -> dict[str, Any]:
        if body.rule_type not in ("fixed", "range", "floating"):
            raise HTTPException(400, "rule_type must be fixed, range or floating")
        holiday_id = store.add_holiday(actor="api", **body.model_dump())
        return {"id": holiday_id, "reconcile_required": True}

    @app.delete("/holidays/{holiday_id}")
    async def delete_holiday(holiday_id: int,
                             user: User = Depends(require("manager"))) -> dict[str, Any]:
        if not store.delete_holiday(holiday_id, actor="api"):
            raise HTTPException(404, f"no holiday {holiday_id}")
        return {"deleted": holiday_id, "reconcile_required": True}

    @app.post("/holidays/seed-us-federal", status_code=201)
    async def seed_us_federal(user: User = Depends(require("manager"))) -> dict[str, Any]:
        """Load a starting set of US federal holidays as reusable rules."""
        existing = {h.name for h in store.holidays(enabled_only=False)}
        added = [
            store.add_holiday(actor="api", **spec)
            for spec in US_FEDERAL_DEFAULTS
            if spec["name"] not in existing
        ]
        return {"added": len(added), "skipped": len(US_FEDERAL_DEFAULTS) - len(added)}

    # --- intent: setpoints ------------------------------------------------------

    @app.put("/intent/setpoints/{scope}/{scope_ref}/{point_key}")
    async def set_setpoint_intent(
        scope: str, scope_ref: str, point_key: str, body: WriteRequest,
        user: User = Depends(require("manager")),
    ) -> dict[str, Any]:
        if scope not in ("device", "zone", "global"):
            raise HTTPException(400, "scope must be device, zone or global")
        if point_key not in SETPOINT_LIMITS:
            raise HTTPException(400, f"{point_key} is not a settable setpoint")
        store.set_setpoint(scope, scope_ref, point_key, float(body.value), actor="api")
        return {"scope": scope, "ref": scope_ref, "point": point_key, "value": body.value}

    # --- reconcile --------------------------------------------------------------

    @app.get("/reconcile")
    async def reconcile_status(user: User = Depends(require("manager"))) -> dict[str, Any]:
        return reconciler.status

    @app.post("/reconcile")
    async def reconcile_now(user: User = Depends(require("manager"))) -> dict[str, Any]:
        """Push intent to every device now instead of waiting for the next cycle."""
        results = await reconciler.reconcile_all(actor="api")
        return {
            "devices": [r.to_dict() for r in results],
            "ok": all(r.ok for r in results),
        }

    @app.get("/audit")
    async def audit(limit: int = 100,
                    user: User = Depends(require("manager"))) -> list[dict[str, Any]]:
        return store.recent_audit(limit)

    # --- schedule groups --------------------------------------------------------

    @app.get("/groups")
    async def list_groups(user: User = Depends(require("manager"))) -> list[dict[str, Any]]:
        return [{**g, "summary": week_summary(g["week"])} for g in store.groups()]

    @app.post("/groups/seed-defaults", status_code=201)
    async def seed_groups(user: User = Depends(require("manager"))) -> dict[str, Any]:
        existing = {g["name"] for g in store.groups()}
        added = [
            store.add_group(spec["name"], spec["week"], spec["description"], actor="api")
            for spec in DEFAULT_GROUPS
            if spec["name"] not in existing
        ]
        return {"added": len(added), "skipped": len(DEFAULT_GROUPS) - len(added)}

    @app.put("/groups/{group_id}/days/{day_of_week}")
    async def set_group_day(
        group_id: int, day_of_week: int, body: DayRequest,
        user: User = Depends(require("manager")),
    ) -> dict[str, Any]:
        if not 1 <= day_of_week <= 7:
            raise HTTPException(400, "day_of_week must be 1 (Mon) to 7 (Sun)")
        try:
            transitions = validate_transitions(body.transitions)
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        store.set_group_day(group_id, day_of_week, transitions, actor="api")
        return {"group_id": group_id, "day": day_of_week, "transitions": transitions}

    @app.put("/devices/{device_id}/schedule-group/{group_id}")
    async def assign_group(device_id: int, group_id: int,
                           user: User = Depends(require("manager"))) -> dict[str, Any]:
        _device(device_id)
        store.assign_group(device_id, group_id, actor="api")
        return {"device_id": device_id, "group_id": group_id, "reconcile_required": True}

    @app.put("/devices/{device_id}/schedule-override/{day_of_week}")
    async def set_override(device_id: int, day_of_week: int, body: DayRequest,
                           user: User = Depends(require("manager"))) -> dict[str, Any]:
        _device(device_id)
        if not 1 <= day_of_week <= 7:
            raise HTTPException(400, "day_of_week must be 1 (Mon) to 7 (Sun)")
        try:
            transitions = validate_transitions(body.transitions)
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        store.set_day_override(device_id, day_of_week, transitions, actor="api")
        return {"device_id": device_id, "day": day_of_week, "reconcile_required": True}

    @app.delete("/devices/{device_id}/schedule-override/{day_of_week}")
    async def clear_override(device_id: int, day_of_week: int,
                             user: User = Depends(require("manager"))) -> dict[str, Any]:
        _device(device_id)
        if not store.clear_day_override(device_id, day_of_week, actor="api"):
            raise HTTPException(404, "no override for that day")
        return {"device_id": device_id, "day": day_of_week, "reconcile_required": True}

    @app.get("/devices/{device_id}/resolved-schedule")
    async def resolved_schedule(device_id: int,
                                user: User = Depends(current_user)) -> dict[str, Any]:
        """What this device *should* hold, before any reconcile."""
        device = require_device(device_id, user)
        group_id = store.group_for_device(device_id)
        if group_id is None:
            return {"device_id": device_id, "group_id": None,
                    "note": "no group assigned; device keeps its existing schedule"}
        overrides = store.day_overrides(device_id)
        week = resolve_week(store.group_week(group_id), overrides)
        return {
            "device_id": device_id,
            "group_id": group_id,
            "overridden_days": sorted(overrides),
            "summary": week_summary(week),
            "week": {DAYS[d - 1]: week[d] for d in range(1, 8)},
        }

    # --- dated one-off exceptions (manager) -------------------------------------

    @app.get("/exceptions")
    async def list_exceptions(upcoming_only: bool = False,
                              user: User = Depends(require("manager"))) -> list[dict[str, Any]]:
        return store.all_exceptions(upcoming_only=upcoming_only)

    @app.post("/exceptions", status_code=201)
    async def add_exception(body: ExceptionRequest,
                            user: User = Depends(require("manager"))) -> dict[str, Any]:
        try:
            transitions = validate_transitions(body.transitions)
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        exception_id = store.add_exception(
            name=body.name, start_date=body.start_date, transitions=transitions,
            end_date=body.end_date, scope=body.scope, scope_ref=body.scope_ref, actor="api",
        )
        return {"id": exception_id, "reconcile_required": True}

    @app.delete("/exceptions/{exception_id}")
    async def delete_exception(exception_id: int,
                               user: User = Depends(require("manager"))) -> dict[str, Any]:
        if not store.delete_exception(exception_id, actor="api"):
            raise HTTPException(404, f"no exception {exception_id}")
        return {"deleted": exception_id, "reconcile_required": True}

    # --- user administration (admin only) ---------------------------------------

    @app.get("/users")
    async def list_users(user: User = Depends(require("admin"))) -> list[dict[str, Any]]:
        return auth.users()

    @app.post("/users", status_code=201)
    async def create_user(
        body: UserRequest, user: User = Depends(require("admin"))
    ) -> dict[str, Any]:
        try:
            auth.create_user(
                body.username, body.password, body.role, body.display_name,
                body.zones, actor=user.username,
            )
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        except Exception as err:  # noqa: BLE001 - almost always a UNIQUE clash
            raise HTTPException(409, f"could not create user: {err}") from err
        return {"username": body.username, "role": body.role}

    @app.put("/users/{username}/password")
    async def set_password(
        username: str, body: PasswordRequest, user: User = Depends(require("admin"))
    ) -> dict[str, Any]:
        try:
            if not auth.set_password(username, body.password, actor=user.username):
                raise HTTPException(404, f"no user {username}")
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        return {"username": username, "sessions_revoked": True}

    @app.put("/users/{username}/zones")
    async def set_zones(
        username: str, body: ZonesRequest, user: User = Depends(require("admin"))
    ) -> dict[str, Any]:
        if not auth.set_zones(username, body.zones, actor=user.username):
            raise HTTPException(404, f"no user {username}")
        return {"username": username, "zones": body.zones}

    @app.delete("/users/{username}")
    async def deactivate_user(
        username: str, user: User = Depends(require("admin"))
    ) -> dict[str, Any]:
        if username == user.username:
            raise HTTPException(400, "cannot deactivate the account you are signed in as")
        if not auth.deactivate(username, actor=user.username):
            raise HTTPException(404, f"no user {username}")
        return {"username": username, "active": False}

    return app
