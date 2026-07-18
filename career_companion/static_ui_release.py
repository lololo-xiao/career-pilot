from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import unicodedata
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlsplit

MANIFEST_NAME = ".careerpilot-static-ui.json"
BUILD_ID_ATTESTATION_NAME = ".careerpilot-build-id"
MANIFEST_VERSION = 3
INDEX_MARKER = "CareerPilot"
MAX_STATIC_UI_HTML_FILES = 256
MAX_STATIC_UI_REFERENCED_ASSETS = 4096
MAX_STATIC_UI_FILES = 100_000
MAX_STATIC_UI_FILE_SIZE = 512 * 1024 * 1024
BUILD_CONTRACT = {
    "CAREERPILOT_STATIC_EXPORT": "true",
    "NEXT_PUBLIC_API_BASE_URL": "",
    "NEXT_TELEMETRY_DISABLED": "1",
}

_IGNORED_SOURCE_DIRECTORIES = frozenset({".next", "node_modules", "out"})
_IGNORED_GENERATED_SOURCE_FILES = frozenset({"next-env.d.ts"})
_JUNK_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
_JUNK_DIRECTORIES = frozenset({"__MACOSX"})
_BUILD_ID_PATTERN = re.compile(rb"careerpilot-[0-9A-Fa-f]{64}")
_NEXT_MANIFEST_NAMES = ("_buildManifest.js", "_ssgManifest.js")
_NEXT_MANIFEST_NAMES_CASEFOLDED = frozenset(
    name.casefold() for name in _NEXT_MANIFEST_NAMES
)
_NEXT_MANIFEST_MARKERS = {
    "_buildManifest.js": "__BUILD_MANIFEST",
    "_ssgManifest.js": "__SSG_MANIFEST",
}
_MAX_NEXT_MANIFEST_SIZE = 2 * 1024 * 1024
_WINDOWS_RESERVED_COMPONENTS = frozenset(
    {
        "aux",
        "con",
        "conin$",
        "conout$",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)
_WINDOWS_ILLEGAL_CHARACTERS = frozenset('<>:"|?*')
_MANIFEST_KEYS = frozenset(
    {
        "build_contract",
        "build_id",
        "export_fingerprint",
        "files",
        "format_version",
        "index_marker",
        "index_sha256",
        "source_fingerprint",
    }
)
_MANIFEST_FILE_KEYS = frozenset({"path", "sha256", "size"})
_LOWERCASE_SHA256 = re.compile(r"[0-9a-f]{64}")
_NEXT_STATIC_CONTENT_DIRECTORIES = frozenset({"chunks", "css", "media"})
_HTML_ASCII_WHITESPACE = " \t\n\f\r"
_JAVASCRIPT_MIME_TYPES = frozenset(
    {
        "application/ecmascript",
        "application/javascript",
        "application/x-ecmascript",
        "application/x-javascript",
        "text/ecmascript",
        "text/javascript",
        "text/javascript1.0",
        "text/javascript1.1",
        "text/javascript1.2",
        "text/javascript1.3",
        "text/javascript1.4",
        "text/javascript1.5",
        "text/jscript",
        "text/livescript",
        "text/x-ecmascript",
        "text/x-javascript",
    }
)
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
class VerifiedBundledStaticUI:
    bundle: Path
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


class _ScriptSourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []
        self.executable_sources: list[str] = []
        self.stylesheet_sources: list[str] = []
        self.script_preload_sources: list[str] = []
        self.style_preload_sources: list[str] = []
        self.has_base = False
        self.invalid_reserved_source = False
        self.unsupported_integrity = False
        self.ambiguous_markup = False
        self._inert_stack: list[str] = []
        self._open_scripts = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        folded_tag = tag.casefold()
        if folded_tag in {"template", "noscript"}:
            self._inert_stack.append(folded_tag)
            return
        if folded_tag == "base":
            self.has_base = True
            return
        if folded_tag == "link":
            if self._inert_stack or _has_duplicate_attributes(
                attrs, {"href", "rel", "as", "type", "integrity"}
            ):
                self.ambiguous_markup = True
            attributes = {name.casefold(): value for name, value in attrs}
            rel_tokens = _html_ascii_tokens(attributes.get("rel"))
            if rel_tokens is None:
                self.ambiguous_markup = True
                rel_tokens = frozenset()
            active_relations = rel_tokens & {
                "stylesheet",
                "preload",
                "modulepreload",
            }
            if len(active_relations) > 1:
                self.ambiguous_markup = True
            active_relation = next(iter(active_relations), None)
            href = attributes.get("href")
            if active_relation is not None and (href is None or not href):
                self.ambiguous_markup = True
            if active_relation is not None and "integrity" in attributes:
                self.unsupported_integrity = True
            if active_relation == "stylesheet" and href:
                if "as" in attributes:
                    self.ambiguous_markup = True
                self.stylesheet_sources.append(href)
            elif active_relation == "preload" and href:
                preload_as = _normalized_link_as(attributes.get("as"))
                if "as" not in attributes or preload_as not in {"script", "style"}:
                    self.ambiguous_markup = True
                elif preload_as == "script":
                    self.script_preload_sources.append(href)
                else:
                    self.style_preload_sources.append(href)
            elif active_relation == "modulepreload" and href:
                if "as" not in attributes:
                    module_as = "script"
                else:
                    module_as = _normalized_link_as(attributes.get("as"))
                if module_as != "script":
                    self.ambiguous_markup = True
                else:
                    self.script_preload_sources.append(href)
            return
        if folded_tag != "script":
            return
        self._open_scripts += 1
        if self._inert_stack or _has_duplicate_attributes(
            attrs, {"src", "type", "integrity"}
        ):
            self.ambiguous_markup = True
        attributes = {name.casefold(): value for name, value in attrs}
        source_values = [
            value
            for name, value in attrs
            if name.casefold() == "src"
        ]
        script_type = _normalized_script_type(attributes.get("type"))
        if script_type is None:
            self.ambiguous_markup = True
            executable = False
        else:
            executable = not self._inert_stack and (
                _script_type_is_executable(script_type)
            )
        if executable and source_values and any(not source for source in source_values):
            self.ambiguous_markup = True
        sources = [source for source in source_values if source is not None]
        for source in sources:
            self.sources.append(source)
            if executable:
                self.executable_sources.append(source)
                if "integrity" in attributes:
                    self.unsupported_integrity = True
            if any(
                name in source.casefold() for name in _NEXT_MANIFEST_NAMES_CASEFOLDED
            ) and (
                not executable
                or len(sources) != 1
                or "integrity" in attributes
            ):
                self.invalid_reserved_source = True

    def handle_endtag(self, tag: str) -> None:
        folded_tag = tag.casefold()
        if folded_tag in {"template", "noscript"}:
            if not self._inert_stack or self._inert_stack[-1] != folded_tag:
                self.ambiguous_markup = True
            else:
                self._inert_stack.pop()
        elif folded_tag == "script":
            if not self._open_scripts:
                self.ambiguous_markup = True
            else:
                self._open_scripts -= 1
        elif folded_tag in {"link", "base"}:
            self.ambiguous_markup = True

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() in {"script", "template", "noscript"}:
            self.ambiguous_markup = True
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in {"link", "base"}:
            self.handle_endtag(tag)

    @property
    def incomplete_security_markup(self) -> bool:
        return bool(self._inert_stack or self._open_scripts)


def _has_duplicate_attributes(
    attrs: list[tuple[str, str | None]],
    security_relevant: set[str],
) -> bool:
    seen: set[str] = set()
    for name, _value in attrs:
        folded = name.casefold()
        if folded in security_relevant:
            if folded in seen:
                return True
            seen.add(folded)
    return False


def _contains_non_ascii_whitespace(value: str) -> bool:
    return any(
        character.isspace() and character not in _HTML_ASCII_WHITESPACE
        for character in value
    )


def _html_ascii_tokens(value: str | None) -> frozenset[str] | None:
    raw = value or ""
    if _contains_non_ascii_whitespace(raw):
        return None
    return frozenset(
        token.casefold()
        for token in re.split(r"[ \t\n\f\r]+", raw.strip(_HTML_ASCII_WHITESPACE))
        if token
    )


def _normalized_script_type(value: str | None) -> str | None:
    raw = value or ""
    if _contains_non_ascii_whitespace(raw):
        return None
    return raw.strip(_HTML_ASCII_WHITESPACE).casefold()


def _normalized_link_as(value: str | None) -> str | None:
    raw = value or ""
    if _contains_non_ascii_whitespace(raw):
        return None
    return raw.strip(_HTML_ASCII_WHITESPACE).casefold()


def _script_type_is_executable(script_type: str) -> bool:
    if not script_type or script_type == "module":
        return True
    mime_essence = script_type.split(";", 1)[0].strip(_HTML_ASCII_WHITESPACE)
    return mime_essence in _JAVASCRIPT_MIME_TYPES


def _parse_release_index(index_payload: bytes) -> _ScriptSourceParser:
    try:
        index_text = index_payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StaticUIReleaseError("Static UI index.html must be valid UTF-8.") from exc
    parser = _ScriptSourceParser()
    try:
        parser.feed(index_text)
        parser.close()
    except ValueError as exc:
        raise StaticUIReleaseError("Static UI index.html contains malformed HTML.") from exc
    if parser.ambiguous_markup or parser.incomplete_security_markup:
        raise StaticUIReleaseError(
            "Static UI index.html contains ambiguous or misnested security markup."
        )
    return parser


def static_ui_manifest_asset_paths(
    index_payload: bytes,
    expected_build_id: str,
) -> tuple[str, str]:
    """Return the uniquely referenced, source-derived Next manifest asset paths."""

    parser = _parse_release_index(index_payload)
    if parser.has_base or parser.invalid_reserved_source:
        raise StaticUIReleaseError(
            "Static UI index.html build-manifest references must be directly executable."
        )
    reserved_sources = [
        source
        for source in parser.sources
        if any(name in source.casefold() for name in _NEXT_MANIFEST_NAMES_CASEFOLDED)
    ]
    referenced_paths = tuple(
        _canonical_export_script_reference(source) for source in reserved_sources
    )
    expected_paths = tuple(
        f"_next/static/{expected_build_id}/{name}" for name in _NEXT_MANIFEST_NAMES
    )
    if len(referenced_paths) != 2 or set(referenced_paths) != set(expected_paths):
        raise StaticUIReleaseError(
            "Static UI index.html must reference exactly one source-derived Next "
            "build-manifest pair."
        )
    return expected_paths


def static_ui_frontend_asset_paths(
    index_payload: bytes,
    *,
    document_path: str = "index.html",
) -> tuple[str, ...]:
    """Return all executable script and stylesheet paths in release HTML."""

    parser = _parse_release_index(index_payload)
    if parser.has_base:
        raise StaticUIReleaseError(
            "Static UI index.html cannot use a base URL for release assets."
        )
    if parser.unsupported_integrity:
        raise StaticUIReleaseError(
            "Static UI executable and stylesheet assets cannot use integrity "
            "attributes in the release export."
        )
    script_paths = tuple(
        _canonical_export_asset_reference(source, document_path=document_path)
        for source in (*parser.executable_sources, *parser.script_preload_sources)
    )
    stylesheet_paths = tuple(
        _canonical_export_asset_reference(source, document_path=document_path)
        for source in (*parser.stylesheet_sources, *parser.style_preload_sources)
    )
    if any(not path.endswith(".js") for path in script_paths):
        raise StaticUIReleaseError(
            "Static UI executable script references must end with .js."
        )
    if any(not path.endswith(".css") for path in stylesheet_paths):
        raise StaticUIReleaseError(
            "Static UI stylesheet references must end with .css."
        )
    paths = (*script_paths, *stylesheet_paths)
    if not paths:
        raise StaticUIReleaseError(
            "Static UI index.html must reference executable or stylesheet assets."
        )
    return paths


def _canonical_export_asset_reference(
    source: str,
    *,
    document_path: str = "index.html",
) -> str:
    if not _safe_manifest_path(document_path) or not document_path.casefold().endswith(
        ".html"
    ):
        raise StaticUIReleaseError("Static UI contains an unsafe HTML document path.")
    if (
        not source
        or "\\" in source
        or any(character.isspace() or ord(character) < 0x20 for character in source)
    ):
        raise StaticUIReleaseError(
            "Static UI index.html contains a malformed frontend asset reference."
        )
    try:
        parsed = urlsplit(source)
    except ValueError as exc:
        raise StaticUIReleaseError(
            "Static UI index.html contains a malformed frontend asset reference."
        ) from exc
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path.startswith("//"):
        raise StaticUIReleaseError(
            "Static UI index.html frontend asset references must be local export paths."
        )
    raw_path = parsed.path
    if not raw_path or "%" in raw_path or not raw_path.startswith("/_next/static/"):
        raise StaticUIReleaseError(
            "Static UI frontend asset references must be root-absolute "
            "/_next/static paths."
        )
    relative = raw_path[1:]
    source_components = relative.split("/")
    if any(component in {"", ".", ".."} for component in source_components):
        raise StaticUIReleaseError(
            "Static UI index.html contains an unsafe frontend asset reference."
        )
    components = source_components
    path = PurePosixPath(*components)
    if not path.as_posix().startswith("_next/static/"):
        raise StaticUIReleaseError(
            "Static UI index.html contains an unsafe frontend asset reference."
        )
    return path.as_posix()


def _canonical_export_script_reference(source: str) -> str:
    path = _canonical_export_asset_reference(source)
    if PurePosixPath(path).name.casefold() not in _NEXT_MANIFEST_NAMES_CASEFOLDED:
        raise StaticUIReleaseError(
            "Static UI index.html contains an unsafe build-manifest reference."
        )
    return path


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
        payload = _load_manifest_json(_read_safe_bytes(manifest, boundary))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
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


def verify_bundled_static_ui(
    bundle: Path,
    *,
    expected_export_fingerprint: str | None = None,
) -> VerifiedBundledStaticUI:
    """Independently verify an installed bundle without frontend source inputs."""

    if expected_export_fingerprint is not None and (
        not isinstance(expected_export_fingerprint, str)
        or _LOWERCASE_SHA256.fullmatch(expected_export_fingerprint) is None
    ):
        raise StaticUIReleaseError(
            "Expected static UI export fingerprint must be a lowercase SHA-256 digest."
        )
    root = Path(os.path.abspath(os.fspath(Path(bundle).expanduser())))
    if not root.is_dir() or _path_is_alias(root):
        raise StaticUIReleaseError(
            f"Bundled static UI root must be a regular directory: {root}"
        )
    root_resolved = _resolve_path(root)
    actual_files, actual_directories = _collect_bundled_static_ui_files(
        root,
        root_resolved,
    )
    manifest_path = _assert_safe_bundled_regular_file(
        root / MANIFEST_NAME,
        root,
        root_resolved,
    )
    try:
        payload = _load_manifest_json(manifest_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise StaticUIReleaseError(
            "Bundled static UI manifest is unreadable or invalid JSON."
        ) from exc
    source_fingerprint = payload.get("source_fingerprint")
    if not isinstance(source_fingerprint, str) or re.fullmatch(
        r"[0-9a-f]{64}", source_fingerprint
    ) is None:
        raise StaticUIReleaseError(
            "Bundled static UI manifest has an invalid source fingerprint."
        )
    _validate_manifest_header(payload, source_fingerprint)
    expected_files = _manifest_files(payload.get("files"))
    if actual_files != expected_files:
        raise StaticUIReleaseError(
            "Bundled static UI file membership or content differs from its manifest."
        )
    expected_directories = {
        parent.as_posix()
        for file in expected_files
        for parent in PurePosixPath(file.path).parents
        if parent.as_posix() != "."
    }
    if actual_directories != expected_directories:
        raise StaticUIReleaseError(
            "Bundled static UI directory membership differs from its manifest."
        )
    export_fingerprint = _fingerprint(actual_files)
    if payload.get("export_fingerprint") != export_fingerprint:
        raise StaticUIReleaseError(
            "Bundled static UI export fingerprint does not match its files."
        )
    if (
        expected_export_fingerprint is not None
        and export_fingerprint != expected_export_fingerprint
    ):
        raise StaticUIReleaseError(
            "Bundled static UI fingerprint differs from the expected release export."
        )
    build_id = _expected_build_id(source_fingerprint)

    def read_file(relative: str) -> bytes:
        path = _assert_safe_bundled_regular_file(
            root.joinpath(*PurePosixPath(relative).parts),
            root,
            root_resolved,
        )
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StaticUIReleaseError(
                f"Could not safely read bundled static UI file: {relative}"
            ) from exc

    attestation = read_file(BUILD_ID_ATTESTATION_NAME)
    if attestation != build_id.encode("ascii"):
        raise StaticUIReleaseError(
            "Bundled static UI build ID attestation is inconsistent."
        )
    _validate_export_inventory(actual_files, build_id, read_file)
    index_payload = read_file("index.html")
    index_sha256 = hashlib.sha256(index_payload).hexdigest()
    if payload.get("index_sha256") != index_sha256:
        raise StaticUIReleaseError(
            "Bundled static UI index fingerprint does not match its manifest."
        )
    return VerifiedBundledStaticUI(
        bundle=root,
        manifest=manifest_path,
        source_fingerprint=source_fingerprint,
        build_id=build_id,
        export_fingerprint=export_fingerprint,
        index_sha256=index_sha256,
        files=actual_files,
    )


def verify_sdist_static_ui(
    archive_path: Path,
    *,
    destination: Path,
    expected_export_fingerprint: str,
) -> VerifiedBundledStaticUI:
    """Safely extract and independently verify one sdist's frontend/out bundle."""

    destination = Path(os.path.abspath(os.fspath(destination)))
    if destination.exists():
        raise StaticUIReleaseError(
            f"sdist verification destination must not already exist: {destination}"
        )
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            members = archive.getmembers()
            seen_names: set[str] = set()
            portable_paths: dict[str, tuple[str, bool]] = {}
            safe_members: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
            for member in members:
                path = PurePosixPath(member.name)
                if (
                    not member.name
                    or "\\" in member.name
                    or path.is_absolute()
                    or ".." in path.parts
                    or member.name != path.as_posix()
                    or member.name in seen_names
                ):
                    raise StaticUIReleaseError(
                        f"sdist contains an unsafe or duplicate member: {member.name}"
                    )
                seen_names.add(member.name)
                if not (member.isfile() or member.isdir()):
                    raise StaticUIReleaseError(
                        f"sdist contains an unsupported member type: {member.name}"
                    )
                _register_portable_release_path(
                    portable_paths,
                    member.name,
                    is_directory=member.isdir(),
                )
                safe_members.append((member, path))
            manifests = [
                path
                for member, path in safe_members
                if member.isfile()
                and len(path.parts) >= 4
                and path.parts[-3:] == ("frontend", "out", MANIFEST_NAME)
            ]
            if len(manifests) != 1:
                raise StaticUIReleaseError(
                    "sdist must contain exactly one frontend/out static UI manifest."
                )
            ui_prefix = manifests[0].parent
            ui_members = [
                (member, path.relative_to(ui_prefix))
                for member, path in safe_members
                if path.is_relative_to(ui_prefix) and path != ui_prefix
            ]
            ui_files = [member for member, _relative in ui_members if member.isfile()]
            if len(ui_files) > MAX_STATIC_UI_FILES + 1 or any(
                member.size > MAX_STATIC_UI_FILE_SIZE for member in ui_files
            ):
                raise StaticUIReleaseError(
                    "sdist static UI inventory exceeds release bounds."
                )
            destination.mkdir(parents=True)
            for member, relative in ui_members:
                target = destination.joinpath(*relative.parts)
                if not target.is_relative_to(destination):
                    raise StaticUIReleaseError(
                        f"sdist static UI member escapes extraction root: {member.name}"
                    )
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise StaticUIReleaseError(
                        f"sdist static UI member could not be read: {member.name}"
                    )
                payload = extracted.read()
                if len(payload) != member.size:
                    raise StaticUIReleaseError(
                        f"sdist static UI member size is inconsistent: {member.name}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
    except (OSError, tarfile.TarError) as exc:
        raise StaticUIReleaseError(
            f"Could not safely inspect static UI sdist: {archive_path}"
        ) from exc
    return verify_bundled_static_ui(
        destination,
        expected_export_fingerprint=expected_export_fingerprint,
    )


def verify_wheel_static_ui(
    archive_path: Path,
    *,
    destination: Path,
    expected_export_fingerprint: str,
) -> VerifiedBundledStaticUI:
    """Safely extract and independently verify one wheel's bundled static UI."""

    destination = Path(os.path.abspath(os.fspath(destination)))
    if destination.exists():
        raise StaticUIReleaseError(
            f"wheel verification destination must not already exist: {destination}"
        )
    try:
        with zipfile.ZipFile(archive_path) as archive:
            seen_names: set[str] = set()
            portable_paths: dict[str, tuple[str, bool]] = {}
            safe_members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
            for member in archive.infolist():
                is_directory = member.is_dir()
                name = member.filename[:-1] if is_directory else member.filename
                path = PurePosixPath(name)
                expected_name = path.as_posix() + ("/" if is_directory else "")
                unix_type = (member.external_attr >> 16) & 0o170000
                if (
                    not name
                    or "\\" in name
                    or path.is_absolute()
                    or ".." in path.parts
                    or member.filename != expected_name
                    or name in seen_names
                ):
                    raise StaticUIReleaseError(
                        f"wheel contains an unsafe or duplicate member: {member.filename}"
                    )
                seen_names.add(name)
                if not is_directory and unix_type not in {0, stat.S_IFREG}:
                    raise StaticUIReleaseError(
                        f"wheel contains an unsupported member type: {member.filename}"
                    )
                _register_portable_release_path(
                    portable_paths,
                    name,
                    is_directory=is_directory,
                )
                safe_members.append((member, path))
            manifest_path = PurePosixPath(
                "career_companion",
                "web",
                MANIFEST_NAME,
            )
            manifests = [
                path
                for member, path in safe_members
                if not member.is_dir() and path == manifest_path
            ]
            if len(manifests) != 1:
                raise StaticUIReleaseError(
                    "wheel must contain exactly one career_companion/web static UI "
                    "manifest."
                )
            ui_prefix = manifest_path.parent
            ui_members = [
                (member, path.relative_to(ui_prefix))
                for member, path in safe_members
                if path.is_relative_to(ui_prefix) and path != ui_prefix
            ]
            ui_files = [
                member for member, _relative in ui_members if not member.is_dir()
            ]
            if len(ui_files) > MAX_STATIC_UI_FILES + 1 or any(
                member.file_size > MAX_STATIC_UI_FILE_SIZE for member in ui_files
            ):
                raise StaticUIReleaseError(
                    "wheel static UI inventory exceeds release bounds."
                )
            destination.mkdir(parents=True)
            for member, relative in ui_members:
                target = destination.joinpath(*relative.parts)
                if not target.is_relative_to(destination):
                    raise StaticUIReleaseError(
                        f"wheel static UI member escapes extraction root: {member.filename}"
                    )
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                try:
                    payload = archive.read(member)
                except RuntimeError as exc:
                    raise StaticUIReleaseError(
                        f"wheel static UI member could not be read: {member.filename}"
                    ) from exc
                if len(payload) != member.file_size:
                    raise StaticUIReleaseError(
                        f"wheel static UI member size is inconsistent: {member.filename}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
    except (OSError, zipfile.BadZipFile) as exc:
        raise StaticUIReleaseError(
            f"Could not safely inspect static UI wheel: {archive_path}"
        ) from exc
    return verify_bundled_static_ui(
        destination,
        expected_export_fingerprint=expected_export_fingerprint,
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
        staged_manifest = _load_manifest_json(manifest_payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
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
    portable_paths: dict[str, tuple[str, bool]] = {}
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
                        _register_portable_release_path(
                            portable_paths,
                            relative.as_posix(),
                            is_directory=True,
                        )
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
                        if entry.name in _JUNK_NAMES:
                            continue
                        _register_portable_release_path(
                            portable_paths,
                            relative.as_posix(),
                            is_directory=False,
                        )
                        if entry.name in ignored_names:
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
    if len(paths) > MAX_STATIC_UI_FILES:
        raise StaticUIReleaseError("Static UI release input contains too many files.")
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        safe = _assert_safe_regular_file(path, boundary)
        payload = _read_safe_bytes(safe, boundary)
        if len(payload) > MAX_STATIC_UI_FILE_SIZE:
            raise StaticUIReleaseError(
                f"Static UI release input file is too large: {safe}"
            )
        files.append(
            StaticUIFile(
                path=safe.relative_to(root).as_posix(),
                sha256=hashlib.sha256(payload).hexdigest(),
                size=len(payload),
            )
        )
    return tuple(files)


def _collect_bundled_static_ui_files(
    root: Path,
    root_resolved: Path,
) -> tuple[tuple[StaticUIFile, ...], set[str]]:
    pending = [root]
    paths: list[Path] = []
    directories: set[str] = set()
    portable_paths: dict[str, tuple[str, bool]] = {}
    try:
        while pending:
            directory = _assert_safe_bundled_path(
                pending.pop(),
                root,
                root_resolved,
            )
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = _assert_safe_bundled_path(
                        Path(entry.path),
                        root,
                        root_resolved,
                    )
                    relative = path.relative_to(root).as_posix()
                    if entry.is_dir(follow_symlinks=False):
                        _register_portable_release_path(
                            portable_paths,
                            relative,
                            is_directory=True,
                        )
                        directories.add(relative)
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        _register_portable_release_path(
                            portable_paths,
                            relative,
                            is_directory=False,
                        )
                        if relative != MANIFEST_NAME:
                            paths.append(path)
                    else:
                        raise StaticUIReleaseError(
                            "Bundled static UI can contain only regular files and "
                            f"directories: {relative}"
                        )
    except OSError as exc:
        raise StaticUIReleaseError(
            f"Could not safely enumerate bundled static UI: {root}"
        ) from exc

    files: list[StaticUIFile] = []
    if len(paths) > MAX_STATIC_UI_FILES:
        raise StaticUIReleaseError("Bundled static UI contains too many files.")
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        safe = _assert_safe_bundled_regular_file(
            path,
            root,
            root_resolved,
        )
        try:
            payload = safe.read_bytes()
        except OSError as exc:
            raise StaticUIReleaseError(
                f"Could not safely read bundled static UI: {safe}"
            ) from exc
        if len(payload) > MAX_STATIC_UI_FILE_SIZE:
            raise StaticUIReleaseError(
                f"Bundled static UI file is too large: {safe}"
            )
        files.append(
            StaticUIFile(
                path=safe.relative_to(root).as_posix(),
                sha256=hashlib.sha256(payload).hexdigest(),
                size=len(payload),
            )
        )
    return tuple(files), directories


def _register_portable_release_path(
    registry: dict[str, tuple[str, bool]],
    value: str,
    *,
    is_directory: bool,
) -> None:
    parts, keys = _portable_release_path_parts(value)
    for index, _key_part in enumerate(keys, start=1):
        raw_prefix = "/".join(parts[:index])
        key = "/".join(keys[:index])
        prefix_is_directory = is_directory or index < len(parts)
        existing = registry.get(key)
        if existing is None:
            registry[key] = (raw_prefix, prefix_is_directory)
            continue
        existing_raw, existing_is_directory = existing
        if existing_raw != raw_prefix or existing_is_directory != prefix_is_directory:
            raise StaticUIReleaseError(
                "Release inventory contains a non-portable path collision: "
                f"{existing_raw!r} and {raw_prefix!r}"
            )


def _portable_release_path_parts(value: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or ".." in path.parts
        or value != path.as_posix()
    ):
        raise StaticUIReleaseError(f"Release inventory path is unsafe: {value!r}")
    keys: list[str] = []
    for component in path.parts:
        if (
            component in {"", ".", ".."}
            or component.endswith((".", " "))
            or any(
                ord(character) < 32 or character in _WINDOWS_ILLEGAL_CHARACTERS
                for character in component
            )
        ):
            raise StaticUIReleaseError(
                f"Release inventory path is not cross-platform safe: {value!r}"
            )
        normalized = unicodedata.normalize("NFC", component.casefold())
        reserved_base = normalized.split(".", 1)[0]
        if reserved_base in _WINDOWS_RESERVED_COMPONENTS:
            raise StaticUIReleaseError(
                f"Release inventory uses a Windows-reserved path: {value!r}"
            )
        keys.append(normalized)
    return path.parts, tuple(keys)


def _assert_safe_bundled_path(
    path: Path,
    root: Path,
    root_resolved: Path,
) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if not absolute.is_relative_to(root):
        raise StaticUIReleaseError(
            f"Bundled static UI path escapes its lexical root: {absolute}"
        )
    current = root
    if _path_is_alias(current):
        raise StaticUIReleaseError(
            f"Bundled static UI paths cannot contain aliases: {current}"
        )
    for part in absolute.relative_to(root).parts:
        current /= part
        if _path_is_alias(current):
            raise StaticUIReleaseError(
                f"Bundled static UI paths cannot contain aliases: {current}"
            )
    if not _resolve_path(absolute).is_relative_to(root_resolved):
        raise StaticUIReleaseError(
            f"Bundled static UI path resolves outside its root: {absolute}"
        )
    return absolute


def _assert_safe_bundled_regular_file(
    path: Path,
    root: Path,
    root_resolved: Path,
) -> Path:
    safe = _assert_safe_bundled_path(path, root, root_resolved)
    if not safe.is_file() or _path_is_alias(safe):
        raise StaticUIReleaseError(
            f"Bundled static UI input must be a regular file: {safe}"
        )
    return safe


def _load_manifest_json(payload: bytes) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"Invalid JSON numeric constant: {value}")

    return json.loads(
        payload.decode("utf-8"),
        parse_constant=reject_constant,
    )


def _manifest_files(value: Any) -> tuple[StaticUIFile, ...]:
    if not isinstance(value, list):
        raise StaticUIReleaseError("Static UI manifest files must be a list.")
    if len(value) > MAX_STATIC_UI_FILES:
        raise StaticUIReleaseError("Static UI manifest contains too many files.")
    files: list[StaticUIFile] = []
    portable_paths: dict[str, tuple[str, bool]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise StaticUIReleaseError("Static UI manifest contains an invalid file entry.")
        if set(item) != _MANIFEST_FILE_KEYS:
            raise StaticUIReleaseError(
                "Static UI manifest file entries must use the exact schema."
            )
        path = item.get("path")
        digest = item.get("sha256")
        size = item.get("size")
        if not isinstance(path, str) or not _safe_manifest_path(path):
            raise StaticUIReleaseError("Static UI manifest contains an unsafe file path.")
        if not isinstance(digest, str) or _LOWERCASE_SHA256.fullmatch(digest) is None:
            raise StaticUIReleaseError("Static UI manifest contains an invalid file hash.")
        if type(size) is not int or not 0 <= size <= MAX_STATIC_UI_FILE_SIZE:
            raise StaticUIReleaseError("Static UI manifest contains an invalid file size.")
        _register_portable_release_path(
            portable_paths,
            path,
            is_directory=False,
        )
        files.append(StaticUIFile(path=path, sha256=digest, size=size))
    result = tuple(sorted(files, key=lambda file: file.path))
    if len({file.path for file in result}) != len(result):
        raise StaticUIReleaseError("Static UI manifest contains duplicate file paths.")
    return result


def _safe_manifest_path(value: str) -> bool:
    if value == MANIFEST_NAME:
        return False
    try:
        _portable_release_path_parts(value)
    except StaticUIReleaseError:
        return False
    return True


def _validate_manifest_header(payload: Any, source_fingerprint: str) -> None:
    if not isinstance(payload, dict):
        raise StaticUIReleaseError("Static UI manifest root must be an object.")
    if set(payload) != _MANIFEST_KEYS:
        raise StaticUIReleaseError("Static UI manifest must use the exact schema.")
    if (
        type(payload.get("format_version")) is not int
        or payload.get("format_version") != MANIFEST_VERSION
    ):
        raise StaticUIReleaseError("Static UI manifest format version is unsupported.")
    manifest_source = payload.get("source_fingerprint")
    if (
        not isinstance(manifest_source, str)
        or _LOWERCASE_SHA256.fullmatch(manifest_source) is None
        or manifest_source != source_fingerprint
    ):
        raise StaticUIReleaseError(
            "Static UI export is stale: frontend sources changed after it was built."
        )
    expected_build_id = _expected_build_id(source_fingerprint)
    expected_contract = _effective_build_contract(source_fingerprint)
    contract = payload.get("build_contract")
    if (
        not isinstance(contract, dict)
        or set(contract) != set(expected_contract)
        or any(not isinstance(value, str) for value in contract.values())
        or contract != expected_contract
    ):
        raise StaticUIReleaseError("Static UI manifest build contract is invalid.")
    if not isinstance(payload.get("build_id"), str) or payload.get(
        "build_id"
    ) != expected_build_id:
        raise StaticUIReleaseError(
            "Static UI manifest does not attest the expected source-derived build ID."
        )
    if not isinstance(payload.get("index_marker"), str) or payload.get(
        "index_marker"
    ) != INDEX_MARKER:
        raise StaticUIReleaseError("Static UI manifest index marker is invalid.")
    for name in ("export_fingerprint", "index_sha256"):
        value = payload.get(name)
        if not isinstance(value, str) or _LOWERCASE_SHA256.fullmatch(value) is None:
            raise StaticUIReleaseError(
                f"Static UI manifest {name} must be a lowercase SHA-256 digest."
            )


def _validate_index(
    index: Path,
    boundary: _ReleaseBoundary,
    expected_build_id: str,
) -> str:
    payload = _read_safe_bytes(index, boundary)
    _validate_index_payload(payload, expected_build_id)
    return hashlib.sha256(payload).hexdigest()


def _validate_index_payload(payload: bytes, expected_build_id: str) -> None:
    if INDEX_MARKER.encode("utf-8") not in payload:
        raise StaticUIReleaseError(
            f"Static UI index.html must contain the {INDEX_MARKER!r} release marker."
        )
    static_ui_manifest_asset_paths(payload, expected_build_id)


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
    files = _export_files(boundary)
    export = boundary.frontend / "out"

    def read_file(relative: str) -> bytes:
        return _read_safe_bytes(
            export.joinpath(*PurePosixPath(relative).parts),
            boundary,
        )

    _validate_next_manifest_file_pair(files, expected_build_id, read_file)
    _validate_observed_build_ids(files, expected_build_id, read_file)
    _ensure_next_manifest_references(boundary, expected_build_id)
    _validate_export_structure(boundary, expected_build_id)
    attestation = _assert_safe_path(
        boundary.frontend / "out" / BUILD_ID_ATTESTATION_NAME,
        boundary,
    )
    try:
        attestation.write_text(expected_build_id, encoding="utf-8")
    except OSError as exc:
        raise StaticUIReleaseError("Could not write the static UI build ID attestation.") from exc
    _validate_export_structure(boundary, expected_build_id)


def _ensure_next_manifest_references(
    boundary: _ReleaseBoundary,
    expected_build_id: str,
) -> None:
    index = boundary.frontend / "out" / "index.html"
    payload = _read_safe_bytes(index, boundary)
    parser = _parse_release_index(payload)
    if parser.has_base:
        raise StaticUIReleaseError(
            "Static UI index.html cannot use a base URL for release assets."
        )
    reserved_sources = [
        source
        for source in parser.sources
        if any(name in source.casefold() for name in _NEXT_MANIFEST_NAMES_CASEFOLDED)
    ]
    if reserved_sources:
        static_ui_manifest_asset_paths(payload, expected_build_id)
        return

    text = payload.decode("utf-8")
    closing_bodies = list(re.finditer(r"</body\s*>", text, flags=re.IGNORECASE))
    if len(closing_bodies) != 1:
        raise StaticUIReleaseError(
            "Static UI index.html must contain exactly one closing body tag for "
            "build-manifest attestations."
        )
    insertion = closing_bodies[0].start()
    tags = "".join(
        f'<script src="/_next/static/{expected_build_id}/{name}"></script>'
        for name in _NEXT_MANIFEST_NAMES
    )
    updated = (text[:insertion] + tags + text[insertion:]).encode("utf-8")
    temporary = _assert_safe_path(
        boundary.frontend / "out" / ".careerpilot-index.html.tmp",
        boundary,
    )
    try:
        temporary.write_bytes(updated)
        os.replace(temporary, index)
    except OSError as exc:
        raise StaticUIReleaseError(
            "Could not write static UI build-manifest attestations."
        ) from exc


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
    files = _export_files(boundary)
    export = boundary.frontend / "out"

    def read_file(relative: str) -> bytes:
        return _read_safe_bytes(
            export.joinpath(*PurePosixPath(relative).parts),
            boundary,
        )

    _validate_export_inventory(files, expected_build_id, read_file)


def _validate_export_inventory(
    files: tuple[StaticUIFile, ...],
    expected_build_id: str,
    read_file: Callable[[str], bytes],
) -> None:
    _validate_next_manifest_file_pair(files, expected_build_id, read_file)
    files_by_path = {file.path: file for file in files}
    actual_paths = set(files_by_path)
    html_paths = tuple(
        file.path
        for file in files
        if PurePosixPath(file.path).suffix.casefold() == ".html"
    )
    if "index.html" not in html_paths:
        raise StaticUIReleaseError("Static UI export is missing index.html.")
    if len(html_paths) > MAX_STATIC_UI_HTML_FILES:
        raise StaticUIReleaseError(
            "Static UI export contains too many HTML pages for bounded verification."
        )
    manifest_assets = {
        f"_next/static/{expected_build_id}/{name}" for name in _NEXT_MANIFEST_NAMES
    }
    all_referenced_assets: set[str] = set()
    for html_path in html_paths:
        html_payload = read_file(html_path)
        if html_path == "index.html":
            _validate_index_payload(html_payload, expected_build_id)
        referenced_assets = set(
            static_ui_frontend_asset_paths(
                html_payload,
                document_path=html_path,
            )
        )
        missing_assets = sorted(referenced_assets - actual_paths)
        if missing_assets:
            raise StaticUIReleaseError(
                f"Static UI {html_path} references files outside the manifested export: "
                + ", ".join(missing_assets)
            )
        if not referenced_assets - manifest_assets:
            raise StaticUIReleaseError(
                f"Static UI {html_path} must reference at least one non-manifest "
                "JS or CSS asset."
            )
        empty_assets = sorted(
            path for path in referenced_assets if files_by_path[path].size == 0
        )
        if empty_assets:
            raise StaticUIReleaseError(
                f"Static UI {html_path} executable and stylesheet assets must be "
                "nonempty: " + ", ".join(empty_assets)
            )
        all_referenced_assets.update(referenced_assets)
    if len(all_referenced_assets) > MAX_STATIC_UI_REFERENCED_ASSETS:
        raise StaticUIReleaseError(
            "Static UI export references too many assets for bounded verification."
        )
    allowed_static_roots = {
        expected_build_id,
        *_NEXT_STATIC_CONTENT_DIRECTORIES,
    }
    unexpected_static_roots = sorted(
        {
            parts[2]
            for file in files
            if len(parts := PurePosixPath(file.path).parts) >= 3
            and parts[:2] == ("_next", "static")
            and parts[2] not in allowed_static_roots
        }
    )
    if unexpected_static_roots:
        raise StaticUIReleaseError(
            "Static UI export contains unknown first-level _next/static directories: "
            + ", ".join(unexpected_static_roots)
        )
    _validate_observed_build_ids(files, expected_build_id, read_file)


def _validate_next_manifest_file_pair(
    files: tuple[StaticUIFile, ...],
    expected_build_id: str,
    read_file: Callable[[str], bytes],
) -> None:
    expected_paths = {
        f"_next/static/{expected_build_id}/{name}" for name in _NEXT_MANIFEST_NAMES
    }
    candidates = [
        file.path
        for file in files
        if PurePosixPath(file.path).name.casefold()
        in _NEXT_MANIFEST_NAMES_CASEFOLDED
    ]
    if len(candidates) != 2 or set(candidates) != expected_paths:
        raise StaticUIReleaseError(
            "Static UI export must contain exactly one source-derived Next "
            "build-manifest pair and no legacy, generic, or case-variant copies."
        )
    for name in _NEXT_MANIFEST_NAMES:
        path = f"_next/static/{expected_build_id}/{name}"
        _validate_next_manifest_payload(name, read_file(path))


def _validate_next_manifest_payload(name: str, payload: bytes) -> None:
    marker = _NEXT_MANIFEST_MARKERS[name]
    other_markers = set(_NEXT_MANIFEST_MARKERS.values()) - {marker}
    if not payload or len(payload) > _MAX_NEXT_MANIFEST_SIZE or b"\0" in payload:
        raise StaticUIReleaseError(
            f"Static UI {name} must be bounded, nonempty JavaScript."
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StaticUIReleaseError(
            f"Static UI {name} must be valid UTF-8 JavaScript."
        ) from exc
    exact_marker = re.compile(
        rf"(?<![A-Za-z0-9_$]){re.escape(marker)}(?![A-Za-z0-9_$])"
    )
    if len(exact_marker.findall(text)) != 1 or any(
        re.search(
            rf"(?<![A-Za-z0-9_$]){re.escape(other)}(?![A-Za-z0-9_$])",
            text,
        )
        for other in other_markers
    ):
        raise StaticUIReleaseError(
            f"Static UI {name} does not contain its unique expected manifest marker."
        )
    prefix = re.compile(
        rf"\A\s*self\s*\.\s*{re.escape(marker)}\s*=\s*"
        + (r"" if name == "_buildManifest.js" else r"new\s+Set\s*\(")
    )
    prefix_match = prefix.match(text)
    try:
        value, end = json.JSONDecoder().raw_decode(
            text,
            prefix_match.end() if prefix_match is not None else 0,
        )
    except (json.JSONDecodeError, ValueError):
        value = None
        end = 0
    expected_type = dict if name == "_buildManifest.js" else list
    suffix = re.compile(
        (r"\s*" if name == "_buildManifest.js" else r"\s*\)\s*")
        + rf";\s*(?:self\s*\.\s*{re.escape(marker)}_CB\s*&&\s*"
        rf"self\s*\.\s*{re.escape(marker)}_CB\s*\(\s*\)\s*;?)?\s*\Z"
    )
    if (
        prefix_match is None
        or type(value) is not expected_type
        or suffix.fullmatch(text, end) is None
    ):
        raise StaticUIReleaseError(
            f"Static UI {name} does not have the expected executable manifest identity."
        )


def _validate_observed_build_ids(
    files: tuple[StaticUIFile, ...],
    expected_build_id: str,
    read_file: Callable[[str], bytes],
) -> None:
    observed: set[bytes] = set()
    for file in files:
        observed.update(_BUILD_ID_PATTERN.findall(file.path.encode("utf-8")))
        observed.update(_BUILD_ID_PATTERN.findall(read_file(file.path)))
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
