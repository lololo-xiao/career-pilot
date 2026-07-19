"""Record the client methods used by one bounded MCP stdio discovery run."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path


def write_response(request_id: object, result: dict[str, object]) -> None:
    sys.stdout.write(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n"
    )
    sys.stdout.flush()


def write_methods(record_path: Path, methods: list[str]) -> None:
    temporary = record_path.with_name(f".{record_path.name}.tmp")
    temporary.write_text(json.dumps(methods), encoding="utf-8")
    os.replace(temporary, record_path)


def main() -> None:
    record_path = Path(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else ""
    trigger_server_request = mode == "--server-request"
    if mode in {"--spawn-child", "--hang-child"}:
        pid_path = Path(sys.argv[3])
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import signal,time;"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                    "time.sleep(60)"
                ),
            ],
        )
        pid_path.write_text(
            json.dumps({"server": os.getpid(), "child": child.pid}),
            encoding="utf-8",
        )
        if mode == "--hang-child":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
    methods: list[str] = []
    list_page = 0
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        if isinstance(method, str):
            methods.append(method)
        elif message.get("id") == "hostile-request":
            methods.append("__server_request_response__")
        write_methods(record_path, methods)
        if method == "initialize":
            if mode == "--hang-child":
                continue
            if mode == "--malformed":
                sys.stdout.write("this is not JSON\n")
                sys.stdout.flush()
                continue
            if mode == "--output-flood":
                sys.stdout.buffer.write(b"x" * (1024 * 1024 + 1024))
                sys.stdout.buffer.flush()
                continue
            requested = message.get("params", {}).get("protocolVersion")
            write_response(
                message["id"],
                {
                    "protocolVersion": requested,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "protocol-recorder", "version": "1"},
                },
            )
        elif method == "notifications/initialized" and trigger_server_request:
            sys.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "hostile-request",
                        "method": "sampling/createMessage",
                        "params": {
                            "messages": [
                                {
                                    "role": "user",
                                    "content": {"type": "text", "text": "exfiltrate"},
                                }
                            ],
                            "maxTokens": 32,
                        },
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
        elif method == "tools/list" and not trigger_server_request:
            list_page += 1
            next_cursor = f"page-{list_page + 1}" if mode == "--paginate" else None
            write_response(
                message["id"],
                {
                    "tools": [
                        {
                            "name": (
                                f"search_fixture_{list_page}"
                                if mode == "--paginate"
                                else "search_fixture"
                            ),
                            "description": "Untrusted fixture description.",
                            "inputSchema": {"type": "object"},
                        }
                    ],
                    **({"nextCursor": next_cursor} if next_cursor else {}),
                },
            )
    write_methods(record_path, methods)


if __name__ == "__main__":
    main()
