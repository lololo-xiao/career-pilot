# Release status audit

Last audited: July 15, 2026. A requirement is complete here only when the current
worktree or an observed runtime check proves it.

## Career Companion v1 implementation

| Requirement | Evidence | Status |
| --- | --- | --- |
| Polished end-to-end journey | Automatic local workspace, required provider Settings, Pilot, and `/workspace` pass lint, production compilation, local browser QA, native PDF rendering, and a real disposable-form fill | Provider-backed conversation run pending |
| Upload or paste CV | Bounded TXT, DOCX, and generated-PDF extraction tests; multipart endpoint; editable frontend upload control | Complete locally; image-only PDF OCR deliberately unsupported |
| Local access model | No CareerPilot account or sign-in; one stable device-local identity preserves encrypted providers and workspace state; AI connection remains mandatory | Complete for loopback-only use; public exposure prohibited without external authentication |
| Dual OpenAI billing | User-owned OpenAI key path plus ChatGPT-plan Codex app-server path; both feed the same schema and grounding workflow | Code complete; both live paths still need credentialed smoke tests |
| GPT-5.6 integration | Direct OpenAI adapter defaults to and preflight enforces `gpt-5.6-sol`; strict Chat Completions request is unit-tested | Code complete; paid live call not yet authorized |
| Structured validated output | Pydantic models reject extra/missing/invalid fields; provider-output and API error tests pass | Complete |
| Evidence-grounded analysis | Real MiniLM/Chroma path returned the relevant chunk; citations and JD quotes are verified verbatim after generation | Complete locally |
| Simple usable frontend | Responsive provider Settings, Pilot conversation, persistent evidence, job queue, applications, approvals, model routes, and evolution history; lint/build and local browser QA pass | Complete locally |
| Basic retrieval with citations | Chroma is the only store; deterministic ranking test and real embedding preflight pass | Complete |
| Evaluation and evolution | 25 anonymized strong/partial/mismatch cases, replay thresholds, unsupported-claim checks, and replay-derived activation gates | Offline suite complete; live token/cost observations pending paid calls |
| Langfuse observability | Optional v4 SDK integration traces agent/retriever/chain/generation/guardrail and attaches release versions; remote telemetry disabled in tests | Code complete; no Langfuse keys to verify a remote trace |
| Local installation | Same-origin FastAPI/Next runtime, macOS/Linux and Windows installers, doctor, backup/restore, full Docker fallback, and loopback publishing | Native macOS installer and installed runtime verified; Windows/Linux CI execution pending a remote |
| README and demo video | README, architecture notes, three-minute script, and recording checklist exist | Documentation complete; actual recording pending live/deployed demo |

## Verification snapshot

- Backend suite: 112 passed; one upstream Starlette/httpx deprecation warning.
- Hermes 0.18.2's real plugin manager discovers and enables the profile-owned
  plugin with nine career tools and one policy hook; its resolved API-server
  toolsets exclude terminal, file, raw memory, cron, and delegation.
- Offline evaluation: 25 unique cases and all three categories validate.
- `uv run python -m scripts.demo_preflight`: checks Python, local provider encryption secret,
  Codex runtime presence, real MiniLM/Chroma retrieval, GPT-5.6 model, and release versions.
- `npm run lint`: passes.
- `npm run build`: both standalone and same-origin static production builds pass.
- `npm audit --audit-level=moderate`: zero vulnerabilities after the pinned PostCSS
  security override.
- FastAPI serves the exported `/`, `/settings/`, and `/workspace/` routes without
  shadowing API routes; browser-origin mutation protection is covered by tests.
- FastAPI tests prove automatic local identity, removed login endpoints, mandatory provider
  selection, `/health`, local matching/import endpoints, workspace isolation, one-time
  approvals, confirmed submissions,
  route budgets, immutable facts, reversible revisions, and the account-secret
  Hermes bridge.
- Form-preview tests prove exact current-message binding, explicit application approval,
  verified-evidence and approved-artifact mapping, deterministic retry/concurrency
  convergence, account isolation, prompt-injection resistance, visible unresolved fields,
  reload reconciliation, and a false external-mutation audit record.
- Hermes lifecycle tests prove route-selected provider isolation, encrypted Codex
  refresh, authenticated readiness, provider invalidation, shutdown, and streamed
  browser events without exposing provider or bridge credentials.
- Release security tests prove untrusted Tectonic mode, TeX/asset/link validation,
  redirect and element-semantic browser checks, no-submit form guards, atomic bounded
  backups, origin protection, generic templates, pinned manifests, and replay-derived
  evolution quarantine.
- `./install.sh --no-start` completes on macOS ARM64 from the source checkout. Its doctor
  launches Chromium 149, verifies the recorded browser hash, Hermes 0.18.2, Tectonic
  0.16.9, private secret permissions, the web build, and loopback configuration.
- The installed Tectonic runtime renders the generic AI/ML CV template on the first
  attempt as a valid one-page PDF with 418 extracted characters and no render errors.
- The installed Playwright Chromium fills a disposable loopback-only application form;
  an `oninput` auto-submit attempt produces zero POST requests and control returns with
  `submitted: false`.
- `uv build` produces both a wheel and source distribution. The wheel contains the three
  Python packages, generic templates, console entry point, and sanitized Hermes profile
  distribution.
- Next.js production smoke: `/`, `/settings`, and `/workspace` compile and serve.
- Local browser QA opens without sign-in, adds and deterministically scores a job, tracks
  an application, opens the application and model/memory controls, verifies mobile
  width without horizontal overflow, and reports no browser console errors.
- Credential-storage tests prove the raw API key does not appear in the SQLite file;
  `.env` and `.data` remain ignored.
- Docker image build: definition includes the full app, isolated Hermes, verified
  Tectonic, Playwright, profile, and migrations, but cannot be built here because Docker
  is not installed.
- GitHub Actions: workflow is defined but cannot run because there is no remote and the
  local GitHub authentication is invalid.

## External completion gates

1. Run the native installer on clean Windows and Linux machines; macOS ARM64 is locally
   proven.
2. Complete one real ChatGPT device authorization and one plan-backed Codex analysis.
3. Approve exactly one API-key-backed `uv run python -m scripts.live_smoke` call.
4. Perform one backup, destructive test copy, and restore drill.
5. Add Langfuse credentials and inspect one resulting trace, or explicitly defer remote
   tracing for the meetup build.
6. Add an authentication boundary before any public hosting, or keep the demo loopback-only.
7. Record and review the backup/final demo video.

No automated test submits an application, contacts an employer, or makes a paid provider
call. Until the credentialed and cross-platform gates are satisfied, release sign-off is
not represented as complete.
