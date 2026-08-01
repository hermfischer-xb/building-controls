"""Authentication, sessions and role checks.

Session cookies rather than bearer tokens, because the primary client is a phone
opening a plain HTML page. A tenant taps a bookmark, logs in once, and stays
logged in; asking that page to manage a token in JavaScript would add failure
modes for no benefit.

Passwords use scrypt from the standard library. It is a real memory-hard KDF, so
this avoids a dependency without settling for a fast hash.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

COOKIE_NAME = "bms_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600  # a tenant should not re-auth every week

# scrypt parameters. n=2**14 costs ~50ms per verification here, which is a fine
# trade for a login that happens rarely.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}

ROLES = ("admin", "manager", "tenant")
# Everything a manager can do, an admin can too.
_RANK = {"tenant": 0, "manager": 1, "admin": 2}


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str
    role: str
    zones: tuple[str, ...]

    def at_least(self, role: str) -> bool:
        return _RANK[self.role] >= _RANK[role]

    def may_access_zone(self, zone: str) -> bool:
        """Managers and admins see the whole building; tenants see their zones."""
        if self.at_least("manager"):
            return True
        return zone in self.zones or "*" in self.zones

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "zones": list(self.zones),
        }


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, salt_hex, key_hex = stored.split("$")
        if algorithm != "scrypt":
            return False
        key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(key.hex(), key_hex)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LoginThrottle:
    """Slow down credential stuffing without locking a real user out for long.

    Keyed on username *and* client address so one attacker cannot deny service to
    a legitimate user by hammering their account from elsewhere.
    """

    def __init__(self, max_attempts: int = 8, window_seconds: float = 300.0) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._attempts: dict[str, list[float]] = {}

    def _prune(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._attempts.get(key, []) if now - t < self._window]
        self._attempts[key] = recent
        return recent

    def blocked(self, username: str, client: str) -> bool:
        key = f"{username.lower()}|{client}"
        return len(self._prune(key, time.time())) >= self._max

    def record_failure(self, username: str, client: str) -> None:
        key = f"{username.lower()}|{client}"
        now = time.time()
        self._prune(key, now).append(now)
        self._attempts[key].append(now)

    def clear(self, username: str, client: str) -> None:
        self._attempts.pop(f"{username.lower()}|{client}", None)


class AuthStore:
    """User and session persistence, over the same sqlite connection as Store."""

    def __init__(self, store) -> None:
        self._store = store
        self._conn = store._conn  # deliberate: one connection, one transaction scope

    # --- users ------------------------------------------------------------------

    def create_user(
        self,
        username: str,
        password: str,
        role: str,
        display_name: str = "",
        zones: list[str] | None = None,
        actor: str = "system",
    ) -> int:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        cur = self._conn.execute(
            "INSERT INTO app_user (username, display_name, password_hash, role, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (username, display_name or username, hash_password(password), role, time.time()),
        )
        user_id = int(cur.lastrowid)
        for zone in zones or []:
            self._conn.execute(
                "INSERT OR IGNORE INTO user_zone (user_id, zone) VALUES (?, ?)", (user_id, zone)
            )
        self._conn.commit()
        self._store.log(actor, "user.create", username, {"role": role, "zones": zones or []})
        return user_id

    def set_password(self, username: str, password: str, actor: str = "system") -> bool:
        cur = self._conn.execute(
            "UPDATE app_user SET password_hash = ? WHERE username = ?",
            (hash_password(password), username),
        )
        self._conn.commit()
        if cur.rowcount:
            # Changing a password kills existing sessions, which is the whole
            # point of changing it after a suspected compromise.
            self.revoke_all_sessions(username)
            self._store.log(actor, "user.set_password", username)
        return bool(cur.rowcount)

    def set_zones(self, username: str, zones: list[str], actor: str = "system") -> bool:
        row = self._conn.execute(
            "SELECT id FROM app_user WHERE username = ?", (username,)
        ).fetchone()
        if not row:
            return False
        self._conn.execute("DELETE FROM user_zone WHERE user_id = ?", (row["id"],))
        for zone in zones:
            self._conn.execute(
                "INSERT OR IGNORE INTO user_zone (user_id, zone) VALUES (?, ?)",
                (row["id"], zone),
            )
        self._conn.commit()
        self._store.log(actor, "user.set_zones", username, {"zones": zones})
        return True

    def deactivate(self, username: str, actor: str = "system") -> bool:
        cur = self._conn.execute(
            "UPDATE app_user SET active = 0 WHERE username = ?", (username,)
        )
        self._conn.commit()
        if cur.rowcount:
            self.revoke_all_sessions(username)
            self._store.log(actor, "user.deactivate", username)
        return bool(cur.rowcount)

    def users(self) -> list[dict[str, Any]]:
        out = []
        for r in self._conn.execute("SELECT * FROM app_user ORDER BY role, username"):
            zones = [
                z["zone"]
                for z in self._conn.execute(
                    "SELECT zone FROM user_zone WHERE user_id = ?", (r["id"],)
                )
            ]
            out.append({
                "username": r["username"], "display_name": r["display_name"],
                "role": r["role"], "active": bool(r["active"]),
                "zones": zones, "last_login": r["last_login"],
            })
        return out

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) c FROM app_user").fetchone()["c"])

    def _load(self, row) -> User:
        zones = tuple(
            z["zone"]
            for z in self._conn.execute(
                "SELECT zone FROM user_zone WHERE user_id = ?", (row["id"],)
            )
        )
        return User(
            id=row["id"], username=row["username"], display_name=row["display_name"],
            role=row["role"], zones=zones,
        )

    # --- authentication ---------------------------------------------------------

    def authenticate(self, username: str, password: str) -> User | None:
        row = self._conn.execute(
            "SELECT * FROM app_user WHERE username = ? AND active = 1", (username,)
        ).fetchone()
        if row is None:
            # Hash anyway so a missing user and a wrong password take the same
            # time, and the response cannot be used to enumerate accounts.
            hash_password("timing-equalising-dummy-value")
            return None
        if not verify_password(password, row["password_hash"]):
            return None

        self._conn.execute(
            "UPDATE app_user SET last_login = ? WHERE id = ?", (time.time(), row["id"])
        )
        self._conn.commit()
        return self._load(row)

    # --- sessions ---------------------------------------------------------------

    def create_session(self, user: User, user_agent: str | None = None) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self._conn.execute(
            "INSERT INTO session (token_hash, user_id, created_at, expires_at, user_agent)"
            " VALUES (?, ?, ?, ?, ?)",
            (hash_token(token), user.id, now, now + SESSION_TTL_SECONDS, user_agent),
        )
        self._conn.commit()
        return token

    def resolve_session(self, token: str) -> User | None:
        row = self._conn.execute(
            "SELECT s.expires_at, u.* FROM session s JOIN app_user u ON u.id = s.user_id"
            " WHERE s.token_hash = ? AND u.active = 1",
            (hash_token(token),),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] < time.time():
            self._conn.execute("DELETE FROM session WHERE token_hash = ?", (hash_token(token),))
            self._conn.commit()
            return None
        return self._load(row)

    def revoke_session(self, token: str) -> None:
        self._conn.execute("DELETE FROM session WHERE token_hash = ?", (hash_token(token),))
        self._conn.commit()

    def revoke_all_sessions(self, username: str) -> None:
        self._conn.execute(
            "DELETE FROM session WHERE user_id IN (SELECT id FROM app_user WHERE username = ?)",
            (username,),
        )
        self._conn.commit()

    def purge_expired_sessions(self) -> int:
        cur = self._conn.execute("DELETE FROM session WHERE expires_at < ?", (time.time(),))
        self._conn.commit()
        return cur.rowcount
