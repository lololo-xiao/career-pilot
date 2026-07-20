"""Prepare, inspect, and verify the CareerPilot iOS client.

This wrapper keeps local frontend dotenv files out of release builds, restores them
after success or failure, synchronizes the verified web export into Capacitor, and
offers a lightweight prerequisite doctor for first-time iOS publishers.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = REPOSITORY_ROOT / "frontend"
IOS_PROJECT = FRONTEND_ROOT / "ios" / "App" / "App.xcodeproj"
IOS_PUBLIC = FRONTEND_ROOT / "ios" / "App" / "App" / "public"
CAPACITOR_CONFIG = FRONTEND_ROOT / "capacitor.config.ts"
PLACEHOLDER_BUNDLE_ID = "com.careerpilot.app"
MINIMUM_NODE_MAJOR = 22
MINIMUM_XCODE_MAJOR = 26
GIB = 1024**3
FALLBACK_TOOL_DIRECTORIES = (
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    Path.home() / ".local" / "bin",
)


class IOSReleaseError(RuntimeError):
    """Raised when the local iOS workflow cannot continue safely."""


def _find_tool(name: str) -> Path | None:
    located = shutil.which(name)
    if located:
        return Path(located).absolute()
    for directory in FALLBACK_TOOL_DIRECTORIES:
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _tool_environment(tool: Path | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    directories: list[str] = []
    if tool is not None:
        directories.append(os.fspath(tool.parent))
    directories.extend(
        os.fspath(directory)
        for directory in FALLBACK_TOOL_DIRECTORIES
        if directory.is_dir()
    )
    existing = environment.get("PATH", "")
    environment["PATH"] = os.pathsep.join(
        [*dict.fromkeys(directories), existing]
    )
    return environment


def _command_result(
    arguments: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path = REPOSITORY_ROOT,
    environment: dict[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [os.fspath(argument) for argument in arguments],
        cwd=cwd,
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )


def _run_checked(
    arguments: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path = REPOSITORY_ROOT,
    environment: dict[str, str] | None = None,
) -> None:
    printable = " ".join(os.fspath(argument) for argument in arguments)
    print(f"\n$ {printable}", flush=True)
    subprocess.run(
        [os.fspath(argument) for argument in arguments],
        cwd=cwd,
        env=environment,
        check=True,
    )


def _major_version(output: str, pattern: str) -> int | None:
    match = re.search(pattern, output, flags=re.IGNORECASE | re.MULTILINE)
    return int(match.group(1)) if match else None


def _bundle_identifier() -> str | None:
    try:
        source = CAPACITOR_CONFIG.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"\bappId\s*:\s*[\"']([^\"']+)[\"']", source)
    return match.group(1) if match else None


def _report(level: str, message: str) -> None:
    print(f"{level:<9} {message}")


def doctor() -> int:
    """Report the machine, native project, and GitHub release prerequisites."""

    blockers: list[str] = []
    print("CareerPilot iOS release doctor\n")

    if platform.system() != "Darwin":
        message = "Native iOS builds require macOS."
        _report("BLOCKED", message)
        blockers.append(message)
    else:
        _report("OK", f"macOS {platform.mac_ver()[0] or 'detected'}")

    node = _find_tool("node")
    node_major: int | None = None
    if node is None:
        message = "Node.js 22+ is missing. Install it before preparing the app."
        _report("BLOCKED", message)
        blockers.append(message)
    else:
        result = _command_result([node, "--version"], environment=_tool_environment(node))
        node_major = _major_version(result.stdout, r"^v?(\d+)")
        if result.returncode or node_major is None or node_major < MINIMUM_NODE_MAJOR:
            message = f"Node.js 22+ is required; found {result.stdout.strip() or node}."
            _report("BLOCKED", message)
            blockers.append(message)
        else:
            _report("OK", f"Node.js {result.stdout.strip()} at {node}")
            if shutil.which("node") is None:
                _report(
                    "ACTION",
                    "Add Homebrew and uv to PATH in ~/.zprofile; see the publishing playbook.",
                )

    npm = _find_tool("npm")
    if npm is None:
        message = "npm is missing. Install it with Node.js."
        _report("BLOCKED", message)
        blockers.append(message)
    else:
        result = _command_result([npm, "--version"], environment=_tool_environment(npm))
        if result.returncode:
            message = "npm could not run with the installed Node.js toolchain."
            _report("BLOCKED", message)
            blockers.append(message)
        else:
            _report("OK", f"npm {result.stdout.strip()} at {npm}")

    xcodebuild = _find_tool("xcodebuild")
    xcode_major: int | None = None
    if xcodebuild is None:
        message = "Xcode 26+ is missing. Install the current Xcode from Apple."
        _report("BLOCKED", message)
        blockers.append(message)
    else:
        result = _command_result([xcodebuild, "-version"])
        xcode_major = _major_version(result.stdout, r"^Xcode\s+(\d+)")
        if result.returncode or xcode_major is None or xcode_major < MINIMUM_XCODE_MAJOR:
            found = result.stdout.splitlines()[0] if result.stdout else os.fspath(xcodebuild)
            message = f"Xcode 26+ is required for Capacitor 8; found {found}."
            _report("BLOCKED", message)
            blockers.append(message)
        else:
            _report("OK", result.stdout.splitlines()[0])

    free_gib = shutil.disk_usage(REPOSITORY_ROOT).free / GIB
    required_free_gib = 30 if xcode_major is None or xcode_major < 26 else 10
    if free_gib < required_free_gib:
        message = (
            f"Only {free_gib:.1f} GiB is free; make at least {required_free_gib} GiB "
            "available before continuing."
        )
        _report("BLOCKED", message)
        blockers.append(message)
    else:
        _report("OK", f"{free_gib:.1f} GiB free disk space")

    if IOS_PROJECT.is_dir():
        _report("OK", f"Native project exists at {IOS_PROJECT.relative_to(REPOSITORY_ROOT)}")
    else:
        message = "The checked-in Capacitor iOS project is missing."
        _report("BLOCKED", message)
        blockers.append(message)

    if (FRONTEND_ROOT / "node_modules").is_dir():
        _report("OK", "Locked frontend dependencies are installed")
    else:
        _report("ACTION", "Run the prepare command with --install to execute npm ci")

    bundle_identifier = _bundle_identifier()
    if bundle_identifier == PLACEHOLDER_BUNDLE_ID:
        _report(
            "ACTION",
            "Replace com.careerpilot.app with a bundle ID owned by your Apple team before signing.",
        )
    elif bundle_identifier:
        _report("OK", f"Configured bundle identifier: {bundle_identifier}")
    else:
        _report("ACTION", "Set appId in frontend/capacitor.config.ts before signing")

    if xcode_major is not None and xcode_major >= MINIMUM_XCODE_MAJOR:
        xcrun = _find_tool("xcrun")
        runtime_result = (
            _command_result([xcrun, "simctl", "list", "runtimes"], timeout=60)
            if xcrun is not None
            else None
        )
        if (
            runtime_result is None
            or runtime_result.returncode
            or "iOS" not in runtime_result.stdout
        ):
            message = "No usable iOS Simulator runtime was found; install one in Xcode Settings."
            _report("BLOCKED", message)
            blockers.append(message)
        else:
            _report("OK", "An iOS Simulator runtime is installed")

    git = _find_tool("git")
    if git is None:
        _report("ACTION", "Install Git before creating release branches")
    else:
        status = _command_result([git, "status", "--short", "--branch"])
        first_line = status.stdout.splitlines()[0] if status.stdout else "repository detected"
        _report("INFO", f"Git: {first_line}")

    gh = _find_tool("gh")
    if gh is None:
        _report("ACTION", "Install GitHub CLI (`brew install gh`) before publishing a PR")
    else:
        auth = _command_result(
            [gh, "auth", "status", "-h", "github.com"],
            environment=_tool_environment(gh),
        )
        if auth.returncode:
            _report("ACTION", "Refresh GitHub login with: gh auth login -h github.com")
        else:
            _report("OK", "GitHub CLI is authenticated")

    print()
    if blockers:
        print(f"Doctor found {len(blockers)} blocking prerequisite(s). Fix them and rerun this command.")
        return 1
    print("All local build prerequisites passed. Next: run the prepare command.")
    return 0


def _dotenv_candidates(frontend: Path) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for entry in frontend.iterdir():
        folded = entry.name.casefold()
        if not folded.startswith(".env") or folded.endswith(".example"):
            continue
        metadata = entry.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise IOSReleaseError(
                f"Refusing to move non-regular frontend dotenv input: {entry}"
            )
        candidates.append(entry)
    return tuple(sorted(candidates))


@contextlib.contextmanager
def _without_frontend_dotenv(frontend: Path) -> Iterator[None]:
    """Atomically hide local dotenv files and restore them on every exit path."""

    candidates = _dotenv_candidates(frontend)
    if not candidates:
        yield
        return

    backup_directory = Path(
        tempfile.mkdtemp(prefix=".careerpilot-ios-env-", dir=frontend.parent)
    )
    backup_directory.chmod(0o700)
    moved: list[tuple[Path, Path]] = []
    try:
        for source in candidates:
            destination = backup_directory / source.name
            source.replace(destination)
            moved.append((source, destination))
        print(
            "Protected local frontend dotenv file(s) during release build: "
            + ", ".join(source.name for source, _destination in moved),
            flush=True,
        )
        yield
    finally:
        conflicts = [source for source, _destination in moved if source.exists()]
        if conflicts:
            retained = ", ".join(
                os.fspath(destination) for _source, destination in moved
            )
            raise IOSReleaseError(
                "A frontend dotenv path was recreated while the release build ran. "
                f"The original file(s) were retained at: {retained}"
            )
        for source, destination in reversed(moved):
            destination.replace(source)
        backup_directory.rmdir()
        print("Restored local frontend dotenv file(s).", flush=True)


def _npm() -> Path:
    npm = _find_tool("npm")
    if npm is None:
        raise IOSReleaseError("npm was not found. Install Node.js 22+ first.")
    return npm


def _verify_synced_bundle() -> None:
    for relative in (
        ".careerpilot-build-id",
        ".careerpilot-static-ui.json",
        "index.html",
    ):
        exported = FRONTEND_ROOT / "out" / relative
        native = IOS_PUBLIC / relative
        if not exported.is_file() or not native.is_file():
            raise IOSReleaseError(
                f"The synchronized iOS bundle is missing required file: {relative}"
            )
        if exported.read_bytes() != native.read_bytes():
            raise IOSReleaseError(
                f"The iOS copy of {relative} does not match the verified web export."
            )
    print("Verified: the Capacitor bundle matches the release export.")


def prepare(*, install: bool, verify_reproducible: bool) -> int:
    npm = _npm()
    environment = _tool_environment(npm)
    if install:
        _run_checked([npm, "ci"], cwd=FRONTEND_ROOT, environment=environment)
    elif not (FRONTEND_ROOT / "node_modules").is_dir():
        raise IOSReleaseError(
            "frontend/node_modules is missing. Rerun this command with --install."
        )

    build_command: list[str | os.PathLike[str]] = [
        sys.executable,
        "-m",
        "scripts.static_ui_release",
        "build",
        "--npm",
        npm,
    ]
    if verify_reproducible:
        build_command.append("--verify-reproducible")

    with _without_frontend_dotenv(FRONTEND_ROOT):
        _run_checked(build_command, environment=environment)
        _run_checked(
            [npm, "run", "ios:sync"],
            cwd=FRONTEND_ROOT,
            environment=environment,
        )

    _verify_synced_bundle()
    plutil = _find_tool("plutil")
    if plutil is not None:
        for path in (
            FRONTEND_ROOT / "ios" / "App" / "App" / "Info.plist",
            FRONTEND_ROOT / "ios" / "App" / "App" / "PrivacyInfo.xcprivacy",
        ):
            _run_checked([plutil, "-lint", path])

    print("\niOS project prepared successfully.")
    print("Next: ./scripts/ios open")
    return 0


def verify(*, native: bool) -> int:
    npm = _npm()
    environment = _tool_environment(npm)
    with _without_frontend_dotenv(FRONTEND_ROOT):
        _run_checked(
            [sys.executable, "-m", "scripts.static_ui_release", "verify"],
            environment=environment,
        )
    _verify_synced_bundle()

    if native:
        xcodebuild = _find_tool("xcodebuild")
        if xcodebuild is None:
            raise IOSReleaseError("xcodebuild was not found. Install Xcode 26+ first.")
        _run_checked(
            [
                xcodebuild,
                "-project",
                IOS_PROJECT,
                "-scheme",
                "App",
                "-configuration",
                "Debug",
                "-sdk",
                "iphonesimulator",
                "-destination",
                "generic/platform=iOS Simulator",
                "CODE_SIGNING_ALLOWED=NO",
                "build",
            ]
        )
    print("\niOS release verification passed.")
    return 0


def open_project() -> int:
    npm = _npm()
    _run_checked(
        [npm, "run", "ios:open"],
        cwd=FRONTEND_ROOT,
        environment=_tool_environment(npm),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="Check local build, signing, Git, and GitHub prerequisites")
    prepare_parser = subparsers.add_parser(
        "prepare", help="Build the verified web export and synchronize the iOS project"
    )
    prepare_parser.add_argument(
        "--install",
        action="store_true",
        help="Run npm ci before building (recommended for a new checkout)",
    )
    prepare_parser.add_argument(
        "--verify-reproducible",
        action="store_true",
        help="Build twice and require identical release fingerprints",
    )
    verify_parser = subparsers.add_parser(
        "verify", help="Verify the release export and synchronized native copy"
    )
    verify_parser.add_argument(
        "--native",
        action="store_true",
        help="Also compile an unsigned iOS Simulator build with Xcode",
    )
    subparsers.add_parser("open", help="Open the synchronized project in Xcode")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return doctor()
        if args.command == "prepare":
            return prepare(
                install=args.install,
                verify_reproducible=args.verify_reproducible,
            )
        if args.command == "verify":
            return verify(native=args.native)
        return open_project()
    except (IOSReleaseError, OSError, subprocess.CalledProcessError) as exc:
        print(f"iOS release error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
