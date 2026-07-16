from collections.abc import Sequence

from app.matching import MatchResponseError
from app.schemas import EvidenceSnippet, MatchRequest, MatchResponse


def validate_report_grounding(
    request: MatchRequest,
    evidence: Sequence[EvidenceSnippet],
    report: MatchResponse,
) -> MatchResponse:
    """Reject citations that cannot be found verbatim in their declared source."""

    sources = {item.source_id: item.text.casefold() for item in evidence}
    failures: list[str] = []

    for skill in [*report.matched_skills, *report.adjacent_skills]:
        for citation in skill.evidence:
            source = sources.get(citation.source_id)
            if source is None:
                failures.append(
                    f"{skill.skill!r} cites unknown source {citation.source_id!r}"
                )
            elif citation.quote.casefold() not in source:
                failures.append(
                    f"{skill.skill!r} uses a quote absent from {citation.source_id!r}"
                )

    job_description = request.job_description.casefold()
    for requirement in report.important_requirements:
        if requirement.evidence_quote.casefold() not in job_description:
            failures.append(
                f"job requirement {requirement.requirement!r} has a non-verbatim quote"
            )

    if failures:
        raise MatchResponseError(
            "The generated report failed grounding validation: " + "; ".join(failures)
        )
    return report
