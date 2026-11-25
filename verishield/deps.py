"""Request authentication and authorisation (FastAPI dependencies).

Two kinds of callers:

* **Users** - ``Authorization: Bearer <session token>`` issued by ``/api/v1/auth/login``.
* **Systems** - ``X-API-Key: vs_xxx.yyy`` (or ``Authorization: ApiKey ...``) for the bank's
  payment switch, connectors and the MCP server. API clients carry explicit scopes.

The tenant always comes from the authenticated credential, never from the payload.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import ApiClient, Tenant, User, UserSession, utcnow
from .rbac import permissions_for
from .security import token_hash

# API scopes -> permissions they grant (system callers never get user-admin rights)
SCOPE_PERMISSIONS = {
    "decisions:write": {"decisions.evaluate"},
    "events:write": {"events.ingest"},
    "execution:write": {"execution.report"},
    "read:operations": {"connectors.view", "system.view", "models.view", "system.summary"},
    "read:aml": {"alerts.view_all", "cases.view_all", "tx.view_masked", "customers.view_masked", "policies.view"},
    "reports:generate": {"reports.system", "reports.aml"},
    "policies:simulate": {"policies.view", "policies.simulate"},
    "read:executive": {"system.summary", "models.view", "models.clusters"},
    "mcp:use": {"mcp.use"},
    # mobile / internet banking backend: customer answers in the app, card freeze, onboarding risk rating
    "confirmations:respond": {"confirmations.respond"},
    "cards:control": {"cards.customer"},
    "customers:risk": {"customers.risk_assess"},
}
API_SCOPES = tuple(SCOPE_PERMISSIONS)


@dataclass
class Principal:
    tenant_id: str
    kind: str                       # user | api
    role: str
    perms: set
    space: str
    display: str
    user_id: Optional[str] = None
    api_client_id: Optional[str] = None
    session_id: Optional[str] = None
    ip: str = ""
    restricted: bool = False        # first-login: password change / MFA enrolment pending
    restrictions: list = field(default_factory=list)

    def can(self, *perms: str) -> bool:
        return any(p in self.perms for p in perms)

    @property
    def demo(self) -> bool:
        return self.space == "DEMO"


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def session_restrictions(user: User, tenant: Tenant, mfa_required: bool) -> list[str]:
    out = []
    if user.must_change_password:
        out.append("CHANGE_PASSWORD")
    if user.is_bootstrap and (not user.email or not user.first_name or not user.last_name):
        out.append("COMPLETE_PROFILE")
    if mfa_required and not user.mfa_enabled:
        out.append("ENROL_MFA")
    if tenant.settings.get("require_email_verification") and not user.email_verified:
        out.append("VERIFY_EMAIL")
    return out


def tenant_mfa_required(tenant: Tenant, role: str) -> bool:
    from .settings import settings
    sec = tenant.settings.get("security", {})
    required = bool(sec.get("mfa_required", settings.require_mfa))
    if not required:
        return False
    roles = sec.get("mfa_roles") or ["IT_ADMIN", "AML_MANAGER", "AML_EMPLOYEE", "CHIEF_AML", "CEO"]
    return role in roles


# --------------------------------------------------------------------------- rate limiting
class RateLimiter:
    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


rate_limiter = RateLimiter()


# --------------------------------------------------------------------------- resolution
def _from_api_key(db: Session, key: str, request: Request) -> Principal:
    prefix = key.split(".", 1)[0]
    client = db.execute(select(ApiClient).where(ApiClient.key_prefix == prefix)).scalar()
    hashed = token_hash(key) if client is not None else ""
    in_grace = (client is not None and client.previous_key_hash == hashed and client.previous_expires_at is not None
                and client.previous_expires_at > utcnow())
    if client is None or not client.active or (client.key_hash != hashed and not in_grace):
        raise HTTPException(401, {"code": "INVALID_API_KEY", "message": "API key is invalid or revoked"})
    if not rate_limiter.allow(f"api:{client.id}", client.rate_limit_per_minute):
        raise HTTPException(429, {"code": "RATE_LIMITED", "message": "rate limit exceeded; retry later"})
    perms: set = set()
    for scope in client.scopes or []:
        perms |= SCOPE_PERMISSIONS.get(scope, set())
    now = utcnow()
    if client.last_used_at is None or now - client.last_used_at > timedelta(seconds=30):
        client.last_used_at = now
        db.commit()
    return Principal(tenant_id=client.tenant_id, kind="api", role="SYSTEM", perms=perms,
                     space=client.space or "LIVE", display=f"API client {client.name}",
                     api_client_id=client.id, ip=client_ip(request))


def _from_session(db: Session, token: str, request: Request) -> Principal:
    sess = db.execute(select(UserSession).where(UserSession.token_hash == token_hash(token))).scalar()
    now = utcnow()
    if sess is None or sess.revoked or sess.expires_at < now:
        raise HTTPException(401, {"code": "SESSION_EXPIRED", "message": "Please sign in again"})
    user = db.get(User, sess.user_id)
    tenant = db.get(Tenant, sess.tenant_id)
    if user is None or user.status in ("DISABLED", "LOCKED"):
        raise HTTPException(401, {"code": "ACCOUNT_UNAVAILABLE", "message": "Account is locked or disabled"})
    idle = int(tenant.settings.get("security", {}).get("session_idle_minutes", 60))
    if now - sess.last_seen_at > timedelta(minutes=idle):
        sess.revoked = True
        db.commit()
        raise HTTPException(401, {"code": "SESSION_IDLE", "message": "Session timed out due to inactivity"})
    if now - sess.last_seen_at > timedelta(seconds=20):
        sess.last_seen_at = now
        db.commit()
    mfa_required = tenant_mfa_required(tenant, user.role)
    restrictions = session_restrictions(user, tenant, mfa_required)
    if user.mfa_enabled and not sess.mfa_passed:
        raise HTTPException(401, {"code": "MFA_REQUIRED", "message": "Multi-factor verification required"})
    return Principal(tenant_id=user.tenant_id, kind="user", role=user.role,
                     perms=permissions_for(user.role, user.extra_permissions),
                     space="DEMO" if tenant.demo_mode else "LIVE", display=user.full_name, user_id=user.id,
                     session_id=sess.id, ip=client_ip(request), restricted=bool(restrictions),
                     restrictions=restrictions)


def resolve_principal(request: Request, db: Session) -> Principal:
    api_key = request.headers.get("x-api-key")
    auth = request.headers.get("authorization", "")
    if not api_key and auth.lower().startswith("apikey "):
        api_key = auth[7:].strip()
    if api_key:
        return _from_api_key(db, api_key.strip(), request)
    if auth.lower().startswith("bearer "):
        return _from_session(db, auth[7:].strip(), request)
    raise HTTPException(401, {"code": "UNAUTHENTICATED", "message": "Authentication required"})


def current_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    """Any authenticated caller, including sessions that still have first-login steps pending."""
    principal = resolve_principal(request, db)
    request.state.principal = principal
    return principal


def principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    """Authenticated caller with no pending first-login restriction."""
    p = current_principal(request, db)
    if p.restricted:
        raise HTTPException(403, {"code": "SETUP_REQUIRED", "message": "Complete first-login steps first",
                                  "restrictions": p.restrictions})
    return p


def require(*perms: str) -> Callable:
    """Dependency factory: the caller must hold at least one of ``perms``."""

    def checker(p: Principal = Depends(principal)) -> Principal:
        if perms and not p.can(*perms):
            raise HTTPException(403, {"code": "FORBIDDEN", "message": "You do not have permission for this action",
                                      "required": list(perms)})
        return p

    return checker


def require_user(*perms: str) -> Callable:
    """Like :func:`require` but only interactive users (not API keys)."""

    def checker(p: Principal = Depends(require(*perms) if perms else principal)) -> Principal:
        if p.kind != "user":
            raise HTTPException(403, {"code": "USER_ONLY", "message": "This action requires a signed-in user"})
        return p

    return checker
