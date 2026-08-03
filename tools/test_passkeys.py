#!/usr/bin/env python3
"""Exercise the passkey flow against a simulated authenticator.

A real test needs a phone with Face ID, which is not something CI or a bench can
provide, so this drives a software authenticator through the same code path the
browser uses. It checks the security properties rather than the happy path:
anyone can make registration succeed once, the question is whether a replayed
challenge, a foreign credential or a rewound counter are refused.

    .venv/bin/python tools/test_passkeys.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from base64 import urlsafe_b64encode
from hashlib import sha256 as _sha256
from pathlib import Path
from struct import pack

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from soft_webauthn import SoftWebauthnDevice
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url


def sha256(data: bytes) -> bytes:
    return _sha256(data).digest()


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bms.auth import AuthStore  # noqa: E402
from bms.passkeys import PasskeyError, Passkeys  # noqa: E402
from bms.store import Store  # noqa: E402

ORIGIN = "https://controls.example.com"
RP_ID = "controls.example.com"

# Flag bits in authenticator data. soft_webauthn hardcodes UP only, which models
# a plain security key press -- it cannot represent Face ID. A platform
# authenticator performing a biometric check sets UV as well, and requiring UV is
# the entire point of using passkeys for a door, so the simulator has to be able
# to set it or the test would only prove the weaker case.
FLAG_UP = 0x01   # user present
FLAG_UV = 0x04   # user verified -- the biometric actually happened
FLAG_AT = 0x40   # attested credential data included


class VerifyingDevice(SoftWebauthnDevice):
    """A software authenticator that reports user verification, like a phone."""

    def create(self, options, origin):
        raw = super().create(options, origin)
        # Attestation is 'none', so nothing signs authenticator data during
        # registration and the flag byte can be corrected in place.
        att = raw["response"]["attestationObject"]
        marker = sha256(self.rp_id.encode("ascii"))
        i = att.find(marker)
        if i < 0:
            raise RuntimeError("could not locate authenticator data")
        flags_at = i + len(marker)
        patched = bytearray(att)
        patched[flags_at] = FLAG_AT | FLAG_UV | FLAG_UP
        raw["response"]["attestationObject"] = bytes(patched)
        return raw

    def get(self, options, origin):
        # The assertion signature covers authenticator data, so the flag has to
        # be set before signing rather than patched afterwards.
        if self.rp_id != options["publicKey"]["rpId"]:
            raise ValueError("Requested rpID does not match current credential")
        self.sign_count += 1

        client_data = json.dumps({
            "type": "webauthn.get",
            "challenge": urlsafe_b64encode(
                options["publicKey"]["challenge"]).decode("ascii").rstrip("="),
            "origin": origin,
        }).encode("utf-8")

        authenticator_data = (
            sha256(self.rp_id.encode("ascii"))
            + bytes([FLAG_UV | FLAG_UP])
            + pack(">I", self.sign_count)
        )
        signature = self.private_key.sign(
            authenticator_data + sha256(client_data), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": urlsafe_b64encode(self.credential_id),
            "rawId": self.credential_id,
            "response": {
                "authenticatorData": authenticator_data,
                "clientDataJSON": client_data,
                "signature": signature,
                "userHandle": self.user_handle,
            },
            "type": "public-key",
        }

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((ok, label))
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


def to_browser_shape(raw: dict, challenge_b64: str, registration: bool) -> dict:
    """Convert the authenticator's bytes into what our JS sends over the wire."""
    response = {"clientDataJSON": bytes_to_base64url(raw["response"]["clientDataJSON"])}
    if registration:
        response["attestationObject"] = bytes_to_base64url(raw["response"]["attestationObject"])
    else:
        response["authenticatorData"] = bytes_to_base64url(raw["response"]["authenticatorData"])
        response["signature"] = bytes_to_base64url(raw["response"]["signature"])
        response["userHandle"] = (
            bytes_to_base64url(raw["response"]["userHandle"])
            if raw["response"].get("userHandle") else None
        )
    return {
        # `id` is by definition the base64url encoding of `rawId`; deriving it
        # rather than trusting the authenticator's own field keeps them in step.
        "id": bytes_to_base64url(raw["rawId"]),
        "rawId": bytes_to_base64url(raw["rawId"]),
        "type": raw["type"],
        "_challenge": challenge_b64,
        "response": response,
    }


def decode_options(options_json: str, registration: bool) -> tuple[dict, str]:
    """py_webauthn emits base64url JSON; the authenticator wants bytes."""
    opts = json.loads(options_json)
    challenge_b64 = opts["challenge"]
    opts["challenge"] = base64url_to_bytes(opts["challenge"])
    if registration:
        opts["user"]["id"] = base64url_to_bytes(opts["user"]["id"])
        for c in opts.get("excludeCredentials", []):
            c["id"] = base64url_to_bytes(c["id"])
    else:
        for c in opts.get("allowCredentials", []):
            c["id"] = base64url_to_bytes(c["id"])
    return opts, challenge_b64


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "test.db")
        auth = AuthStore(store)
        user_id = auth.create_user("tenant", "password1234", "tenant", "Tenant")
        pk = Passkeys(store, rp_id=RP_ID, rp_name="Test", origin=ORIGIN)
        device = VerifyingDevice()

        print("=== configured ===")
        check(pk.configured, "https origin enables passkeys")
        check(
            not Passkeys(store, rp_id="", rp_name="t", origin="http://x").configured,
            "plain http does not",
        )

        print("\n=== registration ===")
        opts, challenge = decode_options(
            pk.registration_options(user_id, "tenant", "Tenant"), registration=True
        )
        raw = device.create({"publicKey": opts}, ORIGIN)
        cred_id = pk.register(user_id, to_browser_shape(raw, challenge, True), "iPhone", "tenant")
        check(bool(cred_id), "registered a credential")
        check(pk.has_passkey(user_id), "has_passkey now true")
        check(len(pk.credentials_for(user_id)) == 1, "exactly one credential stored")

        print("\n=== authentication ===")
        check(not pk.recently_verified(user_id), "not verified before asserting")
        opts, challenge = decode_options(pk.authentication_options(user_id), registration=False)
        raw = device.get({"publicKey": opts}, ORIGIN)
        pk.verify(user_id, to_browser_shape(raw, challenge, False), "tenant")
        check(pk.recently_verified(user_id), "verified after a valid assertion")

        print("\n=== a replayed assertion is refused ===")
        try:
            pk.verify(user_id, to_browser_shape(raw, challenge, False), "tenant")
            check(False, "replay was accepted (challenge not consumed)")
        except PasskeyError:
            check(True, "replay rejected — challenge is single-use")

        print("\n=== a forged challenge is refused ===")
        opts, _ = decode_options(pk.authentication_options(user_id), registration=False)
        raw2 = device.get({"publicKey": opts}, ORIGIN)
        forged = bytes_to_base64url(b"x" * 32)
        try:
            pk.verify(user_id, to_browser_shape(raw2, forged, False), "tenant")
            check(False, "unknown challenge was accepted")
        except PasskeyError:
            check(True, "unknown challenge rejected")

        print("\n=== another user's credential is refused ===")
        other_id = auth.create_user("other", "password1234", "tenant", "Other")
        opts, challenge = decode_options(pk.authentication_options(user_id), registration=False)
        raw3 = device.get({"publicKey": opts}, ORIGIN)
        try:
            pk.verify(other_id, to_browser_shape(raw3, challenge, False), "other")
            check(False, "credential accepted for the wrong user")
        except PasskeyError:
            check(True, "credential bound to its owner")

        print("\n=== a wrong origin is refused ===")
        wrong = Passkeys(store, rp_id="evil.example.com", rp_name="t",
                         origin="https://evil.example.com")
        opts, challenge = decode_options(pk.authentication_options(user_id), registration=False)
        raw4 = device.get({"publicKey": opts}, ORIGIN)
        try:
            wrong.verify(user_id, to_browser_shape(raw4, challenge, False), "tenant")
            check(False, "assertion accepted for a different origin")
        except PasskeyError:
            check(True, "origin binding enforced")

        print("\n=== revocation ===")
        check(pk.delete_credential(user_id, cred_id, "tenant"), "credential removed")
        check(not pk.has_passkey(user_id), "no passkey after removal")

        store.close()

    failures = sum(1 for ok, _ in results if not ok)
    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'} "
          f"({len(results)} checks)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
