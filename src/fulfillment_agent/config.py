"""Runtime configuration, read from environment variables (or a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so the project has no extra dependency for it."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_load_dotenv(PROJECT_ROOT / ".env")

STORE_TZ = ZoneInfo("America/Chicago")

# The synthetic data describes a single store day. "Now" is pinned so that
# questions like "orders due in the next two hours" have stable answers and
# the eval set stays reproducible.
OPS_NOW = datetime(2026, 10, 12, 13, 30, tzinfo=STORE_TZ)


@dataclass(frozen=True)
class Settings:
    admin_url: str = field(
        default_factory=lambda: os.getenv(
            "ADMIN_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/fulfillment"
        )
    )
    ro_url: str = field(
        default_factory=lambda: os.getenv(
            "AGENT_RO_DATABASE_URL", "postgresql://agent_ro:agent_ro@localhost:5432/fulfillment"
        )
    )
    rw_url: str = field(
        default_factory=lambda: os.getenv(
            "AGENT_RW_DATABASE_URL",
            "postgresql://agent_writer:agent_writer@localhost:5432/fulfillment",
        )
    )
    model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"))
    max_tokens: int = field(default_factory=lambda: int(os.getenv("CLAUDE_MAX_TOKENS", "2048")))
    max_turns: int = field(default_factory=lambda: int(os.getenv("AGENT_MAX_TURNS", "12")))
    sql_row_limit: int = 200


def get_settings() -> Settings:
    return Settings()
