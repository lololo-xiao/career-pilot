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

from career_companion.static_ui_release import (
    BUILD_CONTRACT,
    MANIFEST_NAME,
    StaticUIReleaseError,
    build_static_ui,
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
    assert {file.path for file in first.files} == {"_next/app.js", "index.html"}

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


@pytest.mark.parametrize("relative", [Path(".env.production"), Path("config/.env.local")])
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


def test_build_scrubs_inherited_environment_before_fake_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = _frontend(tmp_path)
    sentinel = "inherited-release-sentinel-must-not-leak"
    monkeypatch.setenv("NEXT_PUBLIC_RELEASE_SENTINEL", sentinel)
    monkeypatch.setenv("NODE_OPTIONS", f"--require={sentinel}")
    monkeypatch.setenv("OPENAI_API_KEY", sentinel)
    captured: dict[str, str] = {}

    def fake_build(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert command == ["npm", "run", "build"]
        assert cwd == frontend
        assert check is True
        captured.update(env)
        leaked = env.get("NEXT_PUBLIC_RELEASE_SENTINEL", "clean")
        (frontend / "out" / "_next").mkdir(parents=True)
        (frontend / "out" / "index.html").write_text(
            f"<title>CareerPilot</title>{leaked}\n"
        )
        (frontend / "out" / "_next" / "app.js").write_text(leaked)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_build)
    release = build_static_ui(frontend, project_root=tmp_path)

    inherited_names = {name.upper() for name in captured}
    assert "NEXT_PUBLIC_RELEASE_SENTINEL" not in inherited_names
    assert "NODE_OPTIONS" not in inherited_names
    assert "OPENAI_API_KEY" not in inherited_names
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

    with pytest.raises(StaticUIReleaseError, match="cannot be a symbolic link"):
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
    assert "cannot be a symbolic link" in built.stdout
    assert sentinel.read_text() == "unchanged"


def test_offline_sdist_and_wheel_bundle_only_verified_static_ui(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    _copy_project(repository, project)
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
    isolated_site = subprocess.run(
        [str(python), "-c", "import site; print(site.getsitepackages()[0])"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    ).stdout.strip()
    # Dependencies were already installed from the lock for this test run. Add
    # them after the isolated site-packages directory so the installed wheel,
    # never the source checkout, remains the package under test.
    host_site = Path(pytest.__file__).resolve().parents[1]
    (Path(isolated_site) / "_career_pilot_test_dependencies.pth").write_text(
        str(host_site) + "\n"
    )
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
            "-c",
            "import shutil; "
            "import sys; "
            "from pathlib import Path; "
            "from fastapi import FastAPI; "
            "from fastapi.testclient import TestClient; "
            "from app.static_frontend import mount_static_frontend; "
            "import career_companion; "
            "from career_companion.web import frontend_build_directory; "
            "assert shutil.which('node') is None; "
            "package = Path(career_companion.__file__).resolve(); "
            "assert package.is_relative_to(Path(sys.prefix).resolve()); "
            "web = frontend_build_directory(); assert web is not None; "
            "app = FastAPI(); mount_static_frontend(app, web); "
            "response = TestClient(app).get('/'); "
            "assert response.status_code == 200; assert 'CareerPilot' in response.text",
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
