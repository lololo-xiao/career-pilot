from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# Hatch loads custom hooks as standalone modules in an isolated environment.
# Make this source tree importable without declaring the package as a build dependency.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from career_companion.static_ui_release import (
    MANIFEST_NAME,
    StaticUIReleaseError,
    verify_static_ui,
)


class CustomBuildHook(BuildHookInterface):
    """Force only a verified static UI manifest into release distributions."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name == "wheel" and version == "editable":
            return
        if self.target_name not in {"sdist", "wheel"}:
            return

        try:
            verified = verify_static_ui(
                Path(self.root) / "frontend",
                project_root=Path(self.root),
            )
        except StaticUIReleaseError as exc:
            raise RuntimeError(f"Bundled static UI release policy failed: {exc}") from exc

        destination = (
            PurePosixPath("frontend/out")
            if self.target_name == "sdist"
            else PurePosixPath("career_companion/web")
        )
        force_include = build_data["force_include"]
        for file in verified.files:
            relative = PurePosixPath(file.path)
            source = verified.export.joinpath(*relative.parts)
            force_include[str(source)] = str(destination / relative)
        force_include[str(verified.manifest)] = str(destination / MANIFEST_NAME)


def get_build_hook() -> type[CustomBuildHook]:
    return CustomBuildHook
