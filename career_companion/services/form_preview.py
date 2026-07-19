from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any

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
from career_companion.services.profile import sha256_file


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
    file_sha256 = hashlib.sha256(encoded).hexdigest()
    preview_path = (
        paths.artifacts
        / application.id
        / "form-previews"
        / f"{preview_id}.json"
    )
    _write_deterministic_preview(preview_path, encoded, file_sha256)

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
    imports_root = paths.imports.resolve()
    for row in rows:
        path = Path(row.stored_path).resolve()
        try:
            path.relative_to(imports_root)
        except ValueError:
            continue
        if path.is_file() and sha256_file(path) == row.sha256:
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
        and value in document_text
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
        path = Path(row.path).resolve()
        try:
            path.relative_to(paths.workspace.resolve())
        except ValueError:
            continue
        if not path.is_file() or sha256_file(path) != row.sha256:
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
    path: Path,
    content: bytes,
    expected_sha256: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        if sha256_file(path) != expected_sha256:
            raise RuntimeError("The deterministic form preview file changed unexpectedly")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    if sha256_file(path) != expected_sha256:
        raise RuntimeError("The deterministic form preview file failed verification")


def _read_preview_artifact(
    artifact: ArtifactRecord,
    paths: CompanionPaths,
) -> dict[str, Any] | None:
    path = Path(artifact.path).resolve()
    expected_parent = (
        paths.artifacts / artifact.application_id / "form-previews"
    ).resolve()
    try:
        path.relative_to(expected_parent)
    except ValueError:
        return None
    if not path.is_file() or sha256_file(path) != artifact.sha256:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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
