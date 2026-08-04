"""User administration from the command line.

Exists because of the bootstrap problem: creating users needs an admin, and the
first admin has to come from somewhere. Also the way to recover when someone locks
themselves out, which on an on-premises system with no email is otherwise awkward.

    .venv/bin/python -m bms.useradmin add-admin herm
    .venv/bin/python -m bms.useradmin add-tenant suite301 --zones floor-3
    .venv/bin/python -m bms.useradmin passwd herm
    .venv/bin/python -m bms.useradmin list
    .venv/bin/python -m bms.useradmin import-csv tenants.csv --dry-run
"""

from __future__ import annotations

import argparse
import csv
import getpass
import os
import secrets
import string
import sys
from pathlib import Path

from .auth import AuthStore, ROLES
from .store import Store

ALPHABET = string.ascii_letters + string.digits

# Columns accepted by import-csv. Only `username` is required; a missing role
# means tenant, which is what a bulk file of suites almost always is.
CSV_COLUMNS = ("username", "display_name", "role", "zones", "password")


def generate_password(length: int = 16) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def prompt_password(username: str) -> tuple[str, bool]:
    """Ask twice, or generate one. Returns (password, was_generated)."""
    first = getpass.getpass(f"Password for {username} (blank to generate): ")
    if not first:
        return generate_password(), True
    if len(first) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        raise SystemExit(2)
    if first != getpass.getpass("Repeat: "):
        print("Passwords did not match.", file=sys.stderr)
        raise SystemExit(2)
    return first, False


def read_rows(path: Path) -> list[dict[str, str]]:
    """Parse the CSV into normalised rows, or raise SystemExit with every problem.

    Validation is a separate pass from creation on purpose. Creating 25 tenants
    is a job someone does once, from a spreadsheet an office manager typed, and
    finding the typo on row 19 after rows 1-18 already exist is a mess to unpick
    by hand. Nothing is written until the whole file is known to be good.
    """
    with path.open(newline="", encoding="utf-8-sig") as fh:  # -sig: Excel writes a BOM
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise SystemExit(f"{path}: no header row")
        unknown = set(reader.fieldnames) - set(CSV_COLUMNS)
        if unknown:
            raise SystemExit(
                f"{path}: unknown column(s) {', '.join(sorted(unknown))}. "
                f"Expected any of: {', '.join(CSV_COLUMNS)}"
            )
        if "username" not in reader.fieldnames:
            raise SystemExit(f"{path}: a 'username' column is required")
        raw = list(reader)

    rows, problems, seen = [], [], set()
    for n, r in enumerate(raw, start=2):  # row 1 is the header
        username = (r.get("username") or "").strip()
        if not username:
            problems.append(f"row {n}: blank username")
            continue
        if username in seen:
            problems.append(f"row {n}: {username!r} appears twice in the file")
            continue
        seen.add(username)

        role = (r.get("role") or "tenant").strip().lower()
        if role not in ROLES:
            problems.append(f"row {n}: role {role!r} is not one of {', '.join(ROLES)}")

        # Semicolons or spaces, because a spreadsheet cell holding a comma would
        # have split into another column and never reached us intact.
        zones = [z for z in (r.get("zones") or "").replace(";", " ").split() if z]
        if role == "tenant" and not zones:
            problems.append(f"row {n}: tenant {username!r} has no zones, so would see nothing")

        password = (r.get("password") or "").strip()
        if password and len(password) < 8:
            problems.append(f"row {n}: password for {username!r} is under 8 characters")

        rows.append({
            "username": username,
            "display_name": (r.get("display_name") or "").strip() or username,
            "role": role,
            "zones": zones,
            "password": password,
        })

    if problems:
        raise SystemExit("\n".join([f"{path}: {len(problems)} problem(s)", *problems]))
    if not rows:
        raise SystemExit(f"{path}: no rows")
    return rows


def known_zones(config_path: str) -> set[str] | None:
    """Zones that some device actually sits in, for warning about typos.

    A tenant whose zone matches no device signs in successfully and sees an empty
    page, which reads as a broken system rather than a bad spreadsheet cell. Best
    effort: a missing or unreadable config just means no warning.
    """
    try:
        from .config import load
        return {d.zone for d in load(config_path).devices}
    except Exception:  # noqa: BLE001 - never block an import on an unrelated config error
        return None


def main() -> None:
    parser = argparse.ArgumentParser(prog="bms.useradmin", description=__doc__)
    parser.add_argument("--db", default="data/bms.db")
    sub = parser.add_subparsers(dest="command", required=True)

    for command, role in (("add-admin", "admin"), ("add-manager", "manager"),
                          ("add-tenant", "tenant")):
        p = sub.add_parser(command, help=f"create a {role}")
        p.add_argument("username")
        p.add_argument("--name", default="", help="display name")
        p.add_argument("--zones", nargs="*", default=[],
                       help="zones this tenant may act on (tenants only)")
        p.add_argument("--no-change-required", action="store_true",
                       help="skip the forced password change at first sign-in. Only "
                            "correct when creating your OWN account, e.g. the first "
                            "admin -- otherwise you know someone else's password")
        p.set_defaults(role=role)

    p = sub.add_parser("passwd", help="set a password and revoke existing sessions")
    p.add_argument("username")

    p = sub.add_parser("zones", help="replace a user's zone list")
    p.add_argument("username")
    p.add_argument("zones", nargs="*")

    p = sub.add_parser("deactivate", help="disable an account and revoke its sessions")
    p.add_argument("username")

    sub.add_parser("list", help="list accounts")

    p = sub.add_parser(
        "import-csv",
        help="create many accounts from a CSV",
        description="Columns: " + ", ".join(CSV_COLUMNS)
        + ". Only 'username' is required; role defaults to tenant, and a blank "
          "password column means one is generated.",
    )
    p.add_argument("path", type=Path)
    p.add_argument("--dry-run", action="store_true",
                   help="report what would happen and write nothing")
    p.add_argument("--config", default="config/devices.yaml",
                   help="checked so a tenant zone that matches no device is flagged")
    p.add_argument("--passwords", type=Path,
                   help="write generated passwords to this file (created mode 600) "
                        "instead of printing them")

    args = parser.parse_args()
    store = Store(args.db)
    auth = AuthStore(store)

    try:
        if args.command == "list":
            users = auth.users()
            if not users:
                print("No users yet. Create one with: "
                      ".venv/bin/python -m bms.useradmin add-admin <username>")
                return
            print(f"{'USERNAME':20} {'ROLE':9} {'ACTIVE':7} {'PASSWORD':10} ZONES")
            for u in users:
                zones = ", ".join(u["zones"]) or "—"
                pw = "must set" if u.get("must_change_password") else "own"
                print(f"{u['username']:20} {u['role']:9} "
                      f"{'yes' if u['active'] else 'no':7} {pw:10} {zones}")
            return

        if args.command in ("add-admin", "add-manager", "add-tenant"):
            if args.role != "tenant" and args.zones:
                print("Note: zones are ignored for managers and admins, who see every zone.",
                      file=sys.stderr)
            password, generated = prompt_password(args.username)
            must_change = not args.no_change_required
            auth.create_user(
                args.username, password, args.role, args.name, args.zones, actor="cli",
                must_change=must_change,
            )
            print(f"Created {args.role} {args.username!r}.")
            if generated:
                print(f"\n  Password: {password}\n")
                print("This is shown once. Store it in a password manager now.")
            if must_change:
                print("They will be asked to choose their own password at first sign-in.")
            return

        if args.command == "passwd":
            password, generated = prompt_password(args.username)
            if not auth.set_password(args.username, password, actor="cli"):
                print(f"No user {args.username!r}.", file=sys.stderr)
                raise SystemExit(1)
            print(f"Password updated for {args.username!r}; existing sessions revoked.")
            if generated:
                print(f"\n  Password: {password}\n")
            return

        if args.command == "zones":
            if not auth.set_zones(args.username, args.zones, actor="cli"):
                print(f"No user {args.username!r}.", file=sys.stderr)
                raise SystemExit(1)
            print(f"{args.username!r} zones: {', '.join(args.zones) or '(none)'}")
            return

        if args.command == "import-csv":
            rows = read_rows(args.path)
            existing = {u["username"] for u in auth.users()}
            zones_in_use = known_zones(args.config)

            new = [r for r in rows if r["username"] not in existing]
            skipped = [r for r in rows if r["username"] in existing]

            for r in new:
                if zones_in_use is not None and r["role"] == "tenant":
                    stray = [z for z in r["zones"] if z not in zones_in_use and z != "*"]
                    if stray:
                        print(f"  warning: {r['username']} is assigned "
                              f"{', '.join(stray)}, which no device is in",
                              file=sys.stderr)

            print(f"{len(new)} to create, {len(skipped)} already exist"
                  f"{' (nothing written: --dry-run)' if args.dry_run else ''}")
            for r in skipped:
                print(f"  skip   {r['username']} (already exists)")
            for r in new:
                print(f"  create {r['username']:20} {r['role']:8} "
                      f"{', '.join(r['zones']) or '—'}")
            if args.dry_run or not new:
                return

            issued = []
            for r in new:
                password = r["password"] or generate_password()
                # must_change is left at its default of True: this command exists
                # to hand credentials to other people, so every one of them is a
                # password its owner did not choose.
                auth.create_user(r["username"], password, r["role"],
                                 r["display_name"], r["zones"], actor="cli")
                if not r["password"]:
                    issued.append((r["username"], password))

            print(f"\nCreated {len(new)} account(s).")
            if not issued:
                return
            if args.passwords:
                # Written by this process, then locked down before anything goes
                # in it -- creating it world-readable and fixing it afterwards
                # would leave a window where the passwords were readable.
                fd = os.open(args.passwords, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    w.writerow(["username", "password"])
                    w.writerows(issued)
                print(f"Passwords written to {args.passwords} (mode 600). "
                      f"Hand them out, then delete the file.")
            else:
                print("\nGenerated passwords, shown once:\n")
                for username, password in issued:
                    print(f"  {username:20} {password}")
                print("\nStore these now. They cannot be recovered -- only reset.")
            return

        if args.command == "deactivate":
            if not auth.deactivate(args.username, actor="cli"):
                print(f"No user {args.username!r}.", file=sys.stderr)
                raise SystemExit(1)
            print(f"Deactivated {args.username!r}; sessions revoked.")
            return
    finally:
        store.close()


if __name__ == "__main__":
    main()
