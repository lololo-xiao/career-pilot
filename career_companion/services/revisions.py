from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from career_companion.database import RevisionRecord
from career_companion.services.audit import record_audit
from career_companion.services.replay import evaluate_replay_metrics

ALLOWED_REVISION_KINDS = {"memory", "skill", "rubric"}
IMMUTABLE_TARGETS = {
    "source-code",
    "core-policy",
    "distribution-skill",
    "provider-credential",
    "tool-permission",
    "personality",
    "verified-fact",
}


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
    if kind not in ALLOWED_REVISION_KINDS:
        raise PermissionError("Only memory, user-owned skills, and rubrics may evolve")
    normalized_name = name.casefold().strip()
    if any(
        normalized_name == target
        or normalized_name.startswith((f"{target}:", f"{target}/"))
        for target in IMMUTABLE_TARGETS
    ):
        raise PermissionError(f"{name} is immutable")
    last_version = session.scalar(
        select(func.max(RevisionRecord.version)).where(
            RevisionRecord.kind == kind, RevisionRecord.name == name
        )
    )
    revision = RevisionRecord(
        kind=kind,
        name=name,
        version=int(last_version or 0) + 1,
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
        subject_type=kind,
        subject_id=revision.id,
        payload={"name": name, "version": revision.version},
    )
    return revision


def evaluate_revision(session: Session, revision_id: str, metrics: dict) -> RevisionRecord:
    revision = session.get(RevisionRecord, revision_id)
    if not revision:
        raise LookupError("Revision not found")
    required = {"quality_passed", "security_passed", "cost_passed"}
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
    if all(bool(metrics[key]) for key in required):
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


def rollback_revision(session: Session, revision_id: str) -> RevisionRecord:
    revision = session.get(RevisionRecord, revision_id)
    if not revision:
        raise LookupError("Revision not found")
    was_active = revision.status == "active"
    revision.status = "rolled_back"
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
    if was_active and previous is not None:
        previous.status = "active"
    record_audit(
        session,
        "revision.rolled_back",
        subject_type=revision.kind,
        subject_id=revision.id,
    )
    return revision
