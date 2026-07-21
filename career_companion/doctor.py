from __future__ import annotations

import importlib.metadata
import importlib.util
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

from career_companion.config import ProductConfig, load_config
from career_companion.paths import CompanionPaths
from career_companion.playwright_integrity import chromium_launch_version, chromium_sha256
from career_companion.runtime_versions import HERMES_VERSION, TECTONIC_VERSION
from career_companion.web import frontend_build_directory


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def run_checks(paths: CompanionPaths | None = None) -> list[Check]:
    paths = paths or CompanionPaths.discover()
    try:
        config = load_config(paths)
        config_check = Check("Configuration", True, str(paths.config))
    except Exception as exc:
        config = ProductConfig()
        config_check = Check("Configuration", False, str(exc))
    checks = [
        Check("Python", sys.version_info >= (3, 12), platform.python_version()),
        config_check,
        _version_check("Hermes", config.hermes_executable, HERMES_VERSION),
        _version_check("Tectonic", config.tectonic_executable, TECTONIC_VERSION),
        _playwright_package_check(),
        _playwright_browser_check(config.playwright_chromium_sha256),
        Check(
            "Loopback binding",
            config.server.allow_remote
            or config.server.host in {"127.0.0.1", "localhost", "::1"},
            config.server.host,
        ),
        Check(
            "Browser application",
            frontend_build_directory() is not None,
            str(frontend_build_directory() or "Run the platform installer"),
        ),
        Check(
            "Local auth secret",
            _valid_private_secret(paths.auth_secret),
            str(paths.auth_secret) if paths.auth_secret.exists() else "Run career-companion setup",
        ),
        _distribution_check(),
    ]
    checks.extend(_database_checks(paths))
    checks.extend(_profile_checks(paths))
    return checks


def _version_check(name: str, executable: str, expected: str) -> Check:
    resolved = shutil.which(executable)
    if not resolved:
        return Check(name, False, f"Expected {expected}; executable not installed")
    try:
        result = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(name, False, str(exc))
    output = (result.stdout or result.stderr).strip().splitlines()
    detail = output[0] if output else resolved
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", detail)
    detected = match.group(1) if match else None
    return Check(
        name,
        result.returncode == 0 and detected == expected,
        f"{detail}; expected {expected}",
    )


def _playwright_package_check() -> Check:
    if importlib.util.find_spec("playwright") is None:
        return Check("Playwright package", False, "Install career-pilot[companion]")
    try:
        version = importlib.metadata.version("playwright")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return Check("Playwright package", True, version)


def _playwright_browser_check(expected_sha256: str | None) -> Check:
    if importlib.util.find_spec("playwright") is None:
        return Check("Playwright Chromium", False, "Python package is missing")
    try:
        executable, digest = chromium_sha256()
        browser_version = chromium_launch_version()
        checksum_ok = expected_sha256 is None or digest == expected_sha256
        return Check(
            "Playwright Chromium",
            executable.is_file() and checksum_ok,
            (
                f"Chromium {browser_version}; {executable}; sha256 {digest}"
                if checksum_ok
                else f"Checksum changed: {digest}; expected {expected_sha256}"
            ),
        )
    except Exception as exc:
        return Check("Playwright Chromium", False, str(exc))


def _valid_private_secret(path: Path) -> bool:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if len(value) < 32:
        return False
    if sys.platform != "win32" and path.stat().st_mode & 0o077:
        return False
    return True


def _distribution_check() -> Check:
    distribution = Path(__file__).resolve().parents[1] / "agent-profile" / "distribution.yaml"
    return Check(
        "Hermes profile distribution",
        distribution.is_file(),
        str(distribution),
    )


def _database_checks(paths: CompanionPaths) -> list[Check]:
    databases = []
    if paths.auth_database.is_file():
        databases.append(("Account database", paths.auth_database, False))
    account_root = paths.root / "accounts"
    if account_root.is_dir():
        databases.extend(
            (f"Career database {path.parent.name[:12]}", path, True)
            for path in sorted(account_root.glob("*/career.db"))
            if re.fullmatch(r"[a-f0-9]{64}", path.parent.name)
        )
    if not databases:
        return [Check("Local databases", True, "Created after first sign-in", required=False)]
    return [_database_check(name, database, required) for name, database, required in databases]


def _database_check(name: str, database: Path, required: bool) -> Check:
    try:
        with closing(sqlite3.connect(database)) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
        ok = result == ("ok",)
        return Check(name, ok, str(database), required=required)
    except sqlite3.DatabaseError as exc:
        return Check(name, False, str(exc), required=required)


def _profile_checks(paths: CompanionPaths) -> list[Check]:
    account_root = paths.root / "accounts"
    if not account_root.is_dir():
        return [
            Check(
                "Account Hermes profile",
                True,
                "Installed automatically on first Pilot chat",
                required=False,
            )
        ]
    accounts = [
        path
        for path in account_root.iterdir()
        if path.is_dir() and re.fullmatch(r"[a-f0-9]{64}", path.name)
    ]
    if not accounts:
        return []
    return [
        Check(
            f"Hermes profile {account.name[:12]}",
            (
                account
                / "hermes-profile"
                / "profiles"
                / "career-companion"
                / "config.yaml"
            ).is_file(),
            "Installed" if (
                account
                / "hermes-profile"
                / "profiles"
                / "career-companion"
                / "config.yaml"
            ).is_file() else "Installed automatically on first Pilot chat",
            required=False,
        )
        for account in accounts
    ]


def checks_json(checks: list[Check]) -> list[dict]:
    return [asdict(check) for check in checks]
