from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.auth import AuthStore, AuthenticatedAccount, ProviderConnection
from app.main import app, get_store, require_current_account
from career_companion.database import (
    ApplicationRecord,
    ApprovalRecord,
    ArtifactRecord,
    AuditEventRecord,
    JobRecord,
)
from career_companion.paths import CompanionPaths
from career_companion.persistence import (
    account_session,
    clear_factory_cache,
    session_factory_for,
)
from career_companion.services import approvals as approval_service
from career_companion.schemas import FormFillRequest
from career_companion.services.approval_history import list_approval_history
from career_companion.services.approvals import (
    APPROVAL_ACTIONS,
    AUTHORIZATION_SNAPSHOT_KEY,
    CONSUMER_VALIDATION_MARKER_KEY,
    CONSUMER_VALIDATION_VERSIONS,
    consume_approval,
    current_consumer_validation_marker,
    decide_approval,
    payload_digest,
    request_approval,
)
from career_companion.services.form_fill import (
    FormFillRequestError,
    canonical_form_fill_payload,
)

APPLICATION_ID = "00000000-0000-0000-0000-000000000001"
ARTIFACT_ID = "00000000-0000-0000-0000-000000000002"
JOB_ID = "00000000-0000-0000-0000-000000000003"


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
    paths = CompanionPaths.discover().scoped_to("account-a")
    with account_session(paths) as session:
        _seed_form_resources(session, paths)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()
    clear_factory_cache()


def _form_payload(label: str = "Ada") -> dict:
    return {
        "application_id": APPLICATION_ID,
        "url": "https://jobs.example.test/apply?candidate=private",
        "fields": {"#name": label, "#email": "ada@example.test"},
        "files": {"#resume": ARTIFACT_ID},
        "headless": False,
    }


def _seed_form_resources(session, paths: CompanionPaths) -> ArtifactRecord:
    paths.create()
    attachment = paths.workspace / "approved-resume.pdf"
    approved_bytes = b"approved resume"
    attachment.write_bytes(approved_bytes)
    job = JobRecord(
        id=JOB_ID,
        company="Example GmbH",
        title="Engineer",
        canonical_url="https://jobs.example.test/role",
        fingerprint="f" * 64,
        normalized_spec={},
    )
    application = ApplicationRecord(id=APPLICATION_ID, job=job)
    artifact = ArtifactRecord(
        id=ARTIFACT_ID,
        application=application,
        kind="cv",
        version=1,
        path=str(attachment),
        sha256=hashlib.sha256(approved_bytes).hexdigest(),
        approved=True,
    )
    session.add_all([job, application, artifact])
    session.flush()
    return artifact


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


def _post_form_approval(client: TestClient, payload: dict, preview: dict | None = None):
    return client.post(
        "/api/v1/approvals",
        json={
            "action_type": "application.form_fill",
            "payload": payload,
            "preview": preview or {"summary": "Review the exact form fill"},
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
        _seed_form_resources(session, paths)
        approval = request_approval(
            session,
            "application.form_fill",
            _form_payload(),
            {
                "summary": "Review\u202e    exact\nrequest",
                AUTHORIZATION_SNAPSHOT_KEY: {
                    "target_hostname": "attacker.test",
                    CONSUMER_VALIDATION_MARKER_KEY: {
                        "consumer": "application.form_fill",
                        "version": 999,
                    },
                },
            },
            paths=paths,
        )
        assert approval.preview["summary"] == "Review exact request"
        assert approval.preview[AUTHORIZATION_SNAPSHOT_KEY]["target_hostname"] == (
            "jobs.example.test"
        )
        assert approval.preview[AUTHORIZATION_SNAPSHOT_KEY][
            CONSUMER_VALIDATION_MARKER_KEY
        ] == current_consumer_validation_marker("application.form_fill")

        with pytest.raises(ValueError, match="Unsupported approval action"):
            request_approval(session, "arbitrary.write", {}, {})
        with pytest.raises(ValueError, match="never automated"):
            request_approval(session, "application.submit", {}, {})
        with pytest.raises(ValueError, match="TTL"):
            request_approval(session, "application.form_fill", {}, {}, ttl_minutes=0)
        with pytest.raises(ValueError, match="NaN"):
            request_approval(
                session,
                "email.draft",
                {"number": float("nan")},
                {},
            )
        with pytest.raises(ValueError, match="depth"):
            deeply_nested: dict = {}
            cursor = deeply_nested
            for _ in range(9):
                cursor["child"] = {}
                cursor = cursor["child"]
            request_approval(session, "email.draft", deeply_nested, {})
        with pytest.raises(ValueError, match="byte limit"):
            request_approval(
                session,
                "email.draft",
                {},
                {"summary": "x" * 17_000},
            )


def test_generic_api_cannot_mint_or_spoof_a_usable_mcp_probe_approval(
    approval_client,
) -> None:
    secret = "MCP_GENERIC_SPOOF_SECRET_7a2f"
    payload = {
        "operation": "mcp.probe",
        "version": 1,
        "server": {
            "name": "spoofed-server",
            "transport": "stdio",
            "description": f"untrusted {secret}",
            "command": f"/private/{secret}/server",
            "args": [f"--token={secret}"],
            "url": f"https://example.test/private/{secret}?token={secret}",
            "environment": {"TOKEN": secret},
        },
    }
    response = approval_client.post(
        "/api/v1/approvals",
        json={
            "action_type": "mcp.probe",
            "payload": payload,
            "preview": {
                "summary": f"attacker-controlled {secret}",
                AUTHORIZATION_SNAPSHOT_KEY: {
                    "action_type": "mcp.probe",
                    CONSUMER_VALIDATION_MARKER_KEY: {
                        "consumer": "mcp.probe",
                        "version": CONSUMER_VALIDATION_VERSIONS["mcp.probe"],
                    },
                },
            },
            "consumer_validated": True,
        },
    )

    assert response.status_code == 200
    created = response.json()
    snapshot = created["preview"][AUTHORIZATION_SNAPSHOT_KEY]
    assert snapshot == {
        "action_type": "mcp.probe",
        "server_name": "spoofed-server",
        "mcp_transport": "stdio",
    }
    approval_id = created["id"]
    decided = approval_client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": "approved"},
    )
    assert decided.status_code == 200

    paths = CompanionPaths.discover().scoped_to("account-a")
    with account_session(paths) as session:
        with pytest.raises(PermissionError, match="matching approval"):
            consume_approval(
                session,
                "mcp.probe",
                payload,
                approval_id=approval_id,
            )
        history = list_approval_history(session)

    item = history["items"][0]
    assert item["id"] == approval_id
    assert item["state"] == "approved"
    assert item["usable"] is False
    assert item["request_summary"] is None
    assert item["authorization"]["context"] == [
        {"label": "Server", "value": "spoofed-server"},
        {"label": "Transport", "value": "Local command (stdio)"},
    ]
    assert secret not in json.dumps(history, default=str)


def test_form_fill_approval_hashes_the_shared_normalized_defaulted_payload(
    approval_client,
) -> None:
    raw_payload = {
        "application_id": f"  {APPLICATION_ID}  ",
        "url": "  HTTPS://JOBS.EXAMPLE.TEST:443/apply  ",
        "fields": {"  #name  ": "Ada"},
    }

    created = _post_form_approval(approval_client, raw_payload)

    assert created.status_code == 200
    canonical = canonical_form_fill_payload(raw_payload)
    assert FormFillRequest.model_validate(raw_payload).model_dump(mode="python") == canonical
    assert canonical == {
        "application_id": APPLICATION_ID,
        "url": "https://jobs.example.test/apply",
        "fields": {"#name": "Ada"},
        "files": {},
        "headless": False,
    }
    assert created.json()["payload_digest"] == payload_digest(canonical)
    assert created.json()["payload_digest"] != payload_digest(raw_payload)

    decided = approval_client.post(
        f"/api/v1/approvals/{created.json()['id']}/decision",
        json={"decision": "approved"},
    )
    assert decided.status_code == 200
    paths = CompanionPaths.discover().scoped_to("account-a")
    with account_session(paths) as session:
        consumed = consume_approval(session, "application.form_fill", raw_payload)
        assert consumed.id == created.json()["id"]


@pytest.mark.parametrize(
    "payload",
    [
        {"url": "https://jobs.example.test/apply"},
        {
            "application_id": APPLICATION_ID,
            "url": "https://jobs.example.test/apply",
            "unexpected": True,
        },
        {
            "application_id": APPLICATION_ID,
            "url": "https://jobs.example.test/apply",
            "headless": "false",
        },
        {
            "application_id": APPLICATION_ID,
            "url": "https://jobs.example.test/apply",
            "fields": {"#name": 42},
        },
        {
            "application_id": APPLICATION_ID,
            "url": "https://jobs.example.test/apply",
            "files": {"#resume": 42},
        },
        {
            "application_id": APPLICATION_ID,
            "url": "https://jobs.example.test/apply",
            "fields": {" #target ": "Ada"},
            "files": {"#target": ARTIFACT_ID},
        },
        {
            "application_id": APPLICATION_ID,
            "url": "https://jobs.example.test/apply",
            "files": {f"#file-{index}": ARTIFACT_ID for index in range(11)},
        },
    ],
)
def test_form_fill_approval_rejects_missing_extra_coerced_or_ambiguous_payloads(
    approval_client,
    payload,
) -> None:
    response = _post_form_approval(approval_client, payload)

    assert response.status_code == 422
    paths = CompanionPaths.discover().scoped_to("account-a")
    with account_session(paths) as session:
        assert session.scalar(select(func.count(ApprovalRecord.id))) == 0
        assert session.scalar(select(func.count(AuditEventRecord.id))) == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://b\u00fccher.example/apply",
        "https://%6cinkedin.com/jobs/view/1",
        "https://jobs.example.test/job opening",
        "https://jobs.example.test\\attacker.test/apply",
        "https://jobs.example.test/a/../apply",
        "https://jobs.example.test/%2e%2e/apply",
        "https://jobs.example.test/apply%zz",
        "https://jobs.example.test/apply#review",
        "https://jobs.example.test/apply#different",
    ],
)
def test_form_fill_url_contract_rejects_whatwg_ambiguous_destinations(
    approval_client,
    url,
) -> None:
    response = _post_form_approval(
        approval_client,
        {"application_id": APPLICATION_ID, "url": url},
    )

    assert response.status_code == 422
    paths = CompanionPaths.discover().scoped_to("account-a")
    with account_session(paths) as session:
        assert session.scalar(select(func.count(ApprovalRecord.id))) == 0
        assert session.scalar(select(func.count(AuditEventRecord.id))) == 0


def test_form_fill_fragment_variants_never_enter_the_digest_contract() -> None:
    canonical_payloads = []
    for fragment in ("review", "different"):
        with pytest.raises(FormFillRequestError, match="strict schema"):
            canonical_payloads.append(
                canonical_form_fill_payload(
                    {
                        "application_id": APPLICATION_ID,
                        "url": f"https://jobs.example.test/apply#{fragment}",
                    }
                )
            )

    assert canonical_payloads == []


@pytest.mark.parametrize("fragment", ["review", "different"])
def test_browser_fill_boundary_rejects_fragments_with_422(
    approval_client,
    fragment,
) -> None:
    response = approval_client.post(
        "/api/v1/browser/fill",
        json={
            "application_id": APPLICATION_ID,
            "url": f"https://jobs.example.test/apply#{fragment}",
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {
            "application_id": APPLICATION_ID,
            "url": "https://www.linkedin.com/jobs/view/1",
        },
        {
            "application_id": APPLICATION_ID,
            "url": "https://candidate:secret@jobs.example.test/apply",
        },
        {
            "application_id": APPLICATION_ID,
            "url": "https://jobs.example.test/apply",
            "fields": {"button[type=submit]": "Apply"},
        },
    ],
)
def test_form_fill_approval_rejects_unsafe_destinations_and_controls_without_writes(
    approval_client,
    payload,
) -> None:
    response = _post_form_approval(approval_client, payload)

    assert response.status_code == 403
    paths = CompanionPaths.discover().scoped_to("account-a")
    with account_session(paths) as session:
        assert session.scalar(select(func.count(ApprovalRecord.id))) == 0
        assert session.scalar(select(func.count(AuditEventRecord.id))) == 0


def test_form_fill_approval_maps_missing_and_unapproved_resources_without_writes(
    approval_client,
) -> None:
    missing_application = _post_form_approval(
        approval_client,
        {
            "application_id": "missing-application",
            "url": "https://jobs.example.test/apply",
        },
    )
    missing_artifact = _post_form_approval(
        approval_client,
        {
            "application_id": APPLICATION_ID,
            "url": "https://jobs.example.test/apply",
            "files": {"#resume": "missing-artifact"},
        },
    )
    paths = CompanionPaths.discover().scoped_to("account-a")
    with account_session(paths) as session:
        artifact = session.get(ArtifactRecord, ARTIFACT_ID)
        assert artifact is not None
        artifact.approved = False
    unapproved = _post_form_approval(approval_client, _form_payload())

    assert missing_application.status_code == 404
    assert missing_artifact.status_code == 404
    assert unapproved.status_code == 403
    with account_session(paths) as session:
        assert session.scalar(select(func.count(ApprovalRecord.id))) == 0
        assert session.scalar(select(func.count(AuditEventRecord.id))) == 0


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ({"url": "https://jobs.example.test/apply"}, 422),
        (
            {
                "application_id": APPLICATION_ID,
                "url": "https://jobs.example.test/apply",
                "extra": True,
            },
            422,
        ),
        (
            {
                "application_id": APPLICATION_ID,
                "url": "https://jobs.example.test/apply",
                "headless": 1,
            },
            422,
        ),
        (
            {
                "application_id": APPLICATION_ID,
                "url": "https://linkedin.com/jobs/view/1",
            },
            403,
        ),
        (
            {
                "application_id": APPLICATION_ID,
                "url": "https://candidate:secret@jobs.example.test/apply",
            },
            403,
        ),
        (
            {
                "application_id": APPLICATION_ID,
                "url": "https://jobs.example.test/apply",
                "fields": {"#submit": "Apply"},
            },
            403,
        ),
        (
            {
                "application_id": "missing-application",
                "url": "https://jobs.example.test/apply",
            },
            404,
        ),
        (
            {
                "application_id": APPLICATION_ID,
                "url": "https://jobs.example.test/apply",
                "files": {"#resume": "missing-artifact"},
            },
            404,
        ),
    ],
)
def test_browser_fill_endpoint_uses_the_same_strict_contract_and_error_mapping(
    approval_client,
    payload,
    expected_status,
) -> None:
    response = approval_client.post("/api/v1/browser/fill", json=payload)

    assert response.status_code == expected_status
    paths = CompanionPaths.discover().scoped_to("account-a")
    with account_session(paths) as session:
        assert session.scalar(select(func.count(ApprovalRecord.id))) == 0
        assert session.scalar(select(func.count(AuditEventRecord.id))) == 0


def test_browser_fill_endpoint_rejects_an_unapproved_attachment_without_writes(
    approval_client,
) -> None:
    paths = CompanionPaths.discover().scoped_to("account-a")
    with account_session(paths) as session:
        artifact = session.get(ArtifactRecord, ARTIFACT_ID)
        assert artifact is not None
        artifact.approved = False

    response = approval_client.post("/api/v1/browser/fill", json=_form_payload())

    assert response.status_code == 403
    with account_session(paths) as session:
        assert session.scalar(select(func.count(ApprovalRecord.id))) == 0
        assert session.scalar(select(func.count(AuditEventRecord.id))) == 0


@pytest.mark.parametrize(
    ("marker_kind", "usable"),
    [
        ("absent", False),
        ("wrong", False),
        ("boolean", False),
        ("current", True),
    ],
)
def test_history_and_consumption_require_the_current_validation_marker(
    tmp_path,
    marker_kind,
    usable,
) -> None:
    paths = CompanionPaths.at_root(tmp_path / "companion").scoped_to("account-a")
    payload = canonical_form_fill_payload(_form_payload())
    with account_session(paths) as session:
        _seed_form_resources(session, paths)
        snapshot = {"action_type": "application.form_fill"}
        if marker_kind == "wrong":
            snapshot[CONSUMER_VALIDATION_MARKER_KEY] = {
                "consumer": "application.form_fill",
                "version": 999,
            }
        elif marker_kind == "boolean":
            snapshot[CONSUMER_VALIDATION_MARKER_KEY] = {
                "consumer": "application.form_fill",
                "version": True,
            }
        elif marker_kind == "current":
            snapshot[CONSUMER_VALIDATION_MARKER_KEY] = (
                current_consumer_validation_marker("application.form_fill")
            )
        row = ApprovalRecord(
            action_type="application.form_fill",
            payload_digest=payload_digest(payload),
            preview={AUTHORIZATION_SNAPSHOT_KEY: snapshot},
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            decision="approved",
        )
        session.add(row)
        session.flush()
        row_id = row.id

        item = list_approval_history(session)["items"][0]
        assert item["usable"] is usable
        assert item["authorization"]["binding"]["remaining_uses"] == int(usable)
        if usable:
            assert consume_approval(session, "application.form_fill", payload).id == row_id
        else:
            with pytest.raises(PermissionError, match="matching approval"):
                consume_approval(session, "application.form_fill", payload)


def test_marker_version_rotation_invalidates_old_records_and_marks_new_ones(
    tmp_path,
    monkeypatch,
) -> None:
    paths = CompanionPaths.at_root(tmp_path / "companion").scoped_to("account-a")
    payload = _form_payload()
    with account_session(paths) as session:
        _seed_form_resources(session, paths)
        old = request_approval(
            session,
            "application.form_fill",
            payload,
            {},
            paths=paths,
        )
        decide_approval(session, old.id, "approved")
        old_id = old.id

    monkeypatch.setattr(
        approval_service,
        "CONSUMER_VALIDATION_VERSIONS",
        {"application.form_fill": CONSUMER_VALIDATION_VERSIONS["application.form_fill"] + 1},
    )
    with account_session(paths) as session:
        old_item = list_approval_history(session)["items"][0]
        assert old_item["id"] == old_id
        assert old_item["usable"] is False
        with pytest.raises(PermissionError, match="matching approval"):
            consume_approval(session, "application.form_fill", payload)

        current = request_approval(
            session,
            "application.form_fill",
            payload,
            {},
            paths=paths,
        )
        decide_approval(session, current.id, "approved")
        assert current.preview[AUTHORIZATION_SNAPSHOT_KEY][
            CONSUMER_VALIDATION_MARKER_KEY
        ]["version"] == 2
        assert consume_approval(session, "application.form_fill", payload).id == current.id


def test_validated_consumer_marker_snapshot_is_extensible_by_action(monkeypatch) -> None:
    monkeypatch.setattr(
        approval_service,
        "CONSUMER_VALIDATION_VERSIONS",
        {"future.write": 3},
    )

    snapshot = approval_service._authorization_snapshot(
        "future.write",
        {},
        consumer_validated=True,
    )

    assert snapshot == {
        "action_type": "future.write",
        CONSUMER_VALIDATION_MARKER_KEY: {
            "consumer": "future.write",
            "version": 3,
        },
    }


def test_consumption_skips_invalid_newest_marker_for_valid_older_record(tmp_path) -> None:
    paths = CompanionPaths.at_root(tmp_path / "companion").scoped_to("account-a")
    payload = _form_payload()
    with account_session(paths) as session:
        _seed_form_resources(session, paths)
        valid = request_approval(
            session,
            "application.form_fill",
            payload,
            {},
            paths=paths,
        )
        decide_approval(session, valid.id, "approved")
        valid.created_at = datetime.now(UTC) - timedelta(minutes=1)
        invalid = ApprovalRecord(
            action_type="application.form_fill",
            payload_digest=valid.payload_digest,
            preview={
                AUTHORIZATION_SNAPSHOT_KEY: {
                    "action_type": "application.form_fill",
                    CONSUMER_VALIDATION_MARKER_KEY: {
                        "consumer": "application.form_fill",
                        "version": 999,
                    },
                }
            },
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            decision="approved",
        )
        session.add(invalid)
        session.flush()
        invalid_id = invalid.id
        valid_id = valid.id

        assert consume_approval(session, "application.form_fill", payload).id == valid_id
        assert session.get(ApprovalRecord, invalid_id).decision == "approved"
        assert session.get(ApprovalRecord, valid_id).decision == "consumed"


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
        _seed_form_resources(session, paths)
        approval = request_approval(
            session,
            "application.form_fill",
            _form_payload(),
            {"summary": "Choose once"},
            paths=paths,
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
        _seed_form_resources(session, paths)
        approval = request_approval(
            session,
            "application.form_fill",
            payload,
            {"summary": "Use once"},
            paths=paths,
        )
        decide_approval(session, approval.id, "approved")
        approval_id = approval.id
        assert approval.preview[AUTHORIZATION_SNAPSHOT_KEY][
            CONSUMER_VALIDATION_MARKER_KEY
        ] == current_consumer_validation_marker("application.form_fill")
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
        _seed_form_resources(session, paths)
        approval = request_approval(
            session,
            "application.form_fill",
            payload,
            {"summary": "Atomic request"},
            paths=paths,
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
        "mcp.probe",
    }
    assert [name for name, item in APPROVAL_ACTIONS.items() if item.consumer_enabled] == [
        "application.form_fill",
        "mcp.probe",
    ]
