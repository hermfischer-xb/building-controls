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
import string
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

COOKIE_NAME = "bms_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600  # a tenant should not re-auth every week

# scrypt cost. OWASP's current guidance for scrypt is N=2**17, r=8, p=1; the
# original N=2**14 here was well below that. Measured on this hardware:
#
#   2^14   16 MB    32 ms      what this used to be
#   2^16   64 MB   136 ms
#   2^17  128 MB   286 ms      current
#
# 286 ms is nothing for a login that happens a few times a day, and it multiplies
# an offline attacker's cost by eight. It matters because people reuse passwords:
# the hash of a building login may also be the hash of something that holds money.
_SCRYPT_N = 2**17
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32

# hashlib.scrypt refuses anything above OpenSSL's 32 MB default unless told
# otherwise, so the limit has to be raised alongside N or it simply errors.
def _maxmem(n: int, r: int) -> int:
    return n * r * 128 * 2

ROLES = ("admin", "manager", "tenant")
# Everything a manager can do, an admin can too.
_RANK = {"tenant": 0, "manager": 1, "admin": 2}


def generate_password(length: int = 16) -> str:
    """A first password nobody had to invent.

    Used when one account creates another. The alternative -- an admin typing a
    password and reading it out -- means the admin knows a credential the owner
    may reuse elsewhere, and it cannot be un-known afterwards. Generated here,
    shown once, and `must_change_password` forces the owner to replace it before
    they can do anything.

    Letters and digits only: this gets read aloud, written on a sticky note and
    typed on a phone keyboard, and punctuation in that path causes more lockouts
    than the extra entropy is worth. 16 characters of this alphabet is ~95 bits.
    """
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str
    role: str
    zones: tuple[str, ...]
    must_change_password: bool = False

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
            "must_change_password": self.must_change_password,
        }


def hash_password(password: str) -> str:
    """Hash for storage. The cost parameters travel with the hash.

    The original format was `scrypt$salt$key`, which baked the parameters into
    the code -- so raising the cost would have invalidated every existing
    password. Recording them per hash means old ones keep verifying at their
    original cost while new ones use the current one.
    """
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_DKLEN, maxmem=_maxmem(_SCRYPT_N, _SCRYPT_R),
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${key.hex()}"


def _parse(stored: str) -> tuple[int, int, int, bytes, str] | None:
    parts = stored.split("$")
    if parts[0] != "scrypt":
        return None
    if len(parts) == 6:
        _, n, r, p, salt_hex, key_hex = parts
        return int(n), int(r), int(p), bytes.fromhex(salt_hex), key_hex
    if len(parts) == 3:
        # Legacy: parameters were implicit, and were N=2^14, r=8, p=1.
        _, salt_hex, key_hex = parts
        return 2**14, 8, 1, bytes.fromhex(salt_hex), key_hex
    return None


def verify_password(password: str, stored: str) -> bool:
    try:
        parsed = _parse(stored)
        if parsed is None:
            return False
        n, r, p, salt, key_hex = parsed
        key = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p,
                             dklen=_DKLEN, maxmem=_maxmem(n, r))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(key.hex(), key_hex)


def needs_rehash(stored: str) -> bool:
    """True when a stored hash predates the current cost parameters."""
    parsed = _parse(stored)
    if parsed is None:
        return True
    n, r, p, _, _ = parsed
    return (n, r, p) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P)


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
        # One append, not two. `_prune` stores the list it returns, so appending
        # to its return value and to `self._attempts[key]` are the same list --
        # writing both recorded every failure twice and halved the real budget to
        # four attempts. It failed closed rather than open, which is why it went
        # unnoticed: the throttle simply bit sooner than configured.
        self._prune(key, now).append(now)

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
        must_change: bool = True,
    ) -> int:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        cur = self._conn.execute(
            "INSERT INTO app_user (username, display_name, password_hash, role, created_at,"
            " must_change_password) VALUES (?, ?, ?, ?, ?, ?)",
            (username, display_name or username, hash_password(password), role, time.time(),
             int(must_change)),
        )
        user_id = int(cur.lastrowid)
        for zone in zones or []:
            self._conn.execute(
                "INSERT OR IGNORE INTO user_zone (user_id, zone) VALUES (?, ?)", (user_id, zone)
            )
        self._conn.commit()
        self._store.log(actor, "user.create", username, {"role": role, "zones": zones or []})
        return user_id

    def set_password(self, username: str, password: str, actor: str = "system",
                     must_change: bool | None = None) -> bool:
        """Set a password.

        `must_change` defaults to "whoever is doing this is not the owner", which
        is the case for every administrative reset: the person typing it learns
        the credential, so the owner has to replace it. Passing False is for
        `change_own_password`, where the owner chose it and nobody else saw it.
        """
        if must_change is None:
            must_change = actor.lower() != username.lower()
        cur = self._conn.execute(
            "UPDATE app_user SET password_hash = ?, must_change_password = ?"
            " WHERE username = ?",
            (hash_password(password), int(must_change), username),
        )
        self._conn.commit()
        if cur.rowcount:
            # Changing a password kills existing sessions, which is the whole
            # point of changing it after a suspected compromise.
            self.revoke_all_sessions(username)
            self._store.log(actor, "user.set_password", username)
        return bool(cur.rowcount)

    def change_own_password(self, user: User, current: str, new: str) -> bool:
        """The owner replacing their own password. Verifies the old one first.

        Separate from `set_password` because the checks differ: an admin reset
        does not know the old password and should not need it, while a user
        changing their own must prove they are not someone who sat down at an
        unlocked screen. Returns False if `current` is wrong.
        """
        row = self._conn.execute(
            "SELECT password_hash FROM app_user WHERE id = ?", (user.id,)
        ).fetchone()
        if row is None or not verify_password(current, row["password_hash"]):
            return False
        if current == new:
            raise ValueError("the new password must be different from the old one")

        self._conn.execute(
            "UPDATE app_user SET password_hash = ?, must_change_password = 0 WHERE id = ?",
            (hash_password(new), user.id),
        )
        self._conn.commit()
        # Every other session for this account is dropped: if the reason for the
        # change is that the old password leaked, leaving those alive defeats it.
        # The caller re-issues a session for the browser doing the changing.
        self.revoke_all_sessions(user.username)
        self._store.log(user.username, "user.password_changed")
        return True

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
                "must_change_password": bool(r["must_change_password"]),
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
            # Absent on a row read before the column existed.
            must_change_password=bool(row["must_change_password"])
            if "must_change_password" in row.keys() else False,
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

        # Upgrade the stored hash in place when it predates the current cost.
        # Doing it here is the only moment the plaintext is available, so the
        # alternative is asking every user to reset a password that is fine.
        if needs_rehash(row["password_hash"]):
            self._conn.execute(
                "UPDATE app_user SET password_hash = ? WHERE id = ?",
                (hash_password(password), row["id"]),
            )
            log.info("upgraded password hash cost for %s", row["username"])

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
