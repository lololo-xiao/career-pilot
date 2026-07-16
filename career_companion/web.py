from __future__ import annotations

import os
from pathlib import Path


def frontend_build_directory() -> Path | None:
    """Locate the exported, credential-free browser application."""

    configured = os.getenv("CAREER_COMPANION_WEB_DIR", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            Path(__file__).resolve().parent / "web",
            Path(__file__).resolve().parents[1] / "frontend" / "out",
        ]
    )
    for candidate in candidates:
        resolved = candidate.resolve()
        if (resolved / "index.html").is_file():
            return resolved
    return None
