"""Messages I send an agent from the dashboard. Each waits until that role's next step starts, then joins its prompt."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pydantic import BaseModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket TEXT NOT NULL,
    lane TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    delivered_at TEXT
);
"""


class ChatMessage(BaseModel, frozen=True):
    id: int
    ticket: str
    lane: str
    role: str
    text: str
    created_at: str
    delivered_at: str | None


class ChatStore:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def send(self, ticket: str, lane: str, role: str, text: str) -> int:
        with self._conn:
            cur = self._conn.execute("INSERT INTO chat (ticket, lane, role, text) VALUES (?, ?, ?, ?)", (ticket, lane, role, text))
        return int(cur.lastrowid)

    def take_pending(self, ticket: str, lane: str, role: str) -> list[ChatMessage]:
        """The messages waiting for this role, marked delivered."""
        rows = self.list(ticket, lane, role, pending_only=True)
        if rows:
            with self._conn:
                self._conn.executemany(
                    "UPDATE chat SET delivered_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", [(r.id,) for r in rows]
                )
        return rows

    def forget_pending(self, ticket: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM chat WHERE ticket = ? AND delivered_at IS NULL", (ticket,))

    def list(self, ticket: str, lane: str | None = None, role: str | None = None, pending_only: bool = False) -> list[ChatMessage]:
        query, params = "SELECT id, ticket, lane, role, text, created_at, delivered_at FROM chat WHERE ticket = ?", [ticket]
        if lane is not None:
            query, params = query + " AND lane = ?", [*params, lane]
        if role is not None:
            query, params = query + " AND role = ?", [*params, role]
        if pending_only:
            query += " AND delivered_at IS NULL"
        rows = self._conn.execute(query + " ORDER BY id", params).fetchall()
        return [ChatMessage(id=r[0], ticket=r[1], lane=r[2], role=r[3], text=r[4], created_at=r[5], delivered_at=r[6]) for r in rows]
