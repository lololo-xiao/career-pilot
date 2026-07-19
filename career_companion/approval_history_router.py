from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.dependencies import get_companion_session
from career_companion.services.approval_history import (
    APPROVAL_HISTORY_DEFAULT_LIMIT,
    APPROVAL_HISTORY_MAX_CURSOR_CHARACTERS,
    APPROVAL_HISTORY_MAX_LIMIT,
    ApprovalHistoryCursorError,
    list_approval_history,
)

router = APIRouter(prefix="/approvals", tags=["career-companion"])
SessionDep = Annotated[Session, Depends(get_companion_session)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApprovalHistoryContext(_StrictModel):
    label: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=253)


class ApprovalHistoryBinding(_StrictModel):
    action_type: str = Field(min_length=1, max_length=100)
    algorithm: Literal["sha256-canonical-json-v1"]
    payload_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    maximum_uses: Literal[1]
    remaining_uses: Literal[0, 1]


class ApprovalHistoryAuthorization(_StrictModel):
    title: str = Field(min_length=1, max_length=120)
    effect: str = Field(min_length=1, max_length=500)
    not_authorized: list[str] = Field(min_length=1, max_length=8)
    context: list[ApprovalHistoryContext] = Field(max_length=4)
    binding: ApprovalHistoryBinding


class ApprovalHistoryItem(_StrictModel):
    id: str = Field(min_length=1, max_length=64)
    state: Literal["pending", "approved", "denied", "expired", "consumed"]
    usable: bool
    requested_at: datetime
    last_transition_at: datetime | None = None
    expires_at: datetime
    request_summary: str | None = Field(default=None, max_length=280)
    authorization: ApprovalHistoryAuthorization


class ApprovalHistoryPage(_StrictModel):
    schema_version: Literal["approval-history-v1"]
    as_of: datetime
    pending_count: int = Field(ge=0)
    items: list[ApprovalHistoryItem] = Field(max_length=APPROVAL_HISTORY_MAX_LIMIT)
    next_cursor: str | None = Field(
        default=None,
        max_length=APPROVAL_HISTORY_MAX_CURSOR_CHARACTERS,
    )


@router.get("/history", response_model=ApprovalHistoryPage)
def approval_history(
    response: Response,
    session: SessionDep,
    limit: Annotated[
        int,
        Query(ge=1, le=APPROVAL_HISTORY_MAX_LIMIT),
    ] = APPROVAL_HISTORY_DEFAULT_LIMIT,
    cursor: Annotated[
        str | None,
        Query(min_length=1, max_length=APPROVAL_HISTORY_MAX_CURSOR_CHARACTERS),
    ] = None,
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        return list_approval_history(session, limit=limit, cursor=cursor)
    except ApprovalHistoryCursorError as exc:
        raise HTTPException(422, "Invalid approval-history cursor") from exc
