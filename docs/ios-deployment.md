# CareerPilot iOS deployment

CareerPilot now has a Capacitor 8 iOS target under `frontend/ios`. It reuses the verified
static Next.js export and keeps the current browser feature set, with native handling for
external authorization pages, safe areas, generated-file sharing, the status bar, app icon,
launch artwork, and Apple's privacy manifest.

## What “iOS support” means

The iOS bundle is a client, not a standalone port of the Python application. The following
components remain on the CareerPilot runtime host:

- FastAPI and the encrypted provider store;
- the account-scoped SQLite career database and uploaded source documents;
- Hermes, Codex, Tectonic, Playwright, MCP processes, and workspace files;
- model calls, public-board discovery, document parsing, rendering, approvals, and audits.

This split preserves the existing capabilities without trying to ship subprocesses or a
writable Python runtime inside an App Store bundle. The iOS app stores only the selected
server origin and temporary files created for the system share sheet.

## Current release boundary

The server still creates one automatic local identity and has no CareerPilot account login.
Never expose it directly to the public internet. Use one private runtime per user behind a
VPN/private overlay or a trusted authentication gateway, and keep the host firewall closed
to untrusted networks.

`career-companion start --allow-remote` is an advanced private-access override. It adds
`capacitor://localhost` to the exact CORS allowlist and enables known-board discovery for
that exact native origin. This is origin protection, not public authentication.

For a public consumer App Store release, first implement and review:

- real sign-in, session revocation, account recovery, and abuse controls;
- server-side tenant isolation for databases, files, credentials, processes, and budgets;
- user-facing export and complete account/data deletion;
- an operator-controlled HTTPS service, support channel, privacy policy, retention terms,
  incident response, backups, monitoring, and an App Review demo account;
- enough native/mobile-specific utility to satisfy App Review's minimum-functionality rule.

Until those gates are complete, use a development device, ad hoc distribution, or a
controlled TestFlight group rather than presenting the client as a public hosted service.

## Toolchain

Production submission currently requires:

- Node.js 22 or newer for Capacitor 8;
- Xcode 26 or newer with the iOS 26 SDK;
- an Apple Developer Program team and a unique bundle identifier;
- a private CareerPilot runtime reachable over HTTPS from the test device.

The checked-in target uses `com.careerpilot.app` as a development placeholder. Before the
first signed archive, change both `appId` in `frontend/capacitor.config.ts` and the App
target's Bundle Identifier in Xcode to an identifier owned by the publishing team. Also set
the signing team, marketing version, and build number in Xcode.

## Build and synchronize

The recommended first-publisher command from the repository root is:

```bash
./scripts/ios doctor
./scripts/ios prepare --install --verify-reproducible
./scripts/ios open
```

For the normal local simulator loop, use the combined launcher instead:

```bash
./scripts/ios simulator
```

It uses the repository's Python environment, starts or reuses the loopback API, builds and
installs the app, and launches the selected iPhone Simulator. The API-only root returns 404
by design; `/health` is the endpoint used for readiness.

On later iterations, `./scripts/ios prepare` is sufficient unless the lockfile changed.
The wrapper temporarily moves non-example frontend dotenv files into a private directory,
restores them after success or failure, and verifies that the synchronized native bundle
matches the deterministic release export. Use `./scripts/ios verify --native` to add an
unsigned Xcode Simulator compile after installing Xcode 26.

The underlying manual sequence remains `npm ci`,
`python -m scripts.static_ui_release build`, `npm run ios:sync`, and `npm run ios:open`.
The static release builder intentionally rejects a non-example `frontend/.env.local` so a
developer API URL or secret cannot leak into the native bundle.

`ios:sync` copies `frontend/out` and updates the native Swift packages. It does not rebuild
the web UI; run the verified static build first whenever frontend source changes. Capacitor's
generated `ios/App/App/public` directory is ignored by Git and by CareerPilot's static-source
fingerprint to prevent a build-ID feedback loop.

## Run on a simulator or device

1. Start the private runtime on its host. If it must listen beyond loopback, bind only with
   the advanced override and enforce network access outside the process:

   ```bash
   career-companion start --host 0.0.0.0 --allow-remote --no-open
   ```

2. Terminate TLS in a trusted gateway and restrict ingress to the intended user/device.
   Keep proxy-header trust disabled in CareerPilot.
3. In Xcode, choose an iOS 15+ simulator or a signed physical device and run the `App`
   scheme.
4. On first launch, enter only the HTTPS origin, such as
   `https://careerpilot.example.com`. For a simulator, plain HTTP is accepted only for
   `localhost` or `127.0.0.1`.
5. If the health check reports a CORS error, verify that the launched server contains
   `capacitor://localhost` in `FRONTEND_ORIGINS`. The packaged CLI adds it automatically
   when `--allow-remote` is present.
6. To switch runtimes, open Settings and choose **Change iOS server**.

Binding to `0.0.0.0` is shown only because Hermes must still reach the same process through
loopback. The host firewall or private overlay must prevent access from every untrusted
interface. Never publish port 8787 directly.

## Feature verification matrix

Before TestFlight, exercise these paths on a physical iPhone as well as a simulator:

- first-launch server validation, offline/error recovery, and server switching;
- API-key connection and ChatGPT/Codex device authorization in native Safari;
- streamed Pilot conversation, approval allow/deny, session rename/delete, and memory
  retrieval inspection;
- PDF/DOCX and CSV import from Files, photo library where applicable, and large-file errors;
- profile/project editing, known-board discovery, job selection, ranking, and application
  status changes;
- artifact generation/opening and user-skill export through the iOS share sheet;
- Settings, MCP inspection/probe, capability controls, external job links, and privacy page;
- portrait/landscape, keyboard appearance, Dynamic Type/zoom, VoiceOver labels, reduced
  motion, dark system chrome, IPv6-only networking, background/foreground, and cold launch.

Some backend capabilities depend on binaries installed on the runtime host. A successful
iOS build does not prove that Hermes, Codex, Tectonic, Chromium, or configured MCP commands
are available there; run `career-companion doctor` on that host as well.

## App Store preparation

The target includes a 1024 × 1024 opaque icon, launch artwork, and
`PrivacyInfo.xcprivacy` with the Filesystem plugin's file-timestamp reason. Before every
submission:

- run Xcode's archive validation and inspect the merged privacy report;
- publish an operator-owned privacy-policy URL and support URL, and keep the in-app notice
  consistent with the deployed service;
- complete App Store Connect privacy answers for all backend and third-party data flows,
  not only the native SDKs;
- provide Apple with a working review environment and credentials/instructions;
- test the archive through internal and external TestFlight before App Review.

The repository privacy notice describes the current self-hosted technical behavior. A
company that hosts CareerPilot must replace or supplement it with its own legal identity,
contact details, data-controller terms, retention schedule, subprocessors, and deletion
process.

The generated artwork sources, exact prompts, and regeneration notes are recorded in
`frontend/ios/BRANDING.md`.

For a novice-friendly end-to-end sequence, Apple/GitHub account steps, time estimates,
TestFlight rollout, App Review gates, and recurring releases, use the
[iOS publishing playbook](ios-publishing-playbook.md).
