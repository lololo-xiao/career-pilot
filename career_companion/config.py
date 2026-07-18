from __future__ import annotations

import re
import secrets
from contextlib import suppress
from typing import Any

import yaml
from pydantic import BaseModel, Field, StrictBool, field_validator, model_validator

from career_companion.paths import CompanionPaths


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1024, le=65535)
    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://127.0.0.1:8787", "http://localhost:8787"],
        max_length=64,
    )
    allow_remote: StrictBool = False

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def require_exact_origin_list(cls, value: object) -> object:
        if not isinstance(value, list):
            raise ValueError("Allowed origins must be an exact list")
        return value

    @model_validator(mode="after")
    def enforce_loopback(self) -> "ServerConfig":
        if not self.allow_remote and self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Remote binding requires the explicit advanced override")
        return self


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
    adjacent_claims_allowed: StrictBool = True
    claim_posture: str = "aggressive-but-defensible"
    mcp_env_allowlist: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("mcp_env_allowlist", mode="before")
    @classmethod
    def validate_mcp_env_names(cls, values: object) -> list[str]:
        if not isinstance(values, list) or any(
            not isinstance(value, str) for value in values
        ):
            raise ValueError("MCP environment allowlist must be an exact string list")
        result: list[str] = []
        for value in values:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                raise ValueError(f"Invalid environment variable name: {value}")
            if value not in result:
                result.append(value)
        return result


def load_config(paths: CompanionPaths | None = None) -> ProductConfig:
    paths = paths or CompanionPaths.discover()
    if not paths.config.exists():
        return ProductConfig()
    loaded = yaml.safe_load(paths.config.read_text(encoding="utf-8"))
    data = {} if loaded is None else loaded
    if not isinstance(data, dict):
        raise ValueError("Product configuration must be an exact mapping")
    return ProductConfig.model_validate(data)


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
