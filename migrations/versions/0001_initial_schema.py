"""Initial Career Companion schema."""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from career_companion.database import Base

    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    from career_companion.database import Base

    Base.metadata.drop_all(op.get_bind())
