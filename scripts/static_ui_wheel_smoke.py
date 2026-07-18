"""HTTP-smoke an installed wheel's bundled static UI using only the stdlib."""

from __future__ import annotations

import argparse
import functools
import json
import shutil
import sys
import threading
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from career_companion.static_ui_release import MANIFEST_NAME
from career_companion.web import frontend_build_directory


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-fingerprint", required=True)
    parser.add_argument("--require-no-node", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    package_ui = frontend_build_directory()
    if package_ui is None:
        raise SystemExit("Installed wheel did not expose a bundled static UI")
    package_ui = package_ui.resolve()
    if not package_ui.is_relative_to(Path(sys.prefix).resolve()):
        raise SystemExit(f"Static UI was not loaded from the installed wheel: {package_ui}")
    if args.require_no_node and shutil.which("node") is not None:
        raise SystemExit("Node unexpectedly remained available to the runtime smoke")

    manifest = json.loads((package_ui / MANIFEST_NAME).read_text(encoding="utf-8"))
    if manifest.get("export_fingerprint") != args.expected_fingerprint:
        raise SystemExit("Installed UI fingerprint differs from the release export")
    build_id = manifest.get("build_id")
    if not isinstance(build_id, str):
        raise SystemExit("Installed UI manifest does not carry its build ID attestation")

    handler = functools.partial(_QuietHandler, directory=str(package_ui))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed loopback test server
            f"http://127.0.0.1:{server.server_port}/",
            timeout=10,
        ) as response:
            page = response.read()
        if response.status != 200:
            raise SystemExit(f"Bundled UI returned HTTP {response.status}")
        if b"CareerPilot" not in page or build_id.encode("ascii") not in page:
            raise SystemExit("Bundled UI index lacks its release/build-ID markers")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
