#!/usr/bin/env python3
"""Parse the JavaScript every page emits, and check the HTML is well-formed.

Exists because of a bug that shipped: a `\\n` written into a non-raw Python
f-string became a real newline inside a single-quoted JS string, so the browser
hit `SyntaxError: Invalid or unexpected token`. Each page carries **one** inline
`<script>` block holding all of its functions, so a parse error anywhere kills
every control on the page -- create, deactivate, edit zones -- and it fails in
the browser, with nothing in the server log.

`tools/test_passwords.py` passed 24 of 24 against that page. It drives the API
end to end and never looks at the rendered JavaScript, so no amount of coverage
there can see this class of fault. Hence this: render every page, extract every
script, and hand it to `node --check`.

    .venv/bin/python tools/test_pages.py

Skips the JavaScript checks with a warning if node is missing; the HTML checks
still run.
"""

from __future__ import annotations

import html.parser
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bms.auth import User  # noqa: E402
from bms.ui import pages  # noqa: E402

SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.S)

ADMIN = User(1, "herm", "Herm", "admin", ())
TENANT = User(2, "suite301", "Copperfield CPA", "tenant", ("floor-3",))
FIRST_LOGIN = User(3, "suite305", "Bright Dental", "tenant", ("floor-3",),
                   must_change_password=True)

DEVICE = {
    "device_id": 326, "name": "Suite 326", "zone": "floor-3",
    "address": "192.168.144.226", "online": True, "unstable": False, "stale": False,
    "age_seconds": 4.0, "consecutive_failures": 0, "last_poll_ms": 640,
    "avg_poll_ms": 655, "failure_rate": 0.0, "total_polls": 120, "total_failures": 0,
    "values": {"space_temp": 73.4, "effective_heat_sp": 68, "effective_cool_sp": 76,
               "effective_occupancy": 1, "active_cool_stages": 1, "fan_running": True},
}
ACCOUNTS = [
    {"username": "herm", "display_name": "Herm", "role": "admin", "active": True,
     "zones": [], "last_login": 1754300000.0, "must_change_password": False},
    {"username": "suite301", "display_name": "Copperfield CPA", "role": "tenant",
     "active": True, "zones": ["floor-3"], "last_login": None,
     "must_change_password": True},
]
RECONCILE = {
    "last_run": 1754300000.0, "age_seconds": 12.0,
    "devices": [{"name": "Suite 326", "ok": True, "changed": [], "errors": []}],
    "clock_drift_seconds": {"326": 1.5},
}

# Names that would otherwise sail through: quotes, angle brackets, a backslash
# and an apostrophe are exactly what breaks either HTML attributes or a JS
# string literal, and tenant and device names are operator-supplied free text.
HOSTILE = "O'Brien & <Sons> \"Ltd\" \\ 100%"

PAGES = [
    ("dashboard", lambda: pages.dashboard(
        ADMIN, [DEVICE], RECONCILE, {"temperature_f": 88.0, "humidity_pct": 30.0,
                                     "source": "open-meteo"},
        doors=[{"id": 18, "name": HOSTILE}],
        lighting=[{"id": 6, "name": "Floor 3", "duration": "10 minutes"}])),
    ("dashboard (tenant, no devices)", lambda: pages.dashboard(TENANT, [], None)),
    ("device_detail", lambda: pages.device_detail(
        ADMIN, {**DEVICE, "name": HOSTILE}, [{"id": 1, "name": HOSTILE}], 1, {},
        {"monday": [{"time": "06:00", "state": 0}]}, known_zones=["floor-3"])),
    ("password_page", lambda: pages.password_page(FIRST_LOGIN)),
    ("password_page (voluntary)", lambda: pages.password_page(ADMIN)),
    ("security_page", lambda: pages.security_page(
        TENANT, [{"credential_id": "abc", "label": HOSTILE,
                  "created_at": 1754300000.0, "last_used_at": None}], True)),
    ("security_page (unavailable)", lambda: pages.security_page(
        TENANT, [], False, "requires https")),
    ("users", lambda: pages.users(ADMIN, ACCOUNTS, ["floor-3", "floor-2"])),
    ("zones_page", lambda: pages.zones_page(
        ADMIN, [{"device_id": 326, "name": HOSTILE, "address": "1.2.3.4",
                 "zone": "floor-3"}], ["floor-3"],
        [{"username": "suite301", "display_name": HOSTILE, "zones": ["floor-3"]}])),
    ("schedules", lambda: pages.schedules(
        ADMIN, [{"id": 1, "name": HOSTILE, "description": "Mon-Fri",
                 "week": {d: [] for d in range(1, 8)}}],
        [{"name": "Suite 326", "group_id": 1}])),
    ("holidays", lambda: pages.holidays(
        ADMIN, [{"id": 1, "name": HOSTILE, "rule_type": "fixed", "month": 7, "day": 4,
                 "year": None, "end_month": None, "end_day": None,
                 "week_of_month": None, "day_of_week": None, "state": 1,
                 "scope": "global", "scope_ref": "*", "enabled": True,
                 "dates": ["2026-07-04"]}],
        [], 2026, known_zones=["floor-3"],
        devices=[{"device_id": 326, "name": "Suite 326"}])),
    ("system", lambda: pages.system(
        ADMIN, {"devices_total": 16, "devices_online": 15, "poll_interval_seconds": 30},
        RECONCILE,
        [{"ts": 1754300000.0, "actor": "herm", "action": "door.unlock",
          "target": HOSTILE, "outcome": "ok"}],
        links=[DEVICE])),
]


class WellFormed(html.parser.HTMLParser):
    """Catches tags left unclosed by an f-string branch that emitted nothing."""

    VOID = {"meta", "link", "br", "hr", "img", "input", "source", "col"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack:
            self.problems.append(f"</{tag}> with nothing open")
        elif self.stack[-1] != tag:
            self.problems.append(f"</{tag}> closes <{self.stack[-1]}>")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
        else:
            self.stack.pop()


results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok and detail:
        print("\n".join(f"         {line}" for line in detail.strip().splitlines()[:8]))


def check_scripts(name: str, html_text: str, node: str | None) -> None:
    scripts = SCRIPT_RE.findall(html_text)
    if not scripts:
        return
    if node is None:
        return
    for i, source in enumerate(scripts):
        if not source.strip():
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(source)
            path = fh.name
        proc = subprocess.run([node, "--check", path], capture_output=True, text=True)
        pathlib.Path(path).unlink()
        label = f"{name}: script {i + 1} of {len(scripts)} parses"
        check(proc.returncode == 0, label, proc.stderr)


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("node not found -- JavaScript is NOT being checked. "
              "`brew install node` to enable it.\n", file=sys.stderr)

    for name, render in PAGES:
        try:
            html_text = render()
        except Exception as err:  # noqa: BLE001 - a page that raises is the finding
            check(False, f"{name}: renders", f"{type(err).__name__}: {err}")
            continue
        check(True, f"{name}: renders")

        parser = WellFormed()
        parser.feed(html_text)
        check(not parser.problems, f"{name}: HTML is balanced",
              "\n".join(parser.problems))

        # The hostile name must survive as escaped text, never as raw markup.
        if HOSTILE in html_text:
            check(False, f"{name}: escapes operator-supplied names",
                  "an unescaped name reached the page")

        check_scripts(name, html_text, node)

    failures = sum(1 for ok, _ in results if not ok)
    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'} "
          f"({len(results)} checks)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
