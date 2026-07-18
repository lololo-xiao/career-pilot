from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.auth import AuthCredentialError, AuthStore
from app.dependencies import get_local_companion_paths
from app.main import app, get_api_key_validator, get_oauth_manager, get_store
from career_companion.paths import CompanionPaths


client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_local_dependencies(tmp_path):
    store = AuthStore(tmp_path / "auth.db", "a" * 48)
    paths = CompanionPaths.at_root(tmp_path / "companion")
    paths.create()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_local_companion_paths] = lambda: paths
    app.dependency_overrides[get_api_key_validator] = lambda: lambda _key: None
    yield store
    app.dependency_overrides.clear()


def test_local_session_is_created_without_login() -> None:
    response = client.get("/local/session")

    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert response.json()["user"]["id"]
    assert response.json()["user"]["active_provider"] is None
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_account_login_endpoints_are_removed() -> None:
    paths = app.openapi()["paths"]

    assert "/auth/register" not in paths
    assert "/auth/login" not in paths
    assert "/auth/google" not in paths
    assert "/auth/logout" not in paths


def test_provider_settings_are_available_to_the_local_workspace() -> None:
    response = client.get("/settings/providers")

    assert response.status_code == 200
    assert response.json()["active_provider"] is None


def test_capabilities_show_tools_mcp_and_configure_encrypted_web_search(
    isolated_local_dependencies,
) -> None:
    store = isolated_local_dependencies
    before = client.get("/settings/capabilities")

    assert before.status_code == 200
    payload = before.json()
    assert payload["web_search"]["configured"] is False
    group_states = {group["id"]: group["state"] for group in payload["groups"]}
    assert group_states["local-workspace"] == "disabled"
    assert group_states["web-search"] == "disabled"
    assert group_states["browser-assistance"] == "disabled"
    career_tools = next(
        group["tools"] for group in payload["groups"] if group["id"] == "career-workspace"
    )
    assert "career_public_job_discover" in career_tools
    assert "career_job_track_selected" in career_tools
    assert "career_application_decide" in career_tools
    assert {server["name"] for server in payload["mcp_servers"]} == {
        "gmail",
        "google-calendar",
        "google-sheets",
        "linkedin-search",
    }
    assert not any(server["enabled"] for server in payload["mcp_servers"])

    search_key = "brave-search-secret-with-enough-characters"
    connected = client.put(
        "/settings/capabilities/web-search",
        json={"api_key": search_key},
    )

    assert connected.status_code == 200
    assert connected.json()["web_search"]["configured"] is True
    assert {group["id"]: group["state"] for group in connected.json()["groups"]}[
        "web-search"
    ] == "disabled"
    assert search_key not in connected.text
    assert search_key.encode() not in store.path.read_bytes()
    assert client.get("/local/session").json()["user"]["active_provider"] is None

    removed = client.delete("/settings/capabilities/web-search")
    assert removed.status_code == 200
    assert removed.json()["web_search"]["configured"] is False


def test_api_key_is_connected_and_can_be_removed() -> None:
    api_key = "sk-project-secret-with-enough-characters"

    response = client.post(
        "/settings/providers/api-key",
        json={"api_key": api_key},
    )

    assert response.status_code == 200
    assert response.json()["user"]["active_provider"] == "api_key"
    assert api_key not in response.text
    settings = client.get("/settings/providers").json()
    assert settings["active_provider"] == "api_key"
    assert settings["connections"][1]["connected"] is True

    removed = client.delete("/settings/providers/api_key")
    assert removed.status_code == 200
    assert removed.json()["active_provider"] is None


def test_api_key_connection_rejects_failed_validation() -> None:
    def reject(_: str) -> None:
        raise AuthCredentialError("OpenAI rejected this API key")

    app.dependency_overrides[get_api_key_validator] = lambda: reject
    response = client.post(
        "/settings/providers/api-key",
        json={"api_key": "sk-project-secret-with-enough-characters"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "OpenAI rejected this API key"}


def test_codex_device_login_connects_plan_to_local_workspace() -> None:
    local = client.get("/local/session").json()
    attempt = SimpleNamespace(
        attempt_id="attempt-1",
        user_id=None,
        login_id="login-1",
        verification_url="https://auth.openai.com/codex/device",
        user_code="ABCD-1234",
        expires_at=2_000_000_000,
        status="completed",
        email="chatgpt@example.com",
        plan_type="plus",
        credentials=b'{"tokens":{"access_token":"encrypted-at-rest"}}',
        error=None,
    )

    class FakeManager:
        finished: list[str] = []

        def start(self, user_id: str):
            attempt.user_id = user_id
            return attempt

        def get(self, attempt_id: str):
            return attempt if attempt_id == attempt.attempt_id else None

        def poll(self, attempt_id: str):
            return self.get(attempt_id)

        def finish(self, attempt_id: str) -> None:
            self.finished.append(attempt_id)

    manager = FakeManager()
    app.dependency_overrides[get_oauth_manager] = lambda: manager

    started = client.post("/settings/providers/codex/start")
    completed = client.post("/settings/providers/codex/status/attempt-1")

    assert started.status_code == 200
    assert attempt.user_id == local["user"]["id"]
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["user"]["provider_email"] == "chatgpt@example.com"
    assert completed.json()["user"]["plan_type"] == "plus"
    assert manager.finished == ["attempt-1"]


def test_codex_attempt_from_another_workspace_is_hidden() -> None:
    attempt = SimpleNamespace(user_id="another-workspace")

    class FakeManager:
        def get(self, _attempt_id: str):
            return attempt

    app.dependency_overrides[get_oauth_manager] = lambda: FakeManager()

    response = client.post("/settings/providers/codex/status/not-mine")

    assert response.status_code == 404
