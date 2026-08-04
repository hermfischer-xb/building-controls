"""HTTP surface over the cache.

Reads are served from the poll cache and never touch the wire, so the API stays
fast and a slow or offline thermostat cannot stall a page load. Writes go straight
through, because a write the user cannot see land is worse than a slow one.

There is deliberately no authentication here yet -- this is the gateway, bound to
loopback. Auth, roles and the tenant request workflow belong in the application
layer in front of it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
)
from pydantic import BaseModel, Field

from .auth import (
    COOKIE_NAME, SESSION_TTL_SECONDS, AuthStore, LoginThrottle, User,
    generate_password,
)
from .bacnet import BacnetClient, DeviceUnreachable
from .cache import Cache
from .config import Config
from .holidays import US_FEDERAL_DEFAULTS, describe, occurrences
from .passkeys import VERIFICATION_WINDOW_SECONDS, PasskeyError, Passkeys
from .points import BY_KEY, POINTS, SETPOINT_LIMITS, OccupancyState
from .poller import Poller
from .schedules import DAYS, DEFAULT_GROUPS, resolve_week, validate_transitions, week_summary
from .reconciler import Reconciler
from .store import Store
from .truportal import TruPortal, TruPortalError
from .zones import Zones
from .tenant_page import render as render_tenant
from .tenant_page import render_login
from .ui.routes import build_router as build_ui_router

log = logging.getLogger(__name__)

# How long a page render may spend waiting on the access panel before drawing the
# buttons without its labels. Nothing a person is standing at a door for should
# wait on a device that is not answering.
ACCESS_UI_BUDGET = 3.0


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
    # No password field, deliberately. One is generated and returned once; see
    # POST /users. A caller-supplied password means the caller knows it.
    role: str = Field(description="admin | manager | tenant")
    display_name: str = ""
    zones: list[str] = Field(default_factory=list,
                             description="zones a tenant may act on; ignored for manager/admin")


class ChangePasswordRequest(BaseModel):
    current_password: str
    # The two-field confirmation is enforced in the browser, where a mismatch can
    # be shown against the field that is wrong. The server sees one value and
    # only cares that it is long enough.
    new_password: str = Field(min_length=8)


class ZonesRequest(BaseModel):
    zones: list[str]


class PasskeyRegisterRequest(BaseModel):
    credential: dict = Field(description="the browser's PublicKeyCredential, plus _challenge")
    label: str = Field(default="phone", max_length=60)


class PasskeyVerifyRequest(BaseModel):
    credential: dict


class ZoneRequest(BaseModel):
    zone: str = Field(min_length=1, description="zone name; free text, picked from existing")


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
    scope: str = Field(default="global", description="global | zone | device")
    scope_ref: str = Field(default="*", description="zone name, or device id, for the above")


def create_app(cfg: Config, db_path: str = "data/bms.db") -> FastAPI:
    client = BacnetClient(cfg.bacnet, cfg.request_timeout_seconds,
                          concurrency=cfg.poll_concurrency)
    cache = Cache(stale_after=cfg.poll_interval_seconds * 3,
                  offline_after=cfg.offline_after_failures)
    store = Store(db_path)
    # Zones reads the store, so both must exist before anything that resolves one.
    zones = Zones(cfg.devices, store)
    poller = Poller(cfg, client, cache, zones)
    reconciler = Reconciler(cfg, client, store, zones)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await client.start()
        await poller.start()
        await reconciler.start(cfg.reconcile_interval_seconds)
        if truportal is not None:
            await truportal.start()
        try:
            yield
        finally:
            if truportal is not None:
                await truportal.stop()
            await reconciler.stop()
            await poller.stop()
            await client.stop()
            store.close()

    app = FastAPI(title="BMS Gateway", version="0.1.0", lifespan=lifespan)
    auth = AuthStore(store)

    # Server-rendered pages resolve the session themselves rather than through the
    # `current_user` dependency, so the API-side block on an unchanged password
    # does not reach them -- a first-login account could still read the dashboard.
    # Done here so it covers every page, including ones added later, rather than
    # as a line in each route that someone will forget to copy.
    PAGE_PREFIXES = ("/ui/", "/t/")

    @app.middleware("http")
    async def force_password_change(request: Request, call_next):
        path = request.url.path
        is_page = path == "/" or path.startswith(PAGE_PREFIXES)
        if request.method == "GET" and is_page:
            token = request.cookies.get(COOKIE_NAME)
            user = auth.resolve_session(token) if token else None
            # Stashed so the page route does not resolve the same session a
            # second time. Two SQLite lookups per page load is cheap, but the
            # dashboard reloads itself every 15 seconds, so this is the hottest
            # path in the app and the saving is free.
            request.state.session_user = user
            if user is not None and user.must_change_password and path != "/ui/password":
                return RedirectResponse("/ui/password", status_code=303)
        return await call_next(request)
    throttle = LoginThrottle()
    # RP ID is the bare hostname of the public origin; the origin itself must
    # match exactly, scheme and all, or every assertion fails.
    origin = cfg.public_origin.rstrip("/")
    rp_id = origin.split("://", 1)[-1].split("/")[0].split(":")[0] if origin else ""
    passkeys = Passkeys(store, rp_id=rp_id, rp_name="16400 Ventura", origin=origin)

    tp_cfg = cfg.truportal
    truportal = (
        TruPortal(tp_cfg.host, tp_cfg.username, tp_cfg.password, verify_tls=tp_cfg.verify_tls)
        if tp_cfg.enabled and tp_cfg.host else None
    )

    def _device(device_id: int):
        for d in cfg.devices:
            if d.device_id == device_id:
                return d
        raise HTTPException(404, f"no device {device_id} in inventory")

    # Measured on firmware 01.01.16.00: no_BypassState follows a bypass write
    # somewhere between 0.8s and 1.9s, and clears within 1s.
    SETTLE_SECONDS = 2.0

    async def settle_and_refresh(device) -> None:
        """Re-read a device after commanding it, before answering the caller.

        Commands are written to `ni_*` points but the UI shows the `no_*`
        read-backs, which only the poll loop updates. Without this the page
        reloads against a cache entry up to a poll interval old, so the new state
        appears or not depending on where in the cycle the button was pressed.

        Refreshing from the wire rather than writing the expected value into the
        cache keeps the display honest: if the device did not take the command,
        the page says so instead of showing what we hoped for.
        """
        await asyncio.sleep(SETTLE_SECONDS)
        try:
            cache.record_success(device.device_id, await client.read_points(device))
        except DeviceUnreachable:
            pass  # the next scheduled poll will catch up

    # --- authentication ---------------------------------------------------------

    # Reachable while a password change is still owed. Anything else is refused:
    # a forced change that can be navigated around is not a forced change, and
    # the account is still holding a credential someone else chose and saw.
    PASSWORD_CHANGE_EXEMPT = frozenset({
        "/me", "/me/password", "/logout", "/login", "/ui/password", "/health", "/robots.txt",
    })

    def current_user(request: Request) -> User:
        token = request.cookies.get(COOKIE_NAME)
        user = auth.resolve_session(token) if token else None
        if user is None:
            raise HTTPException(401, "sign in required")
        if user.must_change_password and request.url.path not in PASSWORD_CHANGE_EXEMPT:
            raise HTTPException(
                403,
                {"error": "password change required", "code": "password_change_required",
                 "detail": "Set your own password before using the system."},
            )
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
        if not user.may_access_zone(zones.of(device)):
            # 404 rather than 403: a tenant has no business learning which other
            # device ids exist.
            raise HTTPException(404, f"no device {device_id} in inventory")
        return device

    def client_ip(request: Request) -> str:
        """The caller's real address, for throttling and the audit log.

        Behind a proxy `request.client.host` is the proxy itself, so every failed
        login on the internet would land in one throttle bucket and eight bad
        attempts from anyone would lock out everybody.

        The header is only trusted when `behind_proxy` is set. Trusting it
        unconditionally would be worse than not having it: a direct caller could
        forge an address per request and never trip the throttle at all.
        """
        if cfg.behind_proxy:
            forwarded = request.headers.get(cfg.client_ip_header)
            if forwarded:
                # X-Forwarded-For is a chain; the original client is leftmost.
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

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
        client = client_ip(request)
        if throttle.blocked(username, client):
            store.log(username, "login.throttled", client, outcome="error")
            return HTMLResponse(
                render_login("Too many attempts. Wait a few minutes and try again.", next),
                status_code=429,
            )

        # ~286 ms of CPU-bound scrypt. Left on the event loop it would stall
        # every other request -- including the thermostat poll -- on each
        # login attempt, which an attacker could use as a denial of service.
        user = await asyncio.to_thread(auth.authenticate, username, password)
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
        # A first login on a password someone else issued goes one place.
        if user.must_change_password:
            target = "/ui/password"
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
            # Resolve before revoking, so the audit trail records who left. A log
            # with login.ok and no matching logout cannot answer "was that session
            # still open when the door was unlocked?".
            user = auth.resolve_session(token)
            auth.revoke_session(token)
            if user:
                store.log(user.username, "logout", client_ip(request))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/me")
    async def me(user: User = Depends(current_user)) -> dict[str, Any]:
        return {
            **user.to_dict(),
            "passkeys_available": passkeys.configured,
            "has_passkey": passkeys.has_passkey(user.id) if passkeys.configured else False,
        }

    @app.post("/me/password")
    async def change_own_password(
        request: Request, body: ChangePasswordRequest,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Replace your own password, proving you know the current one.

        Changing a password revokes every session for the account, which is the
        point when the reason is a leak. That would include this browser, so a
        fresh session is issued to the caller -- otherwise the act of securing
        the account logs you out of the page you are standing on, and the most
        common reason to be here is a first login that cannot go anywhere else.
        """
        try:
            ok = await asyncio.to_thread(
                auth.change_own_password, user, body.current_password, body.new_password
            )
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        if not ok:
            store.log(user.username, "user.password_change.failed", client_ip(request),
                      outcome="error")
            raise HTTPException(403, "current password is incorrect")

        token = auth.create_session(user, request.headers.get("user-agent"))
        response = JSONResponse({"changed": True})
        response.set_cookie(
            COOKIE_NAME, token,
            httponly=True, samesite="lax", secure=cfg.secure_cookies,
            max_age=SESSION_TTL_SECONDS, path="/",
        )
        return response

    # --- passkeys ---------------------------------------------------------------
    #
    # Step-up verification for actions that open a door. A session proves someone
    # logged in, possibly weeks ago; a passkey assertion proves a verified person
    # is holding the device right now.

    @app.get("/passkeys")
    async def list_passkeys(user: User = Depends(current_user)) -> dict[str, Any]:
        if not passkeys.configured:
            return {"available": False, "reason": "requires https on a real hostname",
                    "credentials": []}
        return {
            "available": True,
            "credentials": [
                {"credential_id": c.credential_id, "label": c.label,
                 "created_at": c.created_at, "last_used_at": c.last_used_at}
                for c in passkeys.credentials_for(user.id)
            ],
        }

    @app.post("/passkeys/register/options")
    async def passkey_register_options(user: User = Depends(current_user)) -> Any:
        if not passkeys.configured:
            raise HTTPException(400, "passkeys require https on a real hostname")
        return json.loads(
            passkeys.registration_options(user.id, user.username, user.display_name)
        )

    @app.post("/passkeys/register", status_code=201)
    async def passkey_register(
        body: PasskeyRegisterRequest, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        try:
            credential_id = passkeys.register(
                user.id, body.credential, body.label, actor=user.username
            )
        except PasskeyError as err:
            raise HTTPException(400, str(err)) from err
        return {"credential_id": credential_id, "label": body.label}

    @app.delete("/passkeys/{credential_id}")
    async def passkey_delete(
        credential_id: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        if not passkeys.delete_credential(user.id, credential_id, actor=user.username):
            raise HTTPException(404, "no such passkey on this account")
        return {"deleted": credential_id}

    @app.post("/passkeys/verify/options")
    async def passkey_verify_options(user: User = Depends(current_user)) -> Any:
        try:
            return json.loads(passkeys.authentication_options(user.id))
        except PasskeyError as err:
            raise HTTPException(400, str(err)) from err

    @app.post("/passkeys/verify")
    async def passkey_verify(
        body: PasskeyVerifyRequest, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        try:
            passkeys.verify(user.id, body.credential, actor=user.username)
        except PasskeyError as err:
            raise HTTPException(400, str(err)) from err
        store.log(user.username, "passkey.verified")
        return {"verified": True, "valid_for_seconds": VERIFICATION_WINDOW_SECONDS}

    def require_recent_verification(user: User) -> None:
        """Gate for anything that physically opens a door.

        Skipped entirely when passkeys are unavailable -- on plain HTTP the
        browser refuses the API, and refusing the action instead would mean the
        feature simply never works rather than degrading.
        """
        if not passkeys.configured:
            return
        if not passkeys.has_passkey(user.id):
            raise HTTPException(
                403,
                {"error": "no passkey registered", "code": "passkey_required",
                 "detail": "Register this device before unlocking doors."},
            )
        if not passkeys.recently_verified(user.id):
            raise HTTPException(
                401,
                {"error": "verification required", "code": "verify_required",
                 "detail": "Confirm with Face ID or your fingerprint."},
            )

    @app.get("/t/{device_id}", include_in_schema=False)
    async def tenant(request: Request, device_id: int):
        """Mobile bypass page — the one screen a tenant ever needs.

        Deliberately separate from the operator UI: different audience, different
        device, different interaction. Sends an unauthenticated visitor to the
        login form rather than a bare 401, because this is a page a person opens
        from a bookmark, not an API call.
        """
        token = request.cookies.get(COOKIE_NAME)
        user = auth.resolve_session(token) if token else None
        if user is None:
            return RedirectResponse(f"/login?next=/t/{device_id}", status_code=303)

        device = require_device(device_id, user)
        state = cache.get(device_id)
        access = await access_buttons(user)

        return HTMLResponse(render_tenant(
            device_id,
            device.name,
            state.to_dict(cfg.poll_interval_seconds * 3) if state else {},
            passkeys_available=passkeys.configured,
            has_passkey=passkeys.has_passkey(user.id) if passkeys.configured else False,
            doors=access["doors"],
            lighting=access["lighting"],
        ))

    @app.get("/robots.txt", include_in_schema=False)
    async def robots() -> PlainTextResponse:
        """Refuse every crawler, at the origin.

        Cloudflare can block AI and search bots at the edge, but this states it
        independently: a well-behaved crawler that reaches the origin by any
        other route still gets told no, and it keeps the policy with the
        application rather than in a dashboard someone has to remember.

        Not a security control -- everything here needs a session anyway, so a
        crawler only ever sees the login page. This is about not having a
        building's control system turn up in search results.
        """
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

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

        # ni_OccManCom is inert until Cfg_Thermostat_Override is enabled: the write
        # succeeds, the point reads back the new value, and effective occupancy
        # never moves. Enable the gate first, and drop it again when handing
        # control back, so the device is left as it was found.
        try:
            releasing = state is OccupancyState.NO_OVERRIDE
            if not releasing:
                await client.write_point(device, BY_KEY["network_override_enable"], 1)
            await client.write_point(device, BY_KEY["occupancy_override"], int(state))
            if releasing:
                await client.write_point(device, BY_KEY["network_override_enable"], 0)
        except DeviceUnreachable as err:
            raise HTTPException(503, str(err)) from err

        cache.apply_local_write(device_id, "occupancy_override", int(state))
        cache.apply_local_write(device_id, "network_override_enable", 0 if releasing else 1)
        await settle_and_refresh(device)
        return {"device_id": device_id, "override": state.name}

    @app.post("/devices/{device_id}/bypass")
    async def bypass(device_id: int, body: BypassRequest,
                     user: User = Depends(current_user)) -> dict[str, Any]:
        """Timed occupancy bypass -- the 'I'm here now' path for a tenant.

        The duration comes from Cfg_Thermostat_BypOverrideTime, *not* from
        ni_BypassValue. Writing only the latter -- which is what the point naming
        suggests -- is accepted and then ignored, and every bypass runs for
        whatever the config point holds. Verified on firmware 01.01.16.00.

        Order matters: write the duration before enabling, or the device starts a
        timer with whatever value was there previously.

        Open to tenants because it expires by itself: the worst a stray tap costs
        is a few hours of conditioning in a room they already have access to.
        """
        device = require_device(device_id, user)
        try:
            if body.minutes > 0:
                await client.write_point(
                    device, BY_KEY["bypass_duration_cfg"], float(body.minutes)
                )
            # Kept consistent with the config point even though the device does not
            # read it for duration, so the two never disagree on inspection.
            await client.write_point(device, BY_KEY["bypass_minutes"], float(body.minutes))
            await client.write_point(
                device, BY_KEY["bypass_enable"], 1 if body.minutes > 0 else 0
            )
        except DeviceUnreachable as err:
            raise HTTPException(503, str(err)) from err

        cache.apply_local_write(device_id, "bypass_minutes", float(body.minutes))
        cache.apply_local_write(device_id, "bypass_enable", 1 if body.minutes > 0 else 0)
        await settle_and_refresh(device)

        values = (cache.get(device_id).values if cache.get(device_id) else {})
        return {
            "device_id": device_id,
            "bypass_minutes": body.minutes,
            # Report what the device says, not what was asked for.
            "active": bool(values.get("bypass_active")),
            "remaining_minutes": values.get("bypass_remaining_minutes"),
        }

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

    # --- doors and lighting -----------------------------------------------------

    def _door(door_id: int, user: User):
        """Resolve a configured door and check this user may open it.

        Managers and admins are never zone-scoped. A tenant needs the door to
        list '*' or one of their zones; an empty list therefore means
        manager-only, which is how the unused test door is kept off the tenant
        page while staying available for verification.
        """
        door = next((d for d in cfg.truportal.doors if d.id == door_id), None)
        if door is None:
            raise HTTPException(404, f"no door {door_id}")
        if user.at_least("manager"):
            return door
        if "*" in door.zones or any(z in door.zones for z in user.zones):
            return door
        raise HTTPException(404, f"no door {door_id}")

    def _visible_doors(user: User) -> list:
        out = []
        for d in cfg.truportal.doors:
            if user.at_least("manager") or "*" in d.zones or any(z in d.zones for z in user.zones):
                out.append(d)
        return out

    def _visible_triggers(user: User) -> list:
        out = []
        for t in cfg.truportal.lighting_triggers:
            if not user.at_least(t.role):
                continue
            # A tenant only gets the action for a zone they hold; managers get
            # everything, including the building-wide maintenance action.
            if user.at_least("manager") or t.zone == "*" or t.zone in user.zones:
                out.append(t)
        return out

    async def access_buttons(user: User) -> dict[str, list[dict[str, Any]]]:
        """The doors and lighting actions to draw for this user.

        Shared by the tenant page and the dashboard so the two cannot drift into
        offering different doors to the same person. Durations come from the
        panel, so a button can never advertise a period the hardware is not set
        to, and a panel that is unreachable means no buttons rather than a page
        that fails to render.
        """
        if truportal is None:
            return {"doors": [], "lighting": []}

        # Doors come from config, so they are listed whether or not the panel
        # answers -- and the unlock itself would fail loudly if it were down.
        doors = [{"id": d.id, "name": d.name} for d in _visible_doors(user)]

        # Only the *labels* need the panel. Bounded, because the SOAP client
        # waits 20s per call and inventory makes three: an unreachable panel
        # would otherwise hold up a page that every sign-in lands on, and the
        # dashboard reloads itself every 15 seconds. A warm cache returns
        # immediately, so this budget only ever bites when something is wrong.
        by_id: dict[int, Any] = {}
        panel_answered = True
        try:
            inventory = await asyncio.wait_for(truportal.inventory(), ACCESS_UI_BUDGET)
            by_id = {t.id: t for t in inventory["triggers"]}
        except (TruPortalError, asyncio.TimeoutError) as err:
            panel_answered = False
            log.warning("could not read lighting actions: %s", err)

        lighting: list[dict[str, Any]] = []
        for cfg_t in _visible_triggers(user):
            dev = by_id.get(cfg_t.id)
            if dev is None and panel_answered:
                # The panel replied and does not have this action: the config is
                # wrong, and a button that can only fail helps nobody.
                log.warning("configured lighting trigger %s is not on the panel", cfg_t.id)
                continue
            # A panel that did not answer is different -- the action probably
            # still exists, firing it does not depend on the inventory, and a
            # generic label claims no duration the hardware might not be set to.
            lighting.append({
                "id": cfg_t.id,
                "name": dev.name if dev else "Lights on",
                "duration": dev.duration_text if dev else None,
            })

        return {"doors": doors, "lighting": lighting}

    async def _fire_zone_lights(user: User) -> str | None:
        """Turn on the lights for a tenant's own floor, best effort.

        Called alongside a door unlock, because someone arriving after hours
        needs the door *and* the corridor. Never fails the unlock: standing
        outside in the dark is better than standing outside a locked door.

        Strictly the caller's own zone. This also matched `at_least("manager")`
        once, which meant every privileged unlock fired whichever tenant trigger
        was configured first -- floor-2, for any door in the building. There is
        no way to infer the right floor for someone who is not zone-scoped: a
        door records who may use it, not where it is. So they get the explicit
        button rather than an arbitrary guess, and the corridors keep their
        always-on corner lamps regardless.
        """
        for t in cfg.truportal.lighting_triggers:
            if t.role != "tenant" or t.zone == "*" or t.zone not in user.zones:
                continue
            try:
                await truportal.execute_trigger(t.id)
            except TruPortalError as err:
                log.warning("could not light %s after unlock: %s", t.zone, err)
                return None
            # Audited like any other physical act. Being a side effect rather
            # than a button press is exactly why it needs its own record --
            # otherwise the lights come on with nothing to say who or why.
            store.log(user.username, "lighting.on", t.zone,
                      {"trigger_id": t.id, "via": "door.unlock"})
            return t.zone
        return None

    def _require_truportal():
        if truportal is None:
            raise HTTPException(503, "access control is not configured")

    @app.get("/doors")
    async def list_doors(user: User = Depends(current_user)) -> dict[str, Any]:
        _require_truportal()
        return {
            "doors": [
                {"id": d.id, "name": d.name} for d in _visible_doors(user)
            ],
            "verification_required": passkeys.configured,
        }

    @app.post("/doors/{door_id}/unlock")
    async def unlock_door(door_id: int, user: User = Depends(current_user)) -> dict[str, Any]:
        """Momentary unlock — the panel relocks it after its own grant time.

        Gated behind a passkey assertion because this physically opens a
        building. Everything else here can be undone; a door that opened cannot.
        """
        _require_truportal()
        door = _door(door_id, user)
        require_recent_verification(user)

        try:
            await truportal.grant_access(door.id)
        except TruPortalError as err:
            store.log(user.username, "door.unlock", door.name, {"error": str(err)},
                      outcome="error")
            raise HTTPException(503, f"could not reach the door controller: {err}") from err

        store.log(user.username, "door.unlock", door.name, {"door_id": door.id})
        lit = await _fire_zone_lights(user)
        return {"door": door.name, "unlocked": True, "lights": lit}

    @app.get("/lighting")
    async def list_lighting(user: User = Depends(current_user)) -> dict[str, Any]:
        _require_truportal()
        try:
            inventory = await truportal.inventory()
        except TruPortalError as err:
            raise HTTPException(503, str(err)) from err

        by_id = {t.id: t for t in inventory["triggers"]}
        out = []
        for cfg_t in _visible_triggers(user):
            device = by_id.get(cfg_t.id)
            if device is None:
                log.warning("configured lighting trigger %s is not on the panel", cfg_t.id)
                continue
            out.append({
                "id": cfg_t.id,
                "name": device.name,
                # From the panel, so a button can never advertise a duration the
                # hardware is not actually set to.
                "duration": device.duration_text,
                "zone": cfg_t.zone,
                "role": cfg_t.role,
                "self_firing": device.self_firing,
            })
        return {"actions": out}

    @app.post("/lighting/{trigger_id}")
    async def fire_lighting(trigger_id: int, user: User = Depends(current_user)) -> dict[str, Any]:
        _require_truportal()
        trigger = next((t for t in _visible_triggers(user) if t.id == trigger_id), None)
        if trigger is None:
            raise HTTPException(404, f"no lighting action {trigger_id}")
        try:
            await truportal.execute_trigger(trigger_id)
        except TruPortalError as err:
            raise HTTPException(503, str(err)) from err
        # Zone as the target, id in the detail, matching door.unlock. A log line
        # reading "lighting.on 7" makes a reader go and find the config to learn
        # what lit up.
        store.log(user.username, "lighting.on", trigger.zone, {"trigger_id": trigger_id})
        return {"triggered": trigger_id}

    @app.get("/access/status")
    async def access_status(user: User = Depends(require("manager"))) -> dict[str, Any]:
        _require_truportal()
        try:
            status = await truportal.status()
            inventory = await truportal.inventory()
        except TruPortalError as err:
            raise HTTPException(503, str(err)) from err

        names = {d.id: d.name for d in inventory["doors"]}
        out_names = {o.id: o.name for o in inventory["outputs"]}
        return {
            "doors": [
                {"id": i, "name": names.get(i, str(i)),
                 "contact": s.get("contactStatus"), "held": s.get("heldAlarm"),
                 "forced": s.get("forcedAlarm"), "online": s.get("online")}
                for i, s in sorted(status.doors.items())
            ],
            "outputs": [
                {"id": i, "name": out_names.get(i, str(i)), "on": bool(v)}
                for i, v in sorted(status.outputs.items())
                if i in out_names
            ],
        }

    # --- zones ------------------------------------------------------------------

    @app.get("/zones")
    async def list_zones(user: User = Depends(current_user)) -> dict[str, Any]:
        return {
            "zones": zones.known(),
            "devices": {
                str(d.device_id): {"name": d.name, "zone": zones.of(d)}
                for d in cfg.devices
                if user.may_access_zone(zones.of(d))
            },
        }

    @app.put("/devices/{device_id}/zone")
    async def set_device_zone(
        device_id: int, body: ZoneRequest, user: User = Depends(require("manager"))
    ) -> dict[str, Any]:
        """Move a device to another zone -- what happens when a tenant relocates.

        Stored in the database rather than the config file so it takes effect
        immediately, without an edit and a restart.
        """
        device = _device(device_id)
        try:
            zones.set(device_id, body.zone, actor=user.username)
        except ValueError as err:
            raise HTTPException(400, str(err)) from err

        # The cache carries the zone for display and for tenant filtering, so it
        # has to move too or the change is invisible until the next restart.
        state = cache.get(device_id)
        if state is not None:
            state.zone = body.zone
        return {"device_id": device_id, "zone": body.zone, "reconcile_required": True}

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
        # The password is generated here, never supplied by the caller. An admin
        # who types a password knows a credential belonging to someone else, and
        # people reuse passwords -- that cannot be un-known later. This is shown
        # once, in this response, and the account cannot do anything until its
        # owner replaces it.
        password = generate_password()
        try:
            granted = zones.validate_grant(body.zones) if body.role == "tenant" else []
            await asyncio.to_thread(
                auth.create_user, body.username, password, body.role,
                body.display_name, granted, user.username,
            )
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        except Exception as err:  # noqa: BLE001 - almost always a UNIQUE clash
            raise HTTPException(409, f"could not create user: {err}") from err
        return {
            "username": body.username,
            "role": body.role,
            "password": password,
            "must_change_password": True,
        }

    @app.put("/users/{username}/password")
    async def set_password(
        username: str, user: User = Depends(require("admin"))
    ) -> dict[str, Any]:
        """Reset someone's password to a fresh one-time value.

        Takes no body, for the same reason `POST /users` does not: an admin who
        chooses a password knows a credential belonging to someone else, and a
        human-chosen one may be weak or reused besides. `set_password` already
        marks it must-change when the actor is not the owner, so this closes the
        remaining half -- the *value* no longer comes from the caller either.
        """
        password = generate_password()
        try:
            if not await asyncio.to_thread(
                auth.set_password, username, password, user.username
            ):
                raise HTTPException(404, f"no user {username}")
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        return {
            "username": username,
            "password": password,
            "must_change_password": True,
            "sessions_revoked": True,
        }

    @app.put("/users/{username}/zones")
    async def set_zones(
        username: str, body: ZonesRequest, user: User = Depends(require("admin"))
    ) -> dict[str, Any]:
        try:
            cleaned = zones.validate_grant(body.zones)
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
        if not auth.set_zones(username, cleaned, actor=user.username):
            raise HTTPException(404, f"no user {username}")
        return {"username": username, "zones": cleaned}

    @app.delete("/users/{username}")
    async def deactivate_user(
        username: str, user: User = Depends(require("admin"))
    ) -> dict[str, Any]:
        if username == user.username:
            raise HTTPException(400, "cannot deactivate the account you are signed in as")
        if not auth.deactivate(username, actor=user.username):
            raise HTTPException(404, f"no user {username}")
        return {"username": username, "active": False}

    # Mounted last so it can close over require_device and the running services.
    app.include_router(
        build_ui_router(cfg, cache, store, auth, reconciler, client, zones, passkeys,
                        require_device, access_buttons)
    )

    return app
