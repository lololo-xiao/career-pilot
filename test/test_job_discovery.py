from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import func, select

from career_companion.database import JobRecord
from career_companion.paths import CompanionPaths
from career_companion.persistence import clear_factory_cache, session_factory_for
from career_companion.services import discovery
from career_companion.services.discovery import (
    DiscoveryError,
    discover_greenhouse,
    parse_greenhouse_jobs,
    parse_lever_jobs,
)
from career_companion.services.jobs import add_job


def test_greenhouse_provider_payload_is_normalized() -> None:
    jobs = parse_greenhouse_jobs(
        "example-labs",
        {
            "jobs": [
                {
                    "title": " Machine Learning Engineer ",
                    "location": {"name": "Berlin, Germany"},
                    "content": "<p>Build retrieval &amp; evaluation systems.</p>",
                    "updated_at": "2026-07-16T12:34:56Z",
                    "absolute_url": (
                        "https://boards.greenhouse.io/example-labs/jobs/123"
                    ),
                },
                {"title": "Missing URL", "content": "ignored"},
            ]
        },
    )

    assert len(jobs) == 1
    assert jobs[0].spec.title == "Machine Learning Engineer"
    assert jobs[0].spec.company == "Example Labs"
    assert jobs[0].spec.locations == ["Berlin, Germany"]
    assert jobs[0].spec.description == "Build retrieval & evaluation systems."
    assert jobs[0].spec.posted_date.isoformat() == "2026-07-16"
    assert jobs[0].spec.source_type == "greenhouse"


def test_lever_provider_payload_is_normalized() -> None:
    jobs = parse_lever_jobs(
        "example-ai",
        [
            {
                "text": "AI Platform Engineer",
                "descriptionPlain": "Own the model platform.",
                "lists": [
                    {
                        "text": "What you bring",
                        "content": "<ul><li>Python</li><li>Kubernetes</li></ul>",
                    }
                ],
                "categories": {
                    "location": "Paris, France",
                    "commitment": "Full-time",
                },
                "hostedUrl": "https://jobs.lever.co/example-ai/role-1",
            }
        ],
    )

    assert len(jobs) == 1
    assert jobs[0].spec.company == "Example Ai"
    assert jobs[0].spec.locations == ["Paris, France"]
    assert jobs[0].spec.employment_type == "Full-time"
    assert jobs[0].spec.description == (
        "Own the model platform. What you bring: Python Kubernetes"
    )
    assert jobs[0].spec.source_type == "lever"


def test_selected_discovery_result_uses_shared_job_deduplication(tmp_path) -> None:
    clear_factory_cache()
    paths = CompanionPaths.at_root(tmp_path / "companion").scoped_to("account-a")
    selected = parse_greenhouse_jobs(
        "example",
        {
            "jobs": [
                {
                    "title": "ML Engineer",
                    "location": {"name": "Remote"},
                    "content": "Python and evaluation.",
                    "absolute_url": (
                        "https://boards.greenhouse.io/example/jobs/123?utm_source=feed"
                    ),
                }
            ]
        },
    )[0]

    try:
        with session_factory_for(paths)() as session:
            first, first_created = add_job(session, selected)
            duplicate, duplicate_created = add_job(session, selected)
            session.flush()

            assert first_created is True
            assert duplicate_created is False
            assert duplicate.id == first.id
            assert first.canonical_url == (
                "https://boards.greenhouse.io/example/jobs/123"
            )
            assert session.scalar(select(func.count()).select_from(JobRecord)) == 1
    finally:
        clear_factory_cache()


def test_public_discovery_rejects_unsafe_identifier_before_network(
    monkeypatch,
) -> None:
    class UnexpectedClient:
        def __init__(self, **_kwargs):
            raise AssertionError("network client must not be created")

    monkeypatch.setattr(discovery.httpx, "AsyncClient", UnexpectedClient)

    with pytest.raises(ValueError, match="Greenhouse board token"):
        asyncio.run(discover_greenhouse("../private-host"))


def test_public_discovery_hides_transport_failure_details(monkeypatch) -> None:
    class FailingClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, params):
            del params
            request = httpx.Request("GET", url)
            raise httpx.ConnectError("secret resolver detail", request=request)

    monkeypatch.setattr(discovery.httpx, "AsyncClient", FailingClient)

    with pytest.raises(DiscoveryError) as caught:
        asyncio.run(discover_greenhouse("example"))

    assert str(caught.value) == (
        "The public Greenhouse job feed is temporarily unavailable"
    )
    assert "secret resolver detail" not in str(caught.value)


def test_provider_parser_rejects_unexpected_top_level_payload() -> None:
    with pytest.raises(DiscoveryError, match="unexpected public job-feed response"):
        parse_lever_jobs("example", {"postings": []})
