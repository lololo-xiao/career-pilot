# Career Companion v1 transfer handoff

> Current access model: CareerPilot now opens one automatic device-local workspace with
> no email, Google, password, or product session login. An OpenAI API key or ChatGPT/Codex
> connection remains mandatory. The older account terminology below records the original
> transfer architecture; the stable internal identifier is retained for data migration.

## Why this exists

Career Companion v1 work was initially created in the private CV application
workspace by mistake. It was transferred to this CareerPilot repository without
copying private profiles,
applications, credentials, browser sessions, runtime databases, or local job
trackers.

The transfer was deliberately additive. CareerPilot's FastAPI application,
encrypted account/provider storage, Codex OAuth, Next.js UI, grounded matching,
and tests were preserved. The transferred subsystem is now consolidated into
those surfaces rather than running as a second product.

## Transferred source

- `career_companion/`: local-first schemas, SQLAlchemy records, approvals,
  application state machine, profile import, discovery, scoring adapter,
  artifact rendering, Playwright assistance, reversible revisions, model
  routes, the account-scoped Hermes bridge and supervisor, backup/restore,
  doctor, and CLI.
- `agent-profile/`: pinned Hermes 0.18.2 profile distribution, `SOUL.md`, core
  career skills, restricted directory plugin, disabled MCP presets, and
  disabled cron presets.
- `job_pipeline/`: deterministic scoring and CLI compatibility layer.
- `migrations/` and `alembic.ini`: initial operational database migration.
- `frontend/app/workspace/`: the persistent evidence, job, application, model,
  approval, and evolution workspace merged from the former Vite prototype. The
  duplicate `web/` application has been removed.

## Important boundaries

- Do not import anything from a developer's private application workspace. It may
  contain a real profile and application history.
- The public product must use generic templates and anonymized fixtures only.
- Keep CareerPilot's existing encrypted provider credentials and account model.
  Do not introduce the transferred subsystem's separate provider secret store.
- Keep one FastAPI application and one Next.js frontend. Mount new routers and
  services into `app/main.py`; do not run two browser products long term.
- Keep Hermes as a pinned dependency and profile/plugin extension, not a fork.
- Install Hermes from `agent-profile/requirements-hermes.txt` into its own
  isolated environment. Hermes 0.18.2 pins OpenAI 2.24, while CareerPilot uses
  OpenAI 2.45 or newer, so combining them in one resolver would downgrade or
  break the existing product runtime.
- No LinkedIn automation and no automatic final submission.
- Treat JDs, web pages, emails, and MCP output as untrusted content.

## Consolidation status

1. **Complete:** Pure domain services have policy tests on the Python 3.12 stack.
2. **Complete:** Operational persistence is separate from the encrypted account
   store and isolated by an opaque account path. Hermes storage remains untouched.
3. **Complete:** Authenticated `/api/v1` routers are mounted in `app.main` for
   profiles, jobs, applications, artifacts, approvals, schedules, revisions,
   MCP, audit, discovery, browser assistance, and model routes.
4. **Complete:** CareerPilot's API-key and Codex OAuth paths are authoritative.
   The transferred credential writer and provider-auth table were removed.
5. **Complete:** The persistent workflow is available at `/workspace` in the
   existing Next.js UI. The Vite app was removed.
6. **Complete:** The profile-owned Hermes plugin calls account-scoped services
   only through a loopback bearer bridge. Its nine tools, restricted API-server
   toolset, pre-tool policy hook, exact 0.18.2 discovery, and secret-minimized
   supervisor environment are verified.
7. **Complete:** FastAPI owns one on-demand Hermes gateway, stops it at shutdown
   or provider invalidation, resolves the editable `interactive` route, and
   synchronizes only that connected API-key or Codex credential. Pilot consumes
   structured message and tool-progress SSE without receiving either bearer
   secret. Refreshed Codex credentials return to the encrypted account store.
8. **Next:** Complete Tectonic and Playwright installers, diagnostics,
   account-aware backups, Docker, prompt-injection evaluation, anonymized JD
   evaluation, and CI.

## Current verification

- 105 backend tests pass without provider or telemetry calls.
- The shipped profile loads through Hermes 0.18.2's real plugin manager with
  nine tools and one policy hook. The resolved API-server surface excludes
  terminal, file, raw memory writes, cron mutation, and delegation.
- Lifecycle tests cover provider changes, route-selected credentials, authenticated
  readiness, Codex refresh, shutdown, remote bridge rejection, and streamed events.
- Account-isolation, one-time approval, no-submit, no-LinkedIn, verified-evidence,
  route-budget, fallback, and reversible-revision policies have direct tests.
- FastAPI exposes one authenticated application; no duplicate provider-writing
  endpoints exist under `/api/v1`.
- Alembic upgrades a fresh operational SQLite database to revision `0001`.
- Next.js lint and production build pass with `/`, `/settings`, and `/workspace`.
- Browser QA covered automatic local opening, adding and scoring a job, tracking an application,
  viewing model/memory controls, responsive width, and browser console errors.

## Known implementation decisions

- Hermes 0.18.2 stays in `agent-profile/requirements-hermes.txt` because it pins
  OpenAI 2.24 while the main app needs OpenAI 2.45 or newer.
- The career plugin ships as a distribution-owned directory plugin. This is the
  native Hermes 0.18.2 extension path and keeps the isolated Hermes environment
  free of CareerPilot's SQLAlchemy and OpenAI dependency graph.
- Hermes never opens `career.db`. Its plugin receives only an opaque account key,
  a per-account bridge token, a loopback URL, and explicitly forwarded provider
  or MCP environment variables.
- The `interactive` model route selects the Hermes provider when that connection
  exists. If an older/default route points to an unconnected provider, it is
  migrated once to the user's active connection and the change is audited.

## New-session prompt

Use the following prompt from this repository root:

> Continue implementing Career Companion v1 from the repository root on `main`.
> Read `docs/career-companion-handoff.md` first,
> inspect the clean git diff and existing CareerPilot architecture, and preserve
> all current features. The persistence, authenticated routers, provider-auth
> reuse, `/workspace`, restricted Hermes 0.18.2 bridge/plugin, account-aware
> lifecycle, route-selected provider sync, and streamed Pilot chat are complete.
> Continue with Tectonic rendering and validation, Playwright form assistance,
> installers, diagnostics, account-aware backups, Docker, prompt-injection and
> anonymized JD evaluation, and cross-platform release validation.
> Preserve the no-submit, no-LinkedIn, evidence-only-claims, explicit-approval,
> untrusted-content, cost-cap, and reversible-evolution policies. Run backend
> tests, migrations, frontend lint/build, and focused git commits. Never copy
> data from a developer's private application workspace.
