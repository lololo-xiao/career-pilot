from __future__ import annotations

import hashlib
import secrets
import shutil
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career_companion.config import ProductConfig, save_config
from career_companion.database import (
    AgentProfileRecord,
    ApplicationRecord,
    ApprovalRecord,
    AuditEventRecord,
    CandidateProfileRecord,
    ConversationMessageRecord,
    ConversationSessionRecord,
    JobRecord,
    SourceDocumentRecord,
    StatusEventRecord,
)
from career_companion.paths import CompanionPaths
from career_companion.persistence import account_session, clear_factory_cache
from career_companion.schemas import (
    CandidateProfile,
    ClaimStatus,
    EvidenceReference,
    Job,
    JobSpec,
    ProfileClaim,
    ProfileProject,
)
from career_companion.services.jobs import fingerprint_job
from career_companion.workspace import initialize_account_workspace

DEMO_MARKER_NAME = ".careerpilot-fictional-demo-v1"
DEMO_MARKER_CONTENT = "CareerPilot fictional demo workspace\nversion=1\n"

_SOURCE_ID = "00000000-0000-4000-8000-000000000001"
_PROFILE_ID = "00000000-0000-4000-8000-000000000002"
_APPROVAL_ID = "00000000-0000-4000-8000-000000000003"
_CONVERSATION_ID = "00000000-0000-4000-8000-000000000004"
_AUDIT_ID = "00000000-0000-4000-8000-000000000005"
_AGENT_PROFILE_ID = "primary"


class DemoWorkspaceError(RuntimeError):
    """Raised when the requested demo home cannot be handled safely."""


@dataclass(frozen=True)
class DemoSeedResult:
    home: Path
    account_paths: CompanionPaths
    profiles: int
    jobs: int
    applications: int
    conversations: int


@dataclass(frozen=True)
class _JobSeed:
    id: str
    slug: str
    company: str
    title: str
    location: str
    workplace: str
    company_size: str
    description: str
    requirements: tuple[str, ...]
    score: float
    tier: str
    status: str
    next_action: str


_JOBS = (
    _JobSeed(
        id="10000000-0000-4000-8000-000000000001",
        slug="northstar-ai-engineer",
        company="Northstar Storyworks (Fictional)",
        title="AI Engineer",
        location="Paris, France",
        workplace="hybrid",
        company_size="51-200",
        description=(
            "Fictional demo role building Python retrieval services, evaluation "
            "pipelines, and human-reviewed AI workflows."
        ),
        requirements=("Python", "retrieval", "evaluation", "FastAPI"),
        score=93,
        tier="A",
        status="ready",
        next_action="Review the fictional application pack",
    ),
    _JobSeed(
        id="10000000-0000-4000-8000-000000000002",
        slug="blue-harbor-ml-platform",
        company="Blue Harbor Robotics (Fictional)",
        title="ML Platform Engineer",
        location="Berlin, Germany",
        workplace="remote",
        company_size="201-500",
        description=(
            "Fictional demo role operating observable model services and safe data "
            "pipelines for robotics teams."
        ),
        requirements=("Python", "cloud platforms", "observability", "SQL"),
        score=86,
        tier="A",
        status="submitted",
        next_action="Rehearse a fictional follow-up",
    ),
    _JobSeed(
        id="10000000-0000-4000-8000-000000000003",
        slug="emberline-applied-ai",
        company="Emberline Systems (Fictional)",
        title="Applied AI Engineer",
        location="Amsterdam, Netherlands",
        workplace="hybrid",
        company_size="11-50",
        description=(
            "Fictional demo role prototyping grounded assistants with measurable "
            "quality and careful product handoffs."
        ),
        requirements=("LLM evaluation", "product engineering", "TypeScript"),
        score=79,
        tier="B",
        status="interview_1",
        next_action="Practice fictional interview stories",
    ),
    _JobSeed(
        id="10000000-0000-4000-8000-000000000004",
        slug="cedar-peak-data",
        company="Cedar Peak Analytics (Fictional)",
        title="Data Product Engineer",
        location="Lisbon, Portugal",
        workplace="remote",
        company_size="51-200",
        description=(
            "Fictional demo role turning analytics prototypes into reliable, "
            "well-tested internal products."
        ),
        requirements=("Python", "SQL", "data modeling", "stakeholder communication"),
        score=72,
        tier="B",
        status="offer",
        next_action="Compare the fictional offer scenario",
    ),
)


def default_demo_home(production_paths: CompanionPaths | None = None) -> Path:
    """Return a sibling data root that cannot overlap the normal installation."""

    production_paths = production_paths or CompanionPaths.discover()
    return production_paths.root.with_name(f"{production_paths.root.name}-demo")


def seed_demo_home(
    home: Path | None = None,
    *,
    reset: bool = False,
    production_paths: CompanionPaths | None = None,
    today: date | None = None,
) -> DemoSeedResult:
    """Seed a separate, provider-free home with visibly fictional local data."""

    production_paths = production_paths or CompanionPaths.discover()
    root = (home or default_demo_home(production_paths)).expanduser().resolve()
    _reject_overlapping_home(root, production_paths.root.resolve())
    _reject_overlapping_home(root, production_paths.config.parent.resolve())
    demo_paths = CompanionPaths.at_root(root, root / "config")
    _prepare_demo_home(demo_paths, reset=reset)
    _reject_demo_path_symlinks(demo_paths)
    _reject_stored_credentials(demo_paths.auth_database)

    if not demo_paths.config.exists():
        save_config(ProductConfig(), demo_paths)
    secret = _ensure_auth_secret(demo_paths)

    # AuthStore creates only the local device identity here. The credential-table
    # check above guarantees this call cannot decrypt or load a model credential.
    from app.auth import AuthStore

    account = AuthStore(demo_paths.auth_database, secret).ensure_local_account()
    if account.active_provider is not None or account.provider_connection is not None:
        raise DemoWorkspaceError(
            "The demo home unexpectedly selected a provider; use --reset to recreate it."
        )
    account_paths = demo_paths.scoped_to(account.user_id)
    if not account_paths.root.is_relative_to(demo_paths.root / "accounts"):
        raise DemoWorkspaceError("The demo account workspace must remain inside the demo home.")
    initialize_account_workspace(account_paths)
    _seed_records(account_paths, today=today or date.today())

    with account_session(account_paths) as session:
        return DemoSeedResult(
            home=demo_paths.root,
            account_paths=account_paths,
            profiles=_count(session, CandidateProfileRecord),
            jobs=_count(session, JobRecord),
            applications=_count(session, ApplicationRecord),
            conversations=_count(session, ConversationSessionRecord),
        )


def _reject_overlapping_home(demo_root: Path, production_root: Path) -> None:
    if (
        demo_root == production_root
        or demo_root in production_root.parents
        or production_root in demo_root.parents
    ):
        raise DemoWorkspaceError(
            "The demo home must be separate from, and not contain, the normal data home."
        )


def _prepare_demo_home(paths: CompanionPaths, *, reset: bool) -> None:
    root = paths.root
    if root.exists() and not root.is_dir():
        raise DemoWorkspaceError("The demo home must be a directory.")
    marker = root / DEMO_MARKER_NAME
    marked = False
    if marker.is_file() and not marker.is_symlink():
        marked = marker.read_text(encoding="utf-8") == DEMO_MARKER_CONTENT

    if reset:
        if not marked:
            raise DemoWorkspaceError(
                "Refusing to reset an unmarked directory; choose the existing demo home."
            )
        clear_factory_cache()
        shutil.rmtree(root)
    elif root.exists() and not marked and any(root.iterdir()):
        raise DemoWorkspaceError(
            "Refusing to seed a non-empty directory that is not a CareerPilot demo home."
        )

    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(DEMO_MARKER_CONTENT, encoding="utf-8")


def _reject_demo_path_symlinks(paths: CompanionPaths) -> None:
    protected_paths = (
        paths.root / "accounts",
        paths.root / "config",
        paths.auth_database,
        paths.auth_secret,
        paths.config,
    )
    if any(path.is_symlink() for path in protected_paths):
        raise DemoWorkspaceError("Demo authentication and workspace paths cannot be symlinks.")


def _reject_stored_credentials(database: Path) -> None:
    if not database.is_file():
        return
    try:
        with sqlite3.connect(database) as connection:
            table_names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            for table in ("provider_connections", "service_credentials"):
                if table in table_names:
                    count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    if count:
                        raise DemoWorkspaceError(
                            "The demo home contains stored credentials; use --reset so the "
                            "seeder never reads them."
                        )
            if "accounts" in table_names:
                active = connection.execute(
                    "SELECT COUNT(*) FROM accounts WHERE active_provider IS NOT NULL"
                ).fetchone()[0]
                if active:
                    raise DemoWorkspaceError(
                        "The demo home has an active provider; use --reset before seeding."
                    )
    except sqlite3.DatabaseError as exc:
        raise DemoWorkspaceError("The demo authentication database is invalid.") from exc


def _ensure_auth_secret(paths: CompanionPaths) -> str:
    paths.create()
    if paths.auth_secret.is_file():
        secret = paths.auth_secret.read_text(encoding="utf-8").strip()
        if len(secret) >= 32:
            return secret
    secret = secrets.token_urlsafe(48)
    paths.auth_secret.write_text(secret, encoding="utf-8")
    with suppress(OSError):
        paths.auth_secret.chmod(0o600)
    return secret


def _seed_records(paths: CompanionPaths, *, today: date) -> None:
    profile_text = (
        "FICTIONAL MEETUP DEMO PROFILE\n"
        "Maya Laurent is an invented candidate. No person or employer is represented.\n"
        "Built Python retrieval services, evaluation pipelines, and observable APIs.\n"
        "Led a fictional migration that reduced demo incident recovery time by 35%.\n"
    )
    profile_path = paths.imports / "fictional-demo-profile.txt"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(profile_text, encoding="utf-8")
    profile_digest = hashlib.sha256(profile_text.encode("utf-8")).hexdigest()
    anchored_at = datetime.combine(today, time(hour=10), tzinfo=UTC)

    evidence = EvidenceReference(
        source_id=_SOURCE_ID,
        source_name="fictional-demo-profile.txt",
        page=1,
        excerpt="Built Python retrieval services, evaluation pipelines, and observable APIs.",
        content_hash=profile_digest,
    )
    profile = CandidateProfile(
        id=_PROFILE_ID,
        display_name="Maya Laurent — FICTIONAL DEMO",
        seniority="mid",
        email="maya.laurent@careerpilot-demo.invalid",
        claims=[
            ProfileClaim(
                key="experience",
                value="Built Python retrieval services and observable APIs",
                status=ClaimStatus.VERIFIED,
                evidence=[evidence],
                user_verified_at=anchored_at,
            ),
            ProfileClaim(
                key="achievement",
                value="Reduced fictional demo incident recovery time by 35%",
                status=ClaimStatus.VERIFIED,
                evidence=[evidence],
                user_verified_at=anchored_at,
            ),
            ProfileClaim(
                key="skill",
                value="Kubernetes (learning goal for this fictional scenario)",
                status=ClaimStatus.LEARNING,
                confidence=0.5,
            ),
        ],
        target_roles=["AI Engineer", "ML Platform Engineer"],
        preferred_locations=["Paris", "Remote in Europe"],
        languages=["French (Native / bilingual)", "English (CEFR C1)"],
        projects=[
            ProfileProject(
                id="fictional-project-queue-lab",
                name="Queue Lab (Fictional Demo)",
                description="A synthetic job-ranking and evaluation project for meetup rehearsal.",
                repository_url="https://code.careerpilot-demo.invalid/maya/queue-lab",
                technologies=["Python", "FastAPI", "SQLAlchemy"],
                highlights=["Uses only synthetic fixtures", "Has deterministic evaluation cases"],
            )
        ],
        source_documents=[_SOURCE_ID],
    )

    with account_session(paths) as session:
        source = session.get(SourceDocumentRecord, _SOURCE_ID)
        if source is None:
            source = SourceDocumentRecord(id=_SOURCE_ID)
            session.add(source)
        source.filename = "fictional-demo-profile.txt"
        source.stored_path = str(profile_path)
        source.sha256 = profile_digest
        source.media_type = "text/plain"
        source.extracted_text = profile_text

        profile_record = session.get(CandidateProfileRecord, _PROFILE_ID)
        if profile_record is None:
            profile_record = CandidateProfileRecord(id=_PROFILE_ID)
            session.add(profile_record)
        profile_record.display_name = profile.display_name
        profile_record.payload = profile.model_dump(
            mode="json", exclude={"id", "updated_at"}
        )

        for offset, seed in enumerate(_JOBS, start=1):
            spec = JobSpec(
                title=seed.title,
                company=seed.company,
                locations=[seed.location],
                description=seed.description,
                requirements=list(seed.requirements),
                seniority="mid",
                employment_type="full-time",
                workplace_type=seed.workplace,
                company_size=seed.company_size,
                posted_date=today - timedelta(days=offset),
                source_url=f"https://jobs.careerpilot-demo.invalid/{seed.slug}",
                source_type="fictional-demo",
            )
            canonical_url = str(spec.source_url)
            job_payload = Job(spec=spec, canonical_url=canonical_url)
            job = session.get(JobRecord, seed.id)
            if job is None:
                job = JobRecord(id=seed.id)
                session.add(job)
            job.company = seed.company
            job.title = seed.title
            job.canonical_url = canonical_url
            job.fingerprint = fingerprint_job(spec, canonical_url)
            job.source_type = "fictional-demo"
            job.raw_payload = job_payload.model_dump(mode="json")
            job.normalized_spec = spec.model_dump(mode="json")
            job.score = seed.score
            job.tier = seed.tier
            job.score_explanation = [
                "Fictional deterministic score for meetup rehearsal",
                f"Matches {', '.join(seed.requirements[:2])}",
            ]

            application_id = seed.id.replace("10000000", "20000000", 1)
            application = session.get(ApplicationRecord, application_id)
            if application is None:
                application = ApplicationRecord(id=application_id, job_id=seed.id)
                session.add(application)
            application.job_id = seed.id
            application.status = seed.status
            application.next_action = seed.next_action
            application.next_action_at = anchored_at + timedelta(days=offset)
            application.submitted_at = (
                anchored_at - timedelta(days=offset)
                if seed.status in {"submitted", "interview_1", "offer"}
                else None
            )

            event_id = seed.id.replace("10000000", "30000000", 1)
            event = session.get(StatusEventRecord, event_id)
            if event is None:
                event = StatusEventRecord(id=event_id, application_id=application_id)
                session.add(event)
            event.application_id = application_id
            event.from_status = "discovered"
            event.to_status = seed.status
            event.note = (
                "FICTIONAL DEMO HISTORY — no application was sent and no employer was contacted."
            )
            event.created_at = anchored_at - timedelta(days=offset)

        approval = session.get(ApprovalRecord, _APPROVAL_ID)
        if approval is None:
            approval = ApprovalRecord(id=_APPROVAL_ID)
            session.add(approval)
        approval.action_type = "email.draft"
        approval.payload_digest = hashlib.sha256(b"fictional-demo-follow-up").hexdigest()
        approval.preview = {
            "fictional": True,
            "summary": "Rehearse reviewing a fictional follow-up draft; sends nothing.",
        }
        approval.expires_at = anchored_at + timedelta(days=30)
        approval.decision = "pending"
        approval.decided_at = None

        agent = session.get(AgentProfileRecord, _AGENT_PROFILE_ID)
        if agent is None:
            agent = AgentProfileRecord(id=_AGENT_PROFILE_ID)
            session.add(agent)
        agent.name = "Pilot"
        agent.soul = ""
        agent.active_session_id = _CONVERSATION_ID

        conversation = session.get(ConversationSessionRecord, _CONVERSATION_ID)
        if conversation is None:
            conversation = ConversationSessionRecord(id=_CONVERSATION_ID)
            session.add(conversation)
        conversation.title = "Meetup rehearsal — fictional data"
        conversation.title_is_custom = True
        conversation.candidate_profile = profile_text
        conversation.job_description = _JOBS[0].description
        conversation.uploaded_filename = "fictional-demo-profile.txt"
        conversation.match_report = None

        messages = (
            (
                "50000000-0000-4000-8000-000000000001",
                1,
                "assistant",
                (
                    "This is a local rehearsal workspace. Every candidate, company, "
                    "and outcome is fictional."
                ),
            ),
            (
                "50000000-0000-4000-8000-000000000002",
                2,
                "user",
                "Show me why the fictional Northstar role leads the queue.",
            ),
            (
                "50000000-0000-4000-8000-000000000003",
                3,
                "assistant",
                (
                    "The static demo score favors Python, retrieval, evaluation, and "
                    "API evidence. No model was called to create this rehearsal data."
                ),
            ),
        )
        for message_id, position, role, content in messages:
            message = session.get(ConversationMessageRecord, message_id)
            if message is None:
                message = ConversationMessageRecord(
                    id=message_id,
                    session_id=_CONVERSATION_ID,
                    position=position,
                    role=role,
                    content=content,
                )
                session.add(message)
            message.session_id = _CONVERSATION_ID
            message.position = position
            message.role = role
            message.content = content
            message.report = None
            message.created_at = anchored_at + timedelta(minutes=position)

        audit = session.get(AuditEventRecord, _AUDIT_ID)
        if audit is None:
            audit = AuditEventRecord(id=_AUDIT_ID)
            session.add(audit)
        audit.event_type = "demo.workspace_seeded"
        audit.actor = "fictional-demo-seeder"
        audit.subject_type = "demo_workspace"
        audit.subject_id = "fictional-v1"
        audit.payload = {
            "fictional": True,
            "provider_called": False,
            "jobs": len(_JOBS),
            "applications": len(_JOBS),
        }
        audit.created_at = anchored_at


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)
