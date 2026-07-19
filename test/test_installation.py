from __future__ import annotations

import os
import re
import sys
import tomllib
from argparse import Namespace
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.browser_security import browser_request_guard
from app.static_frontend import mount_static_frontend
from career_companion.cli import (
    _configure_runtime_environment,
    setup_command,
    start_command,
)
from career_companion.config import load_config
from career_companion.paths import CompanionPaths
from career_companion.playwright_integrity import chromium_sha256
from career_companion.workspace import initialize_account_workspace


def test_browser_mutations_enforce_origin_and_fetch_metadata() -> None:
    app = FastAPI()
    app.middleware("http")(
        browser_request_guard({"http://127.0.0.1:8787"})
    )

    @app.post("/change")
    def change():
        return {"changed": True}

    client = TestClient(app)
    assert client.post(
        "/change", headers={"Origin": "https://attacker.example"}
    ).status_code == 403
    assert client.post(
        "/change", headers={"Sec-Fetch-Site": "cross-site"}
    ).status_code == 403
    assert client.post(
        "/change", headers={"Origin": "http://127.0.0.1:8787"}
    ).status_code == 200
    assert client.post("/change").status_code == 200


def test_fastapi_serves_exported_frontend_after_api_routes(tmp_path) -> None:
    web = tmp_path / "out"
    workspace = web / "workspace"
    workspace.mkdir(parents=True)
    (web / "index.html").write_text("<h1>Career Companion</h1>", encoding="utf-8")
    (workspace / "index.html").write_text("<h1>Workspace</h1>", encoding="utf-8")
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    mount_static_frontend(app, web)
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
    assert "Career Companion" in client.get("/").text
    assert "Workspace" in client.get("/workspace/").text


def test_static_fallback_keeps_root_absolute_assets_at_deep_unknown_urls(
    tmp_path,
) -> None:
    web = tmp_path / "out"
    asset = web / "_next" / "static" / "chunks" / "fallback.js"
    asset.parent.mkdir(parents=True)
    asset.write_text("self.__FALLBACK=true;", encoding="utf-8")
    (web / "index.html").write_text("CareerPilot", encoding="utf-8")
    (web / "404.html").write_text(
        '<script src="/_next/static/chunks/fallback.js"></script>',
        encoding="utf-8",
    )
    app = FastAPI()
    mount_static_frontend(app, web)

    response = TestClient(app).get("/deep/unknown/route")

    assert response.status_code == 404
    assert 'src="/_next/static/chunks/fallback.js"' in response.text
    assert TestClient(app).get("/_next/static/chunks/fallback.js").status_code == 200


def test_setup_creates_private_runtime_secret_and_pins_executables(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "companion-home"
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("ready", encoding="utf-8")
    monkeypatch.setenv("CAREER_COMPANION_HOME", str(root))
    monkeypatch.setenv("CAREER_COMPANION_WEB_DIR", str(web))

    result = setup_command(
        Namespace(
            hermes_executable=str(tmp_path / "hermes"),
            tectonic_executable=str(tmp_path / "tectonic"),
            playwright_checksum="a" * 64,
        )
    )

    paths = CompanionPaths.discover()
    config = load_config(paths)
    assert result == 0
    assert len(paths.auth_secret.read_text().strip()) >= 32
    assert config.hermes_executable == str((tmp_path / "hermes").resolve())
    assert config.tectonic_executable == str((tmp_path / "tectonic").resolve())
    assert config.playwright_chromium_sha256 == "a" * 64
    if sys.platform != "win32" and paths.auth_secret.stat().st_mode & 0o077:
        raise AssertionError("Local auth secret must not be group or world accessible")


def test_runtime_configuration_does_not_change_default_path_layout(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("CAREER_COMPANION_HOME", raising=False)
    paths = CompanionPaths.at_root(
        tmp_path / "data",
        tmp_path / "config",
    )

    _configure_runtime_environment(
        paths,
        "http://127.0.0.1:8787",
        8787,
        None,
    )

    assert "CAREER_COMPANION_HOME" not in os.environ


def test_start_disables_proxy_headers_even_when_environment_trusts_all(
    tmp_path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    for name in (
        "AUTH_DATABASE_PATH",
        "CAREERPILOT_AUTH_SECRET",
        "CAREERPILOT_INTERNAL_API_URL",
        "FRONTEND_ORIGINS",
        "LANGFUSE_TRACING_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CAREER_COMPANION_HOME", str(tmp_path / "companion"))
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    monkeypatch.setattr("career_companion.cli.frontend_build_directory", lambda: None)
    monkeypatch.setattr("career_companion.cli.uvicorn.run", fake_run)

    result = start_command(
        Namespace(
            host="0.0.0.0",
            port=8787,
            allow_remote=True,
            no_open=True,
            api_only=True,
        )
    )

    assert result == 0
    assert captured["args"]
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["proxy_headers"] is False
    assert "forwarded_allow_ips" not in kwargs
    assert os.environ["FORWARDED_ALLOW_IPS"] == "*"


def test_public_workspace_templates_are_generic_and_non_destructive(tmp_path) -> None:
    paths = CompanionPaths.at_root(tmp_path / "base").scoped_to("account-a")
    initialize_account_workspace(paths)
    templates = paths.workspace / "templates"
    content = "\n".join(path.read_text() for path in templates.glob("*.tex"))
    assert "/users/" not in content.casefold()
    assert "@gmail.com" not in content.casefold()
    custom = templates / "custom.tex"
    custom.write_text("user-owned", encoding="utf-8")
    initialize_account_workspace(paths)
    assert custom.read_text() == "user-owned"


def test_release_runtime_versions_and_integrity_manifests_are_pinned(
    tmp_path, monkeypatch
) -> None:
    root = Path(__file__).resolve().parents[1]
    checksums = (root / "installers" / "tectonic-0.16.9.sha256").read_text().splitlines()
    assert len(checksums) == 7
    assert all(re.fullmatch(r"[a-f0-9]{64}  tectonic-0\.16\.9-.+", line) for line in checksums)
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert project["project"]["optional-dependencies"]["companion"] == [
        "playwright==1.61.0"
    ]
    hermes_requirements = [
        line
        for line in (root / "agent-profile" / "requirements-hermes.txt").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert hermes_requirements == ["hermes-agent==0.18.2", "aiohttp==3.14.1"]

    executable = tmp_path / "chromium"
    executable.write_bytes(b"pinned browser executable")
    monkeypatch.setattr(
        "career_companion.playwright_integrity.chromium_executable",
        lambda: executable,
    )
    selected, digest = chromium_sha256()
    assert selected == executable
    assert digest == "03668e6f287c3db35998c1d3f02fac5170da7624ee8c76550c94e2129eb38e86"


def test_release_files_are_sanitized_and_package_the_full_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    public_files = [
        root / "install.sh",
        root / "install.ps1",
        root / "Dockerfile",
        root / ".github" / "workflows" / "ci.yml",
    ]
    combined = "\n".join(path.read_text() for path in public_files)
    assert "/users/" not in combined.casefold()
    assert "@gmail.com" not in combined.casefold()
    dockerfile = (root / "Dockerfile").read_text()
    for required in ("app", "career_companion", "job_pipeline", "agent-profile"):
        assert f"COPY {required} " in dockerfile
    assert '"--no-proxy-headers"' in dockerfile
    for installer in (root / "install.sh", root / "install.ps1"):
        installer_text = installer.read_text()
        assert "scripts/static_ui_release.py" in installer_text
        assert "npm run build" not in installer_text
