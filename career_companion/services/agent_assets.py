from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Literal

import yaml

from career_companion.paths import CompanionPaths


AgentAssetKind = Literal["memory", "skill"]

_ASSET_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")
_MAX_ASSET_BYTES = 64 * 1024
_MANIFEST_NAME = ".career-pilot-managed-assets.json"
_IMPORT_MARKER = ".profile-assets-imported"


def _agent_root(paths: CompanionPaths) -> Path:
    return paths.workspace / "agent"


def _managed_path(paths: CompanionPaths, kind: AgentAssetKind, name: str) -> Path:
    if kind == "memory":
        return _agent_root(paths) / "memories" / f"{name}.md"
    return _agent_root(paths) / "skills" / name / "SKILL.md"


def _profile_path(paths: CompanionPaths, kind: AgentAssetKind, name: str) -> Path:
    profile = paths.hermes_profile / "profiles" / "career-companion"
    if kind == "memory":
        return profile / "memories" / f"{name}.md"
    return profile / "skills" / name / "SKILL.md"


def _atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Agent files cannot be written through a symbolic link")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(mode)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _validate_name(name: str) -> str:
    normalized = name.strip().casefold()
    if not _ASSET_NAME.fullmatch(normalized):
        raise ValueError("Use lowercase letters, numbers, and single hyphens for the name")
    return normalized


def _safe_read(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Agent files must be regular files")
    if path.stat().st_size > _MAX_ASSET_BYTES:
        raise ValueError("Agent files cannot exceed 64 KB")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("Agent file could not be read as UTF-8") from exc


def _skill_metadata(name: str, content: str) -> dict[str, Any]:
    if not content.startswith("---\n"):
        raise ValueError("Skills must start with YAML front matter")
    parts = content.split("---\n", 2)
    if len(parts) != 3:
        raise ValueError("Skill YAML front matter is not closed")
    try:
        metadata = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise ValueError("Skill YAML front matter is invalid") from exc
    if not isinstance(metadata, dict):
        raise ValueError("Skill YAML front matter must be an object")
    if metadata.get("name") != name:
        raise ValueError(f"Skill front matter name must be '{name}'")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Skills require a description in YAML front matter")
    return metadata


def _validate_content(kind: AgentAssetKind, name: str, content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("Agent files cannot be empty")
    normalized += "\n"
    if len(normalized.encode("utf-8")) > _MAX_ASSET_BYTES:
        raise ValueError("Agent files cannot exceed 64 KB")
    if kind == "skill":
        _skill_metadata(name, normalized)
    return normalized


def _built_in_skill_paths(distribution: Path) -> dict[str, Path]:
    skills = distribution / "skills"
    if not skills.is_dir() or skills.is_symlink():
        return {}
    result: dict[str, Path] = {}
    for path in sorted(skills.glob("*/SKILL.md")):
        if path.is_symlink() or path.parent.is_symlink():
            continue
        try:
            name = _validate_name(path.parent.name)
        except ValueError:
            continue
        result[name] = path
    return result


def _title(name: str, content: str) -> str:
    for line in content.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip()
    return name.replace("-", " ").title()


def _asset_json(
    kind: AgentAssetKind,
    name: str,
    content: str,
    *,
    built_in: bool,
) -> dict[str, Any]:
    relative_path = f"memories/{name}.md" if kind == "memory" else f"skills/{name}/SKILL.md"
    description = None
    if kind == "skill":
        try:
            description = str(_skill_metadata(name, content).get("description") or "")
        except ValueError:
            description = ""
    return {
        "kind": kind,
        "name": name,
        "title": _title(name, content),
        "description": description,
        "content": content,
        "path": relative_path,
        "built_in": built_in,
        "editable": not built_in,
    }


def _import_existing_profile_assets(paths: CompanionPaths, distribution: Path) -> None:
    root = _agent_root(paths)
    marker = root / _IMPORT_MARKER
    if marker.exists():
        return
    built_in = set(_built_in_skill_paths(distribution))
    profile = paths.hermes_profile / "profiles" / "career-companion"
    memories = profile / "memories"
    if memories.is_dir() and not memories.is_symlink():
        for source in memories.glob("*.md"):
            try:
                name = _validate_name(source.stem)
                destination = _managed_path(paths, "memory", name)
                if not destination.exists():
                    _atomic_write(destination, _safe_read(source))
            except ValueError:
                continue
    skills = profile / "skills"
    if skills.is_dir() and not skills.is_symlink():
        for source in skills.glob("*/SKILL.md"):
            try:
                name = _validate_name(source.parent.name)
                destination = _managed_path(paths, "skill", name)
                if name not in built_in and not destination.exists():
                    _atomic_write(destination, _safe_read(source))
            except ValueError:
                continue
    _atomic_write(marker, "imported\n")


def list_agent_assets(paths: CompanionPaths, distribution: Path) -> dict[str, Any]:
    paths.create()
    _import_existing_profile_assets(paths, distribution)
    memories: list[dict[str, Any]] = []
    memory_root = _agent_root(paths) / "memories"
    if memory_root.is_dir() and not memory_root.is_symlink():
        for path in sorted(memory_root.glob("*.md")):
            try:
                name = _validate_name(path.stem)
                memories.append(_asset_json("memory", name, _safe_read(path), built_in=False))
            except ValueError:
                continue
    skills: list[dict[str, Any]] = []
    for name, path in _built_in_skill_paths(distribution).items():
        try:
            skills.append(_asset_json("skill", name, _safe_read(path), built_in=True))
        except ValueError:
            continue
    skill_root = _agent_root(paths) / "skills"
    if skill_root.is_dir() and not skill_root.is_symlink():
        for path in sorted(skill_root.glob("*/SKILL.md")):
            try:
                name = _validate_name(path.parent.name)
                if name in {item["name"] for item in skills}:
                    continue
                skills.append(_asset_json("skill", name, _safe_read(path), built_in=False))
            except ValueError:
                continue
    return {"memories": memories, "skills": skills}


def save_agent_asset(
    paths: CompanionPaths,
    distribution: Path,
    *,
    kind: AgentAssetKind,
    name: str,
    content: str,
    previous_name: str | None = None,
) -> None:
    name = _validate_name(name)
    previous_name = _validate_name(previous_name) if previous_name else None
    built_in = _built_in_skill_paths(distribution) if kind == "skill" else {}
    if name in built_in or (previous_name and previous_name in built_in):
        raise PermissionError("Built-in skills are read-only")
    normalized = _validate_content(kind, name, content)
    destination = _managed_path(paths, kind, name)
    if previous_name is None and destination.exists():
        raise FileExistsError("An agent file with that name already exists")
    if previous_name is not None:
        source = _managed_path(paths, kind, previous_name)
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError("Agent file not found")
        if previous_name != name and destination.exists():
            raise FileExistsError("An agent file with that name already exists")
    _atomic_write(destination, normalized)
    if previous_name and previous_name != name:
        source = _managed_path(paths, kind, previous_name)
        source.unlink(missing_ok=True)
        if kind == "skill":
            try:
                source.parent.rmdir()
            except OSError:
                pass


def delete_agent_asset(
    paths: CompanionPaths,
    distribution: Path,
    *,
    kind: AgentAssetKind,
    name: str,
) -> None:
    name = _validate_name(name)
    if kind == "skill" and name in _built_in_skill_paths(distribution):
        raise PermissionError("Built-in skills are read-only")
    path = _managed_path(paths, kind, name)
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError("Agent file not found")
    path.unlink()
    if kind == "skill":
        try:
            path.parent.rmdir()
        except OSError:
            pass


def synchronize_managed_profile_assets(paths: CompanionPaths, distribution: Path) -> None:
    profile = paths.hermes_profile / "profiles" / "career-companion"
    if not (profile / "config.yaml").is_file():
        return
    assets = list_agent_assets(paths, distribution)
    manifest_path = profile / _MANIFEST_NAME
    previous: list[str] = []
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                previous = [item for item in payload if isinstance(item, str)]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            previous = []
    for relative in previous:
        candidate = profile / relative
        try:
            candidate.relative_to(profile)
        except ValueError:
            continue
        if candidate.is_file() and not candidate.is_symlink():
            candidate.unlink()
    managed: list[str] = []
    for kind, key in (("memory", "memories"), ("skill", "skills")):
        for asset in assets[key]:
            if asset["built_in"]:
                continue
            destination = _profile_path(paths, kind, asset["name"])  # type: ignore[arg-type]
            _atomic_write(destination, asset["content"])
            managed.append(str(destination.relative_to(profile)))
    _atomic_write(manifest_path, json.dumps(managed, indent=2) + "\n")
