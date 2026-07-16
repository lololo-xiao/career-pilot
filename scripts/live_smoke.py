"""Run exactly one explicit provider-backed smoke test."""

from app.schemas import MatchRequest
from app.workflow import match_candidate_v3


def main() -> None:
    request = MatchRequest(
        candidate_profile=(
            "Python AI engineer with experience building RAG applications and "
            "evaluating LLM outputs."
        ),
        job_description=(
            "We need an AI engineer with Python and RAG experience. "
            "Kubernetes experience is preferred."
        ),
    )
    report = match_candidate_v3(request)
    print(report.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
