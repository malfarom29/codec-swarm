"""Secrets live in ~/.codec-swarm/.env (mode 600) or the environment; never in SQLite, events, handoffs or prompts."""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


def env_path(root: Path) -> Path:
    return Path(root).expanduser() / ".env"


def load_secrets(root: Path) -> None:
    """Make the root's .env visible to this process without overriding what the shell already set."""
    if env_path(root).exists():
        load_dotenv(env_path(root), override=False)


def save_secret(root: Path, key: str, value: str) -> None:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
        raise ValueError(f"{key} is not a valid variable name")
    path = env_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {k: v for k, v in dotenv_values(path).items() if v is not None} if path.exists() else {}
    values[key] = value
    path.write_text("".join(f'{k}="{v}"\n' for k, v in values.items()))
    path.chmod(0o600)
    os.environ[key] = value


def remove_secret(root: Path, key: str) -> None:
    path = env_path(root)
    if path.exists():
        values = {k: v for k, v in dotenv_values(path).items() if v is not None and k != key}
        path.write_text("".join(f'{k}="{v}"\n' for k, v in values.items()))
        path.chmod(0o600)
    os.environ.pop(key, None)
