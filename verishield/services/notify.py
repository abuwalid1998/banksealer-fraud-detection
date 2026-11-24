"""E-mail (the bank's SMTP relay) and SMS (the bank's / operator's HTTP SMS gateway) notifications.

Both channels are switched ON/OFF by the IT Admin under *E-mail & SMS*. Server addresses, ports, sender and
templates live in the tenant settings; passwords and gateway tokens live in the encrypted vault and are never
returned by the API. Every attempt is written to the notification outbox without the message body when the
body contains a code or password.

Used for: forgotten-password codes, passwords reset by the IT Admin, new-account credentials, case assignment
and critical alerts.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import smtplib
import ssl
import threading
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional

from sqlalchemy.orm import Session

from ..models import NotificationLog, Tenant
from ..security import sanitize_error

log = logging.getLogger("verishield.notify")

DEFAULTS = {
    "email": {"enabled": False, "host": "", "port": 587, "security": "STARTTLS", "username": "",
              "from_address": "", "from_name": "VeriShield", "password_ref": None, "timeout": 15,
              "verify_tls": True},
    "sms": {"enabled": False, "method": "POST_JSON", "url": "", "sender_id": "", "auth_header": "Authorization",
            "auth_prefix": "Bearer", "token_ref": None,
            "body_template": '{"to": "{to}", "from": "{sender}", "text": "{text}"}',
            "success_contains": "", "timeout": 15, "verify_tls": True, "country_code": "+970"},
    "events": {"password_reset": True, "account_created": True, "case_assigned": True, "critical_alert": True},
}
SECURITY = ("STARTTLS", "SSL", "NONE")
SMS_METHODS = ("POST_JSON", "POST_FORM", "GET")


def config(tenant: Tenant) -> dict:
    cur = (tenant.settings or {}).get("notifications") or {}
    out = {}
    for section, defaults in DEFAULTS.items():
        out[section] = {**defaults, **(cur.get(section) or {})}
    # legacy environment configuration (VERISHIELD_SMTP_*) keeps working
    if not out["email"]["host"] and os.environ.get("VERISHIELD_SMTP_HOST"):
        out["email"].update({"enabled": True, "host": os.environ["VERISHIELD_SMTP_HOST"],
                             "port": int(os.environ.get("VERISHIELD_SMTP_PORT", "587")),
                             "security": "STARTTLS" if os.environ.get("VERISHIELD_SMTP_TLS", "true").lower() != "false"
                             else "NONE", "username": os.environ.get("VERISHIELD_SMTP_USER", ""),
                             "from_address": os.environ.get("VERISHIELD_SMTP_FROM", ""),
                             "password_ref": "env:VERISHIELD_SMTP_PASSWORD" if os.environ.get("VERISHIELD_SMTP_USER")
                             else None})
    return out


def public_config(tenant: Tenant) -> dict:
    cfg = config(tenant)
    for section, key in (("email", "password_ref"), ("sms", "token_ref")):
        cfg[section]["secret_configured"] = bool(cfg[section].pop(key, None))
    cfg["email"]["ready"] = email_ready(tenant)
    cfg["sms"]["ready"] = sms_ready(tenant)
    return cfg


def email_ready(tenant: Tenant) -> bool:
    e = config(tenant)["email"]
    return bool(e["enabled"] and e["host"] and e["from_address"])


def sms_ready(tenant: Tenant) -> bool:
    s = config(tenant)["sms"]
    return bool(s["enabled"] and s["url"])


def _record(db: Session, tenant_id: str, channel: str, recipient: str, subject: str, preview: str, status: str,
            error: str = "", event: str = "") -> None:
    db.add(NotificationLog(tenant_id=tenant_id, channel=channel, recipient=recipient[:200], subject=subject[:200],
                           preview=preview[:500], status=status, error=sanitize_error(error)[:1000], event=event))
    db.commit()


def _secret(db: Session, tenant_id: str, ref: Optional[str]) -> str:
    from .connectors import resolve_secret
    return resolve_secret(db, tenant_id, ref) or ""


# --------------------------------------------------------------------------- e-mail
def _smtp_send(db: Session, tenant: Tenant, to: str, subject: str, text: str, html: Optional[str] = None,
               attachments: Optional[list] = None) -> None:
    e = config(tenant)["email"]
    msg = EmailMessage()
    msg["From"] = formataddr((e.get("from_name") or tenant.display_name, e["from_address"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    for name, data in attachments or []:
        kind = {"pdf": ("application", "pdf"), "csv": ("text", "csv"),
                "xlsx": ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")}.get(
            name.rsplit(".", 1)[-1], ("application", "octet-stream"))
        msg.add_attachment(data, maintype=kind[0], subtype=kind[1], filename=name)
    ctx = ssl.create_default_context()
    if not e.get("verify_tls", True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    port, timeout = int(e.get("port") or 587), float(e.get("timeout") or 15)
    if e.get("security") == "SSL":
        server = smtplib.SMTP_SSL(e["host"], port, timeout=timeout, context=ctx)
    else:
        server = smtplib.SMTP(e["host"], port, timeout=timeout)
    with server as s:
        s.ehlo()
        if e.get("security") == "STARTTLS":
            s.starttls(context=ctx)
            s.ehlo()
        if e.get("username"):
            s.login(e["username"], _secret(db, tenant.id, e.get("password_ref")))
        s.send_message(msg)


def _html(tenant: Tenant, subject: str, text: str) -> str:
    from html import escape
    from .reports import branding
    b = branding(tenant)
    paras = "".join(f"<p style='margin:0 0 12px'>{escape(line)}</p>" for line in text.split("\n") if line.strip())
    return (f"<div style='font-family:Segoe UI,Arial,sans-serif;max-width:560px;margin:auto;border:1px solid #D5DDE6'>"
            f"<div style='background:{escape(b['primary_color'])};color:#fff;padding:16px 20px;font-size:16px;"
            f"font-weight:600'>{escape(b['display_name'])}</div><div style='padding:20px;color:#1B2738;font-size:14px'>"
            f"<h2 style='font-size:17px;margin:0 0 14px'>{escape(subject)}</h2>{paras}</div>"
            f"<div style='padding:12px 20px;background:#F4F7FA;color:#5B6B7F;font-size:11px'>"
            f"{escape(b.get('confidentiality_notice') or '')}</div></div>")


def send_email(db: Session, tenant: Tenant, to: str, subject: str, text: str, event: str = "",
               sensitive: bool = False, attachments: Optional[list] = None) -> tuple[bool, str]:
    preview = "(contains a code or password - not stored)" if sensitive else text
    if not to:
        return False, "no e-mail address"
    if not email_ready(tenant):
        _record(db, tenant.id, "EMAIL", to, subject, preview, "DISABLED", "e-mail channel is off", event)
        return False, "e-mail channel is switched off or not configured"
    try:
        _smtp_send(db, tenant, to, subject, text, _html(tenant, subject, text), attachments)
        _record(db, tenant.id, "EMAIL", to, subject, preview, "SENT", event=event)
        return True, "sent"
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        log.error("SMTP delivery to %s failed: %s", to, err)
        _record(db, tenant.id, "EMAIL", to, subject, preview, "FAILED", err, event)
        return False, sanitize_error(err)


# --------------------------------------------------------------------------- SMS
def normalise_phone(phone: str, country_code: str = "+970") -> str:
    digits = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("0") and country_code:
        digits = country_code + digits[1:]
    return digits


def _fill(template: str, values: dict, encode=None) -> str:
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", encode(v) if encode else v)
    return out


def _sms_send(db: Session, tenant: Tenant, to: str, text: str) -> str:
    s = config(tenant)["sms"]
    token = _secret(db, tenant.id, s.get("token_ref"))
    values = {"to": to, "to_digits": to.lstrip("+"), "text": text, "sender": s.get("sender_id") or "",
              "secret": token}
    method = s.get("method") or "POST_JSON"
    headers = {"User-Agent": "VeriShield-Notify/1.0"}
    if token and s.get("auth_header") and "{secret}" not in (s.get("url") or "") + (s.get("body_template") or ""):
        headers[s["auth_header"]] = f"{s.get('auth_prefix')} {token}".strip() if s.get("auth_prefix") else token
    url = _fill(s["url"], values, lambda v: urllib.parse.quote(v, safe=""))
    data = None
    if method == "POST_JSON":
        body = _fill(s.get("body_template") or DEFAULTS["sms"]["body_template"],
                     values, lambda v: json.dumps(v)[1:-1])
        json.loads(body)          # the template must produce valid JSON
        data, headers["Content-Type"] = body.encode(), "application/json"
    elif method == "POST_FORM":
        body = _fill(s.get("body_template") or "to={to}&from={sender}&text={text}", values,
                     lambda v: urllib.parse.quote_plus(v))
        data, headers["Content-Type"] = body.encode(), "application/x-www-form-urlencoded"
    ctx = ssl.create_default_context()
    if not s.get("verify_tls", True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, data=data, headers=headers, method="GET" if method == "GET" else "POST")
    with urllib.request.urlopen(req, timeout=float(s.get("timeout") or 15), context=ctx) as resp:  # noqa: S310
        payload = resp.read(4000).decode("utf-8", "replace")
        if resp.status >= 300:
            raise RuntimeError(f"gateway answered HTTP {resp.status}")
    if s.get("success_contains") and s["success_contains"] not in payload:
        raise RuntimeError(f"gateway response did not contain '{s['success_contains']}': {payload[:200]}")
    return payload[:200]


def send_sms(db: Session, tenant: Tenant, phone: str, text: str, event: str = "",
             sensitive: bool = False) -> tuple[bool, str]:
    s = config(tenant)["sms"]
    to = normalise_phone(phone or "", s.get("country_code") or "")
    preview = "(contains a code or password - not stored)" if sensitive else text
    if not to:
        return False, "no mobile number"
    if not sms_ready(tenant):
        _record(db, tenant.id, "SMS", to, "", preview, "DISABLED", "SMS channel is off", event)
        return False, "SMS channel is switched off or not configured"
    try:
        _sms_send(db, tenant, to, text[:480])
        _record(db, tenant.id, "SMS", to, "", preview, "SENT", event=event)
        return True, "sent"
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        log.error("SMS delivery to %s failed: %s", to, err)
        _record(db, tenant.id, "SMS", to, "", preview, "FAILED", err, event)
        return False, sanitize_error(err)


# --------------------------------------------------------------------------- events
def event_enabled(tenant: Tenant, event: str) -> bool:
    return bool(config(tenant)["events"].get(event, False))


_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=1000)
_worker: Optional[threading.Thread] = None


def _run_queue() -> None:
    from ..db import SessionLocal
    while True:
        tenant_id, kind, to, subject, text, event = _queue.get()
        try:
            with SessionLocal() as db:
                tenant = db.get(Tenant, tenant_id)
                if kind == "EMAIL":
                    send_email(db, tenant, to, subject, text, event)
                else:
                    send_sms(db, tenant, to, text, event)
        except Exception as exc:  # noqa: BLE001
            log.warning("queued notification failed: %s", exc)


def enqueue(tenant: Tenant, event: str, email: str = "", phone: str = "", subject: str = "", text: str = "") -> None:
    """Fire-and-forget notification for workflow events (never blocks a decision or a request)."""
    global _worker
    if not event_enabled(tenant, event) or not (email_ready(tenant) or sms_ready(tenant)):
        return
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_run_queue, name="verishield-notify", daemon=True)
        _worker.start()
    try:
        if email and email_ready(tenant):
            _queue.put_nowait((tenant.id, "EMAIL", email, subject, text, event))
        if phone and sms_ready(tenant):
            _queue.put_nowait((tenant.id, "SMS", phone, subject, f"{subject}: {text}"[:300], event))
    except queue.Full:
        log.warning("notification queue full - %s dropped", event)


def deliver_secret(db: Session, tenant: Tenant, user, subject: str, text: str, channels: list[str],
                   event: str) -> dict:
    """Send a code / password to a user over the requested channels; returns per-channel results."""
    out = {}
    if "email" in channels:
        out["email"] = send_email(db, tenant, user.email, subject, text, event, sensitive=True)
    if "sms" in channels:
        out["sms"] = send_sms(db, tenant, user.phone, text, event, sensitive=True)
    return {k: {"sent": v[0], "detail": v[1]} for k, v in out.items()}
