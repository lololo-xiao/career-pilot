import argparse
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field

from app.matching import DEFAULT_OPENAI_MODEL, MatchError
from app.prompts import MATCH_PROMPT_VERSION
from app.schemas import MatchRequest, MatchResponse, StrictModel
from app.workflow import MATCH_WORKFLOW_VERSION, match_candidate_v3


CASES_PATH = Path(__file__).with_name("cases.json")


class EvalCase(StrictModel):
    name: str = Field(min_length=1)
    category: Literal["strong_match", "partial_match", "mismatch"]
    candidate_profile: str = Field(min_length=10)
    job_description: str = Field(min_length=10)
    expected_score_min: int = Field(ge=0, le=10)
    expected_score_max: int = Field(ge=0, le=10)
    expected_matched_skills: list[str]
    expected_missing_skills: list[str]
    forbidden_matched_skills: list[str]


def load_cases(path: Path = CASES_PATH) -> list[EvalCase]:
    with path.open(encoding="utf-8") as file:
        raw_cases = json.load(file)

    cases = [EvalCase.model_validate(raw_case) for raw_case in raw_cases]
    for case in cases:
        if case.expected_score_min > case.expected_score_max:
            raise ValueError(f"Invalid score range for eval case: {case.name}")
    return cases


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _contains_skill(actual: list[str], expected: str) -> bool:
    normalized_expected = _normalize(expected)
    return any(
        normalized_expected in _normalize(item) or _normalize(item) in normalized_expected
        for item in actual
    )


def evaluate_report(case: EvalCase, report: MatchResponse) -> list[str]:
    """Return human-readable failures; an empty list means the report passed."""

    failures: list[str] = []
    if not case.expected_score_min <= report.score <= case.expected_score_max:
        failures.append(
            f"score {report.score} outside "
            f"{case.expected_score_min}-{case.expected_score_max}"
        )

    matched = [item.skill for item in report.matched_skills]
    missing = [item.skill for item in report.missing_skills]

    for skill in case.expected_matched_skills:
        if not _contains_skill(matched, skill):
            failures.append(f"expected matched skill absent: {skill}")
    for skill in case.expected_missing_skills:
        if not _contains_skill(missing, skill):
            failures.append(f"expected missing skill absent: {skill}")
    for skill in case.forbidden_matched_skills:
        if _contains_skill(matched, skill):
            failures.append(f"unsupported skill presented as matched: {skill}")

    candidate_text = case.candidate_profile.casefold()
    for item in [*report.matched_skills, *report.adjacent_skills]:
        for evidence in item.evidence:
            if evidence.source_id != "candidate_profile" and not evidence.source_id.startswith(
                "candidate:"
            ):
                failures.append(f"unexpected evidence source: {evidence.source_id}")
            if evidence.quote.casefold() not in candidate_text:
                failures.append(f"candidate evidence is not verbatim: {evidence.quote}")

    job_text = case.job_description.casefold()
    for requirement in report.important_requirements:
        if requirement.evidence_quote.casefold() not in job_text:
            failures.append(
                f"job requirement evidence is not verbatim: {requirement.evidence_quote}"
            )

    return failures


Matcher = Callable[[MatchRequest], MatchResponse]


def select_cases(cases: list[EvalCase], names: list[str]) -> list[EvalCase]:
    if not names:
        return cases

    selected = [case for case in cases if case.name in names]
    missing = sorted(set(names) - {case.name for case in selected})
    if missing:
        raise ValueError(f"Unknown eval case(s): {', '.join(missing)}")
    return selected


def run_live_evals(
    cases: list[EvalCase],
    *,
    matcher: Matcher = match_candidate_v3,
    output_path: Path | None = None,
) -> int:
    load_dotenv()
    passed = 0
    results: list[dict[str, object]] = []
    for case in cases:
        started_at = monotonic()
        try:
            report = matcher(
                MatchRequest(
                    candidate_profile=case.candidate_profile,
                    job_description=case.job_description,
                )
            )
            failures = evaluate_report(case, report)
            report_payload: dict[str, object] | None = report.model_dump(mode="json")
        except MatchError as exc:
            failures = [f"workflow error: {exc}"]
            report_payload = None

        duration_ms = round((monotonic() - started_at) * 1000, 1)
        if failures:
            print(f"FAIL {case.name}: {'; '.join(failures)}")
        else:
            passed += 1
            print(f"PASS {case.name}")
        results.append(
            {
                "name": case.name,
                "category": case.category,
                "passed": not failures,
                "duration_ms": duration_ms,
                "failures": failures,
                "report": report_payload,
            }
        )

    print(f"{passed}/{len(cases)} eval cases passed")
    if output_path is not None:
        model = os.getenv("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
        payload = {
            "generated_at": datetime.now(UTC).isoformat(),
            "model": model,
            "prompt_version": MATCH_PROMPT_VERSION,
            "workflow_version": MATCH_WORKFLOW_VERSION,
            "passed": passed,
            "total": len(cases),
            "results": results,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote evaluation artifact to {output_path}")
    return 0 if passed == len(cases) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or run CareerPilot evals")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Call the configured provider. This can incur API charges.",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        dest="case_names",
        help="Run one named case. Repeat to select multiple cases.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write a versioned JSON artifact for a live run.",
    )
    args = parser.parse_args()
    try:
        cases = select_cases(load_cases(), args.case_names)
    except ValueError as exc:
        parser.error(str(exc))

    if not args.live:
        if args.output is not None:
            parser.error("--output requires --live")
        categories = sorted({case.category for case in cases})
        print(f"Validated {len(cases)} eval cases: {', '.join(categories)}")
        print("Use --live to run paid provider evaluations.")
        return 0

    return run_live_evals(cases, output_path=args.output)


if __name__ == "__main__":
    raise SystemExit(main())
