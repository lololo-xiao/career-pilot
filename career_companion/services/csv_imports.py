from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time

from sqlalchemy.orm import Session

from career_companion.database import JobRecord
from career_companion.schemas import ApplicationStatus, Job, JobSpec
from career_companion.services.applications import (
    ApplicationPersistenceError,
    DEDICATED_WORKFLOW_STATUSES,
    ensure_application,
    transition_application,
)
from career_companion.services.jobs import add_job

MAX_CSV_ROWS = 1_000

COMPANY_SIZE_ALIASES = {
    "1-10": "1-10",
    "11-50": "11-50",
    "51-200": "51-200",
    "201-500": "201-500",
    "501-1000": "501-1000",
    "1001-5000": "1001-5000",
    "5001-10000": "5001-10000",
    "5000+": "5001-10000",
    "10001+": "10001+",
    "10000+": "10001+",
    "unknown": "unknown",
}

STATUS_ALIASES = {
    "tracked": "discovered",
    "found": "discovered",
    "fit_reviewed": "scored",
    "approved_to_tailor": "approved",
    "ready_to_apply": "ready",
    "applied": "submitted",
    "online_assessment": "oa",
    "oa_fail": "oa_failed",
    "interview_1_fail": "interview_1_failed",
    "interview_2_fail": "interview_2_failed",
    "final_interview_fail": "final_interview_failed",
    "no_answer": "no_response",
    "rejected_no_answer": "no_response",
    "hold": "followed_up",
}


@dataclass
class CsvImportResult:
    created_jobs: int = 0
    updated_jobs: int = 0
    created_applications: int = 0
    updated_applications: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int | list[str]]:
        return {
            "created_jobs": self.created_jobs,
            "updated_jobs": self.updated_jobs,
            "created_applications": self.created_applications,
            "updated_applications": self.updated_applications,
            "skipped": self.skipped,
            "errors": self.errors,
        }


def import_jobs_csv(session: Session, csv_text: str) -> CsvImportResult:
    result = CsvImportResult()
    for row_number, row in _read_rows(csv_text):
        try:
            job = _job_from_row(row)
            record, created = add_job(session, job)
            if created:
                result.created_jobs += 1
            elif _enrich_job(record, job.spec):
                result.updated_jobs += 1
            else:
                result.skipped += 1
        except (ValueError, TypeError) as exc:
            result.errors.append(f"Row {row_number}: {exc}")
    return result


def import_applications_csv(session: Session, csv_text: str) -> CsvImportResult:
    result = CsvImportResult()
    for row_number, row in _read_rows(csv_text):
        try:
            job = _job_from_row(row)
            target = _application_status(row)
            applied_date = _parse_date(
                _value(row, "applied_date", "date_applied", "application_date"),
                field_name="applied date",
            )
            job_record, job_created = add_job(session, job)
            if job_created:
                result.created_jobs += 1
            elif _enrich_job(job_record, job.spec):
                result.updated_jobs += 1

            application, application_created = ensure_application(
                session,
                job_record.id,
            )

            if application.status != target.value:
                application = transition_application(
                    session,
                    application.id,
                    target,
                    note=_value(row, "notes", "note") or "Imported from CSV",
                    confirmed_by_user=True,
                    manual_override=True,
                )
            if applied_date:
                application.submitted_at = datetime.combine(applied_date, time.min, tzinfo=UTC)
            next_action = _value(row, "next_action")
            if next_action:
                application.next_action = next_action

            if application_created:
                result.created_applications += 1
            else:
                result.updated_applications += 1
        except (ApplicationPersistenceError, LookupError, ValueError, TypeError) as exc:
            result.errors.append(f"Row {row_number}: {exc}")
    return result


def _read_rows(csv_text: str) -> list[tuple[int, dict[str, str]]]:
    if not csv_text.strip():
        raise ValueError("CSV file is empty")
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        raise ValueError("CSV header row is missing")
    normalized_headers = [_normalize_key(item or "") for item in reader.fieldnames]
    if "company" not in normalized_headers:
        raise ValueError("CSV must include a company column")
    if not ({"title", "role", "job"} & set(normalized_headers)):
        raise ValueError("CSV must include a title or role column")

    rows: list[tuple[int, dict[str, str]]] = []
    for row_number, raw_row in enumerate(reader, start=2):
        if len(rows) >= MAX_CSV_ROWS:
            raise ValueError(f"CSV exceeds the {MAX_CSV_ROWS}-row limit")
        if None in raw_row:
            raise ValueError(f"Row {row_number} has more values than the header row")
        row = {
            _normalize_key(key or ""): (value or "").strip()
            for key, value in raw_row.items()
        }
        if not any(row.values()):
            continue
        rows.append((row_number, row))
    if not rows:
        raise ValueError("CSV contains no data rows")
    return rows


def _job_from_row(row: dict[str, str]) -> Job:
    company = _required(row, "company")
    title = _required(row, "title", "role", "job")
    url = _value(row, "url", "job_url", "canonical_url")
    description = _value(row, "description", "job_description", "jd_text")
    if not description:
        description = f"Imported opportunity for {title} at {company}."
    location_text = _value(row, "locations", "location")
    if not location_text:
        location_text = ", ".join(
            value for value in [_value(row, "city"), _value(row, "country")] if value
        )
    locations = [
        item.strip()
        for item in re.split(r"[;|]", location_text)
        if item.strip()
    ]
    workplace = _normalize_value(_value(row, "workplace", "workplace_type")) or "unknown"
    if workplace == "on_site":
        workplace = "onsite"
    if workplace not in {"onsite", "hybrid", "remote", "unknown"}:
        raise ValueError(f"unsupported workplace value '{workplace}'")
    company_size_raw = _value(row, "company_size") or "unknown"
    company_size_key = (
        company_size_raw.casefold()
        .replace(",", "")
        .replace(" ", "")
        .replace("–", "-")
        .replace("—", "-")
    )
    company_size = COMPANY_SIZE_ALIASES.get(company_size_key)
    if not company_size:
        raise ValueError(f"unsupported company size '{company_size_raw}'")

    spec = JobSpec(
        title=title,
        company=company,
        locations=locations,
        description=description,
        employment_type=_value(row, "employment_type") or "unknown",
        workplace_type=workplace,
        company_size=company_size,
        posted_date=_parse_date(_value(row, "posted_date", "job_posted_date"), field_name="posted date"),
        source_url=url or None,
        source_type=_value(row, "source", "source_type") or "csv",
    )
    return Job(spec=spec, canonical_url=url)


def _application_status(row: dict[str, str]) -> ApplicationStatus:
    raw_status = _value(row, "status", "application_status") or "discovered"
    normalized = _normalize_value(raw_status)
    status_value = STATUS_ALIASES.get(normalized, normalized)
    try:
        status = ApplicationStatus(status_value)
    except ValueError as exc:
        supported = ", ".join(
            item.value
            for item in ApplicationStatus
            if item.value not in DEDICATED_WORKFLOW_STATUSES
        )
        raise ValueError(f"unsupported status '{raw_status}'. Use one of: {supported}") from exc
    if status.value in DEDICATED_WORKFLOW_STATUSES:
        raise ValueError(
            f"status '{raw_status}' requires a dedicated workflow and cannot be imported"
        )
    return status


def _enrich_job(record: JobRecord, spec: JobSpec) -> bool:
    existing = dict(record.normalized_spec or {})
    incoming = spec.model_dump(mode="json")
    changed = False
    for key, value in incoming.items():
        if value in (None, "", [], "unknown"):
            continue
        if existing.get(key) != value:
            existing[key] = value
            changed = True
    if changed:
        record.company = spec.company
        record.title = spec.title
        record.normalized_spec = existing
        raw_payload = dict(record.raw_payload or {})
        raw_payload["spec"] = existing
        record.raw_payload = raw_payload
    return changed


def _parse_date(value: str, *, field_name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid {field_name} '{value}'; expected YYYY-MM-DD") from exc


def _normalize_key(value: str) -> str:
    return _normalize_value(value)


def _normalize_value(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.casefold())).strip("_")


def _required(row: dict[str, str], *keys: str) -> str:
    value = _value(row, *keys)
    if not value:
        raise ValueError(f"missing required field: {keys[0]}")
    return value


def _value(row: dict[str, str], *keys: str) -> str:
    return next((row.get(key, "") for key in keys if row.get(key, "")), "")
