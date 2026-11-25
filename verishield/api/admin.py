"""IT Admin & governance endpoints: users, tenant, branding, API clients, Demo Mode, audit, incidents, health."""
from __future__ import annotations

import copy
import re
import threading
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db, get_engine
from ..deps import API_SCOPES, Principal, require, require_user
from ..models import (ROLE_LABELS, ROLES, ApiClient, AuditEvent, Connector, DeadLetterEvent, SystemIncident, Tenant,
                      User, UserSession, utcnow)
from ..rbac import ALL_PERMISSIONS, GRANTABLE, ROLE_PERMISSIONS
from ..security import hash_password, new_api_key, temporary_password
from ..services import demo as demo_service
from ..services import engine_registry
from ..services.common import row_dict
from ..services.metrics import metrics
from ..services.reports import AML_BRANDING_FIELDS, TENANT_BRANDING_FIELDS, branding
from ..settings import settings

router = APIRouter(prefix="/api/v1", tags=["administration"])


# --------------------------------------------------------------------------- users
class UserIn(BaseModel):
    username: str = Field(..., pattern=r"^\d{4,5}$", description="Unique 4-5 digit login username")
    first_name: str = Field(..., min_length=1, max_length=80)
    last_name: str = Field(..., min_length=1, max_length=80)
    email: str = Field(..., min_length=3, max_length=200)
    role: str
    temporary_password: Optional[str] = Field(None, max_length=100)
    phone: Optional[str] = Field(None, max_length=32)
    send_credentials: list[str] = Field(default_factory=list, description="email and/or sms")


class UserPatch(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    role: Optional[str] = None
    extra_permissions: Optional[list[str]] = None


def _user_out(u: User) -> dict:
    d = row_dict(u, exclude=("password_hash", "mfa_secret", "email_code_hash", "reset_code_hash"))
    d["role_label"] = ROLE_LABELS.get(u.role, u.role)
    d["name"] = u.full_name
    return d


def _check_role_limit(db: Session, tenant: Tenant, role: str, exclude_id: Optional[str] = None) -> None:
    limit = (tenant.settings.get("role_limits") or {}).get(role)
    if not limit:
        return
    q = select(func.count(User.id)).where(User.tenant_id == tenant.id, User.role == role,
                                          User.status.in_(("ACTIVE", "INVITED", "LOCKED")))
    if exclude_id:
        q = q.where(User.id != exclude_id)
    if db.execute(q).scalar() >= int(limit):
        raise HTTPException(409, {"code": "ROLE_LIMIT", "message": f"This institution allows {limit} active "
                                                                   f"{ROLE_LABELS[role]} account(s); disable the "
                                                                   "existing one or change the role limit"})


@router.get("/users")
def list_users(p: Principal = Depends(require("users.manage")), db: Session = Depends(get_db)):
    users = db.execute(select(User).where(User.tenant_id == p.tenant_id).order_by(User.username)).scalars().all()
    return {"users": [_user_out(u) for u in users], "roles": [{"id": r, "label": ROLE_LABELS[r]} for r in ROLES],
            "grantable": {k: sorted(v) for k, v in GRANTABLE.items()}}


@router.get("/users/assignable")
def assignable_users(p: Principal = Depends(require("cases.assign", "cases.view_all")), db: Session = Depends(get_db)):
    users = db.execute(select(User).where(User.tenant_id == p.tenant_id, User.role == "AML_EMPLOYEE",
                                          User.status.in_(("ACTIVE", "INVITED")))).scalars().all()
    return {"users": [{"id": u.id, "name": u.full_name, "username": u.username, "status": u.status} for u in users]}


@router.post("/users", status_code=201)
def create_user(body: UserIn, p: Principal = Depends(require_user("users.manage")), db: Session = Depends(get_db)):
    if body.role not in ROLES:
        raise HTTPException(422, {"code": "INVALID_ROLE", "message": f"role must be one of {', '.join(ROLES)}"})
    if "@" not in body.email:
        raise HTTPException(422, {"code": "INVALID_EMAIL", "message": "invalid e-mail address"})
    tenant = db.get(Tenant, p.tenant_id)
    if db.execute(select(User.id).where(User.tenant_id == p.tenant_id, User.username == body.username)).scalar():
        raise HTTPException(409, {"code": "USERNAME_TAKEN", "message": f"username {body.username} already exists"})
    _check_role_limit(db, tenant, body.role)
    temp = body.temporary_password or temporary_password()
    hours = int(tenant.settings.get("security", {}).get("temp_password_hours", 72))
    user = User(tenant_id=p.tenant_id, username=body.username, first_name=body.first_name.strip(),
                last_name=body.last_name.strip(), email=body.email.strip(), phone=(body.phone or "").strip(),
                role=body.role, status="INVITED",
                password_hash=hash_password(temp), must_change_password=True,
                temp_password_expires_at=utcnow() + timedelta(hours=hours), created_by=p.user_id)
    db.add(user)
    db.commit()
    audit.record(db, p.tenant_id, "user.create", actor=p, resource_type="user", resource_id=user.id,
                 after={"username": user.username, "role": user.role, "email": user.email})
    delivery = _deliver_password(db, tenant, user, temp, body.send_credentials, "account_created", hours) \
        if body.send_credentials else {}
    return {"user": _user_out(user), "temporary_password": temp, "delivery": delivery,
            "note": f"Share the temporary password securely. It is shown only once, is single-use and expires in "
                    f"{hours} hours."}


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: UserPatch, p: Principal = Depends(require_user("users.manage")),
                db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None or user.tenant_id != p.tenant_id:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "user not found"})
    before = _user_out(user)
    if body.role and body.role != user.role:
        if body.role not in ROLES:
            raise HTTPException(422, {"code": "INVALID_ROLE", "message": "invalid role"})
        if user.id == p.user_id:
            raise HTTPException(409, {"code": "SELF_ROLE_CHANGE", "message": "you cannot change your own role"})
        _check_role_limit(db, db.get(Tenant, p.tenant_id), body.role, user.id)
        user.role = body.role
        user.extra_permissions = []
    for field in ("first_name", "last_name", "email", "phone"):
        value = getattr(body, field)
        if value is not None:
            setattr(user, field, value.strip())
    if body.extra_permissions is not None:
        allowed = GRANTABLE.get(user.role, set())
        bad = [x for x in body.extra_permissions if x not in allowed]
        if bad:
            raise HTTPException(422, {"code": "NOT_GRANTABLE", "message": f"cannot grant {', '.join(bad)} to "
                                                                          f"{ROLE_LABELS[user.role]}"})
        user.extra_permissions = sorted(set(body.extra_permissions))
    db.commit()
    audit.record(db, p.tenant_id, "user.update", actor=p, resource_type="user", resource_id=user.id, before=before,
                 after=_user_out(user))
    return {"user": _user_out(user)}


def _deliver_password(db: Session, tenant: Tenant, user: User, password: str, channels: list, event: str,
                      hours: Optional[int]) -> dict:
    from ..services import notify
    chans = [c for c in channels if c in ("email", "sms")]
    if not chans:
        return {}
    expiry = f" It is temporary, expires in {hours} hours and must be changed at sign-in." if hours else ""
    text = (f"Your {tenant.display_name} VeriShield account: username {user.username}, password {password}.{expiry} "
            "Do not share it.")
    return notify.deliver_secret(db, tenant, user, "Your VeriShield sign-in details", text, chans, event)


@router.post("/users/{user_id}/{action}")
def user_action(user_id: str, action: str, body: Optional[dict] = Body(None),
                p: Principal = Depends(require_user("users.manage")), db: Session = Depends(get_db)):
    body = body or {}
    user = db.get(User, user_id)
    if user is None or user.tenant_id != p.tenant_id:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "user not found"})
    out: dict = {}
    if action == "disable":
        if user.id == p.user_id:
            raise HTTPException(409, {"code": "SELF_DISABLE", "message": "you cannot disable your own account"})
        if user.role == "IT_ADMIN" and db.execute(select(func.count(User.id)).where(
                User.tenant_id == p.tenant_id, User.role == "IT_ADMIN", User.status == "ACTIVE")).scalar() <= 1 \
                and user.status == "ACTIVE":
            raise HTTPException(409, {"code": "LAST_ADMIN", "message": "the last active IT Admin cannot be disabled"})
        user.status = "DISABLED"
        for s in db.execute(select(UserSession).where(UserSession.user_id == user.id)).scalars():
            s.revoked = True
    elif action == "enable":
        if user.status == "DISABLED":
            _check_role_limit(db, db.get(Tenant, p.tenant_id), user.role, user.id)
        user.status = "ACTIVE" if not user.must_change_password else "INVITED"
    elif action == "unlock":
        user.status = "ACTIVE" if not user.must_change_password else "INVITED"
        user.failed_logins = 0
    elif action == "reset-password":
        # IT Admin password reset: generated temporary password (default) or a password the admin chooses;
        # optionally delivered to the user by e-mail / SMS.
        from ..security import password_problems
        tenant = db.get(Tenant, p.tenant_id)
        hours = int(tenant.settings.get("security", {}).get("temp_password_hours", 72))
        chosen = str(body.get("password") or "")
        require_change = bool(body.get("require_change", True))
        if chosen:
            problems = password_problems(chosen, tenant.settings.get("security", {}), user.username)
            if problems:
                raise HTTPException(422, {"code": "WEAK_PASSWORD", "message": "Password needs " + ", ".join(problems)})
        temp = chosen or temporary_password()
        user.password_hash = hash_password(temp)
        user.must_change_password = require_change
        user.temp_password_expires_at = utcnow() + timedelta(hours=hours) if require_change else None
        user.reset_by, user.reset_at = p.user_id, utcnow()
        user.failed_logins = 0
        user.reset_code_hash = None
        if user.status == "LOCKED" or (user.status == "INVITED" and not require_change):
            user.status = "INVITED" if require_change else "ACTIVE"
        for s in db.execute(select(UserSession).where(UserSession.user_id == user.id)).scalars():
            s.revoked = True
        db.commit()
        delivery = _deliver_password(db, tenant, user, temp, body.get("notify") or [], "password_reset",
                                     hours if require_change else None)
        out = {"temporary_password": None if chosen else temp, "delivery": delivery,
               "note": (f"Temporary password - must be changed at next sign-in; expires in {hours} hours."
                        if require_change else "Password set; the user is not asked to change it.")
               + (" Delivered: " + ", ".join(k for k, v in delivery.items() if v["sent"]) if delivery else "")}
    elif action == "reset-mfa":
        user.mfa_enabled = False
        user.mfa_secret = None
    else:
        raise HTTPException(404, {"code": "UNKNOWN_ACTION", "message": "unknown action"})
    db.commit()
    audit.record(db, p.tenant_id, f"user.{action}", actor=p, resource_type="user", resource_id=user.id,
                 after={"status": user.status})
    return {"user": _user_out(user), **out}


@router.get("/roles")
def roles(p: Principal = Depends(require("users.manage", "audit.view"))):
    return {"roles": [{"id": r, "label": ROLE_LABELS[r], "permissions": sorted(ROLE_PERMISSIONS[r])} for r in ROLES],
            "permissions": ALL_PERMISSIONS}


# --------------------------------------------------------------------------- tenant & branding
@router.get("/tenant")
def get_tenant(p: Principal = Depends(require("tenant.manage", "policies.view")), db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    return {"tenant": row_dict(t), "branding": branding(t)}


@router.put("/tenant")
def update_tenant(body: dict, p: Principal = Depends(require_user("tenant.manage")), db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    before = row_dict(t)
    for field in ("legal_name", "display_name", "timezone", "base_currency", "locale", "region"):
        if body.get(field):
            value = str(body[field]).strip()
            if field == "base_currency" and not re.fullmatch(r"[A-Z]{3}", value.upper()):
                raise HTTPException(422, {"code": "INVALID", "message": "currency must be an ISO 4217 code"})
            setattr(t, field, value.upper() if field == "base_currency" else value)
    if isinstance(body.get("languages"), list):
        t.languages = [str(x) for x in body["languages"]][:6]
    if isinstance(body.get("settings"), dict):
        merged = copy.deepcopy(t.settings or {})
        for section in ("security", "retention", "governance", "role_limits"):
            if isinstance(body["settings"].get(section), dict):
                merged.setdefault(section, {}).update(body["settings"][section])
        if "require_email_verification" in body["settings"]:
            merged["require_email_verification"] = bool(body["settings"]["require_email_verification"])
        sec = merged.get("security", {})
        if int(sec.get("password_min_length", 10)) < 8:
            raise HTTPException(422, {"code": "INVALID", "message": "minimum password length cannot be below 8"})
        t.settings = merged
    db.commit()
    audit.record(db, p.tenant_id, "tenant.update", actor=p, resource_type="tenant", resource_id=t.id, before=before,
                 after=row_dict(t))
    return {"tenant": row_dict(t), "branding": branding(t)}


@router.get("/branding")
def get_branding(p: Principal = Depends(require("tenant.manage", "branding.reports", "reports.aml",
                                                "reports.executive")), db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    return {"branding": branding(t), "editable_fields": sorted(_editable_branding(p))}


def _editable_branding(p: Principal) -> set:
    fields: set = set()
    if p.can("tenant.manage"):
        fields |= TENANT_BRANDING_FIELDS - {"logo_key"}
    if p.can("branding.reports"):
        fields |= AML_BRANDING_FIELDS
    return fields


@router.put("/branding")
def update_branding(body: dict, p: Principal = Depends(require_user("tenant.manage", "branding.reports")),
                    db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    allowed = _editable_branding(p)
    rejected = [k for k in body if k not in allowed]
    if rejected:
        raise HTTPException(403, {"code": "BRANDING_SCOPE", "message": f"you cannot change: {', '.join(rejected)}"})
    for key in ("primary_color", "accent_color", "secondary_color"):
        if key in body and not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(body[key])):
            raise HTTPException(422, {"code": "INVALID_COLOR", "message": f"{key} must be #RRGGBB"})
    before = dict(t.branding or {})
    merged = dict(before)
    for k, v in body.items():
        merged[k] = v if not isinstance(v, str) else v[:400]
    if not merged.get("disclaimer"):
        merged["disclaimer"] = branding(t)["disclaimer"]      # mandatory disclaimer cannot be removed
    t.branding = merged
    db.commit()
    audit.record(db, p.tenant_id, "branding.update", actor=p, resource_type="tenant", resource_id=t.id,
                 before=before, after=merged)
    return {"branding": branding(t)}


@router.post("/branding/logo")
async def upload_logo(file: UploadFile = File(...), p: Principal = Depends(require_user("tenant.manage")),
                      db: Session = Depends(get_db)):
    content = await file.read()
    if len(content) > 1_000_000:
        raise HTTPException(413, {"code": "TOO_LARGE", "message": "logo must be under 1 MB"})
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    elif content[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    else:
        raise HTTPException(422, {"code": "INVALID_LOGO", "message": "logo must be a PNG or JPEG image"})
    settings.assets_dir.mkdir(parents=True, exist_ok=True)
    key = f"logo_{p.tenant_id}.{ext}"
    (settings.assets_dir / key).write_bytes(content)
    t = db.get(Tenant, p.tenant_id)
    t.branding = {**(t.branding or {}), "logo_key": key}
    db.commit()
    audit.record(db, p.tenant_id, "branding.logo_upload", actor=p, resource_type="tenant", resource_id=t.id,
                 after={"logo": key, "bytes": len(content)})
    return {"branding": branding(t)}


@router.get("/public/logo/{tenant_id}", include_in_schema=False)
def public_logo(tenant_id: str, db: Session = Depends(get_db)):
    t = db.get(Tenant, tenant_id.upper())
    key = (t.branding or {}).get("logo_key") if t else None
    if not key or not (settings.assets_dir / key).exists():
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "no logo"})
    return FileResponse(settings.assets_dir / key)


@router.get("/public/info", include_in_schema=False)
def public_info(db: Session = Depends(get_db)):
    """Unauthenticated: what the sign-in page needs (no sensitive data)."""
    t = db.get(Tenant, settings.tenant_code)
    if t is None:
        return {"ready": False}
    b = branding(t)
    bootstrap_pending = db.execute(select(User.id).where(User.tenant_id == t.id, User.is_bootstrap.is_(True),
                                                         User.must_change_password.is_(True))).scalar() is not None
    return {"ready": True, "tenant": t.id, "name": t.display_name, "demo_mode": t.demo_mode,
            "primary_color": b["primary_color"], "accent_color": b["accent_color"],
            "logo": f"/api/v1/public/logo/{t.id}" if b.get("logo_key") else None,
            "environment": settings.env, "bootstrap_pending": bootstrap_pending,
            "dev_accounts": settings.seed_demo_users and settings.env == "development" and settings.show_dev_accounts,
            "dev_fixed_passwords": settings.demo_fixed_passwords,
            "password_reset": _reset_channels(t)}


def _reset_channels(t: Tenant) -> dict:
    from ..services import notify
    dev = settings.env != "production" and settings.show_dev_accounts
    return {"email": notify.email_ready(t) or dev, "sms": notify.sms_ready(t) or dev,
            "development_codes": dev and not (notify.email_ready(t) or notify.sms_ready(t))}


# --------------------------------------------------------------------------- API clients
@router.get("/api-clients")
def list_api_clients(p: Principal = Depends(require("api_clients.manage")), db: Session = Depends(get_db)):
    rows = db.execute(select(ApiClient).where(ApiClient.tenant_id == p.tenant_id).order_by(ApiClient.created_at))\
        .scalars().all()
    return {"clients": [row_dict(c, exclude=("key_hash", "previous_key_hash")) for c in rows], "scopes": list(API_SCOPES)}


@router.post("/api-clients", status_code=201)
def create_api_client(body: dict, p: Principal = Depends(require_user("api_clients.manage")),
                      db: Session = Depends(get_db)):
    name = str(body.get("name") or "").strip()
    scopes = [s for s in body.get("scopes") or [] if s in API_SCOPES]
    if not name or not scopes:
        raise HTTPException(422, {"code": "INVALID", "message": "name and at least one valid scope are required"})
    key, prefix, hashed = new_api_key()
    client = ApiClient(tenant_id=p.tenant_id, space=p.space, name=name[:120], key_prefix=prefix, key_hash=hashed,
                       scopes=scopes, rate_limit_per_minute=int(body.get("rate_limit_per_minute") or 600),
                       created_by=p.user_id)
    db.add(client)
    db.commit()
    audit.record(db, p.tenant_id, "api_client.create", actor=p, resource_type="api_client", resource_id=client.id,
                 after={"name": name, "scopes": scopes, "space": p.space})
    return {"client": row_dict(client, exclude=("key_hash", "previous_key_hash")), "api_key": key,
            "note": f"Copy the key now - it is shown only once. It is bound to the {p.space} data space."}


@router.delete("/api-clients/{client_id}")
def revoke_api_client(client_id: str, p: Principal = Depends(require_user("api_clients.manage")),
                      db: Session = Depends(get_db)):
    c = db.get(ApiClient, client_id)
    if c is None or c.tenant_id != p.tenant_id:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "API client not found"})
    c.active = False
    db.commit()
    audit.record(db, p.tenant_id, "api_client.revoke", actor=p, resource_type="api_client", resource_id=c.id)
    return {"ok": True}


# --------------------------------------------------------------------------- Demo Mode
_demo_lock = threading.Lock()


@router.get("/demo")
def demo_status(p: Principal = Depends(require_user()), db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    return {"demo_mode": t.demo_mode, "scenarios": demo_service.SCENARIOS,
            "has_demo_data": demo_service.has_demo_data(db, t.id),
            "can_manage": p.can("demo.manage"), "can_inject": p.can("decisions.test") and t.demo_mode}


@router.post("/demo/mode")
def demo_mode(body: dict, p: Principal = Depends(require_user("demo.manage")), db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    before = t.demo_mode
    t.demo_mode = bool(body.get("enabled"))
    db.commit()
    audit.record(db, p.tenant_id, "demo.enable" if t.demo_mode else "demo.disable", actor=p, resource_type="tenant",
                 resource_id=t.id, before={"demo_mode": before}, after={"demo_mode": t.demo_mode})
    if t.demo_mode and not demo_service.has_demo_data(db, t.id):
        with _demo_lock:
            demo_service.seed(db, t.id, p.display)
    return {"demo_mode": t.demo_mode}


@router.post("/demo/reset")
def demo_reset(body: dict, p: Principal = Depends(require_user("demo.manage")), db: Session = Depends(get_db)):
    if body.get("confirm") != "RESET":
        raise HTTPException(422, {"code": "CONFIRMATION_REQUIRED", "message": "send {\"confirm\": \"RESET\"}"})
    if not _demo_lock.acquire(blocking=False):
        raise HTTPException(409, {"code": "BUSY", "message": "a demo reset is already running"})
    try:
        return demo_service.reset(db, p.tenant_id, p, reseed=body.get("reseed", True))
    finally:
        _demo_lock.release()


@router.post("/demo/scenarios")
def demo_scenario(body: dict, p: Principal = Depends(require_user("decisions.test")), db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    if not t.demo_mode:
        raise HTTPException(409, {"code": "DEMO_ONLY", "message": "scenarios can only be injected in Demo Mode"})
    try:
        results = demo_service.inject(db, p.tenant_id, body.get("scenario", ""), body.get("customer_ref") or None,
                                      actor=p.display, mode=body.get("mode") or "PRE_AUTH")
    except ValueError as exc:
        raise HTTPException(422, {"code": "INVALID", "message": str(exc)}) from exc
    audit.record(db, p.tenant_id, "demo.scenario", actor=p, resource_type="demo", resource_id=body.get("scenario"),
                 after={"transactions": [r["transaction_id"] for r in results]})
    return {"results": results}


# --------------------------------------------------------------------------- audit & incidents
@router.get("/audit")
def list_audit(action: str = "", actor: str = "", resource: str = "", outcome: str = "", limit: int = 200,
               offset: int = 0, p: Principal = Depends(require("audit.view")), db: Session = Depends(get_db)):
    q = select(AuditEvent).where(AuditEvent.tenant_id == p.tenant_id)
    if action:
        q = q.where(AuditEvent.action.like(f"%{action}%"))
    if actor:
        q = q.where(AuditEvent.actor_name.like(f"%{actor}%"))
    if resource:
        q = q.where((AuditEvent.resource_id.like(f"%{resource}%")) | (AuditEvent.resource_type == resource))
    if outcome:
        q = q.where(AuditEvent.outcome == outcome.upper())
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar()
    rows = db.execute(q.order_by(AuditEvent.id.desc()).offset(offset).limit(min(limit, 1000))).scalars().all()
    return {"total": total, "events": [row_dict(e) for e in rows]}


@router.get("/audit/verify")
def verify_audit(p: Principal = Depends(require("audit.view")), db: Session = Depends(get_db)):
    return audit.verify_chain(db, p.tenant_id)


@router.get("/incidents")
def incidents(p: Principal = Depends(require("system.view", "system.summary")), db: Session = Depends(get_db)):
    rows = db.execute(select(SystemIncident).where(SystemIncident.tenant_id == p.tenant_id)
                      .order_by(SystemIncident.last_seen_at.desc()).limit(200)).scalars().all()
    return {"incidents": [row_dict(i) for i in rows]}


INCIDENT_FLOW = ("DETECTED", "ACKNOWLEDGED", "INVESTIGATING", "MITIGATED", "RESOLVED", "POSTMORTEM")


@router.patch("/incidents/{incident_id}")
def update_incident(incident_id: str, body: dict, p: Principal = Depends(require_user("system.view")),
                    db: Session = Depends(get_db)):
    inc = db.get(SystemIncident, incident_id)
    if inc is None or inc.tenant_id != p.tenant_id:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "incident not found"})
    status = body.get("status")
    if status:
        if status not in INCIDENT_FLOW or INCIDENT_FLOW.index(status) < INCIDENT_FLOW.index(inc.status):
            raise HTTPException(409, {"code": "INVALID_TRANSITION", "message": f"cannot move to {status}"})
        inc.status = status
        if status == "RESOLVED":
            inc.resolved_at = utcnow()
    if body.get("owner") is not None:
        inc.owner = str(body["owner"])[:120]
    if body.get("notes"):
        inc.notes = (inc.notes + "\n" if inc.notes else "") + str(body["notes"])[:2000]
    inc.timeline = list(inc.timeline or []) + [{"at": utcnow().isoformat(timespec="seconds"), "status": inc.status,
                                                "by": p.display, "note": str(body.get("notes") or "")[:300]}]
    db.commit()
    audit.record(db, p.tenant_id, "incident.update", actor=p, resource_type="incident", resource_id=inc.id,
                 after={"status": inc.status, "owner": inc.owner})
    return {"incident": row_dict(inc)}


# --------------------------------------------------------------------------- health
def _db_ok() -> tuple[bool, str]:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, get_engine().dialect.name
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


@router.get("/system/health")
def system_health(p: Principal = Depends(require("system.view", "system.summary")), db: Session = Depends(get_db)):
    ok, dialect = _db_ok()
    t = db.get(Tenant, p.tenant_id)
    live = engine_registry.status(db, p.tenant_id, "LIVE")
    demo = engine_registry.status(db, p.tenant_id, "DEMO")
    conns = db.execute(select(Connector).where(Connector.tenant_id == p.tenant_id)).scalars().all()
    open_inc = db.execute(select(func.count(SystemIncident.id)).where(
        SystemIncident.tenant_id == p.tenant_id, SystemIncident.status.notin_(("RESOLVED", "POSTMORTEM")))).scalar()
    summary = {"database": {"ok": ok, "engine": dialect}, "decision_service": {"live": live["ready"],
                                                                               "demo": demo["ready"]},
               "connectors": {"total": len(conns), "enabled": sum(1 for c in conns if c.status == "ENABLED"),
                              "degraded": sum(1 for c in conns if c.status == "DEGRADED")},
               "open_incidents": open_inc, "demo_mode": t.demo_mode,
               "latency": {"api": metrics.latency_summary("api"), "decision": metrics.latency_summary("decision")}}
    if p.can("system.view"):
        dlq = db.execute(select(func.count(DeadLetterEvent.id)).where(
            DeadLetterEvent.tenant_id == p.tenant_id, DeadLetterEvent.status == "PENDING")).scalar()
        summary.update({"models": {"live": live, "demo": demo}, "dead_letters_pending": dlq,
                        "metrics": metrics.snapshot(), "version": __import__("verishield").__version__,
                        "environment": settings.env})
    return summary


health_router = APIRouter(tags=["operations"])


@health_router.get("/health")
def health():
    return {"status": "ok"}


@health_router.get("/ready")
def ready():
    from ..services import cluster
    ok, dialect = _db_ok()
    if not ok:
        raise HTTPException(503, {"status": "not ready", "database": dialect})
    return {"status": "ready", "database": dialect, "node": cluster.node_id(),
            "role": "LEADER" if cluster.is_leader() else "FOLLOWER"}


@health_router.get("/metrics", response_class=PlainTextResponse)
def prometheus(request: Request):
    import hmac
    if settings.metrics_token:
        auth = request.headers.get("authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {settings.metrics_token}"):
            raise HTTPException(401, {"code": "UNAUTHENTICATED", "message": "metrics token required"})
    from ..services.metrics import business_gauges
    return metrics.prometheus() + business_gauges()
