from __future__ import annotations

import re
import secrets
from contextlib import suppress
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from career_companion.paths import CompanionPaths


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1024, le=65535)
    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://127.0.0.1:8787", "http://localhost:8787"]
    )
    allow_remote: bool = False

    @field_validator("host")
    @classmethod
    def enforce_loopback(cls, value: str) -> str:
        if value not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Remote binding requires the explicit advanced override")
        return value


class ProductConfig(BaseModel):
    version: int = 1
    server: ServerConfig = Field(default_factory=ServerConfig)
    hermes_executable: str = "hermes"
    tectonic_executable: str = "tectonic"
    playwright_chromium_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    hermes_api_port: int = 8788
    hermes_startup_timeout_seconds: float = Field(default=15.0, ge=1, le=60)
    daily_api_budget_usd: float = Field(default=2.0, ge=0)
    adjacent_claims_allowed: bool = True
    claim_posture: str = "aggressive-but-defensible"
    mcp_env_allowlist: list[str] = Field(default_factory=list)

    @field_validator("mcp_env_allowlist")
    @classmethod
    def validate_mcp_env_names(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            name = value.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"Invalid environment variable name: {value}")
            if name not in result:
                result.append(name)
        return result


def load_config(paths: CompanionPaths | None = None) -> ProductConfig:
    paths = paths or CompanionPaths.discover()
    if not paths.config.exists():
        return ProductConfig()
    data = yaml.safe_load(paths.config.read_text(encoding="utf-8")) or {}
    config = ProductConfig.model_validate(data)
    if config.server.allow_remote:
        # The advanced override deliberately bypasses the normal validator.
        raw_host = data.get("server", {}).get("host", config.server.host)
        object.__setattr__(config.server, "host", str(raw_host))
    return config


def save_config(config: ProductConfig, paths: CompanionPaths | None = None) -> None:
    paths = paths or CompanionPaths.discover()
    paths.create()
    payload: dict[str, Any] = config.model_dump(mode="json")
    paths.config.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def ensure_session_token(paths: CompanionPaths | None = None) -> str:
    paths = paths or CompanionPaths.discover()
    paths.create()
    if paths.session_token.exists():
        return paths.session_token.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    paths.session_token.write_text(token, encoding="utf-8")
    with suppress(OSError):
        paths.session_token.chmod(0o600)
    return token


def public_settings(config: ProductConfig) -> dict[str, Any]:
    return {
        "version": config.version,
        "server": {
            "host": config.server.host,
            "port": config.server.port,
            "loopback_only": not config.server.allow_remote,
        },
        "daily_api_budget_usd": config.daily_api_budget_usd,
        "adjacent_claims_allowed": config.adjacent_claims_allowed,
        "claim_posture": config.claim_posture,
    }
