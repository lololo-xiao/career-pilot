from __future__ import annotations

import hmac
import mimetypes
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from career_companion.database import ApplicationRecord, ArtifactRecord
from career_companion.paths import CompanionPaths
from career_companion.schemas import FormFillRequest, normalize_form_fill_url
from career_companion.services.form_preview import (
    _read_anchored_file,
    _relative_stored_path,
)

BLOCKED_HOSTS = {"linkedin.com", "www.linkedin.com"}
MAX_FORM_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_FORM_ATTACHMENTS_BYTES = 40 * 1024 * 1024
_SUBMIT_SELECTOR = re.compile(
    r"(?<![a-z0-9])(?:submit|apply|send|confirm|complete)(?![a-z0-9])",
    re.IGNORECASE,
)


class FormFillRequestError(ValueError):
    """The request is not part of the strict form-fill contract."""


class FormFillResourceNotFound(LookupError):
    """A resource bound into the form-fill request no longer exists."""


@dataclass(frozen=True, slots=True)
class ValidatedFilePayload:
    """Immutable bytes captured from one hash-bound approved artifact."""

    name: str
    mime_type: str
    buffer: bytes
    sha256: str

    def playwright_payload(self) -> dict[str, str | bytes]:
        return {
            "name": self.name,
            "mimeType": self.mime_type,
            "buffer": self.buffer,
        }


def canonical_form_fill_payload(
    value: FormFillRequest | dict[str, Any],
) -> dict[str, Any]:
    try:
        request = (
            value
            if isinstance(value, FormFillRequest)
            else FormFillRequest.model_validate(value)
        )
    except ValidationError as exc:
        raise FormFillRequestError(
            "Form-fill request does not match the strict schema"
        ) from exc
    return request.model_dump(mode="python")


def validate_application_url(value: str) -> str:
    try:
        normalized = normalize_form_fill_url(value)
    except ValueError as exc:
        raise FormFillRequestError(str(exc)) from exc
    parts = urlsplit(normalized)
    hostname = (parts.hostname or "").rstrip(".").casefold()
    if parts.username or parts.password:
        raise PermissionError("Credential-bearing application URLs are not allowed")
    if hostname in BLOCKED_HOSTS or hostname.endswith(".linkedin.com"):
        raise PermissionError("LinkedIn remains manual in Career Companion v1")
    return normalized


def _validate_selector(selector: str) -> str:
    if _SUBMIT_SELECTOR.search(selector):
        raise PermissionError("Submit-like controls cannot be targeted")
    return selector


def validate_form_payload(
    session: Session,
    payload: dict[str, Any],
    paths: CompanionPaths,
) -> Mapping[str, ValidatedFilePayload]:
    """Validate current resources and safety without creating records or audit rows."""

    validate_application_url(payload["url"])
    application_id = payload["application_id"]
    with session.no_autoflush:
        application_exists = session.scalar(
            select(ApplicationRecord.id).where(ApplicationRecord.id == application_id)
        )
        if application_exists is None:
            raise FormFillResourceNotFound("Application not found")

        for selector in payload["fields"]:
            _validate_selector(selector)

        validated_files: dict[str, ValidatedFilePayload] = {}
        captured_bytes = 0
        for selector, artifact_id in payload["files"].items():
            _validate_selector(selector)
            local_artifact = session.get(ArtifactRecord, artifact_id)
            if local_artifact is not None and not local_artifact.approved:
                raise PermissionError("Every attached artifact must be approved")
            artifact = session.execute(
                select(
                    ArtifactRecord.application_id,
                    ArtifactRecord.approved,
                    ArtifactRecord.path,
                    ArtifactRecord.sha256,
                ).where(ArtifactRecord.id == artifact_id)
            ).one_or_none()
            if artifact is None:
                raise FormFillResourceNotFound("Attached artifact not found")
            if artifact.application_id != application_id:
                raise PermissionError(
                    "Every attached artifact must belong to this application"
                )
            if not artifact.approved:
                raise PermissionError("Every attached artifact must be approved")
            relative_path = _relative_stored_path(artifact.path, paths.workspace)
            if relative_path is None:
                raise PermissionError(
                    "Attachments must come from the Career Companion workspace"
                )
            if re.fullmatch(r"[a-f0-9]{64}", artifact.sha256) is None:
                raise PermissionError("Approved attachment has an invalid stored SHA-256")
            remaining_bytes = MAX_FORM_ATTACHMENTS_BYTES - captured_bytes
            if remaining_bytes <= 0:
                raise PermissionError("Approved attachments exceed the aggregate byte limit")
            try:
                digest, content = _read_anchored_file(
                    paths.workspace,
                    relative_path,
                    capture=True,
                    max_bytes=min(MAX_FORM_ATTACHMENT_BYTES, remaining_bytes),
                )
            except FileNotFoundError as exc:
                raise FormFillResourceNotFound(
                    "Approved attachment file not found"
                ) from exc
            except (OSError, RuntimeError, ValueError) as exc:
                raise PermissionError(
                    "Approved attachment could not be read safely within its byte limits"
                ) from exc
            if content is None or not hmac.compare_digest(digest, artifact.sha256):
                raise PermissionError(
                    "Approved attachment bytes no longer match the stored SHA-256"
                )
            captured_bytes += len(content)
            mime_type = mimetypes.guess_type(relative_path.name)[0]
            validated_files[selector] = ValidatedFilePayload(
                name=relative_path.name,
                mime_type=mime_type or "application/octet-stream",
                buffer=content,
                sha256=digest,
            )
    return MappingProxyType(validated_files)
