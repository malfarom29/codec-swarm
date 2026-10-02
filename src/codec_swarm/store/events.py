"""Append-only event log in SQLite. The UI replays it over WebSocket by monotonic id."""

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
        self._conn = sqlite3.connect(path, check_same_thread=False)
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

    def close(self) -> None:
        self._conn.close()
