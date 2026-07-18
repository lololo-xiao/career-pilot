from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from career_companion.static_ui_release import (
    MANIFEST_NAME,
    StaticUIReleaseError,
    frontend_source_fingerprint,
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


def _export(frontend: Path) -> None:
    (frontend / "out" / "_next").mkdir(parents=True)
    (frontend / "out" / "index.html").write_text("<title>CareerPilot</title>\n")
    (frontend / "out" / "_next" / "app.js").write_text("release bundle\n")
    (frontend / "out" / ".DS_Store").write_bytes(b"junk")


def test_manifest_is_deterministic_and_ignores_only_generated_build_inputs(
    tmp_path: Path,
) -> None:
    frontend = _frontend(tmp_path)
    _export(frontend)

    first = write_static_ui_manifest(frontend)
    manifest = (frontend / "out" / MANIFEST_NAME).read_bytes()
    second = write_static_ui_manifest(frontend)

    assert manifest == (frontend / "out" / MANIFEST_NAME).read_bytes()
    assert first.export_fingerprint == second.export_fingerprint
    assert {file.path for file in first.files} == {"_next/app.js", "index.html"}

    # Next owns and may rewrite this generated type shim during a real build.
    before = frontend_source_fingerprint(frontend)
    (frontend / "next-env.d.ts").write_text("// generated after build\n")
    assert frontend_source_fingerprint(frontend) == before
    assert verify_static_ui(frontend).source_fingerprint == before

    # Authored input remains freshness-sensitive.
    (frontend / "app" / "page.tsx").write_text("export default function Changed() {}\n")
    with pytest.raises(StaticUIReleaseError, match="frontend sources changed"):
        verify_static_ui(frontend)


def test_verifier_rejects_missing_and_modified_exports(tmp_path: Path) -> None:
    frontend = _frontend(tmp_path)
    with pytest.raises(StaticUIReleaseError, match="export is missing"):
        verify_static_ui(frontend)

    _export(frontend)
    write_static_ui_manifest(frontend)
    (frontend / "out" / "_next" / "app.js").write_text("tampered\n")
    with pytest.raises(StaticUIReleaseError, match="export is stale"):
        verify_static_ui(frontend)


def test_offline_sdist_and_wheel_bundle_only_verified_static_ui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
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
    shutil.copytree(
        repository / "frontend",
        project / "frontend",
        ignore=shutil.ignore_patterns("node_modules", ".next", "out", "test", ".DS_Store"),
    )

    uv = shutil.which("uv")
    assert uv is not None, "the package contract test requires the repository's uv tool"
    environment = os.environ.copy()
    environment["UV_OFFLINE"] = "1"

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
    source_release = write_static_ui_manifest(frontend)
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
        sdist_names = archive.getnames()
    assert any(name.endswith(f"frontend/out/{MANIFEST_NAME}") for name in sdist_names)
    assert any(name.endswith("frontend/out/index.html") for name in sdist_names)
    assert not any(name.endswith(".DS_Store") for name in sdist_names)

    wheel = next(distribution.glob("*.whl"))
    unpacked = tmp_path / "wheel"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert f"career_companion/web/{MANIFEST_NAME}" in names
        assert "career_companion/web/index.html" in names
        assert "career_companion/web/_next/app.js" in names
        assert not any(name.endswith(".DS_Store") for name in names)
        archive.extractall(unpacked)

    monkeypatch.delenv("CAREER_COMPANION_WEB_DIR", raising=False)
    spec = importlib.util.spec_from_file_location(
        "isolated_packaged_web", unpacked / "career_companion" / "web.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bundled = module.frontend_build_directory()
    assert bundled == (unpacked / "career_companion" / "web").resolve()
    assert not (unpacked / "pyproject.toml").exists()

    source_manifest = json.loads(source_release.manifest.read_text())
    bundled_manifest = json.loads((bundled / MANIFEST_NAME).read_text())
    assert bundled_manifest == source_manifest
    assert bundled_manifest["export_fingerprint"] == source_release.export_fingerprint
    assert bundled_manifest["index_sha256"] == hashlib.sha256(
        (bundled / "index.html").read_bytes()
    ).hexdigest()
