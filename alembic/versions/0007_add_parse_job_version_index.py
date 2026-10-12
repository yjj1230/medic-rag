"""Add a document-version index on parse_jobs (Task 11 n3b).

The ``parse_jobs.document_version_id`` column already exists (initial schema);
this migration only adds the missing non-unique index that the parsing→indexing
handoff queries on.  Both ``indexing/orchestrator`` and ``retrieval/indexing``
look up the latest ParseJob for a ``document_version_id`` (with a SQL
``ORDER BY created_at DESC``), so an index removes a per-evidence full-table
scan.  Idempotent and additive — no destructive operations.
"""

from alembic import op
from sqlalchemy import inspect

revision = "0007_add_parse_job_version_index"
down_revision = "0006_add_index_job_versions"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_parse_jobs_document_version"
_TABLE = "parse_jobs"
_COLUMNS = ["document_version_id"]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    existing = {index["name"] for index in inspector.get_indexes(_TABLE)}
    if _INDEX_NAME not in existing:
        op.create_index(_INDEX_NAME, _TABLE, _COLUMNS, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    existing = {index["name"] for index in inspector.get_indexes(_TABLE)}
    if _INDEX_NAME in existing:
        op.drop_index(_INDEX_NAME, table_name=_TABLE)
