"""Authentication: login, first-login setup (password, profile, MFA, e-mail), session info."""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import Principal, client_ip, current_principal, rate_limiter, session_restrictions, tenant_mfa_required
from ..models import ROLE_LABELS, Tenant, User, UserSession, utcnow
from ..rbac import navigation_for, permissions_for
from ..security import (decrypt_field, encrypt_field, hash_password, new_token, new_totp_secret, password_problems,
                        token_hash, totp_uri, verify_password, verify_totp)
from ..services.reports import branding
from ..settings import settings

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
log = logging.getLogger("verishield.auth")


class LoginIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=10)
    password: str = Field(..., min_length=1, max_length=200)
    tenant: Optional[str] = Field(None, description="Institution code (defaults to the installation's tenant)")


class PasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(..., max_length=200)


class ProfileIn(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=80)
    last_name: str = Field(..., min_length=1, max_length=80)
    email: str = Field(..., min_length=3, max_length=200)


class CodeIn(BaseModel):
    code: str = Field(..., min_length=4, max_length=12)


def _user(db: Session, p: Principal) -> User:
    return db.get(User, p.user_id)


def me_payload(db: Session, user: User, tenant: Tenant, restrictions: list) -> dict:
    perms = permissions_for(user.role, user.extra_permissions)
    b = branding(tenant)
    return {
        "user": {"id": user.id, "username": user.username, "first_name": user.first_name,
                 "last_name": user.last_name, "name": user.full_name, "email": user.email,
                 "email_verified": user.email_verified, "role": user.role, "role_label": ROLE_LABELS[user.role],
                 "mfa_enabled": user.mfa_enabled, "status": user.status,
                 "last_login_at": user.last_login_at.isoformat() + "Z" if user.last_login_at else None},
        "tenant": {"id": tenant.id, "name": tenant.display_name, "legal_name": tenant.legal_name,
                   "timezone": tenant.timezone, "currency": tenant.base_currency, "locale": tenant.locale,
                   "demo_mode": tenant.demo_mode, "primary_color": b["primary_color"], "accent_color": b["accent_color"],
                   "logo": f"/api/v1/public/logo/{tenant.id}" if b.get("logo_key") else None},
        "permissions": sorted(perms),
        "navigation": navigation_for(perms),
        "restrictions": restrictions,
        "mfa_required": tenant_mfa_required(tenant, user.role),
        "environment": settings.env,
    }


@router.post("/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    if not rate_limiter.allow(f"login:{ip}", 20):
        raise HTTPException(429, {"code": "RATE_LIMITED", "message": "Too many attempts; wait a minute"})
    tenant_id = (body.tenant or settings.tenant_code).strip().upper()
    tenant = db.get(Tenant, tenant_id)
    user = db.execute(select(User).where(User.tenant_id == tenant_id, User.username == body.username.strip())
                      ).scalar() if tenant else None
    fail = HTTPException(401, {"code": "INVALID_CREDENTIALS", "message": "Invalid username or password"})
    if user is None:
        if tenant:
            audit.record(db, tenant_id, "auth.login", actor=f"unknown user {body.username[:10]}", outcome="FAILURE",
                         reason="unknown username", ip=ip)
        raise fail
    if user.status in ("DISABLED", "LOCKED"):
        audit.record(db, tenant_id, "auth.login", actor=user, outcome="FAILURE", reason=f"account {user.status}",
                     ip=ip)
        raise HTTPException(403, {"code": f"ACCOUNT_{user.status}",
                                  "message": f"Account is {user.status.lower()} - contact your IT Admin"})
    if not verify_password(body.password, user.password_hash):
        sec = tenant.settings.get("security", {})
        user.failed_logins += 1
        if user.failed_logins >= int(sec.get("lockout_threshold", 5)):
            user.status = "LOCKED"
        db.commit()
        audit.record(db, tenant_id, "auth.login", actor=user, outcome="FAILURE", reason="bad password", ip=ip)
        raise fail
    if user.must_change_password and user.temp_password_expires_at and user.temp_password_expires_at < utcnow():
        audit.record(db, tenant_id, "auth.login", actor=user, outcome="FAILURE", reason="temporary password expired",
                     ip=ip)
        raise HTTPException(403, {"code": "TEMP_PASSWORD_EXPIRED",
                                  "message": "Temporary password expired - ask your IT Admin for a reset"})
    user.failed_logins = 0
    user.last_login_at = utcnow()
    token = new_token()
    sec = tenant.settings.get("security", {})
    sess = UserSession(token_hash=token_hash(token), user_id=user.id, tenant_id=tenant_id,
                       expires_at=utcnow() + timedelta(hours=int(sec.get("session_absolute_hours", 10))), ip=ip,
                       user_agent=request.headers.get("user-agent", "")[:200], mfa_passed=not user.mfa_enabled)
    db.add(sess)
    db.commit()
    audit.record(db, tenant_id, "auth.login", actor=user, ip=ip,
                 reason="MFA verification pending" if user.mfa_enabled else "")
    restrictions = session_restrictions(user, tenant, tenant_mfa_required(tenant, user.role))
    return {"token": token, "mfa_required": user.mfa_enabled, "restrictions": restrictions,
            "expires_at": sess.expires_at.isoformat() + "Z"}


@router.post("/mfa/verify")
def mfa_verify(body: CodeIn, request: Request, db: Session = Depends(get_db)):
    """Second step of login when the user has MFA enabled."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, {"code": "UNAUTHENTICATED", "message": "Sign in first"})
    sess = db.execute(select(UserSession).where(UserSession.token_hash == token_hash(auth[7:].strip()))).scalar()
    if sess is None or sess.revoked or sess.expires_at < utcnow():
        raise HTTPException(401, {"code": "SESSION_EXPIRED", "message": "Please sign in again"})
    user = db.get(User, sess.user_id)
    ip = client_ip(request)
    if not rate_limiter.allow(f"mfa:{sess.id}", 6):
        raise HTTPException(429, {"code": "RATE_LIMITED", "message": "Too many attempts"})
    if not user.mfa_secret or not verify_totp(decrypt_field(user.mfa_secret), body.code):
        audit.record(db, user.tenant_id, "auth.mfa", actor=user, outcome="FAILURE", ip=ip)
        raise HTTPException(401, {"code": "INVALID_MFA_CODE", "message": "Invalid verification code"})
    sess.mfa_passed = True
    db.commit()
    audit.record(db, user.tenant_id, "auth.mfa", actor=user, ip=ip)
    return {"ok": True}


@router.post("/logout")
def logout(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    if p.session_id:
        sess = db.get(UserSession, p.session_id)
        sess.revoked = True
        db.commit()
        audit.record(db, p.tenant_id, "auth.logout", actor=p)
    return {"ok": True}


@router.get("/me")
def me(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    if p.kind != "user":
        raise HTTPException(403, {"code": "USER_ONLY", "message": "API clients have no user profile"})
    return me_payload(db, _user(db, p), db.get(Tenant, p.tenant_id), p.restrictions)


@router.post("/change-password")
def change_password(body: PasswordIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    user = _user(db, p)
    tenant = db.get(Tenant, p.tenant_id)
    if not verify_password(body.current_password, user.password_hash):
        audit.record(db, p.tenant_id, "user.password_change", actor=p, outcome="FAILURE", reason="wrong current password")
        raise HTTPException(400, {"code": "WRONG_PASSWORD", "message": "Current password is incorrect"})
    if verify_password(body.new_password, user.password_hash):
        raise HTTPException(422, {"code": "PASSWORD_REUSED", "message": "Choose a password different from the current one"})
    problems = password_problems(body.new_password, tenant.settings.get("security", {}), user.username)
    if problems:
        raise HTTPException(422, {"code": "WEAK_PASSWORD", "message": "Password needs " + ", ".join(problems)})
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    user.temp_password_expires_at = None
    user.password_changed_at = utcnow()
    if user.status == "INVITED":
        user.status = "ACTIVE"
    db.commit()
    audit.record(db, p.tenant_id, "user.password_change", actor=p, resource_type="user", resource_id=user.id)
    return me_payload(db, user, tenant, session_restrictions(user, tenant, tenant_mfa_required(tenant, user.role)))


@router.put("/profile")
def update_profile(body: ProfileIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    if "@" not in body.email or "." not in body.email.split("@")[-1]:
        raise HTTPException(422, {"code": "INVALID_EMAIL", "message": "Enter a valid e-mail address"})
    user = _user(db, p)
    if "CHANGE_PASSWORD" in p.restrictions:
        raise HTTPException(403, {"code": "SETUP_REQUIRED", "message": "Change your password first"})
    before = {"first_name": user.first_name, "last_name": user.last_name, "email": user.email}
    if body.email.strip().lower() != (user.email or "").lower():
        user.email_verified = False
    user.first_name, user.last_name, user.email = body.first_name.strip(), body.last_name.strip(), body.email.strip()
    db.commit()
    audit.record(db, p.tenant_id, "user.profile_update", actor=p, resource_type="user", resource_id=user.id,
                 before=before, after={"first_name": user.first_name, "last_name": user.last_name, "email": user.email})
    tenant = db.get(Tenant, p.tenant_id)
    return me_payload(db, user, tenant, session_restrictions(user, tenant, tenant_mfa_required(tenant, user.role)))


@router.post("/mfa/setup")
def mfa_setup(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    user = _user(db, p)
    if "CHANGE_PASSWORD" in p.restrictions:
        raise HTTPException(403, {"code": "SETUP_REQUIRED", "message": "Change your password first"})
    if user.mfa_enabled:
        raise HTTPException(409, {"code": "MFA_ALREADY_ENABLED", "message": "MFA is already enabled"})
    secret = new_totp_secret()
    user.mfa_secret = encrypt_field(secret)
    db.commit()
    tenant = db.get(Tenant, p.tenant_id)
    return {"secret": secret, "otpauth_uri": totp_uri(secret, f"{tenant.id}:{user.username}",
                                                      f"VeriShield {tenant.id}")}


@router.post("/mfa/enable")
def mfa_enable(body: CodeIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    user = _user(db, p)
    if not user.mfa_secret or not verify_totp(decrypt_field(user.mfa_secret), body.code):
        raise HTTPException(400, {"code": "INVALID_MFA_CODE", "message": "Code does not match - check the time on "
                                                                         "your authenticator device"})
    user.mfa_enabled = True
    sess = db.get(UserSession, p.session_id)
    sess.mfa_passed = True
    db.commit()
    audit.record(db, p.tenant_id, "user.mfa_enrol", actor=p, resource_type="user", resource_id=user.id)
    tenant = db.get(Tenant, p.tenant_id)
    return me_payload(db, user, tenant, session_restrictions(user, tenant, tenant_mfa_required(tenant, user.role)))


@router.post("/email/send-code")
def email_send_code(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    user = _user(db, p)
    if not user.email:
        raise HTTPException(422, {"code": "NO_EMAIL", "message": "Set your e-mail address first"})
    code = f"{secrets.randbelow(900000) + 100000}"
    user.email_code_hash = hashlib.sha256(f"{user.id}:{code}".encode()).hexdigest()
    db.commit()
    sent = _send_mail(user.email, "VeriShield e-mail verification", f"Your VeriShield verification code is {code}.")
    out = {"sent": sent}
    if not sent:
        log.warning("e-mail delivery not configured - verification code for %s is %s", user.username, code)
        if settings.env == "development":
            out["dev_code"] = code
            out["message"] = "SMTP is not configured; in development the code is shown here and in the server log."
    return out


@router.post("/email/verify")
def email_verify(body: CodeIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    user = _user(db, p)
    if not user.email_code_hash or user.email_code_hash != hashlib.sha256(f"{user.id}:{body.code}".encode()).hexdigest():
        raise HTTPException(400, {"code": "INVALID_CODE", "message": "Invalid verification code"})
    user.email_verified = True
    user.email_code_hash = None
    db.commit()
    audit.record(db, p.tenant_id, "user.email_verified", actor=p, resource_type="user", resource_id=user.id)
    tenant = db.get(Tenant, p.tenant_id)
    return me_payload(db, user, tenant, session_restrictions(user, tenant, tenant_mfa_required(tenant, user.role)))


def _send_mail(to: str, subject: str, text: str) -> bool:
    import os
    import smtplib
    from email.message import EmailMessage

    host = os.environ.get("VERISHIELD_SMTP_HOST")
    if not host:
        return False
    msg = EmailMessage()
    msg["From"] = os.environ.get("VERISHIELD_SMTP_FROM", "verishield@localhost")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    try:
        with smtplib.SMTP(host, int(os.environ.get("VERISHIELD_SMTP_PORT", "587")), timeout=10) as s:
            if os.environ.get("VERISHIELD_SMTP_TLS", "true").lower() != "false":
                s.starttls()
            if os.environ.get("VERISHIELD_SMTP_USER"):
                s.login(os.environ["VERISHIELD_SMTP_USER"], os.environ.get("VERISHIELD_SMTP_PASSWORD", ""))
            s.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("SMTP delivery failed: %s", exc)
        return False
