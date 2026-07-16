from collections.abc import Sequence

import pytest

from app.grounding import validate_report_grounding
from app.auth import AuthStore, AuthenticatedAccount, ProviderConnection
from app.main import get_matcher
from app.schemas import EvidenceSnippet, MatchRequest, MatchResponse
from app.workflow import MatchWorkflowError, build_match_workflow, match_candidate_v3


REQUEST = MatchRequest(
    candidate_profile="Built Python APIs and retrieval systems.",
    job_description="Python is required for this role.",
)


def test_v3_runs_retrieve_analyze_verify_workflow() -> None:
    calls: list[str] = []
    evidence = EvidenceSnippet(
        source_id="candidate:0001",
        text="Built Python APIs and retrieval systems.",
    )

    def retrieve(candidate_profile: str, job_description: str) -> list[EvidenceSnippet]:
        calls.append("retrieve")
        assert candidate_profile == REQUEST.candidate_profile
        assert job_description == REQUEST.job_description
        return [evidence]

    def generate(
        request: MatchRequest,
        retrieved: Sequence[EvidenceSnippet],
    ) -> MatchResponse:
        calls.append("analyze")
        assert request is REQUEST
        assert list(retrieved) == [evidence]
        return MatchResponse.model_validate(
            {
                "score": 8,
                "summary": "The candidate has direct Python evidence.",
                "matched_skills": [
                    {
                        "skill": "Python",
                        "explanation": "Python work is explicit.",
                        "evidence": [
                            {
                                "source_id": "candidate:0001",
                                "quote": "Built Python APIs",
                            }
                        ],
                    }
                ],
                "adjacent_skills": [],
                "missing_skills": [],
                "important_requirements": [
                    {
                        "requirement": "Python",
                        "importance": "required",
                        "evidence_quote": "Python is required",
                    }
                ],
                "preparation_actions": [],
                "unsupported_claim_warnings": [],
            }
        )

    def verify(
        request: MatchRequest,
        retrieved: Sequence[EvidenceSnippet],
        report: MatchResponse,
    ) -> MatchResponse:
        calls.append("verify")
        return validate_report_grounding(request, retrieved, report)

    workflow = build_match_workflow(
        retriever=retrieve,
        generator=generate,
        verifier=verify,
    )

    report = match_candidate_v3(REQUEST, workflow=workflow)

    assert report.score == 8
    assert calls == ["retrieve", "analyze", "verify"]


def test_fastapi_production_dependency_builds_authenticated_matcher(tmp_path) -> None:
    account = AuthenticatedAccount(
        user_id="test-user",
        email="person@example.com",
        display_name="Person",
        identity_method="local",
        active_provider="api_key",
        provider_connection=ProviderConnection(
            provider="api_key",
            credential=b"sk-test-key-with-enough-characters",
            plan_type="usage-based",
        ),
    )
    store = AuthStore(tmp_path / "auth.db", "t" * 48)

    assert callable(get_matcher(account, store))


def test_v3_rejects_empty_retrieval_result() -> None:
    def generator_must_not_run(
        _request: MatchRequest,
        _evidence: Sequence[EvidenceSnippet],
    ) -> MatchResponse:
        raise AssertionError("generator should not run without evidence")

    workflow = build_match_workflow(
        retriever=lambda _profile, _job: [],
        generator=generator_must_not_run,
    )

    with pytest.raises(MatchWorkflowError, match="No candidate evidence"):
        match_candidate_v3(REQUEST, workflow=workflow)
