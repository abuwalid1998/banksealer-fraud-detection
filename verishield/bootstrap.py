"""First-run initialisation: schema, tenant, bootstrap IT Admin, initial users, policy, Demo data.

Safe to run on every start: it only creates what is missing and never clears existing records.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy import select

from . import audit
from .db import create_schema, init_engine, session_scope
from .models import Tenant, User, utcnow
from .security import hash_password
from .services import demo, engine_registry, policy
from .services.connectors import start_enabled
from .settings import settings

log = logging.getLogger("verishield.bootstrap")

DEFAULT_TENANT_SETTINGS = {
    "security": {"mfa_required": None, "mfa_roles": ["IT_ADMIN", "AML_MANAGER", "AML_EMPLOYEE", "CHIEF_AML"],
                 "password_min_length": 10, "password_require_symbol": True, "temp_password_hours": 72,
                 "session_idle_minutes": 60, "session_absolute_hours": 10, "lockout_threshold": 5},
    "require_email_verification": False,
    "role_limits": {"AML_MANAGER": 1, "CHIEF_AML": 1},
    "retention": {"transactions_days": 1825, "cases_days": 1825, "audit_days": 2555, "reports_days": 1825},
    "governance": {"maker_checker": False},
    "demo": {"seed": 2026},
}

# Development-only initial accounts (BRD 5.2): temporary passwords, must be changed at first login.
DEMO_USERS = [
    ("2000", "Amal", "Mansour", "aml.manager@verishield.local", "AML_MANAGER", "Manager#2026"),
    ("3001", "Yazan", "Odeh", "analyst1@verishield.local", "AML_EMPLOYEE", "Analyst1#2026"),
    ("3002", "Rania", "Khoury", "analyst2@verishield.local", "AML_EMPLOYEE", "Analyst2#2026"),
    ("4000", "Samir", "Haddad", "chief.aml@verishield.local", "CHIEF_AML", "Chief#2026"),
]


def ensure_tenant(db, code: str, name: str, currency: str, tz: str, demo_mode: bool) -> Tenant:
    tenant = db.get(Tenant, code)
    if tenant is None:
        s = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_TENANT_SETTINGS.items()}
        s["security"]["mfa_required"] = settings.require_mfa
        tenant = Tenant(id=code, legal_name=name, display_name=name, timezone=tz, base_currency=currency,
                        region="PS" if code == "BOP" else "", demo_mode=demo_mode, settings=s,
                        branding={"display_name": name, "numbering_prefix": code})
        db.add(tenant)
        db.flush()
        audit.record(db, code, "tenant.create", actor="installer", resource_type="tenant", resource_id=code,
                     after={"name": name, "currency": currency, "timezone": tz}, commit=False)
        log.info("created tenant %s (%s)", code, name)
    return tenant


def ensure_bootstrap_admin(db, tenant: Tenant) -> Optional[User]:
    exists = db.execute(select(User).where(User.tenant_id == tenant.id)).scalars().first()
    if exists:
        return None
    admin = User(tenant_id=tenant.id, username=settings.bootstrap_username, role="IT_ADMIN", status="INVITED",
                 password_hash=hash_password(settings.bootstrap_password), must_change_password=True,
                 is_bootstrap=True, created_by="installer")
    db.add(admin)
    db.flush()
    audit.record(db, tenant.id, "user.bootstrap_create", actor="installer", resource_type="user",
                 resource_id=admin.id, after={"username": admin.username, "role": "IT_ADMIN"}, commit=False)
    log.warning("bootstrap IT Admin created: username %s - the first-run password must be changed at first login",
                admin.username)
    return admin


def ensure_demo_users(db, tenant: Tenant) -> None:
    admin = db.execute(select(User).where(User.tenant_id == tenant.id, User.is_bootstrap.is_(True))).scalar()
    for username, first, last, email, role, password in DEMO_USERS:
        if db.execute(select(User).where(User.tenant_id == tenant.id, User.username == username)).scalar():
            continue
        db.add(User(tenant_id=tenant.id, username=username, first_name=first, last_name=last, email=email, role=role,
                    status="INVITED", password_hash=hash_password(password), must_change_password=True,
                    temp_password_expires_at=utcnow() + timedelta(days=30), created_by=admin.id if admin else None))
        audit.record(db, tenant.id, "user.create", actor="installer (development seed)", resource_type="user",
                     resource_id=username, after={"username": username, "role": role}, commit=False)


def initialise(seed_demo: Optional[bool] = None, quiet: bool = False) -> dict:
    """Create/upgrade the schema and first-run records. Returns a summary for the console."""
    init_engine(settings.database_url)
    create_schema()
    seed_demo = settings.seed_demo if seed_demo is None else seed_demo
    summary = {"tenant": settings.tenant_code, "seeded": False}
    with session_scope() as db:
        tenant = ensure_tenant(db, settings.tenant_code, settings.tenant_name, settings.tenant_currency,
                               settings.tenant_timezone, demo_mode=seed_demo)
        created_admin = ensure_bootstrap_admin(db, tenant)
        summary["bootstrap_created"] = created_admin is not None
        if settings.seed_demo_users:
            ensure_demo_users(db, tenant)
        policy.ensure_default_policy(db, tenant.id)
    with session_scope() as db:
        engine_registry.load_active_models(db)
        if seed_demo and not demo.has_demo_data(db, settings.tenant_code):
            if not quiet:
                log.warning("preparing synthetic Demo Mode data (first start only, ~20-60 s)...")
            summary["seed"] = demo.seed(db, settings.tenant_code, "installer")
            summary["seeded"] = True
        elif seed_demo and settings.demo_live_channels:
            from .models import Connector
            has_channels = db.execute(select(Connector.id).where(Connector.tenant_id == settings.tenant_code,
                                                                 Connector.space == "DEMO").limit(1)).scalar()
            if not has_channels:          # installations seeded before live channels existed
                demo.create_live_channels(db, settings.tenant_code, "installer")
        if settings.background_jobs:
            start_enabled(db)
    return summary
