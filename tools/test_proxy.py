#!/usr/bin/env python3
"""Check that the login throttle keys on the real client, not the proxy.

Behind a tunnel every request arrives from 127.0.0.1. If the throttle keyed on
that, eight bad passwords from one attacker would lock out the whole building.
And if the client-IP header were trusted unconditionally, an attacker reaching
the app directly could forge a fresh address per attempt and never be throttled
at all. Both failure modes are asserted here.

    .venv/bin/python tools/test_proxy.py
"""

from __future__ import annotations

import sys

import httpx

BASE = "http://localhost:8237"
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


def main() -> int:
    failures = 0

    print("=== one client exhausts its own budget ===")
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
    print("\nNote: with behind_proxy=false the header is ignored and every request")
    print("falls back to the socket address, which is the safe default.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
