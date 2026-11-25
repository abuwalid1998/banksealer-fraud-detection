"""Core banking API registry: connections, operations (APIs), purposes, tests, manual operations with approval
and the call log. Configuration is for the IT Admin; AML users request and approve operations."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import Principal, require, require_user
from ..models import CoreApi, CoreApiCall, CoreConnection, Tenant, utcnow
from ..services import core_api as svc

router = APIRouter(prefix="/api/v1/core", tags=["core banking"])

CONN_FIELDS = ("name", "base_url", "auth_type", "auth", "verify_tls", "ca_file", "client_cert_file", "client_key_file",
               "timeout_seconds", "retries", "default_headers", "enabled")
API_FIELDS = ("name", "purpose", "description", "connection_id", "method", "path", "query", "headers", "body_type",
              "body_template", "inputs", "output_mapping", "success_codes", "success_field", "success_values",
              "idempotency_header", "timeout_seconds", "retries", "requires_approval", "allowed_roles", "live_enabled",
              "demo_enabled", "priority")


def _bad(status: int, message: str, code: str = "INVALID"):
    return HTTPException(status, {"code": code, "message": message})


def _tenant(db: Session, p: Principal) -> Tenant:
    return db.get(Tenant, p.tenant_id)


def _conn(db: Session, p: Principal, cid: str) -> CoreConnection:
    c = db.get(CoreConnection, cid)
    if c is None or c.tenant_id != p.tenant_id:
        raise _bad(404, "connection not found", "NOT_FOUND")
    return c


def _api(db: Session, p: Principal, aid: str) -> CoreApi:
    a = db.get(CoreApi, aid)
    if a is None or a.tenant_id != p.tenant_id:
        raise _bad(404, "operation not found", "NOT_FOUND")
    return a


# =========================================================================== overview
@router.get("/overview")
def overview(p: Principal = Depends(require("core.configure", "core.view", "core.execute", "core.approve")),
             db: Session = Depends(get_db)):
    t = _tenant(db, p)
    conns = db.execute(select(CoreConnection).where(CoreConnection.tenant_id == p.tenant_id)
                       .order_by(CoreConnection.created_at)).scalars().all()
    apis = db.execute(select(CoreApi).where(CoreApi.tenant_id == p.tenant_id)
                      .order_by(CoreApi.purpose, CoreApi.priority)).scalars().all()
    since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    stats = dict(db.execute(select(CoreApiCall.status, func.count()).where(
        CoreApiCall.tenant_id == p.tenant_id, CoreApiCall.space == p.space, CoreApiCall.created_at >= since)
        .group_by(CoreApiCall.status)).all())
    pending = db.execute(select(func.count()).select_from(CoreApiCall).where(
        CoreApiCall.tenant_id == p.tenant_id, CoreApiCall.space == p.space,
        CoreApiCall.status == "PENDING_APPROVAL")).scalar()
    purposes = svc.purposes(t)
    for code, pp in purposes.items():
        pp["apis"] = sum(1 for a in apis if a.purpose == code)
        pp["active"] = svc.available(db, p.tenant_id, p.space, code)
    return {"purposes": purposes, "connections": [svc.as_connection(c) for c in conns] if p.can("core.configure") else [],
            "apis": [svc.as_api(a) for a in apis], "today": stats, "pending_approvals": pending, "space": p.space,
            "enums": {"auth_types": svc.AUTH_TYPES, "methods": svc.METHODS, "body_types": svc.BODY_TYPES,
                      "categories": svc.CATEGORIES, "roles": ["AML_MANAGER", "CHIEF_AML", "AML_EMPLOYEE", "IT_ADMIN", "CEO"]},
            "can": {"configure": p.can("core.configure"), "execute": p.can("core.execute"),
                    "approve": p.can("core.approve"), "view": p.can("core.view")}}


# =========================================================================== connections
def _clean_conn(body: dict) -> dict:
    data = {k: v for k, v in (body or {}).items() if k in CONN_FIELDS}
    if "auth_type" in data and data["auth_type"] not in svc.AUTH_TYPES:
        raise _bad(422, f"auth type must be one of {', '.join(svc.AUTH_TYPES)}")
    if "base_url" in data:
        url = str(data["base_url"]).strip()
        if not url.startswith(("https://", "http://", "mock://")):
            raise _bad(422, "base URL must start with https://, http:// or mock://")
        data["base_url"] = url.rstrip("/")
    if data.get("auth_type") == "OAUTH2_CC" and not (data.get("auth") or {}).get("token_url"):
        raise _bad(422, "OAuth2 client credentials need a token URL")
    for k in ("timeout_seconds", "retries"):
        if k in data and data[k] not in (None, ""):
            data[k] = float(data[k]) if k == "timeout_seconds" else int(data[k])
            if (k == "timeout_seconds" and not 0.5 <= data[k] <= 120) or (k == "retries" and not 0 <= data[k] <= 5):
                raise _bad(422, "timeout 0.5-120 s, retries 0-5")
    return data


@router.post("/connections", status_code=201)
def create_connection(body: dict, p: Principal = Depends(require_user("core.configure")), db: Session = Depends(get_db)):
    data = _clean_conn(body)
    if not data.get("name") or not data.get("base_url"):
        raise _bad(422, "name and base URL are required")
    c = CoreConnection(tenant_id=p.tenant_id, created_by=p.display, **data)
    db.add(c)
    db.commit()
    audit.record(db, p.tenant_id, "core.connection_create", actor=p, resource_type="core_connection", resource_id=c.id,
                 after=svc.as_connection(c))
    return svc.as_connection(c)


@router.put("/connections/{cid}")
def update_connection(cid: str, body: dict, p: Principal = Depends(require_user("core.configure")),
                      db: Session = Depends(get_db)):
    c = _conn(db, p, cid)
    before = svc.as_connection(c)
    for k, v in _clean_conn(body).items():
        setattr(c, k, v)
    c.updated_at = utcnow()
    db.commit()
    audit.record(db, p.tenant_id, "core.connection_update", actor=p, resource_type="core_connection", resource_id=c.id,
                 before=before, after=svc.as_connection(c))
    return svc.as_connection(c)


@router.delete("/connections/{cid}")
def delete_connection(cid: str, p: Principal = Depends(require_user("core.configure")), db: Session = Depends(get_db)):
    c = _conn(db, p, cid)
    if db.execute(select(CoreApi.id).where(CoreApi.connection_id == cid)).first():
        raise _bad(409, "operations still use this connection")
    db.delete(c)
    db.commit()
    audit.record(db, p.tenant_id, "core.connection_delete", actor=p, resource_type="core_connection", resource_id=cid)
    return {"deleted": cid}


@router.post("/connections/{cid}/secret")
def connection_secret(cid: str, body: dict, p: Principal = Depends(require_user("core.configure")),
                      db: Session = Depends(get_db)):
    from ..services.connectors import store_secret
    c = _conn(db, p, cid)
    value = str(body.get("value") or "")
    if not value:
        raise _bad(422, "send {value}")
    c.secret_ref = store_secret(db, p.tenant_id, f"core banking: {c.name}", value, p.user_id, c.secret_ref)
    c.updated_at = utcnow()
    db.commit()
    audit.record(db, p.tenant_id, "core.connection_secret", actor=p, resource_type="core_connection", resource_id=c.id)
    return svc.as_connection(c)


@router.post("/connections/{cid}/test")
def connection_test(cid: str, p: Principal = Depends(require_user("core.configure")), db: Session = Depends(get_db)):
    c = _conn(db, p, cid)
    ok, detail = svc.test_connection(db, c)
    audit.record(db, p.tenant_id, "core.connection_test", actor=p, resource_type="core_connection", resource_id=c.id,
                 outcome="SUCCESS" if ok else "FAILURE", reason=detail)
    return {"ok": ok, "detail": detail, "connection": svc.as_connection(c)}


# =========================================================================== operations (APIs)
def _clean_api(db: Session, p: Principal, body: dict, partial: bool) -> dict:
    data = {k: v for k, v in (body or {}).items() if k in API_FIELDS}
    if not partial and (not data.get("name") or not data.get("purpose") or not data.get("connection_id")):
        raise _bad(422, "name, purpose and connection are required")
    if "purpose" in data and data["purpose"] not in svc.purposes(_tenant(db, p)):
        raise _bad(422, "unknown purpose - add it in the Purposes tab first")
    if "connection_id" in data:
        _conn(db, p, data["connection_id"])
    if "method" in data:
        data["method"] = str(data["method"]).upper()
    if "success_codes" in data:
        data["success_codes"] = [int(x) for x in data["success_codes"] or []]
    errs = svc.validate_api(data)
    if errs:
        raise _bad(422, "; ".join(errs))
    return data


@router.post("/apis", status_code=201)
def create_api(body: dict, p: Principal = Depends(require_user("core.configure")), db: Session = Depends(get_db)):
    data = _clean_api(db, p, body, False)
    if not data.get("inputs"):
        data["inputs"] = svc.purposes(_tenant(db, p))[data["purpose"]].get("inputs", [])
    a = CoreApi(tenant_id=p.tenant_id, created_by=p.display, updated_by=p.display, **data)
    db.add(a)
    db.commit()
    audit.record(db, p.tenant_id, "core.api_create", actor=p, resource_type="core_api_def", resource_id=a.id,
                 after=svc.as_api(a))
    return svc.as_api(a)


@router.put("/apis/{aid}")
def update_api(aid: str, body: dict, p: Principal = Depends(require_user("core.configure")), db: Session = Depends(get_db)):
    a = _api(db, p, aid)
    before = svc.as_api(a)
    for k, v in _clean_api(db, p, body, True).items():
        setattr(a, k, v)
    a.version, a.updated_by, a.updated_at = (a.version or 1) + 1, p.display, utcnow()
    db.commit()
    audit.record(db, p.tenant_id, "core.api_update", actor=p, resource_type="core_api_def", resource_id=a.id,
                 before=before, after=svc.as_api(a))
    return svc.as_api(a)


@router.post("/apis/{aid}/duplicate", status_code=201)
def duplicate_api(aid: str, p: Principal = Depends(require_user("core.configure")), db: Session = Depends(get_db)):
    a = _api(db, p, aid)
    data = {k: getattr(a, k) for k in API_FIELDS}
    data.update(name=f"{a.name} (copy)", live_enabled=False)
    b = CoreApi(tenant_id=p.tenant_id, created_by=p.display, updated_by=p.display, **data)
    db.add(b)
    db.commit()
    audit.record(db, p.tenant_id, "core.api_create", actor=p, resource_type="core_api_def", resource_id=b.id,
                 after={"copied_from": a.id})
    return svc.as_api(b)


@router.delete("/apis/{aid}")
def delete_api(aid: str, p: Principal = Depends(require_user("core.configure")), db: Session = Depends(get_db)):
    a = _api(db, p, aid)
    db.delete(a)
    db.commit()
    audit.record(db, p.tenant_id, "core.api_delete", actor=p, resource_type="core_api_def", resource_id=aid)
    return {"deleted": aid}


@router.post("/apis/{aid}/test")
def test_api(aid: str, body: Optional[dict] = None, p: Principal = Depends(require_user("core.configure")),
             db: Session = Depends(get_db)):
    """Render the request with sample inputs (dry run) or send it for real (``send: true``)."""
    a = _api(db, p, aid)
    body = body or {}
    inputs = body.get("inputs") or {i["name"]: i.get("example") for i in (a.inputs or []) if i.get("example")}
    call = svc.execute(db, p.tenant_id, p.space, a.purpose, inputs, p.display, "TEST",
                       str(body.get("reason") or "configuration test"), api_id=a.id, dry_run=not body.get("send"))
    return svc.as_call(call)


# =========================================================================== purposes
@router.put("/purposes/{code}")
def put_purpose(code: str, body: dict, p: Principal = Depends(require_user("core.configure")), db: Session = Depends(get_db)):
    t = _tenant(db, p)
    try:
        out = svc.save_purpose(db, t, code, body)
    except ValueError as exc:
        raise _bad(422, str(exc)) from exc
    audit.record(db, p.tenant_id, "core.purpose_save", actor=p, resource_type="core_purpose", resource_id=out["code"],
                 after=out)
    return out


@router.delete("/purposes/{code}")
def del_purpose(code: str, p: Principal = Depends(require_user("core.configure")), db: Session = Depends(get_db)):
    try:
        svc.delete_purpose(db, _tenant(db, p), code)
    except ValueError as exc:
        raise _bad(409, str(exc)) from exc
    audit.record(db, p.tenant_id, "core.purpose_delete", actor=p, resource_type="core_purpose", resource_id=code)
    return {"deleted": code}


# =========================================================================== operations requested by people
@router.get("/operations")
def available_operations(p: Principal = Depends(require("core.execute", "core.approve")), db: Session = Depends(get_db)):
    """Purposes the caller may request in the current workspace (with an enabled operation)."""
    t = _tenant(db, p)
    out = []
    for code, pp in svc.purposes(t).items():
        api = svc.select_api(db, p.tenant_id, p.space, code)
        if api is None:
            continue
        out.append({"purpose": code, "label": pp["label"], "category": pp.get("category"), "description": pp.get("description"),
                    "api_id": api.id, "api_name": api.name, "inputs": api.inputs or pp.get("inputs", []),
                    "requires_approval": api.requires_approval,
                    "allowed": p.role in (api.allowed_roles or svc.DEFAULT_ROLES)})
    return {"operations": out, "space": p.space}


@router.post("/operations", status_code=201)
def request_operation(body: dict, p: Principal = Depends(require_user("core.execute")), db: Session = Depends(get_db)):
    try:
        call = svc.request_operation(db, _tenant(db, p), p.space, str(body.get("purpose") or ""), body.get("inputs") or {},
                                     p, str(body.get("reason") or ""), body.get("api_id"))
    except LookupError as exc:
        raise _bad(404, str(exc), "NO_API") from exc
    except PermissionError as exc:
        raise _bad(403, str(exc), "ROLE_NOT_ALLOWED") from exc
    except ValueError as exc:
        raise _bad(422, str(exc)) from exc
    return svc.as_call(call)


@router.post("/calls/{cid}/{action}")
def decide_call(cid: str, action: str, p: Principal = Depends(require_user("core.approve")), db: Session = Depends(get_db)):
    if action not in ("approve", "reject"):
        raise _bad(404, "unknown action", "NOT_FOUND")
    try:
        call = svc.decide(db, _tenant(db, p), p.space, cid, action == "approve", p)
    except LookupError as exc:
        raise _bad(404, str(exc), "NOT_FOUND") from exc
    except PermissionError as exc:
        raise _bad(403, str(exc), "FOUR_EYES") from exc
    except ValueError as exc:
        raise _bad(409, str(exc)) from exc
    return svc.as_call(call)


@router.get("/calls")
def list_calls(status: str = "", purpose: str = "", target: str = "", limit: int = 200,
               p: Principal = Depends(require("core.view", "core.configure", "core.approve")), db: Session = Depends(get_db)):
    q = select(CoreApiCall).where(CoreApiCall.tenant_id == p.tenant_id, CoreApiCall.space == p.space)
    if status:
        q = q.where(CoreApiCall.status == status)
    if purpose:
        q = q.where(CoreApiCall.purpose == purpose)
    if target:
        q = q.where(CoreApiCall.target_ref == target)
    rows = db.execute(q.order_by(CoreApiCall.created_at.desc()).limit(max(1, min(limit, 500)))).scalars().all()
    return {"calls": [svc.as_call(c) for c in rows]}


__all__ = ["router"]
