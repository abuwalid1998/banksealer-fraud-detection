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
    "security": {"mfa_required": None, "mfa_roles": ["IT_ADMIN", "AML_MANAGER", "AML_EMPLOYEE", "CHIEF_AML", "CEO"],
                 "password_min_length": 10, "password_require_symbol": True, "temp_password_hours": 72,
                 "session_idle_minutes": 60, "session_absolute_hours": 10, "lockout_threshold": 5},
    "require_email_verification": False,
    "role_limits": {"AML_MANAGER": 1, "CHIEF_AML": 1, "CEO": 1},
    "retention": {"transactions_days": 1825, "cases_days": 1825, "audit_days": 2555, "reports_days": 1825},
    "governance": {"maker_checker": False},
    "demo": {"seed": 2026},
}

# Development / test accounts. With ``demo_fixed_passwords`` (default outside production) they are ready to
# use - no first-login password change; otherwise their passwords are temporary (BRD 5.2).
DEMO_USERS = [
    ("2000", "Amal", "Mansour", "aml.manager@verishield.local", "AML_MANAGER", "Manager#2026"),
    ("3001", "Yazan", "Odeh", "analyst1@verishield.local", "AML_EMPLOYEE", "Analyst1#2026"),
    ("3002", "Rania", "Khoury", "analyst2@verishield.local", "AML_EMPLOYEE", "Analyst2#2026"),
    ("4000", "Samir", "Haddad", "chief.aml@verishield.local", "CHIEF_AML", "Chief#2026"),
    ("5000", "Hashem", "Shawa", "ceo@verishield.local", "CEO", "Ceo#2026"),
]
DEMO_ADMIN_PROFILE = ("System", "Administrator", "it.admin@verishield.local")


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
    fixed = settings.demo_fixed_passwords
    admin = User(tenant_id=tenant.id, username=settings.bootstrap_username, role="IT_ADMIN",
                 status="ACTIVE" if fixed else "INVITED", password_hash=hash_password(settings.bootstrap_password),
                 must_change_password=not fixed, is_bootstrap=True, created_by="installer")
    if fixed:
        admin.first_name, admin.last_name, admin.email = DEMO_ADMIN_PROFILE
    db.add(admin)
    db.flush()
    audit.record(db, tenant.id, "user.bootstrap_create", actor="installer", resource_type="user",
                 resource_id=admin.id, after={"username": admin.username, "role": "IT_ADMIN"}, commit=False)
    log.warning("bootstrap IT Admin created: username %s - %s", admin.username,
                "test install: the published password stays valid (no first-login change)" if fixed else
                "the first-run password must be changed at first login")
    return admin


def ensure_demo_users(db, tenant: Tenant) -> None:
    admin = db.execute(select(User).where(User.tenant_id == tenant.id, User.is_bootstrap.is_(True))).scalar()
    for username, first, last, email, role, password in DEMO_USERS:
        if db.execute(select(User).where(User.tenant_id == tenant.id, User.username == username)).scalar():
            continue
        fixed = settings.demo_fixed_passwords
        db.add(User(tenant_id=tenant.id, username=username, first_name=first, last_name=last, email=email, role=role,
                    status="ACTIVE" if fixed else "INVITED", password_hash=hash_password(password),
                    must_change_password=not fixed,
                    temp_password_expires_at=None if fixed else utcnow() + timedelta(days=30),
                    created_by=admin.id if admin else None))
        audit.record(db, tenant.id, "user.create", actor="installer (development seed)", resource_type="user",
                     resource_id=username, after={"username": username, "role": role}, commit=False)


def _manual_channel_start(db, tenant_id: str) -> None:
    """Channels start only when an IT Admin starts them (upgrade of installs that auto-enabled Demo channels)."""
    from .models import Connector
    tenant = db.get(Tenant, tenant_id)
    flags = dict(tenant.settings or {})
    if (flags.get("demo") or {}).get("manual_channels"):
        return
    for c in db.execute(select(Connector).where(Connector.tenant_id == tenant_id, Connector.space == "DEMO",
                                                Connector.created_by.is_(None),
                                                Connector.status.in_(("ENABLED", "DEGRADED")))).scalars():
        c.status = "VALIDATED"
    flags["demo"] = {**(flags.get("demo") or {}), "manual_channels": True}
    tenant.settings = flags
    db.commit()


def apply_fixed_demo_passwords(db, tenant: Tenant) -> int:
    """Test/demo installs: accounts still on their published default password skip the first-login change."""
    if not settings.demo_fixed_passwords:
        return 0
    from .security import verify_password
    defaults = {u[0]: u[5] for u in DEMO_USERS}
    defaults[settings.bootstrap_username] = settings.bootstrap_password
    n = 0
    for user in db.execute(select(User).where(User.tenant_id == tenant.id, User.username.in_(list(defaults)),
                                              User.must_change_password.is_(True))).scalars():
        if verify_password(defaults[user.username], user.password_hash):
            user.must_change_password = False
            user.temp_password_expires_at = None
            if user.status == "INVITED":
                user.status = "ACTIVE"
            if user.is_bootstrap and not (user.first_name and user.last_name and user.email):
                user.first_name, user.last_name, user.email = DEMO_ADMIN_PROFILE
            n += 1
    return n


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
        apply_fixed_demo_passwords(db, tenant)
        policy.ensure_default_policy(db, tenant.id)
    with session_scope() as db:
        from .services import model_ops
        for t in db.execute(select(Tenant)).scalars():
            model_ops.cache_overrides(t)
        engine_registry.load_active_models(db)
        if seed_demo and not demo.has_demo_data(db, settings.tenant_code):
            if not quiet:
                log.warning("preparing synthetic Demo Mode data (first start only, ~20-60 s)...")
            summary["seed"] = demo.seed(db, settings.tenant_code, "installer")
            summary["seeded"] = True
        elif seed_demo and settings.demo_live_channels:
            from .models import Connector
            has_results = db.execute(select(Connector.id).where(Connector.tenant_id == settings.tenant_code,
                                                                Connector.space == "DEMO",
                                                                Connector.type == "db_results").limit(1)).scalar()
            if not has_results:           # installations seeded before these channels existed
                demo.create_live_channels(db, settings.tenant_code, "installer")
        if seed_demo:
            _manual_channel_start(db, settings.tenant_code)
        if settings.background_jobs:
            start_enabled(db)
    return summary
