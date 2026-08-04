#!/usr/bin/env python3
"""Exercise the one-time password lifecycle, in process.

Nobody creates their own account here, so someone always sees somebody else's
first password. These checks are about the consequences of that: it must be
generated rather than chosen, it must be useless for anything except setting a
real one, and it must stop working the moment it is replaced.

Runs against a temporary database with no hardware and no server:

    .venv/bin/python tools/test_passwords.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import httpx  # noqa: E402

from bms.auth import generate_password  # noqa: E402
from bms.config import load  # noqa: E402
from bms.api import create_app
from bms.auth import AuthStore
from bms.store import Store

ok = fail = 0
def check(cond, label):
    global ok, fail
    ok, fail = ok + bool(cond), fail + (not cond)
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")

def readability_checks() -> None:
    """The one-time password has to survive being read aloud and typed once.

    A manager on Android reported capital I against lowercase l, and capital O
    against zero, as indistinguishable in the page's font -- and 16 characters as
    needlessly long for something used once. Both are properties worth pinning:
    they regress the moment someone reaches for `string.ascii_letters` again.
    """
    from bms.auth import UNAMBIGUOUS_ALPHABET, generate_password

    print("=== the one-time password is readable ===")
    confusable = set("lI1oO0")
    check(not (set(UNAMBIGUOUS_ALPHABET) & confusable),
          "none of I l 1 O o 0 appear, so neither reported pair can occur")
    check(len(UNAMBIGUOUS_ALPHABET) == 32,
          f"32 characters, so 5 bits each (got {len(UNAMBIGUOUS_ALPHABET)})")

    sample = [generate_password() for _ in range(2000)]
    check(all(len(p) == 10 for p in sample), "10 characters by default, i.e. 50 bits")
    check(not any(set(p) & confusable for p in sample),
          "2,000 generated passwords contain no confusable character")
    check(all(p.islower() or p.isalnum() for p in sample),
          "no mixed case, so nobody has to say \"capital B\"")
    # Long enough for hash_password, which refuses under 8.
    check(len(sample[0]) >= 8, "still long enough for the password hasher to accept")


async def main():
    readability_checks()
    print()
    db = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    app = create_app(load("config/devices.example.yaml"), str(db))
    s = Store(db); a = AuthStore(s)
    # The bootstrap admin chose their own password, so no change is owed.
    a.create_user("admin", "adminpass1234", "admin", "Admin", must_change=False)
    s.close()

    T = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=T, base_url="http://t",
                                 follow_redirects=False) as c:
        r = await c.post("/login", data={"username":"admin","password":"adminpass1234"})
        check(r.status_code == 303 and r.headers["location"] == "/",
              "admin with own password lands on the dashboard")

        print("\n=== creating a user ===")
        r = await c.post("/users", json={"username":"suite301","role":"tenant",
                                         "display_name":"Copperfield CPA","zones":["floor-3"]})
        check(r.status_code == 201, f"created ({r.status_code})")
        issued = r.json()
        # Length asserted against the generator, not a literal, so shortening it
        # again does not silently fail here.
        check("password" in issued and len(issued["password"]) == len(generate_password()),
              f"a one-time password is returned ({len(issued.get('password',''))} chars)")
        check(issued.get("must_change_password") is True, "flagged as must-change")
        pw = issued["password"]

        # The admin cannot choose it.
        r = await c.post("/users", json={"username":"x","role":"tenant",
                                         "zones":["floor-3"],"password":"chosen12345"})
        made = r.status_code == 201
        check(made and r.json()["password"] != "chosen12345",
              "a caller-supplied password is ignored, not honoured")

    print("\n=== the new user's first login ===")
    async with httpx.AsyncClient(transport=T, base_url="http://t",
                                 follow_redirects=False) as t:
        r = await t.post("/login", data={"username":"suite301","password":pw})
        check(r.status_code == 303, "the one-time password works")
        check(r.headers["location"] == "/ui/password", "forced to the password page")

        check((await t.get("/devices")).status_code == 403, "cannot use the API yet")
        r = await t.get("/")
        check(r.status_code == 303 and r.headers["location"] == "/ui/password",
              "pages redirect there too, not just the API")
        check((await t.get("/me")).status_code == 200, "/me is exempt")

        print("\n=== changing it ===")
        r = await t.post("/me/password", json={"current_password":"wrong",
                                               "new_password":"chosen-by-me-1234"})
        check(r.status_code == 403, "the wrong current password is refused")

        r = await t.post("/me/password", json={"current_password":pw, "new_password":pw})
        check(r.status_code == 400, "reusing the same password is refused")

        r = await t.post("/me/password", json={"current_password":pw,
                                               "new_password":"chosen-by-me-1234"})
        check(r.status_code == 200, f"accepted ({r.status_code})")
        check((await t.get("/devices")).status_code == 200,
              "the API opens up once the password is theirs")
        check((await t.get("/me")).json()["must_change_password"] is False,
              "the flag is cleared")

    print("\n=== an admin reset ===")
    async with httpx.AsyncClient(transport=T, base_url="http://t",
                                 follow_redirects=False) as c:
        await c.post("/login", data={"username":"admin","password":"adminpass1234"})
        r = await c.put("/users/suite301/password")
        check(r.status_code == 200, f"reset accepted with no body ({r.status_code})")
        reset_pw = r.json().get("password", "")
        check(len(reset_pw) == len(generate_password()),
              f"a fresh one-time password is returned ({len(reset_pw)} chars)")
        check(r.json().get("must_change_password") is True, "flagged as must-change")
        # The whole point: the admin cannot pick the value. This resets again, so
        # the password from here on is this one, not the one above.
        r = await c.put("/users/suite301/password", json={"password": "admin-picked-1"})
        check(r.status_code == 200 and r.json()["password"] != "admin-picked-1",
              "a supplied password is ignored on reset too")
        reset_pw = r.json()["password"]

    print("\n=== afterwards ===")
    async with httpx.AsyncClient(transport=T, base_url="http://t",
                                 follow_redirects=False) as t2:
        r = await t2.post("/login", data={"username":"suite301","password":pw})
        check(r.status_code == 401, "the original one-time password no longer works")
        r = await t2.post("/login", data={"username":"suite301",
                                          "password":"chosen-by-me-1234"})
        check(r.status_code == 401,
              "the password chosen before the reset no longer works either")

        # Walk the reset password all the way back to a normal sign-in. Without
        # this the suite only ever asserts what stops working, and a bug that
        # broke login outright would still pass every check above it.
        r = await t2.post("/login", data={"username":"suite301","password":reset_pw})
        check(r.status_code == 303 and r.headers["location"] == "/ui/password",
              "the reset password works, and lands on the change form")
        r = await t2.post("/me/password", json={"current_password":reset_pw,
                                                "new_password":"final-choice-1234"})
        check(r.status_code == 200, "it can be replaced")

    async with httpx.AsyncClient(transport=T, base_url="http://t",
                                 follow_redirects=False) as t3:
        r = await t3.post("/login", data={"username":"suite301",
                                          "password":"final-choice-1234"})
        check(r.status_code == 303 and r.headers["location"] == "/",
              "the new password signs in normally, straight to the dashboard")

    print(f"\n{'ALL PASS' if not fail else f'{fail} FAILURE(S)'} ({ok+fail} checks)")
    return 1 if fail else 0

sys.exit(asyncio.run(main()))
