"""Intent storage.

The database holds what the building *should* be doing; the thermostats hold what
they are currently doing. Those are different things and the gap between them is
the reconciler's job.

This has to be the source of truth rather than the devices, for three reasons
established by testing:

* The writable points have no BACnet priority array, so there is no protocol-level
  arbitration between us and anything else writing them.
* Devices go offline (they are on Wi-Fi) and must converge when they return.
* Writes can be applied without being acknowledged, so "did that land?" is only
  answerable by comparing intent against a subsequent read.

Plain sqlite3 rather than an ORM: the schema is small, the queries are simple, and
keeping the SQL visible makes the eventual move to Postgres a contained change.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS holiday (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    rule_type     TEXT    NOT NULL CHECK (rule_type IN ('fixed','range','floating')),
    -- fixed:    month, day, and optionally year (null year = every year)
    -- range:    month/day .. end_month/end_day, optionally year
    -- floating: month, week_of_month (5 = last), day_of_week (1=Mon..7=Sun)
    year          INTEGER,
    month         INTEGER,
    day           INTEGER,
    end_month     INTEGER,
    end_day       INTEGER,
    week_of_month INTEGER,
    day_of_week   INTEGER,
    -- Schedule enum, NOT the occupancy enum: 0=Occupied, 1=Unoccupied, 3=Standby
    state         INTEGER NOT NULL DEFAULT 1,
    zone          TEXT    NOT NULL DEFAULT '*',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    REAL    NOT NULL,
    created_by    TEXT    NOT NULL DEFAULT 'system'
);

CREATE TABLE IF NOT EXISTS setpoint_intent (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT    NOT NULL CHECK (scope IN ('device','zone','global')),
    scope_ref  TEXT    NOT NULL,
    point_key  TEXT    NOT NULL,
    value      REAL    NOT NULL,
    updated_at REAL    NOT NULL,
    updated_by TEXT    NOT NULL DEFAULT 'system',
    UNIQUE (scope, scope_ref, point_key)
);

-- A named weekly pattern. Devices point at one of these; a handful of groups
-- ("Standard", "Extended evening", "Six-day") covers most of a building.
CREATE TABLE IF NOT EXISTS schedule_group (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    description TEXT,
    created_at  REAL    NOT NULL
);

-- One row per day per group. `transitions` is a JSON list of {"time","state"},
-- e.g. [{"time":"06:00","state":0},{"time":"18:00","state":1}]. Stored whole
-- because a day is always read and written as a unit.
CREATE TABLE IF NOT EXISTS schedule_group_day (
    group_id    INTEGER NOT NULL REFERENCES schedule_group(id) ON DELETE CASCADE,
    day_of_week INTEGER NOT NULL CHECK (day_of_week BETWEEN 1 AND 7),  -- 1=Mon
    transitions TEXT    NOT NULL,
    PRIMARY KEY (group_id, day_of_week)
);

-- Which group a device follows.
CREATE TABLE IF NOT EXISTS device_schedule (
    device_id INTEGER PRIMARY KEY,
    group_id  INTEGER NOT NULL REFERENCES schedule_group(id),
    updated_at REAL   NOT NULL,
    updated_by TEXT   NOT NULL DEFAULT 'system'
);

-- Per-device deviation from its group, for a single day. Keeping overrides at
-- day granularity means a group-wide change still reaches every day the tenant
-- has not specifically customised.
CREATE TABLE IF NOT EXISTS device_day_override (
    device_id   INTEGER NOT NULL,
    day_of_week INTEGER NOT NULL CHECK (day_of_week BETWEEN 1 AND 7),
    transitions TEXT    NOT NULL,
    updated_at  REAL    NOT NULL,
    updated_by  TEXT    NOT NULL DEFAULT 'system',
    PRIMARY KEY (device_id, day_of_week)
);

-- Dated one-offs, created by a manager: "the office is open Sat 8 Aug 08:00-13:00".
-- Distinct from a holiday (a recurring rule) and from a bypass (immediate and
-- self-expiring). These become BACnet schedule exceptions with inline dates.
CREATE TABLE IF NOT EXISTS occupancy_exception (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    scope       TEXT    NOT NULL CHECK (scope IN ('device','zone','global')),
    scope_ref   TEXT    NOT NULL DEFAULT '*',
    start_date  TEXT    NOT NULL,              -- ISO yyyy-mm-dd
    end_date    TEXT,                          -- null = single day
    transitions TEXT    NOT NULL,              -- same JSON shape as a day
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL    NOT NULL,
    created_by  TEXT    NOT NULL DEFAULT 'system'
);

CREATE INDEX IF NOT EXISTS idx_exception_dates ON occupancy_exception (start_date, end_date);

-- Roles are fixed rather than a permission matrix: three of them cover this
-- building, and a matrix nobody audits is worse than three roles everyone
-- understands.
--   admin   -- everything, including user management
--   manager -- schedules, holidays, setpoints, exceptions, every zone
--   tenant  -- bypass only, and only in zones granted below
CREATE TABLE IF NOT EXISTS app_user (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT    NOT NULL DEFAULT '',
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL CHECK (role IN ('admin','manager','tenant')),
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    REAL    NOT NULL,
    last_login    REAL
);

-- Which zones a tenant may act on. Managers and admins ignore this table.
CREATE TABLE IF NOT EXISTS user_zone (
    user_id INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    zone    TEXT    NOT NULL,
    PRIMARY KEY (user_id, zone)
);

-- Sessions are server-side so they can be revoked. Only a hash of the cookie
-- value is stored, so a copy of this database does not hand over live sessions.
CREATE TABLE IF NOT EXISTS session (
    token_hash TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    created_at REAL    NOT NULL,
    expires_at REAL    NOT NULL,
    user_agent TEXT
);

CREATE INDEX IF NOT EXISTS idx_session_user ON session (user_id);

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,
    target   TEXT,
    detail   TEXT,
    outcome  TEXT NOT NULL DEFAULT 'ok'
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit (ts DESC);
"""


@dataclass
class Holiday:
    id: int
    name: str
    rule_type: str
    state: int
    zone: str
    enabled: bool
    year: int | None = None
    month: int | None = None
    day: int | None = None
    end_month: int | None = None
    end_day: int | None = None
    week_of_month: int | None = None
    day_of_week: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class Store:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL so the reconciler reading does not block the API writing.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- audit ------------------------------------------------------------------

    def log(
        self,
        actor: str,
        action: str,
        target: str | None = None,
        detail: Any = None,
        outcome: str = "ok",
    ) -> None:
        self._conn.execute(
            "INSERT INTO audit (ts, actor, action, target, detail, outcome)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                actor,
                action,
                target,
                json.dumps(detail) if detail is not None else None,
                outcome,
            ),
        )
        self._conn.commit()

    def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM audit ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            out.append(d)
        return out

    # --- holidays ---------------------------------------------------------------

    def add_holiday(self, actor: str = "system", **fields: Any) -> int:
        cols = (
            "name", "rule_type", "year", "month", "day", "end_month", "end_day",
            "week_of_month", "day_of_week", "state", "zone", "enabled",
        )
        values = {c: fields.get(c) for c in cols}
        values["state"] = fields.get("state", 1)
        values["zone"] = fields.get("zone", "*")
        values["enabled"] = int(fields.get("enabled", True))

        cur = self._conn.execute(
            f"INSERT INTO holiday ({', '.join(cols)}, created_at, created_by)"
            f" VALUES ({', '.join('?' for _ in cols)}, ?, ?)",
            (*[values[c] for c in cols], time.time(), actor),
        )
        self._conn.commit()
        holiday_id = int(cur.lastrowid)
        self.log(actor, "holiday.add", str(holiday_id), values)
        return holiday_id

    def delete_holiday(self, holiday_id: int, actor: str = "system") -> bool:
        cur = self._conn.execute("DELETE FROM holiday WHERE id = ?", (holiday_id,))
        self._conn.commit()
        if cur.rowcount:
            self.log(actor, "holiday.delete", str(holiday_id))
        return bool(cur.rowcount)

    def holidays(self, zone: str | None = None, enabled_only: bool = True) -> list[Holiday]:
        sql = "SELECT * FROM holiday"
        clauses, params = [], []
        if enabled_only:
            clauses.append("enabled = 1")
        if zone is not None:
            # '*' applies everywhere, so a zone query must include it.
            clauses.append("(zone = ? OR zone = '*')")
            params.append(zone)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY month, day, id"

        return [
            Holiday(
                id=r["id"], name=r["name"], rule_type=r["rule_type"], state=r["state"],
                zone=r["zone"], enabled=bool(r["enabled"]), year=r["year"],
                month=r["month"], day=r["day"], end_month=r["end_month"],
                end_day=r["end_day"], week_of_month=r["week_of_month"],
                day_of_week=r["day_of_week"],
            )
            for r in self._conn.execute(sql, params).fetchall()
        ]

    # --- setpoint intent --------------------------------------------------------

    def set_setpoint(
        self, scope: str, scope_ref: str, point_key: str, value: float, actor: str = "system"
    ) -> None:
        self._conn.execute(
            "INSERT INTO setpoint_intent (scope, scope_ref, point_key, value, updated_at,"
            " updated_by) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (scope, scope_ref, point_key) DO UPDATE SET"
            " value = excluded.value, updated_at = excluded.updated_at,"
            " updated_by = excluded.updated_by",
            (scope, scope_ref, point_key, value, time.time(), actor),
        )
        self._conn.commit()
        self.log(actor, "setpoint.set", f"{scope}:{scope_ref}", {point_key: value})

    def setpoints_for(self, device_id: int, zone: str) -> dict[str, float]:
        """Resolve intent for one device, most specific scope winning."""
        resolved: dict[str, float] = {}
        for scope, ref in (("global", "*"), ("zone", zone), ("device", str(device_id))):
            for r in self._conn.execute(
                "SELECT point_key, value FROM setpoint_intent WHERE scope = ? AND scope_ref = ?",
                (scope, ref),
            ):
                resolved[r["point_key"]] = r["value"]
        return resolved

    # --- schedule groups --------------------------------------------------------

    def add_group(
        self, name: str, week: dict[int, list[dict]], description: str = "", actor: str = "system"
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO schedule_group (name, description, created_at) VALUES (?, ?, ?)",
            (name, description, time.time()),
        )
        group_id = int(cur.lastrowid)
        for day, transitions in week.items():
            self._conn.execute(
                "INSERT INTO schedule_group_day (group_id, day_of_week, transitions)"
                " VALUES (?, ?, ?)",
                (group_id, day, json.dumps(transitions)),
            )
        self._conn.commit()
        self.log(actor, "group.add", name, {"id": group_id, "days": len(week)})
        return group_id

    def set_group_day(
        self, group_id: int, day_of_week: int, transitions: list[dict], actor: str = "system"
    ) -> None:
        self._conn.execute(
            "INSERT INTO schedule_group_day (group_id, day_of_week, transitions)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT (group_id, day_of_week) DO UPDATE SET transitions = excluded.transitions",
            (group_id, day_of_week, json.dumps(transitions)),
        )
        self._conn.commit()
        self.log(actor, "group.set_day", str(group_id), {"day": day_of_week, "t": transitions})

    def groups(self) -> list[dict[str, Any]]:
        out = []
        for g in self._conn.execute("SELECT * FROM schedule_group ORDER BY name"):
            days = {
                r["day_of_week"]: json.loads(r["transitions"])
                for r in self._conn.execute(
                    "SELECT day_of_week, transitions FROM schedule_group_day WHERE group_id = ?",
                    (g["id"],),
                )
            }
            out.append({"id": g["id"], "name": g["name"], "description": g["description"],
                        "week": days})
        return out

    def group_week(self, group_id: int) -> dict[int, list[dict]]:
        return {
            r["day_of_week"]: json.loads(r["transitions"])
            for r in self._conn.execute(
                "SELECT day_of_week, transitions FROM schedule_group_day WHERE group_id = ?",
                (group_id,),
            )
        }

    def assign_group(self, device_id: int, group_id: int, actor: str = "system") -> None:
        self._conn.execute(
            "INSERT INTO device_schedule (device_id, group_id, updated_at, updated_by)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (device_id) DO UPDATE SET group_id = excluded.group_id,"
            " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (device_id, group_id, time.time(), actor),
        )
        self._conn.commit()
        self.log(actor, "device.assign_group", str(device_id), {"group_id": group_id})

    def group_for_device(self, device_id: int) -> int | None:
        r = self._conn.execute(
            "SELECT group_id FROM device_schedule WHERE device_id = ?", (device_id,)
        ).fetchone()
        return int(r["group_id"]) if r else None

    # --- per-device day overrides ----------------------------------------------

    def set_day_override(
        self, device_id: int, day_of_week: int, transitions: list[dict], actor: str = "system"
    ) -> None:
        self._conn.execute(
            "INSERT INTO device_day_override (device_id, day_of_week, transitions,"
            " updated_at, updated_by) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (device_id, day_of_week) DO UPDATE SET"
            " transitions = excluded.transitions, updated_at = excluded.updated_at,"
            " updated_by = excluded.updated_by",
            (device_id, day_of_week, json.dumps(transitions), time.time(), actor),
        )
        self._conn.commit()
        self.log(actor, "device.day_override", str(device_id),
                 {"day": day_of_week, "transitions": transitions})

    def clear_day_override(self, device_id: int, day_of_week: int, actor: str = "system") -> bool:
        cur = self._conn.execute(
            "DELETE FROM device_day_override WHERE device_id = ? AND day_of_week = ?",
            (device_id, day_of_week),
        )
        self._conn.commit()
        if cur.rowcount:
            self.log(actor, "device.clear_override", str(device_id), {"day": day_of_week})
        return bool(cur.rowcount)

    def day_overrides(self, device_id: int) -> dict[int, list[dict]]:
        return {
            r["day_of_week"]: json.loads(r["transitions"])
            for r in self._conn.execute(
                "SELECT day_of_week, transitions FROM device_day_override WHERE device_id = ?",
                (device_id,),
            )
        }

    # --- dated one-off exceptions ----------------------------------------------

    def add_exception(
        self,
        name: str,
        start_date: str,
        transitions: list[dict],
        end_date: str | None = None,
        scope: str = "global",
        scope_ref: str = "*",
        actor: str = "system",
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO occupancy_exception (name, scope, scope_ref, start_date, end_date,"
            " transitions, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, scope, scope_ref, start_date, end_date, json.dumps(transitions),
             time.time(), actor),
        )
        self._conn.commit()
        exception_id = int(cur.lastrowid)
        self.log(actor, "exception.add", name,
                 {"id": exception_id, "start": start_date, "end": end_date})
        return exception_id

    def delete_exception(self, exception_id: int, actor: str = "system") -> bool:
        cur = self._conn.execute("DELETE FROM occupancy_exception WHERE id = ?", (exception_id,))
        self._conn.commit()
        if cur.rowcount:
            self.log(actor, "exception.delete", str(exception_id))
        return bool(cur.rowcount)

    def all_exceptions(self, upcoming_only: bool = False) -> list[dict[str, Any]]:
        """Every exception regardless of scope, for the management view."""
        sql = "SELECT * FROM occupancy_exception"
        params: list[Any] = []
        if upcoming_only:
            sql += " WHERE COALESCE(end_date, start_date) >= ?"
            params.append(dt.date.today().isoformat())
        sql += " ORDER BY start_date"
        return [
            {**dict(r), "transitions": json.loads(r["transitions"])}
            for r in self._conn.execute(sql, params).fetchall()
        ]

    def exceptions_for(
        self, device_id: int, zone: str, on_or_after: str | None = None
    ) -> list[dict[str, Any]]:
        """Exceptions applying to a device, optionally only current/future ones.

        Past exceptions are filtered out rather than deleted, so the audit trail
        keeps them, but they stop consuming space in the device's exception list.
        """
        sql = (
            "SELECT * FROM occupancy_exception WHERE enabled = 1 AND ("
            " (scope = 'global') OR (scope = 'zone' AND scope_ref = ?)"
            " OR (scope = 'device' AND scope_ref = ?))"
        )
        params: list[Any] = [zone, str(device_id)]
        if on_or_after:
            sql += " AND COALESCE(end_date, start_date) >= ?"
            params.append(on_or_after)
        sql += " ORDER BY start_date"

        return [
            {**dict(r), "transitions": json.loads(r["transitions"])}
            for r in self._conn.execute(sql, params).fetchall()
        ]
