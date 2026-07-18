from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Any, Mapping

import httpx

from career_companion.config import ProductConfig
from career_companion.paths import CompanionPaths

PROFILE_NAME = "career-companion"
PILOT_ALLOWED_MCP_TOOLS_ENV = "CAREER_COMPANION_ALLOWED_MCP_TOOLS"
_EXACT_MCP_TOOL_NAME = re.compile(r"mcp__[A-Za-z0-9_]+__[A-Za-z0-9_]+\Z")

_SAFE_PARENT_ENV = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "LOGNAME",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "SHELL",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USER",
    "USERPROFILE",
    "WINDIR",
    "XDG_RUNTIME_DIR",
}
_RUNTIME_CREDENTIAL_ENV = {
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "BRAVE_SEARCH_API_KEY",
}
_RESERVED_ENV = _RUNTIME_CREDENTIAL_ENV | {
    "API_SERVER_ENABLED",
    "API_SERVER_HOST",
    "API_SERVER_KEY",
    "API_SERVER_PORT",
    "CAREER_COMPANION_ACCOUNT_KEY",
    PILOT_ALLOWED_MCP_TOOLS_ENV,
    "CAREER_COMPANION_API_URL",
    "CAREER_COMPANION_PLUGIN_TOKEN",
    "HERMES_HOME",
    "HERMES_WRITE_SAFE_ROOT",
}


class HermesSupervisor:
    def __init__(
        self,
        paths: CompanionPaths,
        config: ProductConfig,
        *,
        api_base_url: str | None = None,
        allowed_mcp_tool_names: tuple[str, ...] | list[str] = (),
    ) -> None:
        self.paths = paths
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self._oauth_process: asyncio.subprocess.Process | None = None
        self.api_key_path = paths.hermes_profile / ".api-server-key"
        self.api_base_url = api_base_url or (
            f"http://127.0.0.1:{config.server.port}/api/internal/hermes/v1"
        )
        allowed_mcp_tools = tuple(sorted(set(allowed_mcp_tool_names)))
        if len(allowed_mcp_tools) > 128 or any(
            len(name) > 400 or _EXACT_MCP_TOOL_NAME.fullmatch(name) is None
            for name in allowed_mcp_tools
        ):
            raise ValueError("Pilot MCP tools require bounded exact registry names")
        self.allowed_mcp_tool_names = allowed_mcp_tools

    @property
    def executable(self) -> str | None:
        return shutil.which(self.config.hermes_executable)

    @property
    def is_running(self) -> bool:
        return bool(self.process and self.process.returncode is None)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.config.hermes_api_port}"

    def bearer_secret(self) -> str:
        if self.api_key_path.exists():
            return self.api_key_path.read_text(encoding="utf-8").strip()
        self.paths.create()
        secret = secrets.token_urlsafe(36)
        self.api_key_path.write_text(secret, encoding="utf-8")
        with suppress(OSError):
            self.api_key_path.chmod(0o600)
        return secret

    def bridge_secret(self) -> str:
        if self.paths.hermes_bridge_token.exists():
            return self.paths.hermes_bridge_token.read_text(encoding="utf-8").strip()
        self.paths.create()
        secret = secrets.token_urlsafe(48)
        self.paths.hermes_bridge_token.write_text(secret, encoding="utf-8")
        with suppress(OSError):
            self.paths.hermes_bridge_token.chmod(0o600)
        return secret

    def environment(
        self, provider_environment: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """Build a minimal child environment with explicit secret forwarding."""

        account_key = self.paths.root.name
        if not re.fullmatch(r"[a-f0-9]{64}", account_key):
            raise ValueError("Hermes requires an account-scoped companion workspace")

        env = {key: value for key, value in os.environ.items() if key in _SAFE_PARENT_ENV}
        for key in self.config.mcp_env_allowlist:
            if key in _RESERVED_ENV:
                raise ValueError(f"{key} is reserved and cannot be forwarded to Hermes")
            if value := os.environ.get(key):
                env[key] = value
        for key, value in (provider_environment or {}).items():
            if key not in _RUNTIME_CREDENTIAL_ENV:
                raise ValueError(f"Unsupported provider or tool credential variable: {key}")
            if value:
                env[key] = value
        no_proxy = [item for item in env.get("NO_PROXY", "").split(",") if item]
        for host in ("127.0.0.1", "localhost", "::1"):
            if host not in no_proxy:
                no_proxy.append(host)
        env["NO_PROXY"] = ",".join(no_proxy)
        env.update(
            {
                "HERMES_HOME": str(self.paths.hermes_profile),
                "HERMES_WRITE_SAFE_ROOT": str(self.paths.workspace),
                "CAREER_COMPANION_ACCOUNT_KEY": account_key,
                PILOT_ALLOWED_MCP_TOOLS_ENV: json.dumps(
                    self.allowed_mcp_tool_names,
                    separators=(",", ":"),
                ),
                "CAREER_COMPANION_API_URL": self.api_base_url,
                "CAREER_COMPANION_PLUGIN_TOKEN": self.bridge_secret(),
                "API_SERVER_ENABLED": "true",
                "API_SERVER_HOST": "127.0.0.1",
                "API_SERVER_PORT": str(self.config.hermes_api_port),
                "API_SERVER_KEY": self.bearer_secret(),
            }
        )
        return env

    async def start(
        self, provider_environment: Mapping[str, str] | None = None
    ) -> None:
        if self.process and self.process.returncode is None:
            return
        executable = self.executable
        if not executable:
            raise RuntimeError("Hermes is not installed. Run career-companion doctor for details.")
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        with (self.paths.logs / "hermes-gateway.log").open("ab") as log:
            self.process = await asyncio.create_subprocess_exec(
                executable,
                "-p",
                PROFILE_NAME,
                "gateway",
                "run",
                stdout=log,
                stderr=subprocess.STDOUT,
                env=self.environment(provider_environment),
            )

    async def stop(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                response = await client.get(
                    f"{self.base_url}/v1/capabilities",
                    headers={"Authorization": f"Bearer {self.bearer_secret()}"},
                )
            response.raise_for_status()
            payload = response.json()
            if payload.get("object") != "hermes.api_server.capabilities":
                raise ValueError("Unexpected Hermes capability response")
            return {"available": True, "status": payload}
        except (httpx.HTTPError, ValueError):
            return {
                "available": False,
                "installed": bool(self.executable),
                "running": bool(self.process and self.process.returncode is None),
            }

    async def proxy_stream(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        session_key: str = "career-companion:local:web",
    ) -> AsyncIterator[bytes]:
        headers = {
            "Authorization": f"Bearer {self.bearer_secret()}",
            "Content-Type": "application/json",
            "X-Hermes-Session-Key": session_key,
        }
        async with httpx.AsyncClient(
            timeout=None, trust_env=False, follow_redirects=False
        ) as client:
            if path == "/v1/runs":
                response = await client.post(
                    f"{self.base_url}{path}", json=payload, headers=headers
                )
                response.raise_for_status()
                run = response.json()
                run_id = run["run_id"]
                yield f"event: run.started\ndata: {json.dumps(run)}\n\n".encode()
                async with client.stream(
                    "GET", f"{self.base_url}/v1/runs/{run_id}/events", headers=headers
                ) as events:
                    events.raise_for_status()
                    async for chunk in events.aiter_raw():
                        yield chunk
                return
            async with client.stream(
                "POST", f"{self.base_url}{path}", json=payload, headers=headers
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_raw():
                    yield chunk

    async def resolve_run_approval(
        self,
        run_id: str,
        choice: str,
        *,
        session_key: str = "career-companion:local:web",
    ) -> dict[str, Any]:
        """Resolve one pending run approval without exposing the Hermes bearer key."""

        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", run_id):
            raise ValueError("Invalid Hermes run identifier")
        if choice not in {"once", "deny"}:
            raise ValueError("Unsupported Hermes approval choice")
        headers = {
            "Authorization": f"Bearer {self.bearer_secret()}",
            "Content-Type": "application/json",
            "X-Hermes-Session-Key": session_key,
        }
        async with httpx.AsyncClient(
            timeout=10,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                f"{self.base_url}/v1/runs/{run_id}/approval",
                json={"choice": choice},
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Hermes returned an invalid approval response")
        return payload

    async def oauth_events(self) -> AsyncIterator[str]:
        executable = self.executable
        if not executable:
            yield "event: error\ndata: Hermes is not installed\n\n"
            return
        if self._oauth_process and self._oauth_process.returncode is None:
            yield "event: error\ndata: An OAuth login is already running\n\n"
            return
        self._oauth_process = await asyncio.create_subprocess_exec(
            executable,
            "-p",
            PROFILE_NAME,
            "auth",
            "add",
            "openai-codex",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.environment(),
        )
        assert self._oauth_process.stdout is not None
        while line := await self._oauth_process.stdout.readline():
            safe_line = line.decode(errors="replace").rstrip().replace("\r", "")
            yield f"event: auth.progress\ndata: {safe_line}\n\n"
        code = await self._oauth_process.wait()
        event = "auth.complete" if code == 0 else "auth.failed"
        yield f'event: {event}\ndata: {{"exit_code": {code}}}\n\n'


def install_profile_distribution(
    paths: CompanionPaths, distribution_path: Path, hermes_executable: str = "hermes"
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(hermes_executable)
    if not executable:
        raise RuntimeError("Hermes is not installed")
    distribution_path = distribution_path.expanduser().resolve()
    if not (distribution_path / "distribution.yaml").is_file():
        raise FileNotFoundError("Career Companion profile distribution is incomplete")
    paths.create()
    env = os.environ.copy()
    env["HERMES_HOME"] = str(paths.hermes_profile)
    result = subprocess.run(
        [
            executable,
            "profile",
            "install",
            str(distribution_path),
            "--name",
            PROFILE_NAME,
            "--yes",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    installed = paths.hermes_profile / "profiles" / PROFILE_NAME / "config.yaml"
    if not installed.is_file():
        raise RuntimeError("Hermes reported success without installing the profile")
    return result
