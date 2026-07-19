from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.auth import AuthStore, AuthenticatedAccount, ProviderConnection
from app.main import app, get_store, require_current_account
from career_companion.database import ApprovalRecord, AuditEventRecord
from career_companion.paths import CompanionPaths
from career_companion.persistence import (
    account_session,
    clear_factory_cache,
    session_factory_for,
)
from career_companion.services import approvals as approval_service
from career_companion.services.approval_history import list_approval_history
from career_companion.services.approvals import (
    APPROVAL_ACTIONS,
    AUTHORIZATION_SNAPSHOT_KEY,
    consume_approval,
    decide_approval,
    payload_digest,
    request_approval,
)


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
def approval_client(tmp_path, monkeypatch):
    clear_factory_cache()
    monkeypatch.setenv("CAREER_COMPANION_HOME", str(tmp_path / "companion"))
    store = AuthStore(tmp_path / "auth.db", "s" * 48)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[require_current_account] = lambda: _account("account-a")
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()
    clear_factory_cache()


def _form_payload(label: str = "Ada") -> dict:
    return {
        "application_id": "00000000-0000-0000-0000-000000000001",
        "url": "https://jobs.example.test/apply?candidate=private",
        "fields": {"#name": label, "#email": "ada@example.test"},
        "files": {"#resume": "/private/workspace/resume.pdf"},
        "headless": False,
    }


def _create(client: TestClient, *, label: str = "Ada", ttl_minutes: int = 30):
    return client.post(
        "/api/v1/approvals",
        json={
            "action_type": "application.form_fill",
            "payload": _form_payload(label),
            "preview": {
                "summary": f"Fill the Example form for {label}",
                "untrusted": {"raw": "must not appear in history"},
            },
            "ttl_minutes": ttl_minutes,
        },
    )


def test_history_is_account_scoped_private_and_explains_the_exact_binding(
    approval_client,
) -> None:
    created = _create(approval_client)
    assert created.status_code == 200
    approval_id = created.json()["id"]

    response = approval_client.get("/api/v1/approvals/history?limit=20")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    page = response.json()
    assert page["schema_version"] == "approval-history-v1"
    assert page["pending_count"] == 1
    assert page["next_cursor"] is None
    assert len(page["items"]) == 1
    item = page["items"][0]
    assert item["id"] == approval_id
    assert item["state"] == "pending"
    assert item["usable"] is False
    assert item["authorization"]["title"] == "Fill one application form"
    assert "Submitting the form" in item["authorization"]["not_authorized"]
    assert item["authorization"]["binding"] == {
        "action_type": "application.form_fill",
        "algorithm": "sha256-canonical-json-v1",
        "payload_sha256": payload_digest(_form_payload()),
        "maximum_uses": 1,
        "remaining_uses": 0,
    }
    assert item["authorization"]["context"] == [
        {"label": "Target", "value": "jobs.example.test"},
        {
            "label": "Application",
            "value": "00000000-0000-0000-0000-000000000001",
        },
        {"label": "Fields", "value": "2"},
        {"label": "Attachments", "value": "1"},
    ]
    serialized = json.dumps(page)
    assert "ada@example.test" not in serialized
    assert "candidate=private" not in serialized
    assert "/private/workspace" not in serialized
    assert "must not appear in history" not in serialized

    app.dependency_overrides[require_current_account] = lambda: _account("account-b")
    isolated = approval_client.get("/api/v1/approvals/history")
    assert isolated.status_code == 200
    assert isolated.json()["items"] == []
    assert isolated.json()["pending_count"] == 0


def test_history_uses_stable_cursor_pages_without_gaps(approval_client) -> None:
    paths = CompanionPaths.discover().scoped_to("account-a")
    created_at = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    with account_session(paths) as session:
        for index in range(5):
            session.add(
                ApprovalRecord(
                    id=f"record-{index}",
                    action_type="application.form_fill",
                    payload_digest=f"{index:064x}",
                    preview={"summary": f"Request {index}"},
                    expires_at=created_at + timedelta(days=1),
                    decision="pending",
                    created_at=created_at,
                    updated_at=created_at,
                )
            )

    first = approval_client.get("/api/v1/approvals/history?limit=2").json()
    second = approval_client.get(
        "/api/v1/approvals/history",
        params={"limit": 2, "cursor": first["next_cursor"]},
    ).json()
    third = approval_client.get(
        "/api/v1/approvals/history",
        params={"limit": 2, "cursor": second["next_cursor"]},
    ).json()

    ids = [item["id"] for page in (first, second, third) for item in page["items"]]
    assert ids == ["record-4", "record-3", "record-2", "record-1", "record-0"]
    assert len(set(ids)) == 5
    assert first["next_cursor"]
    assert second["next_cursor"]
    assert third["next_cursor"] is None


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=51",
        "limit=20&cursor=%25%25%25",
        f"limit=20&cursor={'a' * 513}",
    ],
)
def test_history_rejects_invalid_bounds_and_cursors(approval_client, query) -> None:
    response = approval_client.get(f"/api/v1/approvals/history?{query}")
    assert response.status_code == 422


def test_history_computes_expiry_without_mutating_the_record_or_audit(tmp_path) -> None:
    paths = CompanionPaths.at_root(tmp_path / "companion").scoped_to("account-a")
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    with account_session(paths) as session:
        row = ApprovalRecord(
            action_type="application.form_fill",
            payload_digest="a" * 64,
            preview={"summary": "Expired request"},
            expires_at=now - timedelta(seconds=1),
            decision="approved",
        )
        session.add(row)
        session.flush()
        row_id = row.id
    with account_session(paths) as session:
        audit_before = session.scalar(select(func.count(AuditEventRecord.id)))
        page = list_approval_history(session, now=now)
        stored = session.get(ApprovalRecord, row_id)
        audit_after = session.scalar(select(func.count(AuditEventRecord.id)))
        assert page["items"][0]["state"] == "expired"
        assert page["items"][0]["usable"] is False
        assert stored is not None and stored.decision == "approved"
        assert audit_after == audit_before


def test_request_rules_are_bounded_and_snapshot_is_server_owned(tmp_path) -> None:
    paths = CompanionPaths.at_root(tmp_path / "companion").scoped_to("account-a")
    with account_session(paths) as session:
        approval = request_approval(
            session,
            "application.form_fill",
            _form_payload(),
            {
                "summary": "Review\u202e    exact\nrequest",
                AUTHORIZATION_SNAPSHOT_KEY: {"target_hostname": "attacker.test"},
            },
        )
        assert approval.preview["summary"] == "Review exact request"
        assert approval.preview[AUTHORIZATION_SNAPSHOT_KEY]["target_hostname"] == (
            "jobs.example.test"
        )

        with pytest.raises(ValueError, match="Unsupported approval action"):
            request_approval(session, "arbitrary.write", {}, {})
        with pytest.raises(ValueError, match="never automated"):
            request_approval(session, "application.submit", {}, {})
        with pytest.raises(ValueError, match="TTL"):
            request_approval(session, "application.form_fill", {}, {}, ttl_minutes=0)
        with pytest.raises(ValueError, match="NaN"):
            request_approval(
                session,
                "application.form_fill",
                {"number": float("nan")},
                {},
            )
        with pytest.raises(ValueError, match="depth"):
            deeply_nested: dict = {}
            cursor = deeply_nested
            for _ in range(9):
                cursor["child"] = {}
                cursor = cursor["child"]
            request_approval(session, "application.form_fill", deeply_nested, {})
        with pytest.raises(ValueError, match="byte limit"):
            request_approval(
                session,
                "application.form_fill",
                {},
                {"summary": "x" * 17_000},
            )


def test_unsupported_legacy_records_are_conservative(tmp_path) -> None:
    paths = CompanionPaths.at_root(tmp_path / "companion").scoped_to("account-a")
    with account_session(paths) as session:
        session.add(
            ApprovalRecord(
                action_type="legacy.unknown",
                payload_digest="b" * 64,
                preview={"summary": "Old request"},
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                decision="approved",
            )
        )
    with account_session(paths) as session:
        item = list_approval_history(session)["items"][0]
        assert item["authorization"]["title"] == "Unsupported legacy approval"
        assert item["authorization"]["effect"].startswith("No current")
        assert item["usable"] is False
        assert item["authorization"]["binding"]["remaining_uses"] == 0


def test_legacy_action_type_and_context_are_safe_for_display(tmp_path) -> None:
    paths = CompanionPaths.at_root(tmp_path / "companion").scoped_to("account-a")
    with account_session(paths) as session:
        session.add(
            ApprovalRecord(
                action_type="legacy.\u202ewrite",
                payload_digest="b" * 64,
                preview={
                    AUTHORIZATION_SNAPSHOT_KEY: {
                        "action_type": "legacy.\u202ewrite",
                        "application_id": "job\u202e    42",
                    },
                },
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                decision="approved",
            )
        )
    with account_session(paths) as session:
        item = list_approval_history(session)["items"][0]
        assert item["authorization"]["binding"]["action_type"] == "legacy.unknown"
        assert item["authorization"]["context"] == [
            {"label": "Application", "value": "job 42"}
        ]


def test_concurrent_decisions_have_exactly_one_winner(tmp_path) -> None:
    clear_factory_cache()
    paths = CompanionPaths.at_root(tmp_path / "companion").scoped_to("account-a")
    factory = session_factory_for(paths)
    with factory() as session:
        approval = request_approval(
            session,
            "application.form_fill",
            _form_payload(),
            {"summary": "Choose once"},
        )
        approval_id = approval.id
        session.commit()
    barrier = Barrier(2)

    def decide(decision: str) -> str:
        with factory() as session:
            session.get(ApprovalRecord, approval_id)
            barrier.wait()
            try:
                decide_approval(session, approval_id, decision)
                session.commit()
                return decision
            except ValueError:
                session.rollback()
                return "lost"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(decide, "approved"),
            executor.submit(decide, "denied"),
        ]
        results = sorted(future.result() for future in futures)
    assert results.count("lost") == 1
    with factory() as session:
        stored = session.get(ApprovalRecord, approval_id)
        assert stored is not None and stored.decision in {"approved", "denied"}
        transitions = session.scalar(
            select(func.count(AuditEventRecord.id)).where(
                AuditEventRecord.subject_id == approval_id,
                AuditEventRecord.event_type.in_({"approval.approved", "approval.denied"}),
            )
        )
        assert transitions == 1
    clear_factory_cache()


def test_concurrent_consumers_can_use_one_token_only_once(tmp_path) -> None:
    clear_factory_cache()
    paths = CompanionPaths.at_root(tmp_path / "companion").scoped_to("account-a")
    factory = session_factory_for(paths)
    payload = _form_payload()
    with factory() as session:
        approval = request_approval(
            session,
            "application.form_fill",
            payload,
            {"summary": "Use once"},
        )
        decide_approval(session, approval.id, "approved")
        approval_id = approval.id
        session.commit()
    barrier = Barrier(2)

    def consume() -> str:
        with factory() as session:
            barrier.wait()
            try:
                consume_approval(session, "application.form_fill", payload)
                session.commit()
                return "consumed"
            except PermissionError:
                session.rollback()
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(consume) for _ in range(2)]
        assert sorted(future.result() for future in futures) == ["consumed", "rejected"]
    with factory() as session:
        stored = session.get(ApprovalRecord, approval_id)
        assert stored is not None and stored.decision == "consumed"
        consumed_audits = session.scalar(
            select(func.count(AuditEventRecord.id)).where(
                AuditEventRecord.subject_id == approval_id,
                AuditEventRecord.event_type == "approval.consumed",
            )
        )
        assert consumed_audits == 1
    clear_factory_cache()


def test_consumption_and_audit_roll_back_together(tmp_path, monkeypatch) -> None:
    paths = CompanionPaths.at_root(tmp_path / "companion").scoped_to("account-a")
    payload = _form_payload()
    with account_session(paths) as session:
        approval = request_approval(
            session,
            "application.form_fill",
            payload,
            {"summary": "Atomic request"},
        )
        decide_approval(session, approval.id, "approved")
        approval_id = approval.id

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(approval_service, "record_audit", fail_audit)
    factory = session_factory_for(paths)
    with factory() as session:
        with pytest.raises(RuntimeError, match="audit failed"):
            consume_approval(session, "application.form_fill", payload)
        session.rollback()
    with factory() as session:
        stored = session.get(ApprovalRecord, approval_id)
        assert stored is not None and stored.decision == "approved"


def test_action_registry_covers_every_declared_external_action() -> None:
    assert set(APPROVAL_ACTIONS) == {
        "application.form_fill",
        "email.draft",
        "email.send",
        "calendar.create",
        "notification.send",
        "mcp.write",
    }
    assert [name for name, item in APPROVAL_ACTIONS.items() if item.consumer_enabled] == [
        "application.form_fill"
    ]
