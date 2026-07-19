"""Isolated, bounded MCP initialize/tools-list probe.

The worker uses the public MCP SDK for session semantics, but wraps its stdio
and HTTP transports so untrusted servers remain subject to hard byte, time,
network-target, and process-tree bounds. It never imports Hermes, loads a
profile, performs OAuth, or calls an MCP tool.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import anyio
import httpcore
import httpx
from anyio.abc import Process
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.streamable_http import streamable_http_client
from mcp.os.win32.utilities import (
    FallbackProcess,
    create_windows_process,
    get_windows_executable_command,
    terminate_windows_process_tree,
)
from mcp.shared.message import SessionMessage
from mcp.shared.exceptions import McpError


MAX_INPUT_BYTES = 256 * 1024
MAX_PROTOCOL_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_FRAME_BYTES = 1024 * 1024
MAX_TOOLS = 128
MAX_TOOL_NAME = 160
MAX_DESCRIPTION = 1_000
MAX_PAGES = 4
PROBE_TIMEOUT_SECONDS = 10.0
IO_TIMEOUT_SECONDS = 5.0
PROCESS_TERMINATION_TIMEOUT = 2.0


class ProbeFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class ByteBudget:
    remaining: int = MAX_PROTOCOL_BYTES

    def consume(self, amount: int) -> None:
        self.remaining -= amount
        if self.remaining < 0:
            raise ProbeFailure("response_too_large")


@dataclass
class OutboundBoundary:
    phase: int = 0
    tool_list_requests: int = 0

    def authorize(self, message: types.JSONRPCMessage) -> None:
        root = message.root
        method = getattr(root, "method", None)
        if (
            self.phase == 0
            and isinstance(root, types.JSONRPCRequest)
            and method == "initialize"
        ):
            self.phase = 1
            return
        if (
            self.phase == 1
            and isinstance(root, types.JSONRPCNotification)
            and method == "notifications/initialized"
        ):
            self.phase = 2
            return
        if (
            self.phase >= 2
            and isinstance(root, types.JSONRPCRequest)
            and method == "tools/list"
            and self.tool_list_requests < MAX_PAGES
        ):
            self.phase = 3
            self.tool_list_requests += 1
            return
        # This also blocks SDK-generated replies to sampling, elicitation,
        # roots, ping, or any other server-initiated request.
        raise ProbeFailure("server_request_blocked")


def _root_probe_failure(exc: BaseException) -> ProbeFailure | None:
    if isinstance(exc, ProbeFailure):
        return exc
    if isinstance(exc, McpError):
        if exc.error.code == httpx.codes.REQUEST_TIMEOUT:
            return ProbeFailure("timed_out")
        if exc.error.code == types.CONNECTION_CLOSED:
            return ProbeFailure("connection_closed")
        return ProbeFailure("protocol_error")
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, tuple):
        for child in nested:
            if isinstance(child, BaseException):
                found = _root_probe_failure(child)
                if found is not None:
                    return found
    cause = exc.__cause__
    if isinstance(cause, BaseException):
        return _root_probe_failure(cause)
    return None


async def _create_stdio_process(
    parameters: StdioServerParameters,
) -> Process | FallbackProcess:
    if os.name == "nt":  # pragma: no cover - exercised by native Windows CI
        command = get_windows_executable_command(parameters.command)
        # The MCP SDK creates a Windows Job Object with
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE and assigns the process to it.
        return await create_windows_process(
            command,
            parameters.args,
            parameters.env,
            subprocess.DEVNULL,
            parameters.cwd,
        )
    return await anyio.open_process(
        [parameters.command, *parameters.args],
        env=parameters.env,
        stderr=subprocess.DEVNULL,
        cwd=parameters.cwd,
    )


async def _terminate_stdio_process(process: Process | FallbackProcess) -> None:
    if os.name == "nt":  # pragma: no cover - exercised by native Windows CI
        await terminate_windows_process_tree(process, PROCESS_TERMINATION_TIMEOUT)
        return
    # The server deliberately inherits the isolated worker's process group so
    # the API-side supervisor can atomically clean up every descendant. Do not
    # use killpg here: that would also kill the supervising worker.
    try:
        process.terminate()
    except (ProcessLookupError, OSError):
        pass
    with anyio.move_on_after(0.5):
        await process.wait()
    if process.returncode is None:
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass
        with anyio.move_on_after(0.5):
            await process.wait()


@asynccontextmanager
async def bounded_stdio_client(
    parameters: StdioServerParameters,
    budget: ByteBudget,
) -> AsyncIterator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    """MCP SDK stdio transport with a pre-parse byte/frame cap."""

    read_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_reader = anyio.create_memory_object_stream[SessionMessage](0)
    outbound = OutboundBoundary()
    try:
        process = await _create_stdio_process(parameters)
    except FileNotFoundError as exc:
        raise ProbeFailure("command_not_found") from exc
    except OSError as exc:
        raise ProbeFailure("unreachable") from exc

    async def stdout_reader() -> None:
        assert process.stdout is not None
        buffer = b""
        try:
            async with read_writer:
                async for chunk in process.stdout:
                    budget.consume(len(chunk))
                    buffer += chunk
                    if len(buffer) > MAX_FRAME_BYTES and b"\n" not in buffer:
                        raise ProbeFailure("response_too_large")
                    lines = buffer.split(b"\n")
                    buffer = lines.pop()
                    for line in lines:
                        if not line:
                            continue
                        if len(line) > MAX_FRAME_BYTES:
                            raise ProbeFailure("response_too_large")
                        try:
                            message = types.JSONRPCMessage.model_validate_json(line)
                        except Exception as exc:
                            raise ProbeFailure("protocol_error") from exc
                        await read_writer.send(SessionMessage(message))
                if buffer:
                    raise ProbeFailure("protocol_error")
        except BaseException as exc:
            if not isinstance(exc, anyio.get_cancelled_exc_class()):
                failure = _root_probe_failure(exc) or ProbeFailure("protocol_error")
                try:
                    await read_writer.send(failure)
                except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                    pass
                raise failure

    async def stdin_writer() -> None:
        assert process.stdin is not None
        try:
            async with write_reader:
                async for session_message in write_reader:
                    outbound.authorize(session_message.message)
                    payload = session_message.message.model_dump_json(
                        by_alias=True,
                        exclude_none=True,
                    ).encode("utf-8") + b"\n"
                    await process.stdin.send(payload)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(stdout_reader)
        task_group.start_soon(stdin_writer)
        try:
            yield read_stream, write_stream
        finally:
            if process.stdin is not None:
                try:
                    await process.stdin.aclose()
                except Exception:
                    pass
            await _terminate_stdio_process(process)
            task_group.cancel_scope.cancel()
            await read_stream.aclose()
            await write_stream.aclose()
            await read_writer.aclose()
            await write_reader.aclose()


class ValidatingNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve, classify, and pin every TCP connection before it is opened."""

    def __init__(self, expected_host: str, *, loopback: bool) -> None:
        self.expected_host = expected_host.casefold().rstrip(".")
        self.loopback = loopback
        self._delegate = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        normalized = host.decode() if isinstance(host, bytes) else host
        if normalized.casefold().rstrip(".") != self.expected_host:
            raise ProbeFailure("target_changed")
        try:
            answers = await asyncio.to_thread(
                socket.getaddrinfo,
                normalized,
                port,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise ProbeFailure("unreachable") from exc
        addresses = sorted({answer[4][0] for answer in answers})
        if not addresses:
            raise ProbeFailure("unreachable")
        parsed = [ipaddress.ip_address(address) for address in addresses]
        if self.loopback:
            if any(not address.is_loopback for address in parsed):
                raise ProbeFailure("private_target_blocked")
        elif any(not address.is_global for address in parsed):
            raise ProbeFailure("private_target_blocked")
        selected = sorted(parsed, key=lambda item: (item.version != 4, str(item)))[0]
        return await self._delegate.connect_tcp(
            str(selected),
            port,
            timeout,
            local_address,
            socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        raise ProbeFailure("unsupported_transport")

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


class BoundedCoreStream(httpx.AsyncByteStream):
    def __init__(self, stream: Any, budget: ByteBudget) -> None:
        self.stream = stream
        self.budget = budget

    async def __aiter__(self):
        async for chunk in self.stream:
            self.budget.consume(len(chunk))
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


class PinnedMCPTransport(httpx.AsyncBaseTransport):
    """One-target transport: no proxies, redirects, cookies, or DNS rebinding."""

    def __init__(self, url: str, budget: ByteBudget) -> None:
        self.expected_url = httpx.URL(url)
        hostname = self.expected_url.host
        loopback = hostname.casefold() in {"127.0.0.1", "localhost", "::1"}
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            proxy=None,
            max_connections=1,
            max_keepalive_connections=1,
            http1=True,
            http2=False,
            retries=0,
            network_backend=ValidatingNetworkBackend(hostname, loopback=loopback),
        )
        self.budget = budget
        self.outbound = OutboundBoundary()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url != self.expected_url:
            raise ProbeFailure("target_changed")
        if request.method != "POST":
            # The SDK may try an optional SSE GET after initialization. Return a
            # local 405 so no method beyond the three disclosed messages reaches
            # the server.
            return httpx.Response(405, request=request)
        try:
            body = await request.aread()
            message = types.JSONRPCMessage.model_validate_json(body)
        except ProbeFailure:
            raise
        except Exception as exc:
            raise ProbeFailure("protocol_error") from exc
        self.outbound.authorize(message)
        headers = [
            (name, value)
            for name, value in request.headers.raw
            if name.lower() not in {b"cookie", b"proxy-authorization", b"accept-encoding"}
        ]
        headers.append((b"Accept-Encoding", b"identity"))
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=headers,
            content=body,
            extensions=request.extensions,
        )
        try:
            response = await self.pool.handle_async_request(core_request)
        except ProbeFailure:
            raise
        except httpcore.TimeoutException as exc:
            raise ProbeFailure("timed_out") from exc
        except (httpcore.NetworkError, OSError) as exc:
            raise ProbeFailure("unreachable") from exc
        if 300 <= response.status < 400:
            await response.aclose()
            raise ProbeFailure("redirect_blocked")
        content_encoding = next(
            (
                value.lower()
                for name, value in response.headers
                if name.lower() == b"content-encoding"
            ),
            b"identity",
        )
        if content_encoding not in {b"", b"identity"}:
            await response.aclose()
            raise ProbeFailure("response_too_large")
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=BoundedCoreStream(response.stream, self.budget),
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self.pool.aclose()


def _append_tools(result: Any, tools: list[dict[str, str]]) -> tuple[str | None, bool]:
    raw_tools = getattr(result, "tools", None)
    if not isinstance(raw_tools, list):
        raise ProbeFailure("protocol_error")
    truncated = False
    for tool in raw_tools:
        if len(tools) >= MAX_TOOLS:
            truncated = True
            break
        name = getattr(tool, "name", None)
        description = getattr(tool, "description", "") or ""
        if not isinstance(name, str) or not isinstance(description, str):
            raise ProbeFailure("protocol_error")
        if not name or len(name) > MAX_TOOL_NAME:
            truncated = True
            continue
        tools.append({"name": name, "description": description[:MAX_DESCRIPTION]})
    cursor = getattr(result, "nextCursor", None)
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 1_024):
        raise ProbeFailure("protocol_error")
    return cursor, truncated


async def _discover_tools(
    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception],
    write_stream: MemoryObjectSendStream[SessionMessage],
) -> dict[str, Any]:
    tools: list[dict[str, str]] = []
    truncated = False
    try:
        async with ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=IO_TIMEOUT_SECONDS),
            client_info=types.Implementation(name="career-pilot-probe", version="1"),
        ) as session:
            initialized = await session.initialize()
            protocol_version = getattr(initialized, "protocolVersion", None)
            if not isinstance(protocol_version, str):
                raise ProbeFailure("unsupported_protocol")
            cursor: str | None = None
            for _ in range(MAX_PAGES):
                result = await session.list_tools(cursor=cursor)
                cursor, page_truncated = _append_tools(result, tools)
                truncated = truncated or page_truncated
                if not cursor or len(tools) >= MAX_TOOLS:
                    truncated = truncated or bool(cursor)
                    break
            else:
                truncated = truncated or bool(cursor)
    except McpError as exc:
        if exc.error.code == httpx.codes.REQUEST_TIMEOUT:
            raise ProbeFailure("timed_out") from exc
        if exc.error.code == types.CONNECTION_CLOSED:
            raise ProbeFailure("connection_closed") from exc
        raise ProbeFailure("protocol_error") from exc
    return {"ok": True, "tools": tools, "truncated": truncated}


async def _probe_stdio(config: dict[str, Any], budget: ByteBudget) -> dict[str, Any]:
    command = config.get("command")
    args = config.get("args")
    environment = config.get("environment")
    cwd = config.get("cwd")
    if (
        not isinstance(command, str)
        or not command
        or not isinstance(args, list)
        or any(not isinstance(item, str) for item in args)
        or not isinstance(environment, dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in environment.items()
        )
        or not isinstance(cwd, str)
        or not Path(cwd).is_dir()
    ):
        raise ProbeFailure("invalid_configuration")
    parameters = StdioServerParameters(
        command=command,
        args=args,
        env=environment,
        cwd=cwd,
    )
    async with bounded_stdio_client(parameters, budget) as (read_stream, write_stream):
        return await _discover_tools(read_stream, write_stream)


async def _probe_http(config: dict[str, Any], budget: ByteBudget) -> dict[str, Any]:
    url = config.get("url")
    if not isinstance(url, str):
        raise ProbeFailure("invalid_configuration")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ProbeFailure("invalid_target")
    loopback = parsed.hostname.casefold() in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme == "http" and not loopback:
        raise ProbeFailure("private_target_blocked")

    transport = PinnedMCPTransport(url, budget)
    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(IO_TIMEOUT_SECONDS),
    ) as client:
        async with streamable_http_client(
            url,
            http_client=client,
            terminate_on_close=False,
        ) as (read_stream, write_stream, _):
            return await _discover_tools(read_stream, write_stream)


async def _probe(config: dict[str, Any]) -> dict[str, Any]:
    budget = ByteBudget()
    async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
        if config.get("transport") == "stdio":
            return await _probe_stdio(config, budget)
        if config.get("transport") == "http":
            return await _probe_http(config, budget)
        raise ProbeFailure("unsupported_transport")


def _safe_output(payload: dict[str, Any]) -> bytes:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        return b'{"ok":false,"error":"response_too_large"}'
    return encoded


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        sys.stdout.buffer.write(_safe_output({"ok": False, "error": "invalid_configuration"}))
        return 0
    try:
        config = json.loads(raw)
        if not isinstance(config, dict):
            raise ProbeFailure("invalid_configuration")
        result = asyncio.run(_probe(config))
    except TimeoutError:
        result = {"ok": False, "error": "timed_out"}
    except BaseException as exc:
        failure = _root_probe_failure(exc)
        result = {"ok": False, "error": failure.code if failure else "probe_failed"}
    sys.stdout.buffer.write(_safe_output(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
