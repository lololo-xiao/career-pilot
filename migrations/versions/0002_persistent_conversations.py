"""Persist conversation sessions and the local agent identity.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("soul", sa.Text(), nullable=False),
        sa.Column("active_session_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "conversation_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("title_is_custom", sa.Boolean(), nullable=False),
        sa.Column("candidate_profile", sa.Text(), nullable=True),
        sa.Column("job_description", sa.Text(), nullable=True),
        sa.Column("uploaded_filename", sa.String(length=255), nullable=True),
        sa.Column("match_report", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["conversation_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_conversation_messages_session_id"),
        "conversation_messages",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversation_messages_created_at"),
        "conversation_messages",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_conversation_messages_created_at"),
        table_name="conversation_messages",
    )
    op.drop_index(
        op.f("ix_conversation_messages_session_id"),
        table_name="conversation_messages",
    )
    op.drop_table("conversation_messages")
    op.drop_table("conversation_sessions")
    op.drop_table("agent_profiles")
