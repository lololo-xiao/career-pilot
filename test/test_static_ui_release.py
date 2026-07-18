from __future__ import annotations

import hashlib
import json
import os
import shutil
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
    verify_static_ui,
    write_static_ui_manifest,
)


def _frontend(root: Path) -> Path:
    frontend = root / "frontend"
    (frontend / "app").mkdir(parents=True)
    (frontend / "app" / "page.tsx").write_text("export default function Page() {}\n")
    (frontend / "package.json").write_text('{"name":"release-test"}\n')
    (frontend / "package-lock.json").write_text('{"lockfileVersion":3}\n')
    (frontend / "next.config.ts").write_text("export default {};\n")
    (frontend / "next-env.d.ts").write_text("// generated before build\n")
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
        f"self.__BUILD_MANIFEST={{buildId:{build_id!r}}};\n"
    )
    (build_directory / "_ssgManifest.js").write_text(
        "self.__SSG_MANIFEST=new Set([]);\n"
    )
    chunks = frontend / "out" / "_next" / "static" / "chunks"
    chunks.mkdir()
    (chunks / "release.js").write_text("self.__CAREERPILOT_RELEASE=true;\n")
    (frontend / "out" / "index.html").write_text(
        f"<title>CareerPilot</title><meta content='{build_id}'>"
        '<script src="/_next/static/chunks/release.js"></script>'
        f'<script src="/_next/static/{build_id}/_buildManifest.js"></script>'
        f'<script src="/_next/static/{build_id}/_ssgManifest.js"></script>\n'
    )
    (frontend / "out" / "_next" / "app.js").write_text("release bundle\n")
    if attest:
        (frontend / "out" / BUILD_ID_ATTESTATION_NAME).write_text(build_id)
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
        f"{representative}{references}{extra}\n"
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
                "node_modules", ".next", "out", "test", ".DS_Store"
            ),
        )


def _uv_environment() -> tuple[str, dict[str, str]]:
    uv = shutil.which("uv")
    assert uv is not None, "the package contract tests require the repository's uv tool"
    environment = os.environ.copy()
    environment["UV_OFFLINE"] = "1"
    return uv, environment


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
        f"_next/static/{first.build_id}/_buildManifest.js",
        f"_next/static/{first.build_id}/_ssgManifest.js",
        "index.html",
    }

    # Next owns and may rewrite this generated type shim during a real build.
    before = frontend_source_fingerprint(frontend, project_root=tmp_path)
    (frontend / "next-env.d.ts").write_text("// generated after build\n")
    assert frontend_source_fingerprint(frontend, project_root=tmp_path) == before
    assert verify_static_ui(frontend, project_root=tmp_path).source_fingerprint == before

    # Authored input remains freshness-sensitive.
    (frontend / "app" / "page.tsx").write_text("export default function Changed() {}\n")
    with pytest.raises(StaticUIReleaseError, match="frontend sources changed"):
        verify_static_ui(frontend, project_root=tmp_path)


def test_verifier_rejects_missing_and_modified_exports(tmp_path: Path) -> None:
    frontend = _frontend(tmp_path)
    with pytest.raises(StaticUIReleaseError, match="export is missing"):
        verify_static_ui(frontend, project_root=tmp_path)

    _export(frontend)
    write_static_ui_manifest(frontend, project_root=tmp_path)
    (frontend / "out" / "_next" / "app.js").write_text("tampered\n")
    with pytest.raises(StaticUIReleaseError, match="export is stale"):
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


def test_index_accepts_safe_relative_references_and_queries(tmp_path: Path) -> None:
    frontend = _frontend(tmp_path)
    build_id = _export(frontend)
    _write_manifest_references(
        frontend,
        build_id,
        f"./_next/static/{build_id}/_buildManifest.js?release=1",
        f"_next/static/{build_id}/_ssgManifest.js?v=1",
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

    with pytest.raises(StaticUIReleaseError, match="executable|base URL"):
        write_static_ui_manifest(frontend, project_root=tmp_path)

    assert (frontend / "out" / BUILD_ID_ATTESTATION_NAME).read_bytes() == before


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
            f"self.__BUILD_MANIFEST={{buildId:{build_id!r}}};"
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
        "(build_dir / '_buildManifest.js').write_text('self.__BUILD_MANIFEST=' + repr(build_id))\n"
        "(build_dir / '_ssgManifest.js').write_text('self.__SSG_MANIFEST=new Set([])')\n"
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
    original_page = page.read_text()
    page.write_text(original_page + "// changed after export\n")
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
    page.write_text(original_page)

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

    wheel = next(distribution.glob("*.whl"))
    unpacked = tmp_path / "wheel"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert f"career_companion/web/{MANIFEST_NAME}" in names
        assert "career_companion/web/index.html" in names
        assert "career_companion/web/_next/app.js" in names
        assert (
            f"career_companion/web/_next/static/{source_release.build_id}/"
            "_buildManifest.js"
        ) in names
        assert f"career_companion/web/{BUILD_ID_ATTESTATION_NAME}" in names
        assert rogue_name not in "\n".join(names)
        assert len(names) == len(set(names))
        assert not any(name.endswith(".DS_Store") for name in names)
        assert all(sentinel.encode() not in archive.read(name) for name in names)
        archive.extractall(unpacked)

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
    assert not (unpacked / "pyproject.toml").exists()

    source_manifest = json.loads(source_release.manifest.read_text())
    bundled = unpacked / "career_companion" / "web"
    bundled_manifest = json.loads((bundled / MANIFEST_NAME).read_text())
    assert bundled_manifest == source_manifest
    assert bundled_manifest["export_fingerprint"] == source_release.export_fingerprint
    assert bundled_manifest["index_sha256"] == hashlib.sha256(
        (bundled / "index.html").read_bytes()
    ).hexdigest()
