from __future__ import annotations

from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import (
    AuthConfigurationError,
    AuthStore,
    AuthenticatedAccount,
    get_auth_store,
)
from career_companion.paths import CompanionPaths
from career_companion.persistence import account_session
from career_companion.workspace import initialize_account_workspace


def get_store() -> AuthStore:
    try:
        return get_auth_store()
    except AuthConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


def get_current_account(
    store: Annotated[AuthStore, Depends(get_store)],
) -> AuthenticatedAccount:
    return store.ensure_local_account()


def require_current_account(
    account: Annotated[AuthenticatedAccount, Depends(get_current_account)],
) -> AuthenticatedAccount:
    return account


def require_provider_account(
    account: Annotated[AuthenticatedAccount, Depends(require_current_account)],
) -> AuthenticatedAccount:
    if account.active_provider is None or account.provider_connection is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Choose an AI connection in Settings before using CareerPilot",
        )
    return account


def get_companion_paths(
    account: Annotated[AuthenticatedAccount, Depends(require_provider_account)],
) -> CompanionPaths:
    paths = CompanionPaths.discover().scoped_to(account.user_id)
    initialize_account_workspace(paths)
    return paths


def get_companion_session(
    paths: Annotated[CompanionPaths, Depends(get_companion_paths)],
) -> Generator[Session, None, None]:
    with account_session(paths) as session:
        yield session
