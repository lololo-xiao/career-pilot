from collections.abc import Callable, Sequence
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.grounding import validate_report_grounding
from app.matching import MatchError, MatchResponseError, match_candidate_v2
from app.observability import (
    trace_attributes,
    trace_observation,
    update_observation,
)
from app.prompts import MATCH_PROMPT_VERSION
from app.retrieval import retrieve_candidate_evidence
from app.schemas import EvidenceSnippet, MatchRequest, MatchResponse


Retriever = Callable[[str, str], list[EvidenceSnippet]]
ReportGenerator = Callable[[MatchRequest, Sequence[EvidenceSnippet]], MatchResponse]
GroundingVerifier = Callable[
    [MatchRequest, Sequence[EvidenceSnippet], MatchResponse], MatchResponse
]
MATCH_WORKFLOW_VERSION = "match-workflow-v1"


class MatchWorkflowState(TypedDict, total=False):
    request: MatchRequest
    evidence: list[EvidenceSnippet]
    report: MatchResponse


class MatchWorkflowError(MatchError):
    pass


def _generate_report(
    request: MatchRequest,
    evidence: Sequence[EvidenceSnippet],
) -> MatchResponse:
    return match_candidate_v2(request, evidence=evidence)


def build_match_workflow(
    *,
    retriever: Retriever = retrieve_candidate_evidence,
    generator: ReportGenerator = _generate_report,
    verifier: GroundingVerifier = validate_report_grounding,
) -> Any:
    """Build the small explicit workflow; dependencies remain injectable for tests."""

    def retrieve_node(state: MatchWorkflowState) -> dict[str, list[EvidenceSnippet]]:
        request = state["request"]
        with trace_observation(
            "retrieve-candidate-evidence",
            as_type="retriever",
            input={
                "candidateCharacters": len(request.candidate_profile),
                "jobDescription": request.job_description,
            },
        ) as observation:
            evidence = retriever(request.candidate_profile, request.job_description)
            if not evidence:
                raise MatchWorkflowError("No candidate evidence could be retrieved")
            update_observation(
                observation,
                output={
                    "sourceIds": [item.source_id for item in evidence],
                    "chunkCount": len(evidence),
                },
            )
            return {"evidence": evidence}

    def analyze_node(state: MatchWorkflowState) -> dict[str, MatchResponse]:
        with trace_observation(
            "generate-match-report",
            as_type="chain",
            input={"sourceIds": [item.source_id for item in state["evidence"]]},
            metadata={"promptVersion": MATCH_PROMPT_VERSION},
        ) as observation:
            report = generator(state["request"], state["evidence"])
            update_observation(observation, output=report.model_dump(mode="json"))
            return {"report": report}

    def verify_node(state: MatchWorkflowState) -> dict[str, MatchResponse]:
        with trace_observation(
            "verify-grounding",
            as_type="guardrail",
            input={
                "matchedSkillCount": len(state["report"].matched_skills),
                "adjacentSkillCount": len(state["report"].adjacent_skills),
            },
        ) as observation:
            report = verifier(state["request"], state["evidence"], state["report"])
            update_observation(observation, output={"grounded": True})
            return {"report": report}

    builder = StateGraph(MatchWorkflowState)
    builder.add_node("retrieve_candidate_evidence", retrieve_node)
    builder.add_node("generate_match_report", analyze_node)
    builder.add_node("verify_grounding", verify_node)
    builder.add_edge(START, "retrieve_candidate_evidence")
    builder.add_edge("retrieve_candidate_evidence", "generate_match_report")
    builder.add_edge("generate_match_report", "verify_grounding")
    builder.add_edge("verify_grounding", END)
    return builder.compile()


MATCH_WORKFLOW = build_match_workflow()


def match_candidate_v3(
    request: MatchRequest,
    *,
    workflow: Any | None = None,
    generator: ReportGenerator | None = None,
) -> MatchResponse:
    """Run retrieval, structured analysis, and grounding verification via LangGraph."""

    if workflow is not None and generator is not None:
        raise ValueError("Pass either workflow or generator, not both")
    selected_workflow = (
        workflow
        or (build_match_workflow(generator=generator) if generator else MATCH_WORKFLOW)
    )
    with trace_observation(
        "career-pilot-match",
        as_type="agent",
        input=request.model_dump(mode="json"),
        metadata={"promptVersion": MATCH_PROMPT_VERSION},
        version=MATCH_WORKFLOW_VERSION,
    ) as observation:
        with trace_attributes(
            metadata={"promptVersion": MATCH_PROMPT_VERSION},
            version=MATCH_WORKFLOW_VERSION,
            tags=["career-pilot", "build-week"],
            trace_name="career-pilot-match",
        ):
            try:
                result = selected_workflow.invoke({"request": request})
            except MatchError:
                raise
            except Exception as exc:
                raise MatchWorkflowError("The evidence workflow failed") from exc

            report = result.get("report")
            if not isinstance(report, MatchResponse):
                raise MatchResponseError("The workflow returned no validated match report")
            update_observation(observation, output=report.model_dump(mode="json"))
            return report
