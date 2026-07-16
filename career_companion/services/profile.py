from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

from docx import Document
from pypdf import PdfReader
from sqlalchemy.orm import Session

from career_companion.database import CandidateProfileRecord, SourceDocumentRecord
from career_companion.paths import CompanionPaths
from career_companion.schemas import (
    CandidateProfile,
    ClaimStatus,
    EvidenceReference,
    ProfileClaim,
)
from career_companion.services.audit import record_audit

SUPPORTED_MEDIA = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
PHONE_PATTERN = re.compile(r"(?:\+\d{1,3}[\s-]?)?(?:\d[\s-]?){7,14}")
DEGREE_PATTERN = re.compile(
    r"\b(?:MSc|BSc|MA|BA|MBA|Master(?:'s)?|Bachelor(?:'s)?|PhD|Doctorate)\b[^\n]{0,120}",
    re.IGNORECASE,
)
LANGUAGE_PATTERN = re.compile(
    r"\b(?:English|German|Chinese|French|Spanish|Italian|Portuguese|Dutch|Arabic|Japanese|Korean)\b",
    re.IGNORECASE,
)
LANGUAGE_LEVEL_PATTERN = re.compile(
    r"\b(?:native|mother tongue|fluent|professional|working proficiency|basic|beginner|intermediate|advanced|[ABC][12])\b",
    re.IGNORECASE,
)
PUBLICATION_SIGNAL_PATTERN = re.compile(
    r"\b(?:co-?author|publication|published|proceedings|journal|arxiv|ACL|COLING|EMNLP|NAACL|EACL|NeurIPS|ICML|ICLR|AAAI|IJCAI|SIGIR|KDD|IEEE|ACM)\b",
    re.IGNORECASE,
)

SECTION_NAMES = {
    "awards": "other",
    "certifications": "other",
    "contact": "contact",
    "education": "education",
    "employment": "experience",
    "experience": "experience",
    "languages": "languages",
    "personal details": "contact",
    "profile": "other",
    "projects": "projects",
    "publications": "publications",
    "research publications": "publications",
    "selected publications": "publications",
    "skills": "skills",
    "summary": "other",
    "work experience": "experience",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract_document(path: Path) -> tuple[str, list[tuple[int, str]]]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        pages = [(index, page.extract_text() or "") for index, page in enumerate(reader.pages, 1)]
        return "\n".join(text for _, text in pages), pages
    if suffix == ".docx":
        doc = Document(str(path))
        text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
        return text, [(1, text)]
    raise ValueError("Only PDF and DOCX files are accepted")


def import_profile_document(
    session: Session, source: Path, paths: CompanionPaths | None = None
) -> tuple[SourceDocumentRecord, CandidateProfile]:
    paths = paths or CompanionPaths.discover()
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_MEDIA:
        raise ValueError("Only PDF and DOCX files are accepted")
    paths.create()
    digest = sha256_file(source)
    destination = paths.imports / f"{digest}{suffix}"
    if not destination.exists():
        shutil.copy2(source, destination)
    text, pages = extract_document(destination)
    document = SourceDocumentRecord(
        filename=source.name,
        stored_path=str(destination),
        sha256=digest,
        media_type=SUPPORTED_MEDIA[suffix],
        extracted_text=text,
    )
    session.add(document)
    session.flush()
    profile = _candidate_from_text(document, pages)
    record_audit(
        session,
        "profile.document_imported",
        subject_type="source_document",
        subject_id=document.id,
        payload={"filename": source.name, "sha256": digest},
    )
    return document, profile


def _candidate_from_text(
    document: SourceDocumentRecord, pages: list[tuple[int, str]]
) -> CandidateProfile:
    claims: list[ProfileClaim] = []
    seen: set[str] = set()
    email = ""
    phone = ""
    languages: list[str] = []
    seen_languages: set[str] = set()
    current_section: str | None = None

    for page_number, page_text in pages:
        if not email and (email_match := EMAIL_PATTERN.search(page_text)):
            email = email_match.group(0)
        if not phone:
            phone = _first_phone(page_text)

        for raw_line in page_text.splitlines():
            line = _clean_line(raw_line)
            if not line:
                continue

            section = _section_name(line)
            if section:
                current_section = section
                continue

            language_values = _languages_from_line(
                line, in_language_section=current_section == "languages"
            )
            for language in language_values:
                fingerprint = language.casefold()
                if fingerprint not in seen_languages:
                    seen_languages.add(fingerprint)
                    languages.append(language)

            degree_match = DEGREE_PATTERN.search(line)
            if degree_match:
                _append_claim(
                    claims,
                    seen,
                    document,
                    page_number,
                    key="degree",
                    value=degree_match.group(0),
                    excerpt=line,
                    confidence=0.82,
                )

            is_publication = current_section == "publications" or bool(
                PUBLICATION_SIGNAL_PATTERN.search(line)
            )
            if is_publication and not degree_match and len(line) >= 8:
                _append_claim(
                    claims,
                    seen,
                    document,
                    page_number,
                    key="publication",
                    value=line,
                    excerpt=line,
                    confidence=0.86 if current_section == "publications" else 0.78,
                )

    return CandidateProfile(
        email=email,
        phone=phone,
        claims=claims,
        languages=languages,
        source_documents=[document.id],
    )


def _clean_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t-–—•·,;|")


def _section_name(line: str) -> str | None:
    normalized = re.sub(r"[^a-z ]", "", line.casefold()).strip()
    return SECTION_NAMES.get(normalized)


def _languages_from_line(line: str, *, in_language_section: bool) -> list[str]:
    if not LANGUAGE_PATTERN.search(line):
        return []
    if not in_language_section and not LANGUAGE_LEVEL_PATTERN.search(line):
        return []

    values: list[str] = []
    for chunk in re.split(r"[,;|]", line):
        value = _clean_line(chunk)
        if value and LANGUAGE_PATTERN.search(value):
            values.append(value)
    return values


def _first_phone(text: str) -> str:
    for match in PHONE_PATTERN.finditer(text):
        value = re.sub(r"\s+", " ", match.group(0)).strip(" ,;|")
        digits = re.sub(r"\D", "", value)
        if 8 <= len(digits) <= 15 and not ("." in value and not value.startswith("+")):
            return value
    return ""


def _append_claim(
    claims: list[ProfileClaim],
    seen: set[str],
    document: SourceDocumentRecord,
    page_number: int,
    *,
    key: str,
    value: str,
    excerpt: str,
    confidence: float,
) -> None:
    cleaned = _clean_line(value)
    fingerprint = f"{key}:{cleaned.casefold()}"
    if not cleaned or fingerprint in seen:
        return
    seen.add(fingerprint)
    claims.append(
        ProfileClaim(
            key=key,
            value=cleaned,
            status=ClaimStatus.LEARNING,
            confidence=confidence,
            evidence=[
                EvidenceReference(
                    source_id=document.id,
                    source_name=document.filename,
                    page=page_number,
                    excerpt=excerpt[:500],
                    content_hash=document.sha256,
                )
            ],
        )
    )


def save_profile(session: Session, profile: CandidateProfile) -> CandidateProfileRecord:
    for claim in [*profile.claims, *profile.work_authorization]:
        if claim.status == ClaimStatus.VERIFIED and not claim.evidence:
            raise ValueError(f"Verified claim {claim.key!r} must include evidence")
    record = session.get(CandidateProfileRecord, profile.id) if profile.id else None
    payload = profile.model_dump(mode="json", exclude={"id", "updated_at"})
    if record is None:
        record = CandidateProfileRecord(display_name=profile.display_name, payload=payload)
        session.add(record)
    else:
        record.display_name = profile.display_name
        record.payload = payload
    session.flush()
    record_audit(
        session,
        "profile.saved",
        subject_type="candidate_profile",
        subject_id=record.id,
        payload={"verified_claims": sum(c.status == ClaimStatus.VERIFIED for c in profile.claims)},
    )
    return record
