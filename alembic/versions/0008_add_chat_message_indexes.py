"""Add the missing chat history / idempotency indexes.

``chat_messages`` and ``chat_sessions`` are the hottest tables in the service
and were the only ones in the schema without indexes.  Both answer-path queries
degenerated into sequential scans:

* history replay — ``WHERE session_id = ? ORDER BY created_at DESC LIMIT n``;
* the retry guard — ``metadata_json ->> 'client_request_id' = ?``.

The second index is functional: it indexes the extracted JSON value rather than
the column, so the predicate is served by the index instead of a per-row JSON
unpack.  ``text()`` is used so Alembic renders the dialect's own operator
(``->>``) on both PostgreSQL and SQLite.

Idempotent and additive — no destructive operations.
"""

from alembic import op
from sqlalchemy import inspect, text

revision = "0008_add_chat_message_indexes"
down_revision = "0007_add_parse_job_version_index"
branch_labels = None
depends_on = None

_SESSION_TABLE = "chat_sessions"
_SESSION_INDEX = "ix_chat_sessions_tenant_patient"
_SESSION_COLUMNS = ["tenant_id", "patient_id"]

_MESSAGE_TABLE = "chat_messages"
_MESSAGE_SESSION_INDEX = "ix_chat_messages_session_created"
_MESSAGE_SESSION_COLUMNS = ["session_id", "created_at", "id"]
_MESSAGE_REQUEST_INDEX = "ix_chat_messages_client_request_id"


def _existing_indexes(table: str) -> set[str]:
    """Index names on ``table``.

    Reflection alone is not enough: SQLite reports expression-based indexes as
    "unsupported reflection" and omits them from ``get_indexes``, so an existing
    functional index would look absent and ``CREATE INDEX`` would fail with
    "already exists".  The dialect's own catalog is queried as a fallback —
    ``pg_indexes`` on PostgreSQL (the deployment target) and ``sqlite_master``
    on the SQLite database this suite runs against.
    """
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
    if _SESSION_INDEX not in _existing_indexes(_SESSION_TABLE):
        op.create_index(_SESSION_INDEX, _SESSION_TABLE, _SESSION_COLUMNS, unique=False)

    existing = _existing_indexes(_MESSAGE_TABLE)
    if _MESSAGE_SESSION_INDEX not in existing:
        op.create_index(
            _MESSAGE_SESSION_INDEX, _MESSAGE_TABLE, _MESSAGE_SESSION_COLUMNS, unique=False
        )
    if _MESSAGE_REQUEST_INDEX not in existing:
        op.create_index(
            _MESSAGE_REQUEST_INDEX,
            _MESSAGE_TABLE,
            ["session_id", text("(metadata_json ->> 'client_request_id')")],
            unique=False,
        )


def downgrade() -> None:
    existing = _existing_indexes(_MESSAGE_TABLE)
    if _MESSAGE_REQUEST_INDEX in existing:
        op.drop_index(_MESSAGE_REQUEST_INDEX, table_name=_MESSAGE_TABLE)
    if _MESSAGE_SESSION_INDEX in existing:
        op.drop_index(_MESSAGE_SESSION_INDEX, table_name=_MESSAGE_TABLE)
    if _SESSION_INDEX in _existing_indexes(_SESSION_TABLE):
        op.drop_index(_SESSION_INDEX, table_name=_SESSION_TABLE)
