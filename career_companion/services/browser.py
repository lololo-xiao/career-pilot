from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from career_companion.paths import CompanionPaths
from career_companion.services.approvals import consume_approval
from career_companion.services.audit import record_audit
from career_companion.services.form_fill import (
    canonical_form_fill_payload,
    validate_application_url,
    validate_form_payload,
)

_FILLABLE_INPUT_TYPES = {
    "",
    "date",
    "datetime-local",
    "email",
    "month",
    "number",
    "search",
    "tel",
    "text",
    "time",
    "url",
    "week",
}
_FORM_GUARD_INSTALL = """
(() => {
  if (window.__careerCompanionFormGuard) return;
  const submit = HTMLFormElement.prototype.submit;
  const requestSubmit = HTMLFormElement.prototype.requestSubmit;
  const listener = (event) => { event.preventDefault(); event.stopImmediatePropagation(); };
  document.addEventListener('submit', listener, true);
  HTMLFormElement.prototype.submit = function () { throw new Error('Submission blocked by Career Companion'); };
  HTMLFormElement.prototype.requestSubmit = function () { throw new Error('Submission blocked by Career Companion'); };
  window.__careerCompanionFormGuard = { submit, requestSubmit, listener };
})()
"""
_FORM_GUARD_REMOVE = """
(() => {
  const guard = window.__careerCompanionFormGuard;
  if (!guard) return;
  HTMLFormElement.prototype.submit = guard.submit;
  HTMLFormElement.prototype.requestSubmit = guard.requestSubmit;
  document.removeEventListener('submit', guard.listener, true);
  delete window.__careerCompanionFormGuard;
})()
"""


class BrowserAssistant:
    def __init__(self, paths: CompanionPaths) -> None:
        self.paths = paths
        self._playwright = None
        self._context = None
        self._page = None

    async def fill(self, session: Session, payload: dict[str, Any]) -> dict[str, Any]:
        canonical_payload = canonical_form_fill_payload(payload)
        validate_form_payload(session, canonical_payload, self.paths)
        payload = canonical_payload
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Install the browser extra and Playwright Chromium first") from exc
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        blocked_requests: list[dict[str, str]] = []
        blocked_web_sockets: list[str] = []

        async def block_mutating_requests(route, request) -> None:
            method = request.method.upper()
            if method not in {"GET", "HEAD", "OPTIONS"}:
                blocked_requests.append({"method": method, "url": request.url[:500]})
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        async def block_web_socket(web_socket) -> None:
            blocked_web_sockets.append(web_socket.url[:500])
            await web_socket.close(code=1008, reason="External WebSocket blocked")

        if self._context is None:
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.paths.browser),
                headless=bool(payload.get("headless", False)),
                service_workers="block",
            )
        await self._context.route("**/*", block_mutating_requests)
        await self._context.route_web_socket("**/*", block_web_socket)
        await self._context.add_init_script(_FORM_GUARD_INSTALL)
        page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._page = page
        approved_destination = validate_application_url(payload["url"])
        filled: list[str] = []
        try:
            await page.goto(
                payload["url"],
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await page.evaluate(_FORM_GUARD_INSTALL)
            if validate_application_url(page.url) != approved_destination:
                raise PermissionError(
                    "Navigation changed from the exact approved application URL"
                )

            field_targets = []
            for selector, value in payload.get("fields", {}).items():
                locator = await _single_safe_locator(page, selector, file_control=False)
                metadata = await _control_metadata(locator)
                field_targets.append((selector, value, locator, metadata))
            file_targets = []
            for selector in payload.get("files", {}):
                locator = await _single_safe_locator(page, selector, file_control=True)
                file_targets.append((selector, locator))

            if validate_application_url(page.url) != approved_destination:
                raise PermissionError(
                    "Navigation changed from the exact approved application URL"
                )
            validated_files = validate_form_payload(session, payload, self.paths)
            consume_approval(session, "application.form_fill", payload)

            for selector, value, locator, metadata in field_targets:
                if metadata["tag"] == "select":
                    await locator.select_option(str(value))
                else:
                    await locator.fill(str(value))
                filled.append(selector)
            for selector, locator in file_targets:
                await locator.set_input_files(validated_files[selector])
                filled.append(selector)
            if validate_application_url(page.url) != approved_destination:
                raise PermissionError(
                    "Navigation changed from the exact approved application URL"
                )
        finally:
            try:
                await page.evaluate(_FORM_GUARD_REMOVE)
            except Exception:
                pass
            await self._context.unroute("**/*", block_mutating_requests)
        blocked_mutations = len(blocked_requests) + len(blocked_web_sockets)
        record_audit(
            session,
            "application.form_filled",
            subject_type="application",
            subject_id=str(payload.get("application_id", "")),
            payload={
                "url": page.url,
                "controls": filled,
                "submitted": False,
                "blocked_mutating_requests": blocked_mutations,
            },
        )
        return {
            "status": "filled",
            "controls": filled,
            "submitted": False,
            "blocked_mutating_requests": blocked_mutations,
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
    if tag == "input" and control_type in _FILLABLE_INPUT_TYPES:
        return locator
    if tag in {"textarea", "select"} or metadata.get("contenteditable"):
        return locator
    raise PermissionError("Only non-submit text, choice, or editable controls may be filled")
