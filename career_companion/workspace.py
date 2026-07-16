from __future__ import annotations

import shutil
from importlib.resources import files
from pathlib import Path

from career_companion.paths import CompanionPaths


def initialize_account_workspace(paths: CompanionPaths) -> None:
    """Create one account workspace without overwriting user-owned templates."""

    paths.create()
    source = Path(str(files("career_companion").joinpath("templates")))
    destination = paths.workspace / "templates"
    if source.is_dir() and not destination.exists():
        shutil.copytree(source, destination)
