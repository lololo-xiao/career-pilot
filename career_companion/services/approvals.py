from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from career_companion.database import ApprovalRecord
from career_companion.services.audit import record_audit

EXTERNAL_ACTIONS = {
    "application.form_fill",
    "email.draft",
    "email.send",
    "calendar.create",
    "notification.send",
    "mcp.write",
}

NEVER_AUTOMATE = {"application.submit", "linkedin.apply", "employer.contact"}


def payload_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def request_approval(
    session: Session,
    action_type: str,
    payload: dict[str, Any],
    preview: dict[str, Any],
    ttl_minutes: int = 30,
) -> ApprovalRecord:
    if action_type in NEVER_AUTOMATE:
        raise ValueError(f"{action_type} is never automated in Career Companion v1")
    approval = ApprovalRecord(
        action_type=action_type,
        payload_digest=payload_digest(payload),
        preview=preview,
        expires_at=datetime.now(UTC) + timedelta(minutes=ttl_minutes),
    )
    session.add(approval)
    session.flush()
    record_audit(
        session,
        "approval.requested",
        subject_type="approval",
        subject_id=approval.id,
        payload={"action_type": action_type, "payload_digest": approval.payload_digest},
    )
    return approval


def decide_approval(session: Session, approval_id: str, decision: str) -> ApprovalRecord:
    if decision not in {"approved", "denied"}:
        raise ValueError("Decision must be approved or denied")
    approval = session.get(ApprovalRecord, approval_id)
    if not approval:
        raise LookupError("Approval not found")
    now = datetime.now(UTC)
    expires_at = approval.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if approval.decision != "pending":
        raise ValueError("Approval has already been decided")
    if expires_at <= now:
        approval.decision = "expired"
        approval.decided_at = now
        raise ValueError("Approval has expired")
    approval.decision = decision
    approval.decided_at = now
    record_audit(
        session,
        f"approval.{decision}",
        subject_type="approval",
        subject_id=approval.id,
        payload={"action_type": approval.action_type},
    )
    return approval


def consume_approval(session: Session, action_type: str, payload: dict[str, Any]) -> ApprovalRecord:
    digest = payload_digest(payload)
    approval = session.scalar(
        select(ApprovalRecord)
        .where(
            ApprovalRecord.action_type == action_type,
            ApprovalRecord.payload_digest == digest,
            ApprovalRecord.decision == "approved",
        )
        .order_by(ApprovalRecord.created_at.desc())
    )
    if not approval:
        raise PermissionError("A matching approval is required")
    now = datetime.now(UTC)
    expires_at = approval.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        approval.decision = "expired"
        raise PermissionError("The matching approval has expired")
    approval.decision = "consumed"
    approval.decided_at = now
    record_audit(
        session,
        "approval.consumed",
        subject_type="approval",
        subject_id=approval.id,
        payload={"action_type": approval.action_type},
    )
    return approval
