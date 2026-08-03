#!/usr/bin/env python3
"""Turn a spreadsheet of thermostats into the `devices:` block for devices.yaml.

Twenty-five thermostats is too many to hand-type into YAML without transposing a
digit somewhere, and a transposed digit here is a device that silently never
answers. This validates the whole sheet first -- duplicate ids, duplicate
addresses, malformed IPs -- and only then prints YAML.

    .venv/bin/python tools/devices_from_csv.py thermostats.csv

Columns: device_id, address, name, zone, mac. `mac` is optional and only
recorded so the DHCP reservation can be rebuilt from this file; nothing reads it.

It deliberately prints rather than editing devices.yaml in place. That file is
mostly comments explaining traps that cost real time to find, and every YAML
library available here would drop them on a round trip. Paste the block in.
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import re
import sys
from pathlib import Path

COLUMNS = ("device_id", "address", "name", "zone", "mac")
MAC_RE = re.compile(r"^[0-9a-fA-F]{2}([:.-]?)(?:[0-9a-fA-F]{2}\1?){4}[0-9a-fA-F]{2}$")


def parse(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as fh:  # -sig: Excel writes a BOM
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise SystemExit(f"{path}: no header row")
        missing = {"device_id", "address"} - set(reader.fieldnames)
        if missing:
            raise SystemExit(f"{path}: missing required column(s): {', '.join(sorted(missing))}")
        unknown = set(reader.fieldnames) - set(COLUMNS)
        if unknown:
            raise SystemExit(f"{path}: unknown column(s): {', '.join(sorted(unknown))}")
        raw = list(reader)

    rows, problems = [], []
    ids: dict[int, int] = {}
    addresses: dict[str, int] = {}

    for n, r in enumerate(raw, start=2):  # row 1 is the header
        raw_id = (r.get("device_id") or "").strip()
        try:
            device_id = int(raw_id)
        except ValueError:
            problems.append(f"row {n}: device_id {raw_id!r} is not a number")
            continue

        # 4194302 is what an unconfigured TC500A reports. Two of them would
        # collide, and the collision looks like one flaky thermostat.
        if device_id == 4194302:
            problems.append(
                f"row {n}: device_id 4194302 is the unconfigured default -- "
                f"set one on the thermostat's touchscreen first"
            )
        if not 0 <= device_id <= 4194302:
            problems.append(f"row {n}: device_id {device_id} is outside the BACnet range")
        if device_id in ids:
            problems.append(f"row {n}: device_id {device_id} already used on row {ids[device_id]}")
        ids[device_id] = n

        address = (r.get("address") or "").strip()
        host = address.split(":")[0]
        try:
            ipaddress.ip_address(host)
        except ValueError:
            problems.append(f"row {n}: address {address!r} is not an IP address")
        if address in addresses:
            problems.append(f"row {n}: address {address} already used on row {addresses[address]}")
        addresses[address] = n

        mac = (r.get("mac") or "").strip()
        if mac and not MAC_RE.match(mac):
            problems.append(f"row {n}: mac {mac!r} is not a MAC address")

        zone = (r.get("zone") or "").strip()
        if not zone:
            problems.append(f"row {n}: no zone, so no tenant could ever be given access")

        rows.append({
            "device_id": device_id,
            "address": address,
            "name": (r.get("name") or "").strip() or f"Device {device_id}",
            "zone": zone,
            "mac": mac,
        })

    if problems:
        raise SystemExit("\n".join([f"{path}: {len(problems)} problem(s)", *problems]))
    if not rows:
        raise SystemExit(f"{path}: no rows")
    return rows


def quote(value: str) -> str:
    """Quote anything YAML would otherwise read as a number, bool or null.

    A name like `301` or a zone called `no` would come back as an int and a
    False, and the mismatch would not surface until a lookup failed.
    """
    if value and not re.fullmatch(r"[A-Za-z][\w .&'/-]*", value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path)
    ap.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    args = ap.parse_args()

    rows = parse(args.path)
    rows.sort(key=lambda r: r["device_id"])

    out = ["devices:"]
    for r in rows:
        out.append(f"  - device_id: {r['device_id']}")
        out.append(f"    address: {r['address']}")
        out.append(f"    name: {quote(r['name'])}")
        out.append(f"    zone: {quote(r['zone'])}")
        if r["mac"]:
            out.append(f"    mac: {quote(r['mac'])}")
        out.append("")
    text = "\n".join(out)

    if args.output:
        args.output.write_text(text)
        print(f"{len(rows)} device(s) written to {args.output}", file=sys.stderr)
    else:
        print(text)

    zones = sorted({r["zone"] for r in rows})
    print(f"# {len(rows)} devices across {len(zones)} zone(s): {', '.join(zones)}",
          file=sys.stderr)
    print("# Tenant accounts must be given one of those zone names exactly, or they "
          "sign in to an empty page.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
