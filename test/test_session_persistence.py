from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from app.auth import AuthStore
from app.dependencies import get_companion_paths, get_local_companion_paths
from app.main import (
    app,
    get_hermes_runtime_manager,
    get_store,
    require_current_account,
)
from career_companion.paths import CompanionPaths
from career_companion.persistence import clear_factory_cache


@pytest.fixture
def session_client(tmp_path, monkeypatch):
    clear_factory_cache()
    store = AuthStore(tmp_path / "auth.db", "p" * 48)
    local = store.ensure_local_account()
    store.save_provider_connection(
        account_id=local.user_id,
        provider="api_key",
        credential=b"sk-test-persistent-session-key",
    )
    account = store.load_account(local.user_id, "local")
    paths = CompanionPaths.at_root(tmp_path / "companion").scoped_to(local.user_id)
    paths.create()
    profile = paths.hermes_profile / "profiles" / "career-companion"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    distribution = tmp_path / "distribution"
    distribution.mkdir()
    (distribution / "distribution.yaml").write_text("name: career-companion\n")
    (distribution / "SOUL.md").write_text("# Core policy\n\nStay truthful.\n")
    monkeypatch.setenv("CAREER_COMPANION_DISTRIBUTION_PATH", str(distribution))

    class FakeRuntime:
        invalidated: list[str] = []

        async def invalidate(self, account_id: str) -> None:
            self.invalidated.append(account_id)

    runtime = FakeRuntime()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[require_current_account] = lambda: account
    app.dependency_overrides[get_local_companion_paths] = lambda: paths
    app.dependency_overrides[get_companion_paths] = lambda: paths
    app.dependency_overrides[get_hermes_runtime_manager] = lambda: runtime
    try:
        yield TestClient(app), paths, account, runtime
    finally:
        app.dependency_overrides.clear()
        clear_factory_cache()


def test_sessions_restore_messages_context_and_active_selection(session_client) -> None:
    client, _, _, _ = session_client

    initial = client.get("/companion/sessions")
    assert initial.status_code == 200
    first_id = initial.json()["active_session_id"]
    first = client.get(f"/companion/sessions/{first_id}").json()
    assert first["title"] == "New conversation"
    assert first["messages"][0]["role"] == "assistant"
    assert "Pilot" in first["messages"][0]["content"]

    created = client.post("/companion/sessions", json={})
    assert created.status_code == 200
    assert len(created.json()["messages"]) == 1
    session_id = created.json()["id"]
    added = client.post(
        f"/companion/sessions/{session_id}/messages",
        json={"role": "user", "content": "Help me target platform engineering roles."},
    )
    assert added.status_code == 200
    context = client.put(
        f"/companion/sessions/{session_id}/context",
        json={
            "candidate_profile": "Python platform engineer with production delivery experience.",
            "job_description": "Seeking a platform engineer with Python and Kubernetes experience.",
            "uploaded_filename": "profile.txt",
            "match_report": None,
        },
    )
    assert context.status_code == 200
    renamed = client.put(
        f"/companion/sessions/{session_id}",
        json={"title": "Platform search"},
    )
    assert renamed.status_code == 200

    restored_list = client.get("/companion/sessions").json()
    assert restored_list["active_session_id"] == session_id
    restored = client.get(f"/companion/sessions/{session_id}").json()
    assert restored["title"] == "Platform search"
    assert [message["role"] for message in restored["messages"]] == [
        "assistant",
        "user",
    ]
    assert restored["candidate_profile"].startswith("Python platform engineer")
    assert restored["uploaded_filename"] == "profile.txt"

    deleted = client.delete(f"/companion/sessions/{session_id}")
    assert deleted.status_code == 200
    assert all(item["id"] != session_id for item in deleted.json()["sessions"])
    assert client.get(f"/companion/sessions/{session_id}").status_code == 404


def test_agent_name_and_soul_are_saved_to_database_and_local_profile(
    session_client,
) -> None:
    client, paths, account, runtime = session_client

    saved = client.put(
        "/settings/agent-identity",
        json={
            "name": "Zey",
            "soul": "Be calm, direct, and gently humorous when we plan the search.",
        },
    )

    assert saved.status_code == 200
    assert saved.json()["name"] == "Zey"
    assert client.get("/settings/agent-identity").json()["soul"].startswith("Be calm")
    assert "Name: Zey" in (paths.workspace / "agent" / "IDENTITY.md").read_text()
    assert "gently humorous" in (paths.workspace / "agent" / "SOUL.md").read_text()
    installed_soul = (
        paths.hermes_profile / "profiles" / "career-companion" / "SOUL.md"
    ).read_text()
    assert "# Core policy" in installed_soul
    assert "Your user-chosen name is Zey" in installed_soul
    assert "gently humorous" in installed_soul
    assert runtime.invalidated == [account.user_id]
    session_id = client.get("/companion/sessions").json()["active_session_id"]
    welcome = client.get(f"/companion/sessions/{session_id}").json()["messages"][0]
    assert "I’m Zey" in welcome["content"]


def test_streamed_reply_uses_session_key_and_is_persisted(session_client) -> None:
    client, _, account, _ = session_client
    captured: dict[str, object] = {}

    class FakeSupervisor:
        async def proxy_stream(self, path, payload, *, session_key):
            captured.update({"path": path, "payload": payload, "session_key": session_key})
            yield b'data: {"event":"message.delta","delta":"We can build a durable plan."}\n\n'
            yield b'data: {"event":"run.completed","output":"We can build a durable plan."}\n\n'

    class StreamingRuntime:
        async def prepare(self, selected_account, _store):
            assert selected_account is account
            return SimpleNamespace(
                model="gpt-5.4",
                account_key="c" * 64,
                supervisor=FakeSupervisor(),
            )

        async def capture_refreshed_codex_credentials(self, _account_id):
            return None

    app.dependency_overrides[get_hermes_runtime_manager] = lambda: StreamingRuntime()
    conversation = client.post("/companion/sessions", json={}).json()
    session_id = conversation["id"]

    response = client.post(
        "/companion/chat/stream",
        json={"session_id": session_id, "message": "Help me make a plan."},
    )

    assert response.status_code == 200
    assert captured["session_key"] == (
        f"career-companion:web:{'c' * 64}:{session_id}"
    )
    restored = client.get(f"/companion/sessions/{session_id}").json()
    assert [message["role"] for message in restored["messages"]] == [
        "assistant",
        "user",
        "assistant",
    ]
    assert restored["messages"][-1]["content"] == "We can build a durable plan."
    assert restored["title"] == "Help me make a plan."
