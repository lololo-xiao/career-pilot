from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


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
