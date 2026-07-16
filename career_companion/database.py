from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy import event
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from career_companion.paths import CompanionPaths


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CandidateProfileRecord(Base, TimestampMixin):
    __tablename__ = "candidate_profiles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    display_name: Mapped[str] = mapped_column(String(200), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class SourceDocumentRecord(Base, TimestampMixin):
    __tablename__ = "source_documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    filename: Mapped[str] = mapped_column(String(500))
    stored_path: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    media_type: Mapped[str] = mapped_column(String(100))
    extracted_text: Mapped[str] = mapped_column(Text, default="")


class JobRecord(Base, TimestampMixin):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    company: Mapped[str] = mapped_column(String(300), index=True)
    title: Mapped[str] = mapped_column(String(500), index=True)
    canonical_url: Mapped[str] = mapped_column(String(2000), default="")
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source_type: Mapped[str] = mapped_column(String(100), default="manual")
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    normalized_spec: Mapped[dict] = mapped_column(JSON, default=dict)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    tier: Mapped[str | None] = mapped_column(String(8), nullable=True)
    score_explanation: Mapped[list] = mapped_column(JSON, default=list)
    applications: Mapped[list[ApplicationRecord]] = relationship(back_populates="job")


class ApplicationRecord(Base, TimestampMixin):
    __tablename__ = "applications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    status: Mapped[str] = mapped_column(String(50), default="discovered", index=True)
    next_action: Mapped[str] = mapped_column(String(1000), default="Review fit")
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    job: Mapped[JobRecord] = relationship(back_populates="applications")
    artifacts: Mapped[list[ArtifactRecord]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    events: Mapped[list[StatusEventRecord]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )


class ArtifactRecord(Base, TimestampMixin):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    kind: Mapped[str] = mapped_column(String(50))
    version: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64))
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    application: Mapped[ApplicationRecord] = relationship(back_populates="artifacts")


class StatusEventRecord(Base):
    __tablename__ = "application_status_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    from_status: Mapped[str] = mapped_column(String(50))
    to_status: Mapped[str] = mapped_column(String(50))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    application: Mapped[ApplicationRecord] = relationship(back_populates="events")


class ApprovalRecord(Base, TimestampMixin):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    action_type: Mapped[str] = mapped_column(String(100), index=True)
    payload_digest: Mapped[str] = mapped_column(String(64), index=True)
    preview: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RevisionRecord(Base, TimestampMixin):
    __tablename__ = "revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kind: Mapped[str] = mapped_column(String(30), index=True)
    name: Mapped[str] = mapped_column(String(300), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    diff: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(200))
    source_session: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    evaluation: Mapped[dict] = mapped_column(JSON, default=dict)


class ModelRouteRecord(Base, TimestampMixin):
    __tablename__ = "model_routes"
    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(200))
    reasoning_effort: Mapped[str] = mapped_column(String(20), default="low")
    token_limit: Mapped[int] = mapped_column(Integer, default=4000)
    cost_budget_usd: Mapped[float] = mapped_column(Float, default=0.25)
    fallback_policy: Mapped[str] = mapped_column(String(30), default="none")
    fallback_route: Mapped[str | None] = mapped_column(String(50), nullable=True)
    scheduled: Mapped[bool] = mapped_column(Boolean, default=False)


class UsageRunRecord(Base):
    __tablename__ = "usage_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    route: Mapped[str] = mapped_column(String(50), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(200))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class ScheduleRecord(Base, TimestampMixin):
    __tablename__ = "schedules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200), unique=True)
    cron: Mapped[str] = mapped_column(String(100))
    route: Mapped[str] = mapped_column(ForeignKey("model_routes.name"))
    task: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MCPServerRecord(Base, TimestampMixin):
    __tablename__ = "mcp_servers"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    transport: Mapped[str] = mapped_column(String(20))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditEventRecord(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    actor: Mapped[str] = mapped_column(String(100), default="local-user")
    subject_type: Mapped[str] = mapped_column(String(100), default="")
    subject_id: Mapped[str] = mapped_column(String(100), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


def build_engine(paths: CompanionPaths | None = None):
    paths = paths or CompanionPaths.discover()
    paths.create()
    engine = create_engine(
        f"sqlite:///{paths.database}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

    return engine


def initialize_database(paths: CompanionPaths | None = None) -> None:
    engine = build_engine(paths)
    Base.metadata.create_all(engine)


def session_factory(paths: CompanionPaths | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=build_engine(paths), autoflush=False, expire_on_commit=False)


def session_dependency(paths: CompanionPaths | None = None) -> Generator[Session, None, None]:
    factory = session_factory(paths)
    with factory() as session:
        yield session
