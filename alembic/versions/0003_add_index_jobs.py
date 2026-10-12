"""Add durable private-index jobs and idempotency fields."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0003_add_index_jobs"
down_revision = "0002_add_password_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # 0001 creates metadata dynamically in this project; checkfirst keeps a
    # fresh migration run compatible with that behavior while still upgrading
    # databases created before IndexJob existed.
    inspector = inspect(bind)
    if inspector.has_table("index_jobs"):
        existing_columns = {column["name"] for column in inspector.get_columns("index_jobs")}
        required_columns = {
            "id",
            "document_version_id",
            "evidence_unit_id",
            "status",
            "index_version",
            "parser_version",
            "embedding_model",
            "attempts",
            "error_code",
            "started_at",
            "finished_at",
            "created_at",
            "updated_at",
        }
        missing = required_columns - existing_columns
        if missing:
            raise RuntimeError(
                "index_jobs exists but is missing required columns: " + ", ".join(sorted(missing))
            )
        return
    op.create_table(
        "index_jobs",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_unit_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("index_version", sa.String(length=100), nullable=False),
        sa.Column("parser_version", sa.String(length=100), nullable=False),
        sa.Column("embedding_model", sa.String(length=200), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"]),
        sa.ForeignKeyConstraint(["evidence_unit_id"], ["evidence_units.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_version_id",
            "evidence_unit_id",
            "parser_version",
            "embedding_model",
            name="uq_index_jobs_idempotency",
        ),
    )
    op.create_index("ix_index_jobs_status", "index_jobs", ["status"], unique=False)
    op.create_index(
        "ix_index_jobs_document_version", "index_jobs", ["document_version_id"], unique=False
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table("index_jobs"):
        return
    op.drop_index("ix_index_jobs_document_version", table_name="index_jobs")
    op.drop_index("ix_index_jobs_status", table_name="index_jobs")
    op.drop_table("index_jobs")
