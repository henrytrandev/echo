"""Database engine and session setup. SQLite by default, Postgres via DATABASE_URL."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from echo.config import normalize_database_url
from echo.models import Base


def make_engine(database_url: str) -> Engine:
    database_url = normalize_database_url(database_url)
    if database_url.startswith("sqlite"):
        in_memory = database_url in {"sqlite://", "sqlite:///:memory:"}
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            # An in-memory database exists per connection, so share one (used by tests).
            poolclass=StaticPool if in_memory else None,
        )

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record) -> None:  # SQLite has them off by default
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine
    return create_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_schema(engine: Engine) -> None:
    """Create tables if they don't exist. Swap for Alembic migrations once the schema settles."""
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One transaction: commit on success, roll back on any error."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
