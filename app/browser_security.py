from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def browser_request_guard(
    allowed_origins: set[str],
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    """Reject cross-site browser mutations before cookies reach an endpoint."""

    normalized = {origin.rstrip("/") for origin in allowed_origins}

    async def middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method.upper() not in _SAFE_METHODS:
            origin = request.headers.get("origin", "").rstrip("/")
            fetch_site = request.headers.get("sec-fetch-site", "").casefold()
            if origin and origin not in normalized:
                return JSONResponse({"detail": "Browser origin is not allowed"}, status_code=403)
            if not origin and fetch_site == "cross-site":
                return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
        return await call_next(request)

    return middleware
