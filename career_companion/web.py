from __future__ import annotations

import os
import tomllib
from pathlib import Path


def frontend_build_directory() -> Path | None:
    """Locate the exported browser application using deterministic precedence.

    A valid explicit operator override wins. In a source checkout, a freshly exported
    ``frontend/out`` wins over the package fallback in ``career_companion/web``.
    Installed distributions normally have no checkout marker, so they continue
    to use their bundled package assets.
    """

    configured = os.getenv("CAREER_COMPANION_WEB_DIR", "").strip()
    package_directory = Path(__file__).resolve().parent
    repository_root = package_directory.parent
    source_frontend = repository_root / "frontend"
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    if _is_source_checkout(repository_root, source_frontend):
        candidates.append(source_frontend / "out")
    candidates.append(package_directory / "web")
    for candidate in candidates:
        resolved = candidate.resolve()
        if (resolved / "index.html").is_file():
            return resolved
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
