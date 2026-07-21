from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

import career_companion.static_ui_release as static_release
from career_companion.static_ui_release import (
    BUILD_ID_ATTESTATION_NAME,
    BUILD_CONTRACT,
    MANIFEST_NAME,
    StaticUIReleaseError,
    build_static_ui,
    frontend_source_fingerprint,
    stage_verified_static_ui,
    verify_bundled_static_ui,
    verify_sdist_static_ui,
    verify_static_ui,
    verify_wheel_static_ui,
    write_static_ui_manifest,
)


def _frontend(root: Path) -> Path:
    frontend = root / "frontend"
    (frontend / "app").mkdir(parents=True)
    (frontend / "app" / "page.tsx").write_text(
        "export default function Page() {}\n", encoding="utf-8"
    )
    (frontend / "package.json").write_text(
        '{"name":"release-test"}\n', encoding="utf-8"
    )
    (frontend / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n', encoding="utf-8"
    )
    (frontend / "next.config.ts").write_text("export default {};\n", encoding="utf-8")
    (frontend / "next-env.d.ts").write_text(
        "// generated before build\n", encoding="utf-8"
    )
    return frontend


def _expected_build_id(frontend: Path) -> str:
    fingerprint = frontend_source_fingerprint(frontend, project_root=frontend.parent)
    return f"careerpilot-{fingerprint}"


def _export(
    frontend: Path,
    *,
    build_id: str | None = None,
    attest: bool = True,
) -> str:
    build_id = build_id or _expected_build_id(frontend)
    build_directory = frontend / "out" / "_next" / "static" / build_id
    build_directory.mkdir(parents=True)
    (build_directory / "_buildManifest.js").write_text(
        f'self.__BUILD_MANIFEST={{"buildId":{json.dumps(build_id)}}};\n',
        encoding="utf-8",
    )
    (build_directory / "_ssgManifest.js").write_text(
        "self.__SSG_MANIFEST=new Set([]);\n", encoding="utf-8"
    )
    chunks = frontend / "out" / "_next" / "static" / "chunks"
    chunks.mkdir()
    (chunks / "release.js").write_text(
        "self.__CAREERPILOT_RELEASE=true;\n", encoding="utf-8"
    )
    (chunks / "workspace.js").write_text(
        "self.__CAREERPILOT_WORKSPACE=true;\n", encoding="utf-8"
    )
    (frontend / "out" / "index.html").write_text(
        f"<title>CareerPilot</title><meta content='{build_id}'>"
        '<script src="/_next/static/chunks/release.js"></script>'
        f'<script src="/_next/static/{build_id}/_buildManifest.js"></script>'
        f'<script src="/_next/static/{build_id}/_ssgManifest.js"></script>\n',
        encoding="utf-8",
    )
    workspace = frontend / "out" / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_text(
        "<title>Workspace</title>"
        '<link rel="preload" as="script" '
        'href="/_next/static/chunks/workspace.js">'
        '<script src="/_next/static/chunks/workspace.js"></script>\n',
        encoding="utf-8",
    )
    (frontend / "out" / "_next" / "app.js").write_text(
        "release bundle\n", encoding="utf-8"
    )
    if attest:
        (frontend / "out" / BUILD_ID_ATTESTATION_NAME).write_text(
            build_id, encoding="utf-8"
        )
    (frontend / "out" / ".DS_Store").write_bytes(b"junk")
    return build_id


def _write_manifest_references(
    frontend: Path,
    build_id: str,
    build_reference: str | None,
    ssg_reference: str | None,
    *,
    extra: str = "",
    include_representative: bool = True,
) -> None:
    references = "".join(
        f'<script src="{reference}"></script>'
        for reference in (build_reference, ssg_reference)
        if reference is not None
    )
    representative = (
        '<script src="/_next/static/chunks/release.js"></script>'
        if include_representative
        else ""
    )
    (frontend / "out" / "index.html").write_text(
        f"<title>CareerPilot</title><meta content='{build_id}'>"
        f"{representative}{references}{extra}\n",
        encoding="utf-8",
    )


def _copy_project(repository: Path, project: Path, *, frontend: bool = True) -> None:
    project.mkdir()
    for name in (
        "README.md",
        "alembic.ini",
        "pyproject.toml",
        "app",
        "career_companion",
        "job_pipeline",
        "agent-profile",
        "migrations",
        "scripts",
    ):
        source = repository / name
        destination = project / name
        if source.is_dir():
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, destination)
    if frontend:
        shutil.copytree(
            repository / "frontend",
            project / "frontend",
            ignore=shutil.ignore_patterns(
                "node_modules", ".next", "out", ".DS_Store", ".env", ".env.*"
            ),
        )


def _uv_environment() -> tuple[str, dict[str, str]]:
    uv = shutil.which("uv")
    assert uv is not None, "the package contract tests require the repository's uv tool"
    environment = os.environ.copy()
    environment["UV_OFFLINE"] = "1"
    return uv, environment


def _static_ui_archive_members(
    release: static_release.VerifiedStaticUI,
    *,
    prefix: str,
) -> list[tuple[str, bytes]]:
    members = [
        (f"{prefix}/{file.path}", (release.export / file.path).read_bytes())
        for file in release.files
    ]
    members.append((f"{prefix}/{MANIFEST_NAME}", release.manifest.read_bytes()))
    return members


def _write_test_sdist(
    path: Path,
    members: list[tuple[str, bytes]],
    *,
    special: tarfile.TarInfo | None = None,
) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        for name, payload in members:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        if special is not None:
            archive.addfile(special)


def _write_test_wheel(path: Path, members: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, mode="w") as archive:
        for name, payload in members:
            archive.writestr(name, payload)


def _release_operation(operation: str, frontend: Path, project: Path) -> None:
    if operation == "build":
        build_static_ui(frontend, project_root=project)
    elif operation == "write":
        write_static_ui_manifest(frontend, project_root=project)
    else:
        verify_static_ui(frontend, project_root=project)


def test_manifest_is_deterministic_and_ignores_only_generated_build_inputs(
    tmp_path: Path,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)

    first = write_static_ui_manifest(frontend, project_root=tmp_path)
    manifest = (frontend / "out" / MANIFEST_NAME).read_bytes()
    second = write_static_ui_manifest(frontend, project_root=tmp_path)

    assert manifest == (frontend / "out" / MANIFEST_NAME).read_bytes()
    assert first.export_fingerprint == second.export_fingerprint
    assert {file.path for file in first.files} == {
        BUILD_ID_ATTESTATION_NAME,
        "_next/app.js",
        "_next/static/chunks/release.js",
        "_next/static/chunks/workspace.js",
        f"_next/static/{first.build_id}/_buildManifest.js",
        f"_next/static/{first.build_id}/_ssgManifest.js",
        "index.html",
        "workspace/index.html",
    }

    # Next owns and may rewrite this generated type shim during a real build.
    before = frontend_source_fingerprint(frontend, project_root=tmp_path)
    (frontend / "next-env.d.ts").write_text("// generated after build\n")
    assert frontend_source_fingerprint(frontend, project_root=tmp_path) == before
    assert verify_static_ui(frontend, project_root=tmp_path).source_fingerprint == before

    # Capacitor owns and rewrites the generated native target and copied web bundle.
    (frontend / "ios" / "App" / "App" / "public").mkdir(parents=True)
    (frontend / "ios" / "App" / "App" / "public" / "index.html").write_text(
        "generated native bundle\n"
    )
    assert frontend_source_fingerprint(frontend, project_root=tmp_path) == before

    # Authored input remains freshness-sensitive.
    (frontend / "app" / "page.tsx").write_text("export default function Changed() {}\n")
    with pytest.raises(StaticUIReleaseError, match="frontend sources changed"):
        verify_static_ui(frontend, project_root=tmp_path)


def test_production_import_from_frontend_test_changes_fingerprint_and_build_id(
    tmp_path: Path,
) -> None:
    frontend = _frontend(tmp_path)
    helper = frontend / "test" / "release-helper.ts"
    helper.parent.mkdir()
    helper.write_text("export const releaseMarker = 'first';\n")
    (frontend / "app" / "page.tsx").write_text(
        'import { releaseMarker } from "../test/release-helper";\n'
        "export default function Page() { return releaseMarker; }\n"
    )

    first_fingerprint = frontend_source_fingerprint(
        frontend,
        project_root=tmp_path,
    )
    first_build_id = f"careerpilot-{first_fingerprint}"
    helper.write_text("export const releaseMarker = 'second';\n")
    second_fingerprint = frontend_source_fingerprint(
        frontend,
        project_root=tmp_path,
    )

    assert second_fingerprint != first_fingerprint
    assert f"careerpilot-{second_fingerprint}" != first_build_id


def test_verifier_rejects_missing_and_modified_exports(tmp_path: Path) -> None:
    frontend = _frontend(tmp_path)
    with pytest.raises(StaticUIReleaseError, match="export is missing"):
        verify_static_ui(frontend, project_root=tmp_path)

    _export(frontend)
    write_static_ui_manifest(frontend, project_root=tmp_path)
    (frontend / "out" / "_next" / "app.js").write_text("tampered\n")
    with pytest.raises(StaticUIReleaseError, match="export is stale"):
        verify_static_ui(frontend, project_root=tmp_path)


@pytest.mark.parametrize(
    "invalid_size",
    [True, False, 1.0, "1", -1, static_release.MAX_STATIC_UI_FILE_SIZE + 1],
)
def test_manifest_file_size_uses_strict_bounded_integer_schema(
    tmp_path: Path,
    invalid_size: object,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    one_byte = frontend / "out" / "one-byte.txt"
    one_byte.write_bytes(b"x")
    release = write_static_ui_manifest(frontend, project_root=tmp_path)
    payload = json.loads(release.manifest.read_text())
    entry = next(item for item in payload["files"] if item["path"] == "one-byte.txt")
    entry["size"] = invalid_size
    release.manifest.write_text(json.dumps(payload))

    with pytest.raises(StaticUIReleaseError, match="invalid file size"):
        verify_static_ui(frontend, project_root=tmp_path)


@pytest.mark.parametrize("invalid_hash", ["g" * 64, "A" * 64, "0" * 63, True])
def test_manifest_file_hash_requires_lowercase_sha256(
    tmp_path: Path,
    invalid_hash: object,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    release = write_static_ui_manifest(frontend, project_root=tmp_path)
    payload = json.loads(release.manifest.read_text())
    payload["files"][0]["sha256"] = invalid_hash
    release.manifest.write_text(json.dumps(payload))

    with pytest.raises(StaticUIReleaseError, match="invalid file hash"):
        verify_static_ui(frontend, project_root=tmp_path)


@pytest.mark.parametrize(
    ("field", "invalid_value", "error"),
    [
        ("format_version", True, "format version"),
        ("format_version", 3.0, "format version"),
        ("source_fingerprint", False, "frontend sources changed"),
        ("export_fingerprint", "A" * 64, "lowercase SHA-256"),
        ("index_sha256", "not-a-hash", "lowercase SHA-256"),
        ("index_marker", True, "index marker"),
        ("build_id", False, "build ID"),
        ("build_contract", True, "build contract"),
    ],
)
def test_manifest_header_rejects_json_type_coercions(
    tmp_path: Path,
    field: str,
    invalid_value: object,
    error: str,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    release = write_static_ui_manifest(frontend, project_root=tmp_path)
    payload = json.loads(release.manifest.read_text())
    payload[field] = invalid_value
    release.manifest.write_text(json.dumps(payload))

    with pytest.raises(StaticUIReleaseError, match=error):
        verify_static_ui(frontend, project_root=tmp_path)


@pytest.mark.parametrize("location", ["root", "file"])
def test_manifest_rejects_schema_extensions(location: str, tmp_path: Path) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    release = write_static_ui_manifest(frontend, project_root=tmp_path)
    payload = json.loads(release.manifest.read_text())
    if location == "root":
        payload["unexpected"] = False
    else:
        payload["files"][0]["unexpected"] = False
    release.manifest.write_text(json.dumps(payload))

    with pytest.raises(StaticUIReleaseError, match="exact schema"):
        verify_static_ui(frontend, project_root=tmp_path)


@pytest.mark.parametrize("attestation", [None, "careerpilot-" + "0" * 64])
def test_manifest_writer_requires_exact_source_derived_build_id(
    tmp_path: Path,
    attestation: str | None,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend, attest=False)
    if attestation is not None:
        (frontend / "out" / BUILD_ID_ATTESTATION_NAME).write_text(attestation)

    with pytest.raises(StaticUIReleaseError, match="build ID attestation"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


@pytest.mark.parametrize("attestation", [None, "careerpilot-" + "f" * 64])
def test_verifier_requires_exact_source_derived_build_id(
    tmp_path: Path,
    attestation: str | None,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    write_static_ui_manifest(frontend, project_root=tmp_path)
    path = frontend / "out" / BUILD_ID_ATTESTATION_NAME
    if attestation is None:
        path.unlink()
    else:
        path.write_text(attestation)

    with pytest.raises(StaticUIReleaseError, match="build ID attestation"):
        verify_static_ui(frontend, project_root=tmp_path)


def test_export_rejects_mixed_source_derived_build_ids(tmp_path: Path) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    stale_id = "careerpilot-" + "a" * 64
    (frontend / "out" / "mixed.js").write_text(f"stale={stale_id}\n")

    with pytest.raises(StaticUIReleaseError, match="stale, or mixed"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


@pytest.mark.parametrize("required_name", ["_buildManifest.js", "_ssgManifest.js"])
def test_export_requires_exact_build_id_artifacts(
    tmp_path: Path,
    required_name: str,
) -> None:
    frontend = _frontend(tmp_path)
    build_id = _export(frontend)
    (frontend / "out" / "_next" / "static" / build_id / required_name).unlink()

    with pytest.raises(StaticUIReleaseError, match="exactly one source-derived"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("_buildManifest.js", b""),
        ("_buildManifest.js", b"self.__BUILD_MANIFEST={"),
        ("_buildManifest.js", b"self.__BUILD_MANIFEST={};alert(1);"),
        ("_buildManifest.js", b"self.__SSG_MANIFEST=new Set([]);"),
        (
            "_buildManifest.js",
            b"self.__BUILD_MANIFEST={};self.__BUILD_MANIFEST={};",
        ),
        ("_ssgManifest.js", b"self.__SSG_MANIFEST={};"),
        ("_ssgManifest.js", b"self.__SSG_MANIFEST=new Set([]);alert(1);"),
        ("_ssgManifest.js", b"\xffself.__SSG_MANIFEST=new Set([]);"),
    ],
)
def test_manifest_writer_rejects_invalid_next_manifest_payload_identity(
    tmp_path: Path,
    name: str,
    payload: bytes,
) -> None:
    frontend = _frontend(tmp_path)
    build_id = _export(frontend)
    target = frontend / "out" / "_next" / "static" / build_id / name
    target.write_bytes(payload)

    with pytest.raises(
        StaticUIReleaseError,
        match="manifest marker|manifest identity|bounded|UTF-8",
    ):
        write_static_ui_manifest(frontend, project_root=tmp_path)


def test_verifier_revalidates_next_manifest_payload_identity(tmp_path: Path) -> None:
    frontend = _frontend(tmp_path)
    build_id = _export(frontend)
    write_static_ui_manifest(frontend, project_root=tmp_path)
    target = (
        frontend
        / "out"
        / "_next"
        / "static"
        / build_id
        / "_ssgManifest.js"
    )
    target.write_text("self.__SSG_MANIFEST={};")

    with pytest.raises(StaticUIReleaseError, match="manifest identity"):
        verify_static_ui(frontend, project_root=tmp_path)


def test_generic_manifest_pair_cannot_hide_behind_inert_expected_id(
    tmp_path: Path,
) -> None:
    frontend = _frontend(tmp_path)
    build_id = _export(frontend)
    generic = frontend / "out" / "_next" / "static" / "GENERIC-BUILD-MARKER"
    generic.mkdir()
    (generic / "_buildManifest.js").write_text("self.__BUILD_MANIFEST={};")
    (generic / "_ssgManifest.js").write_text("self.__SSG_MANIFEST=new Set([]);")
    _write_manifest_references(
        frontend,
        build_id,
        "/_next/static/GENERIC-BUILD-MARKER/_buildManifest.js",
        "/_next/static/GENERIC-BUILD-MARKER/_ssgManifest.js",
    )
    attestation = frontend / "out" / BUILD_ID_ATTESTATION_NAME
    before = attestation.read_bytes()

    with pytest.raises(StaticUIReleaseError, match="exactly one source-derived"):
        write_static_ui_manifest(frontend, project_root=tmp_path)

    assert attestation.read_bytes() == before
    assert not (frontend / "out" / MANIFEST_NAME).exists()


def test_build_rejects_generic_manifest_pair_before_writing_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = _frontend(tmp_path)
    npm = tmp_path / "npm"
    npm.write_text("fake\n")

    def fake_build(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        build_id = env["CAREERPILOT_BUILD_ID"]
        (frontend / ".next").mkdir()
        (frontend / ".next" / "BUILD_ID").write_text(build_id)
        _export(frontend, attest=False)
        generic = frontend / "out" / "_next" / "static" / "GENERIC-BUILD-MARKER"
        generic.mkdir()
        (generic / "_buildManifest.js").write_text("self.__BUILD_MANIFEST={};")
        (generic / "_ssgManifest.js").write_text("self.__SSG_MANIFEST=new Set([]);")
        _write_manifest_references(
            frontend,
            build_id,
            "/_next/static/GENERIC-BUILD-MARKER/_buildManifest.js",
            "/_next/static/GENERIC-BUILD-MARKER/_ssgManifest.js",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(shutil, "which", lambda _: str(npm))
    monkeypatch.setattr(subprocess, "run", fake_build)

    with pytest.raises(StaticUIReleaseError, match="exactly one source-derived"):
        build_static_ui(frontend, project_root=tmp_path)

    assert not (frontend / "out" / BUILD_ID_ATTESTATION_NAME).exists()
    assert not (frontend / "out" / MANIFEST_NAME).exists()


@pytest.mark.parametrize(
    "extra_path",
    [
        Path("legacy/_buildManifest.js"),
        Path("legacy/_ssgManifest.js"),
        Path("legacy/_BuildManifest.js"),
        Path("_next/static/GENERIC/_buildManifest.js"),
    ],
)
def test_export_rejects_extra_legacy_or_case_variant_manifests(
    tmp_path: Path,
    extra_path: Path,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    extra = frontend / "out" / extra_path
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("legacy manifest must not be packaged")
    before = (frontend / "out" / BUILD_ID_ATTESTATION_NAME).read_bytes()

    with pytest.raises(StaticUIReleaseError, match="exactly one source-derived"):
        write_static_ui_manifest(frontend, project_root=tmp_path)

    assert (frontend / "out" / BUILD_ID_ATTESTATION_NAME).read_bytes() == before


def test_index_rejects_missing_and_duplicate_manifest_references(tmp_path: Path) -> None:
    frontend = _frontend(tmp_path)
    build_id = _export(frontend)
    build_reference = f"/_next/static/{build_id}/_buildManifest.js"
    ssg_reference = f"/_next/static/{build_id}/_ssgManifest.js"
    before = (frontend / "out" / BUILD_ID_ATTESTATION_NAME).read_bytes()

    _write_manifest_references(frontend, build_id, build_reference, None)
    with pytest.raises(StaticUIReleaseError, match="exactly one source-derived"):
        write_static_ui_manifest(frontend, project_root=tmp_path)

    _write_manifest_references(
        frontend,
        build_id,
        build_reference,
        ssg_reference,
        extra=f'<script src="{build_reference}"></script>',
    )
    with pytest.raises(StaticUIReleaseError, match="exactly one source-derived"):
        write_static_ui_manifest(frontend, project_root=tmp_path)

    assert (frontend / "out" / BUILD_ID_ATTESTATION_NAME).read_bytes() == before


@pytest.mark.parametrize(
    "bad_reference",
    [
        "https://example.invalid/_next/static/{build_id}/_buildManifest.js",
        "_next/static/{build_id}/_buildManifest.js",
        "./_next/static/{build_id}/_buildManifest.js",
        "../_next/static/{build_id}/_buildManifest.js",
        "/_next/static/legacy-build/_buildManifest.js",
        "/_next/static/{build_id}/%5fbuildManifest.js",
    ],
)
def test_index_rejects_external_traversal_stale_and_malformed_references(
    tmp_path: Path,
    bad_reference: str,
) -> None:
    frontend = _frontend(tmp_path)
    build_id = _export(frontend)
    before = (frontend / "out" / BUILD_ID_ATTESTATION_NAME).read_bytes()
    _write_manifest_references(
        frontend,
        build_id,
        bad_reference.format(build_id=build_id),
        f"/_next/static/{build_id}/_ssgManifest.js",
    )

    with pytest.raises(
        StaticUIReleaseError,
        match="asset reference|manifest reference|manifest pair",
    ):
        write_static_ui_manifest(frontend, project_root=tmp_path)

    assert (frontend / "out" / BUILD_ID_ATTESTATION_NAME).read_bytes() == before


def test_index_accepts_root_absolute_references_and_queries(
    tmp_path: Path,
) -> None:
    frontend = _frontend(tmp_path)
    build_id = _export(frontend)
    _write_manifest_references(
        frontend,
        build_id,
        f"/_next/static/{build_id}/_buildManifest.js?release=1",
        f"/_next/static/{build_id}/_ssgManifest.js?v=1",
    )

    release = write_static_ui_manifest(frontend, project_root=tmp_path)

    assert verify_static_ui(frontend, project_root=tmp_path) == release


@pytest.mark.parametrize("variant", ["base", "inert-type", "template"])
def test_index_manifest_references_must_be_directly_executable(
    tmp_path: Path,
    variant: str,
) -> None:
    frontend = _frontend(tmp_path)
    build_id = _export(frontend)
    build_reference = f"/_next/static/{build_id}/_buildManifest.js"
    ssg_reference = f"/_next/static/{build_id}/_ssgManifest.js"
    if variant == "base":
        markup = (
            '<base href="https://example.invalid/">'
            f'<script src="{build_reference}"></script>'
            f'<script src="{ssg_reference}"></script>'
        )
    elif variant == "inert-type":
        markup = (
            f'<script type="application/json" src="{build_reference}"></script>'
            f'<script src="{ssg_reference}"></script>'
        )
    else:
        markup = (
            f'<template><script src="{build_reference}"></script></template>'
            f'<script src="{ssg_reference}"></script>'
        )
    (frontend / "out" / "index.html").write_text(
        f"<title>CareerPilot</title><meta content='{build_id}'>{markup}"
    )
    before = (frontend / "out" / BUILD_ID_ATTESTATION_NAME).read_bytes()

    with pytest.raises(StaticUIReleaseError, match="executable|base URL|ambiguous"):
        write_static_ui_manifest(frontend, project_root=tmp_path)

    assert (frontend / "out" / BUILD_ID_ATTESTATION_NAME).read_bytes() == before


@pytest.mark.parametrize("element", ["script", "link"])
@pytest.mark.parametrize("unsafe_first", [False, True])
def test_index_rejects_duplicate_asset_url_attributes_in_any_order(
    tmp_path: Path,
    element: str,
    unsafe_first: bool,
) -> None:
    frontend = _frontend(tmp_path)
    build_id = _export(frontend)
    if element == "script":
        safe = "/_next/static/chunks/release.js"
        unsafe = "https://example.invalid/unsafe.js"
        first, second = (unsafe, safe) if unsafe_first else (safe, unsafe)
        duplicate = f'<script src="{first}" SRC="{second}"></script>'
    else:
        css = frontend / "out" / "_next" / "static" / "css" / "release.css"
        css.parent.mkdir()
        css.write_text("body{color:inherit}")
        safe = "/_next/static/css/release.css"
        unsafe = "https://example.invalid/unsafe.css"
        first, second = (unsafe, safe) if unsafe_first else (safe, unsafe)
        duplicate = f'<link rel="stylesheet" href="{first}" HREF="{second}">'
    index = frontend / "out" / "index.html"
    index.write_text(index.read_text() + duplicate)

    with pytest.raises(StaticUIReleaseError, match="ambiguous"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


@pytest.mark.parametrize(
    "duplicate",
    [
        '<script src="/_next/static/chunks/release.js" '
        'type="application/json" TYPE="text/javascript"></script>',
        '<script src="/_next/static/chunks/release.js" '
        'integrity="sha256-first" INTEGRITY="sha256-last"></script>',
        '<link href="/_next/static/css/release.css" '
        'rel="preload" REL="stylesheet">',
        '<link href="/_next/static/css/release.css" '
        'integrity="sha256-first" Integrity="sha256-last" rel="stylesheet">',
    ],
)
def test_index_rejects_other_duplicate_security_attributes(
    tmp_path: Path,
    duplicate: str,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    css = frontend / "out" / "_next" / "static" / "css" / "release.css"
    css.parent.mkdir()
    css.write_text("body{color:inherit}")
    index = frontend / "out" / "index.html"
    index.write_text(index.read_text() + duplicate)

    with pytest.raises(StaticUIReleaseError, match="ambiguous"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


@pytest.mark.parametrize(
    "ambiguous",
    [
        "<template><noscript></template></noscript>",
        "</template>",
        "<template>unclosed",
        '<script src="/_next/static/chunks/release.js">',
        '<script src="/_next/static/chunks/release.js"/>',
    ],
)
def test_index_rejects_misnested_or_incomplete_security_markup(
    tmp_path: Path,
    ambiguous: str,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    index = frontend / "out" / "index.html"
    index.write_text(index.read_text() + ambiguous)

    with pytest.raises(StaticUIReleaseError, match="ambiguous|misnested"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


def test_index_accepts_well_nested_inert_markup_without_assets(tmp_path: Path) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    index = frontend / "out" / "index.html"
    index.write_text(
        index.read_text()
        + "<template><p>inert release note</p></template><noscript>Enable JS</noscript>"
    )

    release = write_static_ui_manifest(frontend, project_root=tmp_path)

    assert verify_static_ui(frontend, project_root=tmp_path) == release


@pytest.mark.parametrize(
    "script_type",
    [
        "",
        " \t\n\f\r ",
        " module ",
        "MoDuLe",
        "\ttext/javascript\n",
        " text/javascript ; charset=utf-8 ",
        "APPLICATION/JAVASCRIPT;charset=UTF-8",
        "APPLICATION/JAVASCRIPT",
        *sorted(static_release._JAVASCRIPT_MIME_TYPES),
    ],
)
@pytest.mark.parametrize("external", [False, True])
def test_browser_executable_script_types_always_validate_their_urls(
    tmp_path: Path,
    script_type: str,
    external: bool,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    source = (
        "https://example.invalid/browser-active.js"
        if external
        else "/_next/static/chunks/release.js"
    )
    index = frontend / "out" / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8")
        + f'<script type="{script_type}" src="{source}"></script>',
        encoding="utf-8",
    )

    if external:
        with pytest.raises(StaticUIReleaseError, match="local export paths"):
            write_static_ui_manifest(frontend, project_root=tmp_path)
    else:
        release = write_static_ui_manifest(frontend, project_root=tmp_path)
        assert verify_static_ui(frontend, project_root=tmp_path) == release


@pytest.mark.parametrize("script_type", ["\u00a0", "\u2003text/javascript", "module\u202f"])
@pytest.mark.parametrize("external", [False, True])
def test_script_type_rejects_unicode_whitespace_ambiguity(
    tmp_path: Path,
    script_type: str,
    external: bool,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    source = (
        "https://example.invalid/ambiguous.js"
        if external
        else "/_next/static/chunks/release.js"
    )
    index = frontend / "out" / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8")
        + f'<script type="{script_type}" src="{source}"></script>',
        encoding="utf-8",
    )

    with pytest.raises(StaticUIReleaseError, match="ambiguous"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


@pytest.mark.parametrize(
    "rel",
    [
        "stylesheet",
        " StyleSheet ",
        "\talternate\nstylesheet\f",
        "stylesheet alternate",
    ],
)
@pytest.mark.parametrize("external", [False, True])
def test_stylesheet_rel_uses_html_ascii_tokenization_and_validates_urls(
    tmp_path: Path,
    rel: str,
    external: bool,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    css = frontend / "out" / "_next" / "static" / "css" / "release.css"
    css.parent.mkdir()
    css.write_text("body{color:inherit}")
    href = (
        "https://example.invalid/browser-active.css"
        if external
        else "/_next/static/css/release.css"
    )
    index = frontend / "out" / "index.html"
    index.write_text(index.read_text() + f'<link rel="{rel}" href="{href}">')

    if external:
        with pytest.raises(StaticUIReleaseError, match="local export paths"):
            write_static_ui_manifest(frontend, project_root=tmp_path)
    else:
        release = write_static_ui_manifest(frontend, project_root=tmp_path)
        assert verify_static_ui(frontend, project_root=tmp_path) == release


@pytest.mark.parametrize("rel", ["stylesheet\u00a0alternate", "\u2003stylesheet"])
def test_stylesheet_rel_rejects_unicode_whitespace_ambiguity(
    tmp_path: Path,
    rel: str,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    index = frontend / "out" / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8")
        + f'<link rel="{rel}" href="https://example.invalid/ambiguous.css">',
        encoding="utf-8",
    )

    with pytest.raises(StaticUIReleaseError, match="ambiguous"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


@pytest.mark.parametrize(
    "markup",
    [
        '<link rel="preload" as="script" href="https://example.invalid/x.js">',
        '<link rel="modulepreload" href="//example.invalid/x.js">',
        '<link rel="preload" as="style" href="../_next/static/css/x.css">',
        '<link rel="preload" as="script" href="/_next/static/chunks/%78.js">',
    ],
)
def test_preload_urls_receive_local_containment_validation(
    tmp_path: Path,
    markup: str,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    index = frontend / "out" / "index.html"
    index.write_text(index.read_text(encoding="utf-8") + markup, encoding="utf-8")

    with pytest.raises(StaticUIReleaseError, match="asset reference|local export"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


@pytest.mark.parametrize(
    "markup",
    [
        '<link rel="preload" as="script">',
        '<link rel="preload" as="script" href>',
        '<link rel="preload" as="script" href="">',
        '<link rel="preload" href="/_next/static/chunks/release.js">',
        '<link rel="preload" as href="/_next/static/chunks/release.js">',
        '<link rel="preload" as="" href="/_next/static/chunks/release.js">',
        '<link rel="preload" as="font" href="/_next/static/chunks/release.js">',
        '<link rel="modulepreload" href="">',
        '<link rel="modulepreload" as="" href="/_next/static/chunks/release.js">',
        '<link rel="preload" REL="stylesheet" as="script" '
        'href="/_next/static/chunks/release.js">',
        '<link rel="preload" as="script" AS="style" '
        'href="/_next/static/chunks/release.js">',
        '<link rel="preload" as="script" href="/_next/static/chunks/release.js" '
        'HREF="https://example.invalid/x.js">',
        '<link rel="preload\u00a0stylesheet" as="script" '
        'href="/_next/static/chunks/release.js">',
        '<link rel="preload" as="\u2003script" '
        'href="/_next/static/chunks/release.js">',
    ],
)
def test_preload_markup_fails_closed_when_missing_or_ambiguous(
    tmp_path: Path,
    markup: str,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    index = frontend / "out" / "index.html"
    index.write_text(index.read_text(encoding="utf-8") + markup, encoding="utf-8")

    with pytest.raises(StaticUIReleaseError, match="ambiguous"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


@pytest.mark.parametrize(
    "markup",
    [
        '<link rel=" preload " as=" script " '
        'href="/_next/static/chunks/release.js">',
        '<link rel="modulepreload" href="/_next/static/chunks/release.js">',
        '<link rel="MODULEPRELOAD" as="SCRIPT" '
        'href="/_next/static/chunks/release.js">',
    ],
)
def test_valid_local_next_preloads_are_inventoried(
    tmp_path: Path,
    markup: str,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    index = frontend / "out" / "index.html"
    index.write_text(index.read_text() + markup)

    release = write_static_ui_manifest(frontend, project_root=tmp_path)

    assert verify_static_ui(frontend, project_root=tmp_path) == release


def test_route_only_preload_asset_is_required(tmp_path: Path) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    workspace = frontend / "out" / "workspace" / "index.html"
    workspace.write_text(
        '<link rel="preload" as="script" '
        'href="/_next/static/chunks/preload-only.js">'
        '<script src="/_next/static/chunks/workspace.js"></script>'
    )

    with pytest.raises(
        StaticUIReleaseError,
        match="workspace/index.html references files outside",
    ):
        write_static_ui_manifest(frontend, project_root=tmp_path)


@pytest.mark.parametrize(
    "script",
    [
        "<script src></script>",
        '<script src=""></script>',
        '<script src=" \t"></script>',
        '<script src="\n\f\r"></script>',
        '<script src="/_next/static/chunks/workspace.js" SRC=""></script>',
        '<script src="" SRC="/_next/static/chunks/workspace.js"></script>',
    ],
)
def test_route_rejects_valueless_empty_or_ambiguous_active_script_src(
    tmp_path: Path,
    script: str,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    workspace = frontend / "out" / "workspace" / "index.html"
    workspace.write_text(workspace.read_text() + script)

    with pytest.raises(
        StaticUIReleaseError,
        match="ambiguous|malformed|asset reference",
    ):
        write_static_ui_manifest(frontend, project_root=tmp_path)


@pytest.mark.parametrize("element", ["script", "link"])
@pytest.mark.parametrize(
    "integrity",
    ["", "not-a-digest", "sha256-bWlzbWF0Y2hlZA==", "sha512-malformed"],
)
def test_release_assets_fail_closed_on_integrity_attributes(
    tmp_path: Path,
    element: str,
    integrity: str,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    if element == "script":
        markup = (
            '<script src="/_next/static/chunks/release.js" '
            f'integrity="{integrity}"></script>'
        )
    else:
        css = frontend / "out" / "_next" / "static" / "css" / "release.css"
        css.parent.mkdir()
        css.write_text("body{color:inherit}")
        markup = (
            '<link rel="stylesheet" href="/_next/static/css/release.css" '
            f'integrity="{integrity}">'
        )
    index = frontend / "out" / "index.html"
    index.write_text(index.read_text() + markup)

    with pytest.raises(StaticUIReleaseError, match="cannot use integrity"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


@pytest.mark.parametrize("reference_generic_chunk", [False, True])
def test_export_rejects_unknown_first_level_static_directory(
    tmp_path: Path,
    reference_generic_chunk: bool,
) -> None:
    frontend = _frontend(tmp_path)
    build_id = _export(frontend)
    generic = frontend / "out" / "_next" / "static" / "GENERIC-BUILD-MARKER"
    generic.mkdir()
    (generic / "chunk.js").write_text("self.__STALE_GENERIC_CHUNK=true;")
    if reference_generic_chunk:
        index = frontend / "out" / "index.html"
        index.write_text(
            index.read_text()
            + '<script src="/_next/static/GENERIC-BUILD-MARKER/chunk.js"></script>'
        )
    before = (frontend / "out" / BUILD_ID_ATTESTATION_NAME).read_bytes()

    with pytest.raises(StaticUIReleaseError, match="unknown first-level"):
        write_static_ui_manifest(frontend, project_root=tmp_path)

    assert (frontend / "out" / BUILD_ID_ATTESTATION_NAME).read_bytes() == before


def test_index_rejects_missing_generic_chunk_reference(tmp_path: Path) -> None:
    frontend = _frontend(tmp_path)
    build_id = _export(frontend)
    index = frontend / "out" / "index.html"
    index.write_text(
        index.read_text()
        + '<script src="/_next/static/GENERIC-BUILD-MARKER/missing.js"></script>'
    )

    with pytest.raises(StaticUIReleaseError, match="outside the manifested export"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


@pytest.mark.parametrize(
    "representative",
    [
        None,
        "_next/static/chunks/release.js",
        "./_next/static/chunks/release.js",
        "../_next/static/chunks/release.js",
        "https://example.invalid/_next/static/chunks/release.js",
        "http://[::1",
        "/_next/static/chunks/%72elease.js",
        "\\_next\\static\\chunks\\release.js",
    ],
)
def test_index_requires_containment_safe_representative_asset(
    tmp_path: Path,
    representative: str | None,
) -> None:
    frontend = _frontend(tmp_path)
    build_id = _export(frontend)
    extra = (
        f'<script src="{representative}"></script>'
        if representative is not None
        else ""
    )
    _write_manifest_references(
        frontend,
        build_id,
        f"/_next/static/{build_id}/_buildManifest.js",
        f"/_next/static/{build_id}/_ssgManifest.js",
        extra=extra,
        include_representative=False,
    )

    with pytest.raises(StaticUIReleaseError, match="asset reference|non-manifest"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


@pytest.mark.parametrize("kind", ["script", "stylesheet"])
def test_index_rejects_mistyped_representative_asset_suffix(
    tmp_path: Path,
    kind: str,
) -> None:
    frontend = _frontend(tmp_path)
    build_id = _export(frontend)
    mistyped = frontend / "out" / "_next" / "static" / "chunks" / "release.txt"
    mistyped.write_text("not an executable or stylesheet suffix")
    if kind == "script":
        extra = '<script src="/_next/static/chunks/release.txt"></script>'
    else:
        extra = (
            '<link rel="stylesheet" href="/_next/static/chunks/release.txt">'
        )
    _write_manifest_references(
        frontend,
        build_id,
        f"/_next/static/{build_id}/_buildManifest.js",
        f"/_next/static/{build_id}/_ssgManifest.js",
        extra=extra,
        include_representative=False,
    )

    with pytest.raises(StaticUIReleaseError, match="must end with"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


def test_index_rejects_empty_representative_asset(tmp_path: Path) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    (frontend / "out" / "_next" / "static" / "chunks" / "release.js").write_bytes(
        b""
    )

    with pytest.raises(StaticUIReleaseError, match="must be nonempty"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


def test_manifest_writer_validates_route_only_asset_inventory(tmp_path: Path) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    (frontend / "out" / "_next" / "static" / "chunks" / "workspace.js").unlink()

    with pytest.raises(
        StaticUIReleaseError,
        match="workspace/index.html references files outside",
    ):
        write_static_ui_manifest(frontend, project_root=tmp_path)


def test_fallback_html_rejects_relative_assets_at_deep_request_urls(
    tmp_path: Path,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    (frontend / "out" / "404.html").write_text(
        '<script src="_next/static/chunks/release.js"></script>'
    )

    with pytest.raises(StaticUIReleaseError, match="root-absolute"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


def test_verifier_rejects_tampered_route_only_asset(tmp_path: Path) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    write_static_ui_manifest(frontend, project_root=tmp_path)
    route_asset = frontend / "out" / "_next" / "static" / "chunks" / "workspace.js"
    route_asset.write_text("self.__TAMPERED_WORKSPACE=true;\n")

    with pytest.raises(StaticUIReleaseError, match="export is stale"):
        verify_static_ui(frontend, project_root=tmp_path)


@pytest.mark.parametrize("mutation", ["tamper", "missing", "extra"])
def test_bundled_verifier_recomputes_full_file_inventory(
    tmp_path: Path,
    mutation: str,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    release = write_static_ui_manifest(frontend, project_root=tmp_path)
    route_asset = frontend / "out" / "_next" / "static" / "chunks" / "workspace.js"
    if mutation == "tamper":
        route_asset.write_text("self.__TAMPERED_WORKSPACE=true;\n")
    elif mutation == "missing":
        route_asset.unlink()
    else:
        (frontend / "out" / "unexpected-route-chunk.js").write_text("rogue\n")

    with pytest.raises(StaticUIReleaseError, match="membership or content"):
        verify_bundled_static_ui(
            frontend / "out",
            expected_export_fingerprint=release.export_fingerprint,
        )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("_next/static/chunks/Route.js", "_next/static/chunks/route.js"),
        ("Workspace/index.html", "workspace/index.html"),
        ("_next/static/media/caf\u00e9.png", "_next/static/media/cafe\u0301.png"),
    ],
)
def test_release_inventory_rejects_case_and_unicode_path_collisions(
    first: str,
    second: str,
) -> None:
    registry: dict[str, tuple[str, bool]] = {}
    static_release._register_portable_release_path(
        registry,
        first,
        is_directory=False,
    )

    with pytest.raises(StaticUIReleaseError, match="non-portable path collision"):
        static_release._register_portable_release_path(
            registry,
            second,
            is_directory=False,
        )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("_next/static/chunks/Collision.js", "_next/static/chunks/collision.js"),
        ("Pages/route.html", "pages/route.html"),
        ("_next/static/media/caf\u00e9.png", "_next/static/media/cafe\u0301.png"),
    ],
)
def test_manifest_writer_rejects_physical_portability_collisions_when_supported(
    tmp_path: Path,
    first: str,
    second: str,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    first_path = frontend / "out" / first
    second_path = frontend / "out" / second
    first_path.parent.mkdir(parents=True, exist_ok=True)
    second_path.parent.mkdir(parents=True, exist_ok=True)
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    if first_path.samefile(second_path):
        registry: dict[str, tuple[str, bool]] = {}
        static_release._register_portable_release_path(
            registry,
            first,
            is_directory=False,
        )
        with pytest.raises(StaticUIReleaseError, match="non-portable path collision"):
            static_release._register_portable_release_path(
                registry,
                second,
                is_directory=False,
            )
        return

    with pytest.raises(StaticUIReleaseError, match="non-portable path collision"):
        write_static_ui_manifest(frontend, project_root=tmp_path)


@pytest.mark.parametrize(
    "relative",
    [
        "_next/static/chunks/CON.js",
        "_next/static/chunks/trailing.",
        "_next/static/chunks/trailing ",
        "_next/static/chunks/alternate:data.js",
        "_next/static/chunks/question?.js",
    ],
)
def test_release_path_validator_rejects_nonportable_export_paths(
    tmp_path: Path,
    relative: str,
) -> None:
    with pytest.raises(
        StaticUIReleaseError,
        match="cross-platform safe|Windows-reserved",
    ):
        static_release._portable_release_path_parts(relative)
    if os.name == "nt":
        return

    frontend = _frontend(tmp_path)
    _export(frontend)
    target = frontend / "out" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("nonportable\n", encoding="utf-8")
    with pytest.raises(
        StaticUIReleaseError,
        match="cross-platform safe|Windows-reserved",
    ):
        write_static_ui_manifest(frontend, project_root=tmp_path)


@pytest.mark.parametrize("next_build_id", [None, "careerpilot-" + "b" * 64])
def test_build_rejects_missing_or_wrong_next_build_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    next_build_id: str | None,
) -> None:
    frontend = _frontend(tmp_path)
    npm = tmp_path / "npm"
    npm.write_text("fake\n")

    def fake_build(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        _export(frontend)
        (frontend / ".next").mkdir()
        if next_build_id is not None:
            (frontend / ".next" / "BUILD_ID").write_text(next_build_id)
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(shutil, "which", lambda _: str(npm))
    monkeypatch.setattr(subprocess, "run", fake_build)

    with pytest.raises(StaticUIReleaseError, match="BUILD_ID"):
        build_static_ui(frontend, project_root=tmp_path)


@pytest.mark.parametrize(
    "relative",
    [
        Path(".env.production"),
        Path("config/.env.local"),
        Path(".ENV"),
        Path("config/.Env.production"),
    ],
)
def test_build_and_verify_reject_non_example_dotenv_anywhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: Path,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    write_static_ui_manifest(frontend, project_root=tmp_path)
    dotenv = frontend / relative
    dotenv.parent.mkdir(parents=True, exist_ok=True)
    dotenv.write_text("NEXT_PUBLIC_RELEASE_SENTINEL=dotenv-secret\n")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("npm must not run with a dotenv input"),
    )

    with pytest.raises(StaticUIReleaseError, match="non-example dotenv"):
        build_static_ui(frontend, project_root=tmp_path)
    with pytest.raises(StaticUIReleaseError, match="non-example dotenv"):
        verify_static_ui(frontend, project_root=tmp_path)


def test_case_insensitive_example_dotenv_is_allowed(tmp_path: Path) -> None:
    frontend = _frontend(tmp_path)
    (frontend / ".ENV.EXAMPLE").write_text("documented=true\n")
    _export(frontend)

    release = write_static_ui_manifest(frontend, project_root=tmp_path)

    assert verify_static_ui(frontend, project_root=tmp_path) == release


def test_build_scrubs_inherited_environment_before_fake_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = _frontend(tmp_path)
    sentinel = "inherited-release-sentinel-must-not-leak"
    monkeypatch.setenv("NEXT_PUBLIC_RELEASE_SENTINEL", sentinel)
    monkeypatch.setenv("NODE_OPTIONS", f"--require={sentinel}")
    monkeypatch.setenv("OPENAI_API_KEY", sentinel)
    monkeypatch.setenv("HOME", sentinel)
    monkeypatch.setenv("USERPROFILE", sentinel)
    monkeypatch.setenv("APPDATA", sentinel)
    monkeypatch.setenv("LOCALAPPDATA", sentinel)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("TEMP", r"C:\Temp")
    fake_npm = tmp_path / "node" / "npm.cmd"
    fake_npm.parent.mkdir()
    resolved_with_parent_environment = False
    captured: dict[str, str] = {}

    def fake_which(executable: str) -> str:
        nonlocal resolved_with_parent_environment
        assert executable == "npm"
        assert os.environ["USERPROFILE"] == sentinel
        resolved_with_parent_environment = True
        return str(fake_npm)

    def fake_build(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert command == [str(fake_npm.absolute()), "run", "build"]
        assert cwd == frontend
        assert check is True
        captured.update(env)
        leaked = env.get("NEXT_PUBLIC_RELEASE_SENTINEL", "clean")
        build_id = env["CAREERPILOT_BUILD_ID"]
        (frontend / ".next").mkdir(parents=True)
        (frontend / ".next" / "BUILD_ID").write_text(build_id)
        build_directory = frontend / "out" / "_next" / "static" / build_id
        build_directory.mkdir(parents=True)
        (build_directory / "_buildManifest.js").write_text(
            f'self.__BUILD_MANIFEST={{"buildId":{json.dumps(build_id)}}};'
        )
        (build_directory / "_ssgManifest.js").write_text(
            "self.__SSG_MANIFEST=new Set([]);"
        )
        chunks = frontend / "out" / "_next" / "static" / "chunks"
        chunks.mkdir()
        (chunks / "release.js").write_text("self.__CAREERPILOT_RELEASE=true;")
        (frontend / "out" / "index.html").write_text(
            "<html><body><title>CareerPilot</title>"
            f"{build_id}{leaked}"
            '<script src="/_next/static/chunks/release.js"></script>'
            "</body></html>\n"
        )
        (frontend / "out" / "_next" / "app.js").write_text(leaked)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(subprocess, "run", fake_build)
    release = build_static_ui(frontend, project_root=tmp_path)

    assert resolved_with_parent_environment
    inherited_names = {name.upper() for name in captured}
    assert "NEXT_PUBLIC_RELEASE_SENTINEL" not in inherited_names
    assert "NODE_OPTIONS" not in inherited_names
    assert "OPENAI_API_KEY" not in inherited_names
    assert "HOME" not in inherited_names
    assert "USERPROFILE" not in inherited_names
    assert "APPDATA" not in inherited_names
    assert "LOCALAPPDATA" not in inherited_names
    assert captured["COMSPEC"] == r"C:\Windows\System32\cmd.exe"
    assert captured["PATHEXT"] == ".COM;.EXE;.BAT;.CMD"
    assert captured["SYSTEMROOT"] == r"C:\Windows"
    assert captured["TEMP"] == r"C:\Temp"
    assert all(captured[name] == value for name, value in BUILD_CONTRACT.items())
    expected_contract = {
        **BUILD_CONTRACT,
        "CAREERPILOT_BUILD_ID": f"careerpilot-{release.source_fingerprint}",
    }
    assert captured["CAREERPILOT_BUILD_ID"] == expected_contract["CAREERPILOT_BUILD_ID"]
    manifest = json.loads(release.manifest.read_text())
    assert manifest["build_contract"] == expected_contract
    assert sentinel not in release.manifest.read_text()
    assert all(
        sentinel.encode() not in path.read_bytes()
        for path in release.export.rglob("*")
        if path.is_file()
    )


def test_build_inserts_manifest_references_without_casefold_index_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = _frontend(tmp_path)
    npm = tmp_path / "npm"
    npm.write_text("fake\n")
    unicode_marker = "Straße İSTANBUL"

    def fake_build(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        build_id = env["CAREERPILOT_BUILD_ID"]
        (frontend / ".next").mkdir()
        (frontend / ".next" / "BUILD_ID").write_text(build_id)
        _export(frontend, build_id=build_id, attest=False)
        (frontend / "out" / "index.html").write_text(
            f"<html><body>{unicode_marker}<title>CareerPilot</title>"
            '<script src="/_next/static/chunks/release.js"></script>'
            "</BODY></html>",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(shutil, "which", lambda _: str(npm))
    monkeypatch.setattr(subprocess, "run", fake_build)

    release = build_static_ui(frontend, project_root=tmp_path)
    index = (release.export / "index.html").read_text(encoding="utf-8")

    assert f"{unicode_marker}<title>" in index
    assert index.index("_buildManifest.js") < index.index("</BODY>")
    assert index.index("_ssgManifest.js") < index.index("</BODY>")
    assert verify_static_ui(frontend, project_root=tmp_path) == release


@pytest.mark.skipif(os.name != "nt", reason="exercises the Windows .cmd launcher")
def test_windows_cmd_launcher_runs_with_minimal_scrubbed_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = _frontend(tmp_path)
    sentinel = "windows-parent-secret-must-not-leak"
    for name in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "NEXT_PUBLIC_RELEASE_SENTINEL",
    ):
        monkeypatch.setenv(name, sentinel)

    fake_build = tmp_path / "fake_npm_build.py"
    fake_build.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "import sys\n"
        "assert sys.argv[1:] == ['run', 'build']\n"
        "for name in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', "
        "'NEXT_PUBLIC_RELEASE_SENTINEL'):\n"
        "    assert name not in os.environ\n"
        "for name in ('COMSPEC', 'PATH', 'PATHEXT', 'SYSTEMROOT', 'TEMP'):\n"
        "    assert os.environ.get(name)\n"
        "build_id = os.environ['CAREERPILOT_BUILD_ID']\n"
        "Path('.next').mkdir(parents=True)\n"
        "Path('.next/BUILD_ID').write_text(build_id)\n"
        "build_dir = Path('out/_next/static') / build_id\n"
        "build_dir.mkdir(parents=True)\n"
        "(build_dir / '_buildManifest.js').write_text("
        "'self.__BUILD_MANIFEST={\"buildId\":\"' + build_id + '\"};')\n"
        "(build_dir / '_ssgManifest.js').write_text('self.__SSG_MANIFEST=new Set([]);')\n"
        "Path('out/_next/static/chunks').mkdir()\n"
        "Path('out/_next/static/chunks/release.js').write_text('self.__CAREERPILOT_RELEASE=true')\n"
        "Path('out/index.html').write_text('<html><body><title>CareerPilot</title>' + build_id + 'clean<script src=\"/_next/static/chunks/release.js\"></script></body></html>\\n')\n"
        "Path('out/_next/app.js').write_text('clean\\n')\n"
    )
    fake_npm = tmp_path / "npm.cmd"
    fake_npm.write_text(
        "@echo off\r\n"
        + subprocess.list2cmdline([sys.executable, str(fake_build)])
        + " %*\r\n"
    )
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setenv("TEMP", str(tmp_path))

    release = build_static_ui(
        frontend,
        project_root=tmp_path,
    )

    assert sentinel not in release.manifest.read_text()
    assert "clean" in (release.export / "_next" / "app.js").read_text()


@pytest.mark.parametrize("operation", ["build", "write", "verify"])
def test_release_operations_reject_symlinked_frontend_root(
    tmp_path: Path,
    operation: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside_root = tmp_path / "outside"
    outside = _frontend(outside_root)
    link = project / "frontend"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(StaticUIReleaseError, match="cannot contain aliases"):
        _release_operation(operation, link, project)


@pytest.mark.parametrize("operation", ["build", "write", "verify"])
def test_release_operations_reject_frontend_outside_expected_project(
    tmp_path: Path,
    operation: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = _frontend(tmp_path / "outside")

    with pytest.raises(StaticUIReleaseError, match="escapes the expected project root"):
        _release_operation(operation, outside, project)


def test_reparse_attribute_is_treated_as_a_release_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "junction"
    path.mkdir()
    metadata = path.lstat()

    class ReparseMetadata:
        st_mode = metadata.st_mode
        st_file_attributes = 0x400

    monkeypatch.setattr(Path, "lstat", lambda self: ReparseMetadata())

    assert static_release._path_is_alias(path)


@pytest.mark.parametrize("operation", ["fingerprint", "verify", "stage"])
def test_nested_resolved_escape_is_rejected_before_release_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    write_static_ui_manifest(frontend, project_root=tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("must-not-be-read")
    if operation == "fingerprint":
        escaped = frontend / "app" / "nested"
        escaped.mkdir()
    else:
        escaped = frontend / "out" / "_next" / "app.js"

    real_resolve = static_release._resolve_path

    def resolved_with_junction_escape(path: Path) -> Path:
        if Path(path) == escaped:
            return sentinel
        return real_resolve(path)

    monkeypatch.setattr(static_release, "_resolve_path", resolved_with_junction_escape)

    with pytest.raises(StaticUIReleaseError, match="resolves outside"):
        if operation == "fingerprint":
            frontend_source_fingerprint(frontend, project_root=tmp_path)
        elif operation == "verify":
            verify_static_ui(frontend, project_root=tmp_path)
        else:
            stage_verified_static_ui(
                frontend,
                project_root=tmp_path,
                destination=tmp_path / "stage",
            )
    assert sentinel.read_text() == "must-not-be-read"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
@pytest.mark.parametrize("operation", ["build", "write", "verify"])
def test_release_operations_reject_real_nested_windows_junction(
    tmp_path: Path,
    operation: str,
) -> None:
    frontend = _frontend(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("unchanged")
    junction = frontend / "app" / "junction"
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    created = subprocess.run(
        [comspec, "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if created.returncode != 0:
        pytest.skip(f"could not create a Windows directory junction: {created.stdout}")
    try:
        with pytest.raises(StaticUIReleaseError, match="aliases"):
            _release_operation(operation, frontend, tmp_path)
        assert sentinel.read_text() == "unchanged"
    finally:
        os.rmdir(junction)


def test_hatch_rejects_frontend_root_symlink_that_escapes_project(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    _copy_project(repository, project, frontend=False)
    outside_root = tmp_path / "outside"
    outside = _frontend(outside_root)
    _export(outside)
    write_static_ui_manifest(outside, project_root=outside_root)
    sentinel = outside / "outside-sentinel.txt"
    sentinel.write_text("unchanged")
    try:
        (project / "frontend").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    uv, environment = _uv_environment()
    built = subprocess.run(
        [uv, "build", "--offline", "--out-dir", str(tmp_path / "dist"), str(project)],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    assert built.returncode != 0
    assert "Bundled static UI release policy failed" in built.stdout
    assert "cannot contain aliases" in built.stdout
    assert sentinel.read_text() == "unchanged"


def test_independent_sdist_verifier_hashes_exact_static_ui_contents(
    tmp_path: Path,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    release = write_static_ui_manifest(frontend, project_root=tmp_path)
    archive = tmp_path / "valid.tar.gz"
    members = _static_ui_archive_members(
        release,
        prefix="career_pilot-1.0/frontend/out",
    )
    _write_test_sdist(archive, members)

    verified = verify_sdist_static_ui(
        archive,
        destination=tmp_path / "verified-sdist-ui",
        expected_export_fingerprint=release.export_fingerprint,
    )

    assert verified.export_fingerprint == release.export_fingerprint
    assert verified.build_id == release.build_id


@pytest.mark.parametrize("mutation", ["tampered", "attestation", "duplicate"])
def test_independent_sdist_verifier_rejects_content_and_duplicate_members(
    tmp_path: Path,
    mutation: str,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    release = write_static_ui_manifest(frontend, project_root=tmp_path)
    prefix = "career_pilot-1.0/frontend/out"
    members = _static_ui_archive_members(release, prefix=prefix)
    if mutation == "tampered":
        target = f"{prefix}/_next/static/chunks/workspace.js"
        members = [
            (name, b"same-name-tamper\n" if name == target else payload)
            for name, payload in members
        ]
    elif mutation == "attestation":
        target = f"{prefix}/{BUILD_ID_ATTESTATION_NAME}"
        members = [
            (name, b"careerpilot-" + b"0" * 64 if name == target else payload)
            for name, payload in members
        ]
    else:
        members.append(members[0])
    archive = tmp_path / f"{mutation}.tar.gz"
    _write_test_sdist(archive, members)

    with pytest.raises(
        StaticUIReleaseError,
        match="duplicate|membership or content|attestation",
    ):
        verify_sdist_static_ui(
            archive,
            destination=tmp_path / f"extract-{mutation}",
            expected_export_fingerprint=release.export_fingerprint,
        )


@pytest.mark.parametrize(
    ("kind", "member_name"),
    [
        (tarfile.SYMTYPE, "career_pilot-1.0/frontend/out/symlink"),
        (tarfile.LNKTYPE, "career_pilot-1.0/frontend/out/hardlink"),
        (tarfile.CHRTYPE, "career_pilot-1.0/frontend/out/device"),
        (tarfile.FIFOTYPE, "career_pilot-1.0/frontend/out/fifo"),
    ],
)
def test_independent_sdist_verifier_rejects_special_members_before_extraction(
    tmp_path: Path,
    kind: bytes,
    member_name: str,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)
    release = write_static_ui_manifest(frontend, project_root=tmp_path)
    members = _static_ui_archive_members(
        release,
        prefix="career_pilot-1.0/frontend/out",
    )
    special = tarfile.TarInfo(member_name)
    special.type = kind
    special.linkname = "outside-target"
    archive = tmp_path / "special.tar.gz"
    _write_test_sdist(archive, members, special=special)
    destination = tmp_path / "must-not-extract"

    with pytest.raises(StaticUIReleaseError, match="unsupported member type"):
        verify_sdist_static_ui(
            archive,
            destination=destination,
            expected_export_fingerprint=release.export_fingerprint,
        )

    assert not destination.exists()


def test_independent_sdist_verifier_rejects_traversal_before_extraction(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "traversal.tar.gz"
    outside = tmp_path / "outside-sentinel"
    outside.write_text("unchanged")
    _write_test_sdist(
        archive,
        [("career_pilot-1.0/frontend/out/../../../outside-sentinel", b"changed")],
    )

    with pytest.raises(StaticUIReleaseError, match="unsafe or duplicate"):
        verify_sdist_static_ui(
            archive,
            destination=tmp_path / "must-not-extract",
            expected_export_fingerprint="0" * 64,
        )

    assert outside.read_text() == "unchanged"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("chunks/Foo.js", "chunks/foo.js"),
        ("pages/caf\u00e9.html", "pages/cafe\u0301.html"),
        ("media/safe.js", "media/CON.js"),
    ],
)
@pytest.mark.parametrize("archive_kind", ["sdist", "wheel"])
def test_archive_verifiers_reject_cross_platform_path_collisions_before_extraction(
    tmp_path: Path,
    first: str,
    second: str,
    archive_kind: str,
) -> None:
    if archive_kind == "sdist":
        prefix = "career_pilot-1.0/frontend/out/_next/static"
        archive = tmp_path / "collision.tar.gz"
        _write_test_sdist(
            archive,
            [(f"{prefix}/{first}", b"first"), (f"{prefix}/{second}", b"second")],
        )
        verifier = verify_sdist_static_ui
    else:
        prefix = "career_companion/web/_next/static"
        archive = tmp_path / "collision.whl"
        _write_test_wheel(
            archive,
            [(f"{prefix}/{first}", b"first"), (f"{prefix}/{second}", b"second")],
        )
        verifier = verify_wheel_static_ui

    with pytest.raises(
        StaticUIReleaseError,
        match="non-portable path collision|Windows-reserved",
    ):
        verifier(
            archive,
            destination=tmp_path / f"extract-{archive_kind}",
            expected_export_fingerprint="0" * 64,
        )


def test_independent_wheel_verifier_rejects_symlink_before_extraction(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "symlink.whl"
    link = zipfile.ZipInfo("career_companion/web/linked.js")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, mode="w") as archive:
        archive.writestr(link, "outside-target")

    with pytest.raises(StaticUIReleaseError, match="unsupported member type"):
        verify_wheel_static_ui(
            archive_path,
            destination=tmp_path / "must-not-extract",
            expected_export_fingerprint="0" * 64,
        )


def test_offline_sdist_and_wheel_bundle_only_verified_static_ui(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    _copy_project(repository, project)
    rogue_name = "legacy-web-rogue-7d7f03c4.txt"
    legacy_web = project / "career_companion" / "web"
    legacy_web.mkdir()
    (legacy_web / "index.html").write_text("stale bundled page\n")
    (legacy_web / rogue_name).write_text("must never enter an artifact\n")
    uv, environment = _uv_environment()
    sentinel = "inherited-release-sentinel-must-not-enter-wheel"
    environment["NEXT_PUBLIC_RELEASE_SENTINEL"] = sentinel

    missing = subprocess.run(
        [uv, "build", "--offline", "--out-dir", str(tmp_path / "missing"), str(project)],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert missing.returncode != 0
    assert "Bundled static UI release policy failed" in missing.stdout
    assert "export is missing" in missing.stdout

    frontend = project / "frontend"
    _export(frontend)
    source_release = write_static_ui_manifest(frontend, project_root=project)
    page = frontend / "app" / "page.tsx"
    original_page = page.read_text(encoding="utf-8")
    page.write_text(original_page + "// changed after export\n", encoding="utf-8")
    stale = subprocess.run(
        [uv, "build", "--offline", "--out-dir", str(tmp_path / "stale"), str(project)],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert stale.returncode != 0
    assert "Bundled static UI release policy failed" in stale.stdout
    assert "frontend sources changed" in stale.stdout
    page.write_text(original_page, encoding="utf-8")

    unmanifested = frontend / "out" / "unmanifested-release-rogue.js"
    unmanifested.write_text("must be rejected\n")
    rogue_export = subprocess.run(
        [
            uv,
            "build",
            "--offline",
            "--out-dir",
            str(tmp_path / "rogue-export"),
            str(project),
        ],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert rogue_export.returncode != 0
    assert "Bundled static UI release policy failed" in rogue_export.stdout
    assert "export is stale" in rogue_export.stdout
    unmanifested.unlink()

    distribution = tmp_path / "dist"
    built = subprocess.run(
        [uv, "build", "--offline", "--out-dir", str(distribution), str(project)],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert built.returncode == 0, built.stdout

    sdist = next(distribution.glob("*.tar.gz"))
    with tarfile.open(sdist) as archive:
        sdist_names = [member.name for member in archive.getmembers() if member.isfile()]
    sdist_root = next(
        name.split("/", 1)[0]
        for name in sdist_names
        if name.endswith(f"frontend/out/{MANIFEST_NAME}")
    )
    expected_sdist_ui = {
        f"{sdist_root}/frontend/out/{file.path}" for file in source_release.files
    } | {f"{sdist_root}/frontend/out/{MANIFEST_NAME}"}
    actual_sdist_ui = {name for name in sdist_names if "/frontend/out/" in name}
    assert actual_sdist_ui == expected_sdist_ui
    assert len(sdist_names) == len(set(sdist_names))
    assert not any(name.endswith(rogue_name) for name in sdist_names)
    assert not any(name.endswith("career_companion/web/index.html") for name in sdist_names)
    assert not any(name.endswith(".DS_Store") for name in sdist_names)
    verified_sdist = verify_sdist_static_ui(
        sdist,
        destination=tmp_path / "verified-real-sdist-ui",
        expected_export_fingerprint=source_release.export_fingerprint,
    )
    assert verified_sdist.build_id == source_release.build_id
    assert verified_sdist.files == source_release.files

    wheel = next(distribution.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert f"career_companion/web/{MANIFEST_NAME}" in names
        assert "career_companion/web/index.html" in names
        assert "career_companion/web/_next/app.js" in names
        assert "career_companion/web/_next/static/chunks/workspace.js" in names
        assert "career_companion/web/workspace/index.html" in names
        assert (
            f"career_companion/web/_next/static/{source_release.build_id}/"
            "_buildManifest.js"
        ) in names
        assert f"career_companion/web/{BUILD_ID_ATTESTATION_NAME}" in names
        assert rogue_name not in "\n".join(names)
        assert len(names) == len(set(names))
        assert not any(name.endswith(".DS_Store") for name in names)
        assert all(sentinel.encode() not in archive.read(name) for name in names)
        expected_wheel_ui = {
            f"career_companion/web/{file.path}" for file in source_release.files
        } | {f"career_companion/web/{MANIFEST_NAME}"}
        actual_wheel_ui = {
            name for name in names if name.startswith("career_companion/web/")
        }
        assert actual_wheel_ui == expected_wheel_ui
    bundled = tmp_path / "verified-real-wheel-ui"
    verified_wheel = verify_wheel_static_ui(
        wheel,
        destination=bundled,
        expected_export_fingerprint=source_release.export_fingerprint,
    )
    assert verified_wheel.build_id == source_release.build_id
    assert verified_wheel.files == source_release.files

    isolated = tmp_path / "isolated"
    created = subprocess.run(
        [uv, "venv", "--offline", "--python", sys.executable, str(isolated)],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert created.returncode == 0, created.stdout
    python = (
        isolated / "Scripts" / "python.exe"
        if os.name == "nt"
        else isolated / "bin" / "python"
    )
    installed = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--python",
            str(python),
            str(wheel),
        ],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert installed.returncode == 0, installed.stdout
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    runtime_environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper()
        in {
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "WINDIR",
        }
    }
    runtime_environment["PATH"] = str(python.parent)
    runtime_environment["CAREER_COMPANION_HOME"] = str(tmp_path / "runtime-data")
    smoke = subprocess.run(
        [
            str(python),
            str(project / "scripts" / "static_ui_wheel_smoke.py"),
            "--expected-fingerprint",
            source_release.export_fingerprint,
            "--require-no-node",
        ],
        cwd=runtime,
        env=runtime_environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert smoke.returncode == 0, smoke.stdout

    located_bundle = subprocess.run(
        [
            str(python),
            "-c",
            "from career_companion.web import frontend_build_directory; "
            "print(frontend_build_directory())",
        ],
        cwd=runtime,
        env=runtime_environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert located_bundle.returncode == 0, located_bundle.stdout
    installed_bundle = Path(located_bundle.stdout.strip())
    installed_route_chunk = (
        installed_bundle / "_next" / "static" / "chunks" / "workspace.js"
    )
    installed_route_chunk.write_text("self.__INSTALLED_ROUTE_TAMPER=true;\n")
    tampered_smoke = subprocess.run(
        [
            str(python),
            str(project / "scripts" / "static_ui_wheel_smoke.py"),
            "--expected-fingerprint",
            source_release.export_fingerprint,
            "--require-no-node",
        ],
        cwd=runtime,
        env=runtime_environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert tampered_smoke.returncode != 0
    assert "membership or content" in tampered_smoke.stdout
    assert not (bundled / "pyproject.toml").exists()

    source_manifest = json.loads(source_release.manifest.read_text())
    bundled_manifest = json.loads((bundled / MANIFEST_NAME).read_text())
    assert bundled_manifest == source_manifest
    assert bundled_manifest["export_fingerprint"] == source_release.export_fingerprint
    assert bundled_manifest["index_sha256"] == hashlib.sha256(
        (bundled / "index.html").read_bytes()
    ).hexdigest()
