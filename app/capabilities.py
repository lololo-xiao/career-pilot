from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.auth import AuthStore, AuthenticatedAccount
from app.hermes_runtime import (
    PILOT_API_SERVER_TOOLSETS,
    PILOT_DISABLED_TOOLSETS,
    installed_profile_directory,
    profile_distribution_directory,
)
from app.schemas import (
    CapabilityGroupResponse,
    CapabilitySettingsResponse,
    MCPServerCapabilityResponse,
    WebSearchSettingsResponse,
)
from career_companion.paths import CompanionPaths


WEB_SEARCH_SERVICE = "brave_search"
WEB_SEARCH_SETUP_URL = "https://brave.com/search/api/"

_CAREER_TOOLS = [
    "career_profile_get",
    "career_job_add",
    "career_job_score",
    "career_job_queue",
    "career_application_queue",
    "career_application_status",
    "career_revision_propose",
    "career_policy_status",
]


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _profile_config(paths: CompanionPaths) -> dict[str, Any]:
    installed = installed_profile_directory(paths) / "config.yaml"
    if installed.is_file():
        return _read_yaml(installed)
    return _read_yaml(profile_distribution_directory() / "config.yaml")


def _mcp_servers(paths: CompanionPaths) -> list[MCPServerCapabilityResponse]:
    raw_servers = _profile_config(paths).get("mcp_servers")
    if not isinstance(raw_servers, dict):
        return []
    servers: list[MCPServerCapabilityResponse] = []
    for name, raw in sorted(raw_servers.items()):
        if not isinstance(name, str) or not isinstance(raw, dict):
            continue
        raw_tools = raw.get("tools")
        included = raw_tools.get("include", []) if isinstance(raw_tools, dict) else []
        tools = [str(tool) for tool in included if isinstance(tool, str)]
        transport = "stdio" if raw.get("command") else "http"
        servers.append(
            MCPServerCapabilityResponse(
                name=name,
                display_name=name.replace("-", " ").title(),
                enabled=bool(raw.get("enabled")),
                transport=transport,
                tools=tools,
            )
        )
    return servers


def build_capability_settings(
    account: AuthenticatedAccount,
    store: AuthStore,
) -> CapabilitySettingsResponse:
    paths = CompanionPaths.discover().scoped_to(account.user_id)
    web_search_configured = (
        store.load_service_credential(account.user_id, WEB_SEARCH_SERVICE) is not None
    )
    groups = [
        CapabilityGroupResponse(
            id="career-workspace",
            name="Career workspace",
            description=(
                "Read reviewed evidence, track and score jobs, and manage the application queue."
            ),
            state="enabled",
            state_label="Ready",
            tools=_CAREER_TOOLS,
        ),
        CapabilityGroupResponse(
            id="local-workspace",
            name="Local workspace tools",
            description=(
                "Work with files and run code inside CareerPilot's device-local workspace."
            ),
            state="enabled",
            state_label="Ready",
            tools=["read_file", "write_file", "patch", "terminal", "execute_code"],
            note="Access stays inside the local CareerPilot workspace.",
        ),
        CapabilityGroupResponse(
            id="web-search",
            name="Web search",
            description="Find current public pages and job links on the web.",
            state="enabled" if web_search_configured else "setup_required",
            state_label="Ready" if web_search_configured else "Needs setup",
            tools=["web_search"],
            note=(
                "Brave Search is connected. LinkedIn remains manual."
                if web_search_configured
                else "Connect a Brave Search key below. LinkedIn remains manual."
            ),
        ),
        CapabilityGroupResponse(
            id="browser-assistance",
            name="Application browser assistance",
            description="Fill approved application fields for review without submitting them.",
            state="limited",
            state_label="Approval required",
            tools=["career_browser_fill"],
            note="General browser automation and final submission are off.",
        ),
        CapabilityGroupResponse(
            id="autonomous-actions",
            name="Autonomous actions",
            description="Delegation, messaging, raw memory writes, and schedules.",
            state="disabled",
            state_label="Off by design",
            tools=list(PILOT_DISABLED_TOOLSETS),
            note="Pilot cannot send messages, submit applications, or schedule itself.",
        ),
    ]
    return CapabilitySettingsResponse(
        enabled_toolsets=list(PILOT_API_SERVER_TOOLSETS),
        disabled_toolsets=list(PILOT_DISABLED_TOOLSETS),
        groups=groups,
        mcp_servers=_mcp_servers(paths),
        web_search=WebSearchSettingsResponse(
            configured=web_search_configured,
            setup_url=WEB_SEARCH_SETUP_URL,
        ),
    )
