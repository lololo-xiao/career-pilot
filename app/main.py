import asyncio
import codecs
import json
import re
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.auth import (
    AuthCredentialError,
    AuthStore,
    AuthenticatedAccount,
    validate_openai_api_key,
)
from app.capabilities import WEB_SEARCH_SERVICE, build_capability_settings
from app.browser_security import browser_request_guard
from app.codex_runtime import (
    CodexAccountSnapshot,
    CodexOAuthManager,
    CodexProtocolError,
    CodexUnavailableError,
    get_codex_oauth_manager,
    read_codex_account_snapshot,
)
from app.companion import (
    CompanionError,
    build_hermes_run_payload,
    chat_with_companion,
    chat_with_companion_codex,
)
from app.config import get_frontend_origins
from app.dependencies import (
    get_companion_paths,
    get_current_account,
    get_local_companion_paths,
    get_store,
    require_current_account,
    require_provider_account,
)
from app.ingestion import (
    CVParseError,
    CVTooLargeError,
    UnsupportedCVTypeError,
    parse_cv_document,
)
from app.hermes_runtime import (
    HermesProviderConfigurationError,
    HermesRuntimeManager,
    HermesRuntimeUnavailable,
    profile_distribution_directory,
)
from app.matching import (
    MatchConfigurationError,
    MatchError,
    match_candidate_v2,
    match_candidate_with_codex,
)
from app.schemas import (
    AgentAccountUsage,
    AgentAssetKind,
    AgentAssetRequest,
    AgentAssetSettingsResponse,
    AgentIdentityRequest,
    AgentIdentityResponse,
    AgentCredits,
    AgentIndividualLimit,
    AgentModelOption,
    AgentRateLimits,
    AgentRateLimitWindow,
    AgentReasoningEffortOption,
    AgentSettingsRequest,
    AgentSettingsResponse,
    ApiKeyConnectionRequest,
    AuthSessionResponse,
    CodexLoginStartResponse,
    CodexLoginStatusResponse,
    CapabilitySettingsResponse,
    CompanionApprovalRequest,
    CompanionChatRequest,
    CompanionChatResponse,
    CompanionTurn,
    ConversationMessageRequest,
    ConversationMessageResponse,
    ConversationSessionContextRequest,
    ConversationSessionCreateRequest,
    ConversationSessionListResponse,
    ConversationSessionRenameRequest,
    ConversationSessionResponse,
    ConversationSessionSummaryResponse,
    EvidenceSnippet,
    MatchRequest,
    MatchResponse,
    MemoryRetrievalHistoryResponse,
    MCPServerSettingsRequest,
    MCPSettingsResponse,
    ParsedCVResponse,
    ProviderMethod,
    ProviderSelectionRequest,
    ProviderSettingsResponse,
    WebSearchConnectionRequest,
)
from app.static_frontend import mount_static_frontend
from app.workflow import match_candidate_v3
from career_companion.hermes_bridge import router as hermes_bridge_router
from career_companion.database import MCPServerRecord, ModelRouteRecord
from career_companion.paths import CompanionPaths
from career_companion.persistence import account_session
from career_companion.router import router as career_companion_router
from career_companion.schemas import ModelRoute
from career_companion.services.model_routes import ensure_default_routes, upsert_route
from career_companion.services.agent_assets import (
    delete_agent_asset,
    list_agent_assets,
    save_agent_asset,
    synchronize_managed_profile_assets,
)
from career_companion.services.conversation_sessions import (
    activate_conversation_session,
    agent_profile_json,
    append_message,
    conversation_turns,
    create_conversation_session,
    delete_conversation_session,
    ensure_active_session,
    ensure_agent_profile,
    get_conversation_session,
    message_json,
    rename_conversation_session,
    reserve_conversation_write,
    session_json,
    session_list_json,
    session_summary_json,
    update_agent_profile,
    update_session_context,
)
from career_companion.services.mcp_servers import (
    delete_mcp_server,
    ensure_default_mcp_servers,
    list_mcp_servers,
    synchronize_mcp_profile_config_from_session,
    upsert_mcp_server,
)
from career_companion.services.memory_context import (
    MAX_RETRIEVAL_HISTORY_LIMIT,
    MAX_RETRIEVAL_HISTORY_OFFSET,
    audit_memory_context_resolution,
    build_active_memory_context,
    list_memory_context_resolutions,
)
from career_companion.web import frontend_build_directory


_hermes_runtime_manager = HermesRuntimeManager()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await _hermes_runtime_manager.close()


app = FastAPI(title="CareerPilot API", version="1.0.0", lifespan=lifespan)
_frontend_origins = get_frontend_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_frontend_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)
app.middleware("http")(browser_request_guard(set(_frontend_origins)))
app.include_router(career_companion_router)
app.include_router(hermes_bridge_router)

MatchFunction = Callable[[MatchRequest], MatchResponse]
CompanionFunction = Callable[[CompanionChatRequest], CompanionChatResponse]
ApiKeyValidator = Callable[[str], None]


def get_api_key_validator() -> ApiKeyValidator:
    return validate_openai_api_key


def get_oauth_manager() -> CodexOAuthManager:
    return get_codex_oauth_manager()


def get_hermes_runtime_manager() -> HermesRuntimeManager:
    return _hermes_runtime_manager


def _prevent_auth_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}


def _rate_limit_window(payload: object) -> AgentRateLimitWindow | None:
    if not isinstance(payload, dict):
        return None
    used_percent = payload.get("usedPercent")
    if not isinstance(used_percent, int):
        return None
    resets_at = payload.get("resetsAt")
    duration = payload.get("windowDurationMins")
    return AgentRateLimitWindow(
        used_percent=max(0, used_percent),
        resets_at=resets_at if isinstance(resets_at, int) else None,
        window_duration_minutes=(duration if isinstance(duration, int) and duration > 0 else None),
    )


def _normalize_rate_limits(payload: dict[str, object] | None) -> AgentRateLimits | None:
    snapshot = payload.get("rateLimits") if isinstance(payload, dict) else None
    if not isinstance(snapshot, dict):
        return None
    individual_payload = snapshot.get("individualLimit")
    individual = None
    if isinstance(individual_payload, dict):
        limit = individual_payload.get("limit")
        used = individual_payload.get("used")
        remaining = individual_payload.get("remainingPercent")
        resets_at = individual_payload.get("resetsAt")
        if (
            isinstance(limit, str)
            and isinstance(used, str)
            and isinstance(remaining, int)
            and isinstance(resets_at, int)
        ):
            individual = AgentIndividualLimit(
                limit=limit,
                used=used,
                remaining_percent=max(0, remaining),
                resets_at=resets_at,
            )
    credits_payload = snapshot.get("credits")
    credits = None
    if isinstance(credits_payload, dict):
        has_credits = credits_payload.get("hasCredits")
        unlimited = credits_payload.get("unlimited")
        balance = credits_payload.get("balance")
        if isinstance(has_credits, bool) and isinstance(unlimited, bool):
            credits = AgentCredits(
                has_credits=has_credits,
                unlimited=unlimited,
                balance=balance if isinstance(balance, str) else None,
            )
    return AgentRateLimits(
        plan_type=(str(snapshot["planType"]) if snapshot.get("planType") else None),
        limit_name=(str(snapshot["limitName"]) if snapshot.get("limitName") else None),
        reached_type=(
            str(snapshot["rateLimitReachedType"])
            if snapshot.get("rateLimitReachedType")
            else None
        ),
        primary=_rate_limit_window(snapshot.get("primary")),
        secondary=_rate_limit_window(snapshot.get("secondary")),
        individual=individual,
        credits=credits,
    )


def _normalize_account_usage(payload: dict[str, object] | None) -> AgentAccountUsage | None:
    summary = payload.get("summary") if isinstance(payload, dict) else None
    if not isinstance(summary, dict):
        return None

    def optional_nonnegative(name: str) -> int | None:
        value = summary.get(name)
        return value if isinstance(value, int) and value >= 0 else None

    return AgentAccountUsage(
        lifetime_tokens=optional_nonnegative("lifetimeTokens"),
        peak_daily_tokens=optional_nonnegative("peakDailyTokens"),
        current_streak_days=optional_nonnegative("currentStreakDays"),
    )


def _normalize_models(
    snapshot: CodexAccountSnapshot,
    *,
    current_model: str,
    current_effort: str,
) -> list[AgentModelOption]:
    models: list[AgentModelOption] = []
    for item in snapshot.models:
        model = item.get("model")
        if not isinstance(model, str) or not model.strip():
            continue
        effort_options: list[AgentReasoningEffortOption] = []
        raw_efforts = item.get("supportedReasoningEfforts")
        if isinstance(raw_efforts, list):
            for raw_effort in raw_efforts:
                if not isinstance(raw_effort, dict):
                    continue
                effort = raw_effort.get("reasoningEffort")
                if not isinstance(effort, str) or effort not in _REASONING_EFFORTS:
                    continue
                description = raw_effort.get("description")
                effort_options.append(
                    AgentReasoningEffortOption(
                        reasoning_effort=effort,  # type: ignore[arg-type]
                        description=description if isinstance(description, str) else "",
                    )
                )
        default_effort = item.get("defaultReasoningEffort")
        if not isinstance(default_effort, str) or default_effort not in _REASONING_EFFORTS:
            default_effort = effort_options[0].reasoning_effort if effort_options else "medium"
        if not effort_options:
            effort_options = [
                AgentReasoningEffortOption(
                    reasoning_effort=default_effort,  # type: ignore[arg-type]
                    description="",
                )
            ]
        models.append(
            AgentModelOption(
                model=model,
                display_name=(
                    str(item["displayName"]) if item.get("displayName") else model
                ),
                description=(
                    str(item["description"]) if item.get("description") else ""
                ),
                is_default=bool(item.get("isDefault")),
                default_reasoning_effort=default_effort,  # type: ignore[arg-type]
                supported_reasoning_efforts=effort_options,
                context_window=snapshot.context_windows.get(model),
            )
        )
    if current_model not in {model.model for model in models}:
        models.append(
            AgentModelOption(
                model=current_model,
                display_name=current_model,
                default_reasoning_effort=current_effort,  # type: ignore[arg-type]
                supported_reasoning_efforts=[
                    AgentReasoningEffortOption(
                        reasoning_effort=current_effort,  # type: ignore[arg-type]
                        description="Current configured effort",
                    )
                ],
                context_window=snapshot.context_windows.get(current_model),
            )
        )
    return models


async def _agent_settings_response(
    account: AuthenticatedAccount,
    store: AuthStore,
    paths: CompanionPaths,
) -> AgentSettingsResponse:
    with account_session(paths) as session:
        ensure_default_routes(session)
        session.flush()
        route = session.get(ModelRouteRecord, "interactive")
        if route is None:
            raise HTTPException(status_code=500, detail="Pilot's model route is missing")
        provider = str(route.provider)
        model = str(route.model)
        reasoning_effort = str(route.reasoning_effort)
        token_limit = int(route.token_limit)

    models: list[AgentModelOption] = []
    rate_limits = None
    account_usage = None
    warnings: list[str] = []
    if provider == "openai-codex":
        connection = store.load_provider_connection(account.user_id, "codex")
        if connection is None:
            warnings.append("Connect ChatGPT / Codex to load its models and usage limits")
        else:
            try:
                snapshot = await asyncio.to_thread(
                    read_codex_account_snapshot, connection.credential
                )
            except CodexUnavailableError as exc:
                warnings.append(str(exc))
            except CodexProtocolError as exc:
                warnings.append(f"Codex account status is unavailable: {exc}")
            else:
                models = _normalize_models(
                    snapshot,
                    current_model=model,
                    current_effort=reasoning_effort,
                )
                rate_limits = _normalize_rate_limits(snapshot.rate_limits)
                account_usage = _normalize_account_usage(snapshot.account_usage)
                warnings.extend(snapshot.warnings)
                if snapshot.refreshed_credentials != connection.credential:
                    store.update_provider_credentials(
                        account.user_id, "codex", snapshot.refreshed_credentials
                    )
    if not models:
        models = [
            AgentModelOption(
                model=model,
                display_name=model,
                default_reasoning_effort=reasoning_effort,  # type: ignore[arg-type]
                supported_reasoning_efforts=[
                    AgentReasoningEffortOption(
                        reasoning_effort=effort,  # type: ignore[arg-type]
                        description="",
                    )
                    for effort in ("minimal", "low", "medium", "high", "xhigh")
                ],
            )
        ]
    return AgentSettingsResponse(
        provider=provider,  # type: ignore[arg-type]
        model=model,
        reasoning_effort=reasoning_effort,  # type: ignore[arg-type]
        token_limit=token_limit,
        models=models,
        rate_limits=rate_limits,
        account_usage=account_usage,
        warnings=warnings,
    )


def _owned_codex_attempt(
    manager: CodexOAuthManager,
    attempt_id: str,
    user_id: str,
):
    attempt = manager.get(attempt_id)
    if attempt is None or attempt.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="ChatGPT connection attempt not found",
        )
    return attempt


def get_matcher(
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    store: Annotated[AuthStore, Depends(get_store)],
) -> MatchFunction:
    """Resolve matching from the account's active OpenAI connection."""

    connection = account.provider_connection
    if connection is None or account.active_provider is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Choose an AI connection in Settings before running a match",
        )

    if connection.provider == "api_key":
        api_key = connection.credential.decode("utf-8")

        def generate_with_api_key(
            request: MatchRequest,
            evidence: Sequence[EvidenceSnippet],
        ) -> MatchResponse:
            return match_candidate_v2(request, api_key=api_key, evidence=evidence)

        return lambda request: match_candidate_v3(
            request,
            generator=generate_with_api_key,
        )

    def generate_with_codex(
        request: MatchRequest,
        evidence: Sequence[EvidenceSnippet],
    ) -> MatchResponse:
        report, refreshed_credentials = match_candidate_with_codex(
            request,
            credentials=connection.credential,
            evidence=evidence,
        )
        store.update_provider_credentials(
            account.user_id,
            "codex",
            refreshed_credentials,
        )
        return report

    return lambda request: match_candidate_v3(
        request,
        generator=generate_with_codex,
    )


def get_companion(
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    store: Annotated[AuthStore, Depends(get_store)],
) -> CompanionFunction:
    """Resolve Pilot chat from the account's active OpenAI connection."""

    connection = account.provider_connection
    if connection is None or account.active_provider is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Choose an AI connection in Settings before talking with Pilot",
        )

    if connection.provider == "api_key":
        api_key = connection.credential.decode("utf-8")
        return lambda request: chat_with_companion(request, api_key=api_key)

    def generate_with_codex(
        request: CompanionChatRequest,
    ) -> CompanionChatResponse:
        reply, refreshed_credentials = chat_with_companion_codex(
            request,
            credentials=connection.credential,
        )
        store.update_provider_credentials(
            account.user_id,
            "codex",
            refreshed_credentials,
        )
        return reply

    return generate_with_codex


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/local/session", response_model=AuthSessionResponse)
def read_local_session(
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(get_current_account)],
) -> AuthSessionResponse:
    _prevent_auth_caching(response)
    return AuthSessionResponse(
        authenticated=True,
        user=account.to_response(),
    )


@app.get("/settings/agent-identity", response_model=AgentIdentityResponse)
def read_agent_identity(
    response: Response,
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
) -> AgentIdentityResponse:
    with account_session(paths) as session:
        profile = ensure_agent_profile(session, paths)
        payload = agent_profile_json(
            profile,
            paths,
            profile_distribution_directory(),
        )
    _prevent_auth_caching(response)
    return AgentIdentityResponse.model_validate(payload)


@app.put("/settings/agent-identity", response_model=AgentIdentityResponse)
async def save_agent_identity(
    request: AgentIdentityRequest,
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
    runtime: Annotated[HermesRuntimeManager, Depends(get_hermes_runtime_manager)],
) -> AgentIdentityResponse:
    try:
        with account_session(paths) as session:
            profile = update_agent_profile(
                session,
                paths,
                profile_distribution_directory(),
                name=request.name,
                soul=request.soul,
            )
            payload = agent_profile_json(
                profile,
                paths,
                profile_distribution_directory(),
            )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await runtime.invalidate(account.user_id)
    _prevent_auth_caching(response)
    return AgentIdentityResponse.model_validate(payload)


@app.get(
    "/companion/sessions",
    response_model=ConversationSessionListResponse,
)
def list_companion_sessions(
    response: Response,
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
) -> ConversationSessionListResponse:
    with account_session(paths) as session:
        payload = session_list_json(session, paths)
    _prevent_auth_caching(response)
    return ConversationSessionListResponse.model_validate(payload)


@app.post(
    "/companion/sessions",
    response_model=ConversationSessionResponse,
)
def create_companion_session(
    request: ConversationSessionCreateRequest,
    response: Response,
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
) -> ConversationSessionResponse:
    try:
        with account_session(paths) as session:
            conversation = create_conversation_session(
                session,
                paths,
                title=request.title,
            )
            payload = session_json(conversation)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _prevent_auth_caching(response)
    return ConversationSessionResponse.model_validate(payload)


@app.get(
    "/companion/sessions/{session_id}",
    response_model=ConversationSessionResponse,
)
def read_companion_session(
    session_id: str,
    response: Response,
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
) -> ConversationSessionResponse:
    try:
        with account_session(paths) as session:
            conversation = activate_conversation_session(
                session,
                paths,
                session_id,
            )
            payload = session_json(conversation)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _prevent_auth_caching(response)
    return ConversationSessionResponse.model_validate(payload)


@app.get(
    "/companion/sessions/{session_id}/memory-retrievals",
    response_model=MemoryRetrievalHistoryResponse,
)
def read_companion_memory_retrievals(
    session_id: str,
    response: Response,
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_RETRIEVAL_HISTORY_LIMIT),
    ] = 5,
    offset: Annotated[
        int,
        Query(ge=0, le=MAX_RETRIEVAL_HISTORY_OFFSET),
    ] = 0,
) -> MemoryRetrievalHistoryResponse:
    try:
        with account_session(paths) as session:
            payload = list_memory_context_resolutions(
                session,
                conversation_id=session_id,
                limit=limit,
                offset=offset,
            )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _prevent_auth_caching(response)
    return MemoryRetrievalHistoryResponse.model_validate(payload)


@app.put(
    "/companion/sessions/{session_id}",
    response_model=ConversationSessionSummaryResponse,
)
def rename_companion_session(
    session_id: str,
    request: ConversationSessionRenameRequest,
    response: Response,
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
) -> ConversationSessionSummaryResponse:
    try:
        with account_session(paths) as session:
            conversation = rename_conversation_session(
                session,
                session_id,
                request.title,
            )
            payload = session_summary_json(conversation)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _prevent_auth_caching(response)
    return ConversationSessionSummaryResponse.model_validate(payload)


@app.delete(
    "/companion/sessions/{session_id}",
    response_model=ConversationSessionListResponse,
)
def remove_companion_session(
    session_id: str,
    response: Response,
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
) -> ConversationSessionListResponse:
    try:
        with account_session(paths) as session:
            delete_conversation_session(session, paths, session_id)
            payload = session_list_json(session, paths)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _prevent_auth_caching(response)
    return ConversationSessionListResponse.model_validate(payload)


@app.post(
    "/companion/sessions/{session_id}/messages",
    response_model=ConversationMessageResponse,
)
def add_companion_session_message(
    session_id: str,
    request: ConversationMessageRequest,
    response: Response,
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
) -> ConversationMessageResponse:
    try:
        with account_session(paths) as session:
            reserve_conversation_write(session)
            conversation = get_conversation_session(session, session_id)
            message = append_message(
                session,
                conversation,
                role=request.role,
                content=request.content,
                report=(request.report.model_dump(mode="json") if request.report else None),
            )
            payload = message_json(message)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _prevent_auth_caching(response)
    return ConversationMessageResponse.model_validate(payload)


@app.put(
    "/companion/sessions/{session_id}/context",
    response_model=ConversationSessionResponse,
)
def save_companion_session_context(
    session_id: str,
    request: ConversationSessionContextRequest,
    response: Response,
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
) -> ConversationSessionResponse:
    try:
        with account_session(paths) as session:
            conversation = get_conversation_session(session, session_id)
            update_session_context(
                session,
                conversation,
                candidate_profile=request.candidate_profile,
                job_description=request.job_description,
                uploaded_filename=request.uploaded_filename,
                match_report=(
                    request.match_report.model_dump(mode="json")
                    if request.match_report
                    else None
                ),
            )
            payload = session_json(conversation)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _prevent_auth_caching(response)
    return ConversationSessionResponse.model_validate(payload)


@app.get("/settings/providers", response_model=ProviderSettingsResponse)
def read_provider_settings(
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    store: Annotated[AuthStore, Depends(get_store)],
) -> ProviderSettingsResponse:
    _prevent_auth_caching(response)
    return store.provider_settings(account.user_id)


@app.get("/settings/capabilities", response_model=CapabilitySettingsResponse)
def read_capability_settings(
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    store: Annotated[AuthStore, Depends(get_store)],
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
) -> CapabilitySettingsResponse:
    _prevent_auth_caching(response)
    return build_capability_settings(account, store, paths)


@app.put("/settings/capabilities/web-search", response_model=CapabilitySettingsResponse)
async def connect_web_search(
    request: WebSearchConnectionRequest,
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    store: Annotated[AuthStore, Depends(get_store)],
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
    runtime: Annotated[
        HermesRuntimeManager,
        Depends(get_hermes_runtime_manager),
    ],
) -> CapabilitySettingsResponse:
    store.save_service_credential(
        account.user_id,
        WEB_SEARCH_SERVICE,
        request.api_key.encode("utf-8"),
    )
    await runtime.invalidate(account.user_id)
    _prevent_auth_caching(response)
    return build_capability_settings(account, store, paths)


@app.delete(
    "/settings/capabilities/web-search",
    response_model=CapabilitySettingsResponse,
)
async def disconnect_web_search(
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    store: Annotated[AuthStore, Depends(get_store)],
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
    runtime: Annotated[
        HermesRuntimeManager,
        Depends(get_hermes_runtime_manager),
    ],
) -> CapabilitySettingsResponse:
    store.delete_service_credential(account.user_id, WEB_SEARCH_SERVICE)
    await runtime.invalidate(account.user_id)
    _prevent_auth_caching(response)
    return build_capability_settings(account, store, paths)


def _agent_asset_settings(paths: CompanionPaths) -> AgentAssetSettingsResponse:
    payload = list_agent_assets(paths, profile_distribution_directory())
    return AgentAssetSettingsResponse.model_validate(payload)


def _mcp_settings(paths: CompanionPaths) -> MCPSettingsResponse:
    distribution = profile_distribution_directory()
    with account_session(paths) as session:
        servers = list_mcp_servers(session, distribution)
    return MCPSettingsResponse.model_validate({"servers": servers})


@app.get(
    "/settings/agent-resources",
    response_model=AgentAssetSettingsResponse,
)
def read_agent_resources(
    response: Response,
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
) -> AgentAssetSettingsResponse:
    _prevent_auth_caching(response)
    return _agent_asset_settings(paths)


@app.post(
    "/settings/agent-resources/{kind}",
    response_model=AgentAssetSettingsResponse,
)
async def create_agent_resource(
    kind: AgentAssetKind,
    request: AgentAssetRequest,
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
    runtime: Annotated[HermesRuntimeManager, Depends(get_hermes_runtime_manager)],
) -> AgentAssetSettingsResponse:
    distribution = profile_distribution_directory()
    try:
        save_agent_asset(
            paths,
            distribution,
            kind=kind,
            name=request.name,
            content=request.content,
        )
        synchronize_managed_profile_assets(paths, distribution)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await runtime.invalidate(account.user_id)
    _prevent_auth_caching(response)
    return _agent_asset_settings(paths)


@app.put(
    "/settings/agent-resources/{kind}/{name}",
    response_model=AgentAssetSettingsResponse,
)
async def update_agent_resource(
    kind: AgentAssetKind,
    name: str,
    request: AgentAssetRequest,
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
    runtime: Annotated[HermesRuntimeManager, Depends(get_hermes_runtime_manager)],
) -> AgentAssetSettingsResponse:
    distribution = profile_distribution_directory()
    try:
        save_agent_asset(
            paths,
            distribution,
            kind=kind,
            name=request.name,
            content=request.content,
            previous_name=name,
        )
        synchronize_managed_profile_assets(paths, distribution)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await runtime.invalidate(account.user_id)
    _prevent_auth_caching(response)
    return _agent_asset_settings(paths)


@app.delete(
    "/settings/agent-resources/{kind}/{name}",
    response_model=AgentAssetSettingsResponse,
)
async def remove_agent_resource(
    kind: AgentAssetKind,
    name: str,
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
    runtime: Annotated[HermesRuntimeManager, Depends(get_hermes_runtime_manager)],
) -> AgentAssetSettingsResponse:
    distribution = profile_distribution_directory()
    try:
        delete_agent_asset(paths, distribution, kind=kind, name=name)
        synchronize_managed_profile_assets(paths, distribution)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await runtime.invalidate(account.user_id)
    _prevent_auth_caching(response)
    return _agent_asset_settings(paths)


@app.get("/settings/mcp", response_model=MCPSettingsResponse)
def read_mcp_settings(
    response: Response,
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
) -> MCPSettingsResponse:
    _prevent_auth_caching(response)
    return _mcp_settings(paths)


async def _apply_mcp_reconfiguration(
    *,
    account: AuthenticatedAccount,
    paths: CompanionPaths,
    runtime: HermesRuntimeManager,
    mutate: Callable[..., None],
) -> None:
    distribution = profile_distribution_directory()
    change_version = str(uuid.uuid4())

    def mutate_and_synchronize() -> None:
        with account_session(paths) as session:
            ensure_default_mcp_servers(session, distribution)
            mutate(session, change_version)
            synchronize_mcp_profile_config_from_session(
                session,
                paths,
                distribution,
            )

    await runtime.reconfigure(
        account.user_id,
        paths,
        mutate_and_synchronize,
    )


@app.post("/settings/mcp", response_model=MCPSettingsResponse)
async def create_mcp_setting(
    request: MCPServerSettingsRequest,
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
    runtime: Annotated[HermesRuntimeManager, Depends(get_hermes_runtime_manager)],
) -> MCPSettingsResponse:
    try:
        def create(session, change_version: str) -> None:
            if session.get(MCPServerRecord, request.name) is not None:
                raise ValueError("An MCP server with that name already exists")
            upsert_mcp_server(
                session,
                request.model_dump(mode="json"),
                change_version=change_version,
            )

        await _apply_mcp_reconfiguration(
            account=account,
            paths=paths,
            runtime=runtime,
            mutate=create,
        )
    except HermesRuntimeUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _prevent_auth_caching(response)
    return _mcp_settings(paths)


@app.put("/settings/mcp/{name}", response_model=MCPSettingsResponse)
async def update_mcp_setting(
    name: str,
    request: MCPServerSettingsRequest,
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
    runtime: Annotated[HermesRuntimeManager, Depends(get_hermes_runtime_manager)],
) -> MCPSettingsResponse:
    if name == "linkedin-search" and request.name != name:
        raise HTTPException(status_code=400, detail="The LinkedIn preset cannot be renamed")
    try:
        def update_server(session, change_version: str) -> None:
            upsert_mcp_server(
                session,
                request.model_dump(mode="json"),
                previous_name=name,
                change_version=change_version,
            )

        await _apply_mcp_reconfiguration(
            account=account,
            paths=paths,
            runtime=runtime,
            mutate=update_server,
        )
    except HermesRuntimeUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _prevent_auth_caching(response)
    return _mcp_settings(paths)


@app.delete("/settings/mcp/{name}", response_model=MCPSettingsResponse)
async def remove_mcp_setting(
    name: str,
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    paths: Annotated[CompanionPaths, Depends(get_local_companion_paths)],
    runtime: Annotated[HermesRuntimeManager, Depends(get_hermes_runtime_manager)],
) -> MCPSettingsResponse:
    try:
        def delete_server(session, change_version: str) -> None:
            delete_mcp_server(session, name, change_version=change_version)

        await _apply_mcp_reconfiguration(
            account=account,
            paths=paths,
            runtime=runtime,
            mutate=delete_server,
        )
    except HermesRuntimeUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _prevent_auth_caching(response)
    return _mcp_settings(paths)


@app.get("/settings/agent", response_model=AgentSettingsResponse)
async def read_agent_settings(
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_provider_account)],
    store: Annotated[AuthStore, Depends(get_store)],
    paths: Annotated[CompanionPaths, Depends(get_companion_paths)],
) -> AgentSettingsResponse:
    _prevent_auth_caching(response)
    return await _agent_settings_response(account, store, paths)


@app.put("/settings/agent", response_model=AgentSettingsResponse)
async def update_agent_settings(
    request: AgentSettingsRequest,
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_provider_account)],
    store: Annotated[AuthStore, Depends(get_store)],
    paths: Annotated[CompanionPaths, Depends(get_companion_paths)],
    runtime: Annotated[
        HermesRuntimeManager,
        Depends(get_hermes_runtime_manager),
    ],
) -> AgentSettingsResponse:
    with account_session(paths) as session:
        ensure_default_routes(session)
        session.flush()
        record = session.get(ModelRouteRecord, "interactive")
        if record is None:
            raise HTTPException(status_code=500, detail="Pilot's model route is missing")
        route = ModelRoute(
            **{
                key: value
                for key, value in record.__dict__.items()
                if not key.startswith("_")
            }
        ).model_copy(
            update={
                "model": request.model,
                "reasoning_effort": request.reasoning_effort,
            }
        )
        upsert_route(session, route)
    await runtime.invalidate(account.user_id)
    _prevent_auth_caching(response)
    return await _agent_settings_response(account, store, paths)


@app.post("/settings/providers/api-key", response_model=AuthSessionResponse)
async def connect_api_key(
    request: ApiKeyConnectionRequest,
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    validator: Annotated[ApiKeyValidator, Depends(get_api_key_validator)],
    store: Annotated[AuthStore, Depends(get_store)],
    runtime: Annotated[
        HermesRuntimeManager,
        Depends(get_hermes_runtime_manager),
    ],
) -> AuthSessionResponse:
    try:
        validator(request.api_key)
    except AuthCredentialError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    store.save_provider_connection(
        account_id=account.user_id,
        provider="api_key",
        credential=request.api_key.encode("utf-8"),
        plan_type="usage-based",
    )
    await runtime.invalidate(account.user_id)
    updated = store.load_account(account.user_id, account.identity_method)
    _prevent_auth_caching(response)
    return AuthSessionResponse(authenticated=True, user=updated.to_response())


@app.post("/settings/providers/select", response_model=AuthSessionResponse)
async def select_provider(
    request: ProviderSelectionRequest,
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    store: Annotated[AuthStore, Depends(get_store)],
    runtime: Annotated[
        HermesRuntimeManager,
        Depends(get_hermes_runtime_manager),
    ],
) -> AuthSessionResponse:
    if not store.select_provider(account.user_id, request.provider):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connect this provider before selecting it",
        )
    await runtime.invalidate(account.user_id)
    updated = store.load_account(account.user_id, account.identity_method)
    _prevent_auth_caching(response)
    return AuthSessionResponse(authenticated=True, user=updated.to_response())


@app.delete(
    "/settings/providers/{provider}",
    response_model=ProviderSettingsResponse,
)
async def disconnect_provider(
    provider: ProviderMethod,
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    store: Annotated[AuthStore, Depends(get_store)],
    runtime: Annotated[
        HermesRuntimeManager,
        Depends(get_hermes_runtime_manager),
    ],
) -> ProviderSettingsResponse:
    store.disconnect_provider(account.user_id, provider)
    await runtime.invalidate(account.user_id)
    _prevent_auth_caching(response)
    return store.provider_settings(account.user_id)


@app.post(
    "/settings/providers/codex/start",
    response_model=CodexLoginStartResponse,
)
def start_codex_login(
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    manager: Annotated[CodexOAuthManager, Depends(get_oauth_manager)],
) -> CodexLoginStartResponse:
    _prevent_auth_caching(response)
    try:
        attempt = manager.start(account.user_id)
    except CodexUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except CodexProtocolError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    return CodexLoginStartResponse(
        attempt_id=attempt.attempt_id,
        verification_url=attempt.verification_url,
        user_code=attempt.user_code,
        expires_at=attempt.expires_at,
    )


@app.post(
    "/settings/providers/codex/status/{attempt_id}",
    response_model=CodexLoginStatusResponse,
)
async def read_codex_login_status(
    attempt_id: str,
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    manager: Annotated[CodexOAuthManager, Depends(get_oauth_manager)],
    store: Annotated[AuthStore, Depends(get_store)],
    runtime: Annotated[
        HermesRuntimeManager,
        Depends(get_hermes_runtime_manager),
    ],
) -> CodexLoginStatusResponse:
    _prevent_auth_caching(response)
    _owned_codex_attempt(manager, attempt_id, account.user_id)
    attempt = manager.poll(attempt_id)
    if attempt is None:
        return CodexLoginStatusResponse(status="expired")
    if attempt.status == "pending":
        return CodexLoginStatusResponse(status="pending")
    if attempt.status == "failed":
        error = attempt.error or "ChatGPT connection failed"
        manager.finish(attempt_id)
        return CodexLoginStatusResponse(status="failed", error=error)
    if attempt.credentials is None:
        manager.finish(attempt_id)
        return CodexLoginStatusResponse(
            status="failed",
            error="Codex completed login without credentials",
        )

    store.save_provider_connection(
        account_id=account.user_id,
        provider="codex",
        credential=attempt.credentials,
        provider_email=attempt.email,
        plan_type=attempt.plan_type,
    )
    await runtime.invalidate(account.user_id)
    updated = store.load_account(account.user_id, account.identity_method)
    manager.finish(attempt_id)
    return CodexLoginStatusResponse(
        status="completed",
        user=updated.to_response(),
    )


@app.delete(
    "/settings/providers/codex/attempts/{attempt_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def cancel_codex_login(
    attempt_id: str,
    response: Response,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    manager: Annotated[CodexOAuthManager, Depends(get_oauth_manager)],
) -> None:
    _prevent_auth_caching(response)
    _owned_codex_attempt(manager, attempt_id, account.user_id)
    manager.cancel(attempt_id)


@app.post("/parse-cv", response_model=ParsedCVResponse)
async def parse_cv(
    file: Annotated[UploadFile, File(description="PDF, DOCX, or UTF-8 text CV")],
    _account: Annotated[AuthenticatedAccount, Depends(require_provider_account)],
) -> ParsedCVResponse:
    filename = file.filename or ""
    try:
        data = await file.read(5 * 1024 * 1024 + 1)
    finally:
        await file.close()

    try:
        return parse_cv_document(filename, data)
    except CVTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except UnsupportedCVTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc
    except CVParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@app.post("/match", response_model=MatchResponse)
def create_match(
    request: MatchRequest,
    matcher: Annotated[MatchFunction, Depends(get_matcher)],
) -> MatchResponse:
    try:
        return matcher(request)
    except MatchConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except MatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc


@app.post("/companion/chat", response_model=CompanionChatResponse)
def create_companion_reply(
    request: CompanionChatRequest,
    companion: Annotated[CompanionFunction, Depends(get_companion)],
    paths: Annotated[CompanionPaths, Depends(get_companion_paths)],
) -> CompanionChatResponse:
    try:
        with account_session(paths) as session:
            reserve_conversation_write(session)
            conversation = (
                get_conversation_session(session, request.session_id)
                if request.session_id
                else ensure_active_session(session, paths)
            )
            history = conversation_turns(conversation)
            if (
                request.candidate_profile is not None
                or request.job_description is not None
                or request.match_report is not None
            ):
                update_session_context(
                    session,
                    conversation,
                    candidate_profile=request.candidate_profile or conversation.candidate_profile,
                    job_description=request.job_description or conversation.job_description,
                    uploaded_filename=conversation.uploaded_filename,
                    match_report=(
                        request.match_report.model_dump(mode="json")
                        if request.match_report
                        else conversation.match_report
                    ),
                )
            append_message(
                session,
                conversation,
                role="user",
                content=request.message,
            )
            effective_request = request.model_copy(
                update={
                    "session_id": conversation.id,
                    "conversation": [CompanionTurn.model_validate(turn) for turn in history],
                    "candidate_profile": conversation.candidate_profile,
                    "job_description": conversation.job_description,
                    "match_report": (
                        MatchResponse.model_validate(conversation.match_report)
                        if conversation.match_report
                        else None
                    ),
                }
            )
            session_id = conversation.id
        reply = companion(effective_request)
        with account_session(paths) as session:
            reserve_conversation_write(session)
            conversation = get_conversation_session(session, session_id)
            append_message(
                session,
                conversation,
                role="assistant",
                content=reply.message,
            )
        return reply
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CompanionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc


@app.post("/companion/chat/stream")
async def stream_companion_reply(
    request: CompanionChatRequest,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    store: Annotated[AuthStore, Depends(get_store)],
    paths: Annotated[CompanionPaths, Depends(get_companion_paths)],
    runtime: Annotated[
        HermesRuntimeManager,
        Depends(get_hermes_runtime_manager),
    ],
) -> StreamingResponse:
    try:
        with account_session(paths) as session:
            reserve_conversation_write(session)
            conversation = (
                get_conversation_session(session, request.session_id)
                if request.session_id
                else ensure_active_session(session, paths)
            )
            history = conversation_turns(conversation)
            if (
                request.candidate_profile is not None
                or request.job_description is not None
                or request.match_report is not None
            ):
                update_session_context(
                    session,
                    conversation,
                    candidate_profile=(
                        request.candidate_profile
                        if request.candidate_profile is not None
                        else conversation.candidate_profile
                    ),
                    job_description=(
                        request.job_description
                        if request.job_description is not None
                        else conversation.job_description
                    ),
                    uploaded_filename=conversation.uploaded_filename,
                    match_report=(
                        request.match_report.model_dump(mode="json")
                        if request.match_report is not None
                        else conversation.match_report
                    ),
                )
            user_message = append_message(
                session,
                conversation,
                role="user",
                content=request.message,
            )
            session_id = conversation.id
            run_message_id = user_message.id
            effective_request = request.model_copy(
                update={
                    "session_id": session_id,
                    "conversation": [CompanionTurn.model_validate(turn) for turn in history],
                    "candidate_profile": conversation.candidate_profile,
                    "job_description": conversation.job_description,
                    "match_report": (
                        MatchResponse.model_validate(conversation.match_report)
                        if conversation.match_report
                        else None
                    ),
                }
            )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        prepared = await runtime.prepare(account, store)
    except HermesProviderConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except HermesRuntimeUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    with account_session(paths) as session:
        active_memory_context = build_active_memory_context(
            session,
            query=effective_request.message,
        )
        audit_memory_context_resolution(
            session,
            active_memory_context,
            conversation_id=session_id,
        )
    payload = build_hermes_run_payload(
        effective_request,
        model=prepared.model,
        active_memory_context=active_memory_context,
    )
    session_key = f"career-companion:web:{prepared.account_key}:{session_id}"
    payload["session_id"] = run_message_id

    async def events() -> AsyncIterator[bytes]:
        decoder = codecs.getincrementaldecoder("utf-8")()
        event_buffer = ""
        assistant_text = ""
        completed = False

        def consume_event_block(block: str) -> None:
            nonlocal assistant_text, completed
            data = "\n".join(
                line[5:].lstrip()
                for line in block.splitlines()
                if line.startswith("data:")
            )
            if not data:
                return
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                return
            if not isinstance(event, dict):
                return
            if event.get("event") == "message.delta" and isinstance(
                event.get("delta"), str
            ):
                assistant_text += event["delta"]
            elif event.get("event") == "run.completed":
                completed = True
                if isinstance(event.get("output"), str) and event["output"].strip():
                    assistant_text = event["output"]

        def consume_available_blocks(*, final: bool = False) -> None:
            nonlocal event_buffer
            while True:
                separator = re.search(r"\r?\n\r?\n", event_buffer)
                if separator is None:
                    break
                consume_event_block(event_buffer[: separator.start()])
                event_buffer = event_buffer[separator.end() :]
            if final and event_buffer.strip():
                consume_event_block(event_buffer)
                event_buffer = ""

        try:
            async for chunk in prepared.supervisor.proxy_stream(
                "/v1/runs",
                payload,
                session_key=session_key,
            ):
                event_buffer += decoder.decode(chunk)
                consume_available_blocks()
                yield chunk
        except httpx.HTTPError:
            failure = {
                "event": "run.failed",
                "error": "Pilot lost contact with the local Hermes runtime",
            }
            yield f"data: {json.dumps(failure)}\n\n".encode("utf-8")
        finally:
            event_buffer += decoder.decode(b"", final=True)
            consume_available_blocks(final=True)
            if completed and assistant_text.strip():
                try:
                    with account_session(paths) as session:
                        reserve_conversation_write(session)
                        conversation = get_conversation_session(session, session_id)
                        append_message(
                            session,
                            conversation,
                            role="assistant",
                            content=assistant_text,
                        )
                except (LookupError, ValueError):
                    pass
            try:
                refreshed = await runtime.capture_refreshed_codex_credentials(
                    account.user_id
                )
            except HermesProviderConfigurationError:
                refreshed = None
            if refreshed is not None:
                store.update_provider_credentials(
                    account.user_id,
                    "codex",
                    refreshed,
                )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/companion/chat/runs/{run_id}/approval")
async def resolve_companion_run_approval(
    run_id: str,
    request: CompanionApprovalRequest,
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
    store: Annotated[AuthStore, Depends(get_store)],
    paths: Annotated[CompanionPaths, Depends(get_companion_paths)],
    runtime: Annotated[
        HermesRuntimeManager,
        Depends(get_hermes_runtime_manager),
    ],
) -> dict[str, object]:
    try:
        with account_session(paths) as session:
            conversation = (
                get_conversation_session(session, request.session_id)
                if request.session_id
                else ensure_active_session(session, paths)
            )
        prepared = await runtime.prepare(account, store)
        return await prepared.supervisor.resolve_run_approval(
            run_id,
            request.choice,
            session_key=(
                f"career-companion:web:{prepared.account_key}:{conversation.id}"
            ),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HermesProviderConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except HermesRuntimeUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pilot could not apply that approval. It may have expired.",
        ) from exc


@app.api_route(
    "/api/{unmatched_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
def reject_unknown_api_path(unmatched_path: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Unknown API path: /api/{unmatched_path}",
    )


mount_static_frontend(app, frontend_build_directory())
