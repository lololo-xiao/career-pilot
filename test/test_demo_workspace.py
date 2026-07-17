from __future__ import annotations

import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.auth import AuthStore
from career_companion.cli import main
from career_companion.database import (
    ApplicationRecord,
    AuditEventRecord,
    CandidateProfileRecord,
    ConversationSessionRecord,
    JobRecord,
    UsageRunRecord,
)
from career_companion.demo_workspace import (
    DEMO_MARKER_NAME,
    DemoWorkspaceError,
    default_demo_home,
    seed_demo_home,
)
from career_companion.paths import CompanionPaths
from career_companion.persistence import account_session, clear_factory_cache


@pytest.fixture(autouse=True)
def clear_demo_database_factories() -> None:
    clear_factory_cache()
    yield
    clear_factory_cache()


def _production_paths(tmp_path: Path) -> CompanionPaths:
    return CompanionPaths.at_root(
        tmp_path / "career-companion",
        tmp_path / "career-companion-config",
    )


def _table_count(database: Path, table: str) -> int:
    with sqlite3.connect(database) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_demo_seed_is_isolated_idempotent_fictional_and_provider_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production = _production_paths(tmp_path)
    production.create()
    sentinel = production.root / "real-user-data.txt"
    sentinel.write_text("must remain untouched", encoding="utf-8")
    demo_home = tmp_path / "meetup-rehearsal"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-looking-value-the-seeder-must-ignore")

    def provider_call_forbidden(*_args, **_kwargs):
        raise AssertionError("The demo seeder must never construct a provider client")

    monkeypatch.setattr("app.auth.OpenAI", provider_call_forbidden)
    first = seed_demo_home(
        demo_home,
        production_paths=production,
        today=date(2026, 7, 17),
    )
    second = seed_demo_home(
        demo_home,
        production_paths=production,
        today=date(2026, 7, 17),
    )

    assert first.account_paths == second.account_paths
    assert (first.home / DEMO_MARKER_NAME).is_file()
    assert sentinel.read_text(encoding="utf-8") == "must remain untouched"
    assert first.home != production.root
    assert first.account_paths.root.is_relative_to(first.home)
    assert (first.profiles, first.jobs, first.applications, first.conversations) == (
        1,
        4,
        4,
        1,
    )
    assert second == first

    assert _table_count(first.home / "accounts.db", "provider_connections") == 0
    assert _table_count(first.home / "accounts.db", "service_credentials") == 0
    with sqlite3.connect(first.home / "accounts.db") as connection:
        active_provider = connection.execute(
            "SELECT active_provider FROM accounts"
        ).fetchone()[0]
    assert active_provider is None

    with account_session(first.account_paths) as session:
        jobs = session.scalars(select(JobRecord).order_by(JobRecord.id)).all()
        profile = session.get(
            CandidateProfileRecord,
            "00000000-0000-4000-8000-000000000002",
        )
        applications = session.scalars(select(ApplicationRecord)).all()
        application_notes = [application.events[0].note for application in applications]
        audit = session.get(
            AuditEventRecord,
            "00000000-0000-4000-8000-000000000005",
        )
        assert session.scalar(select(func.count()).select_from(UsageRunRecord)) == 0

    assert profile is not None
    assert "FICTIONAL DEMO" in profile.display_name
    assert profile.payload["email"].endswith(".invalid")
    assert len(jobs) == 4
    assert all("(Fictional)" in job.company for job in jobs)
    assert all(".invalid/" in job.canonical_url for job in jobs)
    assert all(job.source_type == "fictional-demo" for job in jobs)
    assert all("no application was sent" in note for note in application_notes)
    assert audit is not None
    assert audit.payload["fictional"] is True
    assert audit.payload["provider_called"] is False


def test_demo_seed_reset_is_guarded_and_restores_canonical_data(tmp_path: Path) -> None:
    production = _production_paths(tmp_path)
    unmarked = tmp_path / "unmarked"
    unmarked.mkdir()
    (unmarked / "keep.txt").write_text("user-owned", encoding="utf-8")

    with pytest.raises(DemoWorkspaceError, match="non-empty directory"):
        seed_demo_home(unmarked, production_paths=production)
    with pytest.raises(DemoWorkspaceError, match="unmarked directory"):
        seed_demo_home(unmarked, reset=True, production_paths=production)
    with pytest.raises(DemoWorkspaceError, match="separate from"):
        seed_demo_home(production.root, production_paths=production)
    with pytest.raises(DemoWorkspaceError, match="separate from"):
        seed_demo_home(production.config.parent, production_paths=production)
    file_target = tmp_path / "not-a-directory"
    file_target.write_text("user-owned", encoding="utf-8")
    with pytest.raises(DemoWorkspaceError, match="must be a directory"):
        seed_demo_home(file_target, production_paths=production)
    assert (unmarked / "keep.txt").read_text(encoding="utf-8") == "user-owned"

    result = seed_demo_home(
        tmp_path / "demo",
        production_paths=production,
        today=date(2026, 7, 17),
    )
    with account_session(result.account_paths) as session:
        session.add(
            JobRecord(
                id="user-added-demo-row",
                company="Local rehearsal edit",
                title="Extra role",
                canonical_url="https://extra.careerpilot-demo.invalid/role",
                fingerprint="f" * 64,
                source_type="manual",
                raw_payload={},
                normalized_spec={},
            )
        )

    repeated = seed_demo_home(
        result.home,
        production_paths=production,
        today=date(2026, 7, 17),
    )
    assert repeated.jobs == 5

    reset = seed_demo_home(
        result.home,
        reset=True,
        production_paths=production,
        today=date(2026, 7, 17),
    )
    assert reset.jobs == 4
    with account_session(reset.account_paths) as session:
        assert session.get(JobRecord, "user-added-demo-row") is None


def test_demo_seed_refuses_nested_symlink_without_touching_outside_directory(
    tmp_path: Path,
) -> None:
    production = _production_paths(tmp_path)
    result = seed_demo_home(tmp_path / "demo", production_paths=production)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("must remain untouched", encoding="utf-8")

    shutil.rmtree(result.account_paths.imports)
    result.account_paths.imports.symlink_to(outside, target_is_directory=True)

    with pytest.raises(DemoWorkspaceError, match="symbolic links"):
        seed_demo_home(result.home, production_paths=production)

    assert sentinel.read_text(encoding="utf-8") == "must remain untouched"
    assert not (outside / "fictional-demo-profile.txt").exists()


def test_demo_seeder_refuses_stored_credentials_without_loading_them(
    tmp_path: Path,
) -> None:
    production = _production_paths(tmp_path)
    result = seed_demo_home(tmp_path / "demo", production_paths=production)
    secret = (result.home / ".auth-secret").read_text(encoding="utf-8").strip()
    store = AuthStore(result.home / "accounts.db", secret)
    account = store.ensure_local_account()
    store.save_provider_connection(
        account_id=account.user_id,
        provider="api_key",
        credential=b"sk-demo-test-credential-that-must-not-be-read",
    )

    with pytest.raises(DemoWorkspaceError, match="stored credentials"):
        seed_demo_home(result.home, production_paths=production)

    reset = seed_demo_home(result.home, reset=True, production_paths=production)
    assert reset.jobs == 4
    assert _table_count(reset.home / "accounts.db", "provider_connections") == 0


def test_demo_cli_help_and_command_describe_the_safety_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    production = _production_paths(tmp_path)
    monkeypatch.setenv("CAREER_COMPANION_HOME", str(production.root))
    assert default_demo_home() == production.root.with_name("career-companion-demo")

    with pytest.raises(SystemExit) as help_exit:
        main(["demo", "seed", "--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out.casefold()
    assert "fictional" in help_text
    assert "never reads provider credentials" in help_text
    assert "--home" in help_text
    assert "--reset" in help_text

    demo_home = tmp_path / "cli-demo"
    assert main(["demo", "seed", "--home", str(demo_home)]) == 0
    output = capsys.readouterr().out
    assert f"Fictional demo home: {demo_home}" in output
    assert "Provider credentials: none; no AI provider was contacted." in output
    assert "AI connection: disconnected" in output
    assert "Seeded 1 profile, 4 jobs, 4 applications, and 1 conversation." in output
    assert not production.root.exists()
