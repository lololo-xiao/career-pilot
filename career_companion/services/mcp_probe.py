"""Approval-bound MCP connection health and tool discovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Coroutine, Literal
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

import anyio
from anyio.abc import Process
from mcp.os.win32.utilities import (
    FallbackProcess,
    create_windows_process,
    get_windows_executable_command,
    terminate_windows_process_tree,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from career_companion.database import ApprovalRecord, AuditEventRecord, MCPServerRecord
from career_companion.hermes import _SAFE_PARENT_ENV
from career_companion.paths import CompanionPaths
from career_companion.services.approvals import (
    _request_validated_mcp_probe_approval,
    consume_approval,
    decide_approval,
    has_current_consumer_validation,
    payload_digest,
)
from career_companion.services.audit import record_audit
from career_companion.services.mcp_servers import (
    MCP_RESERVED_ENVIRONMENT,
    _validate_mcp_server_runtime_config,
    mcp_server_json,
    normalized_hermes_tool_name,
    validate_mcp_tool_allowlist,
)


MCP_PROBE_ACTION = "mcp.probe"
MCP_PROBE_VERSION = 1
MCP_PROBE_INTENT_TTL_MINUTES = 5
MCP_PROBE_WORKER_TIMEOUT_SECONDS = 13.0
MCP_PROBE_COOLDOWN_SECONDS = 2.0
MCP_PROBE_MAX_OUTPUT_BYTES = 256 * 1024
MCP_PROBE_MAX_TOOLS = 128
MCP_PROBE_WINDOWS_ROOT_WAIT_SECONDS = 1.0
MCP_PROBE_SUBSTRING_REDACTION_MIN_LENGTH = 8
MCP_PROBE_REDACTION_PLACEHOLDER = "[redacted]"
MCP_PROBE_CONTAINMENT_ERROR = "process_containment_unavailable"
_ENV_REFERENCE = re.compile(r"\$\{([^}]+)\}")
_ALLOWED_WORKER_ERRORS = {
    "authentication_required",
    "command_not_found",
    "connection_closed",
    "invalid_configuration",
    "invalid_target",
    "private_target_blocked",
    "probe_failed",
    MCP_PROBE_CONTAINMENT_ERROR,
    "protocol_error",
    "redirect_blocked",
    "response_too_large",
    "server_request_blocked",
    "target_changed",
    "timed_out",
    "unreachable",
    "unsupported_protocol",
    "unsupported_transport",
}
_PROBE_STATUSES = {
    "configuration_issue",
    "denied",
    "policy_blocked",
    "protocol_error",
    "ready",
    "ready_no_tools",
    "timed_out",
    "unreachable",
}
_PUBLIC_DISCLOSURE_KEYS = (
    "transport",
    "target",
    "target_label",
    "bound_target_note",
    "operations",
    "risk",
    "timeout_seconds",
    "launches_subprocess",
    "network_possible",
    "configuration_will_change",
    "server_side_effects_possible",
)


class MCPProbeConflict(RuntimeError):
    pass


class MCPProbeBusy(RuntimeError):
    pass


class MCPProbeConfigurationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def _complete_cleanup(cleanup: Coroutine[Any, Any, None]) -> None:
    """Finish bounded cleanup even if the owning request is cancelled."""

    task = asyncio.create_task(cleanup)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
    task.result()
    if cancellation is not None:
        raise cancellation


def _runtime_server_payload(server: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in server.items()
        if key not in {"command_available", "last_probe"}
    }


def _probe_approval_payload(server: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": MCP_PROBE_ACTION,
        "version": MCP_PROBE_VERSION,
        "server": _runtime_server_payload(server),
    }


def server_revision(row: MCPServerRecord) -> str:
    updated = row.updated_at
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    return updated.astimezone(UTC).isoformat(timespec="microseconds")


def _sanitized_http_target(url: str) -> str:
    if url.startswith("${"):
        reference = url.removeprefix("${").removesuffix("}")
        if reference.startswith("env:"):
            reference = reference[len("env:") :]
        return f"Endpoint from environment variable {reference}"
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def probe_disclosure(server: dict[str, Any]) -> dict[str, Any]:
    _validate_mcp_server_runtime_config(_runtime_server_payload(server))
    transport = server["transport"]
    if transport == "stdio":
        target = str(server["command"])
        argument_count = len(server["args"])
        target_label = "Command summary"
        bound_target_note = (
            f"The exact saved command and {argument_count} saved "
            f"argument{' is' if argument_count == 1 else 's are'} bound to this review. "
            "Arguments are hidden here because they can contain credentials; confirming "
            "launches all of them without a shell."
        )
        risk = (
            "Confirming launches this local command without a shell. Its startup code "
            "can read or change local data and can use the network with CareerPilot's "
            "operating-system permissions."
        )
    else:
        target = _sanitized_http_target(str(server["url"]))
        target_label = "Endpoint summary"
        if str(server["url"]).startswith("${"):
            bound_target_note = (
                "The exact saved environment-variable reference is bound to this review. "
                "Its current endpoint value is resolved only for the isolated check and is "
                "hidden because its path or query can contain credentials."
            )
        else:
            bound_target_note = (
                "The complete saved endpoint, including its path and query, is bound to this "
                "review. The path and query are hidden here because they can contain credentials."
            )
        risk = (
            "Confirming connects to this endpoint. CareerPilot blocks redirects, proxy "
            "inheritance, private-address DNS rebinding, and non-HTTPS remote targets."
        )
    return {
        "transport": transport,
        "target": target,
        "target_label": target_label,
        "bound_target_note": bound_target_note,
        "operations": [
            "MCP initialize",
            "MCP initialized notification",
            "MCP tools/list (up to 4 paginated requests)",
        ],
        "risk": risk,
        "timeout_seconds": 10,
        "launches_subprocess": transport == "stdio",
        "network_possible": True,
        "configuration_will_change": False,
        "server_side_effects_possible": True,
    }


def public_probe_disclosure(preview: Any) -> dict[str, Any]:
    """Return only disclosure fields allowed by the strict public response schema."""

    if not isinstance(preview, dict):
        raise ValueError("Stored MCP probe disclosure is invalid")
    return {key: preview[key] for key in _PUBLIC_DISCLOSURE_KEYS if key in preview}


def create_probe_intent(
    session: Session,
    row: MCPServerRecord,
) -> ApprovalRecord:
    server = mcp_server_json(row)
    return _request_validated_mcp_probe_approval(
        session,
        _probe_approval_payload(server),
        probe_disclosure(server),
        MCP_PROBE_INTENT_TTL_MINUTES,
    )


def resolve_probe_intent(
    session: Session,
    approval_id: str,
    server: dict[str, Any],
    decision: Literal["approved", "denied"],
) -> bool:
    approval = session.get(ApprovalRecord, approval_id)
    if approval is None or approval.action_type != MCP_PROBE_ACTION:
        raise LookupError("MCP probe approval not found")
    expected_digest = payload_digest(_probe_approval_payload(server))
    if approval.payload_digest != expected_digest:
        raise MCPProbeConflict("The MCP server changed. Review a new connection disclosure.")
    if not has_current_consumer_validation(approval.preview, MCP_PROBE_ACTION):
        raise MCPProbeConflict(
            "This MCP probe approval is not valid for the current consumer. Review it again."
        )
    now = datetime.now(UTC)
    expires_at = approval.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        if approval.decision == "pending":
            approval.decision = "expired"
            approval.decided_at = now
        raise MCPProbeConflict("The MCP probe approval expired. Review it again.")

    resolved_decision = approval.decision
    if resolved_decision == "pending":
        try:
            approval = decide_approval(session, approval_id, decision)
        except ValueError:
            session.expire_all()
            approval = session.get(ApprovalRecord, approval_id)
            if approval is None:
                raise LookupError("MCP probe approval not found") from None
        resolved_decision = approval.decision
    if resolved_decision != decision:
        raise MCPProbeConflict("The MCP probe approval has already been decided.")

    if decision == "denied":
        return False
    try:
        consume_approval(
            session,
            MCP_PROBE_ACTION,
            _probe_approval_payload(server),
            approval_id=approval_id,
        )
    except PermissionError as exc:
        raise MCPProbeConflict(
            "The MCP probe approval has already been used or is no longer valid."
        ) from exc
    return True


def _safe_parent_environment() -> dict[str, str]:
    reserved = {name.casefold() for name in MCP_RESERVED_ENVIRONMENT}
    return {
        name: value
        for name, value in os.environ.items()
        if name in _SAFE_PARENT_ENV and name.casefold() not in reserved
    }


def _reference_name(raw: str) -> str:
    name = raw.strip()
    if name.startswith("env:"):
        name = name[len("env:") :].strip()
    return name


def _resolve_text(value: str, forwarded: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = _reference_name(match.group(1))
        try:
            return forwarded[name]
        except KeyError as exc:
            raise MCPProbeConfigurationError("missing_environment") from exc

    return _ENV_REFERENCE.sub(replace, value)


def prepare_worker_payload(
    server: dict[str, Any],
    paths: CompanionPaths,
) -> tuple[dict[str, Any], set[str]]:
    runtime = _runtime_server_payload(server)
    _validate_mcp_server_runtime_config(runtime)
    forwarded: dict[str, str] = {}
    for name in runtime["forwarded_environment"]:
        value = os.environ.get(name)
        if value is None:
            raise MCPProbeConfigurationError("missing_environment")
        forwarded[name] = value

    configured_environment = {
        name: _resolve_text(value, forwarded)
        for name, value in runtime["environment"].items()
    }
    secrets_to_redact = {
        value
        for value in [
            *forwarded.values(),
            *runtime["environment"].values(),
            *configured_environment.values(),
        ]
        if value
    }
    server_environment = _safe_parent_environment() | forwarded | configured_environment
    if runtime["transport"] == "stdio":
        command = str(runtime["command"])
        args = list(runtime["args"])
        if _ENV_REFERENCE.search(command) or any(
            _ENV_REFERENCE.search(value) for value in args
        ):
            raise MCPProbeConfigurationError("secret_in_process_arguments")
        payload = {
            "transport": "stdio",
            "command": command,
            "args": args,
            "environment": server_environment,
            "cwd": str(paths.workspace),
        }
    else:
        url = _resolve_text(str(runtime["url"]), forwarded)
        secrets_to_redact.update(_http_url_secret_values(url))
        payload = {"transport": "http", "url": url}
    return payload, secrets_to_redact


def _http_url_secret_values(url: str) -> set[str]:
    parsed = urlsplit(url)
    values = {url}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if value:
            values.add(value)
        elif key:
            values.add(key)
    values.update(
        decoded
        for component in parsed.path.split("/")
        if (decoded := unquote(component))
    )
    return values


class _WorkerOutputTooLarge(RuntimeError):
    pass


def _is_windows() -> bool:
    return os.name == "nt"


def _has_confirmed_windows_job(process: Process | FallbackProcess) -> bool:
    """The SDK publishes this handle only after successful Job assignment."""

    return bool(getattr(process, "_job_object", None))


async def _create_worker_process(
    worker: Path,
    environment: dict[str, str],
) -> Process | FallbackProcess:
    if _is_windows():  # pragma: no cover - exercised by native Windows CI
        command = get_windows_executable_command(sys.executable)
        # The SDK attempts to assign this worker to a kill-on-close Job Object.
        # The caller must confirm its published handle before sending a payload:
        # attachment failure is otherwise silent in MCP 1.26.
        return await create_windows_process(
            command,
            ["-I", str(worker)],
            environment,
            subprocess.DEVNULL,
            None,
        )
    return await anyio.open_process(
        [sys.executable, "-I", str(worker)],
        env=environment,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


async def _bounded_wait_windows_root(
    process: Process | FallbackProcess,
    timeout_seconds: float,
) -> bool:
    """Bound root reaping without FallbackProcess's non-cancellable wait."""

    popen = getattr(process, "popen", None)
    poll = getattr(popen, "poll", None)
    if callable(poll):
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                if poll() is not None:
                    return True
            except (ChildProcessError, ProcessLookupError):
                return True
            except Exception:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await anyio.sleep(min(0.02, remaining))
    try:
        with anyio.move_on_after(timeout_seconds) as scope:
            await process.wait()
        return not scope.cancel_called
    except (ChildProcessError, ProcessLookupError):
        return True
    except Exception:
        return False


async def _reap_windows_root(
    process: Process | FallbackProcess,
    *,
    terminate_first: bool,
) -> None:
    if terminate_first:
        try:
            process.terminate()
        except Exception:
            pass
    if await _bounded_wait_windows_root(
        process,
        MCP_PROBE_WINDOWS_ROOT_WAIT_SECONDS,
    ):
        return
    popen = getattr(process, "popen", None)
    force_kill = getattr(popen, "kill", None) or getattr(process, "kill", None)
    if callable(force_kill):
        try:
            force_kill()
        except Exception:
            pass
    await _bounded_wait_windows_root(
        process,
        MCP_PROBE_WINDOWS_ROOT_WAIT_SECONDS,
    )


async def _terminate_worker(
    process: Process | FallbackProcess,
    worker_process_group: int | None,
) -> None:
    if _is_windows():  # pragma: no cover - exercised by native Windows CI
        contained = _has_confirmed_windows_job(process)
        try:
            if contained:
                # This invokes TerminateJobObject through the installed MCP SDK. The
                # confirmed outer Job contains every descendant even when a nested
                # MCP-server Job cannot be assigned inside the worker.
                await terminate_windows_process_tree(process, 2.0)
        except Exception:
            # Root reaping below is still mandatory if the SDK/Win32 helper fails.
            pass
        finally:
            await _reap_windows_root(process, terminate_first=not contained)
        return
    if worker_process_group is None:
        return
    try:
        os.killpg(worker_process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except (PermissionError, OSError):
        try:
            process.terminate()
        except (ProcessLookupError, OSError):
            pass
    else:
        with anyio.move_on_after(1.0):
            while True:
                try:
                    os.killpg(worker_process_group, 0)
                except ProcessLookupError:
                    break
                await anyio.sleep(0.05)
        try:
            os.killpg(worker_process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    with anyio.move_on_after(1.0):
        await process.wait()


async def _communicate_worker(
    process: Process | FallbackProcess,
    encoded: bytes,
) -> bytes:
    if process.stdin is None or process.stdout is None:
        raise RuntimeError("MCP probe worker pipes were unavailable")
    await process.stdin.send(encoded)
    await process.stdin.aclose()
    chunks: list[bytes] = []
    size = 0
    async for chunk in process.stdout:
        size += len(chunk)
        if size > MCP_PROBE_MAX_OUTPUT_BYTES:
            raise _WorkerOutputTooLarge
        chunks.append(chunk)
    await process.wait()
    return b"".join(chunks)


async def run_probe_worker(payload: dict[str, Any]) -> dict[str, Any]:
    worker = Path(__file__).with_name("mcp_probe_worker.py")
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    process = await _create_worker_process(worker, _safe_parent_environment())
    if _is_windows() and not _has_confirmed_windows_job(process):
        # Fail before stdin is touched: without the outer Job, an inner worker
        # could start descendants that root-only cleanup cannot contain.
        await _complete_cleanup(_terminate_worker(process, None))
        return {"ok": False, "error": MCP_PROBE_CONTAINMENT_ERROR}
    # POSIX workers are session/process-group leaders. Preserve the exact PGID
    # now: once the leader exits, os.getpgid(pid) can no longer recover the
    # group even while an inherited MCP-server descendant is still alive.
    worker_process_group = process.pid if not _is_windows() else None
    try:
        async with asyncio.timeout(MCP_PROBE_WORKER_TIMEOUT_SECONDS):
            stdout = await _communicate_worker(process, encoded)
    except _WorkerOutputTooLarge:
        return {"ok": False, "error": "response_too_large"}
    finally:
        await _complete_cleanup(_terminate_worker(process, worker_process_group))
    try:
        result = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "error": "probe_failed"}
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        return {"ok": False, "error": "probe_failed"}
    if result["ok"] is False:
        error = result.get("error")
        if error not in _ALLOWED_WORKER_ERRORS:
            error = "probe_failed"
        return {"ok": False, "error": error}
    tools = result.get("tools")
    truncated = result.get("truncated")
    if (
        not isinstance(tools, list)
        or len(tools) > MCP_PROBE_MAX_TOOLS
        or not isinstance(truncated, bool)
    ):
        return {"ok": False, "error": "probe_failed"}
    return {"ok": True, "tools": tools, "truncated": truncated}


def _normalized_visible_text(value: str, *, preserve_layout: bool) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character) not in {"Cc", "Cf"}
        or preserve_layout and character in {"\n", "\t"}
    )


def _redact(value: str, secrets_to_redact: set[str]) -> str:
    redacted = _normalized_visible_text(value, preserve_layout=True)
    for secret in sorted(secrets_to_redact, key=len, reverse=True):
        if len(secret) >= MCP_PROBE_SUBSTRING_REDACTION_MIN_LENGTH:
            redacted = redacted.replace(secret, MCP_PROBE_REDACTION_PLACEHOLDER)
    return redacted


def _safe_display_text(value: str, maximum: int) -> str:
    return _normalized_visible_text(value, preserve_layout=True)[:maximum]


def _safe_tools(
    raw_tools: list[Any],
    allowed_tools: list[str],
    secrets_to_redact: set[str],
) -> tuple[list[dict[str, Any]], list[str], list[str], list[str], bool]:
    secrets = {
        normalized
        for secret in secrets_to_redact
        if secret
        and (normalized := _normalized_visible_text(secret, preserve_layout=False))
    }

    def matching_secrets(value: str) -> set[str]:
        normalized = _normalized_visible_text(value, preserve_layout=False)
        return {secret for secret in secrets if secret in normalized}

    def contains_secret(value: str) -> bool:
        return bool(matching_secrets(value))

    counts: dict[str, int] = {}
    normalized_origins: dict[str, set[str]] = {}
    validated: list[tuple[str, str, str | None]] = []
    redacted_description = False
    for raw in raw_tools:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        description = raw.get("description")
        if not isinstance(name, str) or not isinstance(description, str):
            continue
        if contains_secret(name):
            continue
        description_matches = matching_secrets(description)
        redacted_description = redacted_description or bool(description_matches)
        counts[name] = counts.get(name, 0) + 1
        reason: str | None = None
        try:
            validate_mcp_tool_allowlist([name])
        except ValueError:
            reason = "Blocked by CareerPilot's MCP tool policy."
        normalized = normalized_hermes_tool_name(name)
        normalized_origins.setdefault(normalized, set()).add(name)
        safe_description = (
            MCP_PROBE_REDACTION_PLACEHOLDER
            if any(
                len(secret) < MCP_PROBE_SUBSTRING_REDACTION_MIN_LENGTH
                for secret in description_matches
            )
            else _redact(description, description_matches)
        )
        validated.append((name, safe_description, reason))

    allowed = {name for name in allowed_tools if not contains_secret(name)}
    result: list[dict[str, Any]] = []
    policy_available_names: set[str] = set()
    for name, description, reason in validated:
        if counts[name] > 1 or len(normalized_origins[normalized_hermes_tool_name(name)]) > 1:
            reason = "Blocked because discovered names collide after runtime normalization."
        if reason is None:
            policy_available_names.add(name)
        display_name = _safe_display_text(name, 160) or "[unprintable tool name]"
        result.append(
            {
                "name": display_name,
                "description": _safe_display_text(description, 1_000),
                "allowed": name in allowed,
                "selectable": reason is None,
                "policy_reason": reason,
            }
        )
    result.sort(key=lambda item: (str(item["name"]).casefold(), str(item["name"])))
    allowed_present = sorted(allowed & policy_available_names)
    allowed_missing = sorted(allowed - policy_available_names)
    discovered_not_allowed = sorted(
        str(item["name"])
        for item in result
        if item["selectable"] and not item["allowed"]
    )
    claims_suppressed = (
        len(validated) < len(raw_tools)
        or len(allowed) < len(allowed_tools)
        or redacted_description
    )
    return result, allowed_present, allowed_missing, discovered_not_allowed, claims_suppressed


def _failed_result(error: str, latency_ms: int) -> dict[str, Any]:
    if error == "timed_out":
        status = "timed_out"
        message = "The server did not complete the bounded connection check in time."
    elif error in {
        "private_target_blocked",
        "redirect_blocked",
        "target_changed",
        "invalid_target",
        "server_request_blocked",
    }:
        status = "policy_blocked"
        message = "CareerPilot blocked behavior outside the safe discovery boundary."
    elif error == MCP_PROBE_CONTAINMENT_ERROR:
        status = "policy_blocked"
        message = "CareerPilot could not establish the required process containment boundary."
    elif error in {
        "command_not_found",
        "invalid_configuration",
        "missing_environment",
        "secret_in_process_arguments",
    }:
        status = "configuration_issue"
        message = "The saved server configuration is not ready to run."
    elif error in {"unreachable", "connection_closed", "authentication_required"}:
        status = "unreachable"
        message = "CareerPilot could not establish an unauthenticated MCP connection."
    else:
        status = "protocol_error"
        message = "The server did not return a valid bounded MCP discovery response."
    return {
        "executed": True,
        "status": status,
        "message": message,
        "latency_ms": latency_ms,
        "truncated": error == "response_too_large",
        "stale_configuration": False,
        "discovered_tools": [],
        "allowed_present": [],
        "allowed_missing": [],
        "discovered_not_allowed": [],
    }


class MCPProbeLease:
    """A bounded probe slot reserved before an approval is consumed."""

    def __init__(self, manager: MCPProbeManager, account_id: str) -> None:
        self._manager = manager
        self._account_id = account_id
        self._attempted = False
        self._released = False

    async def probe(
        self,
        paths: CompanionPaths,
        server: dict[str, Any],
    ) -> dict[str, Any]:
        if self._released:
            raise RuntimeError("The MCP probe reservation has already been released.")
        if self._attempted:
            raise RuntimeError("The MCP probe reservation has already been used.")
        self._attempted = True
        return await self._manager._probe_reserved(paths, server)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await _complete_cleanup(
            self._manager._release(self._account_id, attempted=self._attempted)
        )

    async def __aenter__(self) -> MCPProbeLease:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.release()


class MCPProbeManager:
    def __init__(self, *, global_limit: int = 2) -> None:
        if global_limit < 1:
            raise ValueError("global_limit must be positive")
        self._state_lock = asyncio.Lock()
        self._global_slots = asyncio.Semaphore(global_limit)
        self._active_accounts: set[str] = set()
        self._last_finished: dict[str, float] = {}

    async def reserve(self, account_id: str) -> MCPProbeLease:
        """Reserve per-account and global capacity without contacting a server."""

        try:
            await asyncio.wait_for(self._global_slots.acquire(), timeout=1.0)
        except TimeoutError as exc:
            raise MCPProbeBusy("MCP connection checks are busy. Try again shortly.") from exc
        reserved = False
        try:
            async with self._state_lock:
                now = time.monotonic()
                if account_id in self._active_accounts:
                    raise MCPProbeBusy("An MCP connection check is already running.")
                previous = self._last_finished.get(account_id)
                if previous is not None and now - previous < MCP_PROBE_COOLDOWN_SECONDS:
                    raise MCPProbeBusy(
                        "Wait briefly before starting another MCP connection check."
                    )
                self._active_accounts.add(account_id)
                reserved = True
            return MCPProbeLease(self, account_id)
        finally:
            if not reserved:
                self._global_slots.release()

    async def _release(self, account_id: str, *, attempted: bool) -> None:
        async with self._state_lock:
            self._active_accounts.discard(account_id)
            if attempted:
                self._last_finished[account_id] = time.monotonic()
        self._global_slots.release()

    async def _probe_reserved(
        self,
        paths: CompanionPaths,
        server: dict[str, Any],
    ) -> dict[str, Any]:
        started = time.monotonic()
        try:
            worker_payload, secrets_to_redact = prepare_worker_payload(server, paths)
        except MCPProbeConfigurationError as exc:
            return _failed_result(exc.code, 0)
        try:
            raw = await run_probe_worker(worker_payload)
        except TimeoutError:
            raw = {"ok": False, "error": "timed_out"}
        except OSError:
            raw = {"ok": False, "error": "unreachable"}
        latency_ms = min(60_000, max(0, round((time.monotonic() - started) * 1_000)))
        if raw.get("ok") is not True:
            return _failed_result(str(raw.get("error")), latency_ms)
        (
            tools,
            allowed_present,
            allowed_missing,
            discovered_not_allowed,
            claims_suppressed,
        ) = _safe_tools(
            raw["tools"],
            server["tool_allowlist"],
            secrets_to_redact,
        )
        return {
            "executed": True,
            "status": "ready" if tools else "ready_no_tools",
            "message": (
                "Connection ready. Some discovery claims were suppressed to protect configured secrets."
                if claims_suppressed
                else "Connection ready. Review discovered tool names before changing the allowlist."
                if tools
                else "Connection ready, but the server reported no tools."
            ),
            "latency_ms": latency_ms,
            "truncated": bool(raw["truncated"]) or claims_suppressed,
            "stale_configuration": False,
            "discovered_tools": tools,
            "allowed_present": allowed_present,
            "allowed_missing": allowed_missing,
            "discovered_not_allowed": discovered_not_allowed,
        }

    async def probe(
        self,
        account_id: str,
        paths: CompanionPaths,
        server: dict[str, Any],
    ) -> dict[str, Any]:
        """Convenience entry point for non-HTTP callers."""

        lease = await self.reserve(account_id)
        try:
            return await lease.probe(paths, server)
        finally:
            await lease.release()


def record_probe_result(
    session: Session,
    server_name: str,
    server_revision: str,
    result: dict[str, Any],
    *,
    stale_configuration: bool,
) -> None:
    status = str(result.get("status"))
    if status not in _PROBE_STATUSES:
        status = "protocol_error"
    record_audit(
        session,
        "mcp.probe.completed",
        subject_type="mcp",
        subject_id=server_name,
        payload={
            "status": status,
            "latency_ms": int(result.get("latency_ms", 0)),
            "discovered_count": len(result.get("discovered_tools", [])),
            "allowed_present_count": len(result.get("allowed_present", [])),
            "allowed_missing_count": len(result.get("allowed_missing", [])),
            "truncated": bool(result.get("truncated", False)),
            "stale_configuration": stale_configuration,
            "server_revision": server_revision,
        },
    )


def latest_probe_summaries(
    session: Session,
    rows: list[MCPServerRecord],
) -> dict[str, dict[str, Any]]:
    revisions = {row.name: server_revision(row) for row in rows}
    events = session.scalars(
        select(AuditEventRecord)
        .where(
            AuditEventRecord.event_type == "mcp.probe.completed",
            AuditEventRecord.subject_type == "mcp",
        )
        .order_by(AuditEventRecord.created_at.desc())
        .limit(1_000)
    ).all()
    summaries: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.subject_id in summaries or event.subject_id not in revisions:
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("stale_configuration") is True:
            continue
        if payload.get("server_revision") != revisions[event.subject_id]:
            continue
        status = payload.get("status")
        latency_ms = payload.get("latency_ms")
        discovered_count = payload.get("discovered_count")
        if (
            status not in _PROBE_STATUSES
            or not isinstance(latency_ms, int)
            or not isinstance(discovered_count, int)
        ):
            continue
        summaries[event.subject_id] = {
            "status": status,
            "checked_at": event.created_at,
            "latency_ms": max(0, min(latency_ms, 60_000)),
            "discovered_count": max(0, min(discovered_count, MCP_PROBE_MAX_TOOLS)),
        }
    return summaries


mcp_probe_manager = MCPProbeManager()


def server_configuration_digest(server: dict[str, Any]) -> str:
    """Internal exact snapshot used only for post-probe stale detection."""

    canonical = json.dumps(
        _runtime_server_payload(server),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
