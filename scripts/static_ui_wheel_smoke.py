"""HTTP-smoke an installed wheel's bundled static UI using only the stdlib."""

from __future__ import annotations

import argparse
import functools
import shutil
import sys
import threading
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from career_companion.static_ui_release import (
    MAX_STATIC_UI_HTML_FILES,
    MAX_STATIC_UI_REFERENCED_ASSETS,
    static_ui_frontend_asset_paths,
    static_ui_manifest_asset_paths,
    verify_bundled_static_ui,
)
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

    verified = verify_bundled_static_ui(
        package_ui,
        expected_export_fingerprint=args.expected_fingerprint,
    )
    build_id = verified.build_id
    html_paths = tuple(
        file.path
        for file in verified.files
        if Path(file.path).suffix.casefold() == ".html"
    )
    if not html_paths or len(html_paths) > MAX_STATIC_UI_HTML_FILES:
        raise SystemExit("Installed UI has an invalid bounded HTML route inventory")

    handler = functools.partial(_QuietHandler, directory=str(package_ui))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        referenced_assets: set[str] = set()
        manifest_paths: tuple[str, str] | None = None
        for html_path in html_paths:
            request_path = "/" if html_path == "index.html" else "/" + quote(
                html_path,
                safe="/",
            )
            with urllib.request.urlopen(  # noqa: S310 - fixed loopback test server
                base_url + request_path,
                timeout=10,
            ) as response:
                page = response.read()
            if response.status != 200 or not page:
                raise SystemExit(
                    f"Bundled UI HTML route is invalid over HTTP: {html_path}"
                )
            if html_path == "index.html":
                if b"CareerPilot" not in page or build_id.encode("ascii") not in page:
                    raise SystemExit(
                        "Bundled UI index lacks its release/build-ID markers"
                    )
                manifest_paths = static_ui_manifest_asset_paths(page, build_id)
            referenced_assets.update(
                static_ui_frontend_asset_paths(
                    page,
                    document_path=html_path,
                )
            )
        if manifest_paths is None:
            raise SystemExit("Bundled UI index route was not HTTP-smoked")
        if len(referenced_assets) > MAX_STATIC_UI_REFERENCED_ASSETS:
            raise SystemExit("Bundled UI has too many referenced assets to HTTP-smoke")

        expected_tokens = dict(
            zip(
                manifest_paths,
                (b"__BUILD_MANIFEST", b"__SSG_MANIFEST"),
                strict=True,
            )
        )
        for asset_path in sorted(referenced_assets):
            with urllib.request.urlopen(  # noqa: S310 - fixed loopback test server
                f"{base_url}/{quote(asset_path, safe='/')}",
                timeout=10,
            ) as response:
                asset = response.read()
            if response.status != 200 or not asset:
                raise SystemExit(
                    f"Bundled referenced frontend asset is invalid: {asset_path}"
                )
            expected_token = expected_tokens.get(asset_path)
            if expected_token is not None and (
                expected_token not in asset or len(asset) < 24
            ):
                raise SystemExit(
                    f"Bundled Next manifest asset is invalid: {asset_path}"
                )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
