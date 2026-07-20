# CareerPilot iOS publishing playbook

This is the first-publisher path from a local checkout to repeatable App Store updates:

1. prepare the Mac and Apple account;
2. run CareerPilot in an iOS Simulator;
3. sign and test on a physical iPhone;
4. distribute an internal TestFlight build;
5. run a controlled external beta;
6. complete the public-service safety work and submit to App Review;
7. release future versions through the same Git, CI, TestFlight, and phased-release flow.

Do not skip directly to public distribution. The current iOS client preserves the web
features, but its backend is still a single-user local runtime without product sign-in or
tenant isolation. It is ready for simulator testing and a controlled private beta. It is
not yet safe to expose as a public multi-user service.

## 1. Accounts, software, and identifiers

You need:

- a Mac supported by the current Xcode;
- Xcode 26 or newer and an installed iOS Simulator runtime;
- Node.js 22 or newer and Python 3.12;
- an Apple Account added to Xcode;
- an iPhone for the device-only checks;
- a unique bundle identifier, such as `com.yourcompany.careerpilot`;
- an Apple Developer Program membership before using TestFlight or the App Store;
- an operator-owned HTTPS CareerPilot runtime for physical-device testing;
- public privacy-policy and support URLs before App Review.

A free Apple developer account can run a development build on personally owned devices.
[Apple Developer Program membership](https://developer.apple.com/programs/) is required
for TestFlight and App Store distribution and currently costs USD 99 per membership year.
If enrolling as an individual, the legal personal name is the seller identity. An
organization enrollment requires a legal entity and commonly a D-U-N-S record; review
[Apple's enrollment requirements](https://developer.apple.com/programs/enroll/) before
choosing the account type.

The bundle identifier is the app's permanent identity. Choose the publishing identity
before the first upload because the bundle ID cannot be changed after a build is uploaded.
The repository currently contains the development placeholder `com.careerpilot.app`.

## 2. Prepare this Mac

From the repository root, run:

```bash
./scripts/ios doctor
```

The command checks macOS, Node, npm, Xcode, disk space, the iOS target, the bundle ID, Git,
and GitHub CLI authentication. A `BLOCKED` result must be resolved before a simulator run;
an `ACTION` result can be completed at the stage named in its message.

### Shell path

If Homebrew and `uv` are installed but not found by ordinary commands, add this line to
`~/.zprofile`, then open a new Terminal window:

```bash
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
```

Confirm it with:

```bash
node --version
npm --version
uv --version
gh --version
```

The repository-local `./scripts/ios` launcher also finds the existing project environment
without relying on this shell change.

### Disk space and Xcode

Keep at least 30–40 GiB free before downloading or upgrading Xcode and an iOS runtime. The
compressed download, installed application, simulator runtime, package caches, and build
products temporarily coexist. Use **System Settings → General → Storage** to inspect large
files, and use Xcode's own Settings/Components screen to remove unused simulator runtimes.
Do not delete unknown developer or model caches merely because they are large.

Install the current Xcode from the Mac App Store or
[Apple's developer downloads](https://developer.apple.com/download/all/). Open it once,
accept the license, and install an iOS Simulator runtime under **Xcode → Settings →
Components**. If command-line tools still point to an older copy, run:

```bash
sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer
sudo xcodebuild -runFirstLaunch
xcodebuild -version
```

The expected first line is `Xcode 26.x`. Capacitor 8 requires Xcode 26, and Apple requires
uploads made after April 28, 2026 to use the iOS 26 SDK. Check
[Xcode support](https://developer.apple.com/support/xcode/) and
[Apple's submission requirements](https://developer.apple.com/app-store/submitting/) if
these versions change.

## 3. Keep Git in a known state

Read the companion [Git and GitHub workflow](git-workflow.md) before staging changes. For
this iOS release, work on a branch and merge through a pull request; keep `main` releasable.

At the start of any work session:

```bash
git status -sb
git fetch origin
git switch main
git pull --ff-only
git switch -c feature/short-description
```

Before every commit:

```bash
git diff --check
git diff
git add -- path/to/file another/path
git diff --cached
git status -sb
```

Never commit `.env`, `.env.local`, API keys, Apple signing certificates, provisioning
profiles, App Store Connect private keys, or exported user data. The iOS preparation tool
temporarily protects frontend dotenv files and restores them on every success or failure.

## 4. Prepare and run the iOS Simulator build

For ordinary local testing, one repository command now prepares the app, finds or boots
an iPhone Simulator, builds and installs the native target, starts the project-local API,
and launches CareerPilot:

```bash
./scripts/ios simulator
```

Keep that terminal open while testing; `Ctrl-C` stops a runtime started by the command.
If a healthy runtime is already listening on port 8787, the command reuses it and returns
after launching the app. For a quick repeat run after neither source nor native code has
changed, use `./scripts/ios simulator --skip-prepare --skip-build`.

The first simulator launch may still ask for the private backend URL. Enter
`http://127.0.0.1:8787` once; the native client remembers it for later launches.

From a fresh checkout, use:

```bash
./scripts/ios prepare --install --verify-reproducible
./scripts/ios open
```

On later builds, `--install` is optional unless the lockfile changed. Keep
`--verify-reproducible` for release candidates; an ordinary local iteration can use:

```bash
./scripts/ios prepare
```

The preparation command performs the following as one guarded operation:

- optionally installs the exact npm lockfile with `npm ci`;
- moves local frontend dotenv inputs to a private temporary directory;
- produces and verifies the deterministic static Next.js export;
- copies that export and native plugins into the Capacitor Xcode project;
- restores the dotenv inputs, including after a failed build;
- verifies the native copy matches the export;
- validates `Info.plist` and `PrivacyInfo.xcprivacy`.

In Xcode:

1. choose the `App` scheme;
2. choose a recent iPhone Simulator;
3. click **Run** or press `Command-R`;
4. wait for CareerPilot's first-launch server screen.

If you use Xcode directly instead of `./scripts/ios simulator`, start the project-local
runtime in another Terminal window with the private-mobile origin enabled. Using the
repository path avoids shell PATH problems:

```bash
.venv/bin/career-companion start --allow-remote --no-open --api-only
```

For a Simulator on the same Mac, enter `http://127.0.0.1:8787`. The packaged client permits
plain HTTP only for simulator loopback. Do not use this HTTP route on a physical device or
expose port 8787 to a public interface.

`--api-only` intentionally makes `http://127.0.0.1:8787/` return 404 because the native app
already contains the interface. Use `http://127.0.0.1:8787/health` to check the API; a
healthy response is `{"status":"ok"}`.

### Simulator acceptance test

Complete at least this smoke path:

- first launch accepts the runtime and recovers cleanly from an invalid URL;
- Settings can change and reconnect to the runtime;
- provider setup opens native Safari and returns sensibly;
- Pilot streaming and approval allow/deny work;
- CV/job imports work and invalid/large files show bounded errors;
- job discovery, ranking, selection, and application status updates work;
- generated artifacts open through the iOS share sheet;
- privacy and MCP/settings pages fit in portrait and landscape;
- keyboard, larger text, VoiceOver labels, dark system chrome, offline recovery, cold launch,
  and background/foreground behavior are usable.

After Xcode 26 is installed, an unsigned native compile can also be run from Terminal:

```bash
./scripts/ios verify --native
```

GitHub Actions runs the same unsigned iOS Simulator compilation on every pull request. This
detects web-to-native synchronization failures and Xcode/Swift compilation failures; it
does not sign or upload an app.

## 5. Sign and test on an iPhone

First decide the permanent bundle ID. Change `appId` in
`frontend/capacitor.config.ts`, run `./scripts/ios prepare`, then set the exact same bundle
identifier in Xcode under **App target → Signing & Capabilities**.

In Xcode:

1. open **Xcode → Settings → Accounts** and add the publishing Apple Account;
2. select the App target, open **Signing & Capabilities**, and enable automatic signing;
3. select the correct Team and confirm the unique bundle identifier;
4. connect the unlocked iPhone, trust the Mac, and enable Developer Mode if iOS requests it;
5. choose that iPhone as the run destination and press `Command-R`;
6. resolve any account or device-registration prompt in Xcode, then rerun.

The physical phone cannot use the Mac's `127.0.0.1`. Put the runtime behind a trusted HTTPS
gateway reachable only by the intended tester, preferably over a private network/overlay.
When binding CareerPilot beyond loopback, use:

```bash
career-companion start --host 0.0.0.0 --allow-remote --no-open
```

The gateway and host firewall must restrict ingress. Never port-forward 8787 to the public
internet. Enter the gateway's `https://...` origin on first launch and repeat the complete
feature matrix from [the technical iOS guide](ios-deployment.md) on the physical phone.

## 6. Create the App Store Connect record

After Developer Program enrollment is active:

1. create an explicit App ID in Certificates, Identifiers & Profiles using the final bundle
   identifier;
2. open App Store Connect and create a new iOS app record;
3. select that bundle ID, set the app name, primary language, and an internal SKU;
4. give the developer account access to the app if multiple people will publish;
5. keep automatic signing enabled unless the team has a deliberate manual-signing policy.

Apple's [new-app record guide](https://developer.apple.com/help/app-store-connect/create-an-app-record/add-a-new-app/)
lists the exact required fields. The record must exist before the first build is uploaded.

For the first release, set in Xcode:

- **Version** (`MARKETING_VERSION`) to `1.0.0`;
- **Build** (`CURRENT_PROJECT_VERSION`) to `1`.

The public version communicates the product release. The build number identifies one
upload and must increase for every new upload of that version, for example `1`, `2`, `3`.

## 7. Archive and use TestFlight first

In Xcode:

1. choose **Any iOS Device (arm64)** or the generic device destination, not a Simulator;
2. choose **Product → Archive**;
3. in Organizer, run **Validate App**;
4. choose **Distribute App → App Store Connect → Upload**;
5. wait for App Store Connect to process the build and inspect any warning;
6. answer the export-compliance questionnaire accurately for the shipped app and service.

Start with an internal TestFlight group. Add only trusted account users and run the full
physical-device matrix. Then add release notes and, if desired, submit the build for an
external beta review. Apple supports up to 100 internal testers and 10,000 external
testers; the first build supplied to external testers requires TestFlight App Review, and a
TestFlight build remains testable for up to 90 days. See
[TestFlight](https://developer.apple.com/testflight/) and the
[TestFlight overview](https://developer.apple.com/help/app-store-connect/test-a-beta-version/testflight-overview/).

For CareerPilot's current architecture, keep the beta controlled. A TestFlight link is not
a substitute for backend authentication: each tester needs an isolated private runtime or
a properly authenticated, tenant-isolated service.

## 8. Complete the public App Store gates

Before requesting public review, the service still needs engineering and operational work:

- real sign-in, session revocation, recovery, and abuse/rate controls;
- strict tenant isolation for databases, files, credentials, subprocesses, and budgets;
- account/data export and complete in-app account deletion;
- an operator-controlled HTTPS deployment, monitoring, backups, incident response, and
  retention/deletion procedures;
- a public support channel and an operator-specific privacy policy;
- a stable App Review environment with a working demo account and precise instructions;
- a mobile value proposition and interaction quality that clear Apple's minimum
  functionality rule instead of looking like a thin website wrapper.

Complete the App Store record with:

- description, keywords, categories, age rating, and copyright;
- screenshots from the final build and supported device layouts;
- privacy-policy URL and support URL;
- App Privacy answers covering Apple SDKs, your backend, providers, telemetry, and every
  third-party service;
- review contact details, demo credentials, and notes for non-obvious features;
- content rights and export-compliance answers;
- a manually chosen release option for the first production version.

Apple documents the required
[platform-version information](https://developer.apple.com/help/app-store-connect/reference/app-information/platform-version-information/)
and [App Privacy workflow](https://developer.apple.com/help/app-store-connect/manage-app-information/manage-app-privacy).
Review the [App Review Guidelines](https://developer.apple.com/app-store/review/guidelines/),
especially privacy, account deletion, accurate metadata, review access, and minimum
functionality, immediately before submission.

## 9. Submit and roll out safely

Attach the tested build to version `1.0.0`, complete every validation warning, and submit it
for App Review. Apple reports that 90% of submissions are reviewed in less than 24 hours,
but an incomplete submission, unusual service, rejection, or requested clarification can
make the elapsed time longer. Review the live status in
[App Review](https://developer.apple.com/app-store/review/).

For the first launch, use manual release so the team can verify backend health and support
coverage after approval. For later updates, consider Apple's seven-day phased release,
which progressively makes an automatic update available to 1%, 2%, 5%, 10%, 20%, 50%,
then 100% of eligible users. A phase can be paused, and users can still manually install the
update. See [release a version update in phases](https://developer.apple.com/help/app-store-connect/update-your-app/release-a-version-update-in-phases).

There is no ordinary one-click binary rollback in the App Store. Keep the previous backend
compatible, use server-side kill switches for risky service behavior, and be ready to ship
an incremented hotfix build.

## 10. Repeatable update routine

Use this sequence for every update:

1. sync `main` and create a small feature/fix branch;
2. implement the change and add tests;
3. run backend tests, frontend lint/build, and `./scripts/ios prepare`;
4. test the affected path in a Simulator and on a physical iPhone;
5. open a pull request and wait for backend, frontend, Hermes, and iOS CI checks;
6. merge only a reviewed, passing pull request;
7. increment the build number for every upload and the public version for a user-visible
   release;
8. upload to internal TestFlight and record release notes plus test evidence;
9. promote the exact tested build to external beta/App Review;
10. release manually or in phases, monitor, then create an annotated Git tag such as
    `v1.1.0` on the merged commit.

Keep the app and service protocol backward compatible while a phased rollout is active;
different App Store users can run different client versions for days or longer.

## 11. Realistic elapsed time

These are planning ranges, not Apple guarantees:

| Milestone | Hands-on work | Typical elapsed time |
|---|---:|---:|
| Free disk space, install Xcode/runtime, first doctor pass | 1–2 hours | 2–6 hours, mostly downloads |
| First successful Simulator run | 1–3 hours | Same day after the toolchain works |
| First signed iPhone run | 1–3 hours | Same day, unless account/signing needs resolution |
| Developer enrollment | About 1 hour | Same day to several days; organization verification can take longer |
| App record, signing, first upload, internal TestFlight | 2–5 hours | 1–3 days including processing and fixes |
| External beta | 2–4 hours plus testing | Allow 2–7 days for review, feedback, and a corrected build |
| Apple review of a complete release | 1–2 hours to submit | Often under 24 hours; plan 1–3 days plus rejection buffer |
| Public-ready CareerPilot service work | Several engineering weeks | Roughly 3–8+ weeks for the current architecture |
| A routine tested update after the process is established | 1–4 hours plus feature work | Usually 1–3 days through TestFlight and review |

The fastest safe target is a Simulator build after the Xcode/disk blockers are resolved.
An internal TestFlight build is the next target. The public App Store date should be set only
after the authentication, isolation, deletion, hosting, privacy, and review-environment
gates have owners and passing evidence.

## 12. Definition of done

The first release is finished only when:

- `main` contains the reviewed release commit and all required CI checks pass;
- the exact archived build passed Simulator, physical-device, and internal TestFlight tests;
- App Store Connect metadata and privacy answers match actual runtime behavior;
- the public service gates above are complete or distribution remains explicitly private;
- the production build is approved and released with monitoring/support active;
- the release commit has an annotated version tag and the next update can repeat this guide.
