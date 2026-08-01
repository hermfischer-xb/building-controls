"""User administration from the command line.

Exists because of the bootstrap problem: creating users needs an admin, and the
first admin has to come from somewhere. Also the way to recover when someone locks
themselves out, which on an on-premises system with no email is otherwise awkward.

    .venv/bin/python -m bms.useradmin add-admin herm
    .venv/bin/python -m bms.useradmin add-tenant suite301 --zones floor-3
    .venv/bin/python -m bms.useradmin passwd herm
    .venv/bin/python -m bms.useradmin list
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import string
import sys

from .auth import AuthStore
from .store import Store

ALPHABET = string.ascii_letters + string.digits


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
        p.set_defaults(role=role)

    p = sub.add_parser("passwd", help="set a password and revoke existing sessions")
    p.add_argument("username")

    p = sub.add_parser("zones", help="replace a user's zone list")
    p.add_argument("username")
    p.add_argument("zones", nargs="*")

    p = sub.add_parser("deactivate", help="disable an account and revoke its sessions")
    p.add_argument("username")

    sub.add_parser("list", help="list accounts")

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
            print(f"{'USERNAME':20} {'ROLE':9} {'ACTIVE':7} ZONES")
            for u in users:
                zones = ", ".join(u["zones"]) or "—"
                print(f"{u['username']:20} {u['role']:9} "
                      f"{'yes' if u['active'] else 'no':7} {zones}")
            return

        if args.command in ("add-admin", "add-manager", "add-tenant"):
            if args.role != "tenant" and args.zones:
                print("Note: zones are ignored for managers and admins, who see every zone.",
                      file=sys.stderr)
            password, generated = prompt_password(args.username)
            auth.create_user(
                args.username, password, args.role, args.name, args.zones, actor="cli"
            )
            print(f"Created {args.role} {args.username!r}.")
            if generated:
                print(f"\n  Password: {password}\n")
                print("This is shown once. Store it in a password manager now.")
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
