"""Command gate decisions per repo, keyed by a hash of the resolved script: the same script gets the same answer."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from pydantic import BaseModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS gate_decisions (
    repo TEXT NOT NULL,
    script_hash TEXT NOT NULL,
    resolved TEXT NOT NULL,
    action TEXT NOT NULL,
    source TEXT NOT NULL,
    score REAL,
    band TEXT,
    decided_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (repo, script_hash)
);
"""


class CachedDecision(BaseModel, frozen=True):
    action: str
    source: str
    score: float | None
    band: str | None


def script_hash(resolved: str) -> str:
    return hashlib.sha256(resolved.encode()).hexdigest()


class GateCache:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def get(self, repo: str, resolved: str) -> CachedDecision | None:
        row = self._conn.execute(
            "SELECT action, source, score, band FROM gate_decisions WHERE repo = ? AND script_hash = ?",
            (repo, script_hash(resolved)),
        ).fetchone()
        return CachedDecision(action=row[0], source=row[1], score=row[2], band=row[3]) if row else None

    def put(self, repo: str, resolved: str, action: str, source: str, score: float | None = None, band: str | None = None) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO gate_decisions (repo, script_hash, resolved, action, source, score, band) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (repo, script_hash) DO UPDATE SET action = excluded.action, source = excluded.source,
                score = excluded.score, band = excluded.band, decided_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')""",
                (repo, script_hash(resolved), resolved, action, source, score, band),
            )

    def close(self) -> None:
        self._conn.close()
