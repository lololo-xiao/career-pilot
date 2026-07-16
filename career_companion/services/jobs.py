from __future__ import annotations

import hashlib
import re
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from career_companion.database import JobRecord
from career_companion.schemas import Job, JobSpec
from career_companion.services.audit import record_audit
from job_pipeline.cli import load_config, score_row

TRACKING_QUERY_KEYS = {
    "gh_src",
    "gh_jid",
    "source",
    "ref",
    "referrer",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in TRACKING_QUERY_KEYS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def fingerprint_job(spec: JobSpec, canonical_url: str = "") -> str:
    if canonical_url:
        seed = canonical_url
    else:
        location = "|".join(sorted(value.casefold() for value in spec.locations))
        seed = "|".join(
            [
                spec.company.casefold(),
                spec.title.casefold(),
                location,
                spec.description[:500].casefold(),
            ]
        )
    return hashlib.sha256(normalize_space(seed).encode()).hexdigest()


def add_job(session: Session, job: Job) -> tuple[JobRecord, bool]:
    canonical = canonicalize_url(job.canonical_url or str(job.spec.source_url or ""))
    fingerprint = fingerprint_job(job.spec, canonical)
    existing = session.scalar(select(JobRecord).where(JobRecord.fingerprint == fingerprint))
    if existing:
        return existing, False
    record = JobRecord(
        company=job.spec.company,
        title=job.spec.title,
        canonical_url=canonical,
        fingerprint=fingerprint,
        source_type=job.spec.source_type,
        raw_payload=job.model_dump(mode="json"),
        normalized_spec=job.spec.model_dump(mode="json"),
    )
    session.add(record)
    session.flush()
    record_audit(
        session,
        "job.discovered",
        subject_type="job",
        subject_id=record.id,
        payload={"company": record.company, "title": record.title, "source": record.source_type},
    )
    return record, True


def score_job(session: Session, job_id: str) -> JobRecord:
    record = session.get(JobRecord, job_id)
    if not record:
        raise LookupError("Job not found")
    spec = JobSpec.model_validate(record.normalized_spec)
    row = {
        "company": spec.company,
        "role": spec.title,
        "country": spec.locations[0] if spec.locations else "",
        "city": spec.locations[0] if spec.locations else "",
        "source": spec.source_type,
        "job_url": record.canonical_url,
        "posted_date": spec.posted_date.isoformat() if spec.posted_date else "",
        "deadline": spec.deadline.isoformat() if spec.deadline else "",
        "company_size": "",
        "job_description": spec.description,
        "jd_text": spec.description,
        "cover_letter": "",
        "track": "",
        "skills_added": "",
        "prep_gaps": "",
        "result": "",
        "notes": "",
    }
    result = score_row(row, load_config())
    record.score = float(result["score"])
    record.tier = result["tier"]
    record.score_explanation = result["reasons"]
    record_audit(
        session,
        "job.scored",
        subject_type="job",
        subject_id=record.id,
        payload={"score": record.score, "tier": record.tier, "date": date.today().isoformat()},
    )
    return record
