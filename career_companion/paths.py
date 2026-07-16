from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_path, user_data_path


@dataclass(frozen=True)
class CompanionPaths:
    root: Path
    config: Path
    database: Path
    workspace: Path
    artifacts: Path
    imports: Path
    browser: Path
    hermes_profile: Path
    logs: Path
    backups: Path
    session_token: Path
    hermes_bridge_token: Path
    auth_database: Path
    auth_secret: Path

    @classmethod
    def at_root(cls, root: Path, config_root: Path | None = None) -> CompanionPaths:
        root = root.expanduser().resolve()
        config_root = (config_root or root).expanduser().resolve()
        return cls(
            root=root,
            config=config_root / "config.yaml",
            database=root / "career.db",
            workspace=root / "workspace",
            artifacts=root / "workspace" / "artifacts",
            imports=root / "workspace" / "imports",
            browser=root / "browser-profile",
            hermes_profile=root / "hermes-profile",
            logs=root / "logs",
            backups=root / "backups",
            session_token=root / ".session-token",
            hermes_bridge_token=root / ".hermes-bridge-token",
            auth_database=root / "accounts.db",
            auth_secret=root / ".auth-secret",
        )

    @classmethod
    def discover(cls) -> CompanionPaths:
        override = os.getenv("CAREER_COMPANION_HOME")
        root = (
            Path(override).expanduser().resolve()
            if override
            else user_data_path("career-companion", appauthor=False)
        )
        config_root = (
            root / "config"
            if override
            else user_config_path("career-companion", appauthor=False)
        )
        return cls.at_root(root, config_root)

    def scoped_to(self, account_id: str) -> CompanionPaths:
        """Return an opaque, traversal-safe operational workspace for one account."""

        normalized = account_id.strip()
        if not normalized:
            raise ValueError("account_id must not be empty")
        account_key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return self.at_root(self.root / "accounts" / account_key, self.config.parent)

    def create(self) -> None:
        for directory in {
            self.root,
            self.config.parent,
            self.workspace,
            self.artifacts,
            self.imports,
            self.browser,
            self.hermes_profile,
            self.logs,
            self.backups,
        }:
            directory.mkdir(parents=True, exist_ok=True)
