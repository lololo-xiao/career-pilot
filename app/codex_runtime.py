import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import httpx

from app.config import get_codex_binary, get_codex_login_timeout_seconds


_CODEX_ENVIRONMENT_ALLOWLIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
_DEFAULT_REQUEST_PARAMS = object()


class CodexRuntimeError(RuntimeError):
    """Base error for Codex runtime failures safe to expose to clients."""


class CodexUnavailableError(CodexRuntimeError):
    pass


class CodexProtocolError(CodexRuntimeError):
    pass


def _build_codex_environment(codex_home: Path) -> dict[str, str]:
    """Build a minimal environment so analysis tools cannot read app secrets."""

    environment = {
        name: os.environ[name]
        for name in _CODEX_ENVIRONMENT_ALLOWLIST
        if name in os.environ
    }
    environment.update(
        {
            "CODEX_HOME": str(codex_home),
            "HOME": str(codex_home),
            "RUST_LOG": "warn",
        }
    )
    return environment


class CodexAppServer:
    """Small JSON-RPC client for one isolated Codex app-server process."""

    def __init__(self, credentials: bytes | None = None) -> None:
        binary = get_codex_binary()
        resolved_binary = shutil.which(binary)
        if resolved_binary is None:
            raise CodexUnavailableError(
                "The Codex runtime is not installed on the CareerPilot server"
            )

        self._temp_directory = tempfile.TemporaryDirectory(
            prefix="careerpilot-codex-"
        )
        self.codex_home = Path(self._temp_directory.name)
        self.workspace = self.codex_home / "workspace"
        self.workspace.mkdir(mode=0o700)
        if credentials is not None:
            auth_path = self.codex_home / "auth.json"
            auth_path.write_bytes(credentials)
            auth_path.chmod(0o600)

        environment = _build_codex_environment(self.codex_home)

        try:
            self._process = subprocess.Popen(  # noqa: S603 - configured trusted binary
                [resolved_binary, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=environment,
            )
        except OSError as exc:
            self._temp_directory.cleanup()
            raise CodexUnavailableError("The Codex runtime could not be started") from exc

        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._next_request_id = 1
        self._responses: dict[int, dict[str, Any]] = {}
        self._notifications: list[dict[str, Any]] = []
        self._stderr_lines: list[str] = []
        self._closed = False

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "careerpilot",
                        "title": "CareerPilot",
                        "version": "0.5.0",
                    }
                },
                timeout=10,
            )
            self.notify("initialized", {})
        except Exception:
            self.close()
            raise

    def _read_stdout(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            return
        for line in stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            with self._condition:
                message_id = message.get("id")
                if isinstance(message_id, int) and "method" not in message:
                    self._responses[message_id] = message
                else:
                    self._notifications.append(message)
                self._condition.notify_all()
        with self._condition:
            self._condition.notify_all()

    def _read_stderr(self) -> None:
        stderr = self._process.stderr
        if stderr is None:
            return
        for line in stderr:
            with self._condition:
                self._stderr_lines.append(line.strip())
                self._stderr_lines = self._stderr_lines[-20:]
                self._condition.notify_all()

    def _send(self, message: dict[str, Any]) -> None:
        stdin = self._process.stdin
        if stdin is None or self._process.poll() is not None:
            raise CodexUnavailableError(self._exit_message())
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._write_lock:
                stdin.write(f"{encoded}\n")
                stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexUnavailableError(self._exit_message()) from exc

    def _exit_message(self) -> str:
        detail = " ".join(self._stderr_lines[-3:]).strip()
        if "app-server" in detail or "unexpected argument" in detail:
            return "The installed Codex runtime does not support app-server"
        return "The Codex runtime stopped unexpectedly"

    def request(
        self,
        method: str,
        params: dict[str, Any] | None | object = _DEFAULT_REQUEST_PARAMS,
        *,
        timeout: float = 30,
    ) -> dict[str, Any]:
        with self._condition:
            request_id = self._next_request_id
            self._next_request_id += 1
        request_params = {} if params is _DEFAULT_REQUEST_PARAMS else params
        self._send({"method": method, "id": request_id, "params": request_params})

        deadline = time.monotonic() + timeout
        with self._condition:
            while request_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexProtocolError(
                        f"Codex did not answer {method} before the timeout"
                    )
                if self._process.poll() is not None:
                    raise CodexUnavailableError(self._exit_message())
                self._condition.wait(timeout=min(remaining, 0.5))
            response = self._responses.pop(request_id)

        error = response.get("error")
        if error is not None:
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise CodexProtocolError(message or f"Codex rejected {method}")
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise CodexProtocolError(f"Codex returned an invalid {method} response")
        return result

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def wait_for_notification(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        cursor: int = 0,
        timeout: float = 30,
    ) -> tuple[dict[str, Any], int]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                while cursor < len(self._notifications):
                    message = self._notifications[cursor]
                    cursor += 1
                    if predicate(message):
                        return message, cursor
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexProtocolError("Codex did not complete before the timeout")
                if self._process.poll() is not None:
                    raise CodexUnavailableError(self._exit_message())
                self._condition.wait(timeout=min(remaining, 0.5))

    def notifications(self) -> list[dict[str, Any]]:
        with self._condition:
            return list(self._notifications)

    def read_credentials(self) -> bytes:
        auth_path = self.codex_home / "auth.json"
        try:
            credentials = auth_path.read_bytes()
        except OSError as exc:
            raise CodexProtocolError(
                "Codex completed login without storing credentials"
            ) from exc
        if not credentials:
            raise CodexProtocolError("Codex stored empty authentication credentials")
        return credentials

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3)
        for stream in (
            self._process.stdin,
            self._process.stdout,
            self._process.stderr,
        ):
            if stream is not None:
                stream.close()
        self._temp_directory.cleanup()

    def __enter__(self) -> "CodexAppServer":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass
class CodexLoginAttempt:
    attempt_id: str
    user_id: str
    login_id: str
    verification_url: str
    user_code: str
    expires_at: int
    server: CodexAppServer
    status: str = "pending"
    email: str | None = None
    plan_type: str | None = None
    credentials: bytes | None = None
    error: str | None = None
    cleanup_timer: threading.Timer | None = None


@dataclass(frozen=True)
class CodexAccountSnapshot:
    models: list[dict[str, Any]]
    rate_limits: dict[str, Any] | None
    account_usage: dict[str, Any] | None
    context_windows: dict[str, int]
    refreshed_credentials: bytes
    warnings: list[str]


def _codex_access_token(credentials: bytes) -> str | None:
    try:
        payload = json.loads(credentials)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    token = tokens.get("access_token") if isinstance(tokens, dict) else None
    return token.strip() if isinstance(token, str) and token.strip() else None


def _read_codex_context_windows(credentials: bytes) -> dict[str, int]:
    """Read the provider's authoritative context windows when available."""

    access_token = _codex_access_token(credentials)
    if access_token is None:
        return {}
    try:
        response = httpx.get(
            "https://chatgpt.com/backend-api/codex/models",
            params={"client_version": "1.0.0"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
            follow_redirects=False,
            trust_env=False,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return {}
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return {}
    windows: dict[str, int] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        model = item.get("slug")
        context_window = item.get("context_window")
        if (
            isinstance(model, str)
            and model.strip()
            and isinstance(context_window, int)
            and context_window > 0
        ):
            windows[model.strip()] = context_window
    return windows


def read_codex_account_snapshot(credentials: bytes) -> CodexAccountSnapshot:
    """Read the current model catalog and ChatGPT account meters from Codex."""

    warnings: list[str] = []
    with CodexAppServer(credentials) as server:
        models: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(10):
            params: dict[str, Any] = {"includeHidden": False, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            result = server.request("model/list", params, timeout=30)
            page = result.get("data")
            if not isinstance(page, list):
                raise CodexProtocolError("Codex returned an invalid model catalog")
            models.extend(item for item in page if isinstance(item, dict))
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor

        rate_limits: dict[str, Any] | None = None
        try:
            rate_limits = server.request(
                "account/rateLimits/read", None, timeout=30
            )
        except CodexRuntimeError:
            warnings.append("Account rate limits are temporarily unavailable")

        account_usage: dict[str, Any] | None = None
        try:
            account_usage = server.request("account/usage/read", None, timeout=30)
        except CodexRuntimeError:
            warnings.append("Account token history is temporarily unavailable")

        refreshed_credentials = server.read_credentials()

    context_windows = _read_codex_context_windows(refreshed_credentials)
    if not context_windows:
        warnings.append("Model context-window sizes are temporarily unavailable")
    return CodexAccountSnapshot(
        models=models,
        rate_limits=rate_limits,
        account_usage=account_usage,
        context_windows=context_windows,
        refreshed_credentials=refreshed_credentials,
        warnings=warnings,
    )


class CodexOAuthManager:
    """Own short-lived device-code login processes until the browser flow completes."""

    def __init__(self, *, max_active_attempts: int = 10) -> None:
        self._attempts: dict[str, CodexLoginAttempt] = {}
        self._lock = threading.Lock()
        self._max_active_attempts = max_active_attempts
        self._starting_attempts = 0

    def _cleanup_expired(self) -> None:
        now = int(time.time())
        expired = [
            attempt_id
            for attempt_id, attempt in self._attempts.items()
            if attempt.expires_at <= now
        ]
        for attempt_id in expired:
            attempt = self._attempts.pop(attempt_id)
            if attempt.cleanup_timer is not None:
                attempt.cleanup_timer.cancel()
            attempt.server.close()

    def start(self, user_id: str) -> CodexLoginAttempt:
        with self._lock:
            self._cleanup_expired()
            if (
                len(self._attempts) + self._starting_attempts
                >= self._max_active_attempts
            ):
                raise CodexUnavailableError(
                    "Too many ChatGPT sign-in attempts are active. Try again shortly."
                )
            self._starting_attempts += 1

        server: CodexAppServer | None = None
        try:
            server = CodexAppServer()
            try:
                result = server.request(
                    "account/login/start",
                    {"type": "chatgptDeviceCode"},
                    timeout=30,
                )
                login_id = str(result["loginId"])
                verification_url = str(result["verificationUrl"])
                user_code = str(result["userCode"])
            except (KeyError, TypeError, CodexRuntimeError) as exc:
                server.close()
                if isinstance(exc, CodexRuntimeError):
                    raise
                raise CodexProtocolError(
                    "Codex returned an invalid device login response"
                ) from exc

            timeout = get_codex_login_timeout_seconds()
            attempt = CodexLoginAttempt(
                attempt_id=str(uuid.uuid4()),
                user_id=user_id,
                login_id=login_id,
                verification_url=verification_url,
                user_code=user_code,
                expires_at=int(time.time()) + timeout,
                server=server,
            )
        except Exception:
            if server is not None:
                server.close()
            with self._lock:
                self._starting_attempts -= 1
            raise

        with self._lock:
            self._starting_attempts -= 1
            self._attempts[attempt.attempt_id] = attempt
        cleanup_timer = threading.Timer(timeout, self.finish, (attempt.attempt_id,))
        cleanup_timer.daemon = True
        attempt.cleanup_timer = cleanup_timer
        cleanup_timer.start()
        return attempt

    def get(self, attempt_id: str) -> CodexLoginAttempt | None:
        """Return an attempt without advancing the device-login state."""

        with self._lock:
            self._cleanup_expired()
            return self._attempts.get(attempt_id)

    def poll(self, attempt_id: str) -> CodexLoginAttempt | None:
        with self._lock:
            self._cleanup_expired()
            attempt = self._attempts.get(attempt_id)
            if attempt is None or attempt.status != "pending":
                return attempt

            completed = next(
                (
                    message
                    for message in attempt.server.notifications()
                    if message.get("method") == "account/login/completed"
                    and message.get("params", {}).get("loginId") == attempt.login_id
                ),
                None,
            )
            if completed is None:
                return attempt

            params = completed.get("params", {})
            if not params.get("success"):
                attempt.status = "failed"
                attempt.error = str(params.get("error") or "ChatGPT login failed")
                attempt.server.close()
                return attempt

            try:
                result = attempt.server.request(
                    "account/read", {"refreshToken": True}, timeout=30
                )
                account = result.get("account")
                if not isinstance(account, dict) or account.get("type") != "chatgpt":
                    raise CodexProtocolError("Codex did not return a ChatGPT account")
                attempt.email = (
                    str(account["email"])
                    if account.get("email") is not None
                    else None
                )
                attempt.plan_type = (
                    str(account["planType"])
                    if account.get("planType") is not None
                    else None
                )
                attempt.credentials = attempt.server.read_credentials()
                attempt.status = "completed"
            except CodexRuntimeError as exc:
                attempt.status = "failed"
                attempt.error = str(exc)
            finally:
                attempt.server.close()
            return attempt

    def finish(self, attempt_id: str) -> None:
        with self._lock:
            attempt = self._attempts.pop(attempt_id, None)
        if attempt is not None:
            if attempt.cleanup_timer is not None:
                attempt.cleanup_timer.cancel()
            attempt.server.close()

    def cancel(self, attempt_id: str) -> None:
        with self._lock:
            attempt = self._attempts.pop(attempt_id, None)
        if attempt is None:
            return
        if attempt.cleanup_timer is not None:
            attempt.cleanup_timer.cancel()
        if attempt.status == "pending":
            try:
                attempt.server.request(
                    "account/login/cancel",
                    {"loginId": attempt.login_id},
                    timeout=5,
                )
            except CodexRuntimeError:
                pass
        attempt.server.close()


@lru_cache(maxsize=1)
def get_codex_oauth_manager() -> CodexOAuthManager:
    return CodexOAuthManager()


def run_codex_structured_turn(
    credentials: bytes,
    prompt: str,
    output_schema: dict[str, Any],
    *,
    developer_instructions: str | None = None,
    timeout: float = 150,
) -> tuple[str, bytes]:
    """Run one isolated, read-only Codex turn and return JSON plus refreshed auth."""

    with CodexAppServer(credentials) as server:
        thread_params: dict[str, Any] = {
            "cwd": str(server.workspace),
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "ephemeral": True,
            "serviceName": "careerpilot",
        }
        if developer_instructions:
            thread_params["developerInstructions"] = developer_instructions
        thread_result = server.request("thread/start", thread_params, timeout=30)
        thread = thread_result.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise CodexProtocolError("Codex returned no thread identifier")
        thread_id = thread["id"]

        turn_result = server.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly"},
                "effort": "medium",
                "summary": "concise",
                "outputSchema": output_schema,
            },
            timeout=30,
        )
        turn = turn_result.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise CodexProtocolError("Codex returned no turn identifier")
        turn_id = turn["id"]

        final_text: str | None = None
        cursor = 0
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexProtocolError("Codex analysis timed out")
            message, cursor = server.wait_for_notification(
                lambda item: item.get("method")
                in {"item/completed", "turn/completed"},
                cursor=cursor,
                timeout=remaining,
            )
            params = message.get("params", {})
            if message.get("method") == "item/completed":
                item = params.get("item")
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        final_text = text
                continue

            completed_turn = params.get("turn")
            if not isinstance(completed_turn, dict) or completed_turn.get("id") != turn_id:
                continue
            if completed_turn.get("status") != "completed":
                error = completed_turn.get("error")
                detail = error.get("message") if isinstance(error, dict) else error
                raise CodexProtocolError(
                    str(detail or "Codex did not complete the analysis")
                )
            break

        if final_text is None:
            raise CodexProtocolError("Codex returned no structured match report")
        return final_text, server.read_credentials()
