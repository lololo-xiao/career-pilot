from pathlib import Path

import pytest

from evals.run_evals import (
    EvalCase,
    evaluate_report,
    load_cases,
    run_live_evals,
    select_cases,
)
from test.test_matching import REPORT

from app.schemas import MatchResponse


def test_eval_dataset_covers_required_categories() -> None:
    cases = load_cases()

    assert 20 <= len(cases) <= 30
    assert len({case.name for case in cases}) == len(cases)
    assert {case.category for case in cases} == {
        "strong_match",
        "partial_match",
        "mismatch",
    }


def test_eval_flags_an_unsupported_matched_skill() -> None:
    case = next(case for case in load_cases() if case.name == "clear_mismatch")
    report_data = {
        **REPORT,
        "score": 2,
        "matched_skills": [
            {
                "skill": "Go",
                "explanation": "Incorrectly inferred.",
                "evidence": [
                    {
                        "source_id": "candidate_profile",
                        "quote": "Frontend developer",
                    }
                ],
            }
        ],
        "missing_skills": [
            {
                "skill": "PostgreSQL",
                "importance": "unspecified",
                "explanation": "Not present.",
            }
        ],
        "important_requirements": [],
    }

    failures = evaluate_report(case, MatchResponse.model_validate(report_data))

    assert "unsupported skill presented as matched: Go" in failures


def test_select_cases_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="Unknown eval case"):
        select_cases(load_cases(), ["does-not-exist"])


def test_live_eval_can_write_versioned_artifact_without_network(
    tmp_path: Path,
) -> None:
    case = EvalCase(
        name="artifact_test",
        category="strong_match",
        candidate_profile="Python engineer with RAG experience.",
        job_description="Python and RAG are required; Kubernetes is preferred.",
        expected_score_min=8,
        expected_score_max=8,
        expected_matched_skills=["Python", "RAG"],
        expected_missing_skills=["Kubernetes"],
        forbidden_matched_skills=[],
    )
    report = MatchResponse.model_validate(REPORT)
    output_path = tmp_path / "eval-result.json"

    exit_code = run_live_evals(
        [case],
        matcher=lambda _request: report,
        output_path=output_path,
    )

    artifact = output_path.read_text(encoding="utf-8")
    assert exit_code == 0
    assert '"prompt_version": "match-v2-rag"' in artifact
    assert '"workflow_version": "match-workflow-v1"' in artifact
    assert '"passed": 1' in artifact
