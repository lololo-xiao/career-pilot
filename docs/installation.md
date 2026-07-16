# Local installation and operations

Career Companion v1 is a single-user, local-first application. The native installer
serves the browser UI and API from one loopback URL, `http://127.0.0.1:8787`. No
CareerPilot account or sign-in is required. Provider credentials remain in an encrypted
local database and never enter browser storage.

## Supported systems

| System | Native path | Notes |
| --- | --- | --- |
| macOS 13+ | `./install.sh` | Intel and Apple silicon |
| Windows 11+ | `./install.ps1` | x64; Windows ARM64 uses the official x64 Tectonic build through OS emulation |
| Mainstream Linux | `./install.sh` | x64 glibc and ARM64; Playwright may request elevation for browser libraries |
| Other Linux | Docker Compose | Loopback-published fallback |

Python 3.12+ and Node.js 20.9+ must already be installed. The scripts create isolated
runtime environments under `.runtime` for Career Companion, the locked environment
manager, and the exact Hermes dependency. They also build the same-origin web bundle,
install Playwright Chromium, and install Tectonic.

On macOS or Linux:

```bash
./install.sh
```

On Windows PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
./install.ps1
```

Pass `--no-start` on macOS/Linux or `-NoStart` on Windows to install without opening the
application. Start it later with the command printed by the installer.

## Supply-chain checks

- Hermes is isolated and pinned to `hermes-agent==0.18.2`.
- Playwright is locked to `1.61.0`, which selects a fixed Chromium revision. Setup
  records the installed browser executable's SHA-256 value; `doctor` launches the
  browser and checks that the executable still matches that integrity baseline.
- Tectonic is pinned to `0.16.9`. Native installers and Docker verify the downloaded
  archive against the official release hashes in
  `installers/tectonic-0.16.9.sha256` before extraction.
- `npm ci` and the Python lockfile provide reproducible application dependencies.

The scripts stop on a failed version, checksum, browser launch, build, or diagnostic.

## First run

1. Open the app; CareerPilot creates or reuses one stable local workspace automatically.
2. Choose either an OpenAI API key or ChatGPT/Codex authorization in Settings. This is
   required before AI features are available. API
   billing is separate from ChatGPT subscriptions; the product never silently switches
   between them.
3. Import and review the CV evidence. Uncertain claims remain unverified until the user
   confirms them.
4. Add a job, review deterministic fit evidence, and generate artifacts.

The device-local Hermes profile is installed on the first Pilot chat. General shell,
arbitrary file writes, application submission, LinkedIn automation, messaging, and
unapproved MCP tools are unavailable to the profile.

## Operations

The installed CLI provides:

```text
career-companion setup
career-companion start
career-companion doctor
career-companion backup
career-companion restore BACKUP.zip
career-companion uninstall --yes
```

`doctor` checks pinned versions, browser launch and integrity, the local web build,
loopback configuration, private secret permissions, profile installation, and SQLite
integrity. Backups contain the local operational database, workspace, configuration,
and user-owned Hermes skills. They exclude provider credentials, sessions, browser data,
runtime logs, and distribution-owned profile files. A restored workspace must reconnect its
provider.

The human-editable configuration is in the platform's Career Companion config directory.
Model routes, budgets, MCP allowlists, schedules, memory revisions, and user-owned skills
remain editable through the browser and local workspace. Product-owned policy, verified
facts, credentials, and executable source are not part of the self-evolution surface.

## Docker fallback

```bash
cp .env.example .env
docker compose up --build
```

Generate a random `CAREERPILOT_AUTH_SECRET` before starting Compose. Both published ports
bind to `127.0.0.1`. The backend image includes the full application package, pinned
Hermes environment, verified Tectonic binary, Playwright Chromium, profile distribution,
and migrations. The browser UI remains at `http://127.0.0.1:3000` in the two-container
fallback.

Because the application has no product login boundary, keep it on loopback or behind a
trusted host-level access control. Do not expose it directly to the public internet.

## Safe limitations

- Browser assistance fills approved fields and files, then removes its temporary form
  and mutating-request guard so the user can review and submit manually. It never clicks
  the final submit control.
- LinkedIn stays manual.
- Messaging gateways and integration presets ship disabled.
- Image-only PDFs require OCR outside v1.
- Real provider authorization and employer forms cannot be exercised by CI. Release
  sign-off therefore keeps those explicit, credentialed checks separate from automated
  tests.
