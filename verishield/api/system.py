"""IT Admin system configuration: e-mail & SMS gateways, the fraud-engine database, AI-agent (MCP) access."""
from __future__ import annotations

import copy
import json
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db, get_engine
from ..deps import Principal, require, require_user
from ..models import (Alert, ApiClient, AuditEvent, Case, Connector, Customer, Decision, NotificationLog, Tenant,
                      Transaction)
from ..security import new_api_key, sanitize_error
from ..services import notify
from ..services.common import iso, row_dict
from ..services.connectors import public as connector_public
from ..services.connectors import store_secret
from ..settings import ROOT, settings

router = APIRouter(prefix="/api/v1", tags=["system configuration"])
mcp_router = APIRouter(tags=["ai agents"])


def _bad(message: str, code: str = "INVALID", status: int = 422):
    raise HTTPException(status, {"code": code, "message": message})


# =========================================================================== e-mail & SMS
@router.get("/notifications")
def get_notifications(p: Principal = Depends(require("notifications.manage")), db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    outbox = db.execute(select(NotificationLog).where(NotificationLog.tenant_id == p.tenant_id)
                        .order_by(NotificationLog.id.desc()).limit(60)).scalars().all()
    return {"config": notify.public_config(t), "security_options": notify.SECURITY,
            "sms_methods": notify.SMS_METHODS, "outbox": [row_dict(o) for o in outbox],
            "events": {"password_reset": "Forgotten-password codes and IT Admin password resets",
                       "account_created": "New account credentials (when the IT Admin chooses to send them)",
                       "case_assigned": "Case assigned to an AML Employee",
                       "critical_alert": "CRITICAL alerts to the AML Manager and Chief AML"}}


@router.put("/notifications")
def put_notifications(body: dict, p: Principal = Depends(require_user("notifications.manage")),
                      db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    before = notify.public_config(t)
    current = copy.deepcopy((t.settings or {}).get("notifications") or {})
    e, s, ev = body.get("email") or {}, body.get("sms") or {}, body.get("events") or {}
    pu = body.get("push") or {}
    for key in ("password_ref", "token_ref", "secret_ref"):
        e.pop(key, None)
        s.pop(key, None)
        pu.pop(key, None)
    if pu:
        if pu.get("url") and not str(pu["url"]).startswith(("http://", "https://")):
            _bad("the mobile push URL must start with http:// or https://")
        allowed = set(notify.DEFAULTS["push"]) - {"secret_ref"}
        current["push"] = {**(current.get("push") or {}), **{k: v for k, v in pu.items() if k in allowed}}
    if e:
        if e.get("security") and e["security"] not in notify.SECURITY:
            _bad(f"security must be one of {', '.join(notify.SECURITY)}")
        if e.get("port") not in (None, "") and not 0 < int(e["port"]) < 65536:
            _bad("invalid SMTP port")
        if e.get("from_address") and "@" not in str(e["from_address"]):
            _bad("sender address must be an e-mail address")
        allowed = set(notify.DEFAULTS["email"]) - {"password_ref"}
        current["email"] = {**(current.get("email") or {}),
                            **{k: v for k, v in e.items() if k in allowed}}
        if "port" in e and e["port"] not in (None, ""):
            current["email"]["port"] = int(e["port"])
    if s:
        if s.get("method") and s["method"] not in notify.SMS_METHODS:
            _bad(f"method must be one of {', '.join(notify.SMS_METHODS)}")
        if s.get("url") and not str(s["url"]).startswith(("http://", "https://")):
            _bad("the SMS gateway URL must start with http:// or https://")
        if s.get("method", (current.get("sms") or {}).get("method", "POST_JSON")) == "POST_JSON" and s.get("body_template"):
            try:
                json.loads(notify._fill(s["body_template"], {"to": "1", "to_digits": "1", "text": "x", "sender": "x",
                                                             "secret": "x"}))
            except ValueError:
                _bad("the JSON body template is not valid JSON")
        allowed = set(notify.DEFAULTS["sms"]) - {"token_ref"}
        current["sms"] = {**(current.get("sms") or {}), **{k: v for k, v in s.items() if k in allowed}}
    if ev:
        current["events"] = {**(current.get("events") or {}),
                             **{k: bool(v) for k, v in ev.items() if k in notify.DEFAULTS["events"]}}
    t.settings = {**(t.settings or {}), "notifications": current}
    db.commit()
    after = notify.public_config(t)
    audit.record(db, p.tenant_id, "notifications.update", actor=p, resource_type="tenant", resource_id=t.id,
                 before=before, after=after)
    return {"config": after}


@router.post("/notifications/secret")
def set_notification_secret(body: dict, p: Principal = Depends(require_user("notifications.manage")),
                            db: Session = Depends(get_db)):
    channel, value = body.get("channel"), str(body.get("value") or "")
    if channel not in ("email", "sms", "push") or not value:
        _bad("send {channel: email|sms|push, value}")
    t = db.get(Tenant, p.tenant_id)
    current = copy.deepcopy((t.settings or {}).get("notifications") or {})
    section = current.setdefault(channel, {})
    key = {"email": "password_ref", "sms": "token_ref", "push": "secret_ref"}[channel]
    section[key] = store_secret(db, p.tenant_id, f"{channel} gateway credential", value, p.user_id, section.get(key))
    t.settings = {**(t.settings or {}), "notifications": current}
    db.commit()
    audit.record(db, p.tenant_id, "notifications.secret_set", actor=p, resource_type="tenant", resource_id=t.id,
                 after={"channel": channel})
    return {"config": notify.public_config(t)}


@router.post("/notifications/test")
def test_notification(body: dict, p: Principal = Depends(require_user("notifications.manage")),
                      db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    to = str(body.get("to") or "").strip()
    if body.get("channel") == "sms":
        ok, detail = notify.send_sms(db, t, to, f"VeriShield test message from {t.display_name}.", "test")
    elif body.get("channel") == "push":
        ok, detail = notify.send_push(db, t, {"type": "TEST", "customer_id": to or "TEST",
                                              "message": f"VeriShield test notification from {t.display_name}."}, "test")
    else:
        ok, detail = notify.send_email(db, t, to, "VeriShield test e-mail",
                                       f"This is a test message from VeriShield at {t.display_name}. "
                                       "E-mail notifications are working.", "test")
    audit.record(db, p.tenant_id, "notifications.test", actor=p, resource_type="tenant", resource_id=t.id,
                 outcome="SUCCESS" if ok else "FAILURE", reason=detail)
    return {"ok": ok, "detail": detail}


# =========================================================================== fraud engine database
_TABLES = (("customers", Customer), ("transactions", Transaction), ("decisions", Decision), ("alerts", Alert),
           ("cases", Case), ("audit events", AuditEvent))


def _mask_url(url: str) -> str:
    return re.sub(r"(://[^:/@]+:)[^@]+@", r"\1********@", url)


def _env_path():
    return ROOT / ".env"


@router.get("/system/database")
def database_info(p: Principal = Depends(require("tenant.manage", "connectors.manage")),
                  db: Session = Depends(get_db)):
    eng = get_engine()
    counts = {}
    for name, model in _TABLES:
        q = select(func.count(model.id))
        if hasattr(model, "tenant_id"):
            q = q.where(model.tenant_id == p.tenant_id)
        counts[name] = db.execute(q).scalar()
    size = None
    if eng.dialect.name == "sqlite":
        path = settings.database_url.split("///", 1)[-1]
        try:
            import os
            size = os.path.getsize(path)
        except OSError:
            pass
    results = db.execute(select(Connector).where(Connector.tenant_id == p.tenant_id, Connector.type == "db_results",
                                                 Connector.status != "RETIRED")).scalars().all()
    pending = None
    env = _env_path()
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("DATABASE_URL=") and line[13:].strip() and \
                    line[13:].strip().replace("postgresql://", "postgresql+psycopg://") != settings.database_url:
                pending = _mask_url(line[13:].strip())
    return {"primary": {"engine": eng.dialect.name, "url": _mask_url(settings.database_url), "size_bytes": size,
                        "pool_size": getattr(eng.pool, "size", lambda: None)() if hasattr(eng.pool, "size") else None,
                        "row_counts": counts, "pending_change": pending,
                        "drivers": ["sqlite", "postgresql (psycopg)"]},
            "results_databases": [connector_public(c) for c in results]}


@router.post("/system/database/test")
def database_test(body: dict, p: Principal = Depends(require_user("tenant.manage"))):
    url = str(body.get("url") or "").strip()
    if not url:
        _bad("url is required")
    from sqlalchemy import create_engine, text

    from ..services.results_db import normalise_url
    try:
        eng = create_engine(normalise_url(url))
        try:
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
            return {"ok": True, "detail": f"connected to {eng.dialect.name}"}
        finally:
            eng.dispose()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": sanitize_error(f"{type(exc).__name__}: {exc}")}


@router.put("/system/database")
def database_set(body: dict, p: Principal = Depends(require_user("tenant.manage")), db: Session = Depends(get_db)):
    """Point the fraud engine at another primary database (applied at the next restart)."""
    url = str(body.get("url") or "").strip()
    if body.get("confirm") != "CHANGE":
        _bad('send {"confirm": "CHANGE"} - the new database is used after the service restarts')
    test = database_test({"url": url}, p)
    if not test["ok"]:
        _bad(f"connection test failed: {test['detail']}", "CONNECTION_FAILED")
    env = _env_path()
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [ln for ln in lines if not ln.startswith(("DATABASE_URL=", "VERISHIELD_DATABASE_URL="))]
    lines.append(f"DATABASE_URL={url}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    audit.record(db, p.tenant_id, "system.database_change", actor=p, resource_type="database",
                 before={"url": _mask_url(settings.database_url)}, after={"url": _mask_url(url)})
    return {"ok": True, "message": "Saved. Restart VeriShield to use the new database (Manage-VeriShield.ps1 "
                                   "Restart on Windows). The schema is created automatically; existing records are "
                                   "not copied."}


# =========================================================================== AI agents (MCP)
def _tool_catalogue() -> list[dict]:
    from ..mcp_server import TOOLS
    return [{"name": t["name"], "description": t["description"], "method": t["call"][0]} for t in TOOLS]


DEFAULT_MCP_TOOLS = [t for t in ("list_connectors", "get_connector_health", "get_ingestion_failures", "get_system_health",
                                 "get_model_status", "get_model_health", "get_aml_queue_summary", "get_case_summary",
                                 "search_transactions", "preview_policy", "get_dashboard", "get_clusters")]


def mcp_config(t: Tenant) -> dict:
    c = (t.settings or {}).get("mcp") or {}
    return {"enabled": bool(c.get("enabled", False)), "http_enabled": bool(c.get("http_enabled", True)),
            "tools": list(c.get("tools") or DEFAULT_MCP_TOOLS), "max_calls_per_minute": int(c.get("max_calls_per_minute", 60)),
            "notes": c.get("notes", "")}


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.get("/mcp/settings")
def mcp_settings(request: Request, p: Principal = Depends(require("mcp.manage")), db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    keys = [row_dict(c, exclude=("key_hash", "previous_key_hash")) for c in db.execute(select(ApiClient).where(
        ApiClient.tenant_id == p.tenant_id)).scalars() if "mcp:use" in (c.scopes or [])]
    calls = db.execute(select(AuditEvent).where(AuditEvent.tenant_id == p.tenant_id,
                                                AuditEvent.action.like("mcp.%")).order_by(AuditEvent.id.desc())
                       .limit(30)).scalars().all()
    base = _base_url(request)
    return {"config": mcp_config(t), "tools": _tool_catalogue(), "keys": keys,
            "endpoint": f"{base}/mcp",
            "recent_calls": [row_dict(e, exclude=("before", "prev_hash", "hash")) for e in calls],
            "snippets": {
                "http": {"mcpServers": {"verishield": {"type": "http", "url": f"{base}/mcp",
                                                       "headers": {"X-API-Key": "<MCP access key>"}}}},
                "stdio": {"mcpServers": {"verishield": {
                    "command": "python", "args": ["-m", "verishield.mcp_server", "--url", base],
                    "env": {"VERISHIELD_API_KEY": "<MCP access key>"}}}},
                "claude_code": f"claude mcp add --transport http verishield {base}/mcp --header \"X-API-Key: <MCP access key>\""}}


@router.put("/mcp/settings")
def mcp_update(body: dict, p: Principal = Depends(require_user("mcp.manage")), db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    before = mcp_config(t)
    cfg = dict(before)
    names = {x["name"] for x in _tool_catalogue()}
    if "tools" in body:
        bad = [x for x in body["tools"] or [] if x not in names]
        if bad:
            _bad(f"unknown tools: {', '.join(bad)}")
        cfg["tools"] = list(body["tools"] or [])
    for k in ("enabled", "http_enabled"):
        if k in body:
            cfg[k] = bool(body[k])
    if "max_calls_per_minute" in body:
        cfg["max_calls_per_minute"] = max(1, min(6000, int(body["max_calls_per_minute"])))
    if "notes" in body:
        cfg["notes"] = str(body["notes"])[:500]
    t.settings = {**(t.settings or {}), "mcp": cfg}
    db.commit()
    audit.record(db, p.tenant_id, "mcp.settings_update", actor=p, resource_type="tenant", resource_id=t.id,
                 before=before, after=cfg)
    return {"config": cfg}


MCP_SCOPES = ["mcp:use", "read:operations", "read:aml", "read:executive", "policies:simulate", "reports:generate"]


@router.post("/mcp/keys", status_code=201)
def mcp_key(body: dict, p: Principal = Depends(require_user("mcp.manage")), db: Session = Depends(get_db)):
    name = str(body.get("name") or "AI agent").strip()[:100]
    space = body.get("space") or p.space
    if space not in ("LIVE", "DEMO"):
        _bad("space must be LIVE or DEMO")
    scopes = [s for s in body.get("scopes") or MCP_SCOPES if s in MCP_SCOPES]
    if "mcp:use" not in scopes:
        scopes.insert(0, "mcp:use")
    key, prefix, hashed = new_api_key()
    client = ApiClient(tenant_id=p.tenant_id, space=space, name=f"MCP: {name}", key_prefix=prefix, key_hash=hashed,
                       scopes=scopes, rate_limit_per_minute=int(body.get("rate_limit_per_minute") or 120),
                       created_by=p.user_id)
    db.add(client)
    db.commit()
    audit.record(db, p.tenant_id, "mcp.key_create", actor=p, resource_type="api_client", resource_id=client.id,
                 after={"name": client.name, "scopes": scopes, "space": space})
    return {"client": row_dict(client, exclude=("key_hash", "previous_key_hash")), "api_key": key,
            "note": f"Copy the key now - it is shown only once. Agents using it see the {space} workspace."}


@router.get("/mcp/policy")
def mcp_policy(p: Principal = Depends(require("mcp.use", "mcp.manage")), db: Session = Depends(get_db)):
    cfg = mcp_config(db.get(Tenant, p.tenant_id))
    return {"enabled": cfg["enabled"], "tools": cfg["tools"] if cfg["enabled"] else []}


# ----------------------------------------------------------------- built-in streamable-HTTP MCP endpoint
async def _subrequest(request: Request, method: str, path: str, args: dict, key: str) -> tuple[int, object]:
    """Run a tool as an internal request through the governed REST API (same RBAC, scopes and audit)."""
    import urllib.parse
    path = path.format(**{k: urllib.parse.quote(str(v), safe="") for k, v in args.items()})
    query = ""
    body = b""
    if "?" in path:
        path, query = path.split("?", 1)
    if method == "GET":
        extra = {k: v for k, v in args.items() if v not in (None, "") and "{" + k + "}" not in path}
        if extra:
            query = (query + "&" if query else "") + urllib.parse.urlencode(extra)
    else:
        body = json.dumps({k: v for k, v in args.items() if k != "policy_id"}).encode()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": query.encode(), "root_path": "",
             "headers": [(b"x-api-key", key.encode()), (b"content-type", b"application/json"),
                         (b"content-length", str(len(body)).encode())],
             "client": request.client and (request.client.host, request.client.port) or ("127.0.0.1", 0),
             "server": ("verishield", 80)}
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    status, chunks = 500, []

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await request.app(scope, receive, send)
    raw = b"".join(chunks).decode("utf-8", "replace")
    try:
        return status, json.loads(raw or "null")
    except ValueError:
        return status, raw[:2000]


def _api_key(request: Request) -> str:
    key = request.headers.get("x-api-key", "")
    auth = request.headers.get("authorization", "")
    if not key and auth.lower().startswith(("bearer vs_", "apikey ")):
        key = auth.split(" ", 1)[1].strip()
    return key.strip()


@mcp_router.post("/mcp")
async def mcp_http(request: Request):
    from ..db import SessionLocal
    from ..deps import _from_api_key, rate_limiter
    from ..mcp_server import PROTOCOL, TOOLS

    def rpc_error(mid, code, message, status=200):
        return JSONResponse({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}, status)

    key = _api_key(request)
    if not key:
        return rpc_error(None, -32001, "send the MCP access key in the X-API-Key header", 401)
    with SessionLocal() as db:
        try:
            principal = _from_api_key(db, key, request)
        except HTTPException as exc:
            return rpc_error(None, -32001, (exc.detail or {}).get("message", "unauthorised"), exc.status_code)
        tenant = db.get(Tenant, principal.tenant_id)
        cfg = mcp_config(tenant)
        if not principal.can("mcp.use"):
            return rpc_error(None, -32001, "this API key is not an MCP access key (scope mcp:use)", 403)
        if not cfg["enabled"] or not cfg["http_enabled"]:
            return rpc_error(None, -32002, "AI-agent (MCP) access is switched off by the IT Admin", 403)
    try:
        msg = await request.json()
    except ValueError:
        return rpc_error(None, -32700, "parse error", 400)
    batch = msg if isinstance(msg, list) else [msg]
    replies = []
    for m in batch:
        mid, method, params = m.get("id"), m.get("method"), m.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": f"verishield-{tenant.id.lower()}", "version": "1.1.0"},
                      "instructions": f"VeriShield fraud & AML platform of {tenant.display_name}. Read-only tools; "
                                      f"workspace {principal.space}."}
        elif method == "tools/list":
            result = {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS
                                if t["name"] in cfg["tools"]]}
        elif method == "tools/call":
            tool = next((t for t in TOOLS if t["name"] == params.get("name")), None)
            if tool is None or tool["name"] not in cfg["tools"]:
                replies.append({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602,
                                                                        "message": "unknown or disabled tool"}})
                continue
            if not rate_limiter.allow(f"mcp:{principal.api_client_id}", cfg["max_calls_per_minute"]):
                replies.append({"jsonrpc": "2.0", "id": mid, "error": {"code": -32003, "message": "rate limited"}})
                continue
            status, data = await _subrequest(request, *tool["call"], params.get("arguments") or {}, key)
            with SessionLocal() as db:
                audit.record(db, principal.tenant_id, "mcp.tool_call", actor=principal, resource_type="mcp_tool",
                             resource_id=tool["name"], outcome="SUCCESS" if status < 400 else "FAILURE",
                             reason=f"HTTP {status}", space=principal.space)
            result = {"content": [{"type": "text", "text": json.dumps(data, indent=1, default=str)[:60000]}],
                      "isError": status >= 400}
        elif method == "ping":
            result = {}
        elif mid is None:
            continue
        else:
            replies.append({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601,
                                                                    "message": f"method {method} not supported"}})
            continue
        replies.append({"jsonrpc": "2.0", "id": mid, "result": result})
    if not replies:
        return JSONResponse(None, 202)
    return JSONResponse(replies if isinstance(msg, list) else replies[0])


@mcp_router.get("/mcp")
def mcp_get():
    return JSONResponse({"detail": "VeriShield MCP endpoint - POST JSON-RPC 2.0 messages with an X-API-Key header"},
                        405, headers={"Allow": "POST"})


__all__ = ["router", "mcp_router", "iso", "Optional"]
