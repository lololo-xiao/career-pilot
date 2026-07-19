from __future__ import annotations

import asyncio
import sqlite3
import sys
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    RectangleObject,
)

from career_companion.backup import create_backup, restore_backup
from career_companion.database import ApplicationRecord, ArtifactRecord, JobRecord
from career_companion.paths import CompanionPaths
from career_companion.persistence import clear_factory_cache, session_factory_for
from career_companion.services.approvals import decide_approval, request_approval
from career_companion.services.browser import BrowserAssistant
from career_companion.services.rendering import (
    render_one_page,
    validate_pdf,
    validate_tex_source,
)


def _write_text_pdf(path: Path, uri: str = "https://example.test/profile") -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    stream = DecodedStreamObject()
    text = "Evidence backed candidate profile with Python Go SQL delivery and verified experience. " * 3
    stream.set_data(f"BT /F1 11 Tf 40 740 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_uri(0, uri, RectangleObject([40, 700, 180, 720]))
    with path.open("wb") as output:
        writer.write(output)


def test_rendering_uses_untrusted_mode_and_validates_output(tmp_path, monkeypatch) -> None:
    tex_path = tmp_path / "cv.tex"
    tex_path.write_text("\\documentclass{article}\\begin{document}Safe CV\\end{document}")
    captured = {}

    def fake_run(command, **kwargs):
        captured.update({"command": command, **kwargs})
        _write_text_pdf(tex_path.with_suffix(".pdf"))
        tex_path.with_suffix(".log").write_text("Tectonic build complete", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("career_companion.services.rendering.shutil.which", lambda _: "/bin/tectonic")
    monkeypatch.setattr("career_companion.services.rendering.subprocess.run", fake_run)

    report = render_one_page(tex_path)

    assert report.valid is True
    assert report.pages == 1
    assert report.links == 1
    assert captured["env"]["TECTONIC_UNTRUSTED_MODE"] == "1"
    assert captured["cwd"] == tmp_path


def test_rendering_rejects_unsafe_tex_assets_corrupt_pdfs_and_links(tmp_path) -> None:
    tex_path = tmp_path / "unsafe.tex"
    tex_path.write_text(
        "\\input{/etc/passwd}\\includegraphics{../private.jpg}", encoding="utf-8"
    )
    errors = validate_tex_source(tex_path)
    assert any("prohibited" in error for error in errors)
    assert any("inside the artifact folder" in error for error in errors)

    tex_path.write_text("\\usepackage{shellesc}\\begin{document}unsafe\\end{document}")
    assert any("prohibited" in error for error in validate_tex_source(tex_path))

    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"not a pdf")
    assert validate_pdf(corrupt, engine="test", attempts=1).valid is False

    unsafe_link = tmp_path / "unsafe-link.pdf"
    _write_text_pdf(unsafe_link, "javascript:alert(1)")
    report = validate_pdf(unsafe_link, engine="test", attempts=1)
    assert report.valid is False
    assert any("unsafe external link" in error for error in report.errors)


def test_backup_closes_snapshot_connections_before_cleanup(tmp_path, monkeypatch) -> None:
    source = CompanionPaths.at_root(tmp_path / "source")
    source.create()
    with sqlite3.connect(source.database) as database:
        database.execute("CREATE TABLE proof (value TEXT)")

    original_connect = sqlite3.connect
    closed_connections: list[int] = []

    class TrackingConnection(sqlite3.Connection):
        def close(self) -> None:
            closed_connections.append(id(self))
            super().close()

    def tracked_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr("career_companion.backup.sqlite3.connect", tracked_connect)

    backup = create_backup(source, tmp_path / "safe.zip")

    assert backup.is_file()
    assert len(closed_connections) == 2


def test_backup_round_trip_excludes_credentials_and_symlinks(tmp_path) -> None:
    source = CompanionPaths.at_root(tmp_path / "source")
    source.create()
    with sqlite3.connect(source.database) as database:
        database.execute("CREATE TABLE proof (value TEXT)")
        database.execute("INSERT INTO proof VALUES ('kept')")
    (source.workspace / "profile.md").write_text("verified profile", encoding="utf-8")
    (source.config).write_text("version: 1\n", encoding="utf-8")
    local = source.hermes_profile / "profiles" / "career-companion" / "local"
    local.mkdir(parents=True)
    (local / "user-skill.md").write_text("user owned", encoding="utf-8")
    (source.hermes_profile / "profiles" / "career-companion" / "auth.json").write_text(
        "secret", encoding="utf-8"
    )
    try:
        (source.workspace / "outside-link").symlink_to(tmp_path / "outside")
    except OSError:
        pass

    backup = create_backup(source, tmp_path / "safe.zip")
    with zipfile.ZipFile(backup) as archive:
        names = archive.namelist()
    assert "data/career.db" in names
    assert "workspace/profile.md" in names
    assert "hermes/local/user-skill.md" in names
    assert all("auth.json" not in name for name in names)
    assert all("outside-link" not in name for name in names)

    restored = CompanionPaths.at_root(tmp_path / "restored")
    restore_backup(restored, backup)
    with sqlite3.connect(restored.database) as database:
        assert database.execute("SELECT value FROM proof").fetchone() == ("kept",)
    assert (restored.workspace / "profile.md").read_text() == "verified profile"
    assert (
        restored.hermes_profile
        / "profiles"
        / "career-companion"
        / "local"
        / "user-skill.md"
    ).read_text() == "user owned"
    with pytest.raises(FileExistsError):
        restore_backup(restored, backup)


def test_restore_rejects_paths_outside_the_backup_contract(tmp_path) -> None:
    malicious = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr(
            "backup-manifest.json",
            '{"format_version": 1, "includes_credentials": false}',
        )
        archive.writestr("../escaped.txt", "unsafe")
    with pytest.raises(ValueError, match="unsafe path"):
        restore_backup(CompanionPaths.at_root(tmp_path / "target"), malicious)


class _FakeLocator:
    def __init__(self, metadata, interactions, selector):
        self.metadata = metadata
        self.interactions = interactions
        self.selector = selector

    async def count(self):
        return 1

    async def evaluate(self, _script):
        return self.metadata

    async def fill(self, value):
        self.interactions.append(("fill", self.selector, value))

    async def select_option(self, value):
        self.interactions.append(("select", self.selector, value))

    async def set_input_files(self, value):
        self.interactions.append(("file", self.selector, value))


class _FakePage:
    def __init__(
        self,
        controls,
        redirect_url="https://jobs.example.test/apply",
        on_goto=None,
    ):
        self.controls = controls
        self.url = "about:blank"
        self.redirect_url = redirect_url
        self.on_goto = on_goto
        self.interactions = []
        self.guards = []
        self.init_scripts = []
        self.routes = []
        self.web_socket_routes = []
        self.launch_options = {}

    async def goto(self, _url, **_kwargs):
        if self.on_goto is not None:
            self.on_goto()
        self.url = self.redirect_url

    async def evaluate(self, script):
        self.guards.append(script)

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def unroute(self, pattern, handler):
        self.routes.remove((pattern, handler))

    def locator(self, selector):
        return _FakeLocator(self.controls[selector], self.interactions, selector)


class _HostileRequest:
    method = "POST"
    url = "https://jobs.example.test/collect"


class _HostileRoute:
    def __init__(self) -> None:
        self.aborted = False
        self.continued = False

    async def abort(self, _reason):
        self.aborted = True

    async def continue_(self):
        self.continued = True


class _HostileWebSocket:
    url = "wss://jobs.example.test/collect"

    def __init__(self) -> None:
        self.closed = False

    async def close(self, **_kwargs):
        self.closed = True


class _HostilePage(_FakePage):
    def __init__(self, controls) -> None:
        super().__init__(controls)
        self.mutations_reaching_server: list[str] = []
        self.blocked_during_navigation: list[str] = []
        self.init_guard_present_at_navigation = False

    async def goto(self, _url, **_kwargs):
        self.init_guard_present_at_navigation = bool(self.init_scripts)
        if not self.init_guard_present_at_navigation:
            self.mutations_reaching_server.append("form-submit")
        request_route = _HostileRoute()
        if self.routes:
            await self.routes[-1][1](request_route, _HostileRequest())
        if request_route.aborted:
            self.blocked_during_navigation.append("fetch-post")
        else:
            self.mutations_reaching_server.append("fetch-post")
        web_socket = _HostileWebSocket()
        if self.web_socket_routes:
            await self.web_socket_routes[-1][1](web_socket)
        if web_socket.closed:
            self.blocked_during_navigation.append("websocket")
        else:
            self.mutations_reaching_server.append("websocket")
        self.url = self.redirect_url


def _install_fake_playwright(monkeypatch, page):
    class FakeContext:
        pages = [page]

        async def new_page(self):
            return page

        async def route(self, pattern, handler):
            await page.route(pattern, handler)

        async def unroute(self, pattern, handler):
            await page.unroute(pattern, handler)

        async def route_web_socket(self, pattern, handler):
            page.web_socket_routes.append((pattern, handler))

        async def add_init_script(self, script):
            page.init_scripts.append(script)

        async def close(self):
            return None

    class FakeChromium:
        async def launch_persistent_context(self, *_args, **kwargs):
            page.launch_options = kwargs
            return FakeContext()

    class FakePlaywright:
        chromium = FakeChromium()

        async def stop(self):
            return None

    class FakeStarter:
        async def start(self):
            return FakePlaywright()

    package = types.ModuleType("playwright")
    async_api = types.ModuleType("playwright.async_api")
    async_api.async_playwright = lambda: FakeStarter()
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)


@pytest.fixture
def browser_session(tmp_path):
    clear_factory_cache()
    paths = CompanionPaths.at_root(tmp_path / "companion").scoped_to("account-a")
    with session_factory_for(paths)() as session:
        job = JobRecord(
            company="Example",
            title="Engineer",
            canonical_url="https://jobs.example.test/1",
            fingerprint="f" * 64,
            normalized_spec={},
        )
        session.add(job)
        session.flush()
        application = ApplicationRecord(job_id=job.id)
        session.add(application)
        session.flush()
        yield paths, session, application
        session.rollback()
    clear_factory_cache()


def _approve(session, paths, payload):
    approval = request_approval(
        session,
        "application.form_fill",
        payload,
        {"summary": "Fill without submitting"},
        paths=paths,
    )
    decide_approval(session, approval.id, "approved")
    return approval


def test_browser_fill_checks_element_semantics_and_never_submits(
    browser_session, monkeypatch
) -> None:
    paths, session, application = browser_session
    payload = {
        "url": "https://jobs.example.test/apply",
        "application_id": application.id,
        "fields": {"#application-name": "Ada Candidate"},
    }
    _approve(session, paths, payload)
    page = _FakePage(
        {"#application-name": {"tag": "input", "type": "text", "disabled": False}}
    )
    _install_fake_playwright(monkeypatch, page)

    result = asyncio.run(BrowserAssistant(paths).fill(session, payload))

    assert result["submitted"] is False
    assert page.interactions == [("fill", "#application-name", "Ada Candidate")]
    assert page.routes == []
    assert len(page.guards) == 2

    unsafe_payload = {
        "application_id": application.id,
        "url": "https://jobs.example.test/apply",
        "fields": {"#continue": "ignored"},
    }
    unsafe_approval = _approve(session, paths, unsafe_payload)
    unsafe_page = _FakePage(
        {"#continue": {"tag": "input", "type": "submit", "disabled": False}}
    )
    _install_fake_playwright(monkeypatch, unsafe_page)
    with pytest.raises(PermissionError, match="non-submit"):
        asyncio.run(BrowserAssistant(paths).fill(session, unsafe_payload))
    assert unsafe_approval.decision == "approved"


@pytest.mark.parametrize("control_type", ["checkbox", "radio", "color", "range"])
def test_browser_preflight_blocks_unsupported_input_types_without_consuming(
    browser_session,
    monkeypatch,
    control_type,
) -> None:
    paths, session, application = browser_session
    payload = {
        "application_id": application.id,
        "url": "https://jobs.example.test/apply",
        "fields": {"#unsupported": "value"},
    }
    approval = _approve(session, paths, payload)
    page = _FakePage(
        {
            "#unsupported": {
                "tag": "input",
                "type": control_type,
                "disabled": False,
            }
        }
    )
    _install_fake_playwright(monkeypatch, page)

    with pytest.raises(PermissionError, match="non-submit"):
        asyncio.run(BrowserAssistant(paths).fill(session, payload))

    assert approval.decision == "approved"
    assert page.interactions == []


def test_browser_boundaries_exist_before_hostile_document_startup(
    browser_session,
    monkeypatch,
) -> None:
    paths, session, application = browser_session
    payload = {
        "application_id": application.id,
        "url": "https://jobs.example.test/apply",
        "fields": {"#unsupported": "value"},
    }
    approval = _approve(session, paths, payload)
    page = _HostilePage(
        {
            "#unsupported": {
                "tag": "input",
                "type": "checkbox",
                "disabled": False,
            }
        }
    )
    _install_fake_playwright(monkeypatch, page)

    with pytest.raises(PermissionError, match="non-submit"):
        asyncio.run(BrowserAssistant(paths).fill(session, payload))

    assert page.launch_options["service_workers"] == "block"
    assert page.init_guard_present_at_navigation is True
    assert page.mutations_reaching_server == []
    assert page.blocked_during_navigation == ["fetch-post", "websocket"]
    assert page.routes == []
    assert approval.decision == "approved"


def test_browser_revalidates_redirect_target(browser_session, monkeypatch) -> None:
    paths, session, application = browser_session
    payload = {
        "application_id": application.id,
        "url": "https://jobs.example.test/redirect",
        "fields": {},
    }
    approval = _approve(session, paths, payload)
    page = _FakePage({}, redirect_url="https://www.linkedin.com/jobs/view/1")
    _install_fake_playwright(monkeypatch, page)

    with pytest.raises(PermissionError, match="LinkedIn remains manual"):
        asyncio.run(BrowserAssistant(paths).fill(session, payload))
    assert approval.decision == "approved"


def test_browser_rejects_a_different_safe_redirect_without_consuming(
    browser_session,
    monkeypatch,
) -> None:
    paths, session, application = browser_session
    payload = {
        "application_id": application.id,
        "url": "https://jobs.example.test/apply?opening=1",
        "fields": {"#name": "Ada"},
    }
    approval = _approve(session, paths, payload)
    page = _FakePage(
        {"#name": {"tag": "input", "type": "text", "disabled": False}},
        redirect_url="https://jobs.example.test/apply?opening=2",
    )
    _install_fake_playwright(monkeypatch, page)

    with pytest.raises(PermissionError, match="exact approved application URL"):
        asyncio.run(BrowserAssistant(paths).fill(session, payload))

    assert approval.decision == "approved"
    assert page.interactions == []


def test_browser_url_binding_ignores_fragments_but_not_the_destination(
    browser_session,
    monkeypatch,
) -> None:
    paths, session, application = browser_session
    payload = {
        "application_id": application.id,
        "url": "HTTPS://JOBS.EXAMPLE.TEST:443/apply#approved-step",
        "fields": {"#name": "Ada"},
    }
    approval = _approve(session, paths, payload)
    page = _FakePage(
        {"#name": {"tag": "input", "type": "text", "disabled": False}},
        redirect_url="https://jobs.example.test/apply#runtime-step",
    )
    _install_fake_playwright(monkeypatch, page)

    result = asyncio.run(BrowserAssistant(paths).fill(session, payload))

    assert result["controls"] == ["#name"]
    assert approval.decision == "consumed"


def test_missing_browser_runtime_does_not_consume_approval(
    browser_session,
    monkeypatch,
) -> None:
    paths, session, application = browser_session
    payload = {
        "application_id": application.id,
        "url": "https://jobs.example.test/apply",
    }
    approval = _approve(session, paths, payload)
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)

    with pytest.raises(RuntimeError, match="Install the browser extra"):
        asyncio.run(BrowserAssistant(paths).fill(session, payload))

    assert approval.decision == "approved"


@pytest.mark.parametrize("change", ["unapprove", "delete"])
def test_browser_final_attachment_validation_precedes_consumption(
    browser_session,
    monkeypatch,
    change,
) -> None:
    paths, session, application = browser_session
    attachment = paths.workspace / "approved-cv.pdf"
    attachment.write_bytes(b"approved")
    artifact = ArtifactRecord(
        application_id=application.id,
        kind="cv",
        version=1,
        path=str(attachment),
        sha256="a" * 64,
        approved=True,
    )
    session.add(artifact)
    session.flush()
    payload = {
        "application_id": application.id,
        "url": "https://jobs.example.test/apply",
        "files": {"#resume": artifact.id},
    }
    approval = _approve(session, paths, payload)

    def make_stale() -> None:
        if change == "unapprove":
            artifact.approved = False
        else:
            attachment.unlink()

    page = _FakePage(
        {"#resume": {"tag": "input", "type": "file", "disabled": False}},
        on_goto=make_stale,
    )
    _install_fake_playwright(monkeypatch, page)

    expected = PermissionError if change == "unapprove" else LookupError
    with pytest.raises(expected):
        asyncio.run(BrowserAssistant(paths).fill(session, payload))

    assert approval.decision == "approved"
    assert page.interactions == []
