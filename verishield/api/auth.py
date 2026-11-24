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
                        token_hash, totp_uri, verify_password, verify_totp)  # noqa: F401
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
    phone: Optional[str] = Field(None, max_length=32)


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
                 "email_verified": user.email_verified, "phone": user.phone or "", "role": user.role, "role_label": ROLE_LABELS[user.role],
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
    if body.phone is not None:
        user.phone = body.phone.strip()
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
    from ..services import notify
    tenant = db.get(Tenant, p.tenant_id)
    sent = notify.send_email(db, tenant, user.email, "E-mail verification code",
                             f"Your VeriShield verification code is {code}.", "email_verification", sensitive=True)[0]
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


# --------------------------------------------------------------------------- forgotten password
class ForgotIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=10)
    tenant: Optional[str] = None
    channel: str = Field("email", description="email or sms")


class ResetIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=10)
    tenant: Optional[str] = None
    code: str = Field(..., min_length=4, max_length=12)
    new_password: str = Field(..., max_length=200)


RESET_MINUTES = 15


def _reset_hash(user: User, code: str) -> str:
    return hashlib.sha256(f"reset:{user.id}:{code}:{settings.secret_key}".encode()).hexdigest()


@router.post("/forgot-password")
def forgot_password(body: ForgotIn, request: Request, db: Session = Depends(get_db)):
    """Send a one-time reset code by e-mail or SMS. The answer never reveals whether the account exists."""
    from ..services import notify
    ip = client_ip(request)
    if not rate_limiter.allow(f"forgot:{ip}", 6):
        raise HTTPException(429, {"code": "RATE_LIMITED", "message": "Too many requests; wait a minute"})
    tenant = db.get(Tenant, (body.tenant or settings.tenant_code).strip().upper())
    channel = body.channel.lower()
    generic = {"ok": True, "message": f"If the account exists and has a registered "
                                      f"{'mobile number' if channel == 'sms' else 'e-mail address'}, a 6-digit code "
                                      f"has been sent. It is valid for {RESET_MINUTES} minutes."}
    if tenant is None or channel not in ("email", "sms"):
        return generic
    user = db.execute(select(User).where(User.tenant_id == tenant.id, User.username == body.username.strip())
                      ).scalar()
    if user is None or user.status == "DISABLED":
        audit.record(db, tenant.id, "auth.forgot_password", actor=f"user {body.username[:10]}", outcome="FAILURE",
                     reason="unknown or disabled account", ip=ip)
        return generic
    if not rate_limiter.allow(f"forgot-user:{user.id}", 3, 600):
        return generic
    code = f"{secrets.randbelow(900000) + 100000}"
    user.reset_code_hash = _reset_hash(user, code)
    user.reset_code_expires_at = utcnow() + timedelta(minutes=RESET_MINUTES)
    user.reset_attempts = 0
    db.commit()
    text = (f"Your {tenant.display_name} VeriShield password reset code is {code}. It expires in "
            f"{RESET_MINUTES} minutes. If you did not ask for it, contact your IT Admin.")
    res = notify.deliver_secret(db, tenant, user, "Password reset code", text, [channel], "password_reset")
    sent = res.get(channel, {}).get("sent", False)
    audit.record(db, tenant.id, "auth.forgot_password", actor=user, ip=ip, outcome="SUCCESS" if sent else "FAILURE",
                 reason=f"code via {channel}" + ("" if sent else f" not delivered: {res.get(channel, {}).get('detail')}"))
    out = dict(generic)
    if not sent and settings.env != "production" and settings.show_dev_accounts:
        out["dev_code"] = code
        out["message"] += (f" (Development: {channel.upper()} delivery is not configured, so the code is shown here "
                           "and must never be shown in production.)")
    return out


@router.post("/reset-password")
def reset_password(body: ResetIn, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    if not rate_limiter.allow(f"reset:{ip}", 10):
        raise HTTPException(429, {"code": "RATE_LIMITED", "message": "Too many attempts; wait a minute"})
    tenant = db.get(Tenant, (body.tenant or settings.tenant_code).strip().upper())
    user = db.execute(select(User).where(User.tenant_id == tenant.id, User.username == body.username.strip())
                      ).scalar() if tenant else None
    bad = HTTPException(400, {"code": "INVALID_CODE", "message": "The code is invalid or has expired"})
    if user is None or not user.reset_code_hash or user.status == "DISABLED":
        raise bad
    if (user.reset_code_expires_at and user.reset_code_expires_at < utcnow()) or user.reset_attempts >= 5:
        user.reset_code_hash = None
        db.commit()
        raise bad
    if not secrets.compare_digest(user.reset_code_hash, _reset_hash(user, body.code.strip())):
        user.reset_attempts += 1
        db.commit()
        audit.record(db, tenant.id, "auth.reset_password", actor=user, outcome="FAILURE", reason="wrong code", ip=ip)
        raise bad
    problems = password_problems(body.new_password, tenant.settings.get("security", {}), user.username)
    if problems:
        raise HTTPException(422, {"code": "WEAK_PASSWORD", "message": "Password needs " + ", ".join(problems)})
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    user.temp_password_expires_at = None
    user.password_changed_at = utcnow()
    user.reset_code_hash = None
    user.reset_attempts = 0
    user.failed_logins = 0
    if user.status in ("LOCKED", "INVITED"):
        user.status = "ACTIVE"
    for sess in db.execute(select(UserSession).where(UserSession.user_id == user.id)).scalars():
        sess.revoked = True
    db.commit()
    audit.record(db, tenant.id, "auth.reset_password", actor=user, resource_type="user", resource_id=user.id, ip=ip,
                 reason="self-service reset with one-time code")
    return {"ok": True, "message": "Password changed - sign in with your new password."}
