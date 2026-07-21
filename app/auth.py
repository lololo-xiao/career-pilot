import base64
import hashlib
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from cryptography.fernet import Fernet, InvalidToken
from openai import OpenAI, OpenAIError

from app.config import (
    get_auth_database_path,
    get_auth_secret,
)
from app.schemas import (
    AuthUserResponse,
    IdentityMethod,
    ProviderConnectionResponse,
    ProviderMethod,
    ProviderSettingsResponse,
)


_LOCAL_IDENTITY_SUBJECT = "careerpilot-local-device"
_LOCAL_EMAIL = "local@careerpilot.invalid"
_LOCAL_DISPLAY_NAME = "Local workspace"


class AuthError(RuntimeError):
    """Base error for authentication failures safe to expose to clients."""


class AuthConfigurationError(AuthError):
    pass


class AuthCredentialError(AuthError):
    pass


@dataclass(frozen=True)
class ProviderConnection:
    provider: ProviderMethod
    credential: bytes
    provider_email: str | None = None
    plan_type: str | None = None


@dataclass(frozen=True)
class AuthenticatedAccount:
    user_id: str
    email: str
    display_name: str
    identity_method: IdentityMethod
    active_provider: ProviderMethod | None = None
    provider_connection: ProviderConnection | None = None

    def to_response(self) -> AuthUserResponse:
        connection = self.provider_connection
        return AuthUserResponse(
            id=self.user_id,
            display_name=self.display_name,
            active_provider=self.active_provider,
            plan_type=connection.plan_type if connection else None,
            provider_email=connection.provider_email if connection else None,
            provider_label=(
                "OpenAI API"
                if self.active_provider == "api_key"
                else "ChatGPT / Codex"
                if self.active_provider == "codex"
                else None
            ),
        )


class AuthStore:
    """SQLite-backed local identity and encrypted provider connections."""

    def __init__(self, path: Path, secret: str) -> None:
        if len(secret) < 32:
            raise AuthConfigurationError(
                "CAREERPILOT_AUTH_SECRET must contain at least 32 random characters"
            )
        self.path = path
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        self._cipher = Fernet(base64.urlsafe_b64encode(digest))
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back a short-lived connection, then always close it."""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS accounts (
                        id TEXT PRIMARY KEY,
                        email TEXT NOT NULL,
                        normalized_email TEXT NOT NULL UNIQUE,
                        display_name TEXT NOT NULL,
                        active_provider TEXT,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS account_identities (
                        account_id TEXT NOT NULL
                            REFERENCES accounts(id) ON DELETE CASCADE,
                        provider TEXT NOT NULL,
                        provider_subject TEXT NOT NULL,
                        password_hash TEXT,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY(provider, provider_subject),
                        UNIQUE(account_id, provider)
                    );

                    CREATE TABLE IF NOT EXISTS provider_connections (
                        account_id TEXT NOT NULL
                            REFERENCES accounts(id) ON DELETE CASCADE,
                        provider TEXT NOT NULL,
                        encrypted_credentials BLOB NOT NULL,
                        provider_email TEXT,
                        plan_type TEXT,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY(account_id, provider)
                    );

                    CREATE TABLE IF NOT EXISTS service_credentials (
                        account_id TEXT NOT NULL
                            REFERENCES accounts(id) ON DELETE CASCADE,
                        service TEXT NOT NULL,
                        encrypted_credentials BLOB NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY(account_id, service)
                    );
                    """
                )
            self._schema_ready = True

    def ensure_local_account(self) -> AuthenticatedAccount:
        """Return the device-local identity, reusing one legacy account when safe."""

        self._ensure_schema()
        now = int(time.time())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            local_identity = connection.execute(
                """
                SELECT account_id FROM account_identities
                WHERE provider = 'local' AND provider_subject = ?
                """,
                (_LOCAL_IDENTITY_SUBJECT,),
            ).fetchone()
            if local_identity is not None:
                account_id = str(local_identity["account_id"])
            else:
                accounts = connection.execute(
                    "SELECT id FROM accounts ORDER BY created_at, id LIMIT 2"
                ).fetchall()
                if len(accounts) == 1:
                    # Preserve the workspace and provider credentials of a previous
                    # single-user installation while removing its login requirement.
                    account_id = str(accounts[0]["id"])
                else:
                    account_id = str(uuid.uuid4())
                    connection.execute(
                        """
                        INSERT INTO accounts(
                            id, email, normalized_email, display_name,
                            active_provider, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                        """,
                        (
                            account_id,
                            _LOCAL_EMAIL,
                            _LOCAL_EMAIL,
                            _LOCAL_DISPLAY_NAME,
                            now,
                            now,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO account_identities(
                        account_id, provider, provider_subject, password_hash,
                        created_at, updated_at
                    ) VALUES (?, 'local', ?, NULL, ?, ?)
                    """,
                    (account_id, _LOCAL_IDENTITY_SUBJECT, now, now),
                )
        return self.load_account(account_id, "local")

    def load_account(
        self,
        account_id: str,
        identity_method: IdentityMethod,
    ) -> AuthenticatedAccount:
        self._ensure_schema()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    accounts.id,
                    accounts.email,
                    accounts.display_name,
                    accounts.active_provider,
                    provider_connections.encrypted_credentials,
                    provider_connections.provider_email,
                    provider_connections.plan_type
                FROM accounts
                LEFT JOIN provider_connections
                    ON provider_connections.account_id = accounts.id
                    AND provider_connections.provider = accounts.active_provider
                WHERE accounts.id = ?
                """,
                (account_id,),
            ).fetchone()
        if row is None:
            raise AuthCredentialError("Account no longer exists")

        active_provider = (
            str(row["active_provider"])
            if row["active_provider"] is not None
            else None
        )
        connection_record: ProviderConnection | None = None
        encrypted = row["encrypted_credentials"]
        if active_provider is not None and encrypted is not None:
            try:
                credential = self._cipher.decrypt(bytes(encrypted))
            except InvalidToken:
                self.disconnect_provider(account_id, active_provider)  # type: ignore[arg-type]
                active_provider = None
            else:
                connection_record = ProviderConnection(
                    provider=active_provider,  # type: ignore[arg-type]
                    credential=credential,
                    provider_email=(
                        str(row["provider_email"])
                        if row["provider_email"] is not None
                        else None
                    ),
                    plan_type=(
                        str(row["plan_type"])
                        if row["plan_type"] is not None
                        else None
                    ),
                )
        return AuthenticatedAccount(
            user_id=str(row["id"]),
            email=str(row["email"]),
            display_name=str(row["display_name"]),
            identity_method=identity_method,
            active_provider=active_provider,  # type: ignore[arg-type]
            provider_connection=connection_record,
        )

    def load_provider_connection(
        self,
        account_id: str,
        provider: ProviderMethod,
    ) -> ProviderConnection | None:
        """Decrypt one connected provider without changing the active selection."""

        self._ensure_schema()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT encrypted_credentials, provider_email, plan_type
                FROM provider_connections
                WHERE account_id = ? AND provider = ?
                """,
                (account_id, provider),
            ).fetchone()
        if row is None:
            return None
        try:
            credential = self._cipher.decrypt(bytes(row["encrypted_credentials"]))
        except InvalidToken:
            self.disconnect_provider(account_id, provider)
            return None
        return ProviderConnection(
            provider=provider,
            credential=credential,
            provider_email=(
                str(row["provider_email"])
                if row["provider_email"] is not None
                else None
            ),
            plan_type=(
                str(row["plan_type"])
                if row["plan_type"] is not None
                else None
            ),
        )

    def save_provider_connection(
        self,
        *,
        account_id: str,
        provider: ProviderMethod,
        credential: bytes,
        provider_email: str | None = None,
        plan_type: str | None = None,
    ) -> None:
        self._ensure_schema()
        encrypted = self._cipher.encrypt(credential)
        now = int(time.time())
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO provider_connections(
                    account_id, provider, encrypted_credentials,
                    provider_email, plan_type, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, provider) DO UPDATE SET
                    encrypted_credentials = excluded.encrypted_credentials,
                    provider_email = excluded.provider_email,
                    plan_type = excluded.plan_type,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    provider,
                    encrypted,
                    provider_email,
                    plan_type,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE accounts SET active_provider = ?, updated_at = ?
                WHERE id = ?
                """,
                (provider, now, account_id),
            )

    def select_provider(self, account_id: str, provider: ProviderMethod) -> bool:
        self._ensure_schema()
        with self._connection() as connection:
            exists = connection.execute(
                """
                SELECT 1 FROM provider_connections
                WHERE account_id = ? AND provider = ?
                """,
                (account_id, provider),
            ).fetchone()
            if exists is None:
                return False
            connection.execute(
                "UPDATE accounts SET active_provider = ?, updated_at = ? WHERE id = ?",
                (provider, int(time.time()), account_id),
            )
        return True

    def disconnect_provider(self, account_id: str, provider: ProviderMethod) -> None:
        self._ensure_schema()
        now = int(time.time())
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE accounts SET active_provider = NULL, updated_at = ?
                WHERE id = ? AND active_provider = ?
                """,
                (now, account_id, provider),
            )
            connection.execute(
                """
                DELETE FROM provider_connections
                WHERE account_id = ? AND provider = ?
                """,
                (account_id, provider),
            )

    def update_provider_credentials(
        self,
        account_id: str,
        provider: ProviderMethod,
        credential: bytes,
    ) -> None:
        self._ensure_schema()
        encrypted = self._cipher.encrypt(credential)
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE provider_connections
                SET encrypted_credentials = ?, updated_at = ?
                WHERE account_id = ? AND provider = ?
                """,
                (encrypted, int(time.time()), account_id, provider),
            )

    def save_service_credential(
        self,
        account_id: str,
        service: str,
        credential: bytes,
    ) -> None:
        """Encrypt a non-model service credential without changing the AI provider."""

        self._ensure_schema()
        encrypted = self._cipher.encrypt(credential)
        now = int(time.time())
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO service_credentials(
                    account_id, service, encrypted_credentials, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id, service) DO UPDATE SET
                    encrypted_credentials = excluded.encrypted_credentials,
                    updated_at = excluded.updated_at
                """,
                (account_id, service, encrypted, now, now),
            )

    def load_service_credential(self, account_id: str, service: str) -> bytes | None:
        """Decrypt one optional service credential for the local runtime."""

        self._ensure_schema()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT encrypted_credentials FROM service_credentials
                WHERE account_id = ? AND service = ?
                """,
                (account_id, service),
            ).fetchone()
        if row is None:
            return None
        try:
            return self._cipher.decrypt(bytes(row["encrypted_credentials"]))
        except InvalidToken:
            self.delete_service_credential(account_id, service)
            return None

    def delete_service_credential(self, account_id: str, service: str) -> None:
        self._ensure_schema()
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM service_credentials
                WHERE account_id = ? AND service = ?
                """,
                (account_id, service),
            )

    def provider_settings(self, account_id: str) -> ProviderSettingsResponse:
        self._ensure_schema()
        with self._connection() as connection:
            account = connection.execute(
                "SELECT active_provider FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            rows = connection.execute(
                """
                SELECT provider, provider_email, plan_type
                FROM provider_connections WHERE account_id = ?
                """,
                (account_id,),
            ).fetchall()
        active_provider = (
            str(account["active_provider"])
            if account is not None and account["active_provider"] is not None
            else None
        )
        connected = {str(row["provider"]): row for row in rows}
        connections = []
        for provider, label in (
            ("codex", "ChatGPT / Codex"),
            ("api_key", "OpenAI API"),
        ):
            row = connected.get(provider)
            connections.append(
                ProviderConnectionResponse(
                    provider=provider,  # type: ignore[arg-type]
                    connected=row is not None,
                    active=active_provider == provider,
                    provider_label=label,
                    provider_email=(
                        str(row["provider_email"])
                        if row is not None and row["provider_email"] is not None
                        else None
                    ),
                    plan_type=(
                        str(row["plan_type"])
                        if row is not None and row["plan_type"] is not None
                        else None
                    ),
                )
            )
        return ProviderSettingsResponse(
            active_provider=active_provider,  # type: ignore[arg-type]
            connections=connections,
        )


def validate_openai_api_key(api_key: str) -> None:
    """Verify a user-supplied key without making a billable model request."""

    if not api_key.startswith("sk-"):
        raise AuthCredentialError("Enter a valid OpenAI API key")
    try:
        client = OpenAI(api_key=api_key, timeout=15.0, max_retries=0)
        client.models.list()
    except OpenAIError as exc:
        raise AuthCredentialError(
            "OpenAI rejected this API key. Check the key and API billing status."
        ) from exc


@lru_cache(maxsize=1)
def get_auth_store() -> AuthStore:
    try:
        return AuthStore(
            get_auth_database_path(),
            get_auth_secret(),
        )
    except RuntimeError as exc:
        raise AuthConfigurationError(str(exc)) from exc
