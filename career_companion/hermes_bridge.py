from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import unicodedata
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
from career_companion.schemas import (
    ApplicationStatus,
    FormFieldKey,
    FormFieldSpec,
    FormPreviewRequest,
    Job,
    JobSpec,
)
from career_companion.services.applications import (
    ApplicationPersistenceError,
    decide_scored_application,
    ensure_application,
    transition_application,
)
from career_companion.services.conversation_sessions import (
    agent_profile_json,
    ensure_agent_profile,
    update_agent_profile,
)
from career_companion.services.discovery import DiscoveryError, discover_public_jobs
from career_companion.services.form_preview import prepare_form_preview
from career_companion.services.jobs import add_job, score_job
from career_companion.services.revisions import (
    MemoryPreferenceConflictError,
    MemoryPreferenceProvenanceError,
    MemoryPreferenceValidationError,
    create_revision,
    propose_memory_preference,
)

router = APIRouter(
    prefix="/api/internal/hermes/v1",
    tags=["hermes-bridge"],
    include_in_schema=False,
)

_ACCOUNT_KEY_PATTERN = re.compile(r"[a-f0-9]{64}")
_TRACKED_SELECTION_NOTE = "Deterministic priority recorded after explicit user selection"
_DIRECTIVE_PATTERN = re.compile(
    r"\A(?:(?:please)(?:,\s+|\s+))?"
    r"(?P<action>track|approve|archive)\s+"
    r"(?P<identity>\S(?:.*\S)?)\Z",
    re.IGNORECASE,
)
_FORM_PREVIEW_DIRECTIVE_PATTERN = re.compile(
    r"\A(?:(?:please)(?:,\s+|\s+))?preview\s+application\s+"
    r"(?P<application_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s+"
    r"fields:\s+(?P<fields>[a-z_]+(?:,\s*[a-z_]+)*)(?:[.!])?\Z",
    re.IGNORECASE,
)
_PILOT_GATED_APPLICATION_STATUSES = {
    ApplicationStatus.SCORED,
    ApplicationStatus.APPROVED,
    ApplicationStatus.WITHDRAWN,
    ApplicationStatus.TAILORING,
    ApplicationStatus.READY,
    ApplicationStatus.FORM_PREVIEWED,
    ApplicationStatus.FORM_FILLED,
}


class ApplicationTransition(BaseModel):
    status: ApplicationStatus
    note: str = ""


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
    kind: Literal["skill", "rubric"]
    name: str
    content: dict[str, Any]
    diff: str


class MemoryPreferenceProposalPayload(BoundToolPayload):
    correction_phrase: str = Field(min_length=1, max_length=200)


class FormPreviewPayload(BoundToolPayload):
    preview_reference: str = Field(min_length=1, max_length=1_000)


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
    latest_id = session.scalar(
        select(ConversationMessageRecord.id)
        .where(
            ConversationMessageRecord.session_id == message.session_id,
            ConversationMessageRecord.role == "user",
        )
        .order_by(
            ConversationMessageRecord.position.desc(),
            ConversationMessageRecord.id.desc(),
        )
        .limit(1)
    )
    if latest_id != message.id:
        raise HTTPException(
            409,
            "Pilot run message is no longer the latest user message in its conversation",
        )
    return message


RunMessageDep = Annotated[ConversationMessageRecord, Depends(get_bound_run_message)]


def _rebind_latest_run_message_for_write(
    session: Session,
    run_message_id: str,
) -> ConversationMessageRecord:
    """Reserve SQLite writes and recheck authoritative message freshness."""

    session.rollback()
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    message = session.get(ConversationMessageRecord, run_message_id)
    if message is None or message.role != "user":
        raise HTTPException(409, "Pilot run message binding is invalid")
    latest_id = session.scalar(
        select(ConversationMessageRecord.id)
        .where(
            ConversationMessageRecord.session_id == message.session_id,
            ConversationMessageRecord.role == "user",
        )
        .order_by(
            ConversationMessageRecord.position.desc(),
            ConversationMessageRecord.id.desc(),
        )
        .limit(1)
    )
    if latest_id != message.id:
        raise HTTPException(
            409,
            "Pilot run message is no longer the latest user message in its conversation",
        )
    return message


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
    run_message = _rebind_latest_run_message_for_write(session, run_message.id)
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
    run_message = _rebind_latest_run_message_for_write(session, run_message.id)
    job = session.get(JobRecord, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    current_request = run_message.content
    _validate_explicit_tracking_directive(
        session,
        job,
        current_request,
        payload.selection_reference,
    )

    ranked_job = score_job(session, job.id)
    try:
        application, application_created = ensure_application(session, ranked_job.id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ApplicationPersistenceError as exc:
        raise HTTPException(409, str(exc)) from exc
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
            "reference": current_request,
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
    run_message = _rebind_latest_run_message_for_write(session, run_message.id)
    application = session.get(ApplicationRecord, application_id)
    if application is None:
        raise HTTPException(404, "Application not found")
    job = session.get(JobRecord, application.job_id)
    if job is None:
        raise HTTPException(409, "The application is not linked to a saved job")
    current_request = run_message.content
    decision_reference = payload.decision_reference
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


@router.post("/applications/{application_id}/form-preview")
def preview_application_form(
    application_id: str,
    payload: FormPreviewPayload,
    session: SessionDep,
    paths: PathsDep,
    run_message: RunMessageDep,
) -> dict[str, Any]:
    run_message = _rebind_latest_run_message_for_write(session, run_message.id)
    if payload.preview_reference != run_message.content:
        raise HTTPException(
            409,
            "Preview reference must exactly equal the latest user message",
        )
    bound_application_id, field_keys = _parse_form_preview_directive(
        run_message.content
    )
    if bound_application_id != application_id:
        raise HTTPException(
            409,
            "Preview directive does not identify this application exactly",
        )
    request = FormPreviewRequest(
        form_reference=f"Pilot field checklist for application {application_id}",
        fields=[
            FormFieldSpec(
                field_id=f"pilot-{index + 1}-{field_key.value}",
                label=_FORM_PREVIEW_LABELS[field_key],
                field_key=field_key,
                required=True,
            )
            for index, field_key in enumerate(field_keys)
        ],
    )
    try:
        preview, created = prepare_form_preview(
            session,
            application_id,
            paths,
            request,
            source_session=run_message.session_id,
            source_message_id=run_message.id,
            source_message_sha256=hashlib.sha256(
                run_message.content.encode("utf-8")
            ).hexdigest(),
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "activity": {
            "type": "local_write",
            "operations": ["evidence_bound_form_preview"],
            "public_network_read": False,
            "browser_used": False,
            "external_mutation_performed": False,
        },
        "preview": preview,
        "created": created,
        "next_safe_action": (
            "Review the local preview and resolve every unknown, unsupported, or "
            "ambiguous field. No external fill phase is available."
        ),
    }


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
    run_message = _rebind_latest_run_message_for_write(session, run_message.id)
    data = payload.model_dump()
    data["author"] = "career-agent"
    data["source_session"] = run_message.session_id
    try:
        return _revision_json(create_revision(session, **data))
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@router.post("/memory/preferences")
def propose_user_memory_preference(
    payload: MemoryPreferenceProposalPayload,
    session: SessionDep,
    run_message: RunMessageDep,
) -> dict[str, Any]:
    run_message = _rebind_latest_run_message_for_write(session, run_message.id)
    try:
        revision, created = propose_memory_preference(
            session,
            source_session=run_message.session_id,
            user_request=run_message.content,
            correction_phrase=payload.correction_phrase,
            author="career-agent",
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (MemoryPreferenceProvenanceError, MemoryPreferenceConflictError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except MemoryPreferenceValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _revision_json(revision) | {
        "created": created,
        "review_required": True,
    }


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
    if decision_reference != current_request:
        raise HTTPException(
            409,
            "Decision reference must exactly equal the latest user message",
        )
    action, identities = _parse_bound_directive(current_request)
    if action != decision:
        raise HTTPException(409, "Decision action does not match the user directive")
    references_named_job = _matches_named_job_identity(identities, job)
    references_url = bool(job.canonical_url and job.canonical_url in identities)
    references_application = application.id in identities
    if _has_duplicate_job_identity(session, job) and not (
        references_url or references_application
    ):
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


def _validate_explicit_tracking_directive(
    session: Session,
    job: JobRecord,
    current_request: str,
    selection_reference: str,
) -> None:
    if selection_reference != current_request:
        raise HTTPException(
            409,
            "Selection reference must exactly equal the latest user message",
        )
    action, identities = _parse_bound_directive(current_request)
    if action != "track":
        raise HTTPException(409, "The latest user message is not a tracking directive")
    references_named_job = _matches_named_job_identity(identities, job)
    references_url = bool(job.canonical_url and job.canonical_url in identities)
    if _has_duplicate_job_identity(session, job) and not references_url:
        raise HTTPException(
            409,
            "Company and title are ambiguous; copy the exact canonical URL",
        )
    if not (references_named_job or references_url):
        raise HTTPException(
            409,
            "Tracking directive does not identify this saved job exactly",
        )


def _parse_bound_directive(message: str) -> tuple[str, tuple[str, ...]]:
    match = _DIRECTIVE_PATTERN.fullmatch(message)
    if match is None:
        raise HTTPException(
            409,
            "The latest user message is not one narrow whole-message directive",
        )
    identity = match.group("identity")
    identities = [identity]
    if identity.endswith((".", "!")):
        without_punctuation = identity[:-1].rstrip()
        if without_punctuation:
            identities.append(without_punctuation)
    return match.group("action").casefold(), tuple(identities)


_FORM_PREVIEW_LABELS = {
    FormFieldKey.FULL_NAME: "Full name",
    FormFieldKey.EMAIL: "Email",
    FormFieldKey.PHONE: "Phone",
    FormFieldKey.LOCATION: "Location",
    FormFieldKey.WORK_AUTHORIZATION: "Work authorization",
    FormFieldKey.RESUME: "Resume",
    FormFieldKey.COVER_LETTER: "Cover letter",
}
_PILOT_FORM_FIELD_KEYS = frozenset(_FORM_PREVIEW_LABELS)


def _parse_form_preview_directive(
    message: str,
) -> tuple[str, tuple[FormFieldKey, ...]]:
    match = _FORM_PREVIEW_DIRECTIVE_PATTERN.fullmatch(message)
    if match is None:
        raise HTTPException(
            409,
            "The latest user message is not one narrow form-preview directive",
        )
    raw_fields = tuple(
        value.strip().casefold() for value in match.group("fields").split(",")
    )
    try:
        fields = tuple(FormFieldKey(value) for value in raw_fields)
    except ValueError as exc:
        raise HTTPException(
            409,
            "Preview directive contains an unsupported field key",
        ) from exc
    if any(field not in _PILOT_FORM_FIELD_KEYS for field in fields):
        raise HTTPException(409, "Preview directive contains an unsupported field key")
    if len(set(fields)) != len(fields):
        raise HTTPException(409, "Preview directive field keys must be unique")
    return match.group("application_id").casefold(), fields


def _matches_named_job_identity(
    identities: tuple[str, ...],
    job: JobRecord,
) -> bool:
    named_identities = {
        _normalize_job_identity(f"{job.company} {job.title}"),
        _normalize_job_identity(f"{job.title} {job.company}"),
    }
    return any(
        _normalize_job_identity(identity) in named_identities for identity in identities
    )


def _has_duplicate_job_identity(session: Session, job: JobRecord) -> bool:
    company = _normalize_job_identity(job.company)
    title = _normalize_job_identity(job.title)
    return any(
        _normalize_job_identity(other.company) == company
        and _normalize_job_identity(other.title) == title
        for other in session.scalars(
            select(JobRecord).where(JobRecord.id != job.id)
        ).all()
    )


def _normalize_job_identity(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()
