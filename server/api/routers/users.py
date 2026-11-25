from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_session
from ...models import AuditLog, User
from ...security import ROLES, hash_password
from ..deps import admin, audit, page
from .auth import user_json

router = APIRouter(prefix="/api", tags=["users"])


class UserIn(BaseModel):
    username: str
    full_name: Optional[str] = None
    role: str = "analyst"
    password: Optional[str] = None
    active: bool = True


@router.get("/users")
def list_users(_: User = Depends(admin), s: Session = Depends(get_session)):
    return [user_json(u) for u in s.scalars(select(User).order_by(User.username))]


@router.post("/users")
def create_user(body: UserIn, me: User = Depends(admin), s: Session = Depends(get_session)):
    if body.role not in ROLES:
        raise HTTPException(400, f"role must be one of {', '.join(ROLES)}")
    if not body.password or len(body.password) < 8:
        raise HTTPException(400, "password of at least 8 characters required")
    if s.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, "username already exists")
    u = User(username=body.username, full_name=body.full_name, role=body.role, active=body.active,
             password_hash=hash_password(body.password))
    s.add(u)
    audit(s, me, "user.create", body.username, {"role": body.role})
    s.commit()
    return user_json(u)


@router.put("/users/{user_id}")
def update_user(user_id: int, body: UserIn, me: User = Depends(admin), s: Session = Depends(get_session)):
    u = s.get(User, user_id)
    if u is None:
        raise HTTPException(404, "user not found")
    if body.role not in ROLES:
        raise HTTPException(400, "invalid role")
    if u.id == me.id and (body.role != "admin" or not body.active):
        raise HTTPException(400, "you cannot remove your own admin access")
    u.full_name, u.role, u.active = body.full_name, body.role, body.active
    if body.password:
        if len(body.password) < 8:
            raise HTTPException(400, "password of at least 8 characters required")
        u.password_hash = hash_password(body.password)
    audit(s, me, "user.update", u.username, {"role": body.role, "active": body.active,
                                             "password_reset": bool(body.password)})
    s.commit()
    return user_json(u)


@router.delete("/users/{user_id}")
def delete_user(user_id: int, me: User = Depends(admin), s: Session = Depends(get_session)):
    u = s.get(User, user_id)
    if u is None:
        raise HTTPException(404, "user not found")
    if u.id == me.id:
        raise HTTPException(400, "you cannot delete yourself")
    s.delete(u)
    audit(s, me, "user.delete", u.username)
    s.commit()
    return {"ok": True}


@router.get("/audit")
def audit_log(page_no: int = 1, size: int = 50, action: Optional[str] = None, username: Optional[str] = None,
              _: User = Depends(admin), s: Session = Depends(get_session)):
    q = select(AuditLog)
    if action:
        q = q.where(AuditLog.action.ilike(f"%{action}%"))
    if username:
        q = q.where(AuditLog.username == username)
    from sqlalchemy import func
    total = s.scalar(select(func.count()).select_from(q.subquery()))
    rows = s.scalars(q.order_by(AuditLog.id.desc()).offset((page_no - 1) * size).limit(size))
    return page([{"id": r.id, "ts": r.ts.isoformat(timespec="seconds"), "username": r.username, "action": r.action,
                  "target": r.target, "detail": r.detail} for r in rows], total, page_no, size)
