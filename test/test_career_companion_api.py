from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth import AuthStore, AuthenticatedAccount, ProviderConnection
from app.main import app, get_store, require_current_account
from career_companion.persistence import clear_factory_cache


def _account(user_id: str) -> AuthenticatedAccount:
    return AuthenticatedAccount(
        user_id=user_id,
        email=f"{user_id}@example.test",
        display_name=user_id,
        identity_method="local",
        active_provider="api_key",
        provider_connection=ProviderConnection(
            provider="api_key",
            credential=b"sk-test-key-with-enough-characters",
        ),
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    clear_factory_cache()
    monkeypatch.setenv("CAREER_COMPANION_HOME", str(tmp_path / "companion"))
    store = AuthStore(tmp_path / "auth.db", "s" * 48)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[require_current_account] = lambda: _account("account-a")
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    clear_factory_cache()


def _job_payload() -> dict:
    return {
        "spec": {
            "title": "AI Engineer",
            "company": "Example GmbH",
            "locations": ["Berlin, Germany"],
            "description": "We require Python, retrieval, and evaluation experience.",
            "requirements": ["Python", "retrieval", "evaluation"],
            "source_type": "manual",
        },
        "canonical_url": "https://jobs.example.test/ai-engineer?utm_source=newsletter",
    }


def test_versioned_companion_routes_require_provider_connection(client) -> None:
    app.dependency_overrides.pop(require_current_account)

    response = client.get("/api/v1/jobs")

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Choose an AI connection in Settings before using CareerPilot"
    }


def test_jobs_are_deduplicated_and_isolated_by_account(client) -> None:
    created = client.post("/api/v1/jobs", json=_job_payload())
    duplicate = client.post("/api/v1/jobs", json=_job_payload())

    assert created.status_code == 200
    assert created.json()["created"] is True
    assert created.json()["canonical_url"] == "https://jobs.example.test/ai-engineer"
    assert duplicate.status_code == 200
    assert duplicate.json()["created"] is False
    assert len(client.get("/api/v1/jobs").json()) == 1

    app.dependency_overrides[require_current_account] = lambda: _account("account-b")
    assert client.get("/api/v1/jobs").json() == []


def test_profile_api_rejects_unsupported_verified_claim(client) -> None:
    response = client.put(
        "/api/v1/onboarding/profile",
        json={
            "display_name": "Candidate",
            "claims": [
                {
                    "key": "skill",
                    "value": "Kubernetes",
                    "status": "verified",
                    "evidence": [],
                }
            ],
        },
    )

    assert response.status_code == 422
    assert "must include evidence" in response.json()["detail"]


def test_application_state_api_requires_confirmed_submission(client) -> None:
    job_id = client.post("/api/v1/jobs", json=_job_payload()).json()["id"]
    started = client.post("/api/v1/applications", params={"job_id": job_id})
    assert started.status_code == 200
    application_id = started.json()["id"]

    for status_name in ("scored", "approved", "tailoring", "ready"):
        response = client.post(
            f"/api/v1/applications/{application_id}/status",
            json={"status": status_name},
        )
        assert response.status_code == 200

    unconfirmed = client.post(
        f"/api/v1/applications/{application_id}/status",
        json={"status": "submitted"},
    )
    assert unconfirmed.status_code == 409
    assert "explicit user confirmation" in unconfirmed.json()["detail"]

    confirmed = client.post(
        f"/api/v1/applications/{application_id}/status",
        json={"status": "submitted", "confirmed_by_user": True},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "submitted"
    assert confirmed.json()["submitted_at"] is not None


def test_integrated_api_uses_existing_provider_settings_only(client) -> None:
    duplicate_writer = client.post(
        "/api/v1/provider-auth/openai-api",
        data={"api_key": "sk-this-must-never-be-written-here"},
    )
    existing_settings = client.get("/settings/providers")

    assert duplicate_writer.status_code == 404
    assert existing_settings.status_code == 200


def test_default_model_routes_are_initialized_per_account(client) -> None:
    response = client.get("/api/v1/model-routes")

    assert response.status_code == 200
    routes = {route["name"]: route for route in response.json()}
    assert set(routes) == {
        "interactive",
        "research",
        "extraction",
        "tailoring",
        "evaluation",
        "memory-review",
        "compression",
        "cron",
    }
    assert routes["cron"]["scheduled"] is True
    assert routes["cron"]["fallback_policy"] == "none"
