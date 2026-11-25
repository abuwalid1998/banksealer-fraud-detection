"""Database engine/session handling (PostgreSQL in production, SQLite supported for tests)."""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .settings import settings

log = logging.getLogger("banksealer.db")


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    kwargs = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs.update(pool_size=10, max_overflow=20)
    return create_engine(url, **kwargs)


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def configure(url: str) -> None:
    """Point the module at another database (tests)."""
    global engine
    engine = make_engine(url)
    SessionLocal.configure(bind=engine)


def wait_for_db(timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while True:
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            return
        except Exception as exc:  # noqa: BLE001
            if time.time() > deadline:
                raise
            log.info("waiting for database: %s", exc)
            time.sleep(2)


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session():
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
