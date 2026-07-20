# CareerPilot meetup demo script

Target length: 5 minutes. The story is one role, one evidence-based decision, and one
controlled next step. Keep the screenshot deck open as the fallback.

## Live story

### 0:00–0:35 — Meet Pilot

Open the local app at `http://127.0.0.1:8787/`.

“CareerPilot is a local-first job-search agent that remembers the work, has a personality,
and helps carry a search forward—but it still has to show the evidence behind its advice.”

Point to the named Pilot, persistent sessions, visible model route, and current role context.
Do not make a provider call unless a known-good result was pre-warmed before the meetup.

### 0:35–1:20 — Preview one known board

Open **Discover jobs** and use the deterministic demo source, or a previously verified
Greenhouse/Lever board.

- The preview starts with zero roles selected and zero saved.
- Check one fictional role deliberately.
- Save only that role into the deduplicated queue.

Say: “This bounded read can contact the named public job-board provider, but it does not
send the candidate profile, score a role, create an application, or submit anything.”

### 1:20–2:35 — Decide with evidence

Open the pre-warmed fit report and ask, “Is this worth pursuing?”

Show only four things:

1. One demonstrated requirement with its candidate quote.
2. One adjacent skill, emphasizing that transferable is not equivalent.
3. One honest gap such as Kubernetes.
4. The unsupported-claim warning and a concrete preparation action.

“CareerPilot can say that you are close without rewriting ‘close’ into ‘experienced.’”

If a provider-backed report is unavailable, use `meetup-assets/fit-report.png`; do not run a
paid call on stage just to fill time.

### 2:35–3:25 — Show the source of truth

Open **My profile**, then the ranked **Job queue**.

Point out that imported evidence is reviewable, queue scores have visible reasons, and the
0–10 fit report is a separate evidence-grounded judgment. Move the role to the appropriate
application stage and show its next action. Decide and prepare honestly; do not promise
finished evidence-safe tailoring while that lane remains gated.

### 3:25–4:25 — Open the approval record

Go to **Controls & memory → Approval records** and open the form-fill record.

Show:

- the exact action type and abbreviated SHA-256 request fingerprint;
- the expiry time and `0/1` or `1/1` remaining-use state;
- the recorded request summary; and
- **Does not authorize**.

Say: “This is an audit and explanation record, not a reusable password. The guarded backend
accepts the exact approved request at most once before expiry. Pilot cannot use it to submit
an application, and I will not trigger a browser fill in this demo.”

### 4:25–5:00 — Close at the boundary

“The durable career workspace stays on this device. Disclosed provider calls, public-board
reads, and optional MCP checks can cross a bounded network path. Consequential external
actions still need a specific approval, and final submission stays human.”

End on the product promise: “Automate the search. Keep the truth.”

## Optional 30–40 second MCP segment

Use this only when the owned local fixture is already configured and tested.

1. Open **Settings → Tools & MCP**.
2. Select the disabled **Meetup local job catalog** fixture.
3. Click **Test saved connection** and review the exact saved target.
4. Choose **Run this check once**.
5. Show the allowed, new, and missing tool-name groups, then leave the proposed allowlist as
   an unsaved draft.

Say: “This manual check starts the saved local process and performs MCP initialization plus
at most four `tools/list` pages. Startup or initialization can itself have effects, so it
still needs approval. CareerPilot does not call a discovered tool, start OAuth, run this in
the background, trust server descriptions, or silently save the draft.”

## Recording checklist

- Use a 16:9 browser window at 1920×1080 or 1440×810 and 100% zoom.
- Use the credential-free demo workspace; never show `.env`, API keys, personal CVs,
  provider dashboards, or unrelated tabs.
- Warm the local app, guided demo source, and screenshot fallbacks before recording.
- Keep a successfully generated fit report open only if its provider call was explicitly
  authorized and completed before the demo.
- Keep optional Langfuse tracing disabled unless privacy wording and one opt-in trace were
  reviewed in advance.
- Create a fresh credential-free backup before the final recording. The restore drill itself
  is already proven; provider credentials are intentionally absent and must be reconnected.
- Verify text is readable, notifications are hidden, and the backup video plays offline.

## Pre-demo release gate

Run without a paid model call:

```bash
uv run python -m scripts.demo_preflight
uv run python -m pytest -p no:cacheprovider -q
uv run python -m evals.run_evals
cd frontend
npm test
npm run lint
npx tsc --noEmit --allowImportingTsExtensions
npm run build -- --webpack
```

Then verify the local loopback `/health`, open the seeded workspace, rehearse the exact
five-minute path, and confirm the deck and screenshot fallback work without network access.
