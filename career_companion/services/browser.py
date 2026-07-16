from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from career_companion.database import ArtifactRecord
from career_companion.paths import CompanionPaths
from career_companion.services.approvals import consume_approval
from career_companion.services.audit import record_audit

BLOCKED_HOSTS = {"linkedin.com", "www.linkedin.com"}
_SUBMIT_SELECTOR = re.compile(
    r"(?<![a-z0-9])(?:submit|apply|send|confirm|complete)(?![a-z0-9])",
    re.IGNORECASE,
)
_BLOCKED_FIELD_TYPES = {"button", "file", "hidden", "image", "password", "reset", "submit"}
_MAX_CONTROLS = 100
_MAX_VALUE_CHARACTERS = 10_000
_FORM_GUARD_INSTALL = """
() => {
  if (window.__careerCompanionFormGuard) return;
  const submit = HTMLFormElement.prototype.submit;
  const requestSubmit = HTMLFormElement.prototype.requestSubmit;
  const listener = (event) => { event.preventDefault(); event.stopImmediatePropagation(); };
  document.addEventListener('submit', listener, true);
  HTMLFormElement.prototype.submit = function () { throw new Error('Submission blocked by Career Companion'); };
  HTMLFormElement.prototype.requestSubmit = function () { throw new Error('Submission blocked by Career Companion'); };
  window.__careerCompanionFormGuard = { submit, requestSubmit, listener };
}
"""
_FORM_GUARD_REMOVE = """
() => {
  const guard = window.__careerCompanionFormGuard;
  if (!guard) return;
  HTMLFormElement.prototype.submit = guard.submit;
  HTMLFormElement.prototype.requestSubmit = guard.requestSubmit;
  document.removeEventListener('submit', guard.listener, true);
  delete window.__careerCompanionFormGuard;
}
"""


def _validated_application_url(value: Any) -> str:
    url = str(value or "").strip()
    parts = urlsplit(url)
    hostname = (parts.hostname or "").rstrip(".").casefold()
    if (
        parts.scheme.casefold() not in {"http", "https"}
        or not hostname
        or parts.username
        or parts.password
    ):
        raise ValueError("A valid credential-free HTTP or HTTPS application URL is required")
    if hostname in BLOCKED_HOSTS or hostname.endswith(".linkedin.com"):
        raise PermissionError("LinkedIn remains manual in Career Companion v1")
    return url


def _validate_selector(selector: Any) -> str:
    value = str(selector).strip()
    if not value or len(value) > 1000:
        raise ValueError("Form selectors must contain between 1 and 1000 characters")
    if _SUBMIT_SELECTOR.search(value):
        raise PermissionError("Submit-like controls cannot be targeted")
    return value


def validate_form_payload(session: Session, payload: dict[str, Any], paths: CompanionPaths) -> None:
    _validated_application_url(payload.get("url"))
    fields = payload.get("fields", {})
    files = payload.get("files", {})
    if not isinstance(fields, dict) or not isinstance(files, dict):
        raise ValueError("Form fields and files must be selector mappings")
    if len(fields) + len(files) > _MAX_CONTROLS:
        raise ValueError(f"At most {_MAX_CONTROLS} controls may be filled at once")
    for selector, value in fields.items():
        _validate_selector(selector)
        if len(str(value)) > _MAX_VALUE_CHARACTERS:
            raise ValueError("A form field value exceeds the 10,000 character limit")
    for selector, artifact_id in files.items():
        _validate_selector(selector)
        artifact = session.get(ArtifactRecord, artifact_id)
        if not artifact or not artifact.approved:
            raise PermissionError("Every attached artifact must be approved")
        path = Path(artifact.path).resolve()
        try:
            path.relative_to(paths.workspace.resolve())
        except ValueError as exc:
            raise PermissionError(
                "Attachments must come from the Career Companion workspace"
            ) from exc
        if not path.is_file():
            raise FileNotFoundError(path)


class BrowserAssistant:
    def __init__(self, paths: CompanionPaths) -> None:
        self.paths = paths
        self._playwright = None
        self._context = None
        self._page = None

    async def fill(self, session: Session, payload: dict[str, Any]) -> dict[str, Any]:
        validate_form_payload(session, payload, self.paths)
        consume_approval(session, "application.form_fill", payload)
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Install the browser extra and Playwright Chromium first") from exc
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        if self._context is None:
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.paths.browser), headless=bool(payload.get("headless", False))
            )
        page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._page = page
        await page.goto(
            payload["url"],
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        _validated_application_url(page.url)
        blocked_requests: list[dict[str, str]] = []

        async def block_mutating_requests(route, request) -> None:
            method = request.method.upper()
            if method not in {"GET", "HEAD", "OPTIONS"}:
                blocked_requests.append({"method": method, "url": request.url[:500]})
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        await page.evaluate(_FORM_GUARD_INSTALL)
        await page.route("**/*", block_mutating_requests)
        filled: list[str] = []
        try:
            for selector, value in payload.get("fields", {}).items():
                locator = await _single_safe_locator(page, selector, file_control=False)
                metadata = await _control_metadata(locator)
                if metadata["tag"] == "select":
                    await locator.select_option(str(value))
                else:
                    await locator.fill(str(value))
                filled.append(selector)
            for selector, artifact_id in payload.get("files", {}).items():
                locator = await _single_safe_locator(page, selector, file_control=True)
                artifact = session.get(ArtifactRecord, artifact_id)
                assert artifact is not None
                await locator.set_input_files(artifact.path)
                filled.append(selector)
            _validated_application_url(page.url)
        finally:
            await page.unroute("**/*", block_mutating_requests)
            await page.evaluate(_FORM_GUARD_REMOVE)
        record_audit(
            session,
            "application.form_filled",
            subject_type="application",
            subject_id=str(payload.get("application_id", "")),
            payload={
                "url": page.url,
                "controls": filled,
                "submitted": False,
                "blocked_mutating_requests": len(blocked_requests),
            },
        )
        return {
            "status": "filled",
            "controls": filled,
            "submitted": False,
            "blocked_mutating_requests": len(blocked_requests),
            "message": "Review the browser and submit manually when ready.",
        }

    async def close(self) -> None:
        if self._context:
            await self._context.close()
            self._context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self._page = None


async def _control_metadata(locator) -> dict[str, Any]:
    return await locator.evaluate(
        """
        element => ({
          tag: element.tagName.toLowerCase(),
          type: (element.getAttribute('type') || '').toLowerCase(),
          disabled: Boolean(element.disabled),
          contenteditable: Boolean(element.isContentEditable)
        })
        """
    )


async def _single_safe_locator(page, selector: str, *, file_control: bool):
    locator = page.locator(selector)
    count = await locator.count()
    if count != 1:
        raise ValueError(f"Selector must match exactly one control, found {count}: {selector}")
    metadata = await _control_metadata(locator)
    if metadata.get("disabled"):
        raise ValueError(f"Control is disabled: {selector}")
    tag = metadata.get("tag")
    control_type = metadata.get("type", "")
    if file_control:
        if tag != "input" or control_type != "file":
            raise PermissionError("Attachments may target only one file input")
        return locator
    if tag == "input" and control_type not in _BLOCKED_FIELD_TYPES:
        return locator
    if tag in {"textarea", "select"} or metadata.get("contenteditable"):
        return locator
    raise PermissionError("Only non-submit text, choice, or editable controls may be filled")
