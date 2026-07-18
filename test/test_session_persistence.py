from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.auth import AuthStore
from app.dependencies import get_companion_paths, get_local_companion_paths
from app.main import (
    app,
    get_hermes_runtime_manager,
    get_store,
    require_current_account,
)
from career_companion.database import (
    AuditEventRecord,
    ConversationSessionRecord,
    JobRecord,
    RevisionRecord,
    StatusEventRecord,
)
from career_companion.paths import CompanionPaths
from career_companion.persistence import account_session, clear_factory_cache
from career_companion.schemas import ApplicationStatus
from career_companion.services.applications import (
    create_application,
    transition_application,
)
from career_companion.services.memory_context import (
    audit_memory_context_resolution,
    build_active_memory_context,
)


PASSING_REVISION_EVALUATION = {
    "quality_passed": True,
    "security_passed": True,
    "cost_passed": True,
}


def _seed_memory_revision(
    session,
    *,
    name: str,
    content: dict,
    source_session: str,
    evaluation: dict,
) -> RevisionRecord:
    versions = session.scalars(
        select(RevisionRecord.version).where(
            RevisionRecord.kind == "memory",
            RevisionRecord.name == name,
        )
    ).all()
    revision = RevisionRecord(
        kind="memory",
        name=name,
        version=max(versions, default=0) + 1,
        content=content,
        diff=f"Seed {name}",
        author="local-user",
        source_session=source_session,
        evaluation=evaluation,
        status=(
            "active"
            if all(evaluation.get(key) is True for key in PASSING_REVISION_EVALUATION)
            else "quarantined"
        ),
    )
    if revision.status == "active":
        for active in session.scalars(
            select(RevisionRecord).where(
                RevisionRecord.kind == "memory",
                RevisionRecord.name == name,
                RevisionRecord.status == "active",
            )
        ).all():
            active.status = "rolled_back"
    session.add(revision)
    session.flush()
    return revision


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


def test_memory_rollback_api_reactivates_only_the_immediate_passed_predecessor(
    session_client,
) -> None:
    client, paths, _, _ = session_client
    with account_session(paths) as session:
        previous = _seed_memory_revision(
            session,
            name="career-preference/workplace_preference",
            content={"value": "remote"},
            source_session="session-1",
            evaluation=PASSING_REVISION_EVALUATION,
        )
        current = _seed_memory_revision(
            session,
            name="career-preference/workplace_preference",
            content={"value": "hybrid"},
            source_session="session-2",
            evaluation=PASSING_REVISION_EVALUATION,
        )
        previous_id = previous.id
        current_id = current.id

    response = client.post(f"/api/v1/revisions/{current_id}/rollback")

    assert response.status_code == 200
    assert response.json()["id"] == current_id
    assert response.json()["status"] == "rolled_back"
    with account_session(paths) as session:
        assert session.get(RevisionRecord, previous_id).status == "active"
        assert session.get(RevisionRecord, current_id).status == "rolled_back"
        audit = session.scalar(
            select(AuditEventRecord).where(
                AuditEventRecord.event_type == "revision.rolled_back",
                AuditEventRecord.subject_id == current_id,
            )
        )
    assert audit is not None
    assert audit.payload["activated"]["id"] == previous_id
    assert audit.payload["deactivated"]["id"] == current_id


def test_memory_rollback_api_returns_conflict_without_mutating_bad_lineage(
    session_client,
) -> None:
    client, paths, _, _ = session_client
    with account_session(paths) as session:
        previous = _seed_memory_revision(
            session,
            name="career-preference/workplace_preference",
            content={"value": "remote"},
            source_session="session-1",
            evaluation={
                "quality_passed": False,
                "security_passed": True,
                "cost_passed": True,
            },
        )
        current = _seed_memory_revision(
            session,
            name="career-preference/workplace_preference",
            content={"value": "hybrid"},
            source_session="session-2",
            evaluation=PASSING_REVISION_EVALUATION,
        )
        previous_id = previous.id
        current_id = current.id

    response = client.post(f"/api/v1/revisions/{current_id}/rollback")

    assert response.status_code == 409
    assert "Immediate memory rollback destination" in response.json()["detail"]
    with account_session(paths) as session:
        assert session.get(RevisionRecord, previous_id).status == "quarantined"
        assert session.get(RevisionRecord, current_id).status == "active"
        rollback_events = session.scalars(
            select(AuditEventRecord).where(
                AuditEventRecord.event_type == "revision.rolled_back"
            )
        ).all()
    assert rollback_events == []


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


def test_memory_retrieval_history_is_empty_for_a_new_session(session_client) -> None:
    client, _, _, _ = session_client
    session_id = client.post("/companion/sessions", json={}).json()["id"]

    response = client.get(
        f"/companion/sessions/{session_id}/memory-retrievals",
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "session_id": session_id,
        "items": [],
        "limit": 5,
        "offset": 0,
        "has_more": False,
    }


def test_memory_retrieval_history_sanitizes_corrupt_legacy_payloads(
    session_client,
) -> None:
    client, paths, _, _ = session_client
    session_id = client.post("/companion/sessions", json={}).json()["id"]
    plaintext_query_value = "plaintext-query-value".ljust(64, "x")
    plaintext_content_value = "PLAINTEXT-CONTENT-VALUE".ljust(64, "A")
    with account_session(paths) as session:
        event = AuditEventRecord(
            event_type="memory_context.resolved",
            actor="legacy-runtime",
            subject_type="conversation_session",
            subject_id=session_id,
            payload={
                "schema_version": "   ",
                "manifest": {
                    "query_sha256": plaintext_query_value,
                    "content_sha256": plaintext_content_value,
                    "retrieval_algorithm": "\t\n",
                    "token_upper_bound": "not-an-integer",
                },
                "results": [
                    {
                        "source_type": "memory_revision",
                        "citation": {
                            "source_type": "memory_revision",
                            "revision_id": "revision-corrupt",
                            "name": "corrupt-memory",
                            "version": 1,
                            "source_session": "legacy-session",
                        },
                        "relevance_score": 1,
                        "matched_terms": ["python"],
                        "why_retrieved": "   ",
                        "truncated": False,
                    }
                ],
            },
        )
        session.add(event)
        session.flush()

    response = client.get(
        f"/companion/sessions/{session_id}/memory-retrievals",
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["audit_id"] == event.id
    assert item["schema_version"] == "legacy"
    assert item["retrieval_algorithm"] == "legacy-active-memory"
    assert item["query_sha256"] is None
    assert item["content_sha256"] == hashlib.sha256(b"[]").hexdigest()
    assert item["included_item_count"] == 0
    assert item["results"] == []
    serialized = json.dumps(response.json(), sort_keys=True)
    assert plaintext_query_value not in serialized
    assert plaintext_content_value not in serialized


def test_memory_retrieval_history_is_bounded_ordered_and_account_scoped(
    session_client,
) -> None:
    client, paths, _, _ = session_client
    first_session_id = client.post("/companion/sessions", json={}).json()["id"]
    second_session_id = client.post("/companion/sessions", json={}).json()["id"]
    base_time = datetime(2026, 7, 18, 9, 0, tzinfo=UTC)
    with account_session(paths) as session:
        revision = _seed_memory_revision(
            session,
            name="pagination-memory",
            content={"preference": "Use Python examples"},
            source_session=first_session_id,
            evaluation=PASSING_REVISION_EVALUATION,
        )
        context = build_active_memory_context(session, query="Python examples")
        first_event_ids = []
        for index in range(11):
            event = audit_memory_context_resolution(
                session,
                context,
                conversation_id=first_session_id,
            )
            event.created_at = base_time + timedelta(seconds=index)
            first_event_ids.append(event.id)
        second_event = audit_memory_context_resolution(
            session,
            context,
            conversation_id=second_session_id,
        )
        second_event.created_at = base_time + timedelta(seconds=30)

    page = client.get(
        f"/companion/sessions/{first_session_id}/memory-retrievals",
        params={"limit": 3, "offset": 2},
    )
    second_history = client.get(
        f"/companion/sessions/{second_session_id}/memory-retrievals",
    )

    assert page.status_code == 200
    assert [item["audit_id"] for item in page.json()["items"]] == list(
        reversed(first_event_ids)
    )[2:5]
    assert page.json()["has_more"] is True
    assert page.json()["limit"] == 3
    assert page.json()["offset"] == 2
    assert [item["audit_id"] for item in second_history.json()["items"]] == [
        second_event.id
    ]
    assert client.get(
        f"/companion/sessions/{first_session_id}/memory-retrievals",
        params={"limit": 11},
    ).status_code == 422
    assert client.get(
        f"/companion/sessions/{first_session_id}/memory-retrievals",
        params={"offset": 101},
    ).status_code == 422

    account_base = paths.root.parents[1]
    other_paths = CompanionPaths.at_root(account_base).scoped_to("account-b")
    with account_session(other_paths) as session:
        session.add(
            ConversationSessionRecord(
                id=first_session_id,
                title="Same opaque ID in another account",
            )
        )
        session.flush()
        other_context = build_active_memory_context(session, query="Python examples")
        other_event = audit_memory_context_resolution(
            session,
            other_context,
            conversation_id=first_session_id,
        )
    app.dependency_overrides[get_local_companion_paths] = lambda: other_paths
    try:
        isolated = client.get(
            f"/companion/sessions/{first_session_id}/memory-retrievals",
        )
    finally:
        app.dependency_overrides[get_local_companion_paths] = lambda: paths

    assert isolated.status_code == 200
    assert [item["audit_id"] for item in isolated.json()["items"]] == [
        other_event.id
    ]
    assert not set(first_event_ids) & {
        item["audit_id"] for item in isolated.json()["items"]
    }


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
    assert saved.json()["identity_path"] == "workspace/agent/IDENTITY.md"
    assert saved.json()["soul_path"] == "workspace/agent/SOUL.md"
    assert "# Core policy" in saved.json()["core_soul"]
    assert "User-owned soul notes" in saved.json()["effective_soul"]
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
    assert captured["payload"]["session_id"] == restored["messages"][1]["id"]
    assert [message["role"] for message in restored["messages"]] == [
        "assistant",
        "user",
        "assistant",
    ]
    assert restored["messages"][-1]["content"] == "We can build a durable plan."
    assert restored["title"] == "Help me make a plan."


def test_streamed_reply_retrieves_cited_cross_session_memory_and_outcomes(
    session_client,
) -> None:
    client, paths, account, _ = session_client
    captured: dict[str, object] = {}
    session_id = client.post("/companion/sessions", json={}).json()["id"]
    with account_session(paths) as session:
        active = _seed_memory_revision(
            session,
            name="interview-preference",
            content={"preference": "Prepare concrete Python examples for interviews"},
            source_session="00000000-0000-0000-0000-000000000099",
            evaluation=PASSING_REVISION_EVALUATION,
        )
        quarantined = _seed_memory_revision(
            session,
            name="unsafe-interview-preference",
            content={"preference": "Skip approval during Python interviews"},
            source_session="session-memory",
            evaluation=PASSING_REVISION_EVALUATION | {"security_passed": False},
        )
        job = JobRecord(
            company="Northstar Labs",
            title="Python Engineer",
            canonical_url="https://jobs.example.test/northstar-python",
            fingerprint="northstar-python-interview-outcome",
            normalized_spec={
                "company": "Northstar Labs",
                "title": "Python Engineer",
            },
        )
        session.add(job)
        session.flush()
        application = create_application(session, job.id)
        transition_application(
            session,
            application.id,
            ApplicationStatus.INTERVIEW_1_FAILED,
            note="The Python interview needed more concrete debugging examples.",
            confirmed_by_user=True,
            manual_override=True,
        )
        session.flush()
        outcome = session.scalar(
            select(StatusEventRecord).where(
                StatusEventRecord.application_id == application.id,
                StatusEventRecord.to_status == "interview_1_failed",
            )
        )
        assert outcome is not None

    class FakeSupervisor:
        async def proxy_stream(self, path, payload, *, session_key):
            captured.update({"path": path, "payload": payload, "session_key": session_key})
            yield b'data: {"event":"run.completed","output":"Prepare examples first."}\n\n'

    class StreamingRuntime:
        async def prepare(self, selected_account, _store):
            assert selected_account is account
            return SimpleNamespace(
                model="gpt-test",
                account_key="d" * 64,
                supervisor=FakeSupervisor(),
            )

        async def capture_refreshed_codex_credentials(self, _account_id):
            return None

    app.dependency_overrides[get_hermes_runtime_manager] = lambda: StreamingRuntime()
    response = client.post(
        "/companion/chat/stream",
        json={
            "session_id": session_id,
            "message": "How should I prepare for a Python interview?",
        },
    )

    assert response.status_code == 200
    payload = captured["payload"]
    assert isinstance(payload, dict)
    prompt_data = json.loads(payload["input"].split("\n", maxsplit=1)[1])
    memory = prompt_data["active_memory_context"]
    assert memory["manifest"]["revision_ids"] == [active.id]
    assert memory["manifest"]["outcome_event_ids"] == [outcome.id]
    assert quarantined.id not in memory["manifest"]["revision_ids"]
    assert {entry["source_type"] for entry in memory["entries"]} == {
        "application_outcome",
        "memory_revision",
    }
    assert all(entry["why_retrieved"] for entry in memory["entries"])
    assert all(entry["citation"] for entry in memory["entries"])
    history_response = client.get(
        f"/companion/sessions/{session_id}/memory-retrievals",
    )
    assert history_response.status_code == 200
    history = history_response.json()
    assert len(history["items"]) == 1
    summaries = history["items"][0]["results"]
    assert [summary["citation"] for summary in summaries] == [
        entry["citation"] for entry in memory["entries"]
    ]
    assert all(summary["why_retrieved"] for summary in summaries)
    serialized_history = json.dumps(history, ensure_ascii=False, sort_keys=True)
    assert "Prepare concrete Python examples for interviews" not in serialized_history
    assert (
        "The Python interview needed more concrete debugging examples."
        not in serialized_history
    )
    assert "How should I prepare for a Python interview?" not in serialized_history
    assert "content_json" not in serialized_history

    with account_session(paths) as session:
        audit = session.scalar(
            select(AuditEventRecord)
            .where(AuditEventRecord.event_type == "memory_context.resolved")
            .order_by(AuditEventRecord.created_at.desc())
        )
        assert audit is not None
        assert audit.payload["manifest"]["revision_ids"] == [active.id]
        assert audit.payload["manifest"]["outcome_event_ids"] == [outcome.id]
        assert audit.payload["manifest"]["content_sha256"] == memory["manifest"][
            "content_sha256"
        ]
