"""Build or verify the deterministic static UI bundled into Python releases.

Release workflow:
  1. Install locked frontend dependencies with ``npm ci --prefix frontend``.
  2. Run ``python -m scripts.static_ui_release build``.
  3. Run ``uv build --offline`` once Hatchling is available in the uv cache.

The build command runs the same-origin Next.js export, fingerprints its source and
output without timestamps, and writes a manifest under ``frontend/out``. Hatch
then refuses missing or stale exports and force-includes only manifested files.
Node.js is a release-build dependency, never a wheel runtime dependency.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from career_companion.static_ui_release import (
    StaticUIReleaseError,
    build_static_ui,
    verify_static_ui,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--frontend",
        type=Path,
        default=REPOSITORY_ROOT / "frontend",
        help="Frontend source directory (default: repository frontend/)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser(
        "build",
        help="Run the static Next.js export and write its deterministic manifest",
    )
    build.add_argument("--npm", default="npm", help="npm executable (default: npm)")
    subparsers.add_parser(
        "verify",
        help="Verify source freshness and every manifested export file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_static_ui(args.frontend, npm_executable=args.npm)
        else:
            result = verify_static_ui(args.frontend)
    except StaticUIReleaseError as exc:
        raise SystemExit(f"Static UI release error: {exc}") from exc
    print(f"Static UI source fingerprint: {result.source_fingerprint}")
    print(f"Static UI export fingerprint: {result.export_fingerprint}")
    print(f"Static UI index sha256: {result.index_sha256}")
    print(f"Verified export files: {len(result.files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
