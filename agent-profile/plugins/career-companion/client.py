from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

_ACCOUNT_KEY_PATTERN = re.compile(r"[a-f0-9]{64}")


class BridgeError(RuntimeError):
    pass


class HermesBridgeClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("CAREER_COMPANION_API_URL", "").rstrip("/")
        self.token = os.getenv("CAREER_COMPANION_PLUGIN_TOKEN", "")
        self.account_key = os.getenv("CAREER_COMPANION_ACCOUNT_KEY", "")
        self._validate_configuration()

    def _validate_configuration(self) -> None:
        parts = urlsplit(self.base_url)
        if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise BridgeError("Career Companion bridge must use a loopback HTTP URL")
        if parts.username or parts.password:
            raise BridgeError("Career Companion bridge URL cannot contain credentials")
        if not self.token:
            raise BridgeError("Career Companion bridge token is missing")
        if not _ACCOUNT_KEY_PATTERN.fullmatch(self.account_key):
            raise BridgeError("Career Companion account key is invalid")

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Career-Account": self.account_key,
        }
        try:
            with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
                response = client.request(
                    method,
                    f"{self.base_url}/{path.lstrip('/')}",
                    headers=headers,
                    json=json_body,
                    params=params,
                )
        except httpx.HTTPError as exc:
            raise BridgeError("Career Companion bridge is unavailable") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            message = (
                detail
                if isinstance(detail, str)
                else "Career Companion bridge rejected the request"
            )
            raise BridgeError(message)
        try:
            return response.json()
        except ValueError as exc:
            raise BridgeError("Career Companion bridge returned invalid JSON") from exc
