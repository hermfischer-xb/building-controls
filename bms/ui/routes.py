"""Routes for the operator interface.

These render pages only. Every mutation the pages perform goes back through the
JSON API from the browser, so role checks are not duplicated here -- if the API
would refuse it, the button fails and says so.

Pages redirect an unauthenticated visitor to the login form rather than returning
401, because a person typing a URL deserves a way in, not a status code.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..auth import COOKIE_NAME
from ..schedules import DAYS
from . import pages


def build_router(
    cfg,
    cache,
    store,
    auth,
    reconciler,
    client,
    zones,
    require_device: Callable[[int, Any], Any],
) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    def visitor(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        return auth.resolve_session(token) if token else None

    def signin(request: Request) -> RedirectResponse:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    def visible_devices(user) -> list[dict]:
        stale_after = cfg.poll_interval_seconds * 3
        return [
            d.to_dict(stale_after)
            for d in cache.all()
            if user.may_access_zone(d.zone)
        ]

    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        user = visitor(request)
        if user is None:
            return signin(request)
        status = reconciler.status if user.at_least("manager") else None
        return HTMLResponse(pages.dashboard(user, visible_devices(user), status))

    @router.get("/ui/devices/{device_id}", response_class=HTMLResponse)
    async def device(request: Request, device_id: int):
        user = visitor(request)
        if user is None:
            return signin(request)

        device_cfg = require_device(device_id, user)
        state = cache.get(device_id)
        if state is None:
            raise HTTPException(404, f"no device {device_id}")

        group_id = store.group_for_device(device_id)
        groups = store.groups() if user.at_least("manager") else []
        overrides = store.day_overrides(device_id)

        # Read the live schedule off the device. It is the honest source -- what the
        # thermostat holds may differ from intent until a reconcile runs.
        weekly = None
        try:
            raw = await client.read_weekly_schedule(device_cfg, "schedule,2")
            weekly = {
                DAYS[i]: [
                    {"time": f"{tv.time[0]:02d}:{tv.time[1]:02d}",
                     "state": int(tv.value.get_value())}
                    for tv in (entry.daySchedule or [])
                ]
                for i, entry in enumerate(raw)
            }
        except Exception:  # noqa: BLE001 - an offline device must still render
            weekly = None

        return HTMLResponse(
            pages.device_detail(
                user, state.to_dict(cfg.poll_interval_seconds * 3),
                groups, group_id, overrides, weekly,
                known_zones=zones.known() if user.at_least("manager") else None,
            )
        )

    @router.get("/ui/schedules", response_class=HTMLResponse)
    async def schedules(request: Request):
        user = visitor(request)
        if user is None:
            return signin(request)
        if not user.at_least("manager"):
            return RedirectResponse("/", status_code=303)

        assignments = [
            {"name": d.name, "group_id": store.group_for_device(d.device_id)}
            for d in cfg.devices
        ]
        return HTMLResponse(pages.schedules(user, store.groups(), assignments))

    @router.get("/ui/holidays", response_class=HTMLResponse)
    async def holidays(request: Request):
        user = visitor(request)
        if user is None:
            return signin(request)
        if not user.at_least("manager"):
            return RedirectResponse("/", status_code=303)

        from ..holidays import occurrences

        year = dt.date.today().year
        rules = [
            {**h.to_dict(), "dates": [str(d) for d in occurrences(h, year)]}
            for h in store.holidays(enabled_only=False)
        ]
        return HTMLResponse(
            pages.holidays(
                user, rules, store.all_exceptions(upcoming_only=True), year,
                known_zones=zones.known(),
                devices=[{"device_id": d.device_id, "name": d.name} for d in cfg.devices],
            )
        )

    @router.get("/ui/system", response_class=HTMLResponse)
    async def system(request: Request):
        user = visitor(request)
        if user is None:
            return signin(request)
        if not user.at_least("manager"):
            return RedirectResponse("/", status_code=303)

        devices = cache.all()
        online = sum(1 for d in devices if d.online)
        health = {
            "devices_total": len(devices),
            "devices_online": online,
            "poll_interval_seconds": cfg.poll_interval_seconds,
        }
        return HTMLResponse(
            pages.system(user, health, reconciler.status, store.recent_audit(40))
        )

    @router.get("/ui/users", response_class=HTMLResponse)
    async def users(request: Request):
        user = visitor(request)
        if user is None:
            return signin(request)
        if not user.at_least("admin"):
            return RedirectResponse("/", status_code=303)

        return HTMLResponse(pages.users(user, auth.users(), zones.known()))

    return router
