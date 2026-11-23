"""Small shared helpers: identifiers, tenant lookup, serialisation."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import Tenant

_UPSERT = text("INSERT INTO counters (tenant_id, name, value) VALUES (:t, :n, 1) "
               "ON CONFLICT (tenant_id, name) DO UPDATE SET value = counters.value + 1 RETURNING value")


_counter_engines: dict = {}


def _counter_engine(bind):
    """Separate small pool for sequence increments so they can never wait on the main pool."""
    key = str(bind.url)
    eng = _counter_engines.get(key)
    if eng is None:
        from sqlalchemy import create_engine
        eng = _counter_engines[key] = create_engine(bind.url, pool_size=4, max_overflow=16, pool_pre_ping=True)
    return eng


def next_seq(db: Session, tenant_id: str, name: str) -> int:
    """Atomic per-tenant sequence.

    PostgreSQL: incremented in its own short transaction, so concurrent requests never hold the
    counter row for the duration of a decision (no lock convoy / deadlock). SQLite: a single atomic
    statement inside the caller's transaction (SQLite serialises writers anyway).
    """
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        with _counter_engine(bind).begin() as conn:
            return int(conn.execute(_UPSERT, {"t": tenant_id, "n": name}).scalar())
    return int(db.execute(_UPSERT, {"t": tenant_id, "n": name}).scalar())


def make_ref(db: Session, tenant_id: str, name: str, prefix: str, width: int = 6) -> str:
    return f"{prefix}-{next_seq(db, tenant_id, name):0{width}d}"


def space_ref(db: Session, tenant_id: str, space: str, kind: str, width: int = 6) -> str:
    """Globally unique, human-readable id such as ``BOP-CASE-000042`` or ``BOP-DEMO-ALR-000007``."""
    marker = "-DEMO" if space == "DEMO" else ""
    return f"{tenant_id}{marker}-{kind}-{next_seq(db, tenant_id, f'{kind}:{space}'):0{width}d}"


def tenant(db: Session, tenant_id: str) -> Tenant:
    t = db.get(Tenant, tenant_id)
    if t is None:
        raise LookupError(f"unknown tenant {tenant_id}")
    return t


def iso(value: Any) -> Any:
    """Serialise a naive-UTC datetime as ISO 8601 with a ``Z`` suffix."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds") + ("Z" if value.tzinfo is None else "")
    return value


def row_dict(obj: Any, exclude: tuple = ()) -> dict:
    out = {}
    for col in obj.__table__.columns:
        if col.name in exclude:
            continue
        out[col.name] = iso(getattr(obj, col.name))
    return out
