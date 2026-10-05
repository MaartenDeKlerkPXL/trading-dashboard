"""Secrets from the .env file (never committed). Values are never logged or sent to the dashboard."""

from __future__ import annotations

import os
from pathlib import Path

from .config import ROOT_DIR

ENV_PATH = ROOT_DIR / ".env"


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    """KEY=value lines from .env; real environment variables take precedence."""
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    for key in list(values):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values
