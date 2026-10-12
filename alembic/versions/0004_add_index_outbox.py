"""Add IndexJob leases/retries and transactional Outbox events."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0004_add_index_outbox"
down_revision = "0003_add_index_jobs"
branch_labels = None
depends_on = None


def _add_index_job_columns(bind) -> None:
    inspector = inspect(bind)
    if not inspector.has_table("index_jobs"):
        return
    existing = {column["name"] for column in inspector.get_columns("index_jobs")}
    columns = {
        "worker_id": sa.Column("worker_id", sa.String(length=200), nullable=True),
        "lease_expires_at": sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        "next_retry_at": sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    }
    for name, column in columns.items():
        if name not in existing:
            op.add_column("index_jobs", column)
    if not any(index["name"] == "ix_index_jobs_lease" for index in inspect(bind).get_indexes("index_jobs")):
        op.create_index(
            "ix_index_jobs_lease",
            "index_jobs",
            ["status", "lease_expires_at", "next_retry_at"],
            unique=False,
        )


def upgrade() -> None:
    bind = op.get_bind()
    _add_index_job_columns(bind)
    if inspect(bind).has_table("outbox_events"):
        return
    op.create_table(
        "outbox_events",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("aggregate_id", sa.String(length=200), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_outbox_events_idempotency_key"),
    )
    op.create_index(
        "ix_outbox_events_status_retry",
        "outbox_events",
        ["status", "next_retry_at"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if inspector.has_table("outbox_events"):
        if any(index["name"] == "ix_outbox_events_status_retry" for index in inspector.get_indexes("outbox_events")):
            op.drop_index("ix_outbox_events_status_retry", table_name="outbox_events")
        op.drop_table("outbox_events")
    if inspector.has_table("index_jobs"):
        if any(index["name"] == "ix_index_jobs_lease" for index in inspector.get_indexes("index_jobs")):
            op.drop_index("ix_index_jobs_lease", table_name="index_jobs")
        existing = {column["name"] for column in inspect(bind).get_columns("index_jobs")}
        for name in ("next_retry_at", "lease_expires_at", "worker_id"):
            if name in existing:
                op.drop_column("index_jobs", name)
