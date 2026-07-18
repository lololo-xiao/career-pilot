from __future__ import annotations

from app.auth import AuthStore, AuthenticatedAccount
from app.hermes_runtime import (
    PILOT_API_SERVER_TOOLSETS,
    PILOT_DISABLED_TOOLSETS,
    profile_distribution_directory,
)
from app.schemas import (
    CapabilityGroupResponse,
    CapabilitySettingsResponse,
    MCPServerCapabilityResponse,
    WebSearchSettingsResponse,
)
from career_companion.paths import CompanionPaths
from career_companion.persistence import account_session
from career_companion.services.mcp_servers import list_mcp_servers


WEB_SEARCH_SERVICE = "brave_search"
WEB_SEARCH_SETUP_URL = "https://brave.com/search/api/"

_CAREER_TOOLS = [
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
    "career_memory_preference_propose",
    "career_policy_status",
]


def _mcp_servers(paths: CompanionPaths) -> list[MCPServerCapabilityResponse]:
    distribution = profile_distribution_directory()
    with account_session(paths) as session:
        configured = list_mcp_servers(session, distribution)
    servers: list[MCPServerCapabilityResponse] = []
    for server in configured:
        servers.append(
            MCPServerCapabilityResponse(
                name=server["name"],
                display_name=server["display_name"],
                enabled=server["enabled"],
                transport=server["transport"],
                tools=server["tool_allowlist"],
            )
        )
    return servers


def build_capability_settings(
    account: AuthenticatedAccount,
    store: AuthStore,
    paths: CompanionPaths,
) -> CapabilitySettingsResponse:
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
                "Direct file, terminal, and code tools are unavailable to Pilot."
            ),
            state="disabled",
            state_label="Off",
            tools=[],
            note="Local changes require a dedicated guarded career workflow.",
        ),
        CapabilityGroupResponse(
            id="web-search",
            name="Web search",
            description="General-purpose web access is unavailable to Pilot.",
            state="disabled",
            state_label="Off",
            tools=[],
            note=(
                "A saved Brave key is inactive; public job discovery uses the guarded career tool."
                if web_search_configured
                else "Public job discovery uses the guarded Greenhouse or Lever career tool."
            ),
        ),
        CapabilityGroupResponse(
            id="browser-assistance",
            name="Application browser assistance",
            description="Application form filling is not available to Pilot.",
            state="disabled",
            state_label="Off",
            tools=[],
            note="A dedicated preview, approval, and fill workflow must be added first.",
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
