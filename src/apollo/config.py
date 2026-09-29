"""Settings loaded from .env (project root) and the environment."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Apollo"
TOKEN_FILE = SUPPORT_DIR / "token"

# override=True: the project .env wins over stale keys exported in the shell.
load_dotenv(PROJECT_ROOT / ".env", override=True)


def _list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    user_name: str = os.getenv("APOLLO_USER_NAME", "Vibhor")

    agent_model: str = os.getenv("APOLLO_AGENT_MODEL", "claude-opus-5")
    agent_effort: str = os.getenv("APOLLO_AGENT_EFFORT", "high")
    polish_model: str = os.getenv("APOLLO_POLISH_MODEL", "claude-opus-5")

    realtime_model: str = os.getenv("APOLLO_REALTIME_MODEL", "gpt-realtime-2.1")
    realtime_voice: str = os.getenv("APOLLO_VOICE", "marin")
    realtime_effort: str = os.getenv("APOLLO_REALTIME_EFFORT", "low")
    realtime_transcribe_model: str = os.getenv("APOLLO_REALTIME_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
    dictation_model: str = os.getenv("APOLLO_DICTATION_MODEL", "gpt-transcribe")
    dictation_polish: bool = _bool("APOLLO_DICTATION_POLISH", True)
    keywords: list[str] = field(default_factory=lambda: _list("APOLLO_KEYWORDS", "Apollo,Composio,Claude"))

    composio_user_id: str = os.getenv("APOLLO_COMPOSIO_USER_ID", "vibhor")
    composio_toolkits: list[str] = field(default_factory=lambda: _list("APOLLO_COMPOSIO_TOOLKITS"))

    host: str = os.getenv("APOLLO_HOST", "127.0.0.1")
    port: int = int(os.getenv("APOLLO_PORT", "8765"))

    def missing_keys(self) -> list[str]:
        return [k for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "COMPOSIO_API_KEY") if not os.getenv(k)]


settings = Settings()


def new_session_token() -> str:
    """Per-launch secret the notch app reads to authenticate to the local WebSocket."""
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    return token
