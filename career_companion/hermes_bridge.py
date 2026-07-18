from __future__ import annotations

import ipaddress
import re
import secrets
from collections.abc import Generator
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hermes_runtime import profile_distribution_directory
from career_companion.config import load_config
from career_companion.database import (
    ApplicationRecord,
    CandidateProfileRecord,
    ConversationMessageRecord,
    JobRecord,
    MCPServerRecord,
    ModelRouteRecord,
    ScheduleRecord,
    StatusEventRecord,
)
from career_companion.paths import CompanionPaths
from career_companion.persistence import account_session
from career_companion.router import _application_json, _job_json, _revision_json
from career_companion.schemas import ApplicationStatus, Job, JobSpec
from career_companion.services.applications import (
    decide_scored_application,
    ensure_application,
    transition_application,
)
from career_companion.services.browser import BrowserAssistant
from career_companion.services.conversation_sessions import (
    agent_profile_json,
    ensure_agent_profile,
    update_agent_profile,
)
from career_companion.services.discovery import DiscoveryError, discover_public_jobs
from career_companion.services.jobs import add_job, score_job
from career_companion.services.revisions import create_revision

router = APIRouter(
    prefix="/api/internal/hermes/v1",
    tags=["hermes-bridge"],
    include_in_schema=False,
)

_ACCOUNT_KEY_PATTERN = re.compile(r"[a-f0-9]{64}")
_TRACKED_SELECTION_NOTE = "Deterministic priority recorded after explicit user selection"
_APPROVE_PATTERN = re.compile(r"\bapprov(?:e|ed|ing)\b", re.IGNORECASE)
_ARCHIVE_PATTERN = re.compile(
    r"\b(?:archiv(?:e|ed|ing)|withdraw(?:n|ing)?)\b",
    re.IGNORECASE,
)
_UNCERTAIN_DECISION_PATTERN = re.compile(
    r"\b(?:maybe|perhaps|possibly|unsure|uncertain|consider|might|may|could)\b",
    re.IGNORECASE,
)
_CONDITIONAL_DECISION_PATTERN = re.compile(
    r"\b(?:if|unless|until|after|before|once|when|pending|provided|assuming)\b"
    r"|\bsubject\s+to\b|\bonly\s+(?:if|after|when|once)\b",
    re.IGNORECASE,
)
_REVOKED_DECISION_PATTERN = re.compile(
    r"\bactually\s*,?\s*no\b|\bnever\s+mind\b|\bcancel(?:\s+that)?\b|"
    r"\brevoke(?:\s+that)?\b|\bscratch\s+that\b|\binstead\b|"
    r"\bbut\b[^.]{0,120}\b(?:no|not|cancel|revoke)\b",
    re.IGNORECASE,
)
_NEGATED_DECISION_PATTERN = re.compile(
    r"\b(?:do\s+not|don['’]?t|cannot|can['’]?t|will\s+not|won['’]?t|"
    r"never|not|no|without|refuse|decline|reject|avoid|against)\b"
    r"(?:\W+\w+){0,4}\W+"
    r"(?:approv(?:e|ed|ing)|archiv(?:e|ed|ing)|withdraw(?:n|ing)?)\b",
    re.IGNORECASE,
)
_DECISION_REFERENCE_FILLER = {
    "application",
    "at",
    "for",
    "job",
    "my",
    "opportunity",
    "please",
    "role",
    "saved",
    "the",
    "this",
    "tracked",
}
_PILOT_GATED_APPLICATION_STATUSES = {
    ApplicationStatus.SCORED,
    ApplicationStatus.APPROVED,
    ApplicationStatus.WITHDRAWN,
    ApplicationStatus.TAILORING,
    ApplicationStatus.READY,
    ApplicationStatus.FORM_FILLED,
}


class ApplicationTransition(BaseModel):
    status: ApplicationStatus
    note: str = ""


class FormFillPayload(BaseModel):
    application_id: str
    url: str
    fields: dict[str, str] = Field(default_factory=dict)
    files: dict[str, str] = Field(default_factory=dict)
    headless: bool = False


class BoundToolPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IdentityUpdatePayload(BoundToolPayload):
    name: str = Field(min_length=1, max_length=80)
    soul: str = Field(default="", max_length=32_768)


class PublicJobDiscoveryPayload(BaseModel):
    provider: Literal["greenhouse", "lever"]
    company_identifier: str = Field(min_length=1, max_length=100)
    limit: int = Field(default=25, ge=1, le=50)


class SelectedJobTrackingPayload(BoundToolPayload):
    selection_reference: str = Field(min_length=1, max_length=500)


class ScoredApplicationDecisionPayload(BoundToolPayload):
    decision: Literal["approve", "archive"]
    decision_reference: str = Field(min_length=1, max_length=500)


class HermesRevisionPayload(BoundToolPayload):
    kind: Literal["memory", "skill", "rubric"]
    name: str
    content: dict[str, Any]
    diff: str


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Hermes bridge authentication failed",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_bridge_paths(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    account_key: Annotated[str | None, Header(alias="X-Career-Account")] = None,
) -> CompanionPaths:
    """Resolve an opaque account workspace from a per-account bearer secret."""

    try:
        client_is_loopback = bool(
            request.client and ipaddress.ip_address(request.client.host).is_loopback
        )
    except ValueError:
        client_is_loopback = False
    if not client_is_loopback:
        raise _unauthorized()
    if not account_key or not _ACCOUNT_KEY_PATTERN.fullmatch(account_key):
        raise _unauthorized()
    if not authorization or not authorization.startswith("Bearer "):
        raise _unauthorized()
    supplied = authorization.removeprefix("Bearer ").strip()
    if not supplied:
        raise _unauthorized()

    base = CompanionPaths.discover()
    paths = CompanionPaths.at_root(base.root / "accounts" / account_key, base.config.parent)
    try:
        expected = paths.hermes_bridge_token.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise _unauthorized() from exc
    if not secrets.compare_digest(supplied, expected):
        raise _unauthorized()
    return paths


def get_bridge_session(
    paths: Annotated[CompanionPaths, Depends(get_bridge_paths)],
) -> Generator[Session, None, None]:
    with account_session(paths) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_bridge_session)]
PathsDep = Annotated[CompanionPaths, Depends(get_bridge_paths)]


def get_bound_run_message(
    session: SessionDep,
    run_message_id: Annotated[
        str | None,
        Header(alias="X-Career-Run-Message"),
    ] = None,
) -> ConversationMessageRecord:
    if run_message_id is None:
        raise HTTPException(
            409,
            "This career tool requires a server-bound Pilot run message",
        )
    message = session.get(ConversationMessageRecord, run_message_id)
    if message is None or message.role != "user":
        raise HTTPException(409, "Pilot run message binding is invalid")
    conversation = message.conversation
    if not conversation.messages or conversation.messages[-1].id != message.id:
        raise HTTPException(
            409,
            "Pilot run message is no longer the latest message in its conversation",
        )
    return message


RunMessageDep = Annotated[ConversationMessageRecord, Depends(get_bound_run_message)]


@router.get("/profile")
def read_profile(session: SessionDep) -> dict[str, Any] | None:
    row = session.scalar(
        select(CandidateProfileRecord).order_by(CandidateProfileRecord.updated_at.desc())
    )
    if row is None:
        return None
    return {"id": row.id, **row.payload, "updated_at": row.updated_at}


@router.get("/identity")
def read_identity(session: SessionDep, paths: PathsDep) -> dict[str, Any]:
    profile = ensure_agent_profile(session, paths)
    return agent_profile_json(
        profile,
        paths,
        profile_distribution_directory(),
    )


@router.post("/identity")
def update_identity(
    payload: IdentityUpdatePayload,
    session: SessionDep,
    paths: PathsDep,
    run_message: RunMessageDep,
) -> dict[str, Any]:
    try:
        profile = update_agent_profile(
            session,
            paths,
            profile_distribution_directory(),
            name=payload.name,
            soul=payload.soul,
            actor="career-agent",
            source_session=run_message.session_id,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return agent_profile_json(
        profile,
        paths,
        profile_distribution_directory(),
    )


@router.post("/jobs")
def create_job(job: Job, session: SessionDep) -> dict[str, Any]:
    row, created = add_job(session, job)
    return _job_json(row) | {"created": created}


@router.post("/jobs/discover-public")
async def discover_public_job_feed(
    payload: PublicJobDiscoveryPayload,
    paths: PathsDep,
) -> dict[str, Any]:
    """Perform an explicit public network read without storing its results."""

    del paths
    try:
        discovered = await discover_public_jobs(
            payload.provider,
            payload.company_identifier,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except DiscoveryError as exc:
        raise HTTPException(502, str(exc)) from exc
    returned = discovered[: payload.limit]
    return {
        "activity": {
            "type": "public_network_read",
            "provider": payload.provider,
            "company_identifier": payload.company_identifier.strip(),
        },
        "discovered": len(discovered),
        "returned": len(returned),
        "jobs": [job.spec.model_dump(mode="json") for job in returned],
        "stored": 0,
    }


@router.get("/jobs")
def list_jobs(
    session: SessionDep,
    limit: int = Query(default=10, ge=1, le=50),
) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(JobRecord).order_by(JobRecord.score.desc(), JobRecord.created_at.desc()).limit(limit)
    ).all()
    return [_job_json(row) for row in rows]


@router.post("/jobs/{job_id}/score")
def run_score(job_id: str, session: SessionDep) -> dict[str, Any]:
    try:
        return _job_json(score_job(session, job_id))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/jobs/{job_id}/track-selected")
def track_selected_job(
    job_id: str,
    payload: SelectedJobTrackingPayload,
    session: SessionDep,
    run_message: RunMessageDep,
) -> dict[str, Any]:
    job = session.get(JobRecord, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    current_request = run_message.content
    selection_reference = payload.selection_reference.strip()
    if not selection_reference or selection_reference not in current_request:
        raise HTTPException(
            409,
            "Selection reference must be copied from the latest user message",
        )

    ranked_job = score_job(session, job.id)
    application, application_created = ensure_application(session, ranked_job.id)
    if application.status == ApplicationStatus.DISCOVERED.value:
        application = transition_application(
            session,
            application.id,
            ApplicationStatus.SCORED,
            note=_TRACKED_SELECTION_NOTE,
        )
    spec = JobSpec.model_validate(ranked_job.normalized_spec)
    return {
        "activity": {
            "type": "local_write",
            "operations": ["deterministic_rank", "application_track"],
            "public_network_read": False,
        },
        "selection": {
            "source_session": run_message.session_id,
            "run_message_id": run_message.id,
            "reference": selection_reference,
        },
        "queued_job": {
            "id": ranked_job.id,
            "company": ranked_job.company,
            "title": ranked_job.title,
            "locations": spec.locations,
            "canonical_url": ranked_job.canonical_url,
            "source_type": ranked_job.source_type,
        },
        "deterministic_priority": {
            "score": ranked_job.score,
            "tier": ranked_job.tier,
            "explanation": ranked_job.score_explanation,
        },
        "application": {
            "id": application.id,
            "job_id": application.job_id,
            "status": application.status,
            "created": application_created,
        },
        "next_safe_action": _tracking_next_safe_action(application.status),
    }


@router.post("/applications/{application_id}/decide")
def decide_tracked_application(
    application_id: str,
    payload: ScoredApplicationDecisionPayload,
    session: SessionDep,
    run_message: RunMessageDep,
) -> dict[str, Any]:
    application = session.get(ApplicationRecord, application_id)
    if application is None:
        raise HTTPException(404, "Application not found")
    job = session.get(JobRecord, application.job_id)
    if job is None:
        raise HTTPException(409, "The application is not linked to a saved job")
    current_request = run_message.content
    decision_reference = payload.decision_reference.strip()
    if not decision_reference or decision_reference not in current_request:
        raise HTTPException(
            409,
            "Decision reference must be copied from the latest user message",
        )
    tracked_event = session.scalar(
        select(StatusEventRecord).where(
            StatusEventRecord.application_id == application.id,
            StatusEventRecord.from_status == ApplicationStatus.DISCOVERED.value,
            StatusEventRecord.to_status == ApplicationStatus.SCORED.value,
            StatusEventRecord.note == _TRACKED_SELECTION_NOTE,
        )
    )
    if job.score is None or job.tier is None or tracked_event is None:
        raise HTTPException(
            409,
            "Application decisions require the explicit selected-job tracking path",
        )

    _validate_explicit_application_decision(
        session,
        application,
        job,
        payload.decision,
        current_request,
        decision_reference,
    )
    target = (
        ApplicationStatus.APPROVED
        if payload.decision == "approve"
        else ApplicationStatus.WITHDRAWN
    )
    try:
        result = decide_scored_application(
            session,
            application.id,
            target,
            source_session=run_message.session_id,
            run_message_id=run_message.id,
            user_request=current_request,
            decision_reference=decision_reference,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (PermissionError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    application = result.application

    return {
        "activity": {
            "type": "local_write",
            "operations": ["scored_application_decision"],
            "public_network_read": False,
        },
        "decision": {
            "action": payload.decision,
            "source_session": run_message.session_id,
        },
        "queued_job": {
            "id": job.id,
            "company": job.company,
            "title": job.title,
            "canonical_url": job.canonical_url,
        },
        "application": {
            "id": application.id,
            "job_id": application.job_id,
            "status": application.status,
        },
        "audit": {
            "from_status": ApplicationStatus.SCORED.value,
            "to_status": application.status,
            "status_event_created": result.status_event_created,
            "idempotent_replay": not result.status_event_created,
            "run_message_id": run_message.id,
            "user_request_sha256": result.user_request_sha256,
            "decision_reference_sha256": result.decision_reference_sha256,
            "decision_fingerprint": result.decision_fingerprint,
        },
        "next_safe_action": _decision_next_safe_action(application.status),
    }


@router.get("/applications")
def list_applications(
    session: SessionDep,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(ApplicationRecord)
        .order_by(ApplicationRecord.updated_at.desc())
        .limit(limit)
    ).all()
    return [_application_json(row) for row in rows]


@router.post("/applications/{application_id}/status")
def set_application_status(
    application_id: str,
    payload: ApplicationTransition,
    session: SessionDep,
) -> dict[str, Any]:
    if payload.status is ApplicationStatus.SUBMITTED:
        raise HTTPException(403, "Hermes cannot record application submission")
    if payload.status in _PILOT_GATED_APPLICATION_STATUSES:
        raise HTTPException(
            403,
            "Pilot cannot use generic status updates for selected-job scoring, "
            "approval, archive, tailoring, readiness, or form completion",
        )
    try:
        row = transition_application(
            session,
            application_id,
            payload.status,
            note=payload.note,
            confirmed_by_user=False,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (PermissionError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return _application_json(row)


@router.post("/revisions")
def propose_revision(
    payload: HermesRevisionPayload,
    session: SessionDep,
    run_message: RunMessageDep,
) -> dict[str, Any]:
    data = payload.model_dump()
    data["author"] = "career-agent"
    data["source_session"] = run_message.session_id
    try:
        return _revision_json(create_revision(session, **data))
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@router.get("/policy")
def policy_status(session: SessionDep, paths: PathsDep) -> dict[str, Any]:
    routes = session.scalars(select(ModelRouteRecord)).all()
    schedules = session.scalars(select(ScheduleRecord)).all()
    mcp_servers = session.scalars(select(MCPServerRecord)).all()
    config = load_config(paths)
    return {
        "model_routes": [
            {
                "name": row.name,
                "provider": row.provider,
                "model": row.model,
                "reasoning_effort": row.reasoning_effort,
                "token_limit": row.token_limit,
                "cost_budget_usd": row.cost_budget_usd,
                "fallback_policy": row.fallback_policy,
                "fallback_route": row.fallback_route,
                "scheduled": row.scheduled,
            }
            for row in routes
        ],
        "schedules": [
            {
                "id": row.id,
                "name": row.name,
                "cron": row.cron,
                "route": row.route,
                "enabled": row.enabled,
                "requires_approval": row.requires_approval,
                "next_run_at": row.next_run_at,
            }
            for row in schedules
        ],
        "mcp_servers": [
            {
                "name": row.name,
                "transport": row.transport,
                "enabled": row.enabled,
                "tool_allowlist": row.config.get("tool_allowlist", []),
            }
            for row in mcp_servers
        ],
        "daily_api_budget_usd": config.daily_api_budget_usd,
    }


@router.post("/browser/fill")
async def fill_form(
    payload: FormFillPayload,
    session: SessionDep,
    paths: PathsDep,
) -> dict[str, Any]:
    browser = BrowserAssistant(paths)
    try:
        return await browser.fill(session, payload.model_dump())
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        await browser.close()


def _tracking_next_safe_action(application_status: str) -> str:
    if application_status == ApplicationStatus.SCORED.value:
        return "Ask the user whether to approve or archive this opportunity"
    return (
        f"Review the existing {application_status} application without advancing it "
        "or claiming any materials are complete"
    )


def _validate_explicit_application_decision(
    session: Session,
    application: ApplicationRecord,
    job: JobRecord,
    decision: Literal["approve", "archive"],
    current_request: str,
    decision_reference: str,
) -> None:
    approve_matches = list(_APPROVE_PATTERN.finditer(current_request))
    archive_matches = list(_ARCHIVE_PATTERN.finditer(current_request))
    expected_pattern = _APPROVE_PATTERN if decision == "approve" else _ARCHIVE_PATTERN
    other_pattern = _ARCHIVE_PATTERN if decision == "approve" else _APPROVE_PATTERN
    if (
        len(list(expected_pattern.finditer(decision_reference))) != 1
        or other_pattern.search(decision_reference) is not None
    ):
        raise HTTPException(
            409,
            "Decision reference must explicitly state the requested decision",
        )
    if (len(approve_matches), len(archive_matches)) != (
        (1, 0) if decision == "approve" else (0, 1)
    ):
        raise HTTPException(
            409,
            "The latest user message must contain exactly one decision clause",
        )
    if (
        "?" in current_request
        or _UNCERTAIN_DECISION_PATTERN.search(current_request) is not None
        or _CONDITIONAL_DECISION_PATTERN.search(current_request) is not None
        or _REVOKED_DECISION_PATTERN.search(current_request) is not None
        or _NEGATED_DECISION_PATTERN.search(current_request) is not None
    ):
        raise HTTPException(
            409,
            "The latest user message must contain an affirmative, unconditional decision",
        )

    reference_identity = _decision_reference_identity(decision_reference)
    references_named_job = reference_identity in {
        _decision_reference_identity(f"{job.company} {job.title}"),
        _decision_reference_identity(f"{job.title} {job.company}"),
    }
    references_url = bool(
        job.canonical_url and job.canonical_url in decision_reference
    )
    references_application = application.id in decision_reference
    duplicate_job = session.scalar(
        select(JobRecord.id).where(
            JobRecord.company == job.company,
            JobRecord.title == job.title,
            JobRecord.id != job.id,
        )
    )
    if duplicate_job is not None and not (references_url or references_application):
        raise HTTPException(
            409,
            "Company and title are ambiguous; copy the exact application ID or URL",
        )
    if not (references_named_job or references_url or references_application):
        raise HTTPException(
            409,
            "Decision reference does not identify this saved application",
        )


def _decision_next_safe_action(application_status: str) -> str:
    if application_status == ApplicationStatus.APPROVED.value:
        return (
            "Wait for a new explicit user request before starting evidence-backed "
            "tailoring; no artifacts have been generated"
        )
    return "No further action; the opportunity is archived locally"


def _decision_reference_identity(value: str) -> tuple[str, ...]:
    without_actions = _APPROVE_PATTERN.sub(" ", value)
    without_actions = _ARCHIVE_PATTERN.sub(" ", without_actions)
    return tuple(
        token
        for token in re.findall(r"\w+", without_actions.casefold())
        if token not in _DECISION_REFERENCE_FILLER
    )
