from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career_companion.config import ProductConfig
from career_companion.database import (
    ApplicationRecord,
    ArtifactRecord,
    CandidateProfileRecord,
    JobRecord,
)
from career_companion.paths import CompanionPaths
from career_companion.schemas import (
    ApplicationStatus,
    CandidateProfile,
    ClaimStatus,
    JobSpec,
    ProfileClaim,
    ProfileProject,
)
from career_companion.services.applications import transition_application
from career_companion.services.audit import record_audit
from career_companion.services.rendering import render_one_page, report_json


def generate_application_pack(
    session: Session,
    application_id: str,
    paths: CompanionPaths,
    config: ProductConfig,
) -> dict[str, Any]:
    application = session.get(ApplicationRecord, application_id)
    if not application:
        raise LookupError("Application not found")
    if application.status not in {"approved", "tailoring"}:
        raise ValueError("Application must be approved before tailoring")
    if application.status == "approved":
        transition_application(session, application.id, ApplicationStatus.TAILORING)
    job = session.get(JobRecord, application.job_id)
    profile_record = session.scalar(
        select(CandidateProfileRecord).order_by(CandidateProfileRecord.updated_at.desc())
    )
    if not job or not profile_record:
        raise ValueError("A job and reviewed candidate profile are required")
    profile = CandidateProfile.model_validate({"id": profile_record.id, **profile_record.payload})
    spec = JobSpec.model_validate(job.normalized_spec)
    verified = [claim for claim in profile.claims if claim.status == ClaimStatus.VERIFIED]
    if not verified:
        raise ValueError("Verify at least one evidence-backed profile claim before tailoring")

    workspace = paths.artifacts / application.id
    workspace.mkdir(parents=True, exist_ok=True)
    version = _next_version(session, application.id, "cv")
    fit = _gap_analysis(verified, spec, config.adjacent_claims_allowed)

    cv_path = workspace / f"cv-v{version}.tex"
    cv_path.write_text(_cv_tex(profile, verified, spec, fit), encoding="utf-8")
    gap_path = workspace / f"gap-analysis-v{version}.md"
    gap_path.write_text(_gap_markdown(spec, fit), encoding="utf-8")
    ledger_path = workspace / f"honesty-ledger-v{version}.md"
    ledger_path.write_text(_ledger_markdown(verified), encoding="utf-8")
    study_path = workspace / f"skills-to-learn-v{version}.md"
    study_path.write_text(_study_markdown(fit["adjacent"]), encoding="utf-8")
    interview_path = workspace / f"interview-plan-v{version}.md"
    interview_path.write_text(
        _interview_markdown(spec, fit, profile.projects), encoding="utf-8"
    )

    report = render_one_page(cv_path, engine_executable=config.tectonic_executable)
    if not report.valid:
        raise RuntimeError("CV rendering needs user help: " + "; ".join(report.errors))
    metadata_path = workspace / f"render-report-v{version}.json"
    metadata_path.write_text(json.dumps(report_json(report), indent=2), encoding="utf-8")

    created = []
    for kind, path in [
        ("cv", Path(report.pdf_path)),
        ("gap_analysis", gap_path),
        ("honesty_ledger", ledger_path),
        ("interview_plan", interview_path),
    ]:
        record = ArtifactRecord(
            application_id=application.id,
            kind=kind,
            version=version,
            path=str(path),
            sha256=_sha256(path),
        )
        session.add(record)
        created.append(record)

    if job.tier == "A":
        cover_path = workspace / f"cover-letter-v{version}.md"
        cover_path.write_text(_cover_letter(profile, verified, spec), encoding="utf-8")
        record = ArtifactRecord(
            application_id=application.id,
            kind="cover_letter",
            version=version,
            path=str(cover_path),
            sha256=_sha256(cover_path),
        )
        session.add(record)
        created.append(record)

    transition_application(session, application.id, ApplicationStatus.READY)
    application.next_action = "Review and approve artifacts before filling the form"
    session.flush()
    record_audit(
        session,
        "application.pack_generated",
        subject_type="application",
        subject_id=application.id,
        payload={"version": version, "artifacts": [item.kind for item in created]},
    )
    return {
        "application_id": application.id,
        "version": version,
        "render": report_json(report),
        "artifacts": [
            {"id": item.id, "kind": item.kind, "path": item.path, "sha256": item.sha256}
            for item in created
        ],
        "skills_to_learn_path": str(study_path),
        "added_but_unverified_skills": fit["adjacent"],
    }


def approve_artifact(session: Session, artifact_id: str, sha256: str) -> ArtifactRecord:
    artifact = session.get(ArtifactRecord, artifact_id)
    if not artifact:
        raise LookupError("Artifact not found")
    if artifact.kind == "form_fill_preview":
        raise ValueError("Local form previews are not application attachments")
    if artifact.sha256 != sha256 or _sha256(Path(artifact.path)) != sha256:
        raise ValueError("Artifact changed after review")
    artifact.approved = True
    record_audit(
        session,
        "artifact.approved",
        subject_type="artifact",
        subject_id=artifact.id,
        payload={"sha256": sha256, "kind": artifact.kind},
    )
    return artifact


def _next_version(session: Session, application_id: str, kind: str) -> int:
    value = session.scalar(
        select(func.max(ArtifactRecord.version)).where(
            ArtifactRecord.application_id == application_id, ArtifactRecord.kind == kind
        )
    )
    return int(value or 0) + 1


def _gap_analysis(
    verified: list[ProfileClaim], spec: JobSpec, adjacent_allowed: bool
) -> dict[str, list[str]]:
    profile_text = " ".join(claim.value.casefold() for claim in verified)
    requirements = spec.requirements or _requirements_from_description(spec.description)
    strong: list[str] = []
    missing: list[str] = []
    adjacent: list[str] = []
    for requirement in requirements[:20]:
        keywords = [word for word in re.findall(r"[a-zA-Z][a-zA-Z+#.]{2,}", requirement.casefold())]
        if any(keyword in profile_text for keyword in keywords):
            strong.append(requirement)
        elif adjacent_allowed and _is_learnable(requirement):
            adjacent.append(requirement)
        else:
            missing.append(requirement)
    return {"strong": strong, "adjacent": adjacent, "missing": missing}


def _requirements_from_description(description: str) -> list[str]:
    sentences = re.split(r"(?:\n+|(?<=[.!?])\s+)", description)
    signals = ("require", "experience", "knowledge", "proficien", "skill", "degree", "familiar")
    return [
        sentence.strip(" -•\t")
        for sentence in sentences
        if any(s in sentence.casefold() for s in signals)
    ]


def _is_learnable(requirement: str) -> bool:
    risk = ("phd", "security clearance", "native german", "10+ years", "staff", "principal")
    return not any(item in requirement.casefold() for item in risk)


def _cv_tex(
    profile: CandidateProfile,
    verified: list[ProfileClaim],
    spec: JobSpec,
    fit: dict[str, list[str]],
) -> str:
    contact = [value for value in (profile.email, profile.phone) if value]
    contact.extend(
        claim.value for claim in verified if claim.key in {"email", "phone", "location"}
    )
    contact = list(dict.fromkeys(contact))
    substantive = [claim for claim in verified if claim.key not in {"email", "phone", "location"}]
    bullets = substantive[:8] or verified[:8]
    skills = sorted(
        {
            token
            for claim in verified
            for token in re.findall(
                r"\b(?:Python|Go|Java|C\+\+|PyTorch|SQL|Docker|Kubernetes|Redis|AWS|Azure)\b",
                claim.value,
                re.I,
            )
        }
    )
    title = _tex(profile.display_name or "Candidate")
    summary = _tex(
        f"Evidence-backed candidate targeting {spec.title} at {spec.company}, with "
        f"{len(fit['strong'])} directly matched requirements."
    )
    bullet_lines = "\n".join(f"\\item {_tex(claim.value)}" for claim in bullets)
    skill_line = _tex(", ".join(skills) or "See verified experience above")
    contact_line = _tex(" | ".join(contact))
    return f"""\\documentclass[10pt,a4paper]{{article}}
\\usepackage[margin=1.35cm]{{geometry}}
\\usepackage[hidelinks]{{hyperref}}
\\usepackage{{enumitem}}
\\usepackage{{titlesec}}
\\pagestyle{{empty}}
\\setlist[itemize]{{leftmargin=*,nosep}}
\\titleformat{{\\section}}{{\\bfseries\\large}}{{}}{{0pt}}{{}}[\\titlerule]
\\begin{{document}}
\\begin{{center}}
{{\\LARGE {title}}}\\\\
{contact_line}
\\end{{center}}
\\section{{Summary}}
{summary}
\\section{{Verified Experience and Evidence}}
\\begin{{itemize}}
{bullet_lines}
\\end{{itemize}}
\\section{{Skills}}
{skill_line}
\\section{{Target Role}}
{_tex(spec.title)} at {_tex(spec.company)}. Open to relocation.
\\end{{document}}
"""


def _gap_markdown(spec: JobSpec, fit: dict[str, list[str]]) -> str:
    sections = [f"# Gap analysis: {spec.company} {spec.title}", ""]
    for heading, key in [
        ("Strong match", "strong"),
        ("Partial or adjacent", "adjacent"),
        ("Missing", "missing"),
    ]:
        sections.extend([f"## {heading}", ""])
        sections.extend([f"- {item}" for item in fit[key]] or ["- None identified"])
        sections.append("")
    return "\n".join(sections)


def _ledger_markdown(verified: list[ProfileClaim]) -> str:
    lines = ["# Honesty ledger", "", "| Claim | Status | Evidence |", "|---|---|---|"]
    for claim in verified:
        sources = ", ".join(
            f"{e.source_name} p.{e.page}" if e.page else e.source_name for e in claim.evidence
        )
        lines.append(f"| {claim.value.replace('|', '/')} | verified | {sources} |")
    return "\n".join(lines) + "\n"


def _study_markdown(adjacent: list[str]) -> str:
    lines = ["# Skills to learn before interview", ""]
    if not adjacent:
        return "\n".join(lines + ["No adjacent skills were added.", ""])
    for skill in adjacent:
        lines.extend(
            [
                f"## {skill}",
                "",
                "- Learn path: official overview, guided tutorial, then targeted practice.",
                "- Mini-project: build a small demonstrator and explain its tradeoffs.",
                "- Interview check: prepare one defensible example and one limitation.",
                "",
            ]
        )
    return "\n".join(lines)


def _interview_markdown(
    spec: JobSpec,
    fit: dict[str, list[str]],
    projects: list[ProfileProject] | None = None,
) -> str:
    lines = [
        f"# Interview plan: {spec.company} {spec.title}",
        "",
        "## Evidence stories",
        *[f"- Prepare a STAR story for: {item}" for item in fit["strong"][:5]],
        "",
        "## Gaps to prepare",
        *[
            f"- Study and practice: {item}"
            for item in (fit["adjacent"] + fit["missing"])[:8]
        ],
        "",
    ]
    project_lines = _project_interview_lines(projects or [])
    if project_lines:
        lines.extend(["## Project deep dives", "", *project_lines, ""])
    lines.extend(
        [
            "## Practical questions",
            (
                "- Confirm role scope, team expectations, interview stages, "
                "and work-authorization support."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _project_interview_lines(projects: list[ProfileProject]) -> list[str]:
    lines: list[str] = []
    for project in projects[:4]:
        name = " ".join(project.name.split()) or "Linked project"
        questions = (
            project.analysis.interview_questions[:4]
            if project.analysis
            else [
                f"Walk through the architecture of {name} and one difficult trade-off.",
                f"How did you validate the outcome of {name}?",
            ]
        )
        lines.append(f"### {name}")
        lines.extend(
            f"- {question.strip()}" for question in questions if question.strip()
        )
        lines.append("")
    return lines


def _cover_letter(profile: CandidateProfile, verified: list[ProfileClaim], spec: JobSpec) -> str:
    evidence = "; ".join(claim.value for claim in verified[:3])
    return f"""# Cover letter

Dear {spec.company} hiring team,

I am applying for the {spec.title} role because its work aligns with the
problems I want to solve and the evidence I have built so far.

My most relevant evidence includes {evidence}. I would bring that same habit of
measurable, end-to-end execution to your team.

I am also candid about gaps. I prepare adjacent skills before interviews and
never present them as established expertise. That discipline lets me learn
quickly without overstating what I know.

Thank you for considering my application. I am open to relocation and would
welcome the chance to discuss the role.

Sincerely,
{profile.display_name or "Candidate"}
"""


def _tex(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in value)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
