from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from career_companion.database import ApplicationRecord, ArtifactRecord
from career_companion.paths import CompanionPaths
from career_companion.schemas import FormFillRequest, normalize_form_fill_url

BLOCKED_HOSTS = {"linkedin.com", "www.linkedin.com"}
_SUBMIT_SELECTOR = re.compile(
    r"(?<![a-z0-9])(?:submit|apply|send|confirm|complete)(?![a-z0-9])",
    re.IGNORECASE,
)


class FormFillRequestError(ValueError):
    """The request is not part of the strict form-fill contract."""


class FormFillResourceNotFound(LookupError):
    """A resource bound into the form-fill request no longer exists."""


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
) -> dict[str, str]:
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

        workspace = paths.workspace.resolve()
        validated_files: dict[str, str] = {}
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
            path = Path(artifact.path).resolve()
            try:
                path.relative_to(workspace)
            except ValueError as exc:
                raise PermissionError(
                    "Attachments must come from the Career Companion workspace"
                ) from exc
            if not path.is_file():
                raise FormFillResourceNotFound("Approved attachment file not found")
            validated_files[selector] = str(path)
    return validated_files
