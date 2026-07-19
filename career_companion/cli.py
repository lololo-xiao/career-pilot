from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import sys
import webbrowser
from contextlib import suppress
from pathlib import Path

import uvicorn

from career_companion.backup import create_backup, restore_backup
from career_companion.config import ProductConfig, load_config, save_config
from career_companion.demo_workspace import DemoWorkspaceError, seed_demo_home
from career_companion.doctor import checks_json, run_checks
from career_companion.paths import CompanionPaths
from career_companion.web import frontend_build_directory

_ACCOUNT_KEY = re.compile(r"[a-f0-9]{64}")


def setup_command(args: argparse.Namespace) -> int:
    paths = CompanionPaths.discover()
    paths.create()
    config = load_config(paths) if paths.config.exists() else ProductConfig()
    updates = {}
    if args.hermes_executable:
        updates["hermes_executable"] = _resolved_executable(args.hermes_executable)
    if args.tectonic_executable:
        updates["tectonic_executable"] = _resolved_executable(args.tectonic_executable)
    if args.playwright_checksum:
        updates["playwright_chromium_sha256"] = args.playwright_checksum.casefold()
    if updates:
        config = ProductConfig.model_validate(config.model_dump() | updates)
    save_config(config, paths)
    _ensure_auth_secret(paths)
    print(f"Career Companion data: {paths.root}")
    print(f"Hermes executable: {config.hermes_executable}")
    print(f"Tectonic executable: {config.tectonic_executable}")
    if frontend_build_directory() is None:
        print("Web build: missing; run the platform installer or build frontend/out")
    else:
        print("Web build: ready")
    print("The sanitized Hermes profile will be installed separately for the signed-in account.")
    print("Choose OpenAI API key or ChatGPT/Codex subscription in browser onboarding.")
    print(f"Start URL: http://127.0.0.1:{config.server.port}")
    return 0


def start_command(args: argparse.Namespace) -> int:
    paths = CompanionPaths.discover()
    config = load_config(paths)
    host = args.host or config.server.host
    if host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_remote:
        raise SystemExit(
            "Remote binding is blocked. Pass --allow-remote for the advanced override."
        )
    port = args.port or config.server.port
    web_directory = frontend_build_directory()
    if web_directory is None and not args.api_only:
        raise SystemExit(
            "The browser build is missing. Run the platform installer, or pass --api-only for development."
        )
    origin_host = f"[{host}]" if host == "::1" else host
    origin = f"http://{origin_host}:{port}"
    _configure_runtime_environment(paths, origin, port, web_directory)
    from app.main import app

    if not args.no_open and web_directory is not None:
        webbrowser.open(origin)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        proxy_headers=False,
    )
    return 0


def doctor_command(args: argparse.Namespace) -> int:
    checks = run_checks()
    if args.json:
        print(json.dumps(checks_json(checks), indent=2))
    else:
        for check in checks:
            mark = "OK" if check.ok else "MISSING"
            suffix = " (optional)" if not check.required else ""
            print(f"[{mark}] {check.name}{suffix}: {check.detail}")
    return 0 if all(check.ok for check in checks if check.required) else 1


def backup_command(args: argparse.Namespace) -> int:
    base = CompanionPaths.discover()
    account = _select_account_paths(base, args.account_key)
    destination = (
        Path(args.output).expanduser().resolve()
        if args.output
        else base.backups / f"career-companion-{account.root.name}.zip"
    )
    result = create_backup(account, destination)
    print(result)
    return 0


def restore_command(args: argparse.Namespace) -> int:
    base = CompanionPaths.discover()
    account = _select_account_paths(base, args.account_key)
    restore_backup(
        account,
        Path(args.backup).expanduser().resolve(),
        replace=args.replace,
    )
    print("Backup restored. Provider credentials must be configured separately.")
    return 0


def demo_seed_command(args: argparse.Namespace) -> int:
    try:
        result = seed_demo_home(args.home, reset=args.reset)
    except DemoWorkspaceError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Fictional demo home: {result.home}")
    print(f"Fictional account workspace: {result.account_paths.root}")
    print(
        "Seeded "
        f"{result.profiles} profile, {result.jobs} jobs, "
        f"{result.applications} applications, and {result.conversations} conversation."
    )
    print("Provider credentials: none; no AI provider was contacted.")
    print(
        "AI connection: disconnected; configure one separately inside this demo home "
        "for live UI rehearsal."
    )
    print(f"Start this isolated installation with CAREER_COMPANION_HOME={result.home}")
    return 0


def uninstall_command(args: argparse.Namespace) -> int:
    paths = CompanionPaths.discover()
    if not args.yes:
        raise SystemExit("Refusing to uninstall without --yes. Run backup first.")
    if not args.no_backup and paths.root.exists():
        accounts = _account_paths(paths)
        if len(accounts) == 1:
            result = create_backup(
                accounts[0],
                paths.backups / f"career-companion-{accounts[0].root.name}.zip",
            )
            print(f"Backup created: {result}")
        elif accounts:
            raise SystemExit("Multiple account workspaces exist; back each one up explicitly.")
    if paths.root.exists():
        shutil.rmtree(paths.root)
    if paths.config.parent.exists() and paths.config.parent != paths.root:
        shutil.rmtree(paths.config.parent)
    print("Career Companion local data removed.")
    return 0


def _ensure_auth_secret(paths: CompanionPaths) -> str:
    paths.create()
    if paths.auth_secret.is_file():
        secret = paths.auth_secret.read_text(encoding="utf-8").strip()
        if len(secret) >= 32:
            return secret
    secret = secrets.token_urlsafe(48)
    paths.auth_secret.write_text(secret, encoding="utf-8")
    with suppress(OSError):
        paths.auth_secret.chmod(0o600)
    return secret


def _resolved_executable(value: str) -> str:
    return shutil.which(value) or str(Path(value).expanduser().resolve())


def _configure_runtime_environment(
    paths: CompanionPaths,
    origin: str,
    port: int,
    web_directory: Path | None,
) -> None:
    os.environ["CAREERPILOT_AUTH_SECRET"] = _ensure_auth_secret(paths)
    os.environ["AUTH_DATABASE_PATH"] = str(paths.auth_database)
    os.environ["CAREERPILOT_INTERNAL_API_URL"] = f"http://127.0.0.1:{port}"
    os.environ["FRONTEND_ORIGINS"] = origin
    os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")
    if web_directory is not None:
        os.environ["CAREER_COMPANION_WEB_DIR"] = str(web_directory)


def _account_paths(base: CompanionPaths) -> list[CompanionPaths]:
    account_root = base.root / "accounts"
    if not account_root.is_dir():
        return []
    return [
        CompanionPaths.at_root(path, base.config.parent)
        for path in sorted(account_root.iterdir())
        if path.is_dir() and _ACCOUNT_KEY.fullmatch(path.name)
    ]


def _select_account_paths(base: CompanionPaths, account_key: str | None) -> CompanionPaths:
    if account_key:
        if not _ACCOUNT_KEY.fullmatch(account_key):
            raise SystemExit("--account-key must be the 64-character local account key")
        return CompanionPaths.at_root(base.root / "accounts" / account_key, base.config.parent)
    accounts = _account_paths(base)
    if len(accounts) == 1:
        return accounts[0]
    if not accounts:
        raise SystemExit("Sign in once before using account backup or restore.")
    raise SystemExit("Multiple account workspaces exist; pass --account-key.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="career-companion")
    subparsers = parser.add_subparsers(dest="command", required=True)
    setup_parser = subparsers.add_parser("setup", help="Initialize the local installation")
    setup_parser.add_argument("--hermes-executable")
    setup_parser.add_argument("--tectonic-executable")
    setup_parser.add_argument("--playwright-checksum")
    setup_parser.set_defaults(handler=setup_command)
    start_parser = subparsers.add_parser("start", help="Start the loopback web application")
    start_parser.add_argument("--host")
    start_parser.add_argument("--port", type=int)
    start_parser.add_argument("--allow-remote", action="store_true")
    start_parser.add_argument("--no-open", action="store_true")
    start_parser.add_argument("--api-only", action="store_true")
    start_parser.set_defaults(handler=start_command)
    doctor_parser = subparsers.add_parser("doctor", help="Check local dependencies")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.set_defaults(handler=doctor_command)
    backup_parser = subparsers.add_parser("backup", help="Create a credential-free backup")
    backup_parser.add_argument("--output")
    backup_parser.add_argument("--account-key")
    backup_parser.set_defaults(handler=backup_command)
    restore_parser = subparsers.add_parser("restore", help="Restore a local backup")
    restore_parser.add_argument("backup")
    restore_parser.add_argument("--replace", action="store_true")
    restore_parser.add_argument("--account-key")
    restore_parser.set_defaults(handler=restore_command)
    demo_parser = subparsers.add_parser(
        "demo",
        help="Manage an isolated, explicitly fictional demo workspace",
    )
    demo_subparsers = demo_parser.add_subparsers(dest="demo_command", required=True)
    demo_seed_parser = demo_subparsers.add_parser(
        "seed",
        help="Seed credential-free fictional data without contacting an AI provider",
        description=(
            "Seed an isolated CareerPilot home with explicitly fictional meetup data. "
            "The command never reads provider credentials or contacts an AI provider. "
            "Live AI remains disconnected after seeding."
        ),
    )
    demo_seed_parser.add_argument(
        "--home",
        type=Path,
        help="Dedicated demo home (default: a sibling of the normal data home)",
    )
    demo_seed_parser.add_argument(
        "--reset",
        action="store_true",
        help="Recreate only a previously marked CareerPilot demo home",
    )
    demo_seed_parser.set_defaults(handler=demo_seed_command)
    uninstall_parser = subparsers.add_parser("uninstall", help="Remove local product data")
    uninstall_parser.add_argument("--yes", action="store_true")
    uninstall_parser.add_argument("--no-backup", action="store_true")
    uninstall_parser.set_defaults(handler=uninstall_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
