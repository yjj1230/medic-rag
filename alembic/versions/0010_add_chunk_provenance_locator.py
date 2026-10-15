"""Persist the structured source locator for evidence and knowledge chunks."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0010_chunk_locator"
down_revision = "0009_chat_idem_unique"
branch_labels = None
depends_on = None


def _add_missing(table: str) -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(table):
        return
    existing = {column["name"] for column in inspector.get_columns(table)}
    if "locator" not in existing:
        # Build a fresh Column for each table; SQLAlchemy Column objects are
        # associated with their first table when attached by an operation.
        op.add_column(table, sa.Column("locator", sa.JSON(), nullable=True))


def upgrade() -> None:
    _add_missing("evidence_units")
    _add_missing("knowledge_chunks")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    for table in ("knowledge_chunks", "evidence_units"):
        if not inspector.has_table(table):
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        if "locator" in existing:
            op.drop_column(table, "locator")
