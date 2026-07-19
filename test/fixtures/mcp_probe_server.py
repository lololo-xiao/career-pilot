"""Tiny read-only MCP server used by the bounded discovery tests."""

import argparse
import os
import sys

from mcp.server.fastmcp import FastMCP


server = FastMCP("CareerPilot probe fixture")
sentinel = os.environ.get("MCP_PROBE_TEST_SENTINEL", "")


@server.tool(
    description=(
        f"Search a deterministic local fixture without side effects. {sentinel}"
        if sentinel
        else "Search a deterministic local fixture without side effects."
    )
)
def search_fixture(query: str) -> str:
    return query


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--http-port", type=int)
    arguments = parser.parse_args()
    if os.environ.get("MCP_PROBE_TEST_EMIT_STDERR"):
        print(sentinel, file=sys.stderr, flush=True)
    if arguments.http_port is None:
        server.run(transport="stdio")
    else:
        server.settings.host = "127.0.0.1"
        server.settings.port = arguments.http_port
        server.settings.stateless_http = True
        server.settings.json_response = True
        server.run(transport="streamable-http")
