from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from career_companion.database import (
    ApplicationJobClaimRecord,
    ApplicationRecord,
    JobRecord,
    StatusEventRecord,
)
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

_APPLICATION_ID_ATTEMPTS = 3


def _new_application_id() -> str:
    return str(uuid.uuid4())


class ApplicationPersistenceError(RuntimeError):
    """A local application reservation could not be completed safely."""


@dataclass(frozen=True)
class ScoredApplicationDecisionResult:
    application: ApplicationRecord
    status_event_created: bool
    decision_fingerprint: str
    user_request_sha256: str
    decision_reference_sha256: str


def create_application(session: Session, job_id: str) -> ApplicationRecord:
    """Return the canonical application, creating its durable claim if needed."""

    application, _ = ensure_application(session, job_id)
    return application


def ensure_application(
    session: Session,
    job_id: str,
) -> tuple[ApplicationRecord, bool]:
    """Atomically return the one canonical application for a saved job."""

    if session.get(JobRecord, job_id) is None:
        raise LookupError("Job not found")
    created = False
    for _ in range(_APPLICATION_ID_ATTEMPTS):
        candidate_id = _new_application_id()
        collision = False
        try:
            with session.begin_nested():
                inserted = session.execute(
                    sqlite_insert(ApplicationRecord)
                    .values(id=candidate_id, job_id=job_id)
                    .on_conflict_do_nothing(index_elements=[ApplicationRecord.id])
                )
                if inserted.rowcount != 1:
                    collision = True
                else:
                    claimed = session.execute(
                        sqlite_insert(ApplicationJobClaimRecord)
                        .values(job_id=job_id, application_id=candidate_id)
                        .on_conflict_do_nothing(
                            index_elements=[ApplicationJobClaimRecord.job_id]
                        )
                    )
                    created = claimed.rowcount == 1
                    if created:
                        record_audit(
                            session,
                            "application.created",
                            subject_type="application",
                            subject_id=candidate_id,
                            payload={"job_id": job_id},
                        )
                    else:
                        session.execute(
                            delete(ApplicationRecord).where(
                                ApplicationRecord.id == candidate_id
                            )
                        )
        except SQLAlchemyError as exc:
            raise ApplicationPersistenceError(
                "The local application reservation could not be completed"
            ) from exc
        if collision:
            continue
        break
    else:
        raise ApplicationPersistenceError(
            "The local application reservation could not allocate an identifier"
        )

    claim = session.get(ApplicationJobClaimRecord, job_id)
    if claim is None:
        raise ApplicationPersistenceError(
            "The canonical application reservation is unavailable"
        )
    application = session.get(ApplicationRecord, claim.application_id)
    if application is None:
        raise ApplicationPersistenceError(
            "The canonical application reservation is invalid"
        )
    if application.job_id != job_id:
        raise ApplicationPersistenceError(
            "The canonical application reservation does not match the saved job"
        )
    return application, created


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
        prior_events = session.scalars(
            select(StatusEventRecord).where(
                StatusEventRecord.application_id == application.id,
                StatusEventRecord.to_status == target.value,
            )
        ).all()
        if prior_events and not any(event.note == note for event in prior_events):
            raise ValueError(
                "Application already reached the requested status with a different reference"
            )
        return application
    if not manual_override and target.value not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"Invalid application transition: {current} -> {target.value}")
    if (
        target.value in APPLICATION_STARTED_STATUSES
        and application.submitted_at is None
        and not confirmed_by_user
    ):
        raise PermissionError("Submission can only be recorded after explicit user confirmation")
    values: dict[str, object] = {
        "status": target.value,
        "next_action": NEXT_ACTIONS[target.value],
    }
    if target.value in APPLICATION_STARTED_STATUSES and application.submitted_at is None:
        values["submitted_at"] = datetime.now(UTC)
    updated = session.execute(
        update(ApplicationRecord)
        .where(
            ApplicationRecord.id == application.id,
            ApplicationRecord.status == current,
        )
        .values(**values)
    )
    if updated.rowcount != 1:
        session.expire_all()
        concurrent = session.get(ApplicationRecord, application_id)
        if concurrent is None:
            raise LookupError("Application not found")
        existing_event = session.scalar(
            select(StatusEventRecord).where(
                StatusEventRecord.application_id == application_id,
                StatusEventRecord.from_status == current,
                StatusEventRecord.to_status == target.value,
                StatusEventRecord.note == note,
            )
        )
        if concurrent.status == target.value and existing_event is not None:
            return concurrent
        raise ValueError(
            f"Application status changed concurrently: {current} -> {concurrent.status}"
        )
    session.add(
        StatusEventRecord(
            application_id=application.id,
            from_status=current,
            to_status=target.value,
            note=note,
        )
    )
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
    session.expire(application)
    return application
