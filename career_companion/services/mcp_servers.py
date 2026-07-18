from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from career_companion.database import AuditEventRecord, MCPServerRecord
from career_companion.hermes import PILOT_ALLOWED_MCP_TOOLS_ENV
from career_companion.paths import CompanionPaths
from career_companion.persistence import account_session
from career_companion.services.audit import record_audit


MCP_DEFAULTS_SEEDED_EVENT = "mcp.defaults_seeded"
MCP_RESERVED_ENVIRONMENT = {
    "API_SERVER_ENABLED",
    "API_SERVER_HOST",
    "API_SERVER_KEY",
    "API_SERVER_PORT",
    "BRAVE_SEARCH_API_KEY",
    "CAREER_COMPANION_ACCOUNT_KEY",
    PILOT_ALLOWED_MCP_TOOLS_ENV,
    "CAREER_COMPANION_API_URL",
    "CAREER_COMPANION_PLUGIN_TOKEN",
    "HERMES_HOME",
    "HERMES_WRITE_SAFE_ROOT",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
}

_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_MCP_NAME_COMPONENT = re.compile(r"[^A-Za-z0-9_]")


def _profile_directory(paths: CompanionPaths) -> Path:
    return paths.hermes_profile / "profiles" / "career-companion"


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("MCP configuration cannot be written through a symbolic link")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(mode)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _preset_payload(raw: dict[str, Any]) -> dict[str, Any] | None:
    name = raw.get("name")
    transport = raw.get("transport")
    if not isinstance(name, str) or transport not in {"stdio", "http"}:
        return None
    url = raw.get("url")
    forwarded_environment = raw.get("forwarded_environment", [])
    url_environment = raw.get("url_env")
    if isinstance(url_environment, str) and _ENVIRONMENT_NAME.fullmatch(url_environment):
        url = f"${{{url_environment}}}"
        forwarded_environment = [*forwarded_environment, url_environment]
    environment = raw.get("environment", raw.get("env", {}))
    return {
        "name": name,
        "display_name": raw.get("display_name") or name.replace("-", " ").title(),
        "description": raw.get("description") or "",
        "transport": transport,
        "command": raw.get("command"),
        "args": raw.get("args", []),
        "url": url,
        "tool_allowlist": raw.get("tool_allowlist", []),
        "forwarded_environment": list(dict.fromkeys(forwarded_environment)),
        "environment": environment if isinstance(environment, dict) else {},
        "source_url": raw.get("source_url"),
        "warning": raw.get("warning"),
        "enabled": bool(raw.get("enabled")),
        "preset": True,
    }


def load_mcp_presets(distribution: Path) -> list[dict[str, Any]]:
    path = distribution / "mcp.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    raw_presets = payload.get("presets") if isinstance(payload, dict) else None
    if not isinstance(raw_presets, list):
        return []
    presets: list[dict[str, Any]] = []
    for raw in raw_presets:
        if isinstance(raw, dict) and (preset := _preset_payload(raw)) is not None:
            presets.append(preset)
    return presets


def ensure_default_mcp_servers(session: Session, distribution: Path) -> None:
    marker = session.scalar(
        select(AuditEventRecord.id).where(
            AuditEventRecord.event_type == MCP_DEFAULTS_SEEDED_EVENT
        )
    )
    if marker is not None:
        return
    names: list[str] = []
    for preset in load_mcp_presets(distribution):
        name = str(preset["name"])
        config = {
            key: value
            for key, value in preset.items()
            if key not in {"name", "transport", "enabled"}
        }
        result = session.execute(
            sqlite_insert(MCPServerRecord)
            .values(
                name=name,
                transport=str(preset["transport"]),
                config=config,
                enabled=bool(preset["enabled"]),
            )
            .on_conflict_do_nothing(index_elements=[MCPServerRecord.name])
        )
        if result.rowcount:
            names.append(name)
    record_audit(
        session,
        MCP_DEFAULTS_SEEDED_EVENT,
        subject_type="mcp",
        subject_id="presets",
        payload={"servers": names},
    )


def mcp_server_json(row: MCPServerRecord) -> dict[str, Any]:
    config = row.config if isinstance(row.config, dict) else {}
    command = config.get("command")
    command_available = (
        bool(shutil.which(command)) if isinstance(command, str) and command else None
    )
    return {
        "name": row.name,
        "display_name": config.get("display_name")
        or row.name.replace("-", " ").title(),
        "description": config.get("description") or "",
        "transport": row.transport,
        "command": command,
        "args": config.get("args", []),
        "url": config.get("url"),
        "tool_allowlist": config.get("tool_allowlist", []),
        "forwarded_environment": config.get("forwarded_environment", []),
        "environment": config.get("environment", {}),
        "source_url": config.get("source_url"),
        "warning": config.get("warning"),
        "enabled": row.enabled,
        "preset": bool(config.get("preset")),
        "command_available": command_available,
    }


def list_mcp_servers(session: Session, distribution: Path) -> list[dict[str, Any]]:
    ensure_default_mcp_servers(session, distribution)
    rows = session.scalars(select(MCPServerRecord).order_by(MCPServerRecord.name)).all()
    return [mcp_server_json(row) for row in rows]


def _mcp_registry_name(server_name: str, tool_name: str) -> str | None:
    """Mirror Hermes 0.18.2's exact MCP registry-name construction."""

    if not server_name or not tool_name or "*" in tool_name:
        return None
    safe_server = _MCP_NAME_COMPONENT.sub("_", server_name)
    safe_tool = _MCP_NAME_COMPONENT.sub("_", tool_name)
    if not safe_server or not safe_tool:
        return None
    return f"mcp__{safe_server}__{safe_tool}"


def _configured_mcp_tool_names_from_servers(
    servers: list[dict[str, Any]],
) -> list[str]:
    origins: dict[str, tuple[str, str]] = {}
    for server in servers:
        if not server["enabled"]:
            continue
        for tool_name in server["tool_allowlist"]:
            if not isinstance(tool_name, str):
                raise ValueError("Enabled MCP tool allowlists must contain exact names")
            registry_name = _mcp_registry_name(server["name"], tool_name)
            if registry_name is None:
                raise ValueError(
                    "Enabled MCP tool allowlists must contain exact non-wildcard names"
                )
            if len(registry_name) > 400:
                raise ValueError("Enabled MCP registry names cannot exceed 400 characters")
            origin = (server["name"], tool_name)
            previous = origins.setdefault(registry_name, origin)
            if previous != origin:
                raise ValueError(
                    "Enabled MCP tool allowlists collide after Hermes name "
                    f"normalization: {previous!r} and {origin!r}"
                )
    if len(origins) > 128:
        raise ValueError("Pilot supports at most 128 explicitly allowed MCP tools")
    return sorted(origins)


def configured_mcp_tool_names(
    paths: CompanionPaths,
    distribution: Path,
) -> list[str]:
    """Return only exact names from enabled servers' explicit include lists."""

    with account_session(paths) as session:
        servers = list_mcp_servers(session, distribution)
    return _configured_mcp_tool_names_from_servers(servers)


def upsert_mcp_server(
    session: Session,
    payload: dict[str, Any],
    *,
    previous_name: str | None = None,
) -> MCPServerRecord:
    name = str(payload["name"])
    current_name = previous_name or name
    row = session.get(MCPServerRecord, current_name)
    if previous_name is not None and row is None:
        raise LookupError("MCP server not found")
    if previous_name and previous_name != name:
        if session.get(MCPServerRecord, name) is not None:
            raise ValueError("An MCP server with that name already exists")
        session.delete(row)
        session.flush()
        row = None
    preset = bool(row.config.get("preset")) if row is not None else False
    config = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "name",
            "transport",
            "enabled",
            "preset",
            "command_available",
        }
    }
    config["preset"] = preset
    if row is None:
        row = MCPServerRecord(
            name=name,
            transport=str(payload["transport"]),
            config=config,
            enabled=bool(payload["enabled"]),
        )
        session.add(row)
    else:
        row.transport = str(payload["transport"])
        row.config = config
        row.enabled = bool(payload["enabled"])
    session.flush()
    _configured_mcp_tool_names_from_servers(
        [mcp_server_json(item) for item in session.scalars(select(MCPServerRecord))]
    )
    record_audit(
        session,
        "mcp.updated",
        subject_type="mcp",
        subject_id=name,
        payload={
            "enabled": row.enabled,
            "transport": row.transport,
            "tool_allowlist": config.get("tool_allowlist", []),
        },
    )
    return row


def delete_mcp_server(session: Session, name: str) -> None:
    row = session.get(MCPServerRecord, name)
    if row is None:
        raise LookupError("MCP server not found")
    session.delete(row)
    record_audit(
        session,
        "mcp.deleted",
        subject_type="mcp",
        subject_id=name,
    )


def _render_server(server: dict[str, Any]) -> dict[str, Any]:
    rendered: dict[str, Any] = {"enabled": bool(server["enabled"])}
    if server["transport"] == "stdio":
        rendered["command"] = server["command"]
        rendered["args"] = server["args"]
        if server["environment"]:
            rendered["env"] = server["environment"]
    else:
        rendered["url"] = server["url"]
    rendered["tools"] = {
        "include": server["tool_allowlist"],
        "prompts": False,
        "resources": False,
    }
    return rendered


def synchronize_mcp_profile_config(paths: CompanionPaths, distribution: Path) -> list[str]:
    with account_session(paths) as session:
        servers = list_mcp_servers(session, distribution)
    _configured_mcp_tool_names_from_servers(servers)
    forwarded = sorted(
        {
            name
            for server in servers
            if server["enabled"]
            for name in server["forwarded_environment"]
        }
    )
    profile_config = _profile_directory(paths) / "config.yaml"
    if not profile_config.is_file():
        return forwarded
    if profile_config.is_symlink():
        raise ValueError("Installed MCP configuration cannot be a symbolic link")
    try:
        raw_config = yaml.safe_load(profile_config.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError("Installed MCP configuration could not be read") from exc
    if not isinstance(raw_config, dict):
        raise ValueError("Installed MCP configuration is invalid")
    raw_config["mcp_servers"] = {
        server["name"]: _render_server(server) for server in servers
    }
    mode = profile_config.stat().st_mode & 0o777
    rendered = yaml.safe_dump(raw_config, sort_keys=False).encode("utf-8")
    _atomic_write(profile_config, rendered, mode=mode or 0o600)
    return forwarded
