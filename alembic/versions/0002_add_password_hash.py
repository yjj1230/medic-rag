"""Add password_hash to patients.

Guarded with an ``inspect()`` check, matching 0003-0006.  ``0001`` builds the
schema from ``Base.metadata`` (the *current* models), which already declares
``patients.password_hash``; an unconditional ``add_column`` therefore aborted a
fresh ``alembic upgrade head`` with::

    sqlite3.OperationalError: duplicate column name: password_hash

which made the project impossible to provision from an empty database.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0002_add_password_hash"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def _existing_columns() -> set[str] | None:
    """Column names on ``patients``, or ``None`` when the table is absent."""
    inspector = inspect(op.get_bind())
    if not inspector.has_table("patients"):
        return None
    return {column["name"] for column in inspector.get_columns("patients")}


def upgrade() -> None:
    existing = _existing_columns()
    if existing is None or "password_hash" in existing:
        return
    op.add_column("patients", sa.Column("password_hash", sa.String(128), nullable=True))


def downgrade() -> None:
    existing = _existing_columns()
    if existing is None or "password_hash" not in existing:
        return
    op.drop_column("patients", "password_hash")
