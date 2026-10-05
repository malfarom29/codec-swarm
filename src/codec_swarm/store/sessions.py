"""Claude Code session per role and lane, resumed by session_id."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    ticket TEXT NOT NULL,
    repo TEXT NOT NULL,
    role TEXT NOT NULL,
    session_id TEXT NOT NULL,
    model TEXT NOT NULL,
    turns INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (ticket, repo, role)
);
"""


class SessionStore:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def get(self, ticket: str, repo: str, role: str) -> str | None:
        row = self._conn.execute(
            "SELECT session_id FROM sessions WHERE ticket = ? AND repo = ? AND role = ?", (ticket, repo, role)
        ).fetchone()
        return row[0] if row else None

    def record(self, ticket: str, repo: str, role: str, session_id: str, model: str, turns: int, cost_usd: float) -> None:
        """Store the session and add this step's turns and cost to its totals."""
        with self._conn:
            self._conn.execute(
                """INSERT INTO sessions (ticket, repo, role, session_id, model, turns, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (ticket, repo, role) DO UPDATE SET session_id = excluded.session_id, model = excluded.model,
                turns = turns + excluded.turns, cost_usd = cost_usd + excluded.cost_usd,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')""",
                (ticket, repo, role, session_id, model, turns, cost_usd),
            )

    def close(self) -> None:
        self._conn.close()
