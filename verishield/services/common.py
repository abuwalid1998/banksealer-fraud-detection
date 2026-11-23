"""Small shared helpers: identifiers, tenant lookup, serialisation."""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..models import Counter, Tenant

_seq_lock = threading.Lock()


def next_seq(db: Session, tenant_id: str, name: str) -> int:
    with _seq_lock:
        row = db.get(Counter, (tenant_id, name))
        if row is None:
            row = Counter(tenant_id=tenant_id, name=name, value=0)
            db.add(row)
        row.value += 1
        db.flush()
        return row.value


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
