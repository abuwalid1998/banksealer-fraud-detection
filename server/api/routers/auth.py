from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_session
from ...models import User, now
from ...security import create_token, hash_password, verify_password
from ..deps import audit, current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Login(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


def user_json(u: User) -> dict:
    return {"id": u.id, "username": u.username, "full_name": u.full_name, "role": u.role, "active": u.active,
            "last_login": u.last_login.isoformat(timespec="seconds") if u.last_login else None}


def _login(s: Session, username: str, password: str) -> dict:
    user = s.scalar(select(User).where(User.username == username))
    if user is None or not user.active or not verify_password(password, user.password_hash):
        audit(s, None, "auth.failed", username)
        s.commit()
        raise HTTPException(401, "invalid username or password")
    user.last_login = now()
    audit(s, user, "auth.login")
    s.commit()
    return {"access_token": create_token(user.username, user.role), "token_type": "bearer", "user": user_json(user)}


@router.post("/login")
def login(body: Login, s: Session = Depends(get_session)):
    return _login(s, body.username, body.password)


@router.post("/token", include_in_schema=False)
def token(form: OAuth2PasswordRequestForm = Depends(), s: Session = Depends(get_session)):
    """OAuth2 password flow (used by the Swagger 'Authorize' button)."""
    return _login(s, form.username, form.password)


@router.get("/me")
def me(user: User = Depends(current_user)):
    return user_json(user)


@router.post("/password")
def change_password(body: PasswordChange, user: User = Depends(current_user), s: Session = Depends(get_session)):
    db_user = s.get(User, user.id)
    if not verify_password(body.current_password, db_user.password_hash):
        raise HTTPException(400, "current password is wrong")
    if len(body.new_password) < 8:
        raise HTTPException(400, "the new password needs at least 8 characters")
    db_user.password_hash = hash_password(body.new_password)
    audit(s, user, "auth.password_changed")
    s.commit()
    return {"ok": True}
