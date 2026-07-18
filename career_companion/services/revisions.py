from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, aliased

from career_companion.database import (
    ConversationMessageRecord,
    ConversationSessionRecord,
    RevisionLineageCounterRecord,
    RevisionRecord,
)
from career_companion.services.audit import record_audit
from career_companion.services.replay import evaluate_replay_metrics

GENERIC_REVISION_KINDS = {"skill", "rubric"}
REVISION_EVALUATION_GATES = (
    "quality_passed",
    "security_passed",
    "cost_passed",
)
IMMUTABLE_TARGETS = {
    "source-code",
    "core-policy",
    "distribution-skill",
    "provider-credential",
    "tool-permission",
    "personality",
    "verified-fact",
}

MEMORY_PREFERENCE_SCHEMA_VERSION = "structured-career-preference-proposal-v1"
MEMORY_PREFERENCE_DIFF = "Propose an explicit structured career preference for review"
SUPPORTED_CAREER_PREFERENCES: dict[str, dict[str, str]] = {
    "workplace_preference": {
        "remote": "I prefer remote roles.",
        "remote-first": "I prefer remote-first roles.",
        "hybrid": "I prefer hybrid roles.",
        "hybrid-first": "I prefer hybrid-first roles.",
        "onsite": "I prefer onsite roles.",
    },
    "employment_type_preference": {
        "full-time": "I prefer full-time roles.",
        "part-time": "I prefer part-time roles.",
        "contract": "I prefer contract roles.",
        "internship": "I prefer internship roles.",
    },
    "relocation_preference": {
        "open": "I am open to relocation.",
        "not_open": "I am not open to relocation.",
    },
}
SUPPORTED_CAREER_PREFERENCE_SENTENCES = tuple(
    display
    for values in SUPPORTED_CAREER_PREFERENCES.values()
    for display in values.values()
)
_CAREER_PREFERENCE_REQUEST_TEMPLATES = (
    "Please remember that {display}",
    "Please update your memory: {display}",
    "Please remember this preference: {display}",
)
_STRUCTURED_CAREER_PREFERENCE_MESSAGES = {
    template.format(display=display): (field, value, display)
    for field, values in SUPPORTED_CAREER_PREFERENCES.items()
    for value, display in values.items()
    for template in _CAREER_PREFERENCE_REQUEST_TEMPLATES
}


class MemoryPreferenceValidationError(ValueError):
    """The current user request is not a supported structured career preference."""


class MemoryPreferenceProvenanceError(ValueError):
    """The supplied preference does not match the current persisted conversation."""


class MemoryPreferenceConflictError(ValueError):
    """An identical structured preference proposal has completed review."""


class MemoryRollbackConflictError(ValueError):
    """A memory rollback destination is not eligible for activation."""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_memory_preference_request(
    *,
    user_request: str,
    correction_phrase: str,
) -> tuple[str, str, str]:
    preference = _STRUCTURED_CAREER_PREFERENCE_MESSAGES.get(user_request)
    if preference is None:
        raise MemoryPreferenceValidationError(
            "The whole latest user message must exactly match a supported "
            "structured career-preference template"
        )
    field, value, canonical_display = preference
    if correction_phrase != canonical_display:
        raise MemoryPreferenceProvenanceError(
            "correction_phrase must exactly equal the canonical preference sentence"
        )
    return field, value, canonical_display


def _structured_preference_content(
    *,
    field: str,
    value: str,
    canonical_display: str,
    fingerprint: str,
    source_session: str,
    source_message_id: str,
    user_request_sha256: str,
) -> dict:
    return {
        "schema_version": MEMORY_PREFERENCE_SCHEMA_VERSION,
        "proposal_type": "structured_career_preference",
        "field": field,
        "value": value,
        "display": canonical_display,
        "claim_type": "user_owned_career_preference",
        "evidence_status": "unverified_user_preference",
        "review_required": True,
        "preference_fingerprint": fingerprint,
        "provenance": {
            "source_session": source_session,
            "source_message_id": source_message_id,
            "user_request_sha256": user_request_sha256,
            "canonical_display_sha256": _sha256(canonical_display),
        },
    }


def _is_exact_preference_revision(
    session: Session,
    revision: RevisionRecord,
    *,
    revision_id: str,
    name: str,
    field: str,
    value: str,
    canonical_display: str,
    fingerprint: str,
    author: str,
) -> bool:
    if (
        revision.id != revision_id
        or revision.kind != "memory"
        or revision.name != name
        or revision.author != author
        or revision.diff != MEMORY_PREFERENCE_DIFF
        or revision.status != "draft"
        or revision.evaluation != {}
        or type(revision.version) is not int
        or revision.version < 1
        or not isinstance(revision.content, dict)
    ):
        return False
    provenance = revision.content.get("provenance")
    if not isinstance(provenance, dict):
        return False
    source_session = provenance.get("source_session")
    source_message_id = provenance.get("source_message_id")
    if (
        not isinstance(source_session, str)
        or not isinstance(source_message_id, str)
        or revision.source_session != source_session
    ):
        return False
    source_message = session.get(ConversationMessageRecord, source_message_id)
    if (
        source_message is None
        or source_message.role != "user"
        or source_message.session_id != source_session
        or _STRUCTURED_CAREER_PREFERENCE_MESSAGES.get(source_message.content)
        != (field, value, canonical_display)
    ):
        return False
    expected_content = _structured_preference_content(
        field=field,
        value=value,
        canonical_display=canonical_display,
        fingerprint=fingerprint,
        source_session=source_session,
        source_message_id=source_message_id,
        user_request_sha256=_sha256(source_message.content),
    )
    if revision.content != expected_content:
        return False
    same_version_count = session.scalar(
        select(func.count())
        .select_from(RevisionRecord)
        .where(
            RevisionRecord.kind == revision.kind,
            RevisionRecord.name == revision.name,
            RevisionRecord.version == revision.version,
        )
    )
    if same_version_count != 1:
        return False
    counter = session.get(
        RevisionLineageCounterRecord,
        (revision.kind, revision.name),
    )
    return counter is None or counter.last_version >= revision.version


def propose_memory_preference(
    session: Session,
    *,
    source_session: str,
    user_request: str,
    correction_phrase: str,
    author: str = "career-agent",
) -> tuple[RevisionRecord, bool]:
    """Create or reuse a draft from an explicit enumerated career preference."""

    conversation = session.get(ConversationSessionRecord, source_session)
    if conversation is None:
        raise LookupError("Conversation session not found")
    latest_user_message = next(
        (
            message
            for message in reversed(conversation.messages)
            if message.role == "user"
        ),
        None,
    )
    if latest_user_message is None or latest_user_message.content != user_request:
        raise MemoryPreferenceProvenanceError(
            "Career preferences must match the latest persisted user request in this session"
        )
    field, value, canonical_display = _validate_memory_preference_request(
        user_request=user_request,
        correction_phrase=correction_phrase,
    )

    request_sha256 = _sha256(user_request)
    canonical_display_sha256 = _sha256(canonical_display)
    fingerprint = _sha256(
        json.dumps(
            {
                "field": field,
                "schema_version": MEMORY_PREFERENCE_SCHEMA_VERSION,
                "value": value,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    name = f"career-preference/{field}"
    revision_id = str(uuid.UUID(hex=fingerprint[:32]))
    expected_content = _structured_preference_content(
        field=field,
        value=value,
        canonical_display=canonical_display,
        fingerprint=fingerprint,
        source_session=conversation.id,
        source_message_id=latest_user_message.id,
        user_request_sha256=request_sha256,
    )
    audit_payload = {
        "schema_version": MEMORY_PREFERENCE_SCHEMA_VERSION,
        "preference_field": field,
        "preference_value": value,
        "source_session": conversation.id,
        "source_message_id": latest_user_message.id,
        "user_request_sha256": request_sha256,
        "canonical_display_sha256": canonical_display_sha256,
        "preference_fingerprint": fingerprint,
    }
    deterministic = session.get(RevisionRecord, revision_id)
    if deterministic is not None and not _is_exact_preference_revision(
        session,
        deterministic,
        revision_id=revision_id,
        name=name,
        field=field,
        value=value,
        canonical_display=canonical_display,
        fingerprint=fingerprint,
        author=author,
    ):
        raise MemoryPreferenceConflictError(
            "The deterministic career-preference proposal is not canonical"
        )

    def reuse(revision: RevisionRecord) -> tuple[RevisionRecord, bool]:
        if revision.status != "draft" or revision.evaluation:
            raise MemoryPreferenceConflictError(
                "This career-preference proposal has already completed review"
            )
        record_audit(
            session,
            "memory.career_preference_proposal_reused",
            actor=author,
            subject_type="memory",
            subject_id=revision.id,
            payload=audit_payload | {"version": revision.version},
        )
        return revision, False

    if deterministic is not None:
        return reuse(deterministic)

    try:
        revision = _insert_structured_preference_revision(
            session,
            name=name,
            content=expected_content,
            diff=MEMORY_PREFERENCE_DIFF,
            author=author,
            source_session=conversation.id,
            revision_id=revision_id,
        )
    except IntegrityError as exc:
        session.rollback()
        concurrent = session.get(RevisionRecord, revision_id)
        if concurrent is None or not _is_exact_preference_revision(
            session,
            concurrent,
            revision_id=revision_id,
            name=name,
            field=field,
            value=value,
            canonical_display=canonical_display,
            fingerprint=fingerprint,
            author=author,
        ):
            raise MemoryPreferenceConflictError(
                "The concurrent career-preference proposal could not be resolved"
            ) from exc
        return reuse(concurrent)
    record_audit(
        session,
        "memory.career_preference_proposed",
        actor=author,
        subject_type="memory",
        subject_id=revision.id,
        payload=audit_payload | {"version": revision.version},
    )
    return revision, True


def has_passed_evaluation(revision: RevisionRecord) -> bool:
    """Return whether a revision has explicit passing results for every gate."""

    evaluation = revision.evaluation if isinstance(revision.evaluation, dict) else {}
    return all(evaluation.get(gate) is True for gate in REVISION_EVALUATION_GATES)


def _allocate_revision_version(session: Session, *, kind: str, name: str) -> int:
    lineage_max = (
        select(func.coalesce(func.max(RevisionRecord.version), 0))
        .where(
            RevisionRecord.kind == kind,
            RevisionRecord.name == name,
        )
        .scalar_subquery()
    )
    insert_counter = sqlite_insert(RevisionLineageCounterRecord).values(
        kind=kind,
        name=name,
        last_version=lineage_max + 1,
    )
    allocate = insert_counter.on_conflict_do_update(
        index_elements=[
            RevisionLineageCounterRecord.kind,
            RevisionLineageCounterRecord.name,
        ],
        set_={
            "last_version": func.max(
                RevisionLineageCounterRecord.last_version,
                lineage_max,
            )
            + 1
        },
    ).returning(RevisionLineageCounterRecord.last_version)
    version = session.scalar(allocate)
    if type(version) is not int or version < 1:
        raise RuntimeError("Could not allocate a revision lineage version")
    return version


def _insert_structured_preference_revision(
    session: Session,
    *,
    name: str,
    content: dict,
    diff: str,
    author: str,
    source_session: str,
    revision_id: str,
) -> RevisionRecord:
    revision = RevisionRecord(
        id=revision_id,
        kind="memory",
        name=name,
        version=_allocate_revision_version(session, kind="memory", name=name),
        content=content,
        diff=diff,
        author=author,
        source_session=source_session,
    )
    session.add(revision)
    session.flush()
    record_audit(
        session,
        "revision.created",
        actor=author,
        subject_type="memory",
        subject_id=revision.id,
        payload={"name": name, "version": revision.version},
    )
    return revision


def create_revision(
    session: Session,
    *,
    kind: str,
    name: str,
    content: dict,
    diff: str,
    author: str,
    source_session: str,
) -> RevisionRecord:
    if kind not in GENERIC_REVISION_KINDS:
        raise PermissionError(
            "Generic revision creation supports only user-owned skills and rubrics"
        )
    normalized_name = name.casefold().strip()
    if any(
        normalized_name == target
        or normalized_name.startswith((f"{target}:", f"{target}/"))
        for target in IMMUTABLE_TARGETS
    ):
        raise PermissionError(f"{name} is immutable")
    revision_values = {
        "kind": kind,
        "name": name,
        "version": _allocate_revision_version(session, kind=kind, name=name),
        "content": content,
        "diff": diff,
        "author": author,
        "source_session": source_session,
    }
    revision = RevisionRecord(
        **revision_values,
    )
    session.add(revision)
    session.flush()
    record_audit(
        session,
        "revision.created",
        actor=author,
        subject_type=kind,
        subject_id=revision.id,
        payload={"name": name, "version": revision.version},
    )
    return revision


def evaluate_revision(session: Session, revision_id: str, metrics: dict) -> RevisionRecord:
    revision = session.get(RevisionRecord, revision_id)
    if not revision:
        raise LookupError("Revision not found")
    if revision.kind not in GENERIC_REVISION_KINDS:
        raise PermissionError(
            "Generic revision evaluation is limited to skills and rubrics; other "
            "revision kinds cannot be evaluated or activated through that path"
        )
    required = set(REVISION_EVALUATION_GATES)
    if not required.issubset(metrics):
        raise ValueError("Evaluation must include quality, security, and cost results")
    if revision.kind in {"skill", "rubric"}:
        replay = metrics.get("replay")
        replay_result = evaluate_replay_metrics(
            replay if isinstance(replay, dict) else {},
            require_quality_improvement=revision.kind == "rubric",
        )
        metrics = metrics | replay_result
    revision.evaluation = metrics
    if all(metrics[key] is True for key in required):
        session.execute(
            update(RevisionRecord)
            .where(
                RevisionRecord.kind == revision.kind,
                RevisionRecord.name == revision.name,
                RevisionRecord.status == "active",
            )
            .values(status="rolled_back")
        )
        revision.status = "active"
    else:
        revision.status = "quarantined"
    record_audit(
        session,
        f"revision.{revision.status}",
        subject_type=revision.kind,
        subject_id=revision.id,
        payload=metrics,
    )
    return revision


def _memory_rollback_destination(
    session: Session,
    revision: RevisionRecord,
) -> RevisionRecord:
    if revision.status != "active" or not has_passed_evaluation(revision):
        raise MemoryRollbackConflictError(
            "Memory rollback target must be active and canonically evaluated"
        )
    if type(revision.version) is not int or revision.version < 1:
        raise MemoryRollbackConflictError("Memory rollback lineage is corrupt")

    lineage = (
        RevisionRecord.kind == revision.kind,
        RevisionRecord.name == revision.name,
    )
    duplicate_version = session.scalar(
        select(RevisionRecord.version)
        .where(*lineage)
        .group_by(RevisionRecord.version)
        .having(func.count() != 1)
        .limit(1)
    )
    if duplicate_version is not None:
        raise MemoryRollbackConflictError(
            "Memory rollback lineage contains duplicate versions"
        )
    lineage_versions = session.scalars(
        select(RevisionRecord.version).where(*lineage)
    ).all()
    if any(type(version) is not int or version <= 0 for version in lineage_versions):
        raise MemoryRollbackConflictError(
            "Memory rollback lineage contains non-positive or corrupt versions"
        )
    active_ids = session.scalars(
        select(RevisionRecord.id).where(
            *lineage,
            RevisionRecord.status == "active",
        )
    ).all()
    if active_ids != [revision.id]:
        raise MemoryRollbackConflictError(
            "Memory rollback target must be the lineage's only active revision"
        )

    previous = session.scalar(
        select(RevisionRecord)
        .where(
            *lineage,
            RevisionRecord.version < revision.version,
        )
        .order_by(RevisionRecord.version.desc())
        .limit(1)
    )
    if previous is None:
        raise MemoryRollbackConflictError(
            "Memory rollback requires an immediate prior lineage revision"
        )
    if (
        type(previous.version) is not int
        or previous.version < 1
        or previous.status != "rolled_back"
        or not has_passed_evaluation(previous)
    ):
        raise MemoryRollbackConflictError(
            "Immediate memory rollback destination is not eligible for activation"
        )
    return previous


def _apply_memory_rollback_transition(
    session: Session,
    revision: RevisionRecord,
    previous: RevisionRecord,
) -> None:
    """CAS both validated memory rows and their audit as one transaction unit."""

    savepoint = session.begin_nested()
    try:
        target_result = _cas_memory_rollback_target(session, revision, previous)
        if target_result != 1:
            raise MemoryRollbackConflictError(
                "Memory rollback target changed during the transition"
            )
        destination_result = _cas_memory_rollback_destination(session, previous)
        if destination_result != 1:
            raise MemoryRollbackConflictError(
                "Memory rollback destination changed during the transition"
            )
        record_audit(
            session,
            "revision.rolled_back",
            subject_type=revision.kind,
            subject_id=revision.id,
            payload={
                "deactivated": {
                    "id": revision.id,
                    "version": revision.version,
                    "from_status": "active",
                    "to_status": "rolled_back",
                },
                "activated": {
                    "id": previous.id,
                    "version": previous.version,
                    "from_status": "rolled_back",
                    "to_status": "active",
                },
            },
        )
    except OperationalError as exc:
        savepoint.rollback()
        session.expire(revision)
        session.expire(previous)
        raise MemoryRollbackConflictError(
            "Memory rollback could not acquire its conditional transition"
        ) from exc
    except Exception:
        savepoint.rollback()
        session.expire(revision)
        session.expire(previous)
        raise
    else:
        savepoint.commit()
        session.expire(revision)
        session.expire(previous)


def _memory_lineage_versions_are_unique(
    revision: RevisionRecord,
):
    lineage = aliased(RevisionRecord)
    lineage_count = (
        select(func.count())
        .select_from(lineage)
        .where(
            lineage.kind == revision.kind,
            lineage.name == revision.name,
        )
        .scalar_subquery()
    )
    lineage_version_count = (
        select(func.count(func.distinct(lineage.version)))
        .where(
            lineage.kind == revision.kind,
            lineage.name == revision.name,
        )
        .scalar_subquery()
    )
    return lineage_count == lineage_version_count


def _memory_lineage_versions_are_positive(
    revision: RevisionRecord,
):
    lineage = aliased(RevisionRecord)
    invalid_version_count = (
        select(func.count())
        .select_from(lineage)
        .where(
            lineage.kind == revision.kind,
            lineage.name == revision.name,
            or_(
                lineage.version <= 0,
                func.typeof(lineage.version) != "integer",
            ),
        )
        .scalar_subquery()
    )
    return invalid_version_count == 0


def _memory_lineage_active_count(
    revision: RevisionRecord,
):
    lineage = aliased(RevisionRecord)
    return (
        select(func.count())
        .select_from(lineage)
        .where(
            lineage.kind == revision.kind,
            lineage.name == revision.name,
            lineage.status == "active",
        )
        .scalar_subquery()
    )


def _cas_memory_rollback_target(
    session: Session,
    revision: RevisionRecord,
    previous: RevisionRecord,
) -> int:
    lineage = aliased(RevisionRecord)
    immediate_predecessor_version = (
        select(func.max(lineage.version))
        .where(
            lineage.kind == revision.kind,
            lineage.name == revision.name,
            lineage.version < revision.version,
        )
        .scalar_subquery()
    )
    result = session.execute(
        update(RevisionRecord)
        .where(
            RevisionRecord.id == revision.id,
            RevisionRecord.kind == revision.kind,
            RevisionRecord.name == revision.name,
            RevisionRecord.version == revision.version,
            RevisionRecord.status == "active",
            RevisionRecord.evaluation == revision.evaluation,
            _memory_lineage_versions_are_unique(revision),
            _memory_lineage_versions_are_positive(revision),
            _memory_lineage_active_count(revision) == 1,
            immediate_predecessor_version == previous.version,
        )
        .values(status="rolled_back")
        .execution_options(synchronize_session=False)
    )
    return result.rowcount


def _cas_memory_rollback_destination(
    session: Session,
    previous: RevisionRecord,
) -> int:
    result = session.execute(
        update(RevisionRecord)
        .where(
            RevisionRecord.id == previous.id,
            RevisionRecord.kind == previous.kind,
            RevisionRecord.name == previous.name,
            RevisionRecord.version == previous.version,
            RevisionRecord.status == "rolled_back",
            RevisionRecord.evaluation == previous.evaluation,
            _memory_lineage_versions_are_unique(previous),
            _memory_lineage_versions_are_positive(previous),
            _memory_lineage_active_count(previous) == 0,
        )
        .values(status="active")
        .execution_options(synchronize_session=False)
    )
    return result.rowcount


def rollback_revision(session: Session, revision_id: str) -> RevisionRecord:
    revision = session.get(RevisionRecord, revision_id)
    if not revision:
        raise LookupError("Revision not found")
    if revision.kind == "memory":
        previous = _memory_rollback_destination(session, revision)
        _apply_memory_rollback_transition(session, revision, previous)
        return revision
    if revision.kind not in GENERIC_REVISION_KINDS:
        raise PermissionError(
            "Generic revision rollback is limited to skills and rubrics"
        )

    was_active = revision.status == "active"
    previous = session.scalar(
        select(RevisionRecord)
        .where(
            RevisionRecord.kind == revision.kind,
            RevisionRecord.name == revision.name,
            RevisionRecord.version < revision.version,
            RevisionRecord.status == "rolled_back",
        )
        .order_by(RevisionRecord.version.desc())
    )
    revision.status = "rolled_back"
    if was_active and previous is not None:
        previous.status = "active"
    record_audit(
        session,
        "revision.rolled_back",
        subject_type=revision.kind,
        subject_id=revision.id,
    )
    return revision
