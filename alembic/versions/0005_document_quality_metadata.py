"""Add expanded evidence/fact provenance and review-state columns (Task 5).

Additive, nullable-only columns on ``evidence_units`` and ``medical_facts``.
``retrieval_eligible`` and ``review_status`` are NOT NULL and therefore receive a
server default so existing rows stay valid.  No destructive operations.

The revision id is intentionally 29 characters.  Alembic stores it in
``alembic_version.version_num``, which is ``VARCHAR(32)``; the previous id
(``0005_add_document_quality_metadata``, 34 characters) made ``alembic upgrade
head`` abort on PostgreSQL with ``value too long for type character varying(32)``.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0005_document_quality_metadata"
down_revision = "0004_add_index_outbox"
branch_labels = None
depends_on = None


_EVIDENCE_UNIT_COLUMNS = {
    "raw_text": sa.Column("raw_text", sa.Text(), nullable=True),
    "clean_text": sa.Column("clean_text", sa.Text(), nullable=True),
    # section_path is a JSON list of section labels.
    "section_path": sa.Column("section_path", sa.JSON(), nullable=True),
    "parent_block_id": sa.Column("parent_block_id", sa.String(length=200), nullable=True),
    "chunk_index": sa.Column("chunk_index", sa.Integer(), nullable=True),
    "parser_version": sa.Column("parser_version", sa.String(length=100), nullable=True),
    "cleaner_version": sa.Column("cleaner_version", sa.String(length=100), nullable=True),
    "chunker_version": sa.Column("chunker_version", sa.String(length=100), nullable=True),
    # quality_flags is a JSON list of cleaning/chunking flag strings.
    "quality_flags": sa.Column("quality_flags", sa.JSON(), nullable=True),
    "retrieval_eligible": sa.Column(
        "retrieval_eligible", sa.Boolean(), nullable=False, server_default=sa.text("1")
    ),
}

_MEDICAL_FACT_COLUMNS = {
    "canonical_name": sa.Column("canonical_name", sa.String(length=200), nullable=True),
    "raw_name": sa.Column("raw_name", sa.String(length=200), nullable=True),
    "value_number": sa.Column("value_number", sa.Float(), nullable=True),
    "unit_raw": sa.Column("unit_raw", sa.String(length=100), nullable=True),
    "unit_normalized": sa.Column("unit_normalized", sa.String(length=100), nullable=True),
    "reference_low": sa.Column("reference_low", sa.Float(), nullable=True),
    "reference_high": sa.Column("reference_high", sa.Float(), nullable=True),
    "extractor_version": sa.Column("extractor_version", sa.String(length=100), nullable=True),
    "review_status": sa.Column(
        "review_status", sa.String(length=30), nullable=False, server_default="ACCEPTED"
    ),
}


def _add_missing(table: str, columns: dict[str, sa.Column]) -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(table):
        return
    existing = {column["name"] for column in inspector.get_columns(table)}
    for name, column in columns.items():
        if name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    _add_missing("evidence_units", _EVIDENCE_UNIT_COLUMNS)
    _add_missing("medical_facts", _MEDICAL_FACT_COLUMNS)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    for table, columns in (
        ("evidence_units", _EVIDENCE_UNIT_COLUMNS),
        ("medical_facts", _MEDICAL_FACT_COLUMNS),
    ):
        if not inspector.has_table(table):
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name in columns:
            if name in existing:
                op.drop_column(table, name)