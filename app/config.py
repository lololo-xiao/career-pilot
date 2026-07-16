import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


DEFAULT_FRONTEND_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)


def get_frontend_origins() -> list[str]:
    """Read a comma-separated allowlist for browser clients."""

    load_dotenv()
    configured = os.getenv("FRONTEND_ORIGINS")
    if not configured:
        return list(DEFAULT_FRONTEND_ORIGINS)

    return [
        origin.strip().rstrip("/")
        for origin in configured.split(",")
        if origin.strip()
    ]


def get_auth_secret() -> str:
    """Return the application secret used for credential encryption."""

    load_dotenv()
    secret = os.getenv("CAREERPILOT_AUTH_SECRET", "").strip()
    placeholders = {
        "",
        "replace-with-at-least-32-random-characters",
    }
    if secret in placeholders or len(secret) < 32:
        raise RuntimeError(
            "CAREERPILOT_AUTH_SECRET must contain at least 32 random characters"
        )
    return secret


def get_auth_database_path() -> Path:
    load_dotenv()
    return Path(os.getenv("AUTH_DATABASE_PATH", ".data/careerpilot.db")).expanduser()


def get_codex_binary() -> str:
    load_dotenv()
    return os.getenv("CODEX_BINARY", "codex").strip() or "codex"


def get_codex_login_timeout_seconds() -> int:
    load_dotenv()
    configured = os.getenv("CODEX_LOGIN_TIMEOUT_SECONDS", "900")
    try:
        seconds = int(configured)
    except ValueError as exc:
        raise RuntimeError("CODEX_LOGIN_TIMEOUT_SECONDS must be an integer") from exc
    if not 60 <= seconds <= 3600:
        raise RuntimeError(
            "CODEX_LOGIN_TIMEOUT_SECONDS must be between 60 and 3600"
        )
    return seconds


def get_internal_api_url() -> str:
    """Return the loopback URL Hermes may use to call CareerPilot."""

    load_dotenv()
    value = os.getenv(
        "CAREERPILOT_INTERNAL_API_URL",
        "http://127.0.0.1:8000",
    ).strip().rstrip("/")
    parts = urlsplit(value)
    if (
        parts.scheme != "http"
        or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parts.username
        or parts.password
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise RuntimeError("CAREERPILOT_INTERNAL_API_URL must be a loopback HTTP URL")
    return value
