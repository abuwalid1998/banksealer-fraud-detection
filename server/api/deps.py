"""Shared API dependencies: database session, authentication, roles, audit."""

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt import PyJWTError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import AuditLog, Command, User
from ..security import ROLES, decode_token

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)


def current_user(token: str | None = Depends(oauth2), s: Session = Depends(get_session)) -> User:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated", {"WWW-Authenticate": "Bearer"})
    try:
        payload = decode_token(token)
    except PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token", {"WWW-Authenticate": "Bearer"})
    user = s.scalar(select(User).where(User.username == payload.get("sub")))
    if user is None or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user disabled")
    return user


def require(role: str):
    def checker(user: User = Depends(current_user)) -> User:
        if ROLES.get(user.role, -1) < ROLES[role]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role {role}")
        return user
    return checker


viewer = require("viewer")
analyst = require("analyst")
admin = require("admin")


def audit(s: Session, user: User | None, action: str, target: str | None = None, detail: dict | None = None) -> None:
    s.add(AuditLog(username=user.username if user else None, action=action, target=target, detail=detail))


def enqueue(s: Session, user: User | None, kind: str, payload: dict | None = None) -> Command:
    cmd = Command(kind=kind, payload=payload or {}, created_by=user.username if user else None)
    s.add(cmd)
    s.flush()
    return cmd


def page(items: list, total: int, page_no: int, size: int) -> dict:
    return {"items": items, "total": total, "page": page_no, "size": size}
