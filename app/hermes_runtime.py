from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from app.auth import AuthStore, AuthenticatedAccount, ProviderConnection
from app.config import get_internal_api_url
from app.schemas import ProviderMethod
from career_companion.config import ProductConfig, load_config
from career_companion.database import ModelRouteRecord
from career_companion.hermes import (
    PROFILE_NAME,
    HermesSupervisor,
    install_profile_distribution,
)
from career_companion.paths import CompanionPaths
from career_companion.persistence import account_session
from career_companion.services.audit import record_audit
from career_companion.services.agent_assets import synchronize_managed_profile_assets
from career_companion.services.conversation_sessions import (
    agent_profile_digest,
    ensure_agent_profile,
    synchronize_agent_identity,
)
from career_companion.services.mcp_servers import synchronize_mcp_profile_config
from career_companion.services.model_routes import ensure_default_routes


class HermesRuntimeError(RuntimeError):
    """A Hermes runtime error safe to expose through the local API."""


class HermesRuntimeUnavailable(HermesRuntimeError):
    pass


class HermesProviderConfigurationError(HermesRuntimeError):
    pass


@dataclass
class PreparedHermesRuntime:
    account_id: str
    account_key: str
    provider: str
    model: str
    reasoning_effort: str
    token_limit: int
    credential_digest: str
    identity_digest: str
    paths: CompanionPaths
    supervisor: HermesSupervisor


SupervisorFactory = Callable[..., HermesSupervisor]
PathsFactory = Callable[[], CompanionPaths]
ConfigLoader = Callable[[CompanionPaths], ProductConfig]
ProfileInstaller = Callable[[CompanionPaths, Path, str], object]

_HERMES_TO_ACCOUNT_PROVIDER: dict[str, ProviderMethod] = {
    "openai-api": "api_key",
    "openai-codex": "codex",
}
_ACCOUNT_TO_HERMES_PROVIDER: dict[ProviderMethod, str] = {
    value: key for key, value in _HERMES_TO_ACCOUNT_PROVIDER.items()
}

# These are product-owned capability boundaries. They are applied on every launch so
# existing account-scoped profiles receive capability and safety-policy migrations.
PILOT_API_SERVER_TOOLSETS = (
    "career-web",
    "todo",
    "session_search",
    "skills",
    "clarify",
)
PILOT_DISABLED_TOOLSETS = (
    "delegation",
    "messaging",
    "browser",
    "memory",
    "cronjob",
    "web",
    "terminal",
    "file",
    "code_execution",
)


def _credential_digest(credential: bytes) -> str:
    digest_input = credential
    try:
        payload = json.loads(credential)
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    else:
        if isinstance(payload, dict) and isinstance(payload.get("tokens"), dict):
            digest_input = json.dumps(
                payload["tokens"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
    return hashlib.sha256(digest_input).hexdigest()


def _validate_codex_credentials(credential: bytes) -> dict[str, object]:
    try:
        payload = json.loads(credential)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HermesProviderConfigurationError(
            "The stored ChatGPT authorization is not valid JSON"
        ) from exc
    if not isinstance(payload, dict) or not payload:
        raise HermesProviderConfigurationError(
            "The stored ChatGPT authorization is empty"
        )
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        raise HermesProviderConfigurationError(
            "The stored ChatGPT authorization is missing tokens"
        )
    for name in ("access_token", "refresh_token"):
        token = tokens.get(name)
        if not isinstance(token, str) or not token.strip():
            raise HermesProviderConfigurationError(
                f"The stored ChatGPT authorization is missing {name}"
            )
    return payload


def _hermes_codex_auth(credential: bytes) -> bytes:
    """Translate Codex CLI auth.json into Hermes' provider-scoped store."""

    payload = _validate_codex_credentials(credential)
    provider_state: dict[str, object] = {
        "tokens": payload["tokens"],
        "auth_mode": payload.get("auth_mode") or "chatgpt",
    }
    if last_refresh := payload.get("last_refresh"):
        provider_state["last_refresh"] = last_refresh
    hermes_store = {
        "version": 1,
        "providers": {"openai-codex": provider_state},
        "active_provider": "openai-codex",
    }
    return json.dumps(hermes_store, indent=2).encode("utf-8") + b"\n"


def _codex_auth_from_hermes(credential: bytes) -> bytes:
    """Translate refreshed Hermes auth back into Codex CLI auth.json."""

    try:
        payload = json.loads(credential)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HermesProviderConfigurationError(
            "The refreshed ChatGPT authorization is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise HermesProviderConfigurationError(
            "The refreshed ChatGPT authorization is invalid"
        )
    providers = payload.get("providers")
    provider_state = (
        providers.get("openai-codex") if isinstance(providers, dict) else None
    )
    if not isinstance(provider_state, dict):
        raise HermesProviderConfigurationError(
            "The refreshed ChatGPT authorization is missing the Codex provider"
        )
    codex_store: dict[str, object] = {
        "auth_mode": provider_state.get("auth_mode") or "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": provider_state.get("tokens"),
    }
    if last_refresh := provider_state.get("last_refresh"):
        codex_store["last_refresh"] = last_refresh
    rendered = json.dumps(codex_store, indent=2).encode("utf-8") + b"\n"
    _validate_codex_credentials(rendered)
    return rendered


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(mode)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def installed_profile_directory(paths: CompanionPaths) -> Path:
    return paths.hermes_profile / "profiles" / PROFILE_NAME


def profile_distribution_directory() -> Path:
    configured = os.getenv("CAREER_COMPANION_DISTRIBUTION_PATH", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            Path(__file__).resolve().parents[1] / "agent-profile",
            Path(sys.prefix) / "share" / "career-pilot" / "agent-profile",
        ]
    )
    for candidate in candidates:
        resolved = candidate.resolve()
        if (resolved / "distribution.yaml").is_file():
            return resolved
    return candidates[0].resolve()


def synchronize_hermes_profile_assets(
    paths: CompanionPaths,
    distribution: Path,
) -> None:
    """Refresh product-owned policy and plugin code without touching user data."""

    profile = installed_profile_directory(paths)
    source_soul = distribution / "SOUL.md"
    source_plugin = distribution / "plugins" / "career-companion"
    if not source_soul.is_file() or not source_plugin.is_dir():
        raise HermesProviderConfigurationError(
            "The Career Companion Hermes policy assets are incomplete"
        )

    sources = [source_soul]
    sources.extend(
        source
        for source in sorted(source_plugin.rglob("*"))
        if source.is_file() and "__pycache__" not in source.parts
    )
    for source in sources:
        if source.is_symlink():
            raise HermesProviderConfigurationError(
                "The Career Companion Hermes policy assets are invalid"
            )
        relative = (
            Path("SOUL.md")
            if source == source_soul
            else Path("plugins")
            / "career-companion"
            / source.relative_to(source_plugin)
        )
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise HermesProviderConfigurationError(
                "The Career Companion Hermes policy assets could not be read"
            ) from exc
        _atomic_write(profile / relative, data, mode=0o644)


def synchronize_hermes_provider(
    paths: CompanionPaths,
    connection: ProviderConnection,
    *,
    model: str,
    reasoning_effort: str,
    token_limit: int,
) -> Mapping[str, str]:
    """Update only the installed profile's runtime provider configuration."""

    profile = installed_profile_directory(paths)
    config_path = profile / "config.yaml"
    if not config_path.is_file():
        raise HermesProviderConfigurationError(
            "The Career Companion Hermes profile is not installed. Run setup first."
        )

    try:
        raw_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise HermesProviderConfigurationError(
            "The installed Hermes profile configuration could not be read"
        ) from exc
    if not isinstance(raw_config, dict):
        raise HermesProviderConfigurationError(
            "The installed Hermes profile configuration is invalid"
        )

    model_config = raw_config.get("model")
    if not isinstance(model_config, dict):
        model_config = {}
        raw_config["model"] = model_config
    model_config["default"] = model
    model_config["max_tokens"] = token_limit

    agent_config = raw_config.get("agent")
    if not isinstance(agent_config, dict):
        agent_config = {}
        raw_config["agent"] = agent_config
    agent_config["tool_use_enforcement"] = "auto"
    agent_config["disabled_toolsets"] = list(PILOT_DISABLED_TOOLSETS)
    agent_config["reasoning_effort"] = reasoning_effort

    platform_toolsets = raw_config.get("platform_toolsets")
    if not isinstance(platform_toolsets, dict):
        platform_toolsets = {}
        raw_config["platform_toolsets"] = platform_toolsets
    platform_toolsets["api_server"] = list(PILOT_API_SERVER_TOOLSETS)

    # Remove stale terminal configuration from previously installed profiles. Pilot's
    # Hermes process holds the bridge credential and must not expose arbitrary local I/O.
    raw_config.pop("terminal", None)

    approvals_config = raw_config.get("approvals")
    if not isinstance(approvals_config, dict):
        approvals_config = {}
        raw_config["approvals"] = approvals_config
    approvals_config["mode"] = "manual"
    approvals_config["cron_mode"] = "deny"

    auth_path = profile / "auth.json"
    if connection.provider == "api_key":
        try:
            api_key = connection.credential.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise HermesProviderConfigurationError(
                "The stored OpenAI API key is invalid"
            ) from exc
        if not api_key:
            raise HermesProviderConfigurationError(
                "The stored OpenAI API key is empty"
            )
        model_config["provider"] = "openai-api"
        auth_path.unlink(missing_ok=True)
        provider_environment: Mapping[str, str] = {"OPENAI_API_KEY": api_key}
    elif connection.provider == "codex":
        hermes_credentials = _hermes_codex_auth(connection.credential)
        model_config["provider"] = "openai-codex"
        _atomic_write(auth_path, hermes_credentials)
        provider_environment = {}
    else:
        raise HermesProviderConfigurationError("Unsupported Hermes provider")

    rendered = yaml.safe_dump(raw_config, sort_keys=False).encode("utf-8")
    _atomic_write(config_path, rendered)
    return provider_environment


def _interactive_route(paths: CompanionPaths) -> tuple[str, str, str, int]:
    with account_session(paths) as session:
        ensure_default_routes(session)
        session.flush()
        route = session.get(ModelRouteRecord, "interactive")
        if route is None or not route.model.strip():
            raise HermesProviderConfigurationError(
                "The interactive model route is not configured"
            )
        provider = route.provider.strip()
        if provider not in _HERMES_TO_ACCOUNT_PROVIDER:
            raise HermesProviderConfigurationError(
                "The interactive route uses an unsupported provider"
            )
        return (
            route.model.strip(),
            provider,
            route.reasoning_effort.strip(),
            int(route.token_limit),
        )


def _align_missing_route_connection(
    paths: CompanionPaths,
    connection: ProviderConnection,
) -> None:
    """Migrate a route only when its configured provider is not connected."""

    provider = _ACCOUNT_TO_HERMES_PROVIDER[connection.provider]
    with account_session(paths) as session:
        ensure_default_routes(session)
        session.flush()
        route = session.get(ModelRouteRecord, "interactive")
        if route is not None and route.provider != provider:
            previous_provider = route.provider
            route.provider = provider
            record_audit(
                session,
                "model_route.provider_migrated",
                subject_type="model_route",
                subject_id="interactive",
                payload={
                    "from": previous_provider,
                    "to": provider,
                    "reason": "configured route provider is not connected",
                },
            )


class HermesRuntimeManager:
    """Own one on-demand Hermes gateway for the local single-user installation."""

    def __init__(
        self,
        *,
        paths_factory: PathsFactory = CompanionPaths.discover,
        config_loader: ConfigLoader = load_config,
        supervisor_factory: SupervisorFactory = HermesSupervisor,
        profile_installer: ProfileInstaller = install_profile_distribution,
        distribution_path: Path | None = None,
        internal_api_url: str | None = None,
    ) -> None:
        self._paths_factory = paths_factory
        self._config_loader = config_loader
        self._supervisor_factory = supervisor_factory
        self._profile_installer = profile_installer
        self._distribution_path = distribution_path or profile_distribution_directory()
        self._internal_api_url = internal_api_url or get_internal_api_url()
        self._active: PreparedHermesRuntime | None = None
        self._lock = asyncio.Lock()

    async def prepare(
        self,
        account: AuthenticatedAccount,
        store: AuthStore,
    ) -> PreparedHermesRuntime:
        if account.provider_connection is None or account.active_provider is None:
            raise HermesProviderConfigurationError(
                "Choose an AI connection in Settings before talking with Pilot"
            )

        paths = self._paths_factory().scoped_to(account.user_id)
        config = self._config_loader(paths)
        model, route_provider, reasoning_effort, token_limit = _interactive_route(paths)
        account_provider = _HERMES_TO_ACCOUNT_PROVIDER[route_provider]
        connection = store.load_provider_connection(
            account.user_id,
            cast(ProviderMethod, account_provider),
        )
        if connection is None:
            connection = account.provider_connection
            _align_missing_route_connection(paths, connection)
        digest = _credential_digest(connection.credential)
        with account_session(paths) as session:
            identity_digest = agent_profile_digest(
                ensure_agent_profile(session, paths)
            )

        async with self._lock:
            active = self._active
            if (
                active is not None
                and active.account_id == account.user_id
                and active.provider == connection.provider
                and active.model == model
                and active.reasoning_effort == reasoning_effort
                and active.token_limit == token_limit
                and active.credential_digest == digest
                and active.identity_digest == identity_digest
                and active.supervisor.is_running
            ):
                return active

            if active is not None:
                await active.supervisor.stop()
                self._active = None

            await self._ensure_profile(paths, config)

            try:
                await asyncio.to_thread(
                    synchronize_managed_profile_assets,
                    paths,
                    self._distribution_path,
                )
                with account_session(paths) as session:
                    await asyncio.to_thread(
                        synchronize_agent_identity,
                        session,
                        paths,
                        self._distribution_path,
                    )
                forwarded_mcp_environment = await asyncio.to_thread(
                    synchronize_mcp_profile_config,
                    paths,
                    self._distribution_path,
                )
            except (OSError, ValueError) as exc:
                raise HermesProviderConfigurationError(
                    "Pilot's editable agent or MCP settings could not be synchronized"
                ) from exc
            if forwarded_mcp_environment:
                config = config.model_copy(
                    update={
                        "mcp_env_allowlist": list(
                            dict.fromkeys(
                                [
                                    *config.mcp_env_allowlist,
                                    *forwarded_mcp_environment,
                                ]
                            )
                        )
                    }
                )

            provider_environment = synchronize_hermes_provider(
                paths,
                connection,
                model=model,
                reasoning_effort=reasoning_effort,
                token_limit=token_limit,
            )
            supervisor = self._supervisor_factory(
                paths,
                config,
                api_base_url=(
                    f"{self._internal_api_url}/api/internal/hermes/v1"
                ),
            )
            try:
                await supervisor.start(provider_environment)
                await self._wait_until_ready(
                    supervisor,
                    timeout=config.hermes_startup_timeout_seconds,
                )
            except Exception as exc:
                await supervisor.stop()
                if isinstance(exc, HermesRuntimeError):
                    raise
                raise HermesRuntimeUnavailable(
                    "Pilot's local Hermes runtime could not be started"
                ) from exc

            prepared = PreparedHermesRuntime(
                account_id=account.user_id,
                account_key=paths.root.name,
                provider=connection.provider,
                model=model,
                reasoning_effort=reasoning_effort,
                token_limit=token_limit,
                credential_digest=digest,
                identity_digest=identity_digest,
                paths=paths,
                supervisor=supervisor,
            )
            self._active = prepared
            return prepared

    async def _ensure_profile(
        self,
        paths: CompanionPaths,
        config: ProductConfig,
    ) -> None:
        profile = installed_profile_directory(paths)
        if (profile / "config.yaml").is_file():
            try:
                await asyncio.to_thread(
                    synchronize_hermes_profile_assets,
                    paths,
                    self._distribution_path,
                )
            except HermesProviderConfigurationError as exc:
                raise HermesRuntimeUnavailable(str(exc)) from exc
            return
        distribution = self._distribution_path
        if not (distribution / "distribution.yaml").is_file():
            raise HermesRuntimeUnavailable(
                "The sanitized Career Companion Hermes profile is missing from this installation"
            )
        paths.create()
        try:
            await asyncio.to_thread(
                self._profile_installer,
                paths,
                distribution,
                config.hermes_executable,
            )
        except Exception as exc:
            raise HermesRuntimeUnavailable(
                "Pilot could not install its device-local Hermes profile"
            ) from exc
        if not (profile / "config.yaml").is_file():
            raise HermesRuntimeUnavailable(
                "Hermes did not create the Career Companion profile"
            )

    async def _wait_until_ready(
        self,
        supervisor: HermesSupervisor,
        *,
        timeout: float,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            health = await supervisor.health()
            if health.get("available"):
                return
            if not supervisor.is_running:
                raise HermesRuntimeUnavailable(
                    "Pilot's local Hermes runtime stopped during startup"
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise HermesRuntimeUnavailable(
                    "Pilot's local Hermes runtime did not become ready in time"
                )
            await asyncio.sleep(0.1)

    async def invalidate(self, account_id: str) -> None:
        async with self._lock:
            if self._active is None or self._active.account_id != account_id:
                return
            await self._active.supervisor.stop()
            self._active = None

    async def capture_refreshed_codex_credentials(
        self,
        account_id: str,
    ) -> bytes | None:
        async with self._lock:
            active = self._active
            if (
                active is None
                or active.account_id != account_id
                or active.provider != "codex"
            ):
                return None
            auth_path = installed_profile_directory(active.paths) / "auth.json"
            try:
                credential = _codex_auth_from_hermes(auth_path.read_bytes())
            except OSError:
                return None
            digest = _credential_digest(credential)
            if digest == active.credential_digest:
                return None
            active.credential_digest = digest
            return credential

    async def close(self) -> None:
        async with self._lock:
            if self._active is not None:
                await self._active.supervisor.stop()
                self._active = None
