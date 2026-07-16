# Build Week compressed plan

Deadline: July 21, 2026 at 5:00 PM PT. Demo freeze target: July 16 for the Paris meetup on
July 17.

## Must have

| Deliverable | Status | Release gate |
| --- | --- | --- |
| Polished input-to-report journey | Implemented | Browser QA and production frontend build |
| PDF/DOCX/TXT CV upload | Implemented | Parser, abuse-limit, endpoint, lint, and build tests pass |
| Local workspace and required provider choice | Implemented locally | Live OpenAI key and ChatGPT/Codex connection smoke tests |
| GPT-5.6 structured report | Implemented | One explicit paid smoke test still required |
| Evidence retrieval and citations | Implemented | Deterministic Chroma and grounding tests pass |
| LangGraph workflow | Implemented | Retrieve → analyze → verify test passes |
| Fixed evaluation set | Implemented | Five strong/partial/mismatch cases validate offline |
| Langfuse observability | Implemented, opt-in | Add credentials and inspect one live trace |
| Reproducible demo | Implemented locally | Production commands and standalone frontend smoke pass |
| Public deployment | Credential-blocked | Publish API + UI and run one end-to-end request |
| README, architecture, video | Docs implemented | Record and review the final video |

## Nice to have after the freeze

- Workflow visualization embedded in the UI.
- Save a small set of previous analyses in browser storage.
- Add more adversarial prompt-injection and citation eval cases.

Only take one of these after the frozen deployed flow and backup recording both work.

## Post-hackathon

- Authentication, multi-user isolation, rate limiting, consent records, and audit flows
  before any public deployment.
- Persistent profiles, multiple CV versions, PostgreSQL/pgvector, and application tracking.
- Production RAG ingestion, richer document metadata, access control, and retention policy.
- Kubernetes, multi-region deployment, and production-grade CI/CD/security hardening.

## Daily execution

- July 14: finish the coherent vertical slice and all network-free verification.
- July 15: authorize one paid smoke, run live evals selectively, tune only evidenced
  regressions, and deploy both services.
- July 16: rehearse on the deployed URL, inspect a Langfuse trace, record the backup demo,
  and freeze.
- July 17: demonstrate the frozen build in Paris; fix only blockers.
- July 18–20: improve submission narrative, add safe polish, and record the final cut.
- July 21: run the release checklist in the morning and submit well before 5:00 PM PT.

## What to learn at this checkpoint

- FastAPI dependencies let production and tests select different matcher functions.
- Pydantic validates structure; a separate grounding layer validates provenance.
- Retrieval narrows admissible evidence before generation, instead of asking the model to
  reason over an unstructured profile and trust its citations.
- LangGraph is valuable here because the stages have different failure modes and traces.
- Fixed evals turn prompt edits into measurable regressions rather than subjective tweaks.
