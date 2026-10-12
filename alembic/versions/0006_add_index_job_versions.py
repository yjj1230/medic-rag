"""Add complete pipeline-version and embedding-dimension columns to index_jobs (Task 8).

Additive, nullable-only columns on ``index_jobs`` thread the full
:class:`VersionBundle` through the outbox/IndexJob seam.  Also creates the
eight-field composite unique index ``ix_index_jobs_rebuild_idempotency`` so a
change to any pipeline stage (parser/cleaner/chunker/extractor/embedding/index
version) yields a distinct rebuild identity.  The historical four-field
``uq_index_jobs_idempotency`` constraint is intentionally preserved for data
compatibility; duplicate delivery is de-duplicated by the outbox idempotency
key before a write, so the two constraints do not conflict on a reparse path
(which deletes prior jobs for a document version first).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0006_add_index_job_versions"
down_revision = "0005_document_quality_metadata"
branch_labels = None
depends_on = None


_INDEX_JOB_COLUMNS = {
    "cleaner_version": sa.Column("cleaner_version", sa.String(length=100), nullable=True),
    "chunker_version": sa.Column("chunker_version", sa.String(length=100), nullable=True),
    "extractor_version": sa.Column("extractor_version", sa.String(length=100), nullable=True),
    "embedding_dimensions": sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
}

# Column order must match IndexJob.idempotency_key in models.py.
_REBUILD_IDEMPOTENCY_COLUMNS = (
    "document_version_id",
    "evidence_unit_id",
    "parser_version",
    "cleaner_version",
    "chunker_version",
    "extractor_version",
    "embedding_model",
    "index_version",
)
_REBUILD_INDEX_NAME = "ix_index_jobs_rebuild_idempotency"


def _add_missing_columns(table: str, columns: dict[str, sa.Column]) -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(table):
        return
    existing = {column["name"] for column in inspector.get_columns(table)}
    for name, column in columns.items():
        if name not in existing:
            op.add_column(table, column)


def _create_rebuild_index(table: str) -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(table):
        return
    existing = {index["name"] for index in inspector.get_indexes(table)}
    if _REBUILD_INDEX_NAME not in existing:
        op.create_index(_REBUILD_INDEX_NAME, table, list(_REBUILD_IDEMPOTENCY_COLUMNS), unique=True)


def upgrade() -> None:
    _add_missing_columns("index_jobs", _INDEX_JOB_COLUMNS)
    _create_rebuild_index("index_jobs")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if inspector.has_table("index_jobs"):
        indexes = {index["name"] for index in inspector.get_indexes("index_jobs")}
        if _REBUILD_INDEX_NAME in indexes:
            op.drop_index(_REBUILD_INDEX_NAME, table_name="index_jobs")
        existing = {column["name"] for column in inspector.get_columns("index_jobs")}
        for name in _INDEX_JOB_COLUMNS:
            if name in existing:
                op.drop_column("index_jobs", name)