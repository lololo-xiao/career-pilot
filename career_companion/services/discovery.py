from __future__ import annotations

import re
from datetime import date
from typing import Any

import httpx

from career_companion.schemas import Job, JobSpec


def plain_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()


async def discover_greenhouse(board_token: str) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(url, params={"content": "true"})
        response.raise_for_status()
    jobs = []
    for item in response.json().get("jobs", []):
        jobs.append(
            Job(
                spec=JobSpec(
                    title=item["title"],
                    company=board_token.replace("-", " ").title(),
                    locations=[item.get("location", {}).get("name", "")],
                    description=plain_text(item.get("content", "")),
                    posted_date=_parse_iso_date(item.get("updated_at")),
                    source_url=item.get("absolute_url"),
                    source_type="greenhouse",
                ),
                canonical_url=item.get("absolute_url", ""),
            )
        )
    return jobs


async def discover_lever(company_slug: str) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{company_slug}"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(url, params={"mode": "json"})
        response.raise_for_status()
    jobs = []
    for item in response.json():
        lists = item.get("lists", [])
        description = " ".join(
            [item.get("descriptionPlain", "")]
            + [
                f"{entry.get('text', '')}: {plain_text(entry.get('content', ''))}"
                for entry in lists
            ]
        )
        categories: dict[str, Any] = item.get("categories") or {}
        jobs.append(
            Job(
                spec=JobSpec(
                    title=item["text"],
                    company=company_slug.replace("-", " ").title(),
                    locations=[categories.get("location", "")],
                    description=plain_text(description),
                    employment_type=categories.get("commitment", "unknown"),
                    source_url=item.get("hostedUrl"),
                    source_type="lever",
                ),
                canonical_url=item.get("hostedUrl", ""),
            )
        )
    return jobs


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
