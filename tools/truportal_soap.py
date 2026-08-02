#!/usr/bin/env python3
"""Minimal SOAP client for a TruPortal controller.

The PSTruPortal PowerShell module targets a REST API this firmware does not have.
What it actually speaks is SOAP, at AcsWebservices.wsdl, namespace
http://tempuri.org/ns1.xsd -- discovered by reading the appliance's own AngularJS
client, which loads jquery.soap and xml2json.

There is no session: every operation carries UserName and Password inline. That
removes session handling entirely, at the cost of sending credentials on every
call, so this must only ever run over TLS.

Read-only by default. The door and output commands are deliberately NOT wrapped
here -- unlocking a door is a physical act at an occupied building and belongs in
the driver behind an explicit, audited request, not in an exploration tool.

    TRUPORTAL_HOST=... TRUPORTAL_USER=... TRUPORTAL_PASS=... \
        .venv/bin/python tools/truportal_soap.py
"""

from __future__ import annotations

import os
import sys
from xml.sax.saxutils import escape

import httpx

SERVICE_PATH = "/AcsWebservices.wsdl"
NAMESPACE = "http://tempuri.org/ns1.xsd"

ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"'
    ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
    ' xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
    "<soap:Body><{op} xmlns=\"{ns}\">{payload}</{op}></soap:Body>"
    "</soap:Envelope>"
)


class TruPortal:
    def __init__(self, host: str, username: str, password: str, verify: bool = False) -> None:
        base = host if host.startswith("http") else f"https://{host}"
        self._user = username
        self._password = password
        # The appliance carries a self-signed certificate and has had no vendor
        # since 2020, so there is no path to a trusted one.
        self._client = httpx.Client(base_url=base, verify=verify, timeout=30.0)

    def call(self, operation: str, **fields) -> httpx.Response:
        payload = (
            f"<UserName>{escape(self._user)}</UserName>"
            f"<Password>{escape(self._password)}</Password>"
        )
        for key, value in fields.items():
            payload += f"<{key}>{escape(str(value))}</{key}>"
        body = ENVELOPE.format(op=operation, ns=NAMESPACE, payload=payload)
        return self._client.post(
            SERVICE_PATH,
            content=body.encode(),
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": operation},
        )

    def close(self) -> None:
        self._client.close()


def summarise(response: httpx.Response, limit: int = 700) -> str:
    text = " ".join(response.text.split())
    fault = "FAULT" if "Fault" in text or "fault" in text[:400] else ""
    return f"HTTP {response.status_code} {fault} {len(response.text)}b :: {text[:limit]}"


def main() -> int:
    host = os.environ.get("TRUPORTAL_HOST")
    user = os.environ.get("TRUPORTAL_USER")
    password = os.environ.get("TRUPORTAL_PASS")
    if not (host and user and password):
        print("set TRUPORTAL_HOST, TRUPORTAL_USER, TRUPORTAL_PASS", file=sys.stderr)
        return 2

    tp = TruPortal(host, user, password)
    try:
        # Read-only operations only.
        for operation, fields in [
            ("Login", {}),
            ("GetAccessPoints", {"count": 50, "offset": 0, "revision": 0}),
            ("GetOutputs", {"count": 50, "offset": 0, "revision": 0}),
        ]:
            print(f"\n########## {operation} {fields or ''}")
            try:
                print(" ", summarise(tp.call(operation, **fields)))
            except Exception as err:  # noqa: BLE001
                print(f"  request failed: {type(err).__name__}: {err}")
    finally:
        tp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
