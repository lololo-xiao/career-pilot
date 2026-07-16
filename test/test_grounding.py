import pytest

from app.grounding import validate_report_grounding
from app.matching import MatchResponseError
from app.schemas import EvidenceSnippet, MatchRequest, MatchResponse


REQUEST = MatchRequest(
    candidate_profile="Built Python APIs and evaluated retrieval systems.",
    job_description="Python is required. Kubernetes experience is preferred.",
)
EVIDENCE = [
    EvidenceSnippet(
        source_id="candidate:0001",
        text="Built Python APIs and evaluated retrieval systems.",
    )
]


def build_report(
    *,
    source_id: str = "candidate:0001",
    candidate_quote: str = "Built Python APIs",
    job_quote: str = "Python is required",
) -> MatchResponse:
    return MatchResponse.model_validate(
        {
            "score": 6,
            "summary": "Python is grounded; Kubernetes is not supported.",
            "matched_skills": [
                {
                    "skill": "Python",
                    "explanation": "The profile explicitly describes Python work.",
                    "evidence": [
                        {"source_id": source_id, "quote": candidate_quote}
                    ],
                }
            ],
            "adjacent_skills": [],
            "missing_skills": [
                {
                    "skill": "Kubernetes",
                    "importance": "preferred",
                    "explanation": "No retrieved evidence supports it.",
                }
            ],
            "important_requirements": [
                {
                    "requirement": "Python",
                    "importance": "required",
                    "evidence_quote": job_quote,
                }
            ],
            "preparation_actions": [],
            "unsupported_claim_warnings": [
                {
                    "claim": "Kubernetes experience",
                    "reason": "The candidate evidence does not mention Kubernetes.",
                }
            ],
        }
    )


def test_grounding_accepts_verbatim_declared_sources() -> None:
    report = build_report()

    assert validate_report_grounding(REQUEST, EVIDENCE, report) is report


@pytest.mark.parametrize(
    ("source_id", "candidate_quote"),
    [
        ("candidate:9999", "Built Python APIs"),
        ("candidate:0001", "Led a Kubernetes migration"),
    ],
)
def test_grounding_rejects_unknown_or_invented_candidate_evidence(
    source_id: str,
    candidate_quote: str,
) -> None:
    with pytest.raises(MatchResponseError, match="failed grounding validation"):
        validate_report_grounding(
            REQUEST,
            EVIDENCE,
            build_report(source_id=source_id, candidate_quote=candidate_quote),
        )


def test_grounding_rejects_invented_job_requirement_quote() -> None:
    with pytest.raises(MatchResponseError, match="non-verbatim"):
        validate_report_grounding(
            REQUEST,
            EVIDENCE,
            build_report(job_quote="Five years of Python are required"),
        )
