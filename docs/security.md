# Security model

Career Companion assumes job descriptions, career pages, email, MCP responses, uploaded
documents, and model output are hostile until validated. Its primary boundary is the
loopback FastAPI process, not the language model.

## Enforced boundaries

- The native server binds to loopback. Remote binding requires an explicit advanced CLI
  override. Docker publishes only to `127.0.0.1`.
- CareerPilot creates one stable local identity automatically; there is no product login
  boundary. The service must remain on loopback or behind trusted host-level access control.
  Unsafe browser methods are checked against the configured Origin and Fetch Metadata.
- Provider and optional web-search credentials are encrypted in the local store. Direct
  keys enter only the Hermes child environment; Codex authorization is written atomically
  to the isolated local profile with private permissions. Search keys are never returned
  to the browser after they are saved.
- Hermes receives a minimal environment, an authenticated local API server, and a
  workspace-specific bridge token. The browser receives none of these secrets.
- The profile exposes career tools, workspace-scoped file/terminal/code tools, curated
  session search and skills, optional web search, and allowlisted MCP tools. Raw memory
  writes, delegation, messaging, cron mutation, and general browser automation are
  excluded. The dedicated form-fill tool remains approval-bound and cannot submit.
- The local Settings UI—not retrieved content or the agent—may create, edit, and remove
  user-owned memory and skill files. Names are slug-validated, paths are derived by the
  server, symlinks are rejected, built-in skills are immutable, and edits are synchronized
  into the isolated Hermes profile before restart.
- Conversation sessions and ordered messages are stored in the account-scoped SQLite
  database and addressed with opaque UUIDs. Every Hermes conversation uses a distinct
  account-and-session key. Deleting a session cascades to its messages.
- The user-selected agent name and soul notes are stored locally and mirrored to private
  workspace files. Soul notes are appended beneath the immutable product policy and are
  explicitly unable to weaken safety, approval, truthfulness, or evidence boundaries.
- MCP servers require an exact tool allowlist before they can be enabled. Remote endpoints
  require HTTPS (except loopback HTTP), runtime-reserved environment variables cannot be
  forwarded, and the UI warns that stdio commands execute with the CareerPilot process's
  local permissions. The LinkedIn preset is disabled by default and allowlists only
  `search_jobs` and `get_job_details`.
- Approvals bind action type to an exact payload digest, expire, and can be consumed only
  once. Submission, LinkedIn apply, and employer contact can never obtain an approval.
- Tectonic runs with untrusted mode forced. TeX file/execution commands, escaping asset
  paths, missing assets/fonts/characters, corrupt PDFs, unsafe link schemes, and non-one-
  page output fail validation.
- Browser filling revalidates redirects, permits exactly one safe element per selector,
  validates file inputs and approved artifact hashes, blocks form submission and
  mutating network requests during automation, and records that submission did not occur.
- Backup archives have an allowlisted layout, size/count/compression limits, no symlinks,
  no duplicate or traversal paths, consistent SQLite snapshots, and rollback on a failed
  replacement. Secret and runtime paths are excluded.

## Evolution boundary

Preferences, corrections, workflow memory, user-owned skills, and rubrics may produce
versioned candidate revisions. Activation requires replay quality, security, and cost
checks, and every revision is attributable and reversible. Untrusted source text cannot
invoke these writes directly.

Executable code, core policy, distribution-owned skills, personality, provider
credentials, tool permissions, and verified career facts cannot self-modify.

## Release checks

CI runs the backend on Windows, macOS, and Linux, builds both web deployment modes,
validates installer syntax, installs the exact Hermes profile, runs prompt-injection and
approval tests, validates the offline evaluation set, and never contacts an employer or
makes a paid model call.

Credentialed checks remain manual: one API-key connection, one ChatGPT/Codex connection,
one disposable supported form stopped before submission, and one restore drill. These
are documented as external gates rather than represented as automated proof.
