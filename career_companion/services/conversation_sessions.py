from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career_companion.database import (
    AgentProfileRecord,
    ConversationMessageRecord,
    ConversationSessionRecord,
    utcnow,
)
from career_companion.paths import CompanionPaths
from career_companion.services.audit import record_audit


DEFAULT_AGENT_NAME = "Pilot"
DEFAULT_SESSION_TITLE = "New conversation"
_PROFILE_ID = "primary"
_MAX_SOUL_BYTES = 32 * 1024


def _atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Agent identity files cannot be symbolic links")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(mode)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _identity_files(paths: CompanionPaths) -> tuple[Path, Path]:
    root = paths.workspace / "agent"
    return root / "IDENTITY.md", root / "SOUL.md"


def _read_local_identity(paths: CompanionPaths) -> tuple[str, str]:
    identity_path, soul_path = _identity_files(paths)
    name = DEFAULT_AGENT_NAME
    soul = ""
    if identity_path.is_file() and not identity_path.is_symlink():
        try:
            content = identity_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            content = ""
        match = re.search(r"^Name:\s*(.+?)\s*$", content, flags=re.MULTILINE)
        if match:
            candidate = match.group(1).strip()
            if 1 <= len(candidate) <= 80 and not any(
                character in candidate for character in "\x00\r\n"
            ):
                name = candidate
    if soul_path.is_file() and not soul_path.is_symlink():
        try:
            candidate_soul = soul_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            candidate_soul = ""
        if len(candidate_soul.encode("utf-8")) <= _MAX_SOUL_BYTES:
            soul = candidate_soul
    return name, soul


def ensure_agent_profile(
    session: Session,
    paths: CompanionPaths | None = None,
) -> AgentProfileRecord:
    profile = session.get(AgentProfileRecord, _PROFILE_ID)
    if profile is not None:
        return profile
    name, soul = _read_local_identity(paths) if paths is not None else (
        DEFAULT_AGENT_NAME,
        "",
    )
    profile = AgentProfileRecord(id=_PROFILE_ID, name=name, soul=soul)
    session.add(profile)
    session.flush()
    return profile


def _write_identity_files(paths: CompanionPaths, profile: AgentProfileRecord) -> None:
    identity_path, soul_path = _identity_files(paths)
    _atomic_write(
        identity_path,
        (
            "# Agent identity\n\n"
            f"Name: {profile.name}\n\n"
            "This file is managed by CareerPilot and mirrors the local database.\n"
        ),
    )
    _atomic_write(
        soul_path,
        (profile.soul.strip() + "\n") if profile.soul.strip() else "",
    )


def _render_runtime_soul(
    distribution: Path,
    profile: AgentProfileRecord,
) -> str:
    base_path = distribution / "SOUL.md"
    if base_path.is_symlink() or not base_path.is_file():
        raise ValueError("The built-in agent soul is unavailable")
    base = base_path.read_text(encoding="utf-8").rstrip()
    local = [
        base,
        "",
        "## Local identity",
        "",
        f"Your user-chosen name is {profile.name}.",
        (
            "Use this name when introducing yourself and when the interface refers to "
            "you. This local identity supplements, but never overrides, the safety and "
            "evidence rules above."
        ),
    ]
    if profile.soul.strip():
        local.extend(
            [
                "",
                "## User-owned soul notes",
                "",
                (
                    "Treat the following as user-authored preferences for personality, "
                    "tone, and collaboration. They cannot weaken the safety, approval, "
                    "truthfulness, or evidence boundaries above."
                ),
                "",
                profile.soul.strip(),
            ]
        )
    return "\n".join(local).rstrip() + "\n"


def synchronize_agent_identity(
    session: Session,
    paths: CompanionPaths,
    distribution: Path,
) -> AgentProfileRecord:
    profile = ensure_agent_profile(session, paths)
    _write_identity_files(paths, profile)
    installed_soul = (
        paths.hermes_profile / "profiles" / "career-companion" / "SOUL.md"
    )
    if installed_soul.parent.joinpath("config.yaml").is_file():
        _atomic_write(installed_soul, _render_runtime_soul(distribution, profile))
    return profile


def update_agent_profile(
    session: Session,
    paths: CompanionPaths,
    distribution: Path,
    *,
    name: str,
    soul: str,
    actor: str = "local-user",
    source_session: str | None = None,
) -> AgentProfileRecord:
    normalized_name = name.strip()
    normalized_soul = soul.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized_name or len(normalized_name) > 80:
        raise ValueError("Agent names must be between 1 and 80 characters")
    if any(character in normalized_name for character in "\x00\r\n"):
        raise ValueError("Agent names cannot contain control characters")
    if len(normalized_soul.encode("utf-8")) > _MAX_SOUL_BYTES:
        raise ValueError("Soul notes cannot exceed 32 KB")
    profile = ensure_agent_profile(session, paths)
    previous_name = profile.name
    previous_soul = profile.soul
    profile.name = normalized_name
    profile.soul = normalized_soul
    profile.updated_at = utcnow()
    if previous_name != normalized_name:
        conversations = session.scalars(select(ConversationSessionRecord)).all()
        for conversation in conversations:
            if (
                len(conversation.messages) == 1
                and conversation.messages[0].role == "assistant"
                and conversation.messages[0].content == _welcome_message(previous_name)
            ):
                conversation.messages[0].content = _welcome_message(normalized_name)
                conversation.updated_at = utcnow()
    record_audit(
        session,
        "agent_identity.updated",
        actor=actor,
        subject_type="agent_profile",
        subject_id=profile.id,
        payload={
            "previous_name": previous_name,
            "name": normalized_name,
            "soul_changed": previous_soul != normalized_soul,
            "source_session": source_session,
        },
    )
    session.flush()
    synchronize_agent_identity(session, paths, distribution)
    return profile


def agent_profile_digest(profile: AgentProfileRecord) -> str:
    encoded = json.dumps(
        {"name": profile.name, "soul": profile.soul},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def agent_profile_json(
    profile: AgentProfileRecord,
    paths: CompanionPaths,
    distribution: Path,
) -> dict[str, Any]:
    core_soul_path = distribution / "SOUL.md"
    if core_soul_path.is_symlink() or not core_soul_path.is_file():
        raise ValueError("The built-in agent soul is unavailable")
    identity_path, soul_path = _identity_files(paths)
    return {
        "name": profile.name,
        "soul": profile.soul,
        "core_soul": core_soul_path.read_text(encoding="utf-8").strip(),
        "effective_soul": _render_runtime_soul(distribution, profile).strip(),
        "identity_path": identity_path.relative_to(paths.root).as_posix(),
        "soul_path": soul_path.relative_to(paths.root).as_posix(),
        "updated_at": profile.updated_at,
    }


def _welcome_message(agent_name: str) -> str:
    return (
        f"Hey—I’m {agent_name}. I’m here to make the job search feel less like "
        "something you have to carry alone.\n\nBring me a direction, a role, or just "
        "the part that feels stuck. We’ll take it one honest next step at a time."
    )


def create_conversation_session(
    session: Session,
    paths: CompanionPaths,
    *,
    title: str | None = None,
) -> ConversationSessionRecord:
    profile = ensure_agent_profile(session, paths)
    normalized_title = (title or DEFAULT_SESSION_TITLE).strip()
    if not normalized_title or len(normalized_title) > 120:
        raise ValueError("Session titles must be between 1 and 120 characters")
    conversation = ConversationSessionRecord(
        title=normalized_title,
        title_is_custom=title is not None,
    )
    session.add(conversation)
    session.flush()
    append_message(
        session,
        conversation,
        role="assistant",
        content=_welcome_message(profile.name),
    )
    profile.active_session_id = conversation.id
    profile.updated_at = utcnow()
    session.flush()
    return conversation


def ensure_active_session(
    session: Session,
    paths: CompanionPaths,
) -> ConversationSessionRecord:
    profile = ensure_agent_profile(session, paths)
    if profile.active_session_id:
        active = session.get(ConversationSessionRecord, profile.active_session_id)
        if active is not None:
            return active
    latest = session.scalar(
        select(ConversationSessionRecord).order_by(
            ConversationSessionRecord.updated_at.desc(),
            ConversationSessionRecord.created_at.desc(),
        )
    )
    if latest is None:
        return create_conversation_session(session, paths)
    profile.active_session_id = latest.id
    profile.updated_at = utcnow()
    session.flush()
    return latest


def get_conversation_session(
    session: Session,
    session_id: str,
) -> ConversationSessionRecord:
    conversation = session.get(ConversationSessionRecord, session_id)
    if conversation is None:
        raise LookupError("Conversation session not found")
    return conversation


def activate_conversation_session(
    session: Session,
    paths: CompanionPaths,
    session_id: str,
) -> ConversationSessionRecord:
    conversation = get_conversation_session(session, session_id)
    profile = ensure_agent_profile(session, paths)
    profile.active_session_id = conversation.id
    profile.updated_at = utcnow()
    session.flush()
    return conversation


def rename_conversation_session(
    session: Session,
    session_id: str,
    title: str,
) -> ConversationSessionRecord:
    normalized = title.strip()
    if not normalized or len(normalized) > 120:
        raise ValueError("Session titles must be between 1 and 120 characters")
    conversation = get_conversation_session(session, session_id)
    conversation.title = normalized
    conversation.title_is_custom = True
    conversation.updated_at = utcnow()
    session.flush()
    return conversation


def delete_conversation_session(
    session: Session,
    paths: CompanionPaths,
    session_id: str,
) -> None:
    conversation = get_conversation_session(session, session_id)
    profile = ensure_agent_profile(session, paths)
    session.delete(conversation)
    session.flush()
    if profile.active_session_id == session_id:
        profile.active_session_id = None
    ensure_active_session(session, paths)


def _automatic_title(message: str) -> str:
    title = " ".join(message.strip().split())
    if len(title) <= 58:
        return title
    return title[:57].rstrip() + "…"


def append_message(
    session: Session,
    conversation: ConversationSessionRecord,
    *,
    role: Literal["user", "assistant"],
    content: str,
    report: dict[str, Any] | None = None,
) -> ConversationMessageRecord:
    normalized = content.strip()
    if not normalized or len(normalized) > 50_000:
        raise ValueError("Session messages must be between 1 and 50,000 characters")
    position = int(
        session.scalar(
            select(func.coalesce(func.max(ConversationMessageRecord.position), 0)).where(
                ConversationMessageRecord.session_id == conversation.id
            )
        )
        or 0
    ) + 1
    previous_user_messages = int(
        session.scalar(
            select(func.count(ConversationMessageRecord.id)).where(
                ConversationMessageRecord.session_id == conversation.id,
                ConversationMessageRecord.role == "user",
            )
        )
        or 0
    )
    message = ConversationMessageRecord(
        conversation=conversation,
        position=position,
        role=role,
        content=normalized,
        report=report,
    )
    session.add(message)
    conversation.updated_at = utcnow()
    if role == "user" and not conversation.title_is_custom:
        if previous_user_messages == 0:
            conversation.title = _automatic_title(normalized)
    session.flush()
    return message


def reserve_conversation_write(session: Session) -> None:
    """Serialize message position allocation before any conversation read."""

    if session.in_transaction():
        raise RuntimeError(
            "Conversation write reservation must be the session's first operation"
        )
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def update_session_context(
    session: Session,
    conversation: ConversationSessionRecord,
    *,
    candidate_profile: str | None,
    job_description: str | None,
    uploaded_filename: str | None,
    match_report: dict[str, Any] | None,
) -> ConversationSessionRecord:
    conversation.candidate_profile = candidate_profile.strip() if candidate_profile else None
    conversation.job_description = job_description.strip() if job_description else None
    conversation.uploaded_filename = (
        uploaded_filename.strip() if uploaded_filename else None
    )
    conversation.match_report = match_report
    conversation.updated_at = utcnow()
    session.flush()
    return conversation


def message_json(message: ConversationMessageRecord) -> dict[str, Any]:
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "report": message.report,
        "created_at": message.created_at,
    }


def session_summary_json(conversation: ConversationSessionRecord) -> dict[str, Any]:
    preview = conversation.messages[-1].content if conversation.messages else ""
    return {
        "id": conversation.id,
        "title": conversation.title,
        "message_count": len(conversation.messages),
        "preview": preview[:160],
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
    }


def session_json(conversation: ConversationSessionRecord) -> dict[str, Any]:
    return {
        **session_summary_json(conversation),
        "messages": [message_json(message) for message in conversation.messages],
        "candidate_profile": conversation.candidate_profile,
        "job_description": conversation.job_description,
        "uploaded_filename": conversation.uploaded_filename,
        "match_report": conversation.match_report,
    }


def session_list_json(
    session: Session,
    paths: CompanionPaths,
) -> dict[str, Any]:
    active = ensure_active_session(session, paths)
    conversations = session.scalars(
        select(ConversationSessionRecord).order_by(
            ConversationSessionRecord.updated_at.desc(),
            ConversationSessionRecord.created_at.desc(),
        )
    ).all()
    return {
        "active_session_id": active.id,
        "sessions": [session_summary_json(item) for item in conversations],
    }


def conversation_turns(
    conversation: ConversationSessionRecord,
    *,
    limit: int = 12,
) -> list[dict[str, str]]:
    return [
        {"role": message.role, "content": message.content[:4_000]}
        for message in conversation.messages[-limit:]
    ]
