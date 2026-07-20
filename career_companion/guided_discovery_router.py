from __future__ import annotations

import ipaddress
import os
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthenticatedAccount
from app.dependencies import require_provider_account
from career_companion.services.discovery import DiscoveryError, discover_public_jobs


_LOOPBACK_ONLY_DETAIL = "Public job discovery is available only from this device"
_CLIENT_IDENTITY_HEADERS = frozenset(
    {
        "cf-connecting-ip",
        "fastly-client-ip",
        "forwarded",
        "true-client-ip",
        "x-client-ip",
        "x-cluster-client-ip",
        "x-forwarded-for",
        "x-real-ip",
    }
)
_CAPACITOR_IOS_ORIGIN = "capacitor://localhost"


def _private_mobile_discovery_enabled(request: Request) -> bool:
    enabled = os.getenv("CAREERPILOT_ALLOW_PRIVATE_MOBILE_DISCOVERY", "").strip()
    origin = request.headers.get("origin", "").rstrip("/")
    return enabled.casefold() == "true" and origin == _CAPACITOR_IOS_ORIGIN


def require_direct_loopback_client(request: Request) -> None:
    """Reject discovery outside loopback unless private mobile access is explicit."""

    if _private_mobile_discovery_enabled(request):
        return

    if any(header in request.headers for header in _CLIENT_IDENTITY_HEADERS):
        raise HTTPException(status_code=403, detail=_LOOPBACK_ONLY_DETAIL)

    client = request.client
    host = client.host if client is not None else None
    if not isinstance(host, str):
        raise HTTPException(status_code=403, detail=_LOOPBACK_ONLY_DETAIL)
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=_LOOPBACK_ONLY_DETAIL) from exc
    if not address.is_loopback:
        raise HTTPException(status_code=403, detail=_LOOPBACK_ONLY_DETAIL)


router = APIRouter(
    tags=["guided-job-discovery"],
    dependencies=[Depends(require_direct_loopback_client)],
)
ProviderAccountDep = Annotated[
    AuthenticatedAccount,
    Depends(require_provider_account),
]


class GuidedPublicDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["greenhouse", "lever"]
    company_identifier: str = Field(min_length=1, max_length=100)
    limit: int = Field(default=25, ge=1, le=25)


@router.post("/jobs/discover-public")
async def discover_public_job_feed(
    payload: GuidedPublicDiscoveryRequest,
    account: ProviderAccountDep,
) -> dict[str, Any]:
    """Preview one known public provider feed without opening a workspace."""

    del account
    try:
        discovered = await discover_public_jobs(
            payload.provider,
            payload.company_identifier,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except DiscoveryError as exc:
        raise HTTPException(502, str(exc)) from exc

    returned = discovered[: payload.limit]
    return {
        "activity": {
            "type": "public_network_read",
            "provider": payload.provider,
            "company_identifier": payload.company_identifier.strip(),
        },
        "discovered": len(discovered),
        "returned": len(returned),
        "jobs": [job.spec.model_dump(mode="json") for job in returned],
        "stored": 0,
    }
