from __future__ import annotations

import ipaddress
import re
import secrets
from collections.abc import Generator
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hermes_runtime import profile_distribution_directory
from career_companion.config import load_config
from career_companion.database import (
    ApplicationRecord,
    CandidateProfileRecord,
    ConversationSessionRecord,
    JobRecord,
    MCPServerRecord,
    ModelRouteRecord,
    ScheduleRecord,
)
from career_companion.paths import CompanionPaths
from career_companion.persistence import account_session
from career_companion.router import RevisionRequest, _application_json, _job_json, _revision_json
from career_companion.schemas import ApplicationStatus, Job
from career_companion.services.applications import transition_application
from career_companion.services.browser import BrowserAssistant
from career_companion.services.conversation_sessions import (
    agent_profile_json,
    ensure_agent_profile,
    update_agent_profile,
)
from career_companion.services.jobs import add_job, score_job
from career_companion.services.revisions import create_revision

router = APIRouter(
    prefix="/api/internal/hermes/v1",
    tags=["hermes-bridge"],
    include_in_schema=False,
)

_ACCOUNT_KEY_PATTERN = re.compile(r"[a-f0-9]{64}")


class ApplicationTransition(BaseModel):
    status: ApplicationStatus
    note: str = ""


class FormFillPayload(BaseModel):
    application_id: str
    url: str
    fields: dict[str, str] = Field(default_factory=dict)
    files: dict[str, str] = Field(default_factory=dict)
    headless: bool = False


class IdentityUpdatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    soul: str = Field(default="", max_length=32_768)
    source_session: str = Field(min_length=36, max_length=36)
    user_request: str = Field(min_length=1, max_length=50_000)


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
) -> dict[str, Any]:
    conversation = session.get(ConversationSessionRecord, payload.source_session)
    if conversation is None:
        raise HTTPException(404, "Conversation session not found")
    latest_user_message = next(
        (
            message
            for message in reversed(conversation.messages)
            if message.role == "user"
        ),
        None,
    )
    if (
        latest_user_message is None
        or latest_user_message.content != payload.user_request.strip()
    ):
        raise HTTPException(
            409,
            "Identity changes must match the latest direct user request in this session",
        )
    try:
        profile = update_agent_profile(
            session,
            paths,
            profile_distribution_directory(),
            name=payload.name,
            soul=payload.soul,
            actor="career-agent",
            source_session=conversation.id,
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
def propose_revision(payload: RevisionRequest, session: SessionDep) -> dict[str, Any]:
    data = payload.model_dump()
    data["author"] = "career-agent"
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
