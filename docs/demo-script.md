# CareerPilot demo script

Target length: 3 minutes. Record a backup before the July 16 freeze.

## Storyboard

**0:00–0:20 — Promise**

“Candidates are told to tailor every application, but the pressure to match keywords can
turn into invented experience. CareerPilot helps you tailor aggressively without making
claims your evidence cannot support.”

Show the landing page and point to “Evidence over embellishment.”

**0:20–0:45 — Inputs**

Click **Load demo profile**. Briefly call out that the candidate has Python, RAG, Chroma,
FastAPI, evaluation, Docker, Azure Container Apps, and mentoring evidence. The job prefers
Kubernetes and LangGraph, which are intentionally absent from the profile.

**0:45–1:05 — Run**

Click **Run match analysis**.

“Behind this button, FastAPI invokes a LangGraph workflow. Chroma retrieves relevant CV
chunks, GPT-5.6 returns a strict report, and a deterministic guardrail verifies every
citation before the UI can display it.”

**1:05–2:15 — Explain the report**

Start with the score and one-sentence summary. Then scroll through:

1. A direct matched skill and its verbatim candidate quote.
2. An adjacent skill, emphasizing that transferable is not equivalent.
3. Kubernetes or another missing skill.
4. A prioritized preparation action tied to that gap.
5. The unsupported-claim warning—this is the product differentiator.

Avoid reading every card. The story is the separation between supported, adjacent,
missing, and dishonest-to-claim.

**2:15–2:40 — Evidence and operations**

Show one Langfuse trace with the retriever, analysis/model, and grounding guardrail spans.
Point out prompt/workflow versions, model latency, and token usage. If traces are not
available, show the architecture diagram in `docs/architecture.md`.

**2:40–3:00 — Close**

“CareerPilot does not write fiction about your career. It shows what you can defend,
what is adjacent, and what to learn next. Tailor aggressively. Never invent experience.”

## Recording checklist

- Use a 16:9 browser window at 1920×1080 or 1440×810 and zoom to 100%.
- Hide bookmarks, notifications, secrets, provider dashboards, and unrelated tabs.
- Start from a fresh page and use the built-in demo inputs for repeatability.
- Run one warm-up analysis before recording so the embedding model and services are warm.
- Keep a successfully generated report open in a second tab as a fallback.
- Record a separate 10-second architecture/Langfuse shot so it can be edited in cleanly.
- Verify text is readable and audio has no clipping before recording the final take.
- Never display `.env`, API keys, full personal CVs, or other candidates' trace data.

## Pre-demo release gate

```bash
uv run python -m scripts.demo_preflight
uv run python -m pytest -q
uv run python -m evals.run_evals
cd frontend && npm run lint && npm run build
```

Then check the deployed `/health`, run the built-in sample end-to-end, open its Langfuse
trace if enabled, and confirm the backup video plays without network access.
