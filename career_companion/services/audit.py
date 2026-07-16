from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from career_companion.database import AuditEventRecord


def record_audit(
    session: Session,
    event_type: str,
    *,
    actor: str = "local-user",
    subject_type: str = "",
    subject_id: str = "",
    payload: dict[str, Any] | None = None,
) -> AuditEventRecord:
    event = AuditEventRecord(
        event_type=event_type,
        actor=actor,
        subject_type=subject_type,
        subject_id=subject_id,
        payload=payload or {},
    )
    session.add(event)
    session.flush()
    return event
