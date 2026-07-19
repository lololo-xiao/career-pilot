from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from career_companion.database import ApprovalRecord
from career_companion.services.approvals import (
    APPROVAL_ACTIONS,
    AUTHORIZATION_SNAPSHOT_KEY,
    has_current_consumer_validation,
    sanitize_approval_summary,
)

APPROVAL_HISTORY_SCHEMA_VERSION = "approval-history-v1"
APPROVAL_HISTORY_DEFAULT_LIMIT = 20
APPROVAL_HISTORY_MAX_LIMIT = 50
APPROVAL_HISTORY_MAX_CURSOR_CHARACTERS = 512
_CURSOR_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ACTION_TYPE = re.compile(r"[a-z][a-z0-9_.-]{0,99}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_STATES = {"pending", "approved", "denied", "expired", "consumed"}


class ApprovalHistoryCursorError(ValueError):
    pass


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _database_datetime(value: datetime) -> datetime:
    """SQLite stores these timezone-declared values without an offset."""

    return _as_utc(value).replace(tzinfo=None)


def _encode_cursor(created_at: datetime, approval_id: str) -> str:
    payload = json.dumps(
        {
            "i": approval_id,
            "t": _as_utc(created_at).isoformat().replace("+00:00", "Z"),
            "v": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    if not cursor or len(cursor) > APPROVAL_HISTORY_MAX_CURSOR_CHARACTERS:
        raise ApprovalHistoryCursorError("Invalid approval-history cursor")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
        raise ApprovalHistoryCursorError("Invalid approval-history cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ApprovalHistoryCursorError("Invalid approval-history cursor") from exc
    if not isinstance(payload, dict) or set(payload) != {"i", "t", "v"}:
        raise ApprovalHistoryCursorError("Invalid approval-history cursor")
    approval_id = payload.get("i")
    timestamp = payload.get("t")
    if (
        payload.get("v") != 1
        or not isinstance(approval_id, str)
        or _CURSOR_ID.fullmatch(approval_id) is None
        or not isinstance(timestamp, str)
        or len(timestamp) > 40
    ):
        raise ApprovalHistoryCursorError("Invalid approval-history cursor")
    try:
        created_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApprovalHistoryCursorError("Invalid approval-history cursor") from exc
    if created_at.tzinfo is None:
        raise ApprovalHistoryCursorError("Invalid approval-history cursor")
    return _as_utc(created_at), approval_id


def _effective_state(row: ApprovalRecord, now: datetime) -> str:
    decision = row.decision if row.decision in _STATES else "denied"
    if decision in {"pending", "approved"} and _as_utc(row.expires_at) <= now:
        return "expired"
    return decision


def _snapshot_context(row: ApprovalRecord) -> list[dict[str, str]]:
    if not isinstance(row.preview, dict):
        return []
    snapshot = row.preview.get(AUTHORIZATION_SNAPSHOT_KEY)
    if not isinstance(snapshot, dict) or snapshot.get("action_type") != row.action_type:
        return []
    context: list[dict[str, str]] = []
    values = [
        ("Target", snapshot.get("target_hostname"), 253),
        ("Application", snapshot.get("application_id"), 64),
        ("Server", snapshot.get("server_name"), 80),
    ]
    transport = snapshot.get("mcp_transport")
    if transport == "stdio":
        values.append(("Transport", "Local command (stdio)", 80))
    elif transport == "http":
        values.append(("Transport", "Remote endpoint (HTTP)", 80))
    for label, value, maximum in values:
        if isinstance(value, str) and 1 <= len(value) <= maximum:
            sanitized = sanitize_approval_summary(value)
            if sanitized:
                context.append({"label": label, "value": sanitized[:maximum]})
    for label, key in (("Fields", "field_count"), ("Attachments", "attachment_count")):
        value = snapshot.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100:
            context.append({"label": label, "value": str(value)})
    return context


def _history_item(row: ApprovalRecord, now: datetime) -> dict[str, Any]:
    definition = APPROVAL_ACTIONS.get(row.action_type)
    action_type = (
        row.action_type
        if isinstance(row.action_type, str)
        and _ACTION_TYPE.fullmatch(row.action_type) is not None
        else "legacy.unknown"
    )
    binding_valid = isinstance(row.payload_digest, str) and _SHA256.fullmatch(
        row.payload_digest
    ) is not None
    state = _effective_state(row, now)
    usable = bool(
        definition
        and definition.consumer_enabled
        and has_current_consumer_validation(row.preview, row.action_type)
        and binding_valid
        and state == "approved"
    )
    if definition is None:
        title = "Unsupported legacy approval"
        effect = "No current CareerPilot action can use this legacy record."
        not_authorized = (
            "Any external or local action",
            "Reusing this record as a permission",
        )
    else:
        title = definition.title
        effect = definition.effect
        not_authorized = definition.not_authorized
    summary = None
    if row.action_type != "mcp.probe" and isinstance(row.preview, dict):
        summary = sanitize_approval_summary(row.preview.get("summary"))
    return {
        "id": row.id,
        "state": state,
        "usable": usable,
        "requested_at": _as_utc(row.created_at),
        "last_transition_at": (
            _as_utc(row.decided_at) if row.decided_at is not None else None
        ),
        "expires_at": _as_utc(row.expires_at),
        "request_summary": summary,
        "authorization": {
            "title": title,
            "effect": effect,
            "not_authorized": list(not_authorized),
            "context": _snapshot_context(row),
            "binding": {
                "action_type": action_type,
                "algorithm": "sha256-canonical-json-v1",
                "payload_sha256": row.payload_digest if binding_valid else None,
                "maximum_uses": 1,
                "remaining_uses": 1 if usable else 0,
            },
        },
    }


def list_approval_history(
    session: Session,
    *,
    limit: int = APPROVAL_HISTORY_DEFAULT_LIMIT,
    cursor: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not 1 <= limit <= APPROVAL_HISTORY_MAX_LIMIT:
        raise ValueError(
            f"History limit must be between 1 and {APPROVAL_HISTORY_MAX_LIMIT}"
        )
    as_of = _as_utc(now or datetime.now(UTC))
    statement = select(ApprovalRecord)
    if cursor is not None:
        cursor_time, cursor_id = _decode_cursor(cursor)
        database_time = _database_datetime(cursor_time)
        statement = statement.where(
            or_(
                ApprovalRecord.created_at < database_time,
                and_(
                    ApprovalRecord.created_at == database_time,
                    ApprovalRecord.id < cursor_id,
                ),
            )
        )
    rows = session.scalars(
        statement.order_by(
            ApprovalRecord.created_at.desc(),
            ApprovalRecord.id.desc(),
        ).limit(limit + 1)
    ).all()
    page_rows = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)
    pending_count = session.scalar(
        select(func.count(ApprovalRecord.id)).where(
            ApprovalRecord.decision == "pending",
            ApprovalRecord.expires_at > _database_datetime(as_of),
        )
    )
    return {
        "schema_version": APPROVAL_HISTORY_SCHEMA_VERSION,
        "as_of": as_of,
        "pending_count": int(pending_count or 0),
        "items": [_history_item(row, as_of) for row in page_rows],
        "next_cursor": next_cursor,
    }
