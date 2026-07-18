from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from career_companion.database import (
    ApplicationRecord,
    CandidateProfileRecord,
    JobRecord,
    RevisionRecord,
    SourceDocumentRecord,
    StatusEventRecord,
    UsageRunRecord,
)
from career_companion.paths import CompanionPaths
from career_companion.persistence import clear_factory_cache, session_factory_for
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
    create_application,
    decide_scored_application,
    ensure_application,
    transition_application,
)
from career_companion.services.approvals import (
    consume_approval,
    decide_approval,
    payload_digest,
    request_approval,
)
from career_companion.services.browser import validate_form_payload
from career_companion.services.model_routes import record_usage, upsert_route
from career_companion.services.profile import _candidate_from_text, save_profile
from career_companion.services.revisions import (
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
    barrier = threading.Barrier(2)
    factory = session_factory_for(paths)

    def decide() -> bool:
        with factory() as worker_session:
            barrier.wait()
            return decide_scored_application(
                worker_session,
                application.id,
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
                StatusEventRecord.application_id == application.id,
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
            kind="memory",
            name="verified-fact:work-authorization",
            content={"value": "changed"},
            diff="unsafe",
            author="agent",
            source_session="hostile-jd",
        )


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
