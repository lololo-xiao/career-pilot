from __future__ import annotations

from typing import Any


def evaluate_replay_metrics(
    metrics: dict[str, Any],
    *,
    require_quality_improvement: bool,
) -> dict[str, Any]:
    required = {
        "cases_total",
        "ranking_pass_rate",
        "baseline_ranking_pass_rate",
        "unsupported_claims",
        "keyword_coverage",
        "one_page_success_rate",
        "security_failures",
        "baseline_estimated_cost_usd",
        "candidate_estimated_cost_usd",
    }
    missing = sorted(required - metrics.keys())
    if missing:
        return {
            "quality_passed": False,
            "security_passed": False,
            "cost_passed": False,
            "reasons": [f"Missing replay metric: {name}" for name in missing],
        }

    cases_total = int(metrics["cases_total"])
    ranking = float(metrics["ranking_pass_rate"])
    baseline_ranking = float(metrics["baseline_ranking_pass_rate"])
    unsupported = int(metrics["unsupported_claims"])
    keyword_coverage = float(metrics["keyword_coverage"])
    one_page = float(metrics["one_page_success_rate"])
    security_failures = int(metrics["security_failures"])
    baseline_cost = float(metrics["baseline_estimated_cost_usd"])
    candidate_cost = float(metrics["candidate_estimated_cost_usd"])

    reasons: list[str] = []
    quality_passed = (
        20 <= cases_total <= 30
        and ranking >= 0.8
        and keyword_coverage >= 0.75
        and one_page >= 0.9
        and (
            ranking > baseline_ranking
            if require_quality_improvement
            else ranking >= baseline_ranking
        )
    )
    if not 20 <= cases_total <= 30:
        reasons.append("Replay suite must contain 20 through 30 cases")
    if ranking < 0.8:
        reasons.append("Ranking pass rate is below 0.80")
    if require_quality_improvement and ranking <= baseline_ranking:
        reasons.append("Rubric candidate does not improve ranking quality")
    elif not require_quality_improvement and ranking < baseline_ranking:
        reasons.append("Skill candidate regresses ranking quality")
    if keyword_coverage < 0.75:
        reasons.append("Keyword coverage is below 0.75")
    if one_page < 0.9:
        reasons.append("One-page success rate is below 0.90")

    security_passed = unsupported == 0 and security_failures == 0
    if unsupported:
        reasons.append("Replay produced unsupported claims")
    if security_failures:
        reasons.append("Replay produced security failures")

    cost_passed = candidate_cost <= baseline_cost
    if not cost_passed:
        reasons.append("Candidate increases estimated replay cost")
    return {
        "quality_passed": quality_passed,
        "security_passed": security_passed,
        "cost_passed": cost_passed,
        "reasons": reasons,
    }
