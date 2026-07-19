from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import sessionmaker

from career_companion.database import (
    ApplicationJobClaimRecord,
    ApplicationRecord,
    ArtifactRecord,
    AuditEventRecord,
    Base,
    CandidateProfileRecord,
    ConversationSessionRecord,
    JobRecord,
    RevisionRecord,
    SourceDocumentRecord,
    StatusEventRecord,
    UsageRunRecord,
    build_engine,
    ensure_application_job_claims,
)
from career_companion.paths import CompanionPaths
from career_companion.persistence import clear_factory_cache, session_factory_for
from career_companion.hermes_bridge import get_bound_run_message
from career_companion.schemas import (
    ApplicationStatus,
    CandidateProfile,
    ClaimStatus,
    EvidenceReference,
    JobSpec,
    ModelRoute,
    ProfileClaim,
    ProfileProject,
    ProjectAnalysis,
)
from career_companion.services.applications import (
    ApplicationPersistenceError,
    create_application,
    decide_scored_application,
    ensure_application,
    transition_application,
)
from career_companion.services import form_preview as form_preview_service
from career_companion.services.approvals import (
    consume_approval,
    decide_approval,
    payload_digest,
    request_approval,
)
from career_companion.services.browser import validate_form_payload
from career_companion.services.model_routes import record_usage, upsert_route
from career_companion.services.conversation_sessions import (
    append_message,
    create_conversation_session,
    reserve_conversation_write,
)
from career_companion.services.profile import _candidate_from_text, save_profile
from career_companion.services.revisions import (
    MemoryRollbackConflictError,
    create_revision,
    evaluate_revision,
    rollback_revision,
)
from career_companion.services.tailoring import _interview_markdown


@pytest.fixture
def paths(tmp_path) -> CompanionPaths:
    clear_factory_cache()
    scoped = CompanionPaths.at_root(tmp_path / "companion").scoped_to("account-a")
    yield scoped
    clear_factory_cache()


@pytest.fixture
def session(paths):
    with session_factory_for(paths)() as database_session:
        yield database_session
        database_session.rollback()


def _job(session) -> JobRecord:
    row = JobRecord(
        company="Example GmbH",
        title="AI Engineer",
        canonical_url="https://example.test/jobs/ai-engineer",
        fingerprint="a" * 64,
        normalized_spec={
            "title": "AI Engineer",
            "company": "Example GmbH",
            "locations": ["Berlin, Germany"],
            "description": "Python and retrieval experience required.",
        },
    )
    session.add(row)
    session.flush()
    return row


def _passing_replay(*, baseline: float, candidate: float) -> dict:
    return {
        "cases_total": 25,
        "ranking_pass_rate": candidate,
        "baseline_ranking_pass_rate": baseline,
        "unsupported_claims": 0,
        "keyword_coverage": 0.9,
        "one_page_success_rate": 0.96,
        "security_failures": 0,
        "baseline_estimated_cost_usd": 1.0,
        "candidate_estimated_cost_usd": 1.0,
    }


def test_account_scopes_use_opaque_separate_databases(tmp_path) -> None:
    clear_factory_cache()
    base = CompanionPaths.at_root(tmp_path / "companion")
    account_a = base.scoped_to("../account-a")
    account_b = base.scoped_to("account-b")

    assert account_a.root.parent == base.root / "accounts"
    assert account_b.root.parent == base.root / "accounts"
    assert account_a.database != account_b.database
    assert "account-a" not in str(account_a.root)
    assert ".." not in account_a.root.name

    with session_factory_for(account_a)() as session_a:
        session_a.add(CandidateProfileRecord(display_name="A", payload={}))
        session_a.commit()
    with session_factory_for(account_b)() as session_b:
        assert session_b.scalars(select(CandidateProfileRecord)).all() == []
    clear_factory_cache()


def test_application_submission_requires_confirmation_and_valid_state(session) -> None:
    application = create_application(session, _job(session).id)

    with pytest.raises(ValueError, match="discovered -> ready"):
        transition_application(session, application.id, ApplicationStatus.READY)

    for target in (
        ApplicationStatus.SCORED,
        ApplicationStatus.APPROVED,
        ApplicationStatus.TAILORING,
        ApplicationStatus.READY,
    ):
        transition_application(session, application.id, target)

    with pytest.raises(PermissionError, match="explicit user confirmation"):
        transition_application(session, application.id, ApplicationStatus.SUBMITTED)

    submitted = transition_application(
        session,
        application.id,
        ApplicationStatus.SUBMITTED,
        confirmed_by_user=True,
    )
    assert submitted.status == "submitted"
    assert submitted.submitted_at is not None
    assert submitted.next_action == "Schedule a follow-up"
    assert [(event.from_status, event.to_status) for event in submitted.events][-1] == (
        "ready",
        "submitted",
    )


@pytest.mark.parametrize(
    "target",
    [ApplicationStatus.FORM_PREVIEWED, ApplicationStatus.FORM_FILLED],
)
def test_generic_transition_cannot_enter_dedicated_form_states(
    session,
    target,
) -> None:
    application = create_application(session, _job(session).id)

    with pytest.raises(ValueError, match="requires its dedicated workflow"):
        transition_application(
            session,
            application.id,
            target,
            manual_override=True,
        )


def test_form_preview_storage_contract_fails_closed_without_posix_support(
    tmp_path,
    monkeypatch,
) -> None:
    paths = CompanionPaths.at_root(tmp_path / "companion")
    paths.create()
    monkeypatch.setattr(
        form_preview_service,
        "_secure_dir_fd_available",
        lambda: False,
    )

    with pytest.raises(RuntimeError, match="require POSIX directory-descriptor"):
        form_preview_service._assert_secure_preview_storage(paths)


def test_ensure_application_is_idempotent_for_one_saved_job(session) -> None:
    job = _job(session)

    first, first_created = ensure_application(session, job.id)
    repeated, repeated_created = ensure_application(session, job.id)

    assert first_created is True
    assert repeated_created is False
    assert repeated.id == first.id
    assert session.scalars(
        select(ApplicationRecord).where(ApplicationRecord.job_id == job.id)
    ).all() == [first]


def test_ensure_application_rejects_missing_job_without_partial_state(session) -> None:
    with pytest.raises(LookupError, match="Job not found"):
        ensure_application(session, "00000000-0000-0000-0000-000000000099")
    assert session.scalars(select(ApplicationRecord)).all() == []
    assert session.scalars(select(ApplicationJobClaimRecord)).all() == []


def test_ensure_application_retries_bounded_uuid_collision(
    session,
    monkeypatch,
) -> None:
    occupied_job = _job(session)
    target_job = JobRecord(
        company="Target GmbH",
        title="Engineer",
        canonical_url="https://example.test/jobs/target-engineer",
        fingerprint="b" * 64,
        normalized_spec={
            "title": "Engineer",
            "company": "Target GmbH",
            "description": "Build reliable systems.",
        },
    )
    session.add(target_job)
    session.flush()
    occupied_id = "00000000-0000-0000-0000-000000000071"
    successful_id = "00000000-0000-0000-0000-000000000072"
    session.add(ApplicationRecord(id=occupied_id, job_id=occupied_job.id))
    session.flush()
    generated = iter([occupied_id, successful_id])
    monkeypatch.setattr(
        "career_companion.services.applications._new_application_id",
        lambda: next(generated),
    )

    application, created = ensure_application(session, target_job.id)

    assert created is True
    assert application.id == successful_id
    assert session.get(ApplicationRecord, occupied_id).job_id == occupied_job.id


@pytest.mark.parametrize(
    "table", ["applications", "application_job_claims", "audit_events"]
)
def test_ensure_application_insert_fault_leaves_no_orphan_or_audit(
    session,
    table,
) -> None:
    job = _job(session)
    session.commit()
    trigger = f"interrupt_{table}"
    with session.bind.begin() as connection:
        connection.exec_driver_sql(
            f"""
            CREATE TRIGGER {trigger}
            BEFORE INSERT ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'reservation interrupted');
            END
            """
        )
    try:
        with pytest.raises(ApplicationPersistenceError, match="could not be completed"):
            ensure_application(session, job.id)
        assert session.scalars(
            select(ApplicationRecord).where(ApplicationRecord.job_id == job.id)
        ).all() == []
        assert session.get(ApplicationJobClaimRecord, job.id) is None
        assert session.scalars(
            select(AuditEventRecord).where(
                AuditEventRecord.event_type == "application.created"
            )
        ).all() == []
    finally:
        session.rollback()
        with session.bind.begin() as connection:
            connection.exec_driver_sql(f"DROP TRIGGER {trigger}")

    application, created = ensure_application(session, job.id)
    assert created is True
    assert application.job_id == job.id
    assert session.get(ApplicationJobClaimRecord, job.id).application_id == application.id


@pytest.mark.parametrize("attempt", range(3))
def test_parallel_application_tracking_uses_one_cross_engine_reservation(
    paths,
    session,
    attempt,
) -> None:
    job = _job(session)
    job.raw_payload = {"concurrency_attempt": attempt}
    session.commit()
    barrier = threading.Barrier(2)

    def track() -> tuple[str, bool]:
        engine = build_engine(paths)
        worker_factory = sessionmaker(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
        )
        try:
            with worker_factory() as worker_session:
                barrier.wait()
                application, created = ensure_application(worker_session, job.id)
                if application.status == ApplicationStatus.DISCOVERED.value:
                    transition_application(
                        worker_session,
                        application.id,
                        ApplicationStatus.SCORED,
                        note="Explicit selected-job tracking",
                    )
                worker_session.commit()
                return application.id, created
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result() for future in [executor.submit(track) for _ in range(2)]]

    application_ids = {application_id for application_id, _ in results}
    assert len(application_ids) == 1
    assert sorted(created for _, created in results) == [False, True]
    session.expire_all()
    applications = session.scalars(
        select(ApplicationRecord).where(ApplicationRecord.job_id == job.id)
    ).all()
    assert len(applications) == 1
    assert applications[0].id in application_ids
    assert applications[0].status == ApplicationStatus.SCORED.value
    assert len(
        session.scalars(
            select(StatusEventRecord).where(
                StatusEventRecord.application_id == applications[0].id
            )
        ).all()
    ) == 1
    application_audits = session.scalars(
        select(AuditEventRecord).where(
            AuditEventRecord.subject_type == "application",
            AuditEventRecord.subject_id == applications[0].id,
        )
    ).all()
    assert [event.event_type for event in application_audits].count(
        "application.created"
    ) == 1
    assert [event.event_type for event in application_audits].count(
        "application.status_changed"
    ) == 1


def test_parallel_conversation_appends_have_unique_authoritative_order(
    paths,
    session,
) -> None:
    conversation = create_conversation_session(session, paths)
    conversation_id = conversation.id
    session.commit()
    barrier = threading.Barrier(2)

    def append(content: str) -> tuple[str, int]:
        engine = build_engine(paths)
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            with factory() as worker:
                barrier.wait()
                reserve_conversation_write(worker)
                current = worker.get(ConversationSessionRecord, conversation_id)
                message = append_message(worker, current, role="user", content=content)
                worker.commit()
                return message.id, message.position
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result()
            for future in (
                executor.submit(append, "Track Example GmbH AI Engineer."),
                executor.submit(append, "Show the queue instead."),
            )
        ]

    assert {position for _, position in results} == {2, 3}
    latest_id = max(results, key=lambda result: result[1])[0]
    stale_id = min(results, key=lambda result: result[1])[0]
    factory = session_factory_for(paths)
    with factory() as verification:
        assert get_bound_run_message(verification, latest_id).id == latest_id
    with factory() as verification:
        with pytest.raises(HTTPException, match="no longer the latest"):
            get_bound_run_message(verification, stale_id)


def test_existing_database_backfills_claim_without_mutating_legacy_duplicates(
    tmp_path,
) -> None:
    clear_factory_cache()
    paths = CompanionPaths.at_root(tmp_path / "legacy").scoped_to("account-a")
    engine = build_engine(paths)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE application_job_claims")
    legacy_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with legacy_factory() as legacy_session:
        job = JobRecord(
            id="00000000-0000-0000-0000-000000000001",
            company="Legacy GmbH",
            title="Engineer",
            canonical_url="https://jobs.example.test/legacy",
            fingerprint="b" * 64,
            normalized_spec={},
        )
        canonical = ApplicationRecord(
            id="00000000-0000-0000-0000-000000000002",
            job_id=job.id,
            status="withdrawn",
            next_action="No action needed",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        duplicate = ApplicationRecord(
            id="00000000-0000-0000-0000-000000000003",
            job_id=job.id,
            status="scored",
            next_action="Approve or archive this opportunity",
            created_at=datetime(2025, 1, 2, tzinfo=UTC),
            updated_at=datetime(2025, 1, 2, tzinfo=UTC),
        )
        legacy_session.add_all([job, canonical, duplicate])
        legacy_session.flush()
        legacy_session.add_all(
            [
                ArtifactRecord(
                    application_id=canonical.id,
                    kind="resume",
                    version=1,
                    path="canonical.pdf",
                    sha256="d" * 64,
                ),
                ArtifactRecord(
                    application_id=duplicate.id,
                    kind="resume",
                    version=1,
                    path="legacy.pdf",
                    sha256="c" * 64,
                ),
                StatusEventRecord(
                    application_id=canonical.id,
                    from_status="discovered",
                    to_status="withdrawn",
                    note="canonical history",
                ),
                StatusEventRecord(
                    application_id=duplicate.id,
                    from_status="discovered",
                    to_status="scored",
                    note="duplicate history",
                ),
                AuditEventRecord(
                    event_type="application.created",
                    subject_type="application",
                    subject_id=canonical.id,
                    payload={"job_id": job.id, "source": "canonical"},
                ),
                AuditEventRecord(
                    event_type="application.created",
                    subject_type="application",
                    subject_id=duplicate.id,
                    payload={"job_id": job.id, "source": "duplicate"},
                ),
            ]
        )
        legacy_session.commit()
    engine.dispose()

    migrated_factory = session_factory_for(paths)
    with migrated_factory() as repaired_session:
        applications = repaired_session.scalars(select(ApplicationRecord)).all()
        assert {application.id for application in applications} == {
            canonical.id,
            duplicate.id,
        }
        assert {application.status for application in applications} == {
            "scored",
            "withdrawn",
        }
        assert {
            (artifact.application_id, artifact.version, artifact.path)
            for artifact in repaired_session.scalars(select(ArtifactRecord))
        } == {
            (canonical.id, 1, "canonical.pdf"),
            (duplicate.id, 1, "legacy.pdf"),
        }
        assert {
            (event.application_id, event.note)
            for event in repaired_session.scalars(select(StatusEventRecord))
        } == {
            (canonical.id, "canonical history"),
            (duplicate.id, "duplicate history"),
        }
        assert {
            event.subject_id
            for event in repaired_session.scalars(select(AuditEventRecord))
        } == {canonical.id, duplicate.id}
        claim = repaired_session.get(ApplicationJobClaimRecord, job.id)
        assert claim is not None
        assert claim.application_id == canonical.id
        returned, created = ensure_application(repaired_session, job.id)
        assert returned.id == canonical.id
        assert created is False
        assert create_application(repaired_session, job.id).id == canonical.id
        repaired_session.commit()

    clear_factory_cache()
    restarted_factory = session_factory_for(paths)
    with restarted_factory() as restarted_session:
        assert len(restarted_session.scalars(select(ApplicationRecord)).all()) == 2
        assert (
            restarted_session.get(ApplicationJobClaimRecord, job.id).application_id
            == canonical.id
        )
    clear_factory_cache()


def test_application_claim_backfill_rolls_back_atomically_and_restarts(tmp_path) -> None:
    paths = CompanionPaths.at_root(tmp_path / "interrupted").scoped_to("account-a")
    engine = build_engine(paths)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        for index in range(2):
            job = JobRecord(
                id=f"00000000-0000-0000-0000-00000000001{index}",
                company="Legacy GmbH",
                title=f"Engineer {index}",
                canonical_url=f"https://jobs.example.test/legacy-{index}",
                fingerprint=str(index) * 64,
                normalized_spec={},
            )
            session.add(job)
            session.flush()
            session.add(
                ApplicationRecord(
                    id=f"00000000-0000-0000-0000-00000000002{index}",
                    job_id=job.id,
                )
            )
        session.commit()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TRIGGER interrupt_claim_backfill
            BEFORE INSERT ON application_job_claims
            WHEN NEW.job_id = '00000000-0000-0000-0000-000000000011'
            BEGIN
                SELECT RAISE(ABORT, 'interrupted claim backfill');
            END
            """
        )

    with pytest.raises(DatabaseError, match="interrupted claim backfill"):
        ensure_application_job_claims(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM application_job_claims"
        ).scalar_one() == 0
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER interrupt_claim_backfill")
    ensure_application_job_claims(engine)
    ensure_application_job_claims(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM application_job_claims"
        ).scalar_one() == 2
    engine.dispose()
    clear_factory_cache()


@pytest.mark.parametrize(
    ("target", "decision_reference"),
    [
        (ApplicationStatus.APPROVED, "Approve Example GmbH AI Engineer"),
        (ApplicationStatus.WITHDRAWN, "Archive Example GmbH AI Engineer"),
    ],
)
def test_scored_application_decision_is_exact_and_idempotent(
    session,
    target: ApplicationStatus,
    decision_reference: str,
) -> None:
    application = create_application(session, _job(session).id)
    transition_application(session, application.id, ApplicationStatus.SCORED)
    session.commit()

    decided = decide_scored_application(
        session,
        application.id,
        target,
        source_session="00000000-0000-0000-0000-000000000001",
        run_message_id="00000000-0000-0000-0000-000000000002",
        user_request=f"{decision_reference}.",
        decision_reference=decision_reference,
    )
    repeated = decide_scored_application(
        session,
        application.id,
        target,
        source_session="00000000-0000-0000-0000-000000000001",
        run_message_id="00000000-0000-0000-0000-000000000002",
        user_request=f"{decision_reference}.",
        decision_reference=decision_reference,
    )

    assert decided.application.status == target.value
    assert repeated.application.id == decided.application.id
    assert decided.status_event_created is True
    assert repeated.status_event_created is False
    assert repeated.decision_fingerprint == decided.decision_fingerprint
    decision_events = session.scalars(
        select(StatusEventRecord).where(
            StatusEventRecord.application_id == application.id,
            StatusEventRecord.from_status == "scored",
        )
    ).all()
    assert len(decision_events) == 1
    assert decision_events[0].to_status == target.value
    assert decided.decision_fingerprint in decision_events[0].note
    assert decision_reference not in decision_events[0].note


def test_scored_application_decision_rejects_other_targets_and_states(session) -> None:
    application = create_application(session, _job(session).id)
    session.commit()

    with pytest.raises(ValueError, match="must approve or archive"):
        decide_scored_application(
            session,
            application.id,
            ApplicationStatus.TAILORING,
            source_session="00000000-0000-0000-0000-000000000001",
            run_message_id="00000000-0000-0000-0000-000000000002",
            user_request="Start tailoring Example GmbH AI Engineer.",
            decision_reference="Start tailoring Example GmbH AI Engineer",
        )
    with pytest.raises(ValueError, match="only valid for a scored"):
        decide_scored_application(
            session,
            application.id,
            ApplicationStatus.APPROVED,
            source_session="00000000-0000-0000-0000-000000000001",
            run_message_id="00000000-0000-0000-0000-000000000002",
            user_request="Approve Example GmbH AI Engineer.",
            decision_reference="Approve Example GmbH AI Engineer",
        )

    assert application.status == "discovered"
    assert session.scalars(
        select(StatusEventRecord).where(
            StatusEventRecord.application_id == application.id
        )
    ).all() == []


def test_parallel_identical_scored_decisions_create_one_event(paths, session) -> None:
    application = create_application(session, _job(session).id)
    transition_application(session, application.id, ApplicationStatus.SCORED)
    session.commit()
    application_id = application.id
    barrier = threading.Barrier(2)
    factory = session_factory_for(paths)

    def decide() -> bool:
        with factory() as worker_session:
            barrier.wait()
            return decide_scored_application(
                worker_session,
                application_id,
                ApplicationStatus.APPROVED,
                source_session="00000000-0000-0000-0000-000000000001",
                run_message_id="00000000-0000-0000-0000-000000000002",
                user_request="Approve Example GmbH AI Engineer.",
                decision_reference="Approve Example GmbH AI Engineer",
            ).status_event_created

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(decide) for _ in range(2)]
        created = sorted(future.result() for future in futures)

    assert created == [False, True]
    with factory() as verification_session:
        decision_events = verification_session.scalars(
            select(StatusEventRecord).where(
                StatusEventRecord.application_id == application_id,
                StatusEventRecord.from_status == "scored",
                StatusEventRecord.to_status == "approved",
            )
        ).all()
    assert len(decision_events) == 1


def test_approval_is_bound_to_payload_and_can_only_be_consumed_once(session) -> None:
    payload = {"url": "https://example.test/apply", "fields": {"#name": "Ada"}}
    approval = request_approval(
        session,
        "application.form_fill",
        payload,
        {"summary": "Fill Example GmbH form"},
    )
    assert approval.payload_digest == payload_digest(
        {"fields": {"#name": "Ada"}, "url": "https://example.test/apply"}
    )
    decide_approval(session, approval.id, "approved")

    with pytest.raises(PermissionError, match="matching approval"):
        consume_approval(session, "application.form_fill", payload | {"extra": True})

    consumed = consume_approval(session, "application.form_fill", payload)
    assert consumed.decision == "consumed"
    with pytest.raises(PermissionError, match="matching approval"):
        consume_approval(session, "application.form_fill", payload)


@pytest.mark.parametrize(
    "action_type",
    ["application.submit", "linkedin.apply", "employer.contact"],
)
def test_never_automate_actions_cannot_request_approval(session, action_type) -> None:
    with pytest.raises(ValueError, match="never automated"):
        request_approval(session, action_type, {}, {})


def test_expired_approval_cannot_be_decided_or_consumed(session) -> None:
    payload = {"message": "draft only"}
    approval = request_approval(session, "email.draft", payload, {})
    approval.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(ValueError, match="expired"):
        decide_approval(session, approval.id, "approved")
    assert approval.decision == "expired"


def test_verified_profile_claims_require_source_evidence(session) -> None:
    unsupported = CandidateProfile(
        claims=[
            ProfileClaim(
                key="skill",
                value="Kubernetes",
                status=ClaimStatus.VERIFIED,
            )
        ]
    )
    with pytest.raises(ValueError, match="must include evidence"):
        save_profile(session, unsupported)

    supported = CandidateProfile(
        display_name="Ada Candidate",
        claims=[
            ProfileClaim(
                key="skill",
                value="Python",
                status=ClaimStatus.VERIFIED,
                evidence=[
                    EvidenceReference(
                        source_id="cv-1",
                        source_name="candidate.pdf",
                        page=1,
                        excerpt="Built Python services",
                        content_hash="b" * 64,
                    )
                ],
            )
        ],
    )
    row = save_profile(session, supported)
    assert row.display_name == "Ada Candidate"
    assert row.payload["claims"][0]["status"] == "verified"


def test_profile_extraction_separates_contacts_languages_and_publications() -> None:
    document = SourceDocumentRecord(
        id="source-1",
        filename="candidate.pdf",
        stored_path="/tmp/candidate.pdf",
        sha256="c" * 64,
        media_type="application/pdf",
        extracted_text="",
    )
    text = """candidate@example.test | +49 30 12345678
Education
MSc Language Science and Technology 10.2022 – 05.2026
Languages
Chinese (native), English (C1), German (A1)
Publications
Chinese Dataset for Multi-Party, Multi-Charge
Chinese Legal LLM | Co-author, COLING 2025
"""

    profile = _candidate_from_text(document, [(1, text)])

    assert profile.email == "candidate@example.test"
    assert profile.phone == "+49 30 12345678"
    assert profile.languages == ["Chinese (native)", "English (C1)", "German (A1)"]
    assert [(claim.key, claim.value) for claim in profile.claims] == [
        ("degree", "MSc Language Science and Technology 10.2022 – 05.2026"),
        ("publication", "Chinese Dataset for Multi-Party, Multi-Charge"),
        ("publication", "Chinese Legal LLM | Co-author, COLING 2025"),
    ]
    assert all(claim.status == ClaimStatus.LEARNING for claim in profile.claims)


def test_language_keyword_alone_does_not_create_a_language() -> None:
    document = SourceDocumentRecord(
        id="source-2",
        filename="candidate.pdf",
        stored_path="/tmp/candidate.pdf",
        sha256="d" * 64,
        media_type="application/pdf",
        extracted_text="",
    )

    profile = _candidate_from_text(
        document,
        [(1, "Chinese Legal LLM | Co-author, COLING 2025")],
    )

    assert profile.languages == []
    assert [(claim.key, claim.value) for claim in profile.claims] == [
        ("publication", "Chinese Legal LLM | Co-author, COLING 2025")
    ]


def test_interview_plan_includes_analyzed_project_questions() -> None:
    project = ProfileProject(
        name="CareerPilot",
        analysis=ProjectAnalysis(
            source="local",
            repository_name="CareerPilot",
            analyzed_at=datetime.now(UTC),
            file_count=120,
            interview_questions=[
                "Why did you choose FastAPI, and what alternative did you reject?"
            ],
        ),
    )
    markdown = _interview_markdown(
        JobSpec(
            title="AI Engineer",
            company="Example GmbH",
            description="Build reliable AI systems.",
        ),
        {"strong": [], "adjacent": [], "missing": []},
        [project],
    )

    assert "## Project deep dives" in markdown
    assert "### CareerPilot" in markdown
    assert "Why did you choose FastAPI" in markdown


def test_scheduled_routes_forbid_fallbacks_and_usage_must_match_route(session) -> None:
    unsafe_cron = ModelRoute(
        name="cron",
        provider="openai-api",
        model="cheap-model",
        scheduled=True,
        fallback_policy="same-provider",
    )
    with pytest.raises(ValueError, match="Scheduled routes"):
        upsert_route(session, unsafe_cron)

    route = ModelRoute(
        name="evaluation",
        provider="openai-api",
        model="evaluation-model",
        cost_budget_usd=0.2,
    )
    upsert_route(session, route)

    with pytest.raises(PermissionError, match="cost budget"):
        record_usage(
            session,
            UsageRunRecord(
                route="evaluation",
                provider="openai-api",
                model="evaluation-model",
                estimated_cost_usd=0.21,
            ),
        )
    with pytest.raises(ValueError, match="must match"):
        record_usage(
            session,
            UsageRunRecord(
                route="evaluation",
                provider="openai-codex",
                model="other-model",
                estimated_cost_usd=0.1,
            ),
        )


def test_explicit_fallback_route_must_be_present_and_not_self(session) -> None:
    with pytest.raises(ValueError, match="requires a fallback route"):
        upsert_route(
            session,
            ModelRoute(
                name="research",
                provider="openai-api",
                model="research-model",
                fallback_policy="explicit",
            ),
        )
    with pytest.raises(ValueError, match="cannot fall back to itself"):
        upsert_route(
            session,
            ModelRoute(
                name="research",
                provider="openai-api",
                model="research-model",
                fallback_policy="explicit",
                fallback_route="research",
            ),
        )
    with pytest.raises(ValueError, match="does not exist"):
        upsert_route(
            session,
            ModelRoute(
                name="research",
                provider="openai-api",
                model="research-model",
                fallback_policy="explicit",
                fallback_route="missing-route",
            ),
        )


def test_immutable_revision_names_are_prefix_protected(session) -> None:
    with pytest.raises(PermissionError, match="immutable"):
        create_revision(
            session,
            kind="skill",
            name="verified-fact:work-authorization",
            content={"value": "changed"},
            diff="unsafe",
            author="agent",
            source_session="hostile-jd",
        )


def test_generic_revision_service_rejects_memory_before_mutation(session) -> None:
    with pytest.raises(PermissionError, match="skills and rubrics"):
        create_revision(
            session,
            kind="memory",
            name="arbitrary-memory",
            content={"value": "unsafe"},
            diff="bypass",
            author="agent",
            source_session="session-1",
        )
    assert session.scalars(select(RevisionRecord)).all() == []
    assert session.scalars(
        select(AuditEventRecord).where(
            AuditEventRecord.event_type.like("revision.%")
        )
    ).all() == []

    legacy = RevisionRecord(
        kind="memory",
        name="legacy-memory",
        version=1,
        content={"value": "legacy"},
        diff="legacy",
        author="local-user",
        source_session="legacy-session",
    )
    session.add(legacy)
    session.flush()
    with pytest.raises(PermissionError, match="skills and rubrics"):
        evaluate_revision(
            session,
            legacy.id,
            {
                "quality_passed": True,
                "security_passed": True,
                "cost_passed": True,
                "replay": _passing_replay(baseline=0.8, candidate=0.9),
            },
        )
    with pytest.raises(
        MemoryRollbackConflictError,
        match="active and canonically evaluated",
    ):
        rollback_revision(session, legacy.id)
    assert legacy.status == "draft"


def test_revision_evaluation_quarantines_failures_and_rollback_restores_previous(
    session,
) -> None:
    first = create_revision(
        session,
        kind="rubric",
        name="ranking-v1",
        content={"work_authorization_weight": 5},
        diff="initial",
        author="user",
        source_session="session-1",
    )
    evaluate_revision(
        session,
        first.id,
        {
            "quality_passed": True,
            "security_passed": True,
            "cost_passed": True,
            "replay": _passing_replay(baseline=0.8, candidate=0.85),
        },
    )
    second = create_revision(
        session,
        kind="rubric",
        name="ranking-v1",
        content={"work_authorization_weight": 7},
        diff="increase authorization weight",
        author="agent",
        source_session="session-2",
    )
    evaluate_revision(
        session,
        second.id,
        {
            "quality_passed": True,
            "security_passed": True,
            "cost_passed": True,
            "replay": _passing_replay(baseline=0.85, candidate=0.9),
        },
    )
    assert first.status == "rolled_back"
    assert second.status == "active"

    rollback_revision(session, second.id)
    assert first.status == "active"
    assert second.status == "rolled_back"

    failed = create_revision(
        session,
        kind="skill",
        name="user-owned-discovery",
        content={"steps": ["browse"]},
        diff="candidate change",
        author="agent",
        source_session="session-3",
    )
    evaluate_revision(
        session,
        failed.id,
        {"quality_passed": True, "security_passed": False, "cost_passed": True},
    )
    assert failed.status == "quarantined"
    assert session.scalar(
        select(RevisionRecord).where(
            RevisionRecord.name == "user-owned-discovery",
            RevisionRecord.status == "active",
        )
    ) is None


def test_rubric_activation_derives_gates_from_replay_not_caller_booleans(session) -> None:
    revision = create_revision(
        session,
        kind="rubric",
        name="ranking-replay-guard",
        content={"freshness_weight": 2},
        diff="candidate weighting change",
        author="agent",
        source_session="session-replay",
    )
    result = evaluate_revision(
        session,
        revision.id,
        {
            "quality_passed": True,
            "security_passed": True,
            "cost_passed": True,
            "replay": _passing_replay(baseline=0.9, candidate=0.85),
        },
    )

    assert result.status == "quarantined"
    assert result.evaluation["quality_passed"] is False
    assert any(
        "does not improve ranking quality" in reason
        for reason in result.evaluation["reasons"]
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"url": "https://www.linkedin.com/jobs/view/1"}, "LinkedIn remains manual"),
        (
            {
                "url": "https://jobs.example.test/apply",
                "fields": {"button[type=submit]": "Apply"},
            },
            "Submit-like controls",
        ),
    ],
)
def test_browser_assistance_rejects_linkedin_and_submit_controls(
    session, paths, payload, message
) -> None:
    with pytest.raises(PermissionError, match=message):
        validate_form_payload(session, payload, paths)
