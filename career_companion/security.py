from __future__ import annotations

import hmac
import secrets
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware

from career_companion.config import ProductConfig, ensure_session_token
from career_companion.paths import CompanionPaths

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_PATHS = {"/health", "/api/v1/session/bootstrap"}


class LocalSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, paths: CompanionPaths, config: ProductConfig) -> None:
        super().__init__(app)
        self.session_token = ensure_session_token(paths)
        self.allowed_origins = set(config.server.allowed_origins)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path.startswith("/assets/") or request.url.path in {"/", "/favicon.ico"}:
            response = await call_next(request)
            return self._headers(response)
        if request.url.path not in PUBLIC_PATHS:
            cookie = request.cookies.get("cc_session", "")
            header = request.headers.get("X-Session-Token", "")
            if not (
                hmac.compare_digest(cookie, self.session_token)
                or hmac.compare_digest(header, self.session_token)
            ):
                return self._headers(
                    Response("Local session authentication required", status_code=401)
                )
        origin = request.headers.get("origin")
        if origin and origin not in self.allowed_origins:
            return self._headers(Response("Origin is not allowed", status_code=403))
        if request.method not in SAFE_METHODS and request.url.path != "/api/v1/session/bootstrap":
            csrf_cookie = request.cookies.get("cc_csrf", "")
            csrf_header = request.headers.get("X-CSRF-Token", "")
            if not csrf_cookie or not hmac.compare_digest(csrf_cookie, csrf_header):
                return self._headers(Response("CSRF validation failed", status_code=403))
        response = await call_next(request)
        return self._headers(response)

    @staticmethod
    def _headers(response: Response) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        return response


def establish_session(response: Response, submitted_token: str, paths: CompanionPaths) -> str:
    expected = ensure_session_token(paths)
    if not hmac.compare_digest(submitted_token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid local token")
    csrf = secrets.token_urlsafe(24)
    response.set_cookie(
        "cc_session", expected, httponly=True, samesite="strict", secure=False, path="/"
    )
    response.set_cookie("cc_csrf", csrf, httponly=False, samesite="strict", secure=False, path="/")
    return csrf
