"""Make the chat idempotency index UNIQUE (P1-1, review 2026-10-12).

The orchestrator's idempotency check was a SELECT followed by an INSERT
(TOCTOU): two concurrent requests carrying the same ``client_request_id``
both passed the probe and persisted duplicate answers (redundant PHI plus
a duplicate paid model call).  A database-level UNIQUE constraint on
``(session_id, metadata_json ->> 'client_request_id')`` is the final
defense; the loser of the race receives an IntegrityError and re-reads the
persisted answer instead of failing the request.

``0008`` created the same functional index without ``UNIQUE``.  Upgrade is
idempotent: drop the non-unique variant if present, then create the unique
one if absent.  JSON extraction evaluates to NULL for messages without a
``client_request_id``; NULL values never conflict under both PostgreSQL and
SQLite uniqueness semantics, so ordinary history rows are unaffected.
"""

from alembic import op
from sqlalchemy import inspect, text

# Keep the revision within Alembic's PostgreSQL ``version_num`` limit (32
# characters).  The descriptive migration filename remains unchanged.
revision = "0009_chat_idem_unique"
down_revision = "0008_add_chat_message_indexes"
branch_labels = None
depends_on = None

_TABLE = "chat_messages"
_OLD_INDEX = "ix_chat_messages_client_request_id"
# A new name avoids any dialect quirk around in-place index replacement and
# makes the unique variant trivially distinguishable in catalog queries.
_UNIQUE_INDEX = "ux_chat_messages_session_client_request_id"


def _existing_indexes(table: str) -> set[str]:
    """Same reflection strategy as 0008: inspector first, dialect catalog as
    fallback (SQLite omits expression indexes from ``get_indexes``)."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(table):
        return set()
    names = {index["name"] for index in inspector.get_indexes(table)}
    if bind.dialect.name == "postgresql":
        statement = text("SELECT indexname FROM pg_indexes WHERE tablename = :table")
    elif bind.dialect.name == "sqlite":
        statement = text(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :table"
        )
    else:  # pragma: no cover - other backends rely on reflection alone
        return names
    names.update(row[0] for row in bind.execute(statement, {"table": table}))
    return names


def upgrade() -> None:
    existing = _existing_indexes(_TABLE)
    if _OLD_INDEX in existing:
        op.drop_index(_OLD_INDEX, table_name=_TABLE)
    if _UNIQUE_INDEX not in existing:
        op.create_index(
            _UNIQUE_INDEX,
            _TABLE,
            ["session_id", text("(metadata_json ->> 'client_request_id')")],
            unique=True,
        )


def downgrade() -> None:
    existing = _existing_indexes(_TABLE)
    if _UNIQUE_INDEX in existing:
        op.drop_index(_UNIQUE_INDEX, table_name=_TABLE)
    if _OLD_INDEX not in existing:
        op.create_index(
            _OLD_INDEX,
            _TABLE,
            ["session_id", text("(metadata_json ->> 'client_request_id')")],
            unique=False,
        )
