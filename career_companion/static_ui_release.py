from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MANIFEST_NAME = ".careerpilot-static-ui.json"
BUILD_ID_ATTESTATION_NAME = ".careerpilot-build-id"
MANIFEST_VERSION = 2
INDEX_MARKER = "CareerPilot"
BUILD_CONTRACT = {
    "CAREERPILOT_STATIC_EXPORT": "true",
    "NEXT_PUBLIC_API_BASE_URL": "",
    "NEXT_TELEMETRY_DISABLED": "1",
}

_IGNORED_SOURCE_DIRECTORIES = frozenset({".next", "node_modules", "out", "test"})
_IGNORED_GENERATED_SOURCE_FILES = frozenset({"next-env.d.ts"})
_JUNK_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
_JUNK_DIRECTORIES = frozenset({"__MACOSX"})
_BUILD_ID_PATTERN = re.compile(rb"careerpilot-[0-9A-Fa-f]{64}")
_BUILD_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)


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
    build_id: str
    export_fingerprint: str
    index_sha256: str
    files: tuple[StaticUIFile, ...]


@dataclass(frozen=True)
class _ReleaseBoundary:
    project: Path
    project_resolved: Path
    frontend: Path
    frontend_resolved: Path


def build_static_ui(
    frontend: Path,
    *,
    project_root: Path,
    npm_executable: str = "npm",
) -> VerifiedStaticUI:
    """Create a static export and bind it to the unchanged frontend source tree."""

    boundary = _release_boundary(frontend, project_root=project_root)
    source_before = _frontend_source_fingerprint(boundary)
    build_id = _expected_build_id(source_before)
    npm_launcher = shutil.which(npm_executable)
    if npm_launcher is None:
        raise StaticUIReleaseError(
            f"npm executable was not found in the parent environment: {npm_executable}"
        )
    npm_launcher = os.path.abspath(npm_launcher)
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _BUILD_ENVIRONMENT_ALLOWLIST
    }
    environment.update(_effective_build_contract(source_before))
    try:
        subprocess.run(
            [npm_launcher, "run", "build"],
            cwd=boundary.frontend,
            env=environment,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise StaticUIReleaseError(
            "The frontend static export failed; install locked Node dependencies and retry."
        ) from exc
    _attest_next_build(boundary, build_id)
    return write_static_ui_manifest(
        boundary.frontend,
        expected_source_fingerprint=source_before,
        project_root=project_root,
    )


def write_static_ui_manifest(
    frontend: Path,
    *,
    project_root: Path,
    expected_source_fingerprint: str | None = None,
) -> VerifiedStaticUI:
    """Fingerprint a completed export for deterministic sdist/wheel inclusion."""

    boundary = _release_boundary(frontend, project_root=project_root)
    export = boundary.frontend / "out"
    source_fingerprint = _frontend_source_fingerprint(boundary)
    if (
        expected_source_fingerprint is not None
        and source_fingerprint != expected_source_fingerprint
    ):
        raise StaticUIReleaseError(
            "Frontend sources changed while the static export was building; rebuild it."
        )
    build_id = _validate_export_build_id(boundary, source_fingerprint)
    files = _export_files(boundary)
    index = export / "index.html"
    index_sha256 = _validate_index(index, boundary, build_id)
    export_fingerprint = _fingerprint(files)
    payload = {
        "build_contract": _effective_build_contract(source_fingerprint),
        "build_id": build_id,
        "export_fingerprint": export_fingerprint,
        "files": [file.as_json() for file in files],
        "format_version": MANIFEST_VERSION,
        "index_marker": INDEX_MARKER,
        "index_sha256": index_sha256,
        "source_fingerprint": source_fingerprint,
    }
    manifest = _assert_safe_path(export / MANIFEST_NAME, boundary)
    temporary = _assert_safe_path(export / f"{MANIFEST_NAME}.tmp", boundary)
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, manifest)
    return verify_static_ui(boundary.frontend, project_root=project_root)


def verify_static_ui(
    frontend: Path,
    *,
    project_root: Path,
) -> VerifiedStaticUI:
    """Verify source freshness, exported file hashes, and the stable index marker."""

    boundary = _release_boundary(frontend, project_root=project_root)
    source_fingerprint = _frontend_source_fingerprint(boundary)
    export = _assert_safe_path(boundary.frontend / "out", boundary)
    if not export.is_dir():
        raise StaticUIReleaseError(
            "Static UI export is missing at frontend/out; run the release UI build first."
        )
    manifest = _assert_safe_path(export / MANIFEST_NAME, boundary)
    if not manifest.is_file() or _path_is_alias(manifest):
        raise StaticUIReleaseError(
            f"Static UI manifest {MANIFEST_NAME} is missing; run the release UI build first."
        )
    try:
        payload = json.loads(_read_safe_bytes(manifest, boundary).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StaticUIReleaseError("Static UI manifest is unreadable or invalid JSON.") from exc
    _validate_manifest_header(payload, source_fingerprint)
    build_id = _validate_export_build_id(boundary, source_fingerprint)
    expected_files = _manifest_files(payload.get("files"))
    actual_files = _export_files(boundary)
    if actual_files != expected_files:
        raise StaticUIReleaseError(
            "Static UI export is stale: its file list or content differs from the manifest."
        )

    export_fingerprint = _fingerprint(actual_files)
    if payload.get("export_fingerprint") != export_fingerprint:
        raise StaticUIReleaseError("Static UI export fingerprint does not match the manifest.")
    index_sha256 = _validate_index(export / "index.html", boundary, build_id)
    if payload.get("index_sha256") != index_sha256:
        raise StaticUIReleaseError("Static UI index fingerprint does not match the manifest.")
    return VerifiedStaticUI(
        frontend=boundary.frontend,
        export=export,
        manifest=manifest,
        source_fingerprint=source_fingerprint,
        build_id=build_id,
        export_fingerprint=export_fingerprint,
        index_sha256=index_sha256,
        files=actual_files,
    )


def stage_verified_static_ui(
    frontend: Path,
    *,
    project_root: Path,
    destination: Path,
) -> VerifiedStaticUI:
    """Copy one verified bundle into an empty, private packaging directory."""

    verified = verify_static_ui(frontend, project_root=project_root)
    boundary = _release_boundary(verified.frontend, project_root=project_root)
    destination = Path(os.path.abspath(os.fspath(destination)))
    if destination.exists():
        raise StaticUIReleaseError(
            f"Static UI staging destination must not already exist: {destination}"
        )
    destination.mkdir(parents=True)

    for file in verified.files:
        relative = PurePosixPath(file.path)
        source = _assert_safe_regular_file(
            verified.export.joinpath(*relative.parts),
            boundary,
        )
        payload = _read_safe_bytes(source, boundary)
        if len(payload) != file.size or hashlib.sha256(payload).hexdigest() != file.sha256:
            raise StaticUIReleaseError(
                f"Static UI export changed while staging the release: {file.path}"
            )
        target = destination.joinpath(*relative.parts)
        if not target.is_relative_to(destination):
            raise StaticUIReleaseError(
                f"Static UI manifest contains an unsafe staging path: {file.path}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    manifest_payload = _read_safe_bytes(verified.manifest, boundary)
    try:
        staged_manifest = json.loads(manifest_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StaticUIReleaseError(
            "Static UI manifest changed while staging the release."
        ) from exc
    _validate_manifest_header(staged_manifest, verified.source_fingerprint)
    if (
        _manifest_files(staged_manifest.get("files")) != verified.files
        or staged_manifest.get("export_fingerprint") != verified.export_fingerprint
        or staged_manifest.get("index_sha256") != verified.index_sha256
    ):
        raise StaticUIReleaseError(
            "Static UI manifest changed while staging the release."
        )
    (destination / MANIFEST_NAME).write_bytes(manifest_payload)
    return verified


def frontend_source_fingerprint(
    frontend: Path,
    *,
    project_root: Path,
) -> str:
    boundary = _release_boundary(frontend, project_root=project_root)
    return _frontend_source_fingerprint(boundary)


def _frontend_source_fingerprint(boundary: _ReleaseBoundary) -> str:
    if not boundary.frontend.is_dir():
        raise StaticUIReleaseError(
            f"Frontend source directory is missing: {boundary.frontend}"
        )
    _reject_dotenv_files(boundary)
    files = _collect_files(
        boundary.frontend,
        boundary,
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


def _release_boundary(
    frontend: Path,
    *,
    project_root: Path,
) -> _ReleaseBoundary:
    supplied = Path(frontend).expanduser()
    absolute = Path(os.path.abspath(os.fspath(supplied)))
    expected = Path(os.path.abspath(os.fspath(Path(project_root).expanduser())))
    if not absolute.is_relative_to(expected):
        raise StaticUIReleaseError(
            f"Frontend release root escapes the expected project root: {absolute}"
        )
    _reject_alias_components(absolute, expected)
    expected_resolved = _resolve_path(expected)
    absolute_resolved = _resolve_path(absolute)
    if not absolute_resolved.is_relative_to(expected_resolved):
        raise StaticUIReleaseError(
            f"Frontend release root resolves outside the expected project root: {absolute}"
        )
    return _ReleaseBoundary(
        project=expected,
        project_resolved=expected_resolved,
        frontend=absolute,
        frontend_resolved=absolute_resolved,
    )


def _resolve_path(path: Path) -> Path:
    """Indirection for testing Windows junction-style resolved escapes."""

    return path.resolve()


def _path_is_alias(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise StaticUIReleaseError(f"Could not inspect release path metadata: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            if is_junction():
                return True
        except OSError as exc:
            raise StaticUIReleaseError(f"Could not inspect release path alias: {path}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _reject_alias_components(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise StaticUIReleaseError(f"Release path escapes its lexical root: {path}") from exc
    current = root
    if _path_is_alias(current):
        raise StaticUIReleaseError(f"Release paths cannot contain aliases: {current}")
    for part in relative.parts:
        current /= part
        if _path_is_alias(current):
            raise StaticUIReleaseError(f"Release paths cannot contain aliases: {current}")


def _assert_safe_path(
    path: Path,
    boundary: _ReleaseBoundary,
    *,
    lexical_root: Path | None = None,
) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    required_root = lexical_root or boundary.frontend
    if not absolute.is_relative_to(required_root):
        raise StaticUIReleaseError(f"Release path escapes its lexical root: {absolute}")
    if not absolute.is_relative_to(boundary.frontend):
        raise StaticUIReleaseError(f"Release path escapes the frontend root: {absolute}")
    _reject_alias_components(absolute, boundary.frontend)
    resolved = _resolve_path(absolute)
    if not resolved.is_relative_to(boundary.frontend_resolved):
        raise StaticUIReleaseError(
            f"Release path resolves outside the frontend root: {absolute}"
        )
    if not resolved.is_relative_to(boundary.project_resolved):
        raise StaticUIReleaseError(
            f"Release path resolves outside the project root: {absolute}"
        )
    return absolute


def _assert_safe_regular_file(path: Path, boundary: _ReleaseBoundary) -> Path:
    safe = _assert_safe_path(path, boundary)
    if not safe.is_file() or _path_is_alias(safe):
        raise StaticUIReleaseError(f"Release input must be a regular file: {safe}")
    return safe


def _read_safe_bytes(path: Path, boundary: _ReleaseBoundary) -> bytes:
    safe = _assert_safe_regular_file(path, boundary)
    try:
        return safe.read_bytes()
    except OSError as exc:
        raise StaticUIReleaseError(f"Could not safely read release input: {safe}") from exc


def _reject_dotenv_files(boundary: _ReleaseBoundary) -> None:
    """Reject inputs Next could load implicitly without following nested links."""

    pending = [boundary.frontend]
    try:
        while pending:
            directory = _assert_safe_path(pending.pop(), boundary)
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = _assert_safe_path(Path(entry.path), boundary)
                    relative = path.relative_to(boundary.frontend)
                    folded_name = entry.name.casefold()
                    if folded_name.startswith(".env") and not folded_name.endswith(
                        ".example"
                    ):
                        raise StaticUIReleaseError(
                            "Frontend release inputs cannot contain non-example dotenv "
                            f"files: {relative}"
                        )
                    if entry.is_dir(follow_symlinks=False):
                        if (
                            len(relative.parts) == 1
                            and entry.name in _IGNORED_SOURCE_DIRECTORIES
                        ):
                            continue
                        pending.append(path)
                    elif not entry.is_file(follow_symlinks=False):
                        raise StaticUIReleaseError(
                            f"Release inputs must be regular files or directories: {relative}"
                        )
    except OSError as exc:
        raise StaticUIReleaseError(
            f"Could not safely inspect frontend dotenv inputs: {boundary.frontend}"
        ) from exc


def _export_files(boundary: _ReleaseBoundary) -> tuple[StaticUIFile, ...]:
    export = _assert_safe_path(boundary.frontend / "out", boundary)
    if not export.is_dir():
        raise StaticUIReleaseError(
            "Static UI export is missing at frontend/out; run the release UI build first."
        )
    files = _collect_files(
        export,
        boundary,
        ignored_names={MANIFEST_NAME, f"{MANIFEST_NAME}.tmp"},
    )
    if not files:
        raise StaticUIReleaseError("Static UI export is empty.")
    return files


def _collect_files(
    root: Path,
    boundary: _ReleaseBoundary,
    *,
    ignored_top_level_directories: frozenset[str] = frozenset(),
    ignored_names: set[str] | frozenset[str] = frozenset(),
    ignore_source_files: bool = False,
) -> tuple[StaticUIFile, ...]:
    root = _assert_safe_path(root, boundary)
    pending = [root]
    paths: list[Path] = []
    try:
        while pending:
            directory = _assert_safe_path(pending.pop(), boundary, lexical_root=root)
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = _assert_safe_path(
                        Path(entry.path),
                        boundary,
                        lexical_root=root,
                    )
                    relative = path.relative_to(root)
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
                        folded_name = entry.name.casefold()
                        if folded_name.startswith(".env") and not folded_name.endswith(
                            ".example"
                        ):
                            raise StaticUIReleaseError(
                                "Frontend release inputs cannot contain non-example "
                                f"dotenv files: {relative}"
                            )
                        if entry.name in _JUNK_NAMES or entry.name in ignored_names:
                            continue
                        if ignore_source_files:
                            # Next rewrites this generated type shim during builds.
                            if relative.as_posix() in _IGNORED_GENERATED_SOURCE_FILES:
                                continue
                        paths.append(path)
                    else:
                        raise StaticUIReleaseError(
                            f"Release inputs must be regular files or directories: {relative}"
                        )
    except OSError as exc:
        raise StaticUIReleaseError(f"Could not safely inspect release input: {root}") from exc

    files = []
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        safe = _assert_safe_regular_file(path, boundary)
        payload = _read_safe_bytes(safe, boundary)
        files.append(
            StaticUIFile(
                path=safe.relative_to(root).as_posix(),
                sha256=hashlib.sha256(payload).hexdigest(),
                size=len(payload),
            )
        )
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


def _validate_manifest_header(payload: Any, source_fingerprint: str) -> None:
    if not isinstance(payload, dict):
        raise StaticUIReleaseError("Static UI manifest root must be an object.")
    if payload.get("format_version") != MANIFEST_VERSION:
        raise StaticUIReleaseError("Static UI manifest format version is unsupported.")
    manifest_source = payload.get("source_fingerprint")
    if manifest_source != source_fingerprint:
        raise StaticUIReleaseError(
            "Static UI export is stale: frontend sources changed after it was built."
        )
    expected_build_id = _expected_build_id(source_fingerprint)
    if payload.get("build_contract") != _effective_build_contract(source_fingerprint):
        raise StaticUIReleaseError("Static UI manifest build contract is invalid.")
    if payload.get("build_id") != expected_build_id:
        raise StaticUIReleaseError(
            "Static UI manifest does not attest the expected source-derived build ID."
        )
    if payload.get("index_marker") != INDEX_MARKER:
        raise StaticUIReleaseError("Static UI manifest index marker is invalid.")


def _validate_index(
    index: Path,
    boundary: _ReleaseBoundary,
    expected_build_id: str,
) -> str:
    payload = _read_safe_bytes(index, boundary)
    if INDEX_MARKER.encode("utf-8") not in payload:
        raise StaticUIReleaseError(
            f"Static UI index.html must contain the {INDEX_MARKER!r} release marker."
        )
    if expected_build_id.encode("utf-8") not in payload:
        raise StaticUIReleaseError(
            "Static UI index.html does not reference the expected source-derived build ID."
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


def _effective_build_contract(source_fingerprint: str) -> dict[str, str]:
    return {
        **BUILD_CONTRACT,
        "CAREERPILOT_BUILD_ID": _expected_build_id(source_fingerprint),
    }


def _expected_build_id(source_fingerprint: str) -> str:
    return f"careerpilot-{source_fingerprint}"


def _attest_next_build(boundary: _ReleaseBoundary, expected_build_id: str) -> None:
    next_build_id_path = _assert_safe_path(
        boundary.frontend / ".next" / "BUILD_ID", boundary
    )
    if not next_build_id_path.is_file() or _path_is_alias(next_build_id_path):
        raise StaticUIReleaseError("Next build did not produce a regular .next/BUILD_ID.")
    next_build_id = _read_safe_bytes(next_build_id_path, boundary)
    if next_build_id != expected_build_id.encode("utf-8"):
        raise StaticUIReleaseError(
            "Next BUILD_ID does not match the expected source-derived release build ID."
        )
    attestation = _assert_safe_path(
        boundary.frontend / "out" / BUILD_ID_ATTESTATION_NAME,
        boundary,
    )
    try:
        attestation.write_text(expected_build_id, encoding="utf-8")
    except OSError as exc:
        raise StaticUIReleaseError("Could not write the static UI build ID attestation.") from exc
    _validate_export_structure(boundary, expected_build_id)


def _validate_export_structure(
    boundary: _ReleaseBoundary,
    expected_build_id: str,
) -> None:
    build_directory = _assert_safe_path(
        boundary.frontend / "out" / "_next" / "static" / expected_build_id,
        boundary,
    )
    if not build_directory.is_dir():
        raise StaticUIReleaseError(
            "Static UI export is missing the expected source-derived build directory."
        )
    for required_name in ("_buildManifest.js", "_ssgManifest.js"):
        _assert_safe_regular_file(build_directory / required_name, boundary)
    _validate_index(
        boundary.frontend / "out" / "index.html",
        boundary,
        expected_build_id,
    )
    observed: set[bytes] = set()
    export = boundary.frontend / "out"
    for file in _export_files(boundary):
        observed.update(_BUILD_ID_PATTERN.findall(file.path.encode("utf-8")))
        observed.update(
            _BUILD_ID_PATTERN.findall(
                _read_safe_bytes(
                    export.joinpath(*PurePosixPath(file.path).parts),
                    boundary,
                )
            )
        )
    if observed != {expected_build_id.encode("ascii")}:
        raise StaticUIReleaseError(
            "Static UI export contains a missing, stale, or mixed source-derived build ID."
        )


def _validate_export_build_id(
    boundary: _ReleaseBoundary,
    source_fingerprint: str,
) -> str:
    expected_build_id = _expected_build_id(source_fingerprint)
    attestation_path = _assert_safe_path(
        boundary.frontend / "out" / BUILD_ID_ATTESTATION_NAME,
        boundary,
    )
    if not attestation_path.is_file() or _path_is_alias(attestation_path):
        raise StaticUIReleaseError(
            "Static UI export is missing its source-derived build ID attestation."
        )
    attestation = _read_safe_bytes(attestation_path, boundary)
    if attestation != expected_build_id.encode("utf-8"):
        raise StaticUIReleaseError(
            "Static UI build ID attestation does not match the current source fingerprint."
        )
    _validate_export_structure(boundary, expected_build_id)
    return expected_build_id
