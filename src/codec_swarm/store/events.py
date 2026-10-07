"""Append-only event log in SQLite. Pages follow it over server-sent events by monotonic id.

Most events are agent tool calls, so the log grows by thousands per mission: anything a page needs often
(the newest id, which missions exist, today's cost) is an indexed query, not a scan of every row.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission TEXT NOT NULL,
    kind TEXT NOT NULL,
    role TEXT,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS events_mission ON events (mission, id);
CREATE INDEX IF NOT EXISTS events_kind ON events (kind, id);
"""


class Event(BaseModel, frozen=True):
    id: int
    mission: str
    kind: str
    role: str | None
    payload: dict[str, Any]
    created_at: str


class EventLog:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def append(self, mission: str, kind: str, payload: dict[str, Any], role: str | None = None) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO events (mission, kind, role, payload) VALUES (?, ?, ?, ?)",
                (mission, kind, role, json.dumps(payload, default=str)),
            )
        return int(cur.lastrowid)

    def list(self, mission: str | None = None, since: int = 0) -> list[Event]:
        query = "SELECT id, mission, kind, role, payload, created_at FROM events WHERE id > ?"
        params: list[Any] = [since]
        if mission is not None:
            query += " AND mission = ?"
            params.append(mission)
        rows = self._conn.execute(query + " ORDER BY id", params).fetchall()
        return [Event(id=r[0], mission=r[1], kind=r[2], role=r[3], payload=json.loads(r[4]), created_at=r[5]) for r in rows]

    def last_id(self, mission: str | None = None) -> int:
        if mission is None:
            row = self._conn.execute("SELECT max(id) FROM events").fetchone()
        else:
            row = self._conn.execute("SELECT max(id) FROM events WHERE mission = ?", (mission,)).fetchone()
        return row[0] or 0

    def of_kind(self, kind: str, mission: str | None = None) -> list[Event]:
        query, params = "SELECT id, mission, kind, role, payload, created_at FROM events WHERE kind = ?", [kind]
        if mission is not None:
            query, params = query + " AND mission = ?", [*params, mission]
        rows = self._conn.execute(query + " ORDER BY id", params).fetchall()
        return [Event(id=r[0], mission=r[1], kind=r[2], role=r[3], payload=json.loads(r[4]), created_at=r[5]) for r in rows]

    def cost_since(self, created_at: str) -> float:
        row = self._conn.execute(
            "SELECT sum(json_extract(payload, '$.cost_usd')) FROM events WHERE kind = 'cost' AND created_at >= ?", (created_at,)
        ).fetchone()
        return float(row[0] or 0.0)

    def close(self) -> None:
        self._conn.close()
