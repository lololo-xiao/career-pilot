from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.auth import AuthenticatedAccount, ProviderConnection
from app.dependencies import get_companion_paths, require_current_account
from app.main import app
from career_companion.database import (
    ApplicationRecord,
    ApprovalRecord,
    AuditEventRecord,
    JobRecord,
    RevisionRecord,
)
from career_companion.guided_discovery_router import require_direct_loopback_client
from career_companion.paths import CompanionPaths
from career_companion.persistence import clear_factory_cache, session_factory_for
from career_companion.services.discovery import parse_greenhouse_jobs


def _account(user_id: str, *, connected: bool = True) -> AuthenticatedAccount:
    connection = (
        ProviderConnection(
            provider="api_key",
            credential=b"sk-test-key-with-enough-characters",
        )
        if connected
        else None
    )
    return AuthenticatedAccount(
        user_id=user_id,
        email=f"{user_id}@example.test",
        display_name=user_id,
        identity_method="local",
        active_provider="api_key" if connected else None,
        provider_connection=connection,
    )


@pytest.fixture
def guided_client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    clear_factory_cache()
    monkeypatch.setenv("CAREER_COMPANION_HOME", str(tmp_path / "companion"))
    app.dependency_overrides[require_current_account] = lambda: _account("account-a")
    with TestClient(
        app,
        client=("127.0.0.1", 50000),
        raise_server_exceptions=False,
    ) as client:
        yield client
    app.dependency_overrides.clear()
    clear_factory_cache()


def _public_jobs(count: int):
    return parse_greenhouse_jobs(
        "example-labs",
        {
            "jobs": [
                {
                    "title": f"Role {index}",
                    "location": {"name": "Remote"},
                    "content": f"Public description {index}",
                    "absolute_url": (
                        f"https://boards.greenhouse.io/example-labs/jobs/{index}"
                    ),
                }
                for index in range(count)
            ]
        },
    )


def _zero_mutation_counts(paths: CompanionPaths) -> dict[str, int]:
    with session_factory_for(paths)() as session:
        return {
            model.__tablename__: session.scalar(
                select(func.count()).select_from(model)
            )
            or 0
            for model in (
                JobRecord,
                ApplicationRecord,
                ApprovalRecord,
                RevisionRecord,
                AuditEventRecord,
            )
        }


def _direct_request(client: tuple[str, int] | None) -> Request:
    return Request({"type": "http", "client": client, "headers": []})


def _request_with_origin(
    client: tuple[str, int] | None,
    origin: str,
) -> Request:
    return Request(
        {
            "type": "http",
            "client": client,
            "headers": [(b"origin", origin.encode("ascii"))],
        }
    )


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_direct_loopback_helper_accepts_ipv4_and_ipv6(host: str) -> None:
    require_direct_loopback_client(_direct_request((host, 50_000)))


@pytest.mark.parametrize(
    "client",
    [
        None,
        ("localhost", 50_000),
        ("not-an-ip", 50_000),
        ("203.0.113.10", 50_000),
    ],
)
def test_direct_loopback_helper_rejects_missing_malformed_and_remote_clients(
    client: tuple[str, int] | None,
) -> None:
    with pytest.raises(HTTPException) as caught:
        require_direct_loopback_client(_direct_request(client))

    assert caught.value.status_code == 403
    assert caught.value.detail == (
        "Public job discovery is available only from this device"
    )


def test_private_mobile_override_requires_flag_and_exact_capacitor_origin(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CAREERPILOT_ALLOW_PRIVATE_MOBILE_DISCOVERY", raising=False)
    remote = ("203.0.113.10", 50_000)
    request = _request_with_origin(remote, "capacitor://localhost")
    with pytest.raises(HTTPException):
        require_direct_loopback_client(request)

    monkeypatch.setenv("CAREERPILOT_ALLOW_PRIVATE_MOBILE_DISCOVERY", "true")
    require_direct_loopback_client(request)
    with pytest.raises(HTTPException):
        require_direct_loopback_client(
            _request_with_origin(remote, "https://untrusted.example")
        )


def test_preview_is_bounded_and_creates_no_operational_rows(
    guided_client: TestClient,
    monkeypatch,
) -> None:
    paths = CompanionPaths.discover().scoped_to("account-a")
    before = _zero_mutation_counts(paths)

    async def fake_discovery(provider, identifier):
        assert provider == "greenhouse"
        assert identifier == "example-labs"
        return _public_jobs(31)

    monkeypatch.setattr(
        "career_companion.guided_discovery_router.discover_public_jobs",
        fake_discovery,
    )
    app.dependency_overrides[get_companion_paths] = lambda: (_ for _ in ()).throw(
        AssertionError("preview must not resolve or initialize an operational workspace")
    )

    response = guided_client.post(
        "/api/v1/jobs/discover-public",
        json={
            "provider": "greenhouse",
            "company_identifier": "example-labs",
            "limit": 25,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["activity"] == {
        "type": "public_network_read",
        "provider": "greenhouse",
        "company_identifier": "example-labs",
    }
    assert payload["discovered"] == 31
    assert payload["returned"] == 25
    assert len(payload["jobs"]) == 25
    assert payload["stored"] == 0
    assert _zero_mutation_counts(paths) == before == {
        "jobs": 0,
        "applications": 0,
        "approvals": 0,
        "revisions": 0,
        "audit_events": 0,
    }


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_bare_loopback_endpoint_allows_ipv4_and_ipv6(
    guided_client: TestClient,
    monkeypatch,
    host: str,
) -> None:
    del guided_client

    async def fake_discovery(_provider, _identifier):
        return []

    monkeypatch.setattr(
        "career_companion.guided_discovery_router.discover_public_jobs",
        fake_discovery,
    )
    with TestClient(
        app,
        client=(host, 50_000),
        raise_server_exceptions=False,
    ) as loopback_client:
        response = loopback_client.post(
            "/api/v1/jobs/discover-public",
            json={"provider": "lever", "company_identifier": "example"},
        )

    assert response.status_code == 200
    assert response.json()["stored"] == 0


@pytest.mark.parametrize(
    ("host", "header", "value"),
    [
        ("127.0.0.1", "X-Forwarded-For", "203.0.113.10"),
        ("::1", "Forwarded", "for=203.0.113.10"),
        ("127.0.0.1", "X-Real-IP", "203.0.113.10"),
    ],
)
def test_loopback_peer_with_identity_header_is_rejected_through_uvicorn(
    guided_client: TestClient,
    monkeypatch,
    host: str,
    header: str,
    value: str,
) -> None:
    del guided_client
    calls = {"account": 0, "provider": 0}

    def resolve_account():
        calls["account"] += 1
        return _account("account-a")

    async def unexpected_discovery(*_args):
        calls["provider"] += 1
        return []

    app.dependency_overrides[require_current_account] = resolve_account
    monkeypatch.setattr(
        "career_companion.guided_discovery_router.discover_public_jobs",
        unexpected_discovery,
    )
    proxied_app = ProxyHeadersMiddleware(app, trusted_hosts="*")
    with TestClient(
        proxied_app,
        client=(host, 50_000),
        raise_server_exceptions=False,
    ) as proxied_client:
        response = proxied_client.post(
            "/api/v1/jobs/discover-public",
            headers={header: value},
            json={"provider": "lever", "company_identifier": "example"},
        )

    assert response.status_code == 403
    assert calls == {"account": 0, "provider": 0}


def test_remote_peer_is_rejected_before_auth_workspace_or_provider_read(
    guided_client: TestClient,
    monkeypatch,
) -> None:
    del guided_client
    paths = CompanionPaths.discover().scoped_to("account-a")
    before = _zero_mutation_counts(paths)
    calls = {"account": 0, "provider": 0}

    def resolve_account():
        calls["account"] += 1
        return _account("account-a")

    async def unexpected_discovery(*_args):
        calls["provider"] += 1
        return _public_jobs(1)

    app.dependency_overrides[require_current_account] = resolve_account
    monkeypatch.setattr(
        "career_companion.guided_discovery_router.discover_public_jobs",
        unexpected_discovery,
    )
    with TestClient(
        app,
        client=("203.0.113.10", 50_000),
        raise_server_exceptions=False,
    ) as remote_client:
        response = remote_client.post(
            "/api/v1/jobs/discover-public",
            json={"provider": "greenhouse", "company_identifier": "example"},
        )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Public job discovery is available only from this device"
    }
    assert calls == {"account": 0, "provider": 0}
    assert _zero_mutation_counts(paths) == before == {
        "jobs": 0,
        "applications": 0,
        "approvals": 0,
        "revisions": 0,
        "audit_events": 0,
    }


def test_remote_peer_cannot_spoof_loopback_through_uvicorn_proxy_middleware(
    guided_client: TestClient,
    monkeypatch,
) -> None:
    del guided_client
    paths = CompanionPaths.discover().scoped_to("account-a")
    before = _zero_mutation_counts(paths)
    calls = {"account": 0, "provider": 0}

    def resolve_account():
        calls["account"] += 1
        return _account("account-a")

    async def unexpected_discovery(*_args):
        calls["provider"] += 1
        return _public_jobs(1)

    app.dependency_overrides[require_current_account] = resolve_account
    monkeypatch.setattr(
        "career_companion.guided_discovery_router.discover_public_jobs",
        unexpected_discovery,
    )
    proxied_app = ProxyHeadersMiddleware(app, trusted_hosts="*")
    with TestClient(
        proxied_app,
        client=("203.0.113.10", 50_000),
        raise_server_exceptions=False,
    ) as remote_client:
        response = remote_client.post(
            "/api/v1/jobs/discover-public",
            headers={"X-Forwarded-For": "127.0.0.1"},
            json={"provider": "greenhouse", "company_identifier": "example"},
        )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Public job discovery is available only from this device"
    }
    assert calls == {"account": 0, "provider": 0}
    assert _zero_mutation_counts(paths) == before == {
        "jobs": 0,
        "applications": 0,
        "approvals": 0,
        "revisions": 0,
        "audit_events": 0,
    }


def test_preview_api_rejects_more_than_25_before_provider_read(
    guided_client: TestClient,
    monkeypatch,
) -> None:
    async def unexpected(*_args):
        raise AssertionError("invalid request must not reach the provider")

    monkeypatch.setattr(
        "career_companion.guided_discovery_router.discover_public_jobs",
        unexpected,
    )
    response = guided_client.post(
        "/api/v1/jobs/discover-public",
        json={
            "provider": "lever",
            "company_identifier": "example",
            "limit": 26,
        },
    )

    assert response.status_code == 422


def test_preview_requires_a_connected_provider_account(
    guided_client: TestClient,
) -> None:
    app.dependency_overrides[require_current_account] = lambda: _account(
        "account-b",
        connected=False,
    )

    response = guided_client.post(
        "/api/v1/jobs/discover-public",
        json={"provider": "lever", "company_identifier": "example"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Choose an AI connection in Settings before using CareerPilot"
    }


def test_checked_jobs_use_shared_account_isolated_dedup_path(
    guided_client: TestClient,
    monkeypatch,
) -> None:
    async def fake_discovery(_provider, _identifier):
        return _public_jobs(2)

    monkeypatch.setattr(
        "career_companion.guided_discovery_router.discover_public_jobs",
        fake_discovery,
    )
    preview = guided_client.post(
        "/api/v1/jobs/discover-public",
        json={"provider": "greenhouse", "company_identifier": "example-labs"},
    ).json()
    assert preview["stored"] == 0

    selected = preview["jobs"][1]
    write = {"spec": selected, "canonical_url": selected["source_url"]}
    first = guided_client.post("/api/v1/jobs", json=write)
    duplicate = guided_client.post("/api/v1/jobs", json=write)

    assert first.status_code == 200
    assert first.json()["created"] is True
    assert duplicate.json()["created"] is False
    assert duplicate.json()["id"] == first.json()["id"]
    assert len(guided_client.get("/api/v1/jobs").json()) == 1
    paths = CompanionPaths.discover().scoped_to("account-a")
    with session_factory_for(paths)() as session:
        audit_events = session.scalars(select(AuditEventRecord)).all()
    assert [event.event_type for event in audit_events] == ["job.discovered"]

    app.dependency_overrides[require_current_account] = lambda: _account("account-b")
    assert guided_client.get("/api/v1/jobs").json() == []
