from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from career_companion.paths import CompanionPaths

BACKUP_FORMAT_VERSION = 1
EXCLUDED_NAMES = {
    ".env",
    ".hermes-bridge-token",
    ".session-token",
    ".api-server-key",
    ".auth-secret",
    "auth.json",
    "accounts.db",
    "browser-profile",
    "logs",
    "backups",
}
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_MEMBER_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 1024 * 1024 * 1024
_ALLOWED_ARCHIVE_ROOTS = {
    "backup-manifest.json",
    "config/config.yaml",
    "data/career.db",
}
_ALLOWED_ARCHIVE_PREFIXES = ("workspace/", "hermes/local/")
_EXCLUDED_CASEFOLD = {name.casefold() for name in EXCLUDED_NAMES}


def create_backup(paths: CompanionPaths, destination: Path | None = None) -> Path:
    paths.create()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = destination or paths.backups / f"career-companion-{timestamp}.zip"
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("Backup destination must not be a symbolic link")
    manifest = {
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "includes_credentials": False,
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("backup-manifest.json", json.dumps(manifest, indent=2))
            if paths.database.is_file():
                _add_database_snapshot(archive, paths.database)
            if paths.workspace.is_dir():
                _add_tree(archive, paths.workspace, "workspace")
            if paths.config.is_file() and not paths.config.is_symlink():
                archive.write(paths.config, "config/config.yaml")
            user_owned = paths.hermes_profile / "profiles" / "career-companion" / "local"
            if user_owned.is_dir() and not user_owned.is_symlink():
                _add_tree(archive, user_owned, "hermes/local")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _add_tree(archive: zipfile.ZipFile, root: Path, prefix: str) -> None:
    for path in sorted(root.rglob("*")):
        if (
            not path.is_file()
            or path.is_symlink()
            or any(part.casefold() in _EXCLUDED_CASEFOLD for part in path.parts)
        ):
            continue
        archive.write(path, str(Path(prefix) / path.relative_to(root)))


def _add_database_snapshot(archive: zipfile.ZipFile, source: Path) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        snapshot = Path(temporary) / "career.db"
        with (
            closing(sqlite3.connect(source)) as input_database,
            closing(sqlite3.connect(snapshot)) as output_database,
        ):
            input_database.backup(output_database)
        archive.write(snapshot, "data/career.db")


def restore_backup(paths: CompanionPaths, source: Path, *, replace: bool = False) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    with zipfile.ZipFile(source) as archive:
        _validate_archive(archive)
        manifest = json.loads(archive.read("backup-manifest.json"))
        if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
            raise ValueError("Unsupported backup format")
        destinations = _restore_destinations(paths, archive.namelist())
        if not replace and any(_contains_data(target) for _, target in destinations):
            raise FileExistsError("Local data exists. Pass --replace to restore over it.")
        paths.root.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=paths.root.parent) as temporary:
            extracted = Path(temporary)
            archive.extractall(extracted)
            components = [
                (extracted / "data" / "career.db", paths.database),
                (extracted / "workspace", paths.workspace),
                (extracted / "config" / "config.yaml", paths.config),
                (
                    extracted / "hermes" / "local",
                    paths.hermes_profile / "profiles" / "career-companion" / "local",
                ),
            ]
            _replace_components_atomically(
                [(item, target) for item, target in components if item.exists()]
            )
    paths.create()


def _restore_destinations(paths: CompanionPaths, names: list[str]) -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    if "data/career.db" in names:
        result.append(("data/career.db", paths.database))
    if any(name.startswith("workspace/") for name in names):
        result.append(("workspace", paths.workspace))
    if "config/config.yaml" in names:
        result.append(("config/config.yaml", paths.config))
    if any(name.startswith("hermes/local/") for name in names):
        result.append(
            ("hermes/local", paths.hermes_profile / "profiles" / "career-companion" / "local")
        )
    return result


def _contains_data(path: Path) -> bool:
    if path.is_file() or path.is_symlink():
        return True
    return path.is_dir() and next(path.iterdir(), None) is not None


def _replace_components_atomically(components: list[tuple[Path, Path]]) -> None:
    prepared: list[tuple[Path, Path]] = []
    swaps: list[tuple[Path, Path | None]] = []
    try:
        for source, target in components:
            target.parent.mkdir(parents=True, exist_ok=True)
            candidate = target.parent / f".{target.name}.restore-{uuid.uuid4().hex}"
            if source.is_dir():
                shutil.copytree(source, candidate)
            else:
                shutil.copy2(source, candidate)
            prepared.append((candidate, target))
        for candidate, target in prepared:
            previous = None
            if target.exists() or target.is_symlink():
                previous = target.parent / f".{target.name}.previous-{uuid.uuid4().hex}"
                os.replace(target, previous)
            try:
                os.replace(candidate, target)
            except Exception:
                if previous is not None:
                    os.replace(previous, target)
                raise
            swaps.append((target, previous))
    except Exception:
        for target, previous in reversed(swaps):
            _remove_path(target)
            if previous is not None:
                os.replace(previous, target)
        raise
    finally:
        for candidate, _ in prepared:
            _remove_path(candidate)
    for _, previous in swaps:
        if previous is not None:
            _remove_path(previous)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _validate_archive(archive: zipfile.ZipFile) -> None:
    if "backup-manifest.json" not in archive.namelist():
        raise ValueError("This is not a Career Companion backup")
    if len(archive.infolist()) > MAX_ARCHIVE_MEMBERS:
        raise ValueError("Backup contains too many files")
    seen: set[str] = set()
    total_size = 0
    for member in archive.infolist():
        if "\\" in member.filename or "\x00" in member.filename:
            raise ValueError("Backup contains an unsafe path")
        path = Path(member.filename)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Backup contains an unsafe path")
        normalized = path.as_posix().rstrip("/")
        if normalized in seen:
            raise ValueError("Backup contains a duplicate path")
        seen.add(normalized)
        if any(part.casefold() in _EXCLUDED_CASEFOLD for part in path.parts):
            raise ValueError("Backup unexpectedly contains a secret or runtime path")
        if not (
            normalized in _ALLOWED_ARCHIVE_ROOTS
            or any(normalized.startswith(prefix) for prefix in _ALLOWED_ARCHIVE_PREFIXES)
        ):
            raise ValueError(f"Backup contains an unexpected path: {normalized}")
        mode = member.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ValueError("Backup contains a symbolic link")
        if member.flag_bits & 0x1:
            raise ValueError("Encrypted backup members are not supported")
        if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError("Backup contains an oversized file")
        total_size += member.file_size
        if total_size > MAX_ARCHIVE_TOTAL_BYTES:
            raise ValueError("Backup expands beyond the 1 GB safety limit")
        if (
            member.file_size > 1024 * 1024
            and member.compress_size
            and member.file_size / member.compress_size > 500
        ):
            raise ValueError("Backup contains a suspicious compression ratio")
