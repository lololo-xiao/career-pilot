from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
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


@dataclass(frozen=True)
class ScoredApplicationDecisionResult:
    application: ApplicationRecord
    status_event_created: bool
    decision_fingerprint: str
    user_request_sha256: str
    decision_reference_sha256: str


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


def ensure_application(
    session: Session,
    job_id: str,
) -> tuple[ApplicationRecord, bool]:
    """Idempotently return one existing application for a job or create it."""

    existing = session.scalar(
        select(ApplicationRecord)
        .where(ApplicationRecord.job_id == job_id)
        .order_by(ApplicationRecord.created_at.asc(), ApplicationRecord.id.asc())
    )
    if existing is not None:
        return existing, False
    return create_application(session, job_id), True


def decide_scored_application(
    session: Session,
    application_id: str,
    target: ApplicationStatus,
    *,
    source_session: str,
    run_message_id: str,
    user_request: str,
    decision_reference: str,
) -> ScoredApplicationDecisionResult:
    """Atomically commit one exact decision and its event in SQLite.

    The conditional status update is the compare-and-set guard. SQLite serializes
    concurrent writers, so losers observe the committed event and become idempotent
    replays instead of creating a second event.
    """

    if target not in {ApplicationStatus.APPROVED, ApplicationStatus.WITHDRAWN}:
        raise ValueError("A scored decision must approve or archive the application")
    request_hash = hashlib.sha256(user_request.encode("utf-8")).hexdigest()
    reference_hash = hashlib.sha256(decision_reference.encode("utf-8")).hexdigest()
    fingerprint_payload = {
        "application_id": application_id,
        "decision": target.value,
        "decision_reference_sha256": reference_hash,
        "run_message_id": run_message_id,
        "source_session": source_session,
        "user_request_sha256": request_hash,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    note = (
        f"Explicit user decision; action={target.value}; "
        f"source_session={source_session}; run_message={run_message_id}; "
        f"user_request_sha256={request_hash}; "
        f"decision_reference_sha256={reference_hash}; "
        f"fingerprint={fingerprint}"
    )
    # End validation-only reads so the compare-and-set starts a fresh transaction.
    session.rollback()
    try:
        updated = session.execute(
            update(ApplicationRecord)
            .where(
                ApplicationRecord.id == application_id,
                ApplicationRecord.status == ApplicationStatus.SCORED.value,
            )
            .values(
                status=target.value,
                next_action=NEXT_ACTIONS[target.value],
            )
        )
        if updated.rowcount == 1:
            session.add(
                StatusEventRecord(
                    application_id=application_id,
                    from_status=ApplicationStatus.SCORED.value,
                    to_status=target.value,
                    note=note,
                )
            )
            record_audit(
                session,
                "application.status_changed",
                subject_type="application",
                subject_id=application_id,
                payload={
                    "from": ApplicationStatus.SCORED.value,
                    "to": target.value,
                    "note": note,
                    "manual_override": False,
                },
            )
            session.commit()
            application = session.get(ApplicationRecord, application_id)
            assert application is not None
            return ScoredApplicationDecisionResult(
                application=application,
                status_event_created=True,
                decision_fingerprint=fingerprint,
                user_request_sha256=request_hash,
                decision_reference_sha256=reference_hash,
            )
        application = session.get(ApplicationRecord, application_id)
        if application is None:
            raise LookupError("Application not found")
        existing_event = session.scalar(
            select(StatusEventRecord).where(
                StatusEventRecord.application_id == application.id,
                StatusEventRecord.from_status == ApplicationStatus.SCORED.value,
                StatusEventRecord.to_status == target.value,
                StatusEventRecord.note == note,
            )
        )
        if application.status == target.value and existing_event is not None:
            session.commit()
            return ScoredApplicationDecisionResult(
                application=application,
                status_event_created=False,
                decision_fingerprint=fingerprint,
                user_request_sha256=request_hash,
                decision_reference_sha256=reference_hash,
            )
        raise ValueError(
            "Explicit approval or archive is only valid for a scored application"
        )
    except Exception:
        session.rollback()
        raise


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
