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
    "submitted": {
        "followed_up", "oa", "interview", "interview_1", "rejected",
        "no_response", "offer", "withdrawn",
    },
    "followed_up": {
        "oa", "interview", "interview_1", "rejected", "no_response",
        "offer", "withdrawn",
    },
    "oa": {"oa_failed", "interview", "interview_1", "rejected", "withdrawn"},
    "oa_failed": set(),
    "interview": {
        "interview", "interview_1_failed", "interview_2", "final_interview",
        "offer", "rejected", "withdrawn",
    },
    "interview_1": {
        "interview_1_failed", "interview_2", "final_interview", "offer",
        "rejected", "withdrawn",
    },
    "interview_1_failed": set(),
    "interview_2": {
        "interview_2_failed", "final_interview", "offer", "rejected", "withdrawn",
    },
    "interview_2_failed": set(),
    "final_interview": {"final_interview_failed", "offer", "rejected", "withdrawn"},
    "final_interview_failed": set(),
    "offer": {"accepted", "withdrawn", "rejected"},
    "accepted": set(),
    "rejected": set(),
    "no_response": {"followed_up", "oa", "interview", "interview_1", "rejected"},
    "withdrawn": set(),
}

APPLICATION_STARTED_STATUSES = {
    "submitted",
    "followed_up",
    "oa",
    "oa_failed",
    "interview",
    "interview_1",
    "interview_1_failed",
    "interview_2",
    "interview_2_failed",
    "final_interview",
    "final_interview_failed",
    "offer",
    "accepted",
    "rejected",
    "no_response",
}

NEXT_ACTIONS = {
    "discovered": "Review fit",
    "scored": "Approve or archive this opportunity",
    "approved": "Prepare application materials",
    "tailoring": "Review the tailored application",
    "ready": "Submit when ready",
    "form_filled": "Review and submit manually",
    "submitted": "Schedule a follow-up",
    "followed_up": "Wait for a response",
    "oa": "Complete the online assessment",
    "oa_failed": "Record learnings from the assessment",
    "interview": "Prepare for the interview",
    "interview_1": "Prepare for interview 1",
    "interview_1_failed": "Record learnings from interview 1",
    "interview_2": "Prepare for interview 2",
    "interview_2_failed": "Record learnings from interview 2",
    "final_interview": "Prepare for the final interview",
    "final_interview_failed": "Record learnings from the final interview",
    "offer": "Review the offer",
    "accepted": "Prepare for your start date",
    "rejected": "Close the loop and record learnings",
    "no_response": "Decide whether to follow up",
    "withdrawn": "No action needed",
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
    manual_override: bool = False,
) -> ApplicationRecord:
    application = session.get(ApplicationRecord, application_id)
    if not application:
        raise LookupError("Application not found")
    current = application.status
    if target.value == current:
        return application
    if not manual_override and target.value not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"Invalid application transition: {current} -> {target.value}")
    if (
        target.value in APPLICATION_STARTED_STATUSES
        and application.submitted_at is None
        and not confirmed_by_user
    ):
        raise PermissionError("Submission can only be recorded after explicit user confirmation")
    event = StatusEventRecord(
        application_id=application.id,
        from_status=current,
        to_status=target.value,
        note=note,
    )
    session.add(event)
    application.status = target.value
    if target.value in APPLICATION_STARTED_STATUSES and application.submitted_at is None:
        application.submitted_at = datetime.now(UTC)
    application.next_action = NEXT_ACTIONS[target.value]
    record_audit(
        session,
        "application.status_changed",
        subject_type="application",
        subject_id=application.id,
        payload={
            "from": current,
            "to": target.value,
            "note": note,
            "manual_override": manual_override,
        },
    )
    return application
