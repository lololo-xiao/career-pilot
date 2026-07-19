from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StrictBool, model_validator


class ClaimStatus(StrEnum):
    VERIFIED = "verified"
    ADJACENT = "adjacent"
    LEARNING = "learning"
    PROHIBITED = "prohibited"


class EvidenceReference(BaseModel):
    source_id: str
    source_name: str
    page: int | None = Field(default=None, ge=1)
    excerpt: str = Field(max_length=500)
    content_hash: str


class ProfileClaim(BaseModel):
    key: str
    value: str
    status: ClaimStatus
    evidence: list[EvidenceReference] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0, le=1)
    user_verified_at: datetime | None = None


class ProjectAnalysis(BaseModel):
    source: Literal["local", "github"]
    repository_name: str
    analyzed_at: datetime
    file_count: int = Field(ge=0)
    primary_languages: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    notable_files: list[str] = Field(default_factory=list)
    improvement_suggestions: list[str] = Field(default_factory=list)
    interview_questions: list[str] = Field(default_factory=list)
    summary: str = ""


class ProfileProject(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=3000)
    repository_url: HttpUrl | None = None
    local_path: str = Field(default="", max_length=1200)
    technologies: list[str] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)
    analysis: ProjectAnalysis | None = None


class CandidateProfile(BaseModel):
    id: str | None = None
    display_name: str = ""
    seniority: str = ""
    email: str = ""
    phone: str = ""
    claims: list[ProfileClaim] = Field(default_factory=list)
    target_roles: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    projects: list[ProfileProject] = Field(default_factory=list)
    work_authorization: list[ProfileClaim] = Field(default_factory=list)
    source_documents: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class JobSpec(BaseModel):
    title: str
    company: str
    locations: list[str] = Field(default_factory=list)
    description: str
    requirements: list[str] = Field(default_factory=list)
    preferred: list[str] = Field(default_factory=list)
    seniority: str = "unknown"
    employment_type: str = "unknown"
    workplace_type: Literal["onsite", "hybrid", "remote", "unknown"] = "unknown"
    company_size: Literal[
        "1-10",
        "11-50",
        "51-200",
        "201-500",
        "501-1000",
        "1001-5000",
        "5001-10000",
        "10001+",
        "unknown",
    ] = "unknown"
    posted_date: date | None = None
    deadline: date | None = None
    source_url: HttpUrl | None = None
    source_type: str = "manual"


class Job(BaseModel):
    id: str | None = None
    spec: JobSpec
    canonical_url: str = ""
    fingerprint: str = ""
    freshness_days: int | None = None
    score: float | None = None
    tier: str | None = None
    score_explanation: list[str] = Field(default_factory=list)
    evidence: list[EvidenceReference] = Field(default_factory=list)
    created_at: datetime | None = None


class ApplicationStatus(StrEnum):
    DISCOVERED = "discovered"
    SCORED = "scored"
    APPROVED = "approved"
    TAILORING = "tailoring"
    READY = "ready"
    FORM_PREVIEWED = "form_previewed"
    FORM_FILLED = "form_filled"
    SUBMITTED = "submitted"
    FOLLOWED_UP = "followed_up"
    OA = "oa"
    OA_FAILED = "oa_failed"
    INTERVIEW = "interview"
    INTERVIEW_1 = "interview_1"
    INTERVIEW_1_FAILED = "interview_1_failed"
    INTERVIEW_2 = "interview_2"
    INTERVIEW_2_FAILED = "interview_2_failed"
    FINAL_INTERVIEW = "final_interview"
    FINAL_INTERVIEW_FAILED = "final_interview_failed"
    OFFER = "offer"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NO_RESPONSE = "no_response"
    WITHDRAWN = "withdrawn"


class FormFieldKey(StrEnum):
    FULL_NAME = "full_name"
    EMAIL = "email"
    PHONE = "phone"
    LOCATION = "location"
    WORK_AUTHORIZATION = "work_authorization"
    RESUME = "resume"
    COVER_LETTER = "cover_letter"
    UNSUPPORTED = "unsupported"


class FormFieldState(StrEnum):
    MAPPED = "mapped"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"


class FormFieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$",
    )
    label: str = Field(min_length=1, max_length=300)
    field_key: FormFieldKey
    required: StrictBool = False
    options: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list,
        max_length=50,
    )

    @model_validator(mode="after")
    def validate_options(self) -> "FormFieldSpec":
        normalized = [" ".join(value.split()).casefold() for value in self.options]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Form field options must be unique")
        if self.field_key in {FormFieldKey.RESUME, FormFieldKey.COVER_LETTER} and self.options:
            raise ValueError("Artifact fields cannot define choice options")
        return self


class FormPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    form_reference: str = Field(min_length=1, max_length=300)
    fields: list[FormFieldSpec] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_field_ids(self) -> "FormPreviewRequest":
        field_ids = [field.field_id for field in self.fields]
        if len(set(field_ids)) != len(field_ids):
            raise ValueError("Form field IDs must be unique")
        return self


class ArtifactVersion(BaseModel):
    id: str | None = None
    kind: Literal["cv", "cover_letter", "gap_analysis", "honesty_ledger", "interview_plan"]
    version: int
    path: str
    sha256: str
    approved: bool = False
    created_at: datetime | None = None


class Application(BaseModel):
    id: str | None = None
    job_id: str
    status: ApplicationStatus = ApplicationStatus.DISCOVERED
    next_action: str = "Review fit"
    next_action_at: datetime | None = None
    artifacts: list[ArtifactVersion] = Field(default_factory=list)
    status_events: list[dict[str, Any]] = Field(default_factory=list)
    submitted_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ApprovalDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class Approval(BaseModel):
    id: str | None = None
    action_type: str
    payload_digest: str
    preview: dict[str, Any]
    expires_at: datetime
    decision: ApprovalDecision = ApprovalDecision.PENDING
    decided_at: datetime | None = None
    created_at: datetime | None = None


class RevisionStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    ROLLED_BACK = "rolled_back"


class RevisionBase(BaseModel):
    id: str | None = None
    name: str
    version: int
    content: dict[str, Any]
    diff: str
    author: str
    source_session: str
    status: RevisionStatus = RevisionStatus.DRAFT
    evaluation: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class SkillRevision(RevisionBase):
    kind: Literal["skill"] = "skill"


class RubricRevision(RevisionBase):
    kind: Literal["rubric"] = "rubric"


class ModelRoute(BaseModel):
    name: Literal[
        "interactive",
        "research",
        "extraction",
        "tailoring",
        "evaluation",
        "memory-review",
        "compression",
        "cron",
    ]
    provider: Literal["openai-api", "openai-codex"]
    model: str
    reasoning_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh"
    ] = "low"
    token_limit: int = Field(default=4000, ge=128, le=200000)
    cost_budget_usd: float = Field(default=0.25, ge=0)
    fallback_policy: Literal["none", "same-provider", "explicit"] = "none"
    fallback_route: str | None = None
    scheduled: bool = False


class UsageRun(BaseModel):
    route: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0
    created_at: datetime | None = None


class ProviderChoice(BaseModel):
    provider: Literal["openai-api", "openai-codex"]
    api_key: str | None = Field(default=None, repr=False)


class Schedule(BaseModel):
    id: str | None = None
    name: str
    cron: str
    route: str
    task: str
    enabled: bool = False
    requires_approval: bool = True
    next_run_at: datetime | None = None


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=80)
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str = Field(default="", max_length=1_000)
    transport: Literal["stdio", "http"]
    command: str | None = Field(default=None, min_length=1, max_length=512)
    args: list[Annotated[str, Field(max_length=1_024)]] = Field(
        default_factory=list,
        max_length=64,
    )
    url: str | None = Field(default=None, min_length=1, max_length=2_048)
    tool_allowlist: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(
        default_factory=list,
        max_length=128,
    )
    forwarded_environment: list[
        Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    ] = Field(default_factory=list, max_length=64)
    environment: dict[str, str] = Field(default_factory=dict)
    enabled: StrictBool = False

    @model_validator(mode="after")
    def validate_runtime_boundary(self) -> "MCPServerConfig":
        from career_companion.services.mcp_servers import (
            _configured_mcp_tool_names_from_servers,
            _validate_mcp_server_runtime_config,
        )

        payload = self.model_dump(mode="python")
        if payload["display_name"] is None:
            payload["display_name"] = self.name.replace("-", " ").title()
        _validate_mcp_server_runtime_config(payload)
        _configured_mcp_tool_names_from_servers([payload])
        return self
