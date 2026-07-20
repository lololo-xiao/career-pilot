# Release status audit

Last audited: July 20, 2026 against integrated product baseline `a5dae60`, with this
documentation refresh layered above it. A requirement is complete here only when the
integrated tree or an observed runtime check proves it. The isolated Windows candidate
`387f32b` is reported separately and is not part of `main`.

## Career Companion v1 implementation

| Requirement | Evidence | Status |
| --- | --- | --- |
| Polished end-to-end journey | Recoverable five-step Settings, Pilot, dedicated `/discover`, skills, and `/workspace` pass unit tests, lint, compatible TypeScript, production compilation, and local browser QA | Provider-backed conversation still needs a live smoke; tailoring and practice remain gated |
| Upload or paste CV | Bounded TXT, DOCX, and generated-PDF extraction tests; multipart endpoint; editable frontend upload control | Complete locally; image-only PDF OCR deliberately unsupported |
| Local access model | No CareerPilot account or sign-in; one stable device-local identity preserves encrypted providers and workspace state; AI connection remains mandatory | Complete for loopback-only use; public exposure prohibited without external authentication |
| Dual OpenAI billing | User-owned OpenAI key path plus ChatGPT-plan Codex app-server path; both feed the same schema and grounding workflow | Code complete; both live paths still need credentialed smoke tests |
| GPT-5.6 integration | Direct OpenAI adapter defaults to and preflight enforces `gpt-5.6-sol`; strict Chat Completions request is unit-tested | Code complete; paid live call not yet authorized |
| Structured validated output | Pydantic models reject extra/missing/invalid fields; provider-output and API error tests pass | Complete |
| Evidence-grounded analysis | Real MiniLM/Chroma path returned the relevant chunk; citations and JD quotes are verified verbatim after generation | Complete locally |
| Simple usable frontend | Responsive recoverable Settings, Pilot conversation, guided known-board discovery, persistent evidence, job queue, applications, bounded Approval records, model routes, skills, MCP connection review, and evolution history | Complete locally for the integrated screens |
| Guided known-board discovery | Loopback-only bounded Greenhouse/Lever preview, explicit network disclosure, zero default selection/write, canonical checked saves, and account/generation guards | Complete locally; intentionally unavailable through forwarding proxies or the Docker bridge |
| Agent customization | User-owned name/SOUL, editable memories, visible built-ins, four preview-before-install skill starters, bounded import/export, and allowlisted MCP configuration | Complete locally |
| Approval history | Account-scoped cursor pagination shows current effective state, exact action and SHA-256 request fingerprint, expiry, remaining one-use count, and explicit non-authorized scope; legacy or obsolete consumer markers are unusable | Complete locally; this is an audit/explanation surface, not a replay credential |
| MCP connection health and discovery | A manual short-lived review binds the exact saved stdio/HTTP configuration, then permits only initialize, initialized, and at most four `tools/list` requests; results group allowed/new/missing names and modify only the unsaved draft | Complete locally against the owned fixture; no production connector has been smoked, and native Windows containment remains a gate |
| Form-preview portability | Evidence-bound local preview uses secure POSIX directory-descriptor storage on current `main`; independently reviewed Windows storage remains isolated | POSIX complete; Windows candidate requires native CI before acceptance or integration |
| Guarded form fill | Strict normalized payload binding, current validation markers, immutable attachment buffers, frozen post-navigation networking, stable controls, final resource/hash revalidation, and atomic one-use consumption are integrated in the separate backend | Safety backend only; not exposed to Pilot, not a user-ready external-fill workflow, and never submits |
| Tailoring and interview practice | Both worktrees are preserved with known acceptance findings | Not release-ready; resumption requires explicit approval |
| Basic retrieval with citations | Fit analysis uses ephemeral Chroma; cross-session memory uses bounded deterministic weighted-token retrieval with a privacy-safe provenance inspector | Complete locally; semantic/vector long-term memory remains planned |
| Evaluation and evolution | 25 anonymized strong/partial/mismatch cases, replay thresholds, unsupported-claim checks, and replay-derived activation gates | Offline suite complete; live token/cost observations pending paid calls |
| Langfuse observability | Optional v4 SDK integration traces agent/retriever/chain/generation/guardrail and attaches release versions; remote telemetry disabled in tests | Code complete; no Langfuse keys to verify a remote trace |
| Local installation | Same-origin FastAPI/Next runtime, macOS/Linux and Windows installers, doctor, backup/restore, full Docker fallback, and loopback publishing | Native macOS installer and a credential-free backup/restore drill are verified; Windows/Linux and Docker execution remain gates |
| README and meetup materials | README, architecture notes, demo scripts, and a 13-slide HTML deck with current production UI captures exist | Documentation and local loopback captures refreshed; final recording pending |

## Verification snapshot

- Clean release-input worktree at exact integrated feature baseline `a5dae60`:
  **827 backend tests passed, 6 platform-specific skips**.
- Post-integration verification also passes all **106 approval/MCP-focused backend tests**
  and all **74 frontend unit tests**.
- ESLint, the compatible TypeScript check, and the Next.js 16.2.10 production build pass
  with routes `/`, `/discover`, `/settings`, and `/workspace`.
- The clean release-input run includes the offline wheel and source-distribution
  contracts. Those contracts deliberately reject non-example frontend dotenv files.
- Hermes 0.18.2's real plugin manager discovers and enables the profile-owned
  plugin with fifteen restricted career tools and one policy hook; its resolved API-server
  toolsets exclude terminal, file, raw memory, cron, and delegation.
- Offline evaluation: 25 unique cases and all three categories validate.
- `uv run python -m scripts.demo_preflight`: checks Python, local provider encryption secret,
  Codex runtime presence, real MiniLM/Chroma retrieval, GPT-5.6 model, and release versions.
- `npm run lint`: passes.
- `npm run build`: both standalone and same-origin static production builds pass.
- The deterministic same-origin export reproduced exactly across two clean builds with
  62 verified files, source fingerprint `bb5048ad…`, export fingerprint `cd880a98…`, and
  index SHA-256 `44340bc5…`.
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
- Approval-history and guarded-fill tests prove account/cursor isolation, computed expiry,
  exact canonical fingerprints, current consumer-version markers, conservative legacy
  records, unsafe URL/selector/resource rejection, immutable hash-bound attachment buffers,
  frozen post-navigation networking, stable control binding, final resource/hash
  revalidation, one concurrent winner, and atomic consumption/audit rollback.
- MCP connection-check tests prove manual-only exact-server approval, no background checks,
  public SDK initialize/initialized/`tools/list` use, request/time/result bounds, redirect,
  proxy, cookie, and private-target controls, process-tree cleanup, secret suppression,
  concurrency/cooldown handling, stale-configuration rejection, untrusted descriptions,
  and draft-only allowlist reconciliation. End-to-end evidence is fixture-based; no
  production connector or MCP tool call is represented as tested.
- Guided Settings tests prove six reads recover independently, preserve loaded data and
  unsaved drafts, suppress stale/unmounted completions, wrap generations safely, keep
  keyboard navigation in the stepper, and bind Codex authorization attempts to the exact
  local account.
- Guided discovery tests prove provider host pinning, redirect refusal, proxy-header and
  remote-peer rejection, an 8 MiB streaming bound, zero preview writes, zero default
  selection, sequential account-bound canonical saves, and no extra operational effects.
- Isolated Windows candidate `387f32b` passed independent local review, 793 backend tests
  with 24 platform skips on that candidate branch, and exact selection of all 18 native
  tests. Because all 18 skip on macOS, this means **ready for native CI**, not accepted on
  Windows.
- Hermes lifecycle tests prove route-selected provider isolation, encrypted Codex
  refresh, authenticated readiness, provider invalidation, shutdown, and streamed
  browser events without exposing provider or bridge credentials.
- Release security tests prove untrusted Tectonic mode, TeX/asset/link validation,
  redirect and element-semantic browser checks, no-submit form guards, atomic bounded
  backups, origin protection, generic templates, pinned manifests, and replay-derived
  evolution quarantine.
- The credential-free backup/restore drill completed from a source workspace into a fresh
  restore target. Its manifest records `includes_credentials: false`; credential and
  sentinel values were absent, while the database, workspace files, and a user skill were
  restored. Provider reconnection remains intentionally required. The three focused backup
  security tests also pass.
- `./install.sh --no-start` completes on macOS ARM64 from the source checkout. Its doctor
  launches Chromium 149, verifies the recorded browser hash, Hermes 0.18.2, Tectonic
  0.16.9, private secret permissions, the web build, and loopback configuration.
- The installed Tectonic runtime renders the generic AI/ML CV template on the first
  attempt as a valid one-page PDF with 418 extracted characters and no render errors.
- The installed Playwright Chromium exercised the separate guarded backend against a
  disposable loopback-only application form; an `oninput` auto-submit attempt produced
  zero POST requests and control returned with `submitted: false`. This is containment
  evidence, not a user-ready external-fill or submission feature.
- `uv build` produces both a wheel and source distribution. The wheel contains the three
  Python packages, generic templates, console entry point, and sanitized Hermes profile
  distribution.
- Next.js production smoke: `/`, `/discover`, `/settings`, and `/workspace` compile and serve.
- Local browser QA on the integrated production build opens without sign-in, walks the
  recoverable Settings navigator, previews a starter skill without installing it, and
  exercises deterministic guided discovery through deliberate role selection. Earlier
  queue/application/mobile checks remain recorded from the release-hardening audit.
- Credential-storage tests prove the raw API key does not appear in the SQLite file;
  `.env` and `.data` remain ignored.
- Docker image build: definition includes the full app, isolated Hermes, verified
  Tectonic, Playwright, profile, and migrations, but cannot be built here because Docker
  is not installed.
- GitHub Actions: the repository remote and a mandatory `windows-latest` native-test job
  are defined. The isolated Windows candidate has not been pushed or opened as a draft PR;
  that external mutation awaits explicit user authorization.

## External completion gates

1. Authorize a push and draft PR for the isolated Windows storage candidate; accept it only
   after the mandatory native selection reports zero skips, failures, and errors.
2. Run the native installer on clean Windows and Linux machines; macOS ARM64 is locally
   proven.
3. Complete one real ChatGPT device authorization and one plan-backed Codex analysis.
4. Approve exactly one API-key-backed `uv run python -m scripts.live_smoke` call.
5. Run MCP probe containment on native Windows and explicitly smoke one production MCP
   connector; current end-to-end discovery proof uses the deterministic fixture only.
6. Add Langfuse credentials and inspect one resulting trace, or explicitly defer remote
   tracing for the meetup build.
7. Add an authentication boundary before any public hosting, or keep the demo loopback-only.
8. Explicitly approve resuming the preserved tailoring and interview-practice worktrees,
   then close their known acceptance findings before integration.
9. Create a fresh credential-free backup, then record and review the final demo video.

No automated test submits an application, contacts an employer, or makes a paid provider
call. Until the credentialed and cross-platform gates are satisfied, release sign-off is
not represented as complete.
