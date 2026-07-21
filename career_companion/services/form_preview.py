from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import stat
import threading
import unicodedata
import uuid
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - the storage gate fails closed off POSIX
    fcntl = None  # type: ignore[assignment]

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from career_companion.database import (
    ApplicationRecord,
    ArtifactRecord,
    CandidateProfileRecord,
    SourceDocumentRecord,
    StatusEventRecord,
)
from career_companion.paths import CompanionPaths
from career_companion.schemas import (
    ApplicationStatus,
    CandidateProfile,
    ClaimStatus,
    FormFieldKey,
    FormFieldSpec,
    FormFieldState,
    FormPreviewRequest,
    ProfileClaim,
)
from career_companion.services.audit import record_audit


FORM_PREVIEW_ARTIFACT_KIND = "form_fill_preview"
FORM_PREVIEW_SCHEMA = "local-form-fill-preview-v1"
_PROFILE_FIELD_ALIASES: dict[FormFieldKey, frozenset[str]] = {
    FormFieldKey.FULL_NAME: frozenset({"full_name", "legal_name", "name"}),
    FormFieldKey.EMAIL: frozenset({"email"}),
    FormFieldKey.PHONE: frozenset({"phone", "telephone"}),
    FormFieldKey.LOCATION: frozenset({"address", "location"}),
    FormFieldKey.WORK_AUTHORIZATION: frozenset(
        {"visa_status", "work_authorization", "work_authorisation"}
    ),
}
_ARTIFACT_FIELD_KINDS = {
    FormFieldKey.RESUME: "cv",
    FormFieldKey.COVER_LETTER: "cover_letter",
}
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\Z")
_SECRET = re.compile(
    r"(?:\b(?:api[_ -]?key|bearer|password|secret|token)\b\s*[:=]|"
    r"\bsk-[A-Za-z0-9_-]{20,}\b)",
    re.IGNORECASE,
)
_URL_LIKE = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
_MAX_MAPPED_VALUE = 1_000
_MAX_PREVIEW_BYTES = 16 * 1024 * 1024
_MAX_SOURCE_DOCUMENT_BYTES = 20 * 1024 * 1024
_MAX_APPROVED_ARTIFACT_BYTES = 20 * 1024 * 1024
_PREVIEW_THREAD_LOCK = threading.Lock()
_PREVIEW_SAFETY = {
    "browser_used": False,
    "navigation_performed": False,
    "controls_clicked": False,
    "files_uploaded": False,
    "form_filled": False,
    "submitted": False,
    "external_mutation_performed": False,
    "later_external_phase_implemented": False,
    "later_explicit_confirmation_required": True,
}


def prepare_form_preview(
    session: Session,
    application_id: str,
    paths: CompanionPaths,
    request: FormPreviewRequest,
    *,
    source_session: str | None = None,
    source_message_id: str | None = None,
    source_message_sha256: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Persist one deterministic local preview without touching an external form."""

    pilot_provenance = (
        bool(source_session),
        bool(source_message_id),
        bool(source_message_sha256),
    )
    if len(set(pilot_provenance)) != 1:
        raise ValueError("Pilot preview provenance must be complete or omitted")
    if _request_contains_sensitive_or_external_text(request):
        raise ValueError(
            "Form descriptions must not contain credentials, secrets, or external URLs"
        )
    application = session.get(ApplicationRecord, application_id)
    if application is None:
        raise LookupError("Application not found")
    if application.status not in {
        ApplicationStatus.READY.value,
        ApplicationStatus.FORM_PREVIEWED.value,
    }:
        raise ValueError("Application must be review-ready before creating a form preview")
    if not _has_explicit_approval_event(session, application.id):
        raise PermissionError(
            "Form previews require an application explicitly approved from the scored state"
        )
    _assert_secure_preview_storage(paths)

    profile_record = session.scalar(
        select(CandidateProfileRecord).order_by(
            CandidateProfileRecord.updated_at.desc(),
            CandidateProfileRecord.id.desc(),
        )
    )
    if profile_record is None:
        raise ValueError("A reviewed candidate profile is required")
    profile = CandidateProfile.model_validate(
        {"id": profile_record.id, **profile_record.payload}
    )
    documents = _source_documents(session, profile, paths)
    claims = _verified_claims(profile, documents)
    artifacts = _validated_artifacts(session, application.id, paths)
    if not artifacts.get("cv"):
        raise ValueError("An exact approved CV artifact is required for a form preview")
    if not claims:
        raise ValueError("At least one verified evidence-backed profile claim is required")

    request_payload = request.model_dump(mode="json")
    request_digest = _digest(
        {"application_id": application.id, "request": request_payload}
    )
    evidence_payload = {
        "profile_id": profile_record.id,
        "profile": profile_record.payload,
        "documents": [
            {"id": row.id, "sha256": row.sha256}
            for row in sorted(documents.values(), key=lambda item: item.id)
        ],
        "artifacts": [
            {
                "id": item.id,
                "kind": item.kind,
                "version": item.version,
                "sha256": item.sha256,
            }
            for kind in sorted(artifacts)
            for item in artifacts[kind]
        ],
    }
    evidence_digest = _digest(evidence_payload)
    fields = [
        _map_field(field, profile_record.id, documents, claims, artifacts)
        for field in request.fields
    ]
    unresolved_required = sum(
        field["required"] and field["state"] != FormFieldState.MAPPED.value
        for field in fields
    )
    core = {
        "schema_version": FORM_PREVIEW_SCHEMA,
        "application_id": application.id,
        "form_reference": request.form_reference,
        "request_digest": request_digest,
        "evidence_digest": evidence_digest,
        "mode": "preview_only",
        "all_required_mapped": unresolved_required == 0,
        "unresolved_required_count": unresolved_required,
        "fields": fields,
        "source": (
            {
                "type": "pilot_message",
                "session_id": source_session,
                "message_id": source_message_id,
                "message_sha256": source_message_sha256,
            }
            if source_session
            else {"type": "local_workspace"}
        ),
        "safety": dict(_PREVIEW_SAFETY),
    }
    plan_digest = _digest(core)
    preview_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"careerpilot:{plan_digest}"))
    preview = {"id": preview_id, **core}
    encoded = (json.dumps(preview, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > _MAX_PREVIEW_BYTES:
        raise ValueError("The deterministic form preview exceeds the 16 MiB local limit")
    file_sha256 = hashlib.sha256(encoded).hexdigest()
    preview_relative_path = Path(_preview_filename(application.id, preview_id))
    preview_path = paths.artifacts / preview_relative_path
    _write_deterministic_preview(
        paths.artifacts,
        preview_relative_path,
        encoded,
        file_sha256,
    )

    inserted = session.execute(
        sqlite_insert(ArtifactRecord)
        .values(
            id=preview_id,
            application_id=application.id,
            kind=FORM_PREVIEW_ARTIFACT_KIND,
            version=1,
            path=str(preview_path),
            sha256=file_sha256,
            approved=False,
        )
        .on_conflict_do_nothing(index_elements=[ArtifactRecord.id])
    )
    created = inserted.rowcount == 1
    artifact = session.get(ArtifactRecord, preview_id)
    if (
        artifact is None
        or artifact.application_id != application.id
        or artifact.kind != FORM_PREVIEW_ARTIFACT_KIND
        or artifact.sha256 != file_sha256
    ):
        raise RuntimeError("The deterministic form preview binding is invalid")
    if artifact.path != str(preview_path):
        # A deterministic row created by the former nested layout has the same
        # content binding. Point it at the newly verified flat root artifact before
        # advancing or reconciling workflow state.
        artifact.path = str(preview_path)
        session.flush()
    if artifact.path != str(preview_path):
        raise RuntimeError("The deterministic form preview path binding is invalid")

    next_action = (
        f"Resolve {unresolved_required} required form field"
        f"{'s' if unresolved_required != 1 else ''} before external review"
        if unresolved_required
        else "Review the local preview; any later external phase needs confirmation"
    )
    status_event_created = _record_preview_status(
        session,
        application,
        preview_id=preview_id,
        request_digest=request_digest,
        evidence_digest=evidence_digest,
        next_action=next_action,
    )
    if created:
        state_counts = {
            state.value: sum(field["state"] == state.value for field in fields)
            for state in FormFieldState
        }
        record_audit(
            session,
            "application.form_preview_created",
            subject_type="application",
            subject_id=application.id,
            payload={
                "preview_id": preview_id,
                "request_digest": request_digest,
                "evidence_digest": evidence_digest,
                "field_count": len(fields),
                "field_states": state_counts,
                "unresolved_required_count": unresolved_required,
                "status_event_created": status_event_created,
                "source_message_sha256": source_message_sha256,
                "external_mutation_performed": False,
            },
        )
    return preview, created


def list_latest_form_previews(
    session: Session,
    paths: CompanionPaths,
) -> dict[str, dict[str, Any]]:
    """Return the latest valid local preview for every application in this account."""

    rows = session.scalars(
        select(ArtifactRecord)
        .where(ArtifactRecord.kind == FORM_PREVIEW_ARTIFACT_KIND)
        .order_by(ArtifactRecord.created_at.desc(), ArtifactRecord.id.desc())
    ).all()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.application_id in latest:
            continue
        preview = _read_preview_artifact(row, paths)
        if preview is not None:
            latest[row.application_id] = preview
    return latest


def latest_form_preview(
    session: Session,
    application_id: str,
    paths: CompanionPaths,
) -> dict[str, Any]:
    if session.get(ApplicationRecord, application_id) is None:
        raise LookupError("Application not found")
    preview = list_latest_form_previews(session, paths).get(application_id)
    if preview is None:
        raise LookupError("Form preview not found")
    return preview


def _has_explicit_approval_event(session: Session, application_id: str) -> bool:
    return session.scalar(
        select(StatusEventRecord.id)
        .where(
            StatusEventRecord.application_id == application_id,
            StatusEventRecord.from_status == ApplicationStatus.SCORED.value,
            StatusEventRecord.to_status == ApplicationStatus.APPROVED.value,
        )
        .limit(1)
    ) is not None


def _source_documents(
    session: Session,
    profile: CandidateProfile,
    paths: CompanionPaths,
) -> dict[str, SourceDocumentRecord]:
    if not profile.source_documents:
        return {}
    rows = session.scalars(
        select(SourceDocumentRecord).where(
            SourceDocumentRecord.id.in_(set(profile.source_documents))
        )
    ).all()
    documents: dict[str, SourceDocumentRecord] = {}
    for row in rows:
        try:
            relative = _relative_stored_path(row.stored_path, paths.imports)
            if (
                relative is None
                or _sha256_anchored_file(
                    paths.imports,
                    relative,
                    max_bytes=_MAX_SOURCE_DOCUMENT_BYTES,
                )
                != row.sha256
            ):
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        documents[row.id] = row
    return documents


def _verified_claims(
    profile: CandidateProfile,
    documents: dict[str, SourceDocumentRecord],
) -> list[ProfileClaim]:
    return [
        claim
        for claim in [*profile.claims, *profile.work_authorization]
        if claim.status == ClaimStatus.VERIFIED
        and any(
            _evidence_is_valid(claim, evidence, documents)
            for evidence in claim.evidence
        )
    ]


def _evidence_is_valid(
    claim: ProfileClaim,
    evidence: Any,
    documents: dict[str, SourceDocumentRecord],
) -> bool:
    document = documents.get(evidence.source_id)
    if document is None or document.sha256 != evidence.content_hash:
        return False
    document_text = _normalize(document.extracted_text)
    excerpt = _normalize(evidence.excerpt)
    value = _normalize(claim.value)
    return bool(
        excerpt
        and value
        and excerpt in document_text
        and value in excerpt
    )


def _validated_artifacts(
    session: Session,
    application_id: str,
    paths: CompanionPaths,
) -> dict[str, list[ArtifactRecord]]:
    rows = session.scalars(
        select(ArtifactRecord).where(
            ArtifactRecord.application_id == application_id,
            ArtifactRecord.approved.is_(True),
            ArtifactRecord.kind.in_(set(_ARTIFACT_FIELD_KINDS.values())),
        )
    ).all()
    result: dict[str, list[ArtifactRecord]] = {}
    for row in rows:
        try:
            relative = _relative_stored_path(row.path, paths.artifacts)
            if (
                relative is None
                or _sha256_anchored_file(
                    paths.artifacts,
                    relative,
                    max_bytes=_MAX_APPROVED_ARTIFACT_BYTES,
                )
                != row.sha256
            ):
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        result.setdefault(row.kind, []).append(row)
    for kind in result:
        result[kind].sort(key=lambda item: (item.version, item.id), reverse=True)
    return result


def _map_field(
    field: FormFieldSpec,
    profile_id: str,
    documents: dict[str, SourceDocumentRecord],
    claims: list[ProfileClaim],
    artifacts: dict[str, list[ArtifactRecord]],
) -> dict[str, Any]:
    base = {
        "field_id": field.field_id,
        "label": field.label,
        "field_key": field.field_key.value,
        "required": field.required,
        "options": field.options,
    }
    if field.field_key == FormFieldKey.UNSUPPORTED:
        return base | {
            "state": FormFieldState.UNSUPPORTED.value,
            "value": None,
            "source": None,
            "reason": "This field is outside the finite evidence-backed preview vocabulary.",
        }
    if field.field_key in _ARTIFACT_FIELD_KINDS:
        return base | _map_artifact_field(field.field_key, artifacts)

    candidates: list[dict[str, Any]] = []
    aliases = _PROFILE_FIELD_ALIASES[field.field_key]
    for claim in claims:
        if claim.key.strip().casefold() not in aliases or not _valid_value(
            field.field_key, claim.value
        ):
            continue
        candidates.append(
            {
                "value": claim.value.strip(),
                "source": {
                    "type": "verified_profile_claim",
                    "profile_id": profile_id,
                    "claim_key": claim.key,
                    "evidence": [
                        {
                            "source_id": evidence.source_id,
                            "page": evidence.page,
                            "content_hash": evidence.content_hash,
                        }
                        for evidence in claim.evidence
                        if _evidence_is_valid(claim, evidence, documents)
                    ],
                },
            }
        )
    distinct: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        distinct.setdefault(
            _normalize_value(field.field_key, candidate["value"]),
            candidate,
        )
    if not distinct:
        return base | {
            "state": FormFieldState.UNKNOWN.value,
            "value": None,
            "source": None,
            "reason": "No exact evidence-verified value is available for this field.",
        }
    if len(distinct) != 1:
        return base | {
            "state": FormFieldState.AMBIGUOUS.value,
            "value": None,
            "source": None,
            "reason": "Multiple different evidence-verified values could map to this field.",
        }
    candidate = next(iter(distinct.values()))
    if field.options:
        matching = [
            option
            for option in field.options
            if _normalize(option) == _normalize(candidate["value"])
        ]
        if len(matching) != 1:
            return base | {
                "state": FormFieldState.AMBIGUOUS.value,
                "value": None,
                "source": candidate["source"],
                "reason": "The verified value does not exactly match one supplied choice.",
            }
        candidate = candidate | {"value": matching[0]}
    return base | {
        "state": FormFieldState.MAPPED.value,
        "value": candidate["value"],
        "source": candidate["source"],
        "reason": "Mapped from exact local evidence without inference.",
    }


def _map_artifact_field(
    field_key: FormFieldKey,
    artifacts: dict[str, list[ArtifactRecord]],
) -> dict[str, Any]:
    kind = _ARTIFACT_FIELD_KINDS[field_key]
    candidates = artifacts.get(kind, [])
    if not candidates:
        return {
            "state": FormFieldState.UNKNOWN.value,
            "value": None,
            "source": None,
            "reason": f"No exact approved {kind.replace('_', ' ')} artifact is available.",
        }
    latest_version = candidates[0].version
    latest = [item for item in candidates if item.version == latest_version]
    if len({item.sha256 for item in latest}) != 1:
        return {
            "state": FormFieldState.AMBIGUOUS.value,
            "value": None,
            "source": None,
            "reason": (
                f"Multiple different approved {kind.replace('_', ' ')} artifacts "
                "share the latest version."
            ),
        }
    artifact = sorted(latest, key=lambda item: item.id)[0]
    return {
        "state": FormFieldState.MAPPED.value,
        "value": f"{kind.replace('_', ' ')} v{artifact.version}",
        "source": {
            "type": "approved_artifact",
            "artifact_id": artifact.id,
            "kind": artifact.kind,
            "version": artifact.version,
            "sha256": artifact.sha256,
        },
        "reason": "Mapped to the exact approved local artifact; nothing was uploaded.",
    }


def _valid_value(field_key: FormFieldKey, value: str) -> bool:
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > _MAX_MAPPED_VALUE
        or _SECRET.search(cleaned)
        or _URL_LIKE.search(cleaned)
    ):
        return False
    if field_key == FormFieldKey.EMAIL:
        return _EMAIL.fullmatch(cleaned) is not None
    if field_key == FormFieldKey.PHONE:
        return 7 <= len(re.sub(r"\D", "", cleaned)) <= 15
    return True


def _request_contains_sensitive_or_external_text(
    request: FormPreviewRequest,
) -> bool:
    values = [request.form_reference]
    for field in request.fields:
        values.extend([field.field_id, field.label, *field.options])
    return any(_SECRET.search(value) or _URL_LIKE.search(value) for value in values)


def _normalize_value(field_key: FormFieldKey, value: str) -> str:
    if field_key == FormFieldKey.PHONE:
        return re.sub(r"\D", "", value)
    return _normalize(value)


def _record_preview_status(
    session: Session,
    application: ApplicationRecord,
    *,
    preview_id: str,
    request_digest: str,
    evidence_digest: str,
    next_action: str,
) -> bool:
    note = (
        f"Local preview only; preview_id={preview_id}; "
        f"request_digest={request_digest}; evidence_digest={evidence_digest}; "
        "external_mutation_performed=false"
    )
    if application.status == ApplicationStatus.READY.value:
        updated = session.execute(
            update(ApplicationRecord)
            .where(
                ApplicationRecord.id == application.id,
                ApplicationRecord.status == ApplicationStatus.READY.value,
            )
            .values(
                status=ApplicationStatus.FORM_PREVIEWED.value,
                next_action=next_action,
            )
        )
        if updated.rowcount == 1:
            session.add(
                StatusEventRecord(
                    application_id=application.id,
                    from_status=ApplicationStatus.READY.value,
                    to_status=ApplicationStatus.FORM_PREVIEWED.value,
                    note=note,
                )
            )
            record_audit(
                session,
                "application.status_changed",
                subject_type="application",
                subject_id=application.id,
                payload={
                    "from": ApplicationStatus.READY.value,
                    "to": ApplicationStatus.FORM_PREVIEWED.value,
                    "note": note,
                    "manual_override": False,
                    "dedicated_workflow": "form_preview",
                },
            )
            session.expire(application)
            return True
        session.expire_all()
        concurrent = session.get(ApplicationRecord, application.id)
        if concurrent is None:
            raise LookupError("Application not found")
        if concurrent.status != ApplicationStatus.FORM_PREVIEWED.value:
            raise ValueError(
                "Application status changed while the form preview was being created"
            )
        application = concurrent
    updated = session.execute(
        update(ApplicationRecord)
        .where(
            ApplicationRecord.id == application.id,
            ApplicationRecord.status == ApplicationStatus.FORM_PREVIEWED.value,
        )
        .values(next_action=next_action)
    )
    if updated.rowcount != 1:
        raise ValueError(
            "Application status changed while the form preview was being reconciled"
        )
    session.expire(application)
    return False


def _write_deterministic_preview(
    root: Path,
    relative_path: Path,
    content: bytes,
    expected_sha256: str,
) -> None:
    with _PREVIEW_THREAD_LOCK:
        _write_deterministic_preview_locked(
            root,
            relative_path,
            content,
            expected_sha256,
        )


def _write_deterministic_preview_locked(
    root: Path,
    relative_path: Path,
    content: bytes,
    expected_sha256: str,
) -> None:
    """Write beneath a trusted root without following any path component.

    POSIX directory descriptors bind every operation to the directory that was
    actually inspected. Platforms without the required primitives fail closed.
    """

    parts = _relative_parts(relative_path)
    if len(parts) != 1:
        raise ValueError(
            "Deterministic form previews must be stored directly under the artifacts root"
        )
    parent_parts, filename = parts[:-1], parts[-1]
    try:
        root_fd = _open_trusted_directory(root)
    except OSError as exc:
        raise RuntimeError(
            "The deterministic form preview root is not a trusted local path"
        ) from exc
    parent_fd = -1
    temporary_name = f".form-preview-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    replaced_identity: os.stat_result | None = None
    try:
        _lock_preview_storage_root(root_fd)
        parent_fd = _open_relative_directory(
            root_fd,
            parent_parts,
            create=True,
        )
        try:
            existing_digest = _sha256_anchored_file(
                root,
                relative_path,
                max_bytes=_MAX_PREVIEW_BYTES,
            )
        except FileNotFoundError:
            existing_digest = None
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                "The deterministic form preview path is not a trusted local path"
            ) from exc
        if existing_digest is not None:
            if existing_digest != expected_sha256:
                raise RuntimeError(
                    "The deterministic form preview file changed unexpectedly"
                )
            _assert_directory_binding(root, parent_parts, parent_fd)
            return

        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
            0o600,
            dir_fd=root_fd,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)

        try:
            _assert_directory_binding(root, parent_parts, parent_fd)
            os.replace(
                temporary_name,
                filename,
                src_dir_fd=root_fd,
                dst_dir_fd=parent_fd,
            )
        except (NotImplementedError, TypeError) as exc:
            raise RuntimeError(
                "Secure local preview replacement is unavailable on this platform"
            ) from exc
        final_fd = _open_regular_file(parent_fd, filename)
        try:
            replaced_identity = os.fstat(final_fd)
        finally:
            os.close(final_fd)

        try:
            _assert_directory_binding(root, parent_parts, parent_fd)
            final_digest = _sha256_anchored_file(
                root,
                relative_path,
                max_bytes=_MAX_PREVIEW_BYTES,
            )
            os.fsync(parent_fd)
            _assert_directory_binding(root, parent_parts, parent_fd)
        except (OSError, RuntimeError, ValueError) as exc:
            _unlink_if_same_file(parent_fd, filename, replaced_identity)
            raise RuntimeError(
                "The deterministic form preview parent changed during the write"
            ) from exc
        if final_digest != expected_sha256:
            _unlink_if_same_file(parent_fd, filename, replaced_identity)
            raise RuntimeError("The deterministic form preview file failed verification")
    except OSError as exc:
        if replaced_identity is not None and parent_fd >= 0:
            _unlink_if_same_file(parent_fd, filename, replaced_identity)
        raise RuntimeError(
            "The deterministic form preview path is not a trusted local path"
        ) from exc
    finally:
        try:
            os.unlink(temporary_name, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)
            os.close(root_fd)


def _read_preview_artifact(
    artifact: ArtifactRecord,
    paths: CompanionPaths,
) -> dict[str, Any] | None:
    try:
        expected_relative = Path(
            _preview_filename(artifact.application_id, artifact.id)
        )
    except ValueError:
        return None
    stored_relative = _relative_stored_path(artifact.path, paths.artifacts)
    if stored_relative != expected_relative:
        return None
    try:
        digest, encoded = _read_anchored_file(
            paths.artifacts,
            expected_relative,
            capture=True,
            max_bytes=_MAX_PREVIEW_BYTES,
        )
    except (OSError, RuntimeError, ValueError):
        return None
    if digest != artifact.sha256 or encoded is None:
        return None
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("id") != artifact.id
        or payload.get("application_id") != artifact.application_id
        or payload.get("schema_version") != FORM_PREVIEW_SCHEMA
        or payload.get("mode") != "preview_only"
        or not isinstance(payload.get("fields"), list)
        or payload.get("safety") != _PREVIEW_SAFETY
    ):
        return None
    return payload


def _secure_dir_fd_available() -> bool:
    required = (os.open, os.mkdir, os.stat, os.unlink)
    return bool(
        os.name == "posix"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_NONBLOCK")
        and fcntl is not None
        and hasattr(fcntl, "flock")
        and all(function in os.supports_dir_fd for function in required)
        and _replace_supports_dir_fd()
    )


def _assert_secure_preview_storage(paths: CompanionPaths) -> None:
    if not _secure_dir_fd_available():
        raise RuntimeError(
            "Secure local form previews require POSIX directory-descriptor storage "
            "and are unavailable on this platform"
        )
    for root in (paths.imports, paths.artifacts):
        try:
            descriptor = _open_trusted_directory(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(
                "A local form-preview storage root is not a trusted non-symlink directory"
            ) from exc
        else:
            os.close(descriptor)


def _replace_supports_dir_fd() -> bool:
    try:
        parameters = inspect.signature(os.replace).parameters
    except (TypeError, ValueError):
        return False
    return {"src_dir_fd", "dst_dir_fd"}.issubset(parameters)


def _no_follow_flag() -> int:
    if not _secure_dir_fd_available():
        raise RuntimeError(
            "Secure local preview storage is unavailable on this platform"
        )
    return int(os.O_NOFOLLOW)


def _directory_flags() -> int:
    flags = os.O_RDONLY | int(os.O_DIRECTORY) | _no_follow_flag()
    return flags | int(getattr(os, "O_CLOEXEC", 0))


def _safe_path_component(value: str) -> str:
    if not value or value in {".", ".."} or Path(value).name != value:
        raise ValueError("Unsafe local preview path component")
    return value


def _preview_filename(application_id: str, preview_id: str) -> str:
    application = _safe_path_component(application_id)
    preview = _safe_path_component(preview_id)
    return _safe_path_component(f"form-preview-{application}-{preview}.json")


def _relative_parts(relative_path: Path) -> tuple[str, ...]:
    if relative_path.is_absolute() or not relative_path.parts:
        raise ValueError("Local preview paths must be relative to the artifacts root")
    parts = tuple(relative_path.parts)
    if any(part in {"", ".", ".."} or Path(part).name != part for part in parts):
        raise ValueError("Unsafe local preview path component")
    return parts


def _open_trusted_directory(root: Path) -> int:
    """Open an absolute directory component-by-component without symlink traversal."""

    if not _secure_dir_fd_available():
        raise RuntimeError(
            "Secure local preview storage is unavailable on this platform"
        )
    absolute = Path(os.path.abspath(os.fspath(root)))
    if not absolute.is_absolute():
        raise RuntimeError("The local preview artifacts root must be absolute")
    flags = _directory_flags()
    descriptor = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            next_descriptor = os.open(
                _safe_path_component(component),
                flags,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_relative_directory(
    root_fd: int,
    parts: tuple[str, ...],
    *,
    create: bool,
) -> int:
    descriptor = os.dup(root_fd)
    try:
        for raw_component in parts:
            component = _safe_path_component(raw_component)
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            next_descriptor = os.open(
                component,
                _directory_flags(),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_regular_file(parent_fd: int, filename: str) -> int:
    descriptor = os.open(
        _safe_path_component(filename),
        os.O_RDONLY
        | _no_follow_flag()
        | int(os.O_NONBLOCK)
        | int(getattr(os, "O_CLOEXEC", 0)),
        dir_fd=parent_fd,
    )
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise RuntimeError("Local preview storage accepts regular files only")
    return descriptor


def _lock_preview_storage_root(root_fd: int) -> None:
    if fcntl is None:  # pragma: no cover - guarded by _secure_dir_fd_available
        raise RuntimeError("Secure local preview locking is unavailable")
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX)
    except OSError as exc:
        raise RuntimeError(
            "Secure local preview locking is unavailable on this platform"
        ) from exc


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _same_file_generation(first: os.stat_result, second: os.stat_result) -> bool:
    return bool(
        _same_file(first, second)
        and first.st_mode == second.st_mode
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _assert_directory_binding(
    root: Path,
    parent_parts: tuple[str, ...],
    expected_parent_fd: int,
) -> None:
    fresh_root_fd = _open_trusted_directory(root)
    fresh_parent_fd = -1
    try:
        fresh_parent_fd = _open_relative_directory(
            fresh_root_fd,
            parent_parts,
            create=False,
        )
        if not _same_file(os.fstat(expected_parent_fd), os.fstat(fresh_parent_fd)):
            raise RuntimeError("The artifact parent changed during access")
    finally:
        if fresh_parent_fd >= 0:
            os.close(fresh_parent_fd)
        os.close(fresh_root_fd)


def _read_anchored_file(
    root: Path,
    relative_path: Path,
    *,
    capture: bool,
    max_bytes: int,
) -> tuple[str, bytes | None]:
    if max_bytes <= 0:
        raise ValueError("Anchored file reads require a positive byte limit")
    if os.name == "nt":
        from career_companion.services.windows_anchored_file import (
            read_anchored_file as read_windows_anchored_file,
        )

        return read_windows_anchored_file(
            root,
            relative_path,
            capture=capture,
            max_bytes=max_bytes,
        )
    parts = _relative_parts(relative_path)
    root_fd = _open_trusted_directory(root)
    parent_fd = -1
    file_fd = -1
    try:
        parent_fd = _open_relative_directory(root_fd, parts[:-1], create=False)
        file_fd = _open_regular_file(parent_fd, parts[-1])
        root_identity = os.fstat(root_fd)
        parent_identity = os.fstat(parent_fd)
        file_identity = os.fstat(file_fd)
        if file_identity.st_size > max_bytes:
            raise RuntimeError("The local artifact exceeds its bounded read limit")
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if capture else None
        total = 0
        with os.fdopen(os.dup(file_fd), "rb") as stream:
            while block := stream.read(min(1024 * 1024, max_bytes - total + 1)):
                total += len(block)
                if total > max_bytes:
                    raise RuntimeError("The local artifact exceeds its bounded read limit")
                digest.update(block)
                if chunks is not None:
                    chunks.append(block)

        # Resolve-based policy is checked before the final descriptor identity pass.
        # The final pass below is the linearization point for the bytes just read.
        _assert_resolves_beneath_root(root, relative_path)
        fresh_root_fd = _open_trusted_directory(root)
        fresh_parent_fd = -1
        fresh_file_fd = -1
        try:
            if not _same_file(root_identity, os.fstat(fresh_root_fd)):
                raise RuntimeError("The trusted artifacts root changed during access")
            fresh_parent_fd = _open_relative_directory(
                fresh_root_fd,
                parts[:-1],
                create=False,
            )
            if not _same_file(parent_identity, os.fstat(fresh_parent_fd)):
                raise RuntimeError("The artifact parent changed during access")
            fresh_file_fd = _open_regular_file(fresh_parent_fd, parts[-1])
            if not _same_file_generation(file_identity, os.fstat(fresh_file_fd)):
                raise RuntimeError("The artifact file changed during access")
        finally:
            if fresh_file_fd >= 0:
                os.close(fresh_file_fd)
            if fresh_parent_fd >= 0:
                os.close(fresh_parent_fd)
            os.close(fresh_root_fd)

        return digest.hexdigest(), b"".join(chunks) if chunks is not None else None
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(root_fd)


def _sha256_anchored_file(
    root: Path,
    relative_path: Path,
    *,
    max_bytes: int,
) -> str:
    digest, _content = _read_anchored_file(
        root,
        relative_path,
        capture=False,
        max_bytes=max_bytes,
    )
    return digest


def _assert_resolves_beneath_root(root: Path, relative_path: Path) -> None:
    lexical_root = Path(os.path.abspath(os.fspath(root)))
    resolved_root = root.resolve(strict=True)
    resolved_path = (root / relative_path).resolve(strict=True)
    if resolved_root != lexical_root:
        raise RuntimeError("The trusted artifacts root contains a symbolic link")
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise RuntimeError("The artifact path escaped its trusted root") from exc


def _relative_stored_path(stored_path: str, root: Path) -> Path | None:
    candidate = Path(stored_path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return None
    lexical_root = Path(os.path.abspath(os.fspath(root)))
    lexical_candidate = Path(os.path.abspath(os.fspath(candidate)))
    try:
        relative = lexical_candidate.relative_to(lexical_root)
        _relative_parts(relative)
    except ValueError:
        return None
    return relative


def _unlink_if_same_file(
    parent_fd: int,
    filename: str,
    expected: os.stat_result,
) -> None:
    """Remove our failed write while the caller holds the pinned-root inode lock."""

    descriptor = -1
    try:
        descriptor = _open_regular_file(parent_fd, filename)
        if _same_file(expected, os.fstat(descriptor)):
            os.unlink(filename, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except FileNotFoundError:
        pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
