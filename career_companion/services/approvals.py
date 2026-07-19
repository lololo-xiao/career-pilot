from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from career_companion.database import ApprovalRecord
from career_companion.paths import CompanionPaths
from career_companion.services.audit import record_audit
from career_companion.services.form_fill import (
    canonical_form_fill_payload,
    validate_form_payload,
)

MAX_APPROVAL_TTL_MINUTES = 1440
MAX_APPROVAL_PAYLOAD_BYTES = 1_100_000
MAX_APPROVAL_PREVIEW_BYTES = 16_384
MAX_APPROVAL_JSON_DEPTH = 8
MAX_APPROVAL_JSON_NODES = 2_048
MAX_APPROVAL_SUMMARY_CHARACTERS = 280
AUTHORIZATION_SNAPSHOT_KEY = "_careerpilot_authorization_v1"
CONSUMER_VALIDATION_MARKER_KEY = "consumer_validation"
CONSUMER_VALIDATION_VERSIONS: Mapping[str, int] = MappingProxyType(
    {"application.form_fill": 1}
)


@dataclass(frozen=True)
class ApprovalActionDefinition:
    title: str
    effect: str
    not_authorized: tuple[str, ...]
    consumer_enabled: bool = False


_COMMON_SINGLE_USE_LIMITS = (
    "A changed destination, field, value, file, recipient, or mode",
    "A second use or use after expiry",
)

APPROVAL_ACTIONS: Mapping[str, ApprovalActionDefinition] = MappingProxyType(
    {
        "application.form_fill": ApprovalActionDefinition(
            title="Fill one application form",
            effect=(
                "Open the exact application URL and fill the exact non-submit fields "
                "and approved attachments in the matching request."
            ),
            not_authorized=(
                "Submitting the form",
                "Applying on LinkedIn",
                "Contacting an employer",
                *_COMMON_SINGLE_USE_LIMITS,
            ),
            consumer_enabled=True,
        ),
        "email.draft": ApprovalActionDefinition(
            title="Reserved email-draft approval",
            effect=(
                "Reserve a record for one exact email-draft request; CareerPilot has no "
                "enabled consumer for this action."
            ),
            not_authorized=(
                "Creating an email draft in the current product",
                "Sending the draft",
                "Contacting an employer",
                *_COMMON_SINGLE_USE_LIMITS,
            ),
        ),
        "email.send": ApprovalActionDefinition(
            title="Reserved email-send approval",
            effect=(
                "Reserve a record for one exact email-send request; CareerPilot has no "
                "enabled consumer for this action."
            ),
            not_authorized=(
                "Sending anything in the current product",
                "Contacting an employer",
                *_COMMON_SINGLE_USE_LIMITS,
            ),
        ),
        "calendar.create": ApprovalActionDefinition(
            title="Reserved calendar approval",
            effect=(
                "Reserve a record for one exact calendar-create request; CareerPilot has "
                "no enabled consumer for this action."
            ),
            not_authorized=(
                "Creating or changing calendar data in the current product",
                *_COMMON_SINGLE_USE_LIMITS,
            ),
        ),
        "notification.send": ApprovalActionDefinition(
            title="Reserved notification approval",
            effect=(
                "Reserve a record for one exact notification request; CareerPilot has no "
                "enabled consumer for this action."
            ),
            not_authorized=(
                "Sending a notification in the current product",
                *_COMMON_SINGLE_USE_LIMITS,
            ),
        ),
        "mcp.write": ApprovalActionDefinition(
            title="Reserved MCP write approval",
            effect=(
                "Reserve a record for one exact MCP write request; CareerPilot has no "
                "enabled consumer for this action."
            ),
            not_authorized=(
                "Calling an MCP write tool in the current product",
                *_COMMON_SINGLE_USE_LIMITS,
            ),
        ),
    }
)

EXTERNAL_ACTIONS = frozenset(APPROVAL_ACTIONS)
NEVER_AUTOMATE = frozenset(
    {"application.submit", "linkedin.apply", "employer.contact"}
)


def sanitize_approval_summary(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) not in {"Cc", "Cf"}
        or character in {"\n", "\t"}
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return None
    return normalized[:MAX_APPROVAL_SUMMARY_CHARACTERS]


def current_consumer_validation_marker(action_type: str) -> dict[str, Any] | None:
    version = CONSUMER_VALIDATION_VERSIONS.get(action_type)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return None
    return {"consumer": action_type, "version": version}


def has_current_consumer_validation(
    preview: Any,
    action_type: str,
) -> bool:
    expected = current_consumer_validation_marker(action_type)
    if expected is None or not isinstance(preview, dict):
        return False
    snapshot = preview.get(AUTHORIZATION_SNAPSHOT_KEY)
    if not isinstance(snapshot, dict) or snapshot.get("action_type") != action_type:
        return False
    marker = snapshot.get(CONSUMER_VALIDATION_MARKER_KEY)
    return bool(
        isinstance(marker, dict)
        and set(marker) == {"consumer", "version"}
        and marker.get("consumer") == expected["consumer"]
        and isinstance(marker.get("version"), int)
        and not isinstance(marker.get("version"), bool)
        and marker.get("version") == expected["version"]
    )


def _validate_json_shape(value: Any, *, label: str) -> None:
    nodes = 0

    def visit(candidate: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_APPROVAL_JSON_NODES:
            raise ValueError(
                f"{label} exceeds the {MAX_APPROVAL_JSON_NODES}-node limit"
            )
        if depth > MAX_APPROVAL_JSON_DEPTH:
            raise ValueError(
                f"{label} exceeds the {MAX_APPROVAL_JSON_DEPTH}-level depth limit"
            )
        if candidate is None or isinstance(candidate, (str, bool, int)):
            return
        if isinstance(candidate, float):
            if not math.isfinite(candidate):
                raise ValueError(f"{label} must not contain NaN or infinity")
            return
        if isinstance(candidate, dict):
            for key, item in candidate.items():
                if not isinstance(key, str):
                    raise ValueError(f"{label} object keys must be strings")
                visit(item, depth + 1)
            return
        if isinstance(candidate, list):
            for item in candidate:
                visit(item, depth + 1)
            return
        raise ValueError(f"{label} must contain only JSON values")

    visit(value, 1)


def _canonical_json(value: Any, *, label: str, maximum_bytes: int) -> str:
    _validate_json_shape(value, label=label)
    try:
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain only JSON values") from exc
    if len(canonical.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} exceeds the {maximum_bytes}-byte limit")
    return canonical


def payload_digest(payload: dict[str, Any]) -> str:
    canonical = _canonical_json(
        payload,
        label="Approval payload",
        maximum_bytes=MAX_APPROVAL_PAYLOAD_BYTES,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _authorization_snapshot(
    action_type: str,
    payload: dict[str, Any],
    *,
    consumer_validated: bool,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {"action_type": action_type}
    if action_type == "application.form_fill":
        raw_url = payload.get("url")
        if isinstance(raw_url, str):
            parsed = urlsplit(raw_url.strip())
            hostname = (parsed.hostname or "").rstrip(".").casefold()
            if hostname and parsed.scheme.casefold() in {"http", "https"}:
                snapshot["target_hostname"] = hostname[:253]
        application_id = payload.get("application_id")
        if isinstance(application_id, str) and 1 <= len(application_id) <= 64:
            snapshot["application_id"] = application_id
        fields = payload.get("fields")
        files = payload.get("files")
        snapshot["field_count"] = len(fields) if isinstance(fields, dict) else 0
        snapshot["attachment_count"] = len(files) if isinstance(files, dict) else 0
    marker = current_consumer_validation_marker(action_type)
    if consumer_validated and marker is not None:
        snapshot[CONSUMER_VALIDATION_MARKER_KEY] = marker
    return snapshot


def _stored_preview(
    action_type: str,
    payload: dict[str, Any],
    preview: dict[str, Any],
    *,
    consumer_validated: bool,
) -> dict[str, Any]:
    _canonical_json(
        preview,
        label="Approval preview",
        maximum_bytes=MAX_APPROVAL_PREVIEW_BYTES,
    )
    stored = json.loads(json.dumps(preview, ensure_ascii=True, allow_nan=False))
    summary = sanitize_approval_summary(stored.get("summary"))
    if summary is None:
        stored.pop("summary", None)
    else:
        stored["summary"] = summary
    stored[AUTHORIZATION_SNAPSHOT_KEY] = _authorization_snapshot(
        action_type,
        payload,
        consumer_validated=consumer_validated,
    )
    _canonical_json(
        stored,
        label="Approval preview",
        maximum_bytes=MAX_APPROVAL_PREVIEW_BYTES,
    )
    return stored


def _require_known_action(action_type: str) -> ApprovalActionDefinition:
    if action_type in NEVER_AUTOMATE:
        raise ValueError(f"{action_type} is never automated in CareerPilot")
    definition = APPROVAL_ACTIONS.get(action_type)
    if definition is None:
        raise ValueError("Unsupported approval action type")
    return definition


def request_approval(
    session: Session,
    action_type: str,
    payload: dict[str, Any],
    preview: dict[str, Any],
    ttl_minutes: int = 30,
    *,
    paths: CompanionPaths | None = None,
) -> ApprovalRecord:
    _require_known_action(action_type)
    if not 1 <= ttl_minutes <= MAX_APPROVAL_TTL_MINUTES:
        raise ValueError(
            f"Approval TTL must be between 1 and {MAX_APPROVAL_TTL_MINUTES} minutes"
        )
    consumer_validated = False
    if action_type == "application.form_fill":
        if paths is None:
            raise ValueError("Form-fill approval validation requires account paths")
        payload = canonical_form_fill_payload(payload)
        validate_form_payload(session, payload, paths)
        consumer_validated = True
    digest = payload_digest(payload)
    stored_preview = _stored_preview(
        action_type,
        payload,
        preview,
        consumer_validated=consumer_validated,
    )
    approval = ApprovalRecord(
        action_type=action_type,
        payload_digest=digest,
        preview=stored_preview,
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

    result = session.execute(
        update(ApprovalRecord)
        .where(
            ApprovalRecord.id == approval_id,
            ApprovalRecord.decision == "pending",
            ApprovalRecord.expires_at > now,
        )
        .values(decision=decision, decided_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        session.expire(approval)
        session.refresh(approval)
        raise ValueError("Approval has already been decided")
    session.expire(approval)
    session.refresh(approval)
    record_audit(
        session,
        f"approval.{decision}",
        subject_type="approval",
        subject_id=approval.id,
        payload={"action_type": approval.action_type},
    )
    return approval


def consume_approval(session: Session, action_type: str, payload: dict[str, Any]) -> ApprovalRecord:
    definition = _require_known_action(action_type)
    if not definition.consumer_enabled:
        raise PermissionError("This approval action has no enabled consumer")
    if action_type == "application.form_fill":
        payload = canonical_form_fill_payload(payload)
    marker = current_consumer_validation_marker(action_type)
    if marker is None:
        raise PermissionError("This approval consumer has no current validation marker")
    digest = payload_digest(payload)
    now = datetime.now(UTC)
    marker_consumer = ApprovalRecord.preview[AUTHORIZATION_SNAPSHOT_KEY][
        CONSUMER_VALIDATION_MARKER_KEY
    ]["consumer"].as_string()
    marker_version = ApprovalRecord.preview[AUTHORIZATION_SNAPSHOT_KEY][
        CONSUMER_VALIDATION_MARKER_KEY
    ]["version"].as_integer()
    current_marker = (
        ApprovalRecord.preview[AUTHORIZATION_SNAPSHOT_KEY][
            "action_type"
        ].as_string()
        == action_type,
        marker_consumer == marker["consumer"],
        func.json_type(
            ApprovalRecord.preview,
            f'$."{AUTHORIZATION_SNAPSHOT_KEY}".'
            f'"{CONSUMER_VALIDATION_MARKER_KEY}"."version"',
        )
        == "integer",
        marker_version == marker["version"],
    )
    candidate_id = (
        select(ApprovalRecord.id)
        .where(
            ApprovalRecord.action_type == action_type,
            ApprovalRecord.payload_digest == digest,
            ApprovalRecord.decision == "approved",
            ApprovalRecord.expires_at > now,
            *current_marker,
        )
        .order_by(ApprovalRecord.created_at.desc(), ApprovalRecord.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    consumed_id = session.execute(
        update(ApprovalRecord)
        .where(
            ApprovalRecord.id == candidate_id,
            ApprovalRecord.decision == "approved",
            ApprovalRecord.expires_at > now,
        )
        .values(decision="consumed", decided_at=now, updated_at=now)
        .returning(ApprovalRecord.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    if consumed_id is None:
        expired = session.scalar(
            select(ApprovalRecord.id)
            .where(
                ApprovalRecord.action_type == action_type,
                ApprovalRecord.payload_digest == digest,
                ApprovalRecord.decision == "approved",
                ApprovalRecord.expires_at <= now,
                *current_marker,
            )
            .limit(1)
        )
        if expired is not None:
            raise PermissionError("The matching approval has expired")
        raise PermissionError("A matching approval is required")

    approval = session.get(ApprovalRecord, consumed_id)
    assert approval is not None
    session.expire(approval)
    session.refresh(approval)
    record_audit(
        session,
        "approval.consumed",
        subject_type="approval",
        subject_id=approval.id,
        payload={"action_type": approval.action_type},
    )
    return approval
