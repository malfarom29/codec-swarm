"""The command queue between the dashboard and the worker process.

The dashboard never runs a mission: it writes a command (start, answer a gate, restart…) and returns.
The worker claims commands, at most one at a time per mission, runs them and records how they ended.
Everything they produce reaches the dashboard through the event log, as before.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket TEXT NOT NULL,
    kind TEXT NOT NULL,
    args TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed | interrupted
    error TEXT,
    worker TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS commands_pending ON commands (status, id);
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    beat_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""
ALIVE_SECONDS = 10  # a worker that hasn't beaten for this long is considered gone


class Command(BaseModel):
    id: int
    ticket: str
    kind: str
    args: dict[str, Any]
    status: str
    error: str | None = None


class CommandQueue:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    # --- dashboard side ------------------------------------------------------------------

    def submit(self, ticket: str, kind: str, args: dict[str, Any] | None = None) -> int:
        cur = self._conn.execute("INSERT INTO commands (ticket, kind, args) VALUES (?, ?, ?)", (ticket, kind, json.dumps(args or {})))
        return int(cur.lastrowid)

    def is_busy(self, ticket: str) -> bool:
        """Something for this mission is queued or running in the worker."""
        row = self._conn.execute("SELECT 1 FROM commands WHERE ticket = ? AND status IN ('pending', 'running') LIMIT 1", (ticket,)).fetchone()
        return row is not None

    def worker_alive(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM workers WHERE beat_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?) LIMIT 1", (f"-{ALIVE_SECONDS} seconds",)
        ).fetchone()
        return row is not None

    def get(self, command_id: int) -> Command | None:
        row = self._conn.execute("SELECT id, ticket, kind, args, status, error FROM commands WHERE id = ?", (command_id,)).fetchone()
        return _command(row) if row else None

    def pending_count(self) -> int:
        return self._conn.execute("SELECT count(*) FROM commands WHERE status = 'pending'").fetchone()[0]

    # --- worker side -----------------------------------------------------------------------

    def register(self, worker: str) -> list[Command]:
        """Start a worker: commands a dead worker left running are marked interrupted and returned."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for pid, in self._conn.execute(
                "SELECT pid FROM workers WHERE beat_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)", (f"-{ALIVE_SECONDS} seconds",)
            ).fetchall():
                if pid != os.getpid() and _pid_alive(pid):
                    raise WorkerRunning(f"another worker (pid {pid}) is already running missions for this workspace")
            rows = self._conn.execute("SELECT id, ticket, kind, args, status, error FROM commands WHERE status = 'running' ORDER BY id").fetchall()
            self._conn.execute(
                "UPDATE commands SET status = 'interrupted', finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE status = 'running'"
            )
            self._conn.execute("DELETE FROM workers")
            self._conn.execute("INSERT INTO workers (id, pid) VALUES (?, ?)", (worker, os.getpid()))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return [_command(r) for r in rows]

    def beat(self, worker: str) -> None:
        self._conn.execute("UPDATE workers SET beat_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (worker,))

    def retire(self, worker: str) -> None:
        self._conn.execute("DELETE FROM workers WHERE id = ?", (worker,))

    def claim(self, worker: str) -> Command | None:
        """The oldest pending command whose mission has nothing running, marked running. Atomic across processes."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                """SELECT id, ticket, kind, args, status, error FROM commands
                   WHERE status = 'pending' AND ticket NOT IN (SELECT ticket FROM commands WHERE status = 'running')
                   ORDER BY id LIMIT 1"""
            ).fetchone()
            if row is not None:
                self._conn.execute(
                    "UPDATE commands SET status = 'running', worker = ?, started_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                    (worker, row[0]),
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return _command(row) if row else None

    def finish(self, command_id: int, error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE commands SET status = ?, error = ?, finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            ("failed" if error else "done", error, command_id),
        )


class WorkerRunning(RuntimeError):
    """A second worker on the same workspace would run every mission twice."""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _command(row: tuple) -> Command:
    return Command(id=row[0], ticket=row[1], kind=row[2], args=json.loads(row[3]), status=row[4], error=row[5])
