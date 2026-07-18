from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

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
    "CAREER_COMPANION_GUARD_NONCE",
    "CAREER_COMPANION_PLUGIN_TOKEN",
    "HERMES_HOME",
    "HERMES_WRITE_SAFE_ROOT",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
}

_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_MCP_NAME_COMPONENT = re.compile(r"[^A-Za-z0-9_]")
_HERMES_ENV_REFERENCE = re.compile(r"\$\{([^}]+)\}")
_INTERPOLATION_PREFIX = re.compile(r"\$\s*\{")
_SERVER_NAME = re.compile(
    r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,78}[A-Za-z0-9_])?\Z"
)
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
_PILOT_RESERVED_TOOL_NAMES = {
    "browser_navigate",
    "browser_snapshot",
    "clarify",
    "close_terminal",
    "execute_code",
    "patch",
    "process",
    "read_file",
    "search_files",
    "session_search",
    "skill_manage",
    "skill_view",
    "skills_list",
    "todo",
    "tool_call",
    "tool_describe",
    "tool_search",
    "web_extract",
    "web_search",
    "write_file",
    "career_identity_get",
    "career_identity_update",
    "career_profile_get",
    "career_public_job_discover",
    "career_job_add",
    "career_job_score",
    "career_job_track_selected",
    "career_job_queue",
    "career_application_queue",
    "career_application_decide",
    "career_application_status",
    "career_revision_propose",
    "career_policy_status",
}
_MCP_SERVER_FIELDS = {
    "args",
    "command",
    "command_available",
    "description",
    "display_name",
    "enabled",
    "environment",
    "forwarded_environment",
    "name",
    "preset",
    "source_url",
    "tool_allowlist",
    "transport",
    "url",
    "warning",
}
_MCP_PRESET_FIELDS = (_MCP_SERVER_FIELDS | {"env", "url_env"}) - {
    "command_available",
    "preset",
}


def _normalized_hermes_environment_name(reference: str) -> str:
    name = reference.strip()
    if name.startswith("env:"):
        name = name[len("env:") :].strip()
    if _ENVIRONMENT_NAME.fullmatch(name) is None:
        raise ValueError("MCP environment references must use exact variable names")
    return name


def _hermes_registry_component(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("MCP tool names must be exact non-empty strings")
    return _MCP_NAME_COMPONENT.sub("_", name)


def normalized_hermes_tool_name(name: str) -> str:
    """Mirror Hermes registry component normalization for policy checks."""

    return _hermes_registry_component(name).casefold()


def validate_mcp_tool_allowlist(tool_allowlist: Any) -> None:
    if not isinstance(tool_allowlist, list) or len(tool_allowlist) > 128:
        raise ValueError("MCP tool allowlists must be exact bounded lists")
    normalized_names: set[str] = set()
    for tool_name in tool_allowlist:
        if not isinstance(tool_name, str) or _TOOL_NAME.fullmatch(tool_name) is None:
            raise ValueError(
                "MCP tool allowlists require exact non-wildcard names"
            )
        normalized = normalized_hermes_tool_name(tool_name)
        tokens = set(normalized.split("_"))
        outbound_prefix = normalized.split("_", 1)[0] in {
            "apply",
            "connect",
            "contact",
            "delete",
            "post",
            "remove",
            "send",
            "submit",
        }
        if (
            normalized in _PILOT_RESERVED_TOOL_NAMES
            or normalized == "create_post"
            or normalized.startswith("create_post_")
            or outbound_prefix
            or "secret" in tokens
            or "secrets" in tokens
        ):
            raise ValueError(
                f"MCP tool is prohibited after Hermes normalization: {tool_name}"
            )
        if normalized in normalized_names:
            raise ValueError("MCP tools collide after Hermes name normalization")
        normalized_names.add(normalized)


def _mcp_environment_references(value: Any) -> set[str]:
    """Mirror Hermes 0.18.2's recursive ${...} interpolation grammar."""

    if isinstance(value, str):
        decoded = value
        for _ in range(3):
            next_value = unquote(decoded)
            if next_value == decoded:
                break
            decoded = next_value
            if _INTERPOLATION_PREFIX.search(decoded):
                raise ValueError("Encoded MCP environment references are not allowed")
        references = {
            _normalized_hermes_environment_name(match.group(1))
            for match in _HERMES_ENV_REFERENCE.finditer(value)
        }
        unmatched = _HERMES_ENV_REFERENCE.sub("", value)
        if _INTERPOLATION_PREFIX.search(unmatched):
            raise ValueError("Malformed MCP environment reference")
        return references
    if isinstance(value, dict):
        references: set[str] = set()
        for nested in value.values():
            references.update(_mcp_environment_references(nested))
        return references
    if isinstance(value, list):
        references = set()
        for nested in value:
            references.update(_mcp_environment_references(nested))
        return references
    return set()


def validate_mcp_environment_references(
    value: Any,
    forwarded_environment: list[str],
) -> None:
    """Permit interpolation only for explicit, non-runtime forwarded names."""

    if not isinstance(forwarded_environment, list) or any(
        not isinstance(name, str) or _ENVIRONMENT_NAME.fullmatch(name) is None
        for name in forwarded_environment
    ):
        raise ValueError("MCP forwarded_environment must be an exact name list")
    if len(forwarded_environment) > 64:
        raise ValueError("MCP forwarded_environment cannot exceed 64 exact names")
    if len(set(forwarded_environment)) != len(forwarded_environment):
        raise ValueError("MCP forwarded_environment cannot contain duplicate names")
    reserved = {name.casefold() for name in MCP_RESERVED_ENVIRONMENT}
    prohibited_forwarding = sorted(
        name for name in forwarded_environment if name.casefold() in reserved
    )
    if prohibited_forwarding:
        raise ValueError(
            "Reserved runtime variables cannot be forwarded: "
            + ", ".join(prohibited_forwarding)
        )
    references = _mcp_environment_references(value)
    prohibited_references = sorted(
        name for name in references if name.casefold() in reserved
    )
    if prohibited_references:
        raise ValueError(
            "Reserved runtime variables cannot be referenced: "
            + ", ".join(prohibited_references)
        )
    absent = sorted(references - set(forwarded_environment))
    if absent:
        raise ValueError(
            "MCP environment references require explicit forwarding: "
            + ", ".join(absent)
        )


def _validate_mcp_server_runtime_config(server: dict[str, Any]) -> None:
    if not isinstance(server, dict):
        raise ValueError("MCP runtime server entries must be objects")
    transport = server.get("transport")
    name = server.get("name")
    args = server.get("args")
    environment = server.get("environment")
    forwarded_environment = server.get("forwarded_environment")
    if set(server) - _MCP_SERVER_FIELDS:
        raise ValueError("MCP runtime server entries contain unsupported settings")
    if not isinstance(name, str) or _SERVER_NAME.fullmatch(name) is None:
        raise ValueError("MCP runtime server names must be exact bounded names")
    if not isinstance(server.get("enabled"), bool):
        raise ValueError("MCP server enabled must be an exact boolean")
    for field, maximum in (("display_name", 120), ("description", 1_000)):
        value = server.get(field)
        if (
            not isinstance(value, str)
            or len(value) > maximum
            or (field == "display_name" and not value)
        ):
            raise ValueError(f"MCP {field} must be an exact bounded string")
    for field, maximum in (("source_url", 2_048), ("warning", 1_000)):
        value = server.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise ValueError(f"MCP {field} must be an exact bounded string")
    for field in ("preset", "command_available"):
        value = server.get(field)
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"MCP {field} must be an exact boolean")
    if transport not in {"stdio", "http"}:
        raise ValueError("MCP runtime transports must be stdio or http")
    if (
        not isinstance(args, list)
        or len(args) > 64
        or any(
            not isinstance(arg, str)
            or len(arg) > 1_024
            or any(character in arg for character in "\x00\r\n")
            for arg in args
        )
    ):
        raise ValueError("MCP runtime arguments must be an exact string list")
    if not isinstance(environment, dict) or any(
        not isinstance(key, str)
        or _ENVIRONMENT_NAME.fullmatch(key) is None
        or not isinstance(value, str)
        or len(value) > 2_048
        or "\x00" in value
        for key, value in environment.items()
    ) or len(environment) > 64:
        raise ValueError("MCP runtime environment must map strings to strings")
    validate_mcp_tool_allowlist(server.get("tool_allowlist"))
    if transport == "stdio":
        command = server.get("command")
        if (
            not isinstance(command, str)
            or not command
            or len(command) > 512
            or any(character in command for character in "\x00\r\n")
        ):
            raise ValueError("Stdio MCP servers require an exact command")
        if server.get("url") is not None:
            raise ValueError("Stdio MCP servers cannot define a URL")
    else:
        url = server.get("url")
        if not isinstance(url, str) or not url or len(url) > 2_048:
            raise ValueError("HTTP MCP servers require an exact URL")
        if server.get("command") is not None or args:
            raise ValueError("HTTP MCP servers cannot define a command or arguments")
        if url.startswith("${"):
            if re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", url) is None:
                raise ValueError("MCP URL references must use exact variable names")
        else:
            parsed = urlsplit(url)
            loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            if parsed.username or parsed.password:
                raise ValueError("MCP runtime URLs cannot contain credentials")
            if parsed.scheme != "https" and not (
                parsed.scheme == "http" and loopback
            ):
                raise ValueError("Remote MCP runtime URLs must use HTTPS")
    validate_mcp_environment_references(
        {
            "command": server.get("command"),
            "args": args,
            "url": server.get("url"),
            "environment": environment,
            "tool_allowlist": server.get("tool_allowlist"),
        },
        forwarded_environment,
    )


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
    if set(raw) - _MCP_PRESET_FIELDS:
        return None
    if {"env", "environment"} <= set(raw) or {"url", "url_env"} <= set(raw):
        return None
    name = raw.get("name")
    transport = raw.get("transport")
    if not isinstance(name, str) or transport not in {"stdio", "http"}:
        return None
    if "display_name" in raw and (
        not isinstance(raw["display_name"], str) or not raw["display_name"]
    ):
        return None
    if "description" in raw and not isinstance(raw["description"], str):
        return None
    enabled = raw.get("enabled", False)
    tool_allowlist = raw.get("tool_allowlist", [])
    forwarded_environment = raw.get("forwarded_environment", [])
    if not isinstance(enabled, bool):
        return None
    if (
        not isinstance(tool_allowlist, list)
        or any(not isinstance(item, str) for item in tool_allowlist)
        or (enabled and not tool_allowlist)
    ):
        return None
    if not isinstance(forwarded_environment, list) or any(
        not isinstance(item, str) for item in forwarded_environment
    ):
        return None
    url = raw.get("url")
    url_environment = raw.get("url_env")
    if url_environment is not None:
        if (
            not isinstance(url_environment, str)
            or _ENVIRONMENT_NAME.fullmatch(url_environment) is None
        ):
            return None
        url = f"${{{url_environment}}}"
        forwarded_environment = [*forwarded_environment, url_environment]
    environment = raw.get("environment", raw.get("env", {}))
    if not isinstance(environment, dict):
        return None
    payload = {
        "name": name,
        "display_name": raw.get("display_name") or name.replace("-", " ").title(),
        "description": raw.get("description") or "",
        "transport": transport,
        "command": raw.get("command"),
        "args": raw.get("args", []),
        "url": url,
        "tool_allowlist": tool_allowlist,
        "forwarded_environment": list(dict.fromkeys(forwarded_environment)),
        "environment": environment if isinstance(environment, dict) else {},
        "source_url": raw.get("source_url"),
        "warning": raw.get("warning"),
        "enabled": enabled,
        "preset": True,
    }
    try:
        _validate_mcp_server_runtime_config(payload)
        _configured_mcp_tool_names_from_servers([payload])
    except ValueError:
        return None
    return payload


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
                enabled=preset["enabled"],
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
    if not isinstance(row.config, dict):
        raise ValueError("Persisted MCP server configuration must be an object")
    config = row.config
    persisted_fields = _MCP_SERVER_FIELDS - {
        "command_available",
        "enabled",
        "name",
        "transport",
    }
    if set(config) - persisted_fields:
        raise ValueError("Persisted MCP server configuration has unsupported settings")
    runtime = {
        "name": row.name,
        "display_name": config.get(
            "display_name",
            row.name.replace("-", " ").title(),
        ),
        "description": config.get("description", ""),
        "transport": row.transport,
        "command": config.get("command"),
        "args": config.get("args", []),
        "url": config.get("url"),
        "tool_allowlist": config.get("tool_allowlist", []),
        "forwarded_environment": config.get("forwarded_environment", []),
        "environment": config.get("environment", {}),
        "source_url": config.get("source_url"),
        "warning": config.get("warning"),
        "enabled": row.enabled,
        "preset": config.get("preset", False),
    }
    _validate_mcp_server_runtime_config(runtime)
    command = runtime["command"]
    command_available = (
        bool(shutil.which(command)) if isinstance(command, str) and command else None
    )
    return runtime | {"command_available": command_available}


def list_mcp_servers(session: Session, distribution: Path) -> list[dict[str, Any]]:
    ensure_default_mcp_servers(session, distribution)
    rows = session.scalars(select(MCPServerRecord).order_by(MCPServerRecord.name)).all()
    return [mcp_server_json(row) for row in rows]


def _mcp_registry_name(server_name: str, tool_name: str) -> str | None:
    """Mirror Hermes 0.18.2's exact MCP registry-name construction."""

    if (
        not isinstance(server_name, str)
        or not isinstance(tool_name, str)
        or not server_name
        or not tool_name
        or "*" in tool_name
    ):
        return None
    safe_server = _hermes_registry_component(server_name)
    safe_tool = _hermes_registry_component(tool_name)
    if not safe_server or not safe_tool:
        return None
    return f"mcp__{safe_server}__{safe_tool}"


def _configured_mcp_tool_names_from_servers(
    servers: list[dict[str, Any]],
) -> list[str]:
    if not isinstance(servers, list):
        raise ValueError("MCP runtime configuration must be an exact server list")
    origins: dict[str, tuple[tuple[str, str], str]] = {}
    for server in servers:
        if not isinstance(server, dict):
            raise ValueError("MCP runtime server entries must be objects")
        enabled = server.get("enabled")
        server_name = server.get("name")
        tool_allowlist = server.get("tool_allowlist")
        if not isinstance(enabled, bool):
            raise ValueError("MCP server enabled must be an exact boolean")
        if (
            not isinstance(server_name, str)
            or _SERVER_NAME.fullmatch(server_name) is None
        ):
            raise ValueError("MCP runtime server names must be non-empty strings")
        if not isinstance(tool_allowlist, list):
            raise ValueError("MCP tool allowlists must be exact lists")
        validate_mcp_tool_allowlist(tool_allowlist)
        if len(tool_allowlist) > 128:
            raise ValueError("MCP tool allowlists cannot exceed 128 exact names")
        if enabled and not tool_allowlist:
            raise ValueError("Enabled MCP servers require an explicit tool allowlist")
        for tool_name in tool_allowlist:
            if not isinstance(tool_name, str) or _TOOL_NAME.fullmatch(tool_name) is None:
                raise ValueError(
                    "Enabled MCP tool allowlists must contain exact non-wildcard names"
                )
            registry_name = _mcp_registry_name(server_name, tool_name)
            if registry_name is None:
                raise ValueError(
                    "Enabled MCP tool allowlists must contain exact non-wildcard names"
                )
            if not enabled:
                continue
            if len(registry_name) > 400:
                raise ValueError("Enabled MCP registry names cannot exceed 400 characters")
            origin = (server_name, tool_name)
            normalized_registry_name = registry_name.casefold()
            previous = origins.setdefault(
                normalized_registry_name,
                (origin, registry_name),
            )
            if previous[0] != origin:
                raise ValueError(
                    "Enabled MCP tool allowlists collide after Hermes name "
                    f"normalization: {previous[0]!r} and {origin!r}"
                )
    if len(origins) > 128:
        raise ValueError("Pilot supports at most 128 explicitly allowed MCP tools")
    return sorted(registry_name for _, registry_name in origins.values())


def configured_mcp_tool_names(
    paths: CompanionPaths,
    distribution: Path,
) -> list[str]:
    """Return only exact names from enabled servers' explicit include lists."""

    with account_session(paths) as session:
        servers = list_mcp_servers(session, distribution)
    for server in servers:
        _validate_mcp_server_runtime_config(server)
    return _configured_mcp_tool_names_from_servers(servers)


def upsert_mcp_server(
    session: Session,
    payload: dict[str, Any],
    *,
    previous_name: str | None = None,
    change_version: str | None = None,
) -> MCPServerRecord:
    if not isinstance(payload, dict):
        raise ValueError("MCP server payloads must be objects")
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("MCP server names must be non-empty strings")
    current_name = previous_name or name
    row = session.get(MCPServerRecord, current_name)
    if previous_name is not None and row is None:
        raise LookupError("MCP server not found")
    if previous_name and previous_name != name:
        if session.get(MCPServerRecord, name) is not None:
            raise ValueError("An MCP server with that name already exists")
    preset_value = row.config.get("preset", False) if row is not None else False
    if not isinstance(preset_value, bool):
        raise ValueError("MCP preset markers must be exact booleans")
    preset = preset_value
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
    candidate = {
        **config,
        "name": name,
        "transport": payload.get("transport"),
        "enabled": payload.get("enabled"),
    }
    candidate.setdefault("display_name", name.replace("-", " ").title())
    candidate.setdefault("description", "")
    _validate_mcp_server_runtime_config(candidate)
    proposed_servers = [
        mcp_server_json(item)
        for item in session.scalars(select(MCPServerRecord))
        if item.name != current_name
    ]
    proposed_servers.append(candidate)
    for server in proposed_servers:
        _validate_mcp_server_runtime_config(server)
    _configured_mcp_tool_names_from_servers(proposed_servers)
    if previous_name and previous_name != name:
        session.delete(row)
        session.flush()
        row = None
    if row is None:
        row = MCPServerRecord(
            name=name,
            transport=payload["transport"],
            config=config,
            enabled=payload["enabled"],
        )
        session.add(row)
    else:
        row.transport = payload["transport"]
        row.config = config
        row.enabled = payload["enabled"]
    session.flush()
    record_audit(
        session,
        "mcp.updated",
        subject_type="mcp",
        subject_id=name,
        payload={
            "enabled": row.enabled,
            "transport": row.transport,
            "tool_allowlist": config.get("tool_allowlist", []),
            "change_version": change_version,
            "outcome": "committed",
        },
    )
    return row


def delete_mcp_server(
    session: Session,
    name: str,
    *,
    change_version: str | None = None,
) -> None:
    row = session.get(MCPServerRecord, name)
    if row is None:
        raise LookupError("MCP server not found")
    session.delete(row)
    record_audit(
        session,
        "mcp.deleted",
        subject_type="mcp",
        subject_id=name,
        payload={"change_version": change_version, "outcome": "committed"},
    )


def _render_server(server: dict[str, Any]) -> dict[str, Any]:
    _validate_mcp_server_runtime_config(server)
    _configured_mcp_tool_names_from_servers([server])
    rendered: dict[str, Any] = {"enabled": server["enabled"]}
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


def _validated_mcp_servers(
    session: Session,
    distribution: Path,
) -> list[dict[str, Any]]:
    servers = list_mcp_servers(session, distribution)
    for server in servers:
        _validate_mcp_server_runtime_config(server)
    _configured_mcp_tool_names_from_servers(servers)
    return servers


def _render_mcp_profile_bytes(
    paths: CompanionPaths,
    servers: list[dict[str, Any]],
) -> tuple[bytes, int, list[str]]:
    forwarded = sorted(
        {
            name
            for server in servers
            if server["enabled"]
            for name in server["forwarded_environment"]
        }
    )
    if len(forwarded) > 128:
        raise ValueError("Pilot supports at most 128 forwarded MCP environment names")
    profile_config = _profile_directory(paths) / "config.yaml"
    if not profile_config.is_file() or profile_config.is_symlink():
        raise ValueError("Installed MCP profile configuration is unavailable")
    try:
        loaded = yaml.safe_load(profile_config.read_text(encoding="utf-8"))
        raw_config = {} if loaded is None else loaded
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError("Installed MCP configuration could not be read") from exc
    if not isinstance(raw_config, dict):
        raise ValueError("Installed MCP configuration is invalid")
    raw_config["mcp_servers"] = {
        server["name"]: _render_server(server) for server in servers
    }
    mode = profile_config.stat().st_mode & 0o777
    return yaml.safe_dump(raw_config, sort_keys=False).encode("utf-8"), mode, forwarded


def synchronize_mcp_profile_config_from_session(
    session: Session,
    paths: CompanionPaths,
    distribution: Path,
) -> list[str]:
    """Validate and atomically render the current transaction's MCP state."""

    servers = _validated_mcp_servers(session, distribution)
    rendered, mode, forwarded = _render_mcp_profile_bytes(paths, servers)
    profile_config = _profile_directory(paths) / "config.yaml"
    _atomic_write(profile_config, rendered, mode=mode or 0o600)
    return forwarded


def synchronize_mcp_profile_config(paths: CompanionPaths, distribution: Path) -> list[str]:
    with account_session(paths) as session:
        return synchronize_mcp_profile_config_from_session(
            session,
            paths,
            distribution,
        )
