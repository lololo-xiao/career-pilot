from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    """Base model for API contracts that must reject unexpected fields."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MatchRequest(StrictModel):
    candidate_profile: str = Field(min_length=10, max_length=50_000)
    job_description: str = Field(min_length=10, max_length=50_000)


CVFileType = Literal["pdf", "docx", "text"]


class ParsedCVResponse(StrictModel):
    filename: str = Field(min_length=1, max_length=255)
    file_type: CVFileType
    text: str = Field(min_length=10, max_length=50_000)
    character_count: int = Field(ge=10, le=50_000)
    page_count: int | None = Field(default=None, ge=1, le=30)


class EvidenceSnippet(StrictModel):
    source_id: str = Field(
        min_length=1,
        max_length=120,
        description="Stable source identifier supplied by the retrieval layer.",
    )
    text: str = Field(
        min_length=1,
        max_length=2_000,
        description="Candidate text made available to the match model.",
    )


class EvidenceCitation(StrictModel):
    source_id: str = Field(
        min_length=1,
        max_length=120,
        description="Identifier of the retrieved candidate source containing the quote.",
    )
    quote: str = Field(
        min_length=1,
        max_length=500,
        description="Verbatim quote from the candidate source.",
    )


class MatchedSkill(StrictModel):
    skill: str = Field(min_length=1, max_length=120)
    explanation: str = Field(min_length=1, max_length=400)
    evidence: list[EvidenceCitation] = Field(min_length=1, max_length=5)


class AdjacentSkill(StrictModel):
    skill: str = Field(min_length=1, max_length=120)
    explanation: str = Field(
        min_length=1,
        max_length=400,
        description="Why explicit candidate experience is transferable but not equivalent.",
    )
    evidence: list[EvidenceCitation] = Field(min_length=1, max_length=5)


RequirementImportance = Literal["required", "preferred", "unspecified"]


class MissingSkill(StrictModel):
    skill: str = Field(min_length=1, max_length=120)
    importance: RequirementImportance
    explanation: str = Field(min_length=1, max_length=400)


class JobRequirement(StrictModel):
    requirement: str = Field(min_length=1, max_length=200)
    importance: RequirementImportance
    evidence_quote: str = Field(
        min_length=1,
        max_length=500,
        description="Verbatim quote from the job description.",
    )


class PreparationAction(StrictModel):
    priority: int = Field(ge=1, le=5)
    action: str = Field(min_length=1, max_length=300)
    rationale: str = Field(min_length=1, max_length=400)
    addresses: list[str] = Field(min_length=1, max_length=5)


class UnsupportedClaimWarning(StrictModel):
    claim: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=400)


class MatchResponse(StrictModel):
    score: int = Field(
        ge=0,
        le=10,
        description="Overall evidence-grounded match score from 0 to 10.",
    )
    summary: str = Field(min_length=1, max_length=600)
    matched_skills: list[MatchedSkill] = Field(max_length=20)
    adjacent_skills: list[AdjacentSkill] = Field(max_length=20)
    missing_skills: list[MissingSkill] = Field(max_length=20)
    important_requirements: list[JobRequirement] = Field(max_length=20)
    preparation_actions: list[PreparationAction] = Field(max_length=10)
    unsupported_claim_warnings: list[UnsupportedClaimWarning] = Field(max_length=20)


CompanionRole = Literal["user", "assistant"]


class CompanionTurn(StrictModel):
    role: CompanionRole
    content: str = Field(min_length=1, max_length=4_000)


class CompanionChatRequest(StrictModel):
    session_id: str | None = Field(default=None, min_length=36, max_length=36)
    message: str = Field(min_length=1, max_length=4_000)
    conversation: list[CompanionTurn] = Field(default_factory=list, max_length=12)
    candidate_profile: str | None = Field(default=None, min_length=10, max_length=50_000)
    job_description: str | None = Field(default=None, min_length=10, max_length=50_000)
    match_report: MatchResponse | None = None


class CompanionChatResponse(StrictModel):
    message: str = Field(min_length=1, max_length=1_600)
    suggested_prompts: list[
        Annotated[str, Field(min_length=1, max_length=120)]
    ] = Field(min_length=1, max_length=3)


class CompanionApprovalRequest(StrictModel):
    choice: Literal["once", "deny"]
    session_id: str | None = Field(default=None, min_length=36, max_length=36)


class AgentIdentityRequest(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    soul: str = Field(default="", max_length=32_768)


class AgentIdentityResponse(AgentIdentityRequest):
    core_soul: str
    effective_soul: str
    identity_path: str
    soul_path: str
    updated_at: datetime


class ConversationMessageRequest(StrictModel):
    role: CompanionRole
    content: str = Field(min_length=1, max_length=50_000)
    report: MatchResponse | None = None


class ConversationMessageResponse(ConversationMessageRequest):
    id: str
    created_at: datetime


class ConversationSessionCreateRequest(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)


class ConversationSessionRenameRequest(StrictModel):
    title: str = Field(min_length=1, max_length=120)


class ConversationSessionContextRequest(StrictModel):
    candidate_profile: str | None = Field(default=None, min_length=10, max_length=50_000)
    job_description: str | None = Field(default=None, min_length=10, max_length=50_000)
    uploaded_filename: str | None = Field(default=None, min_length=1, max_length=255)
    match_report: MatchResponse | None = None


class ConversationSessionSummaryResponse(StrictModel):
    id: str
    title: str
    message_count: int = Field(ge=0)
    preview: str
    created_at: datetime
    updated_at: datetime


class ConversationSessionResponse(ConversationSessionSummaryResponse):
    messages: list[ConversationMessageResponse]
    candidate_profile: str | None = None
    job_description: str | None = None
    uploaded_filename: str | None = None
    match_report: MatchResponse | None = None


class ConversationSessionListResponse(StrictModel):
    active_session_id: str
    sessions: list[ConversationSessionSummaryResponse]


class MemoryRevisionCitation(StrictModel):
    source_type: Literal["memory_revision"]
    revision_id: str
    name: str
    version: int = Field(ge=1)
    source_session: str


class ApplicationOutcomeCitation(StrictModel):
    source_type: Literal["application_status_event"]
    event_id: str
    application_id: str
    job_id: str
    recorded_at: str


class MemoryRetrievalResultSummary(StrictModel):
    source_type: Literal["memory_revision", "application_outcome"]
    citation: MemoryRevisionCitation | ApplicationOutcomeCitation
    relevance_score: int = Field(ge=1)
    matched_terms: list[str] = Field(max_length=32)
    why_retrieved: str = Field(min_length=1, max_length=1_000)
    truncated: bool


class MemoryRetrievalResolutionSummary(StrictModel):
    audit_id: str
    created_at: datetime
    schema_version: str = Field(min_length=1, max_length=50)
    query_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    retrieval_algorithm: str = Field(min_length=1, max_length=100)
    content_sha256: str = Field(min_length=64, max_length=64)
    included_item_count: int = Field(ge=0, le=6)
    token_upper_bound: int = Field(ge=0, le=8_192)
    results: list[MemoryRetrievalResultSummary] = Field(max_length=6)


class MemoryRetrievalHistoryResponse(StrictModel):
    session_id: str
    items: list[MemoryRetrievalResolutionSummary] = Field(max_length=10)
    limit: int = Field(ge=1, le=10)
    offset: int = Field(ge=0, le=100)
    has_more: bool


IdentityMethod = Literal["local"]
ProviderMethod = Literal["api_key", "codex"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]


class ApiKeyConnectionRequest(StrictModel):
    api_key: str = Field(min_length=20, max_length=512)


class AuthUserResponse(StrictModel):
    id: str
    display_name: str
    active_provider: ProviderMethod | None = None
    plan_type: str | None = None
    provider_email: str | None = None
    provider_label: str | None = None


class AuthSessionResponse(StrictModel):
    authenticated: bool
    user: AuthUserResponse | None = None


class ProviderConnectionResponse(StrictModel):
    provider: ProviderMethod
    connected: bool
    active: bool
    provider_label: str
    provider_email: str | None = None
    plan_type: str | None = None


class ProviderSettingsResponse(StrictModel):
    active_provider: ProviderMethod | None = None
    connections: list[ProviderConnectionResponse]


class ProviderSelectionRequest(StrictModel):
    provider: ProviderMethod


CapabilityState = Literal["enabled", "setup_required", "limited", "disabled"]


class CapabilityGroupResponse(StrictModel):
    id: str
    name: str
    description: str
    state: CapabilityState
    state_label: str
    tools: list[str] = Field(default_factory=list)
    note: str | None = None


class MCPServerCapabilityResponse(StrictModel):
    name: str
    display_name: str
    enabled: bool
    transport: Literal["http", "stdio"]
    tools: list[str] = Field(default_factory=list)


class WebSearchSettingsResponse(StrictModel):
    provider: Literal["brave"] = "brave"
    provider_label: str = "Brave Search"
    configured: bool
    setup_url: str
    key_storage: Literal["encrypted_local"] = "encrypted_local"


class CapabilitySettingsResponse(StrictModel):
    enabled_toolsets: list[str]
    disabled_toolsets: list[str]
    groups: list[CapabilityGroupResponse]
    mcp_servers: list[MCPServerCapabilityResponse]
    web_search: WebSearchSettingsResponse


class WebSearchConnectionRequest(StrictModel):
    api_key: str = Field(min_length=16, max_length=512)


AgentAssetKind = Literal["memory", "skill"]


class AgentAssetRequest(StrictModel):
    name: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$",
    )
    content: str = Field(min_length=1, max_length=65_536)


class AgentAssetResponse(StrictModel):
    kind: AgentAssetKind
    name: str
    title: str
    description: str | None = None
    content: str
    path: str
    built_in: bool
    editable: bool


class AgentAssetSettingsResponse(StrictModel):
    memories: list[AgentAssetResponse]
    skills: list[AgentAssetResponse]


MCPTransport = Literal["stdio", "http"]


class MCPServerSettingsRequest(StrictModel):
    name: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$",
    )
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1_000)
    transport: MCPTransport
    command: str | None = Field(default=None, min_length=1, max_length=512)
    args: list[Annotated[str, Field(max_length=1_024)]] = Field(
        default_factory=list,
        max_length=64,
    )
    url: str | None = Field(default=None, min_length=1, max_length=2_048)
    tool_allowlist: list[
        Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,160}$")]
    ] = Field(default_factory=list, max_length=128)
    forwarded_environment: list[
        Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    ] = Field(default_factory=list, max_length=64)
    environment: dict[str, str] = Field(default_factory=dict)
    source_url: str | None = Field(default=None, max_length=2_048)
    warning: str | None = Field(default=None, max_length=1_000)
    enabled: bool = False

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str | None) -> str | None:
        if value is not None and any(character in value for character in "\x00\r\n"):
            raise ValueError("MCP commands cannot contain control characters")
        return value

    @field_validator("args")
    @classmethod
    def validate_args(cls, values: list[str]) -> list[str]:
        if any("\x00" in value or "\r" in value or "\n" in value for value in values):
            raise ValueError("MCP arguments cannot contain control characters")
        return values

    @field_validator("tool_allowlist", "forwarded_environment")
    @classmethod
    def deduplicate_list(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @field_validator("forwarded_environment")
    @classmethod
    def protect_runtime_environment(cls, values: list[str]) -> list[str]:
        from career_companion.services.mcp_servers import MCP_RESERVED_ENVIRONMENT

        reserved = sorted(set(values) & MCP_RESERVED_ENVIRONMENT)
        if reserved:
            raise ValueError(
                f"Reserved runtime variables cannot be forwarded: {', '.join(reserved)}"
            )
        return values

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, values: dict[str, str]) -> dict[str, str]:
        import re

        if len(values) > 64:
            raise ValueError("MCP environment cannot contain more than 64 values")
        result: dict[str, str] = {}
        for key, value in values.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"Invalid MCP environment variable name: {key}")
            if len(value) > 2_048 or "\x00" in value:
                raise ValueError(f"Invalid MCP environment value for {key}")
            result[key] = value
        return result

    @model_validator(mode="after")
    def validate_transport(self) -> "MCPServerSettingsRequest":
        import re
        from urllib.parse import urlsplit

        blocked_tools = {
            "apply_to_job",
            "connect_with_person",
            "create_post",
            "send_message",
            "submit_application",
        }
        prohibited = sorted(
            tool for tool in self.tool_allowlist if tool.casefold() in blocked_tools
        )
        if prohibited:
            raise ValueError(
                f"MCP tools that contact people, post, or apply are disabled: {', '.join(prohibited)}"
            )
        if self.enabled and not self.tool_allowlist:
            raise ValueError("Enabled MCP servers require an explicit tool allowlist")
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("Stdio MCP servers require a command")
            if self.url is not None:
                raise ValueError("Stdio MCP servers cannot also define a URL")
        else:
            if not self.url:
                raise ValueError("HTTP MCP servers require a URL")
            if self.command is not None or self.args:
                raise ValueError("HTTP MCP servers cannot also define a command")
            if self.url.startswith("${"):
                if not re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", self.url):
                    raise ValueError("MCP URL environment references must use ${VARIABLE_NAME}")
            else:
                parsed = urlsplit(self.url)
                loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                if parsed.username or parsed.password:
                    raise ValueError("MCP URLs cannot contain credentials")
                if parsed.scheme != "https" and not (
                    parsed.scheme == "http" and loopback
                ):
                    raise ValueError("Remote MCP URLs must use HTTPS")
        if self.source_url:
            parsed_source = urlsplit(self.source_url)
            if parsed_source.scheme != "https" or not parsed_source.hostname:
                raise ValueError("MCP source links must use HTTPS")
        if self.name == "linkedin-search":
            allowed_linkedin_tools = {"search_jobs", "get_job_details"}
            if (
                self.transport != "stdio"
                or self.command != "uvx"
                or self.args != ["mcp-server-linkedin@latest"]
                or not set(self.tool_allowlist).issubset(allowed_linkedin_tools)
            ):
                raise ValueError(
                    "The LinkedIn preset is restricted to the supported uvx command and read-only job tools"
                )
        return self


class MCPServerSettingsResponse(MCPServerSettingsRequest):
    preset: bool = False
    command_available: bool | None = None


class MCPSettingsResponse(StrictModel):
    servers: list[MCPServerSettingsResponse]


class AgentReasoningEffortOption(StrictModel):
    reasoning_effort: ReasoningEffort
    description: str = ""


class AgentModelOption(StrictModel):
    model: str
    display_name: str
    description: str = ""
    is_default: bool = False
    default_reasoning_effort: ReasoningEffort
    supported_reasoning_efforts: list[AgentReasoningEffortOption]
    context_window: int | None = Field(default=None, ge=1)


class AgentRateLimitWindow(StrictModel):
    used_percent: int = Field(ge=0)
    resets_at: int | None = None
    window_duration_minutes: int | None = Field(default=None, ge=1)


class AgentIndividualLimit(StrictModel):
    limit: str
    used: str
    remaining_percent: int = Field(ge=0)
    resets_at: int


class AgentCredits(StrictModel):
    has_credits: bool
    unlimited: bool
    balance: str | None = None


class AgentRateLimits(StrictModel):
    plan_type: str | None = None
    limit_name: str | None = None
    reached_type: str | None = None
    primary: AgentRateLimitWindow | None = None
    secondary: AgentRateLimitWindow | None = None
    individual: AgentIndividualLimit | None = None
    credits: AgentCredits | None = None


class AgentAccountUsage(StrictModel):
    lifetime_tokens: int | None = Field(default=None, ge=0)
    peak_daily_tokens: int | None = Field(default=None, ge=0)
    current_streak_days: int | None = Field(default=None, ge=0)


class AgentSettingsResponse(StrictModel):
    provider: Literal["openai-api", "openai-codex"]
    model: str
    reasoning_effort: ReasoningEffort
    token_limit: int = Field(ge=128, le=200000)
    models: list[AgentModelOption]
    rate_limits: AgentRateLimits | None = None
    account_usage: AgentAccountUsage | None = None
    warnings: list[str] = Field(default_factory=list)


class AgentSettingsRequest(StrictModel):
    model: str = Field(min_length=1, max_length=120)
    reasoning_effort: ReasoningEffort


class CodexLoginStartResponse(StrictModel):
    attempt_id: str
    verification_url: str
    user_code: str
    expires_at: int


CodexLoginStatus = Literal["pending", "completed", "failed", "expired"]


class CodexLoginStatusResponse(StrictModel):
    status: CodexLoginStatus
    user: AuthUserResponse | None = None
    error: str | None = None
