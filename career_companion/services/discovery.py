from __future__ import annotations

import html
import json
import re
from datetime import date
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from career_companion.schemas import Job, JobSpec

DiscoveryProvider = Literal["greenhouse", "lever"]

_COMPANY_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
MAX_PUBLIC_FEED_BYTES = 8 * 1024 * 1024
MAX_DESCRIPTION_CHARACTERS = 50_000
MAX_JOB_URL_CHARACTERS = 2_000
MAX_JOB_TITLE_CHARACTERS = 500
MAX_LOCATION_CHARACTERS = 500
MAX_EMPLOYMENT_TYPE_CHARACTERS = 100


class DiscoveryError(RuntimeError):
    """A public job-feed failure whose message is safe to show to a user."""


def plain_text(value: str) -> str:
    unescaped = html.unescape(value or "")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescaped)).strip()


def bounded_description(value: str) -> str:
    return plain_text(value)[:MAX_DESCRIPTION_CHARACTERS]


def safe_job_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_JOB_URL_CHARACTERS:
        return None
    try:
        parts = urlsplit(candidate)
        hostname = parts.hostname
    except ValueError:
        return None
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not hostname
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    return candidate


def validate_company_identifier(value: str) -> str:
    identifier = value.strip()
    if not _COMPANY_IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError(
            "Company identifier must be a Greenhouse board token or Lever company slug"
        )
    return identifier


def parse_greenhouse_jobs(board_token: str, payload: Any) -> list[Job]:
    identifier = validate_company_identifier(board_token)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise DiscoveryError(
            "Greenhouse returned an unexpected public job-feed response"
        )

    jobs: list[Job] = []
    for item in payload["jobs"]:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        source_url = safe_job_url(item.get("absolute_url"))
        if not isinstance(title, str) or not title.strip():
            continue
        if source_url is None:
            continue
        location = item.get("location") or {}
        safe_title = plain_text(title)[:MAX_JOB_TITLE_CHARACTERS]
        if not safe_title:
            continue
        location_name = location.get("name", "") if isinstance(location, dict) else ""
        safe_location = (
            plain_text(location_name)[:MAX_LOCATION_CHARACTERS]
            if isinstance(location_name, str)
            else ""
        )
        locations = [safe_location] if safe_location else []
        try:
            jobs.append(
                Job(
                    spec=JobSpec(
                        title=safe_title,
                        company=_company_name(identifier),
                        locations=locations,
                        description=bounded_description(
                            str(item.get("content") or "")
                        ),
                        posted_date=_parse_iso_date(item.get("updated_at")),
                        source_url=source_url,
                        source_type="greenhouse",
                    ),
                    canonical_url=source_url,
                )
            )
        except ValidationError:
            continue
    return jobs


def parse_lever_jobs(company_slug: str, payload: Any) -> list[Job]:
    identifier = validate_company_identifier(company_slug)
    if not isinstance(payload, list):
        raise DiscoveryError("Lever returned an unexpected public job-feed response")

    jobs: list[Job] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        title = item.get("text")
        source_url = safe_job_url(item.get("hostedUrl"))
        if not isinstance(title, str) or not title.strip():
            continue
        if source_url is None:
            continue
        safe_title = plain_text(title)[:MAX_JOB_TITLE_CHARACTERS]
        if not safe_title:
            continue
        description_parts = [str(item.get("descriptionPlain") or "")]
        lists = item.get("lists") or []
        if isinstance(lists, list):
            for entry in lists:
                if not isinstance(entry, dict):
                    continue
                heading = str(entry.get("text") or "").strip()
                content = plain_text(str(entry.get("content") or ""))
                description_parts.append(
                    f"{heading}: {content}" if heading else content
                )
        categories: dict[str, Any] = (
            item.get("categories") if isinstance(item.get("categories"), dict) else {}
        )
        location = categories.get("location", "")
        safe_location = (
            plain_text(location)[:MAX_LOCATION_CHARACTERS]
            if isinstance(location, str)
            else ""
        )
        locations = [safe_location] if safe_location else []
        commitment = categories.get("commitment", "unknown")
        employment_type = (
            plain_text(commitment)[:MAX_EMPLOYMENT_TYPE_CHARACTERS]
            if isinstance(commitment, str)
            else "unknown"
        )
        try:
            jobs.append(
                Job(
                    spec=JobSpec(
                        title=safe_title,
                        company=_company_name(identifier),
                        locations=locations,
                        description=bounded_description(" ".join(description_parts)),
                        employment_type=employment_type or "unknown",
                        source_url=source_url,
                        source_type="lever",
                    ),
                    canonical_url=source_url,
                )
            )
        except ValidationError:
            continue
    return jobs


async def discover_greenhouse(board_token: str) -> list[Job]:
    identifier = validate_company_identifier(board_token)
    payload = await _read_public_feed(
        "greenhouse",
        f"https://boards-api.greenhouse.io/v1/boards/{identifier}/jobs",
        params={"content": "true"},
    )
    return parse_greenhouse_jobs(identifier, payload)


async def discover_lever(company_slug: str) -> list[Job]:
    identifier = validate_company_identifier(company_slug)
    payload = await _read_public_feed(
        "lever",
        f"https://api.lever.co/v0/postings/{identifier}",
        params={"mode": "json"},
    )
    return parse_lever_jobs(identifier, payload)


async def discover_public_jobs(
    provider: DiscoveryProvider,
    company_identifier: str,
) -> list[Job]:
    """Read one provider's public feed without persisting any returned jobs."""

    if provider == "greenhouse":
        return await discover_greenhouse(company_identifier)
    if provider == "lever":
        return await discover_lever(company_identifier)
    raise ValueError("Unsupported public job discovery provider")


async def _read_public_feed(
    provider: DiscoveryProvider,
    url: str,
    *,
    params: dict[str, str],
) -> Any:
    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream("GET", url, params=params) as response:
                if response.is_redirect:
                    raise DiscoveryError(
                        f"The public {provider.title()} job feed is temporarily unavailable"
                    )
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError:
                        declared_length = 0
                    if declared_length > MAX_PUBLIC_FEED_BYTES:
                        raise DiscoveryError(
                            f"The public {provider.title()} job feed is too large to preview safely"
                        )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_PUBLIC_FEED_BYTES:
                        raise DiscoveryError(
                            f"The public {provider.title()} job feed is too large to preview safely"
                        )
                    body.extend(chunk)
                return json.loads(body)
    except DiscoveryError:
        raise
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise DiscoveryError(
                f"No public {provider.title()} job feed was found for that company identifier"
            ) from exc
        raise DiscoveryError(
            f"The public {provider.title()} job feed is temporarily unavailable"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise DiscoveryError(
            f"The public {provider.title()} job feed is temporarily unavailable"
        ) from exc


def _company_name(identifier: str) -> str:
    return re.sub(r"[._-]+", " ", identifier).title()


def _parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
