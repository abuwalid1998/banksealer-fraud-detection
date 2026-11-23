"""Tamper-evident audit trail (BRD 20, 21.1).

Each event's hash covers its content plus the previous event's hash for the same
tenant, so altering or deleting a past record breaks the chain and is detected by
:func:`verify_chain`.
"""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditEvent, utcnow

_lock = threading.Lock()


def _digest(ev: AuditEvent) -> str:
    payload = json.dumps({
        "tenant": ev.tenant_id, "space": ev.space, "actor": ev.actor_id, "actor_name": ev.actor_name,
        "role": ev.actor_role, "action": ev.action, "type": ev.resource_type, "id": ev.resource_id,
        "before": ev.before, "after": ev.after, "outcome": ev.outcome, "reason": ev.reason,
        "ip": ev.ip, "corr": ev.correlation_id, "at": ev.created_at.isoformat(timespec="microseconds"),
        "prev": ev.prev_hash,
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def record(db: Session, tenant_id: str, action: str, *, actor: Any = None, resource_type: str = "",
           resource_id: str = "", before: Optional[dict] = None, after: Optional[dict] = None,
           outcome: str = "SUCCESS", reason: str = "", ip: str = "", correlation_id: str = "",
           space: str = "LIVE", commit: bool = True) -> AuditEvent:
    """Append an audit event. ``actor`` is a User, a Principal or a plain name."""
    actor_id, actor_name, actor_role = None, "system", "SYSTEM"
    if actor is not None:
        if isinstance(actor, str):
            actor_name = actor
        else:
            actor_id = getattr(actor, "user_id", None) or getattr(actor, "id", None)
            actor_name = getattr(actor, "display", None) or getattr(actor, "full_name", None) or str(actor_id)
            actor_role = getattr(actor, "role", "SYSTEM") or "SYSTEM"
            ip = ip or getattr(actor, "ip", "") or ""
            space = getattr(actor, "space", space) or space
    with _lock:
        prev = db.execute(select(AuditEvent.hash).where(AuditEvent.tenant_id == tenant_id)
                          .order_by(AuditEvent.id.desc()).limit(1)).scalar()
        ev = AuditEvent(tenant_id=tenant_id, space=space, actor_id=actor_id, actor_name=actor_name,
                        actor_role=actor_role, action=action, resource_type=resource_type,
                        resource_id=str(resource_id or ""), before=_clean(before), after=_clean(after),
                        outcome=outcome, reason=reason or "", ip=ip, correlation_id=correlation_id,
                        created_at=utcnow(), prev_hash=prev or "")
        ev.hash = _digest(ev)
        db.add(ev)
        if commit:
            db.commit()
        else:
            db.flush()
    return ev


_SENSITIVE = ("password", "secret", "token", "key_hash", "mfa_secret", "api_key", "ciphertext")


def _clean(data: Optional[dict]) -> Optional[dict]:
    if data is None:
        return None
    out = {}
    for k, v in data.items():
        if any(s in k.lower() for s in _SENSITIVE):
            out[k] = "***"
        elif isinstance(v, dict):
            out[k] = _clean(v)
        else:
            out[k] = v
    return json.loads(json.dumps(out, default=str))


def verify_chain(db: Session, tenant_id: str) -> dict:
    prev = ""
    count = 0
    for ev in db.execute(select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
                         .order_by(AuditEvent.id)).scalars():
        count += 1
        if ev.prev_hash != prev or _digest(ev) != ev.hash:
            return {"valid": False, "checked": count, "broken_at": ev.id}
        prev = ev.hash
    return {"valid": True, "checked": count, "broken_at": None}
