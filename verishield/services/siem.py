"""Forward audit and security events to the bank's SIEM.

Every audit event (sign-ins and failures, permission denials, configuration changes, decisions taken by people,
exports, agent calls...) is sent once, in order, to the SIEM:

* **syslog** over UDP, TCP or TLS (RFC 5424 framing) with a **CEF** (ArcSight, QRadar, Sentinel...) or JSON body;
* **HTTPS** collector (Splunk HEC, Elastic, Sentinel data collector...) as JSON lines.

The forwarder runs on the leader node every 30 seconds. It keeps a cursor (the last audit event id sent), so a SIEM
outage only delays events - they are sent when the connection is back, never lost or duplicated.
"""
from __future__ import annotations

import copy
import json
import socket
import ssl
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__
from ..models import AuditEvent, Tenant, utcnow
from ..security import check_url, sanitize_error
from .metrics import metrics

DEFAULTS = {"enabled": False, "protocol": "udp", "host": "", "port": 514, "format": "cef", "facility": 13,
            "url": "", "token_ref": None, "token_header": "Authorization", "token_prefix": "Splunk",
            "verify_tls": True, "include_demo": False, "batch": 200, "app_name": "VeriShield"}
PROTOCOLS = ("udp", "tcp", "tls", "https")
FORMATS = ("cef", "json")


def config(tenant: Tenant) -> dict:
    return {**DEFAULTS, **((tenant.settings or {}).get("siem") or {})}


def state(tenant: Tenant) -> dict:
    return dict((tenant.settings or {}).get("siem_state") or {})


def _save_state(db: Session, tenant: Tenant, **values) -> None:
    st = state(tenant)
    st.update(values)
    tenant.settings = {**(tenant.settings or {}), "siem_state": st}
    db.commit()


def public(db: Session, tenant: Tenant) -> dict:
    c = config(tenant)
    c["token_configured"] = bool(c.pop("token_ref", None))
    st = state(tenant)
    last = db.execute(select(func.max(AuditEvent.id)).where(AuditEvent.tenant_id == tenant.id)).scalar() or 0
    st["lag"] = max(0, last - int(st.get("cursor", 0))) if c["enabled"] else None
    return {"config": c, "state": st, "protocols": PROTOCOLS, "formats": FORMATS}


# --------------------------------------------------------------------------- formatting
def severity(ev: AuditEvent) -> int:
    """CEF severity 0-10."""
    a = ev.action or ""
    if ev.outcome != "SUCCESS" or a.endswith((".denied", ".failed", ".locked")):
        return 7
    if any(k in a for k in ("delete", "secret", "rotate", "override", "disable", "role", "permission", "mfa",
                            "password", "unfreeze", "directory", "siem", "api_client", "retire")):
        return 5
    return 3


def _cef_esc_header(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace("|", "\\|")


def _cef_esc(v) -> str:
    return str(v if v is not None else "").replace("\\", "\\\\").replace("=", "\\=").replace("\n", " ").replace("\r", " ")


def to_cef(ev: AuditEvent, tenant: Tenant) -> str:
    ts = int(ev.created_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
    ext = {"rt": ts, "suser": ev.actor_name, "suid": ev.actor_id or "", "cs1Label": "role", "cs1": ev.actor_role,
           "src": ev.ip or "", "act": ev.action, "outcome": ev.outcome, "cs2Label": "resource",
           "cs2": f"{ev.resource_type}:{ev.resource_id}", "cs3Label": "workspace", "cs3": ev.space,
           "cs4Label": "institution", "cs4": tenant.id, "externalId": ev.id, "msg": (ev.reason or "")[:500],
           "cs5Label": "hash", "cs5": ev.hash}
    body = " ".join(f"{k}={_cef_esc(v)}" for k, v in ext.items() if v not in ("", None))
    return (f"CEF:0|VeriShield|VeriShield|{_cef_esc_header(__version__)}|{_cef_esc_header(ev.action)}|"
            f"{_cef_esc_header(ev.action.replace('.', ' ').replace('_', ' '))}|{severity(ev)}|{body}")


def to_json(ev: AuditEvent, tenant: Tenant) -> dict:
    return {"time": ev.created_at.isoformat(timespec="milliseconds") + "Z", "product": "VeriShield",
            "version": __version__, "institution": tenant.id, "workspace": ev.space, "event_id": ev.id,
            "action": ev.action, "outcome": ev.outcome, "severity": severity(ev), "actor": ev.actor_name,
            "actor_id": ev.actor_id, "actor_role": ev.actor_role, "source_ip": ev.ip, "resource_type": ev.resource_type,
            "resource_id": ev.resource_id, "reason": ev.reason, "correlation_id": ev.correlation_id, "hash": ev.hash}


def _syslog(msg: str, c: dict, sev: int) -> bytes:
    sys_sev = 4 if sev >= 7 else 5 if sev >= 5 else 6
    pri = int(c.get("facility", 13)) * 8 + sys_sev
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return f"<{pri}>1 {now} {socket.gethostname()} {c.get('app_name') or 'VeriShield'} - AUDIT - {msg}\n".encode()


# --------------------------------------------------------------------------- transport
def _send(db: Session, tenant: Tenant, c: dict, events: list[AuditEvent]) -> None:
    proto = c["protocol"]
    if proto == "https":
        lines = []
        for ev in events:
            body = to_json(ev, tenant)
            lines.append(json.dumps({"event": body, "sourcetype": "verishield:audit"} if c.get("token_prefix") == "Splunk"
                                    else body, separators=(",", ":")))
        data = ("\n".join(lines) + "\n").encode()
        headers = {"Content-Type": "application/json"}
        if c.get("token_ref"):
            from .connectors import resolve_secret
            tok = resolve_secret(db, tenant.id, c["token_ref"]) or ""
            headers[c.get("token_header") or "Authorization"] = f"{c.get('token_prefix')} {tok}".strip()
        ctx = ssl.create_default_context()
        if not c.get("verify_tls", True):
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        req = urllib.request.Request(check_url(c["url"]), data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:  # nosec B310 - admin-configured, http(s) only
            if resp.status >= 300:
                raise RuntimeError(f"HTTP {resp.status}")
        return
    if not c.get("host"):
        raise RuntimeError("SIEM host is not configured")
    payloads = [_syslog(to_cef(ev, tenant) if c["format"] == "cef" else json.dumps(to_json(ev, tenant)), c, severity(ev))
                for ev in events]
    addr = (c["host"], int(c["port"]))
    if proto == "udp":
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for p in payloads:
                s.sendto(p[:8192], addr)
        return
    raw = socket.create_connection(addr, timeout=10)
    try:
        conn = raw
        if proto == "tls":
            ctx = ssl.create_default_context()
            if not c.get("verify_tls", True):
                ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            conn = ctx.wrap_socket(raw, server_hostname=c["host"])
        conn.sendall(b"".join(payloads))
    finally:
        raw.close()


def forward(db: Session) -> int:
    """Leader job: send new audit events of every institution that has SIEM forwarding on."""
    total = 0
    for tenant in db.execute(select(Tenant)).scalars().all():
        c = config(tenant)
        if not c["enabled"]:
            continue
        st = state(tenant)
        cursor = int(st.get("cursor", 0))
        if not st.get("initialised"):
            # start from the current end of the audit trail when forwarding is switched on
            cursor = db.execute(select(func.max(AuditEvent.id)).where(AuditEvent.tenant_id == tenant.id)).scalar() or 0
            _save_state(db, tenant, cursor=cursor, initialised=True, sent=0)
        q = select(AuditEvent).where(AuditEvent.tenant_id == tenant.id, AuditEvent.id > cursor)
        if not c.get("include_demo"):
            q = q.where(AuditEvent.space != "DEMO")
        events = db.execute(q.order_by(AuditEvent.id).limit(int(c.get("batch", 200)))).scalars().all()
        if not events:
            # skipped demo events still move the cursor
            top = db.execute(select(func.max(AuditEvent.id)).where(AuditEvent.tenant_id == tenant.id)).scalar() or cursor
            if top > cursor:
                _save_state(db, tenant, cursor=top)
            continue
        try:
            _send(db, tenant, c, events)
        except Exception as exc:  # noqa: BLE001 - retried on the next run
            metrics.inc("siem_errors")
            _save_state(db, tenant, last_error=sanitize_error(f"{type(exc).__name__}: {exc}"),
                        last_error_at=utcnow().isoformat(timespec="seconds") + "Z")
            continue
        metrics.inc("siem_events_sent", len(events))
        _save_state(db, tenant, cursor=events[-1].id, sent=int(st.get("sent", 0)) + len(events), last_error="",
                    last_sent_at=utcnow().isoformat(timespec="seconds") + "Z")
        total += len(events)
    return total


def test(db: Session, tenant: Tenant, c: Optional[dict] = None) -> tuple[bool, str]:
    c = c or config(tenant)
    ev = AuditEvent(id=0, tenant_id=tenant.id, space="LIVE", actor_name="VeriShield", actor_role="SYSTEM",
                    action="siem.test", resource_type="siem", resource_id="test", outcome="SUCCESS",
                    reason="Test event from VeriShield", ip="", created_at=utcnow(), hash="test", prev_hash="")
    try:
        _send(db, tenant, c, [ev])
        return True, f"test event sent ({c['protocol'].upper()} {c.get('format', '').upper()})"
    except Exception as exc:  # noqa: BLE001
        return False, sanitize_error(f"{type(exc).__name__}: {exc}")


def update(db: Session, tenant: Tenant, body: dict) -> dict:
    cur = copy.deepcopy((tenant.settings or {}).get("siem") or {})
    body = {k: v for k, v in (body or {}).items() if k in DEFAULTS and k != "token_ref"}
    if body.get("protocol") and body["protocol"] not in PROTOCOLS:
        raise ValueError(f"protocol must be one of {', '.join(PROTOCOLS)}")
    if body.get("format") and body["format"] not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    if body.get("port") not in (None, ""):
        body["port"] = int(body["port"])
        if not 0 < body["port"] < 65536:
            raise ValueError("invalid port")
    if body.get("url"):
        check_url(body["url"])
    was = bool(cur.get("enabled"))
    cur.update(body)
    tenant.settings = {**(tenant.settings or {}), "siem": cur}
    if cur.get("enabled") and not was:
        st = state(tenant)
        st["initialised"] = False
        tenant.settings = {**tenant.settings, "siem_state": st}
    db.commit()
    return public(db, tenant)


__all__ = ["forward", "test", "update", "public", "config", "to_cef", "to_json", "severity"]
