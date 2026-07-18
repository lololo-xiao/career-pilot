from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST_NAME = ".careerpilot-static-ui.json"
MANIFEST_VERSION = 1
INDEX_MARKER = "CareerPilot"
BUILD_CONTRACT = {
    "CAREERPILOT_STATIC_EXPORT": "true",
    "NEXT_PUBLIC_API_BASE_URL": "",
}

_IGNORED_SOURCE_DIRECTORIES = frozenset({".next", "node_modules", "out", "test"})
_IGNORED_GENERATED_SOURCE_FILES = frozenset({"next-env.d.ts"})
_JUNK_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
_JUNK_DIRECTORIES = frozenset({"__MACOSX"})


class StaticUIReleaseError(RuntimeError):
    """Raised when the static UI does not satisfy the release artifact contract."""


@dataclass(frozen=True)
class StaticUIFile:
    path: str
    sha256: str
    size: int

    def as_json(self) -> dict[str, str | int]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class VerifiedStaticUI:
    frontend: Path
    export: Path
    manifest: Path
    source_fingerprint: str
    export_fingerprint: str
    index_sha256: str
    files: tuple[StaticUIFile, ...]


def build_static_ui(frontend: Path, *, npm_executable: str = "npm") -> VerifiedStaticUI:
    """Create a static export and bind it to the unchanged frontend source tree."""

    frontend = frontend.resolve()
    source_before = frontend_source_fingerprint(frontend)
    environment = os.environ.copy()
    environment.update(BUILD_CONTRACT)
    try:
        subprocess.run(
            [npm_executable, "run", "build"],
            cwd=frontend,
            env=environment,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise StaticUIReleaseError(
            "The frontend static export failed; install locked Node dependencies and retry."
        ) from exc
    return write_static_ui_manifest(
        frontend,
        expected_source_fingerprint=source_before,
    )


def write_static_ui_manifest(
    frontend: Path,
    *,
    expected_source_fingerprint: str | None = None,
) -> VerifiedStaticUI:
    """Fingerprint a completed export for deterministic sdist/wheel inclusion."""

    frontend = frontend.resolve()
    export = frontend / "out"
    source_fingerprint = frontend_source_fingerprint(frontend)
    if (
        expected_source_fingerprint is not None
        and source_fingerprint != expected_source_fingerprint
    ):
        raise StaticUIReleaseError(
            "Frontend sources changed while the static export was building; rebuild it."
        )
    files = _export_files(export)
    index = export / "index.html"
    index_sha256 = _validate_index(index)
    export_fingerprint = _fingerprint(files)
    payload = {
        "build_contract": BUILD_CONTRACT,
        "export_fingerprint": export_fingerprint,
        "files": [file.as_json() for file in files],
        "format_version": MANIFEST_VERSION,
        "index_marker": INDEX_MARKER,
        "index_sha256": index_sha256,
        "source_fingerprint": source_fingerprint,
    }
    manifest = export / MANIFEST_NAME
    temporary = export / f"{MANIFEST_NAME}.tmp"
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, manifest)
    return verify_static_ui(frontend)


def verify_static_ui(frontend: Path) -> VerifiedStaticUI:
    """Verify source freshness, exported file hashes, and the stable index marker."""

    frontend = frontend.resolve()
    export = frontend / "out"
    if not export.is_dir():
        raise StaticUIReleaseError(
            "Static UI export is missing at frontend/out; run the release UI build first."
        )
    manifest = export / MANIFEST_NAME
    if not manifest.is_file() or manifest.is_symlink():
        raise StaticUIReleaseError(
            f"Static UI manifest {MANIFEST_NAME} is missing; run the release UI build first."
        )
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StaticUIReleaseError("Static UI manifest is unreadable or invalid JSON.") from exc
    _validate_manifest_header(payload)
    expected_files = _manifest_files(payload.get("files"))
    actual_files = _export_files(export)
    if actual_files != expected_files:
        raise StaticUIReleaseError(
            "Static UI export is stale: its file list or content differs from the manifest."
        )

    source_fingerprint = frontend_source_fingerprint(frontend)
    if payload.get("source_fingerprint") != source_fingerprint:
        raise StaticUIReleaseError(
            "Static UI export is stale: frontend sources changed after it was built."
        )
    export_fingerprint = _fingerprint(actual_files)
    if payload.get("export_fingerprint") != export_fingerprint:
        raise StaticUIReleaseError("Static UI export fingerprint does not match the manifest.")
    index_sha256 = _validate_index(export / "index.html")
    if payload.get("index_sha256") != index_sha256:
        raise StaticUIReleaseError("Static UI index fingerprint does not match the manifest.")
    return VerifiedStaticUI(
        frontend=frontend,
        export=export,
        manifest=manifest,
        source_fingerprint=source_fingerprint,
        export_fingerprint=export_fingerprint,
        index_sha256=index_sha256,
        files=actual_files,
    )


def frontend_source_fingerprint(frontend: Path) -> str:
    frontend = frontend.resolve()
    if not frontend.is_dir():
        raise StaticUIReleaseError(f"Frontend source directory is missing: {frontend}")
    files = _collect_files(
        frontend,
        ignored_top_level_directories=_IGNORED_SOURCE_DIRECTORIES,
        ignore_source_files=True,
    )
    required = {"package.json", "package-lock.json", "next.config.ts", "app/page.tsx"}
    present = {file.path for file in files}
    missing = sorted(required - present)
    if missing:
        raise StaticUIReleaseError(
            f"Frontend source inputs are incomplete: missing {', '.join(missing)}"
        )
    return _fingerprint(files)


def _export_files(export: Path) -> tuple[StaticUIFile, ...]:
    if not export.is_dir():
        raise StaticUIReleaseError(
            "Static UI export is missing at frontend/out; run the release UI build first."
        )
    files = _collect_files(export, ignored_names={MANIFEST_NAME, f"{MANIFEST_NAME}.tmp"})
    if not files:
        raise StaticUIReleaseError("Static UI export is empty.")
    return files


def _collect_files(
    root: Path,
    *,
    ignored_top_level_directories: frozenset[str] = frozenset(),
    ignored_names: set[str] | frozenset[str] = frozenset(),
    ignore_source_files: bool = False,
) -> tuple[StaticUIFile, ...]:
    if root.is_symlink():
        raise StaticUIReleaseError(f"Release input cannot be a symbolic link: {root}")
    pending = [root]
    paths: list[Path] = []
    try:
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    relative = path.relative_to(root)
                    if entry.is_symlink():
                        raise StaticUIReleaseError(
                            f"Release inputs cannot contain symbolic links: {relative}"
                        )
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in _JUNK_DIRECTORIES:
                            continue
                        if (
                            len(relative.parts) == 1
                            and entry.name in ignored_top_level_directories
                        ):
                            continue
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        if entry.name in _JUNK_NAMES or entry.name in ignored_names:
                            continue
                        if ignore_source_files:
                            # Next rewrites this generated type shim during builds.
                            if relative.as_posix() in _IGNORED_GENERATED_SOURCE_FILES:
                                continue
                            if entry.name.startswith(".env"):
                                continue
                        paths.append(path)
                    else:
                        raise StaticUIReleaseError(
                            f"Release inputs must be regular files or directories: {relative}"
                        )
    except OSError as exc:
        raise StaticUIReleaseError(f"Could not safely inspect release input: {root}") from exc

    files = [
        StaticUIFile(
            path=path.relative_to(root).as_posix(),
            sha256=_sha256_file(path),
            size=path.stat().st_size,
        )
        for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix())
    ]
    return tuple(files)


def _manifest_files(value: Any) -> tuple[StaticUIFile, ...]:
    if not isinstance(value, list):
        raise StaticUIReleaseError("Static UI manifest files must be a list.")
    files: list[StaticUIFile] = []
    for item in value:
        if not isinstance(item, dict):
            raise StaticUIReleaseError("Static UI manifest contains an invalid file entry.")
        path = item.get("path")
        digest = item.get("sha256")
        size = item.get("size")
        if not isinstance(path, str) or not _safe_manifest_path(path):
            raise StaticUIReleaseError("Static UI manifest contains an unsafe file path.")
        if not isinstance(digest, str) or len(digest) != 64:
            raise StaticUIReleaseError("Static UI manifest contains an invalid file hash.")
        if not isinstance(size, int) or size < 0:
            raise StaticUIReleaseError("Static UI manifest contains an invalid file size.")
        files.append(StaticUIFile(path=path, sha256=digest, size=size))
    result = tuple(sorted(files, key=lambda file: file.path))
    if len({file.path for file in result}) != len(result):
        raise StaticUIReleaseError("Static UI manifest contains duplicate file paths.")
    return result


def _safe_manifest_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and "\\" not in value
        and not path.is_absolute()
        and ".." not in path.parts
        and value == path.as_posix()
        and value != MANIFEST_NAME
    )


def _validate_manifest_header(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise StaticUIReleaseError("Static UI manifest root must be an object.")
    if payload.get("format_version") != MANIFEST_VERSION:
        raise StaticUIReleaseError("Static UI manifest format version is unsupported.")
    if payload.get("build_contract") != BUILD_CONTRACT:
        raise StaticUIReleaseError("Static UI manifest build contract is invalid.")
    if payload.get("index_marker") != INDEX_MARKER:
        raise StaticUIReleaseError("Static UI manifest index marker is invalid.")


def _validate_index(index: Path) -> str:
    if not index.is_file() or index.is_symlink():
        raise StaticUIReleaseError("Static UI export must contain a regular index.html file.")
    payload = index.read_bytes()
    if INDEX_MARKER.encode("utf-8") not in payload:
        raise StaticUIReleaseError(
            f"Static UI index.html must contain the {INDEX_MARKER!r} release marker."
        )
    return hashlib.sha256(payload).hexdigest()


def _fingerprint(files: tuple[StaticUIFile, ...]) -> str:
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(file.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
