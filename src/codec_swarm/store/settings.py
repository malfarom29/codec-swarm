"""Machine-local settings edited from the dashboard: orchestration, harness overrides, Jira connection.

Secrets never land here: the Jira API token goes to ~/.codec-swarm/.env (see workspace.secrets).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


class Orchestration(BaseModel):
    jev_enabled: bool = True  # global switch; a missing TYPESAFE_API_KEY turns Jev off regardless
    gate_threshold: float = 0.90
    gate_margin: float = 0.03


class RoleOverride(BaseModel):
    """A machine-local tweak to one role. It can change the model and add capabilities, never remove them."""

    model: str | None = None
    extra_mcp: list[str] = []
    extra_skills: list[str] = []


class JiraSettings(BaseModel):
    site: str = ""  # https://<you>.atlassian.net
    email: str = ""
    jql: str = "assignee = currentUser() AND issuetype in (Task, subTaskIssueTypes()) AND statusCategory != Done ORDER BY updated DESC"
    project: str = ""  # for the quick request form


class Settings:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def get(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value: Any) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (key, json.dumps(value, default=str)),
            )

    def orchestration(self) -> Orchestration:
        return Orchestration.model_validate(self.get("orchestration", {}))

    def set_orchestration(self, value: Orchestration) -> None:
        self.put("orchestration", value.model_dump())

    def role_override(self, pack: str, role: str) -> RoleOverride:
        return RoleOverride.model_validate(self.get(f"harness.{pack}.{role}", {}))

    def set_role_override(self, pack: str, role: str, value: RoleOverride) -> None:
        self.put(f"harness.{pack}.{role}", value.model_dump())

    def jira(self) -> JiraSettings:
        return JiraSettings.model_validate(self.get("jira", {}))

    def set_jira(self, value: JiraSettings) -> None:
        self.put("jira", value.model_dump())
