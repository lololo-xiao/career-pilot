from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dependencies import get_companion_paths, get_companion_session
from career_companion.approval_history_router import router as approval_history_router
from career_companion.config import load_config, public_settings
from career_companion.database import (
    ApplicationRecord,
    ApprovalRecord,
    ArtifactRecord,
    AuditEventRecord,
    CandidateProfileRecord,
    JobRecord,
    MCPServerRecord,
    ModelRouteRecord,
    RevisionRecord,
    ScheduleRecord,
)
from career_companion.guided_discovery_router import router as guided_discovery_router
from career_companion.paths import CompanionPaths
from career_companion.schemas import (
    ApplicationStatus,
    CandidateProfile,
    FormFillRequest,
    FormPreviewRequest,
    Job,
    MCPServerConfig,
    ModelRoute,
    ProfileProject,
    Schedule,
)
from career_companion.services.applications import (
    ApplicationPersistenceError,
    create_application,
    transition_application,
)
from career_companion.services.approvals import decide_approval, request_approval
from career_companion.services.audit import record_audit
from career_companion.services.browser import BrowserAssistant
from career_companion.services.csv_imports import import_applications_csv, import_jobs_csv
from career_companion.services.discovery import (
    DiscoveryError,
    discover_greenhouse,
    discover_lever,
)
from career_companion.services.form_preview import (
    FORM_PREVIEW_ARTIFACT_KIND,
    latest_form_preview,
    list_latest_form_previews,
    prepare_form_preview,
)
from career_companion.services.form_fill import (
    FormFillRequestError,
    FormFillResourceNotFound,
)
from career_companion.services.jobs import add_job, score_job
from career_companion.services.model_routes import daily_cost, upsert_route
from career_companion.services.profile import import_profile_document, save_profile
from career_companion.services.projects import analyze_project
from career_companion.services.revisions import (
    MemoryRollbackConflictError,
    create_revision,
    evaluate_revision,
    rollback_revision,
)
from career_companion.services.tailoring import approve_artifact, generate_application_pack

router = APIRouter(prefix="/api/v1", tags=["career-companion"])
router.include_router(guided_discovery_router)
router.include_router(approval_history_router)
SessionDep = Annotated[Session, Depends(get_companion_session)]
PathsDep = Annotated[CompanionPaths, Depends(get_companion_paths)]


class ApprovalRequest(BaseModel):
    action_type: str
    payload: dict[str, Any]
    preview: dict[str, Any]
    ttl_minutes: int = Field(default=30, ge=1, le=1440)


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approved", "denied"]


class ApplicationTransitionRequest(BaseModel):
    status: ApplicationStatus
    note: str = ""
    confirmed_by_user: bool = False
    manual_override: bool = False


class RevisionRequest(BaseModel):
    kind: Literal["skill", "rubric"]
    name: str
    content: dict[str, Any]
    diff: str
    author: str = "agent"
    source_session: str


class EvaluationRequest(BaseModel):
    quality_passed: bool
    security_passed: bool
    cost_passed: bool
    metrics: dict[str, Any] = Field(default_factory=dict)


class ArtifactApprovalRequest(BaseModel):
    sha256: str = Field(min_length=64, max_length=64)


@router.get("/settings")
def settings(session: SessionDep, paths: PathsDep) -> dict[str, Any]:
    return {**public_settings(load_config(paths)), "daily_cost_usd": daily_cost(session)}


@router.post("/onboarding/import")
async def import_profile(
    session: SessionDep,
    paths: PathsDep,
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    original_filename = file.filename or "profile"
    suffix = Path(original_filename).suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise HTTPException(400, "Only PDF and DOCX files are accepted")
    paths.create()
    temporary = paths.imports / f"upload-{uuid.uuid4().hex}{suffix}"
    size = 0
    try:
        with temporary.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 20 * 1024 * 1024:
                    raise HTTPException(413, "Profile document exceeds 20 MB")
                handle.write(chunk)
        document, profile = import_profile_document(
            session,
            temporary,
            paths,
            original_filename=original_filename,
        )
    finally:
        await file.close()
        temporary.unlink(missing_ok=True)
    return {
        "source_document_id": document.id,
        "profile": profile.model_dump(mode="json"),
    }


@router.post("/onboarding/projects/analyze")
async def analyze_profile_project(
    project: ProfileProject, session: SessionDep
) -> dict[str, Any]:
    try:
        analysis = await analyze_project(project)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    record_audit(
        session,
        "profile.project_analyzed",
        subject_type="profile_project",
        subject_id=project.id,
        payload={
            "name": project.name,
            "source": analysis.source,
            "repository_name": analysis.repository_name,
            "file_count": analysis.file_count,
        },
    )
    return analysis.model_dump(mode="json")


@router.get("/onboarding/profile")
def get_profile(session: SessionDep) -> dict[str, Any] | None:
    record = session.scalar(
        select(CandidateProfileRecord).order_by(CandidateProfileRecord.updated_at.desc())
    )
    if not record:
        return None
    return {"id": record.id, **record.payload, "updated_at": record.updated_at}


@router.put("/onboarding/profile")
def put_profile(profile: CandidateProfile, session: SessionDep) -> dict[str, Any]:
    try:
        record = save_profile(session, profile)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"id": record.id, **record.payload, "updated_at": record.updated_at}


@router.post("/jobs")
def create_job(job: Job, session: SessionDep) -> dict[str, Any]:
    record, created = add_job(session, job)
    return _job_json(record) | {"created": created}


@router.post("/jobs/import")
async def import_jobs(file: Annotated[UploadFile, File()], session: SessionDep) -> dict[str, Any]:
    csv_text = await _read_csv_upload(file)
    try:
        return import_jobs_csv(session, csv_text).as_dict()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/jobs")
def list_jobs(session: SessionDep) -> list[dict[str, Any]]:
    return [_job_json(row) for row in session.scalars(select(JobRecord)).all()]


@router.post("/jobs/{job_id}/score")
def run_score(job_id: str, session: SessionDep) -> dict[str, Any]:
    try:
        return _job_json(score_job(session, job_id))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/discovery/greenhouse")
async def run_greenhouse(board_token: str, session: SessionDep) -> dict[str, int]:
    try:
        discovered = await discover_greenhouse(board_token)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except DiscoveryError as exc:
        raise HTTPException(502, str(exc)) from exc
    created = sum(add_job(session, job)[1] for job in discovered)
    return {"discovered": len(discovered), "created": created}


@router.post("/discovery/lever")
async def run_lever(company_slug: str, session: SessionDep) -> dict[str, int]:
    try:
        discovered = await discover_lever(company_slug)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except DiscoveryError as exc:
        raise HTTPException(502, str(exc)) from exc
    created = sum(add_job(session, job)[1] for job in discovered)
    return {"discovered": len(discovered), "created": created}


@router.post("/applications")
def start_application(job_id: str, session: SessionDep) -> dict[str, Any]:
    try:
        return _application_json(create_application(session, job_id))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ApplicationPersistenceError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/applications/import")
async def import_applications(
    file: Annotated[UploadFile, File()], session: SessionDep
) -> dict[str, Any]:
    csv_text = await _read_csv_upload(file)
    try:
        return import_applications_csv(session, csv_text).as_dict()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/applications")
def list_applications(session: SessionDep) -> list[dict[str, Any]]:
    return [_application_json(row) for row in session.scalars(select(ApplicationRecord)).all()]


@router.post("/applications/{application_id}/status")
def set_application_status(
    application_id: str, payload: ApplicationTransitionRequest, session: SessionDep
) -> dict[str, Any]:
    if payload.status in {
        ApplicationStatus.FORM_PREVIEWED,
        ApplicationStatus.FORM_FILLED,
    }:
        raise HTTPException(
            409,
            "Form preview and form completion require their dedicated workflows",
        )
    try:
        record = transition_application(
            session,
            application_id,
            payload.status,
            note=payload.note,
            confirmed_by_user=payload.confirmed_by_user,
            manual_override=payload.manual_override,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (PermissionError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return _application_json(record)


@router.get("/applications/form-previews")
def list_form_previews(session: SessionDep, paths: PathsDep) -> dict[str, dict[str, Any]]:
    return list_latest_form_previews(session, paths)


@router.get("/applications/{application_id}/form-preview")
def get_form_preview(
    application_id: str,
    session: SessionDep,
    paths: PathsDep,
) -> dict[str, Any]:
    try:
        return latest_form_preview(session, application_id, paths)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/applications/{application_id}/form-preview")
def create_form_preview(
    application_id: str,
    payload: FormPreviewRequest,
    session: SessionDep,
    paths: PathsDep,
) -> dict[str, Any]:
    try:
        preview, created = prepare_form_preview(
            session,
            application_id,
            paths,
            payload,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return preview | {"created": created}


@router.post("/applications/{application_id}/artifacts/generate")
def generate_artifacts(
    application_id: str, session: SessionDep, paths: PathsDep
) -> dict[str, Any]:
    try:
        return generate_application_pack(
            session, application_id, paths, load_config(paths)
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/artifacts/{artifact_id}/approve")
def approve_artifact_endpoint(
    artifact_id: str, payload: ArtifactApprovalRequest, session: SessionDep
) -> dict[str, Any]:
    try:
        row = approve_artifact(session, artifact_id, payload.sha256)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _artifact_json(row)


@router.get("/artifacts/{artifact_id}/content")
def artifact_content(
    artifact_id: str, session: SessionDep, paths: PathsDep
) -> FileResponse:
    row = session.get(ArtifactRecord, artifact_id)
    if not row:
        raise HTTPException(404, "Artifact not found")
    path = Path(row.path).resolve()
    try:
        path.relative_to(paths.workspace.resolve())
    except ValueError as exc:
        raise HTTPException(403, "Artifact path is outside the workspace") from exc
    if not path.is_file():
        raise HTTPException(404, "Artifact file not found")
    return FileResponse(path, filename=path.name)


@router.get("/approvals")
def list_approvals(session: SessionDep) -> list[dict[str, Any]]:
    return [_approval_json(row) for row in session.scalars(select(ApprovalRecord)).all()]


@router.post("/approvals")
def create_approval(
    payload: ApprovalRequest,
    session: SessionDep,
    paths: PathsDep,
) -> dict[str, Any]:
    try:
        row = request_approval(
            session,
            payload.action_type,
            payload.payload,
            payload.preview,
            payload.ttl_minutes,
            paths=paths,
        )
    except FormFillRequestError as exc:
        raise HTTPException(422, str(exc)) from exc
    except FormFillResourceNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _approval_json(row)


@router.post("/approvals/{approval_id}/decision")
def resolve_approval(
    approval_id: str, payload: ApprovalDecisionRequest, session: SessionDep
) -> dict[str, Any]:
    try:
        return _approval_json(decide_approval(session, approval_id, payload.decision))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/model-routes")
def list_routes(session: SessionDep) -> list[dict[str, Any]]:
    return [_model_route_json(row) for row in session.scalars(select(ModelRouteRecord)).all()]


@router.put("/model-routes/{route_name}")
def set_route(route_name: str, route: ModelRoute, session: SessionDep) -> dict[str, Any]:
    if route.name != route_name:
        raise HTTPException(400, "Route name does not match URL")
    try:
        return _model_route_json(upsert_route(session, route))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/revisions")
def list_revisions(
    session: SessionDep, kind: str | None = Query(default=None)
) -> list[dict[str, Any]]:
    statement = select(RevisionRecord)
    if kind:
        statement = statement.where(RevisionRecord.kind == kind)
    return [_revision_json(row) for row in session.scalars(statement).all()]


@router.post("/revisions")
def new_revision(payload: RevisionRequest, session: SessionDep) -> dict[str, Any]:
    try:
        row = create_revision(session, **payload.model_dump())
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return _revision_json(row)


@router.post("/revisions/{revision_id}/evaluate")
def evaluate_revision_endpoint(
    revision_id: str, payload: EvaluationRequest, session: SessionDep
) -> dict[str, Any]:
    metrics = payload.metrics | {
        "quality_passed": payload.quality_passed,
        "security_passed": payload.security_passed,
        "cost_passed": payload.cost_passed,
    }
    try:
        return _revision_json(evaluate_revision(session, revision_id, metrics))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@router.post("/revisions/{revision_id}/rollback")
def rollback_revision_endpoint(revision_id: str, session: SessionDep) -> dict[str, Any]:
    try:
        return _revision_json(rollback_revision(session, revision_id))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except MemoryRollbackConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@router.get("/schedules")
def list_schedules(session: SessionDep) -> list[dict[str, Any]]:
    return [_schedule_json(row) for row in session.scalars(select(ScheduleRecord)).all()]


@router.post("/schedules")
def add_schedule(schedule: Schedule, session: SessionDep) -> dict[str, Any]:
    route = session.get(ModelRouteRecord, schedule.route)
    if not route:
        raise HTTPException(400, "Schedule route does not exist")
    if schedule.enabled and not route.scheduled:
        raise HTTPException(400, "Enabled schedules require a route pinned for scheduled use")
    row = ScheduleRecord(**schedule.model_dump(exclude={"id"}))
    session.add(row)
    session.flush()
    return _schedule_json(row)


@router.get("/mcp")
def list_mcp(session: SessionDep) -> list[dict[str, Any]]:
    return [
        {"name": row.name, "transport": row.transport, **row.config, "enabled": row.enabled}
        for row in session.scalars(select(MCPServerRecord)).all()
    ]


@router.put("/mcp/{name}")
def set_mcp(name: str, server: MCPServerConfig, session: SessionDep) -> dict[str, Any]:
    del name, server, session
    raise HTTPException(
        403,
        "MCP mutations are only available through the authenticated Settings workflow",
    )


@router.get("/audit")
def audit_history(
    session: SessionDep, limit: int = Query(default=100, ge=1, le=1000)
) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(AuditEventRecord).order_by(AuditEventRecord.created_at.desc()).limit(limit)
    ).all()
    return [
        {
            "id": row.id,
            "event_type": row.event_type,
            "actor": row.actor,
            "subject_type": row.subject_type,
            "subject_id": row.subject_id,
            "payload": row.payload,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.post("/browser/fill")
async def fill_application_form(
    payload: FormFillRequest, session: SessionDep, paths: PathsDep
) -> dict[str, Any]:
    browser = BrowserAssistant(paths)
    try:
        return await browser.fill(session, payload.model_dump(mode="python"))
    except FormFillRequestError as exc:
        raise HTTPException(422, str(exc)) from exc
    except FormFillResourceNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        await browser.close()


def _job_json(row: JobRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "company": row.company,
        "title": row.title,
        "canonical_url": row.canonical_url,
        "fingerprint": row.fingerprint,
        "source_type": row.source_type,
        "spec": row.normalized_spec,
        "score": row.score,
        "tier": row.tier,
        "score_explanation": row.score_explanation,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


async def _read_csv_upload(file: UploadFile) -> str:
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, "Only CSV files are accepted")
    try:
        payload = await file.read(2 * 1024 * 1024 + 1)
    finally:
        await file.close()
    if len(payload) > 2 * 1024 * 1024:
        raise HTTPException(413, "CSV file exceeds 2 MB")
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "CSV must use UTF-8 encoding") from exc


def _artifact_json(row: ArtifactRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "version": row.version,
        "sha256": row.sha256,
        "approved": row.approved,
    }


def _application_json(row: ApplicationRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "job_id": row.job_id,
        "status": row.status,
        "next_action": row.next_action,
        "next_action_at": row.next_action_at,
        "submitted_at": row.submitted_at,
        "artifacts": [
            _artifact_json(item)
            for item in row.artifacts
            if item.kind != FORM_PREVIEW_ARTIFACT_KIND
        ],
        "status_events": [
            {
                "from": event.from_status,
                "to": event.to_status,
                "note": event.note,
                "created_at": event.created_at,
            }
            for event in row.events
        ],
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _approval_json(row: ApprovalRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "action_type": row.action_type,
        "payload_digest": row.payload_digest,
        "preview": row.preview,
        "expires_at": row.expires_at,
        "decision": row.decision,
        "decided_at": row.decided_at,
        "created_at": row.created_at,
    }


def _model_route_json(row: ModelRouteRecord) -> dict[str, Any]:
    return {
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


def _revision_json(row: RevisionRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "version": row.version,
        "content": row.content,
        "diff": row.diff,
        "author": row.author,
        "source_session": row.source_session,
        "status": row.status,
        "evaluation": row.evaluation,
        "created_at": row.created_at,
    }


def _schedule_json(row: ScheduleRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "cron": row.cron,
        "route": row.route,
        "task": row.task,
        "enabled": row.enabled,
        "requires_approval": row.requires_approval,
        "next_run_at": row.next_run_at,
    }
