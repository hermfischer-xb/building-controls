#!/usr/bin/env python3
"""Check the login throttle: its budget, its buckets, and which address it uses.

Behind a tunnel every request arrives from 127.0.0.1. If the throttle keyed on
that, eight bad passwords from one attacker would lock out the whole building.
And if the client-IP header were trusted unconditionally, an attacker reaching
the app directly could forge a fresh address per attempt and never be throttled
at all.

Two sections, because they need different things:

- The **throttle semantics** run in process against LoginThrottle directly. No
  server, so these always run and are the ones that assert the security property.
- The **header handling** needs a live gateway, and is only meaningful when that
  gateway runs `behind_proxy: true`. Against a `behind_proxy: false` server every
  forged header is correctly ignored and all three synthetic clients collapse to
  127.0.0.1 -- which used to be reported as "buckets are shared", a security
  failure that was not happening. It now says so instead of crying wolf.

    .venv/bin/python tools/test_proxy.py
"""

from __future__ import annotations

import os
import pathlib
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bms.auth import LoginThrottle  # noqa: E402

BASE = os.environ.get("BMS_BASE_URL", "http://localhost:8237")
HEADER = "cf-connecting-ip"


def attempts(client_ip: str | None, count: int) -> list[int]:
    """Fire `count` bad logins, optionally claiming to come from `client_ip`."""
    codes = []
    with httpx.Client(base_url=BASE, timeout=30.0, follow_redirects=False) as c:
        for _ in range(count):
            headers = {HEADER: client_ip} if client_ip else {}
            r = c.post(
                "/login",
                data={"username": "throttle-probe", "password": "wrong"},
                headers=headers,
            )
            codes.append(r.status_code)
    return codes


def throttle_checks() -> int:
    """The security properties, in process. No server, no configuration."""
    failures = 0

    def check(ok: bool, label: str) -> None:
        nonlocal failures
        failures += not ok
        print(f"  {'ok ' if ok else 'FAIL'} {label}")

    print("=== throttle semantics (in process) ===")

    # The budget must be the configured one. It was not: `_prune` stores the list
    # it returns, so `record_failure` appending to both its return value and to
    # the dict recorded every failure twice and blocked after four.
    t = LoginThrottle(max_attempts=8, window_seconds=300)
    allowed = 0
    while not t.blocked("u", "A") and allowed < 20:
        t.record_failure("u", "A")
        allowed += 1
    check(allowed == 8, f"blocks after exactly 8 failures, as configured (got {allowed})")

    # One attacker must not lock anyone else out.
    t2 = LoginThrottle()
    for _ in range(50):
        t2.record_failure("victim", "203.0.113.10")
    check(t2.blocked("victim", "203.0.113.10"), "the attacking address is blocked")
    check(not t2.blocked("victim", "203.0.113.99"),
          "a different address for the same account is not")
    check(not t2.blocked("someone-else", "203.0.113.10"),
          "a different account from the same address is not")

    # Username spraying is the case Cloudflare's rule covers, not this one.
    t3 = LoginThrottle()
    for i in range(50):
        t3.record_failure(f"suite{i}", "203.0.113.10")
    check(not t3.blocked("suite0", "203.0.113.10"),
          "spraying many usernames from one address does NOT trip it "
          "(by design; that is the edge rate-limit's job)")

    # Clearing on success must not clear anyone else.
    t4 = LoginThrottle()
    for _ in range(20):
        t4.record_failure("u", "A")
        t4.record_failure("u", "B")
    t4.clear("u", "A")
    check(not t4.blocked("u", "A") and t4.blocked("u", "B"),
          "a successful login clears only that client's bucket")
    return failures


def main() -> int:
    failures = throttle_checks()

    # The header section needs a server, and is only meaningful behind a proxy.
    try:
        with httpx.Client(base_url=BASE, timeout=5.0) as c:
            health = c.get("/health").json()
    except Exception as err:  # noqa: BLE001 - no server is a skip, not a failure
        print(f"\nNo gateway on {BASE} ({type(err).__name__}); "
              f"skipping the client-IP header checks.")
        print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
        return 1 if failures else 0

    if not health.get("behind_proxy"):
        print("\nGateway reports behind_proxy=false, so the client-IP header is "
              "correctly ignored\nand every synthetic client here would collapse to "
              "127.0.0.1. Skipping those\nchecks rather than reporting a shared "
              "bucket that is not shared.")
        print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
        return 1 if failures else 0

    print("\n=== one client exhausts its own budget ===")
    codes = attempts("203.0.113.10", 12)
    blocked = codes.count(429)
    ok = blocked > 0
    failures += not ok
    print(f"  {'ok ' if ok else 'FAIL'} 12 bad logins from 203.0.113.10 -> "
          f"{codes.count(401)} rejected, {blocked} throttled")

    print("\n=== a different client is unaffected ===")
    codes = attempts("203.0.113.99", 3)
    ok = 429 not in codes
    failures += not ok
    print(f"  {'ok ' if ok else 'FAIL'} 203.0.113.99 got {codes} "
          f"({'not throttled' if ok else 'THROTTLED — buckets are shared'})")

    print("\n=== the throttled client is still blocked ===")
    codes = attempts("203.0.113.10", 1)
    ok = codes == [429]
    failures += not ok
    print(f"  {'ok ' if ok else 'FAIL'} 203.0.113.10 got {codes} (want [429])")

    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
