from __future__ import annotations

import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# Hatch loads custom hooks as standalone modules in an isolated environment.
# Make this source tree importable without declaring the package as a build dependency.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from career_companion.static_ui_release import (
    MANIFEST_NAME,
    StaticUIReleaseError,
    stage_verified_static_ui,
)


class CustomBuildHook(BuildHookInterface):
    """Force only a verified static UI manifest into release distributions."""

    PLUGIN_NAME = "custom"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._staging: tempfile.TemporaryDirectory[str] | None = None

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name == "wheel" and version == "editable":
            return
        if self.target_name not in {"sdist", "wheel"}:
            return

        try:
            self._staging = tempfile.TemporaryDirectory(
                prefix="career-pilot-static-ui-"
            )
            staged = Path(self._staging.name) / "bundle"
            verified = stage_verified_static_ui(
                Path(self.root) / "frontend",
                project_root=Path(self.root),
                destination=staged,
            )
        except StaticUIReleaseError as exc:
            if self._staging is not None:
                self._staging.cleanup()
                self._staging = None
            raise RuntimeError(f"Bundled static UI release policy failed: {exc}") from exc

        destination = (
            PurePosixPath("frontend/out")
            if self.target_name == "sdist"
            else PurePosixPath("career_companion/web")
        )
        force_include = build_data["force_include"]
        for file in verified.files:
            relative = PurePosixPath(file.path)
            source = staged.joinpath(*relative.parts)
            force_include[str(source)] = str(destination / relative)
        force_include[str(staged / MANIFEST_NAME)] = str(destination / MANIFEST_NAME)

    def finalize(
        self,
        version: str,
        build_data: dict[str, Any],
        artifact_path: str,
    ) -> None:
        if self._staging is not None:
            self._staging.cleanup()
            self._staging = None


def get_build_hook() -> type[CustomBuildHook]:
    return CustomBuildHook
