"""Database engine and per-request session wiring.

Two rules drive this module:

1. **One engine per process.**  ``create_engine`` builds a connection pool;
   calling it per request throws that pool away and opens a fresh connection
   for every query.  The engine is therefore created once at startup (the
   ``create_app`` lifespan) and handed to the request dependency through
   ``app.state``.  A per-request ``create_session_factory()`` made the pool
   meaningless and multiplied connection setup by the number of dependencies
   resolving a session on each request.

2. **No silent fallback to a fresh engine.**  ``get_session`` failing closed is
   correct: serving a request from an engine nobody configured means running
   against an unintended database.  Tests opt in explicitly by overriding the
   dependency with their own session.
"""
from collections.abc import Generator

from fastapi import HTTPException, Request, status
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from medical_rag.core.config import Settings

# ``app.state`` attribute holding the process-wide session factory.  Exported so
# the composition root and the request dependency cannot drift apart.
SESSION_FACTORY_STATE = "db_session_factory"


def create_db_engine(settings: Settings) -> Engine:
    """Build an engine lazily; creating it does not connect to the database."""
    return create_engine(settings.database_url, pool_pre_ping=True)


def create_session_factory(settings: Settings) -> sessionmaker[Session]:
    engine = create_db_engine(settings)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session(request: Request) -> Generator[Session, None, None]:
    """Request-scoped session from the process-wide factory.

    A missing factory is a composition bug (the process is serving requests
    without its configured database), so it fails closed rather than dialling
    the default DSN.
    """
    factory = getattr(request.app.state, SESSION_FACTORY_STATE, None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="数据库会话工厂未初始化",
        )
    session: Session = factory()
    try:
        yield session
    finally:
        session.close()


__all__ = [
    "SESSION_FACTORY_STATE",
    "create_db_engine",
    "create_session_factory",
    "get_session",
]
