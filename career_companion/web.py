from __future__ import annotations

import os
import tomllib
from pathlib import Path

from career_companion.static_ui_release import StaticUIReleaseError, verify_static_ui


def frontend_build_directory() -> Path | None:
    """Locate the exported browser application using deterministic precedence.

    A valid explicit operator override wins. In a source checkout, only a source
    export whose manifest, files, and source fingerprint verify can win over the
    package fallback in ``career_companion/web``. Installed distributions normally
    have no checkout marker, so they use their bundled package assets.
    """

    configured = os.getenv("CAREER_COMPANION_WEB_DIR", "").strip()
    package_directory = Path(__file__).resolve().parent
    repository_root = package_directory.parent
    source_frontend = repository_root / "frontend"
    if configured:
        override = Path(configured).expanduser().resolve()
        if (override / "index.html").is_file():
            return override
    if _is_source_checkout(repository_root, source_frontend):
        try:
            verified = verify_static_ui(
                source_frontend,
                project_root=repository_root,
            )
        except StaticUIReleaseError:
            pass
        else:
            return verified.export
    bundled = (package_directory / "web").resolve()
    if (bundled / "index.html").is_file():
        return bundled
    return None


def _is_source_checkout(repository_root: Path, source_frontend: Path) -> bool:
    project_file = repository_root / "pyproject.toml"
    if not project_file.is_file() or not (source_frontend / "package.json").is_file():
        return False
    try:
        project = tomllib.loads(project_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    return project.get("project", {}).get("name") == "career-pilot"
