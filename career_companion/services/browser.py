from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

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
_SAFE_INITIAL_METHODS = {"GET", "HEAD", "OPTIONS"}
_MAX_INITIAL_DOCUMENT_REQUESTS = 8
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
        approved_destination = validate_application_url(payload["url"])
        approved_origin = _url_origin(approved_destination)
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Install the browser extra and Playwright Chromium first") from exc
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        blocked_requests: list[dict[str, str]] = []
        blocked_web_sockets: list[str] = []
        network_phase = {"frozen": False, "document_requests": 0}

        async def enforce_network_boundary(route, request) -> None:
            method = request.method.upper()
            reason = ""
            if network_phase["frozen"]:
                reason = "network-frozen-after-navigation"
            elif method not in _SAFE_INITIAL_METHODS:
                reason = "mutating-method"
            elif _url_origin(request.url) != approved_origin:
                reason = "cross-origin-initial-load"
            elif _is_document_navigation(request):
                network_phase["document_requests"] += 1
                if network_phase["document_requests"] > _MAX_INITIAL_DOCUMENT_REQUESTS:
                    reason = "initial-navigation-chain-limit"
            if reason:
                blocked_requests.append(
                    {"method": method, "url": request.url[:500], "reason": reason}
                )
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
        await self._context.route("**/*", enforce_network_boundary)
        await self._context.route_web_socket("**/*", block_web_socket)
        await self._context.add_init_script(_FORM_GUARD_INSTALL)
        page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._page = page
        filled: list[str] = []
        bound_handles: list[Any] = []
        try:
            await page.goto(
                payload["url"],
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            network_phase["frozen"] = True
            await page.evaluate(_FORM_GUARD_INSTALL)
            _assert_exact_destination(page, approved_destination)

            field_targets = []
            for selector, value in payload.get("fields", {}).items():
                locator, handle, metadata = await _single_safe_control(
                    page,
                    selector,
                    file_control=False,
                )
                bound_handles.append(handle)
                field_targets.append((selector, value, locator, handle, metadata))
            file_targets = []
            for selector in payload.get("files", {}):
                locator, handle, metadata = await _single_safe_control(
                    page,
                    selector,
                    file_control=True,
                )
                bound_handles.append(handle)
                file_targets.append((selector, locator, handle, metadata))

            _assert_exact_destination(page, approved_destination)
            validated_files = validate_form_payload(session, payload, self.paths)
            for selector, _value, locator, handle, metadata in field_targets:
                await _assert_bound_control(
                    locator,
                    handle,
                    metadata,
                    selector,
                    file_control=False,
                )
            for selector, locator, handle, metadata in file_targets:
                await _assert_bound_control(
                    locator,
                    handle,
                    metadata,
                    selector,
                    file_control=True,
                )
            _assert_exact_destination(page, approved_destination)
            consume_approval(session, "application.form_fill", payload)

            for selector, value, locator, handle, metadata in field_targets:
                await _assert_bound_control(
                    locator,
                    handle,
                    metadata,
                    selector,
                    file_control=False,
                )
                _assert_exact_destination(page, approved_destination)
                if metadata["tag"] == "select":
                    await handle.select_option(str(value))
                else:
                    await handle.fill(str(value))
                _assert_exact_destination(page, approved_destination)
                await _assert_bound_control(
                    locator,
                    handle,
                    metadata,
                    selector,
                    file_control=False,
                )
                filled.append(selector)
            for selector, locator, handle, metadata in file_targets:
                await _assert_bound_control(
                    locator,
                    handle,
                    metadata,
                    selector,
                    file_control=True,
                )
                _assert_exact_destination(page, approved_destination)
                await handle.set_input_files(
                    validated_files[selector].playwright_payload()
                )
                _assert_exact_destination(page, approved_destination)
                await _assert_bound_control(
                    locator,
                    handle,
                    metadata,
                    selector,
                    file_control=True,
                )
                filled.append(selector)
            _assert_exact_destination(page, approved_destination)
        finally:
            network_phase["frozen"] = True
            for handle in bound_handles:
                try:
                    await handle.dispose()
                except Exception:
                    pass
            try:
                await page.evaluate(_FORM_GUARD_REMOVE)
            except Exception:
                pass
            await self._context.unroute("**/*", enforce_network_boundary)
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


async def _single_safe_control(page, selector: str, *, file_control: bool):
    locator = page.locator(selector)
    count = await locator.count()
    if count != 1:
        raise ValueError(f"Selector must match exactly one control, found {count}: {selector}")
    handle = await locator.element_handle()
    if handle is None:
        raise ValueError(f"Control detached before it could be bound: {selector}")
    metadata = await _control_metadata(handle)
    _validate_control_metadata(metadata, selector, file_control=file_control)
    return locator, handle, metadata


def _validate_control_metadata(
    metadata: dict[str, Any],
    selector: str,
    *,
    file_control: bool,
) -> None:
    if metadata.get("disabled"):
        raise ValueError(f"Control is disabled: {selector}")
    tag = metadata.get("tag")
    control_type = metadata.get("type", "")
    if file_control:
        if tag != "input" or control_type != "file":
            raise PermissionError("Attachments may target only one file input")
        return
    if tag == "input" and control_type in _FILLABLE_INPUT_TYPES:
        return
    if tag in {"textarea", "select"} or metadata.get("contenteditable"):
        return
    raise PermissionError("Only non-submit text, choice, or editable controls may be filled")


async def _assert_bound_control(
    locator,
    handle,
    expected_metadata: dict[str, Any],
    selector: str,
    *,
    file_control: bool,
) -> None:
    if await locator.count() != 1:
        raise PermissionError(f"Control binding changed after preflight: {selector}")
    current = await locator.element_handle()
    if current is None:
        raise PermissionError(f"Control detached after preflight: {selector}")
    try:
        same_element = await handle.evaluate(
            "(element, current) => element.isConnected && element === current",
            current,
        )
    finally:
        try:
            await current.dispose()
        except Exception:
            pass
    if not same_element:
        raise PermissionError(f"Control was detached or replaced after preflight: {selector}")
    metadata = await _control_metadata(handle)
    _validate_control_metadata(metadata, selector, file_control=file_control)
    if _control_signature(metadata) != _control_signature(expected_metadata):
        raise PermissionError(f"Control semantics changed after preflight: {selector}")


def _control_signature(metadata: dict[str, Any]) -> tuple[Any, ...]:
    return (
        metadata.get("tag"),
        metadata.get("type", ""),
        bool(metadata.get("disabled")),
        bool(metadata.get("contenteditable")),
    )


def _assert_exact_destination(page, approved_destination: str) -> None:
    if "#" in page.url:
        raise PermissionError("Navigation changed from the exact approved application URL")
    try:
        current = validate_application_url(page.url)
    except Exception as exc:
        if isinstance(exc, PermissionError):
            raise
        raise PermissionError(
            "Navigation changed from the exact approved application URL"
        ) from exc
    if current != approved_destination:
        raise PermissionError("Navigation changed from the exact approved application URL")


def _url_origin(value: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(value)
        scheme = parts.scheme.casefold()
        hostname = (parts.hostname or "").rstrip(".").casefold()
        port = parts.port
    except ValueError:
        return None
    if scheme not in {"http", "https"} or not hostname:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname, port


def _is_document_navigation(request) -> bool:
    predicate = getattr(request, "is_navigation_request", None)
    if not callable(predicate) or not predicate():
        return False
    return getattr(request, "resource_type", "document") == "document"
