#!/usr/bin/env python3
"""Exercise the authorisation matrix against a running gateway.

Asserts the whole point of the role model: a tenant can start the air in their own
suite and do nothing else, a manager runs the building but cannot create users,
and nobody unauthenticated gets anywhere.

Credentials come from the environment so real ones never end up in the repo:

    BMS_ADMIN=herm:secret BMS_MANAGER=bldgmgr:secret BMS_TENANT=suite301:secret \\
        .venv/bin/python tools/test_roles.py
"""

from __future__ import annotations

import os
import sys

import httpx

BASE = os.environ.get("BMS_BASE_URL", "http://localhost:8237")


def _account(env_var: str, default_user: str) -> tuple[str, str]:
    """Read `user:password` from the environment.

    No password default: a test that silently runs against a guessable account is
    worse than one that refuses to start.
    """
    raw = os.environ.get(env_var)
    if not raw or ":" not in raw:
        raise SystemExit(
            f"set {env_var}=<username>:<password> (e.g. {env_var}={default_user}:...)"
        )
    username, _, password = raw.partition(":")
    return username, password


ACCOUNTS = {
    "admin": _account("BMS_ADMIN", "herm"),
    "manager": _account("BMS_MANAGER", "bldgmgr"),
    "tenant": _account("BMS_TENANT", "suite301"),
}

# (label, method, path, json body, expected status per role)
CHECKS = [
    ("GET  /devices", "GET", "/devices", None, {"admin": 200, "manager": 200, "tenant": 200}),
    ("GET  /t/301 (own zone)", "GET", "/t/301", None,
     {"admin": 200, "manager": 200, "tenant": 200}),
    ("POST bypass (self-expiring)", "POST", "/devices/301/bypass", {"minutes": 0},
     {"admin": 200, "manager": 200, "tenant": 200}),
    ("POST override (never expires)", "POST", "/devices/301/override",
     {"state": "NO_OVERRIDE"}, {"admin": 200, "manager": 200, "tenant": 403}),
    ("POST setpoint write", "POST", "/devices/301/points/occ_cool_sp", {"value": 76},
     {"admin": 200, "manager": 200, "tenant": 403}),
    ("GET  /holidays", "GET", "/holidays", None,
     {"admin": 200, "manager": 200, "tenant": 200}),
    ("POST /holidays", "POST", "/holidays",
     {"name": "role-test", "rule_type": "fixed", "month": 1, "day": 2},
     {"admin": 201, "manager": 201, "tenant": 403}),
    ("POST /reconcile", "POST", "/reconcile", None,
     {"admin": 200, "manager": 200, "tenant": 403}),
    ("GET  /audit", "GET", "/audit", None, {"admin": 200, "manager": 200, "tenant": 403}),
    ("GET  /users", "GET", "/users", None, {"admin": 200, "manager": 403, "tenant": 403}),
    ("GET  /reconcile status", "GET", "/reconcile", None,
     {"admin": 200, "manager": 200, "tenant": 403}),
]


def login(role: str) -> httpx.Client:
    username, password = ACCOUNTS[role]
    client = httpx.Client(base_url=BASE, follow_redirects=False, timeout=30.0)
    res = client.post("/login", data={"username": username, "password": password})
    if res.status_code != 303:
        raise SystemExit(f"login failed for {username}: HTTP {res.status_code}")
    return client


def main() -> int:
    failures = 0

    print("=== unauthenticated ===")
    anon = httpx.Client(base_url=BASE, follow_redirects=False, timeout=30.0)
    for label, path, expected in [
        ("GET /devices", "/devices", 401),
        ("GET /users", "/users", 401),
        ("GET /t/301", "/t/301", 303),
    ]:
        got = anon.get(path).status_code
        ok = got == expected
        failures += not ok
        print(f"  {'ok ' if ok else 'FAIL'} {label:20} {got} (want {expected})")

    got = anon.post("/login", data={"username": "herm", "password": "wrong"}).status_code
    ok = got == 401
    failures += not ok
    print(f"  {'ok ' if ok else 'FAIL'} {'bad password':20} {got} (want 401)")

    clients = {role: login(role) for role in ACCOUNTS}

    print("\n=== authorisation matrix ===")
    print(f"{'ACTION':32} {'admin':>7} {'manager':>9} {'tenant':>8}")
    print("-" * 60)
    for label, method, path, body, expected in CHECKS:
        cells = []
        for role in ("admin", "manager", "tenant"):
            res = clients[role].request(method, path, json=body)
            want = expected[role]
            ok = res.status_code == want
            failures += not ok
            cells.append(f"{res.status_code}{'' if ok else f'!={want}'}")
        print(f"{label:32} {cells[0]:>7} {cells[1]:>9} {cells[2]:>8}")

    print("\n=== tenant zone isolation ===")
    # suite301 holds zone 'floor-3'; device 301 is in it. A device in another zone
    # must be invisible, not merely forbidden.
    visible = clients["tenant"].get("/devices").json()
    zones = {d["zone"] for d in visible}
    ok = zones <= {"floor-3"}
    failures += not ok
    print(f"  {'ok ' if ok else 'FAIL'} tenant sees only {zones or '{}'} (want {{'floor-3'}})")

    res = clients["tenant"].get("/devices/9999")
    ok = res.status_code == 404
    failures += not ok
    print(f"  {'ok ' if ok else 'FAIL'} unknown device -> {res.status_code} (want 404)")

    print("\n=== session revocation ===")
    me = clients["tenant"].get("/me").status_code
    clients["tenant"].post("/logout")
    after = clients["tenant"].get("/me").status_code
    ok = me == 200 and after == 401
    failures += not ok
    print(f"  {'ok ' if ok else 'FAIL'} /me before logout {me}, after {after} (want 200 then 401)")

    # Clean up the holiday rows the matrix created.
    for h in clients["admin"].get("/holidays").json():
        if h["name"] == "role-test":
            clients["admin"].delete(f"/holidays/{h['id']}")

    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
