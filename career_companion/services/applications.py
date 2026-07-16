from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from career_companion.database import ApplicationRecord, StatusEventRecord
from career_companion.schemas import ApplicationStatus
from career_companion.services.audit import record_audit

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "discovered": {"scored", "withdrawn"},
    "scored": {"approved", "withdrawn"},
    "approved": {"tailoring", "withdrawn"},
    "tailoring": {"ready", "withdrawn"},
    "ready": {"form_filled", "submitted", "withdrawn"},
    "form_filled": {"submitted", "withdrawn"},
    "submitted": {"followed_up", "interview", "rejected", "offer", "withdrawn"},
    "followed_up": {"interview", "rejected", "offer", "withdrawn"},
    "interview": {"interview", "offer", "rejected", "withdrawn"},
    "offer": {"withdrawn"},
    "rejected": set(),
    "withdrawn": set(),
}


def create_application(session: Session, job_id: str) -> ApplicationRecord:
    application = ApplicationRecord(job_id=job_id)
    session.add(application)
    session.flush()
    record_audit(
        session,
        "application.created",
        subject_type="application",
        subject_id=application.id,
        payload={"job_id": job_id},
    )
    return application


def transition_application(
    session: Session,
    application_id: str,
    target: ApplicationStatus,
    *,
    note: str = "",
    confirmed_by_user: bool = False,
) -> ApplicationRecord:
    application = session.get(ApplicationRecord, application_id)
    if not application:
        raise LookupError("Application not found")
    current = application.status
    if target.value not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"Invalid application transition: {current} -> {target.value}")
    if target is ApplicationStatus.SUBMITTED and not confirmed_by_user:
        raise PermissionError("Submission can only be recorded after explicit user confirmation")
    event = StatusEventRecord(
        application_id=application.id,
        from_status=current,
        to_status=target.value,
        note=note,
    )
    session.add(event)
    application.status = target.value
    if target is ApplicationStatus.SUBMITTED:
        application.submitted_at = datetime.now(UTC)
        application.next_action = "Schedule a follow-up"
    record_audit(
        session,
        "application.status_changed",
        subject_type="application",
        subject_id=application.id,
        payload={"from": current, "to": target.value, "note": note},
    )
    return application
