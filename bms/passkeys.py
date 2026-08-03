"""Step-up verification with WebAuthn, for actions that open a door.

A session cookie proves someone logged in once, possibly a month ago. That is a
reasonable bar for nudging a setpoint and the wrong one for unlocking an exterior
door: a phone that is stolen while unlocked carries a live session, and a button
in a pocket can be pressed by a leg.

WebAuthn answers both with the same mechanism. `userVerification: "required"`
makes the authenticator prove a *person* is present and verified -- Face ID on
iOS, fingerprint or face on Android -- and signs a server-issued challenge with a
key that never leaves the device. The biometric itself is never transmitted and
this server never sees it; what arrives is a signature over a challenge we chose.

Deliberately scoped to door unlock. Requiring a face scan to adjust a thermostat
would be friction with nothing to show for it.

Three properties this relies on, in decreasing order of how easy they are to get
wrong:

* **Challenges are single-use and server-side.** Stored in the database and
  deleted on use, so a replayed one has nothing to match.
* **The sign counter must never go backwards.** Authenticators increment it on
  every assertion; a decrease means the credential was cloned.
* **RP ID must match the hostname exactly.** It is the origin binding that stops
  a phished page using a credential registered here.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

log = logging.getLogger(__name__)

# Long enough to find the phone and look at it; short enough that an intercepted
# challenge is worthless by the time anyone could use it.
CHALLENGE_TTL_SECONDS = 180.0

# How recently a verification must have happened for a step-up action to be
# allowed. Long enough to unlock two doors in a row without a second face scan,
# short enough that walking away from an unlocked phone does not leave the
# building openable.
VERIFICATION_WINDOW_SECONDS = 120.0


class PasskeyError(Exception):
    """Registration or verification failed. The message is safe to show a user."""


@dataclass(frozen=True)
class Credential:
    credential_id: str
    label: str
    created_at: float
    last_used_at: float | None


class Passkeys:
    def __init__(self, store, rp_id: str, rp_name: str, origin: str) -> None:
        self._store = store
        self._conn = store._conn
        self._rp_id = rp_id
        self._rp_name = rp_name
        self._origin = origin
        # user_id -> when they last completed a verified assertion
        self._verified_at: dict[int, float] = {}

    @property
    def configured(self) -> bool:
        """WebAuthn needs a real hostname and a secure origin.

        On plain HTTP or a bare IP the browser refuses the API outright, so this
        stays off rather than offering a button that cannot work.
        """
        return bool(self._rp_id) and self._origin.startswith("https://")

    # --- stored credentials -----------------------------------------------------

    def credentials_for(self, user_id: int) -> list[Credential]:
        return [
            Credential(r["credential_id"], r["label"], r["created_at"], r["last_used_at"])
            for r in self._conn.execute(
                "SELECT credential_id, label, created_at, last_used_at"
                " FROM webauthn_credential WHERE user_id = ? ORDER BY created_at",
                (user_id,),
            )
        ]

    def has_passkey(self, user_id: int) -> bool:
        return bool(self.credentials_for(user_id))

    def delete_credential(self, user_id: int, credential_id: str, actor: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM webauthn_credential WHERE user_id = ? AND credential_id = ?",
            (user_id, credential_id),
        )
        self._conn.commit()
        if cur.rowcount:
            self._store.log(actor, "passkey.delete", credential_id[:16])
        return bool(cur.rowcount)

    # --- challenges -------------------------------------------------------------

    def _issue_challenge(self, user_id: int, purpose: str) -> bytes:
        challenge = secrets.token_bytes(32)
        self._conn.execute("DELETE FROM webauthn_challenge WHERE expires_at < ?", (time.time(),))
        self._conn.execute(
            "INSERT INTO webauthn_challenge (challenge, user_id, purpose, expires_at)"
            " VALUES (?, ?, ?, ?)",
            (bytes_to_base64url(challenge), user_id, purpose, time.time() + CHALLENGE_TTL_SECONDS),
        )
        self._conn.commit()
        return challenge

    def _consume_challenge(self, user_id: int, purpose: str, challenge_b64: str) -> bytes:
        """Take a challenge and destroy it, so it can only ever be used once."""
        row = self._conn.execute(
            "SELECT challenge, expires_at FROM webauthn_challenge"
            " WHERE challenge = ? AND user_id = ? AND purpose = ?",
            (challenge_b64, user_id, purpose),
        ).fetchone()
        # Delete regardless of validity: a challenge that has been presented,
        # even wrongly, must not be presentable again.
        self._conn.execute("DELETE FROM webauthn_challenge WHERE challenge = ?", (challenge_b64,))
        self._conn.commit()

        if row is None:
            raise PasskeyError("that request has expired, please try again")
        if row["expires_at"] < time.time():
            raise PasskeyError("that request has expired, please try again")
        return base64url_to_bytes(row["challenge"])

    # --- registration -----------------------------------------------------------

    def registration_options(self, user_id: int, username: str, display_name: str) -> str:
        options = generate_registration_options(
            rp_id=self._rp_id,
            rp_name=self._rp_name,
            user_id=str(user_id).encode(),
            user_name=username,
            user_display_name=display_name or username,
            # Platform authenticator: the phone's own Face ID or fingerprint,
            # not a roaming USB key. That is what tenants actually have.
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            # Do not offer to enrol a device that is already enrolled.
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id))
                for c in self.credentials_for(user_id)
            ],
        )
        self._conn.execute(
            "DELETE FROM webauthn_challenge WHERE user_id = ? AND purpose = 'register'",
            (user_id,),
        )
        self._conn.execute(
            "INSERT INTO webauthn_challenge (challenge, user_id, purpose, expires_at)"
            " VALUES (?, ?, 'register', ?)",
            (bytes_to_base64url(options.challenge), user_id,
             time.time() + CHALLENGE_TTL_SECONDS),
        )
        self._conn.commit()
        return options_to_json(options)

    def register(self, user_id: int, credential: dict, label: str, actor: str) -> str:
        challenge_b64 = credential.get("_challenge")
        if not challenge_b64:
            raise PasskeyError("missing challenge")
        expected = self._consume_challenge(user_id, "register", challenge_b64)

        try:
            verified = verify_registration_response(
                credential={k: v for k, v in credential.items() if k != "_challenge"},
                expected_challenge=expected,
                expected_rp_id=self._rp_id,
                expected_origin=self._origin,
                require_user_verification=True,
            )
        except Exception as err:  # noqa: BLE001 - library raises many types
            log.warning("passkey registration failed for user %s: %s", user_id, err)
            raise PasskeyError("could not register that device") from err

        credential_id = bytes_to_base64url(verified.credential_id)
        self._conn.execute(
            "INSERT OR REPLACE INTO webauthn_credential"
            " (credential_id, user_id, public_key, sign_count, label, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (credential_id, user_id, verified.credential_public_key,
             verified.sign_count, label or "phone", time.time()),
        )
        self._conn.commit()
        self._store.log(actor, "passkey.register", label or "phone",
                        {"credential_id": credential_id[:16]})
        return credential_id

    # --- authentication ---------------------------------------------------------

    def authentication_options(self, user_id: int) -> str:
        creds = self.credentials_for(user_id)
        if not creds:
            raise PasskeyError("no passkey registered on this account")

        options = generate_authentication_options(
            rp_id=self._rp_id,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id))
                for c in creds
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        self._conn.execute(
            "DELETE FROM webauthn_challenge WHERE user_id = ? AND purpose = 'authenticate'",
            (user_id,),
        )
        self._conn.execute(
            "INSERT INTO webauthn_challenge (challenge, user_id, purpose, expires_at)"
            " VALUES (?, ?, 'authenticate', ?)",
            (bytes_to_base64url(options.challenge), user_id,
             time.time() + CHALLENGE_TTL_SECONDS),
        )
        self._conn.commit()
        return options_to_json(options)

    def verify(self, user_id: int, credential: dict, actor: str) -> None:
        """Verify an assertion, or raise. Records the time on success."""
        challenge_b64 = credential.get("_challenge")
        if not challenge_b64:
            raise PasskeyError("missing challenge")
        expected = self._consume_challenge(user_id, "authenticate", challenge_b64)

        credential_id = credential.get("id")
        row = self._conn.execute(
            "SELECT public_key, sign_count FROM webauthn_credential"
            " WHERE credential_id = ? AND user_id = ?",
            (credential_id, user_id),
        ).fetchone()
        if row is None:
            raise PasskeyError("that device is not registered on this account")

        try:
            verified = verify_authentication_response(
                credential={k: v for k, v in credential.items() if k != "_challenge"},
                expected_challenge=expected,
                expected_rp_id=self._rp_id,
                expected_origin=self._origin,
                credential_public_key=row["public_key"],
                credential_current_sign_count=row["sign_count"],
                require_user_verification=True,
            )
        except Exception as err:  # noqa: BLE001
            log.warning("passkey assertion failed for user %s: %s", user_id, err)
            self._store.log(actor, "passkey.verify", credential_id or "", outcome="error")
            raise PasskeyError("verification failed") from err

        # A counter that does not advance is how a cloned credential shows up.
        # Some authenticators legitimately report 0 always; only a decrease from
        # a previously non-zero value is evidence.
        if row["sign_count"] and verified.new_sign_count <= row["sign_count"]:
            self._store.log(actor, "passkey.verify", credential_id or "",
                            {"reason": "sign counter did not advance"}, outcome="error")
            raise PasskeyError("verification failed")

        self._conn.execute(
            "UPDATE webauthn_credential SET sign_count = ?, last_used_at = ?"
            " WHERE credential_id = ?",
            (verified.new_sign_count, time.time(), credential_id),
        )
        self._conn.commit()
        self._verified_at[user_id] = time.time()

    def recently_verified(self, user_id: int) -> bool:
        last = self._verified_at.get(user_id)
        return last is not None and (time.time() - last) <= VERIFICATION_WINDOW_SECONDS

    def clear_verification(self, user_id: int) -> None:
        self._verified_at.pop(user_id, None)
