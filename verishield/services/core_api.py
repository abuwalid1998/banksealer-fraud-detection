"""Core banking API registry: every operation VeriShield performs on the bank's core systems.

The IT Admin configures, without code:

* **Connections** - base URL, authentication (API key, bearer token, basic, OAuth2 client credentials, HMAC
  signature), mutual TLS, certificate checks, timeout, retries and default headers. Secrets live in the vault.
* **Operations (APIs)** - one per purpose: HTTP method, path / query / header / body templates, the inputs they
  need, how to read the response (output mapping), what counts as success, idempotency header, whether a manual
  request needs a second person's approval, which roles may run it, and whether it is used in Live and/or Demo.
* **Purposes** - what an operation is *for* (freeze a card, reject a transfer, reverse a transaction, block an
  account...). A built-in catalogue is provided and can be edited; the bank can add its own purposes.

VeriShield calls ``execute(purpose, inputs)``: the highest-priority enabled operation for that purpose and workspace
is rendered, sent, parsed and logged (``core_api_calls``, with secrets and card / account numbers masked).

Templates use ``{{name}}`` placeholders with optional filters: ``{{amount|cents}}``, ``{{reason|upper}}``,
``{{note|default:none}}``, ``{{ts|date}}``, ``{{amount|int}}``, ``{{card_ref|last4}}``. In a JSON body a string that
is exactly one placeholder (``"{{amount}}"``) keeps the value's type (number, boolean...).

``mock://core`` as base URL answers in-process like a core system would - for demos and for testing templates.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import re
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET  # nosec B405 - responses are parsed only after DTDs are rejected
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..models import CoreApi, CoreApiCall, CoreConnection, Customer, Tenant, Transaction, utcnow
from ..security import check_url, mask, sanitize_error

AUTH_TYPES = ("NONE", "API_KEY", "BEARER", "BASIC", "OAUTH2_CC", "HMAC")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
BODY_TYPES = ("JSON", "FORM", "XML", "NONE")
SENSITIVE_KEYS = re.compile(r"(card|pan|account|iban|token|secret|password|pin|cvv|authorization|api[-_]?key|signature)",
                            re.I)
STANDARD_OUTPUTS = ("reference", "status", "message")

# --------------------------------------------------------------------------- purposes
_IN = lambda name, label, required=True, example="": {"name": name, "label": label, "required": required,  # noqa: E731
                                                       "example": example}
BUILTIN_PURPOSES: dict[str, dict] = {
    "FREEZE_CARD": {"label": "Freeze a card", "category": "CARD", "automatic": True, "approval": False,
                    "description": "Temporarily block a payment card (customer denied a payment, fraud suspicion).",
                    "inputs": [_IN("card_ref", "Card reference / token", example="CARD-00010-7593"),
                               _IN("customer_id", "Customer", example="C00010"), _IN("reason", "Reason", False, example="Fraud suspicion - customer denied the payment")]},
    "UNFREEZE_CARD": {"label": "Unfreeze a card", "category": "CARD", "automatic": True, "approval": False,
                      "description": "Lift a temporary card block.",
                      "inputs": [_IN("card_ref", "Card reference / token", example="CARD-00010-7593"),
                                 _IN("customer_id", "Customer", example="C00010"), _IN("reason", "Reason", False, example="Fraud suspicion - customer denied the payment")]},
    "BLOCK_CARD_PERMANENT": {"label": "Block a card permanently (lost / stolen)", "category": "CARD", "automatic": False,
                             "approval": True, "description": "Cancel the card; a replacement must be issued.",
                             "inputs": [_IN("card_ref", "Card reference / token", example="CARD-00010-7593"), _IN("customer_id", "Customer", example="C00011"),
                                        _IN("reason", "Reason", example="Fraud suspicion - customer denied the payment")]},
    "RELEASE_PAYMENT": {"label": "Release a held payment", "category": "PAYMENT", "automatic": True, "approval": False,
                        "description": "Let the core execute a payment that VeriShield held before authorisation.",
                        "inputs": [_IN("transaction_id", "Transaction", example="MB-2026-118204"),
                                   _IN("decision_id", "VeriShield decision", False, example="BOP-DEC-0001234"), _IN("reason", "Rationale", False, example="Fraud suspicion - customer denied the payment")]},
    "REJECT_PAYMENT": {"label": "Block / reject a transfer", "category": "PAYMENT", "automatic": True, "approval": False,
                       "description": "Tell the core not to execute a held payment.",
                       "inputs": [_IN("transaction_id", "Transaction", example="MB-2026-118204"),
                                  _IN("decision_id", "VeriShield decision", False, example="BOP-DEC-0001234"), _IN("reason", "Rationale", False, example="Fraud suspicion - customer denied the payment")]},
    "REVERSE_TRANSACTION": {"label": "Reverse / recall an executed transaction", "category": "PAYMENT",
                            "automatic": False, "approval": True,
                            "description": "Roll back an executed transaction (internal) or start a recall (outgoing).",
                            "inputs": [_IN("transaction_id", "Transaction", example="MB-2026-118204"), _IN("amount", "Amount", example="250.00"),
                                       _IN("currency", "Currency", example="ILS"), _IN("customer_id", "Customer", example="C00011"),
                                       _IN("reason", "Reason", example="Fraud suspicion - customer denied the payment")]},
    "BLOCK_ACCOUNT": {"label": "Block an account (debit freeze)", "category": "ACCOUNT", "automatic": False,
                      "approval": True, "description": "Stop all debits from the customer's account.",
                      "inputs": [_IN("customer_id", "Customer", example="C00011"), _IN("account", "Account (IBAN)", False, example="PS92PALS000000000400123456789"),
                                 _IN("reason", "Reason", example="Fraud suspicion - customer denied the payment")]},
    "UNBLOCK_ACCOUNT": {"label": "Unblock an account", "category": "ACCOUNT", "automatic": False, "approval": True,
                        "description": "Lift a debit freeze.",
                        "inputs": [_IN("customer_id", "Customer", example="C00011"), _IN("account", "Account (IBAN)", False, example="PS92PALS000000000400123456789"),
                                   _IN("reason", "Reason", example="Fraud suspicion - customer denied the payment")]},
    "PLACE_HOLD": {"label": "Place an amount hold (lien)", "category": "ACCOUNT", "automatic": False, "approval": True,
                   "description": "Earmark an amount on the account until the investigation ends.",
                   "inputs": [_IN("customer_id", "Customer", example="C00011"), _IN("account", "Account (IBAN)", False, example="PS92PALS000000000400123456789"),
                              _IN("amount", "Amount", example="250.00"), _IN("currency", "Currency", example="ILS"), _IN("reason", "Reason", example="Fraud suspicion - customer denied the payment")]},
    "BLOCK_DIGITAL_BANKING": {"label": "Suspend mobile / internet banking", "category": "DIGITAL", "automatic": False,
                              "approval": False, "description": "Suspend the customer's digital-banking access.",
                              "inputs": [_IN("customer_id", "Customer", example="C00011"), _IN("reason", "Reason", example="Fraud suspicion - customer denied the payment")]},
    "GET_TRANSACTION_STATUS": {"label": "Read the status of a transaction", "category": "INQUIRY", "automatic": True,
                               "approval": False, "description": "Ask the core whether a payment was executed.",
                               "inputs": [_IN("transaction_id", "Transaction", example="MB-2026-118204")]},
    "GET_BALANCE": {"label": "Read an account balance", "category": "INQUIRY", "automatic": False, "approval": False,
                    "description": "Available balance of the customer's account (investigation support).",
                    "inputs": [_IN("customer_id", "Customer", example="C00011"), _IN("account", "Account (IBAN)", False, example="PS92PALS000000000400123456789")]},
}
CATEGORIES = ("CARD", "PAYMENT", "ACCOUNT", "DIGITAL", "INQUIRY", "CUSTOMER", "OTHER")
DEFAULT_ROLES = ["AML_MANAGER", "CHIEF_AML"]


def purposes(tenant: Tenant) -> dict[str, dict]:
    """Built-in purposes (with the bank's edits) plus the bank's own purposes."""
    custom = (tenant.settings or {}).get("core_purposes") or {}
    out = {}
    for code, p in BUILTIN_PURPOSES.items():
        out[code] = {**copy.deepcopy(p), **(custom.get(code) or {}), "code": code, "builtin": True}
    for code, p in custom.items():
        if code not in BUILTIN_PURPOSES:
            out[code] = {"category": "OTHER", "automatic": False, "approval": True, "inputs": [], "description": "",
                         **p, "code": code, "builtin": False}
    return out


def save_purpose(db: Session, tenant: Tenant, code: str, data: dict) -> dict:
    code = re.sub(r"[^A-Z0-9_]", "", (code or "").upper())[:40]
    if len(code) < 3:
        raise ValueError("purpose code: 3-40 characters A-Z, 0-9 and _")
    label = str(data.get("label") or "").strip()
    if not label:
        raise ValueError("label is required")
    if data.get("category") and data["category"] not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
    inputs = []
    for i in data.get("inputs") or []:
        name = re.sub(r"[^a-z0-9_]", "", str(i.get("name") or "").lower())
        if name:
            inputs.append({"name": name, "label": str(i.get("label") or name)[:80], "required": bool(i.get("required", True)),
                           "example": str(i.get("example") or "")[:80]})
    rec = {"label": label[:120], "description": str(data.get("description") or "")[:400],
           "category": data.get("category") or "OTHER", "automatic": bool(data.get("automatic", False)),
           "approval": bool(data.get("approval", False)), "inputs": inputs}
    if code in BUILTIN_PURPOSES and not rec["inputs"]:
        rec.pop("inputs")
    cur = dict((tenant.settings or {}).get("core_purposes") or {})
    cur[code] = rec
    tenant.settings = {**(tenant.settings or {}), "core_purposes": cur}
    db.commit()
    return purposes(tenant)[code]


def delete_purpose(db: Session, tenant: Tenant, code: str) -> None:
    cur = dict((tenant.settings or {}).get("core_purposes") or {})
    if code in BUILTIN_PURPOSES:
        cur.pop(code, None)                 # back to the built-in definition
    else:
        used = db.execute(select(CoreApi.id).where(CoreApi.tenant_id == tenant.id, CoreApi.purpose == code)).first()
        if used:
            raise ValueError("an operation still uses this purpose")
        cur.pop(code, None)
    tenant.settings = {**(tenant.settings or {}), "core_purposes": cur}
    db.commit()


# --------------------------------------------------------------------------- templates
_PH = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*(?:\|\s*([a-z0-9_]+)(?::([^}]*))?)?\s*\}\}")


def _lookup(ctx: dict, name: str):
    cur: Any = ctx
    for part in name.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _filter(value, f: Optional[str], arg: Optional[str]):
    if not f:
        return value
    if f == "default":
        return value if value not in (None, "") else (arg or "")
    if value is None:
        return None
    if f == "upper":
        return str(value).upper()
    if f == "lower":
        return str(value).lower()
    if f == "cents":
        return int(round(float(value) * 100))
    if f == "int":
        return int(float(value))
    if f == "float":
        return float(value)
    if f == "str":
        return str(value)
    if f == "date":
        return str(value)[:10]
    if f == "last4":
        return str(value)[-4:]
    if f == "digits":
        return re.sub(r"\D", "", str(value))
    raise ValueError(f"unknown filter '{f}'")


def render_str(template: str, ctx: dict, escape=None):
    """Render a template string. A template that is exactly one placeholder returns the typed value."""
    if not isinstance(template, str):
        return template
    m = _PH.fullmatch(template.strip())
    if m:
        return _filter(_lookup(ctx, m.group(1)), m.group(2), m.group(3))

    def rep(mm):
        v = _filter(_lookup(ctx, mm.group(1)), mm.group(2), mm.group(3))
        v = "" if v is None else str(v)
        return escape(v) if escape else v
    return _PH.sub(rep, template)


def render_tree(obj, ctx: dict):
    if isinstance(obj, dict):
        return {k: render_tree(v, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render_tree(v, ctx) for v in obj]
    if isinstance(obj, str):
        return render_str(obj, ctx)
    return obj


def placeholders(api: CoreApi) -> set[str]:
    text = " ".join([api.path or "", json.dumps(api.query or {}), json.dumps(api.headers or {}), api.body_template or ""])
    return {m.group(1).split(".")[0] for m in _PH.finditer(text)}


def validate_api(data: dict) -> list[str]:
    errs = []
    if data.get("method") and data["method"] not in METHODS:
        errs.append(f"method must be one of {', '.join(METHODS)}")
    if data.get("body_type") and data["body_type"] not in BODY_TYPES:
        errs.append(f"body type must be one of {', '.join(BODY_TYPES)}")
    if data.get("body_type", "JSON") in ("JSON", "FORM") and (data.get("body_template") or "").strip():
        try:
            json.loads(data["body_template"])
        except ValueError as exc:
            errs.append(f"the body template is not valid JSON: {exc}")
    for code in data.get("success_codes") or []:
        if not 100 <= int(code) <= 599:
            errs.append(f"invalid HTTP status {code}")
    for k, v in (data.get("output_mapping") or {}).items():
        if not re.fullmatch(r"[A-Za-z0-9_]+", str(k)) or not str(v).strip():
            errs.append(f"output mapping '{k}': name and response path are required")
    if (data.get("path") or "").startswith(("http://", "https://")):
        errs.append("the path is relative to the connection's base URL")
    return errs


# --------------------------------------------------------------------------- context
def build_inputs(db: Session, tenant_id: str, space: str, purpose: str, inputs: dict) -> dict:
    """Fill inputs that VeriShield knows from the transaction / customer (the caller's values win)."""
    out = {k: v for k, v in (inputs or {}).items() if v not in (None, "")}
    tx = None
    if out.get("transaction_id"):
        tx = db.execute(select(Transaction).where(Transaction.tenant_id == tenant_id, Transaction.space == space,
                                                  Transaction.tx_ref == str(out["transaction_id"]))).scalar()
    if tx is not None:
        d = tx.details or {}
        for k, v in (("customer_id", tx.customer_ref), ("amount", round(float(tx.amount), 2)),
                     ("currency", tx.currency), ("transaction_type", tx.tx_type), ("channel", tx.channel),
                     ("beneficiary", tx.beneficiary), ("beneficiary_country", tx.beneficiary_country),
                     ("timestamp", tx.ts.isoformat(timespec="seconds") + "Z"), ("decision_id", tx.decision_id),
                     ("card_ref", d.get("card_ref")), ("cheque_number", d.get("cheque_number"))):
            if v not in (None, "") and k not in out:
                out[k] = v
    if out.get("customer_id") and "customer_name" not in out:
        c = db.execute(select(Customer).where(Customer.tenant_id == tenant_id, Customer.space == space,
                                              Customer.customer_ref == str(out["customer_id"]))).scalar()
        if c is not None:
            out["customer_name"] = c.display_name
            if "account" not in out and (c.attributes or {}).get("account"):
                out["account"] = c.attributes["account"]
            if "card_ref" not in out and (c.attributes or {}).get("card_ref") and purpose.endswith("_CARD"):
                out["card_ref"] = c.attributes["card_ref"]
    return out


def _target(inputs: dict) -> str:
    for k in ("transaction_id", "card_ref", "account", "customer_id"):
        if inputs.get(k):
            return str(inputs[k])[:80]
    return ""


# --------------------------------------------------------------------------- transport
_token_cache: dict[str, tuple[str, float]] = {}


def _secret(db: Session, conn: CoreConnection) -> str:
    from .connectors import resolve_secret
    return resolve_secret(db, conn.tenant_id, conn.secret_ref) or ""


def _ssl_ctx(conn: CoreConnection):
    ctx = ssl.create_default_context(cafile=conn.ca_file or None)
    if not conn.verify_tls:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    if conn.client_cert_file:
        ctx.load_cert_chain(conn.client_cert_file, conn.client_key_file or None)
    return ctx


def _oauth_token(db: Session, conn: CoreConnection) -> str:
    a = conn.auth or {}
    key = f"{conn.id}:{conn.updated_at}"
    tok = _token_cache.get(key)
    if tok and tok[1] > time.time() + 30:
        return tok[0]
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": a.get("client_id", ""),
                                   "client_secret": _secret(db, conn),
                                   **({"scope": a["scope"]} if a.get("scope") else {})}).encode()
    req = urllib.request.Request(check_url(a.get("token_url", "")), data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=float(conn.timeout_seconds or 10), context=_ssl_ctx(conn)) as resp:  # nosec B310
        body = json.loads(resp.read(200_000) or b"{}")
    token = body.get("access_token")
    if not token:
        raise RuntimeError("the token endpoint returned no access_token")
    _token_cache[key] = (token, time.time() + float(body.get("expires_in", 300)))
    return token


def _auth_headers(db: Session, conn: CoreConnection, body: bytes) -> dict:
    a, t = conn.auth or {}, conn.auth_type
    if t == "API_KEY":
        v = _secret(db, conn)
        return {a.get("header") or "X-API-Key": f"{a['prefix']} {v}" if a.get("prefix") else v}
    if t == "BEARER":
        return {"Authorization": f"Bearer {_secret(db, conn)}"}
    if t == "BASIC":
        raw = f"{a.get('username', '')}:{_secret(db, conn)}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    if t == "OAUTH2_CC":
        return {"Authorization": f"Bearer {_oauth_token(db, conn)}"}
    if t == "HMAC":
        ts = str(int(time.time()))
        sig = hmac.new(_secret(db, conn).encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        return {a.get("timestamp_header") or "X-Timestamp": ts,
                a.get("header") or "X-Signature": f"{a.get('prefix', 'sha256=')}{sig}"}
    return {}


def _mock_core(method: str, url: str, body: bytes, purpose: str) -> tuple[int, str, str]:
    """In-process stand-in for a core-banking system (demo / template testing)."""
    try:
        payload = json.loads(body or b"{}") if body and body[:1] in (b"{", b"[") else {}
    except ValueError:
        payload = {}
    if isinstance(payload, dict) and str(payload.get("force_error", "")).lower() in ("1", "true"):
        return 422, "application/json", json.dumps({"status": "REJECTED", "errorCode": "E422",
                                                    "message": "Business rule violated (simulated)"})
    ref = f"CORE-{secrets.token_hex(4).upper()}"
    out = {"status": "OK", "reference": ref, "message": f"{purpose.replace('_', ' ').lower()} accepted",
           "processedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "data": {"reference": ref, "result": "ACCEPTED", "method": method, "path": urllib.parse.urlparse(url).path}}
    if purpose == "GET_BALANCE":
        out["data"].update({"available": 12450.75, "currency": "ILS"})
    if purpose == "GET_TRANSACTION_STATUS":
        out["data"]["result"] = "EXECUTED"
    return 200, "application/json", json.dumps(out)


def _send(db: Session, conn: CoreConnection, api: CoreApi, req: dict, retries: int, timeout: float) -> dict:
    """Send with retries on network errors and 5xx (same idempotency key on every attempt)."""
    attempts, last_exc = 0, None
    while attempts <= retries:
        attempts += 1
        t0 = time.perf_counter()
        try:
            if req["url"].startswith("mock://"):
                status, ctype, text = _mock_core(req["method"], req["url"], req["body_bytes"], api.purpose)
            else:
                headers = {**req["headers"], **_auth_headers(db, conn, req["body_bytes"])}
                r = urllib.request.Request(check_url(req["url"]), data=req["body_bytes"] or None, method=req["method"],
                                           headers=headers)
                try:
                    with urllib.request.urlopen(r, timeout=timeout, context=_ssl_ctx(conn)) as resp:  # nosec B310
                        status, ctype = resp.status, resp.headers.get("Content-Type", "")
                        text = resp.read(500_000).decode("utf-8", "replace")
                except urllib.error.HTTPError as he:
                    status, ctype = he.code, he.headers.get("Content-Type", "") if he.headers else ""
                    text = he.read(500_000).decode("utf-8", "replace") if he.fp else ""
            ms = (time.perf_counter() - t0) * 1000
            if status >= 500 and attempts <= retries:
                time.sleep(min(2.0, 0.3 * attempts))
                continue
            return {"status": status, "ctype": ctype, "text": text, "ms": ms, "attempts": attempts}
        except Exception as exc:  # noqa: BLE001 - network errors are retried, then reported
            last_exc = exc
            if attempts <= retries:
                time.sleep(min(2.0, 0.3 * attempts))
                continue
            raise RuntimeError(sanitize_error(f"{type(exc).__name__}: {exc}")) from exc
    raise RuntimeError(str(last_exc))


# --------------------------------------------------------------------------- responses
def _parse(text: str, ctype: str):
    t = (text or "").strip()
    if not t:
        return None
    if "json" in ctype or t[:1] in ("{", "["):
        try:
            return json.loads(t)
        except ValueError:
            return None
    if "xml" in ctype or t.startswith("<"):
        if "<!DOCTYPE" in t[:2000].upper() or "<!ENTITY" in t.upper():
            return None
        try:
            root = ET.fromstring(t)
        except ET.ParseError:
            return None
        for el in root.iter():
            if "}" in el.tag:
                el.tag = el.tag.split("}", 1)[1]
        return root
    return None


def extract(doc, path: str):
    """Value at a response path: ``data.reference``, ``items[0].id`` (JSON) or ``Body/Response/Ref`` (XML)."""
    if doc is None or not path:
        return None
    if isinstance(doc, ET.Element):
        p = path.replace(".", "/")
        if p.split("/")[0] == doc.tag:
            p = "/".join(p.split("/")[1:])
        return doc.findtext(p) if p else doc.text
    cur = doc
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            idx = int(part[1:-1])
            cur = cur[idx] if isinstance(cur, list) and len(cur) > idx else None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _mask_tree(obj):
    if isinstance(obj, dict):
        return {k: (mask(str(v)) if SENSITIVE_KEYS.search(k) and isinstance(v, (str, int)) else _mask_tree(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_tree(v) for v in obj]
    return obj


def _mask_text(text: str) -> str:
    text = re.sub(r"(\"[^\"]*(?:card|pan|account|iban|token|secret|password)[^\"]*\"\s*:\s*\")([^\"]{4,})(\")",
                  lambda m: m.group(1) + mask(m.group(2)) + m.group(3), text or "", flags=re.I)
    return text[:4000]


def _mask_url(url: str) -> str:
    p = urllib.parse.urlparse(url)
    q = [(k, mask(v) if SENSITIVE_KEYS.search(k) else v) for k, v in urllib.parse.parse_qsl(p.query)]
    path = re.sub(r"(PS\d{2}[A-Z0-9]{8,}|\d{12,19})", lambda m: mask(m.group(1)), p.path)
    return urllib.parse.urlunparse(p._replace(path=path, query=urllib.parse.urlencode(q)))


# --------------------------------------------------------------------------- request building
def render_request(conn: CoreConnection, api: CoreApi, ctx: dict) -> dict:
    path = render_str(api.path or "", ctx, escape=lambda v: urllib.parse.quote(v, safe=""))
    url = conn.base_url.rstrip("/") + ("/" + path.lstrip("/") if path else "")
    query = {k: render_str(v, ctx) for k, v in (api.query or {}).items()}
    query = {k: v for k, v in query.items() if v not in (None, "")}
    if query:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
    headers = {"Accept": "application/json, application/xml;q=0.9", "User-Agent": "VeriShield"}
    headers.update({k: str(render_str(v, ctx)) for k, v in (conn.default_headers or {}).items()})
    headers.update({k: str(render_str(v, ctx)) for k, v in (api.headers or {}).items()})
    if api.idempotency_header:
        headers[api.idempotency_header] = ctx["idempotency_key"]
    body_obj, body = None, b""
    if api.method != "GET" and api.body_type != "NONE" and (api.body_template or "").strip():
        if api.body_type == "JSON":
            body_obj = render_tree(json.loads(api.body_template), ctx)
            body = json.dumps(body_obj, separators=(",", ":"), default=str).encode()
            headers["Content-Type"] = "application/json"
        elif api.body_type == "FORM":
            body_obj = render_tree(json.loads(api.body_template), ctx)
            body = urllib.parse.urlencode({k: "" if v is None else v for k, v in body_obj.items()}).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            from xml.sax.saxutils import escape as xml_escape
            body = render_str(api.body_template, ctx, escape=xml_escape).encode()
            headers["Content-Type"] = "application/xml"
    return {"method": api.method, "url": url, "headers": headers, "body_bytes": body, "body_obj": body_obj}


def masked_request(req: dict, conn: CoreConnection) -> dict:
    hdr = {k: ("***" if SENSITIVE_KEYS.search(k) else v) for k, v in req["headers"].items()}
    if conn.auth_type != "NONE":
        hdr["(authentication)"] = f"{conn.auth_type} - added when sent"
    body = _mask_tree(req["body_obj"]) if req["body_obj"] is not None else _mask_text(req["body_bytes"].decode("utf-8", "replace"))
    return {"method": req["method"], "url": _mask_url(req["url"]), "headers": hdr, "body": body}


# --------------------------------------------------------------------------- execution
def select_api(db: Session, tenant_id: str, space: str, purpose: str, api_id: Optional[str] = None) -> Optional[CoreApi]:
    q = select(CoreApi).where(CoreApi.tenant_id == tenant_id)
    if api_id:
        q = q.where(CoreApi.id == api_id)
    else:
        q = q.where(CoreApi.purpose == purpose,
                    (CoreApi.live_enabled.is_(True)) if space == "LIVE" else (CoreApi.demo_enabled.is_(True)))
    for api in db.execute(q.order_by(CoreApi.priority, CoreApi.created_at)).scalars():
        conn = db.get(CoreConnection, api.connection_id)
        if conn is not None and (conn.enabled or api_id):
            return api
    return None


def available(db: Session, tenant_id: str, space: str, purpose: str) -> bool:
    return select_api(db, tenant_id, space, purpose) is not None


def _run(db: Session, call: CoreApiCall, api: CoreApi, conn: CoreConnection, ctx: dict, dry_run: bool = False) -> None:
    missing = [i["name"] for i in (api.inputs or []) if i.get("required") and ctx.get(i["name"]) in (None, "")]
    if missing and not dry_run:
        call.status, call.error = "FAILED", f"missing required input(s): {', '.join(missing)}"
        return
    try:
        req = render_request(conn, api, ctx)
    except Exception as exc:  # noqa: BLE001 - bad template / filter
        call.status, call.error = "FAILED", sanitize_error(f"template error: {exc}")
        return
    call.request = masked_request(req, conn)
    if dry_run:
        call.status = "DRY_RUN"
        if missing:
            call.error = f"would fail: missing required input(s) {', '.join(missing)}"
        return
    try:
        res = _send(db, conn, api, req, int(api.retries if api.retries is not None else conn.retries or 0),
                    float(api.timeout_seconds or conn.timeout_seconds or 10))
    except Exception as exc:  # noqa: BLE001
        call.status, call.error = "FAILED", sanitize_error(str(exc))
        call.attempts = int((api.retries if api.retries is not None else conn.retries or 0)) + 1
        return
    call.http_status, call.attempts, call.duration_ms = res["status"], res["attempts"], round(res["ms"], 1)
    call.response_excerpt = _mask_text(res["text"])
    doc = _parse(res["text"], res["ctype"])
    mapping = {**{"reference": "reference", "status": "status", "message": "message"}, **(api.output_mapping or {})}
    result = {}
    for field, path in mapping.items():
        v = extract(doc, path)
        if v is not None:
            result[field] = v if isinstance(v, (int, float, bool)) else str(v)[:300]
    call.result = result
    codes = [int(c) for c in (api.success_codes or [])] or list(range(200, 300))
    ok = res["status"] in codes
    if ok and api.success_field:
        allowed = [str(v).upper() for v in (api.success_values or [])]
        ok = str(result.get(api.success_field, "")).upper() in allowed if allowed else bool(result.get(api.success_field))
    call.status = "SUCCESS" if ok else "FAILED"
    if not ok:
        call.error = str(result.get("message") or f"HTTP {res['status']}")[:500]


def execute(db: Session, tenant_id: str, space: str, purpose: str, inputs: dict, actor: str = "VeriShield",
            trigger: str = "AUTO", reason: str = "", api_id: Optional[str] = None, dry_run: bool = False,
            commit: bool = True) -> CoreApiCall:
    """Run the operation configured for ``purpose`` (or the given ``api_id``) and log the call."""
    inputs = build_inputs(db, tenant_id, space, purpose, {**(inputs or {}), **({"reason": reason} if reason else {})})
    call = CoreApiCall(tenant_id=tenant_id, space=space, purpose=purpose, trigger="DRYRUN" if dry_run else trigger,
                       inputs=_mask_tree(inputs), target_ref=_target(inputs), reason=(reason or "")[:400],
                       requested_by=actor[:120])
    db.add(call)
    db.flush()
    api = select_api(db, tenant_id, space, purpose, api_id)
    if api is None:
        call.status = "NO_API"
        call.error = f"no enabled core-banking API for {purpose} in the {space.lower()} workspace"
    else:
        call.api_id = api.id
        call.purpose = api.purpose
        conn = db.get(CoreConnection, api.connection_id)
        call.idempotency_key = f"VS-{call.id}"
        ctx = {**inputs, "idempotency_key": call.idempotency_key, "request_id": call.id, "institution": tenant_id,
               "workspace": space, "requested_by": actor, "now": utcnow().isoformat(timespec="seconds") + "Z",
               "purpose": api.purpose}
        _run(db, call, api, conn, ctx, dry_run)
        if call.status == "SUCCESS" and trigger != "TEST":
            _after_success(db, call, inputs)
    if commit:
        db.commit()
    if call.status not in ("DRY_RUN",):
        audit.record(db, tenant_id, f"core.{purpose.lower()}", actor=actor, resource_type="core_api",
                     resource_id=call.target_ref or call.id, space=space,
                     outcome="SUCCESS" if call.status in ("SUCCESS",) else "FAILURE" if call.status == "FAILED" else "SUCCESS",
                     reason=call.error or reason, after={"call": call.id, "status": call.status,
                                                         "reference": (call.result or {}).get("reference")})
    return call


def _after_success(db: Session, call: CoreApiCall, inputs: dict) -> None:
    """Reflect a successful core operation in VeriShield's own records."""
    ref = (call.result or {}).get("reference") or ""
    if call.purpose == "REVERSE_TRANSACTION" and inputs.get("transaction_id"):
        tx = db.execute(select(Transaction).where(Transaction.tenant_id == call.tenant_id, Transaction.space == call.space,
                                                  Transaction.tx_ref == str(inputs["transaction_id"]))).scalar()
        if tx is not None:
            from .decision import _execution
            _execution(db, tx, "REVERSED", "CORE_API", f"Reversed in the core banking system ({call.reason})",
                       call.approved_by or call.requested_by, ref)
    flags = {"BLOCK_ACCOUNT": ("account_blocked", True), "UNBLOCK_ACCOUNT": ("account_blocked", False),
             "BLOCK_DIGITAL_BANKING": ("digital_banking_suspended", True)}
    if call.purpose in flags and inputs.get("customer_id"):
        c = db.execute(select(Customer).where(Customer.tenant_id == call.tenant_id, Customer.space == call.space,
                                              Customer.customer_ref == str(inputs["customer_id"]))).scalar()
        if c is not None:
            key, value = flags[call.purpose]
            c.attributes = {**(c.attributes or {}), key: value, f"{key}_ref": ref,
                            f"{key}_at": utcnow().isoformat(timespec="seconds") + "Z"}


# --------------------------------------------------------------------------- manual operations & approval
def request_operation(db: Session, tenant: Tenant, space: str, purpose: str, inputs: dict, principal,
                      reason: str, api_id: Optional[str] = None) -> CoreApiCall:
    """A person asks for a core operation; operations that need approval wait for a second person."""
    api = select_api(db, tenant.id, space, purpose, api_id)
    if api is None:
        raise LookupError(f"no enabled core-banking API for {purpose} in the {space.lower()} workspace")
    roles = api.allowed_roles or DEFAULT_ROLES
    if principal.role not in roles:
        raise PermissionError(f"role {principal.role} may not run {purpose}")
    if len((reason or "").strip()) < 5:
        raise ValueError("give a reason")
    if not api.requires_approval:
        return execute(db, tenant.id, space, api.purpose, inputs, principal.display, "MANUAL", reason, api.id)
    full = build_inputs(db, tenant.id, space, api.purpose, {**inputs, "reason": reason})
    call = CoreApiCall(tenant_id=tenant.id, space=space, purpose=api.purpose, api_id=api.id, trigger="MANUAL",
                       status="PENDING_APPROVAL", inputs=full, target_ref=_target(full), reason=reason[:400],
                       requested_by=principal.display[:120])
    db.add(call)
    db.commit()
    audit.record(db, tenant.id, "core.request", actor=principal, resource_type="core_api", resource_id=call.id,
                 space=space, after={"purpose": api.purpose, "target": call.target_ref}, reason=reason)
    return call


def decide(db: Session, tenant: Tenant, space: str, call_id: str, approve: bool, principal) -> CoreApiCall:
    call = db.get(CoreApiCall, call_id)
    if call is None or call.tenant_id != tenant.id or call.space != space:
        raise LookupError("request not found")
    if call.status != "PENDING_APPROVAL":
        raise ValueError(f"request is {call.status}")
    if call.requested_by == principal.display:
        raise PermissionError("a second person must approve (four eyes)")
    call.approved_by, call.decided_at = principal.display[:120], utcnow()
    if not approve:
        call.status = "REJECTED"
        db.commit()
        audit.record(db, tenant.id, "core.reject", actor=principal, resource_type="core_api", resource_id=call.id,
                     space=space)
        return call
    api = db.get(CoreApi, call.api_id)
    conn = db.get(CoreConnection, api.connection_id) if api else None
    if api is None or conn is None:
        call.status, call.error = "FAILED", "the operation was deleted"
        db.commit()
        return call
    call.idempotency_key = f"VS-{call.id}"
    ctx = {**(call.inputs or {}), "idempotency_key": call.idempotency_key, "request_id": call.id,
           "institution": tenant.id, "workspace": space, "requested_by": call.requested_by,
           "approved_by": call.approved_by, "now": utcnow().isoformat(timespec="seconds") + "Z", "purpose": api.purpose}
    _run(db, call, api, conn, ctx)
    if call.status == "SUCCESS":
        _after_success(db, call, call.inputs or {})
    call.inputs = _mask_tree(call.inputs)
    db.commit()
    audit.record(db, tenant.id, f"core.{call.purpose.lower()}", actor=principal, resource_type="core_api",
                 resource_id=call.target_ref or call.id, space=space,
                 outcome="SUCCESS" if call.status == "SUCCESS" else "FAILURE", reason=call.error or call.reason,
                 after={"call": call.id, "status": call.status, "approved_by": call.approved_by})
    return call


# --------------------------------------------------------------------------- connection test
def test_connection(db: Session, conn: CoreConnection) -> tuple[bool, str]:
    if conn.base_url.startswith("mock://"):
        ok, detail = True, "built-in mock core system"
    else:
        try:
            if conn.auth_type == "OAUTH2_CC":
                _oauth_token(db, conn)
            req = urllib.request.Request(check_url(conn.base_url), method="GET",
                                         headers={**_auth_headers(db, conn, b""), "User-Agent": "VeriShield"})
            try:
                with urllib.request.urlopen(req, timeout=float(conn.timeout_seconds or 10), context=_ssl_ctx(conn)) as r:  # nosec B310
                    ok, detail = True, f"reachable (HTTP {r.status})"
            except urllib.error.HTTPError as he:
                ok, detail = he.code < 500, f"reachable (HTTP {he.code})" if he.code < 500 else f"server error HTTP {he.code}"
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, sanitize_error(f"{type(exc).__name__}: {exc}")
    conn.last_test_at, conn.last_test_ok, conn.last_test_detail = utcnow(), ok, detail[:300]
    db.commit()
    return ok, detail


# --------------------------------------------------------------------------- examples
EXAMPLES = {
    "FREEZE_CARD": ("POST", "/cards/{{card_ref}}/freeze",
                    '{"cardToken": "{{card_ref}}", "customerId": "{{customer_id}}", "reasonCode": "FRAUD_SUSPICION",'
                    ' "comment": "{{reason|default:VeriShield}}", "requestId": "{{request_id}}"}'),
    "UNFREEZE_CARD": ("POST", "/cards/{{card_ref}}/unfreeze",
                      '{"cardToken": "{{card_ref}}", "customerId": "{{customer_id}}", "comment": "{{reason|default:}}"}'),
    "BLOCK_CARD_PERMANENT": ("POST", "/cards/{{card_ref}}/block",
                             '{"cardToken": "{{card_ref}}", "blockType": "PERMANENT", "comment": "{{reason}}"}'),
    "RELEASE_PAYMENT": ("POST", "/payments/{{transaction_id}}/release",
                        '{"paymentId": "{{transaction_id}}", "decisionId": "{{decision_id|default:}}", "comment": "{{reason|default:}}"}'),
    "REJECT_PAYMENT": ("POST", "/payments/{{transaction_id}}/reject",
                       '{"paymentId": "{{transaction_id}}", "reasonCode": "FRAUD", "comment": "{{reason|default:}}"}'),
    "REVERSE_TRANSACTION": ("POST", "/transactions/{{transaction_id}}/reversal",
                            '{"originalTransactionId": "{{transaction_id}}", "amount": "{{amount}}", "currency": "{{currency}}",'
                            ' "reason": "{{reason}}", "requestedBy": "{{requested_by}}", "approvedBy": "{{approved_by|default:}}"}'),
    "BLOCK_ACCOUNT": ("POST", "/customers/{{customer_id}}/accounts/debit-block",
                      '{"customerId": "{{customer_id}}", "account": "{{account|default:ALL}}", "reason": "{{reason}}"}'),
    "UNBLOCK_ACCOUNT": ("DELETE", "/customers/{{customer_id}}/accounts/debit-block", ""),
    "PLACE_HOLD": ("POST", "/customers/{{customer_id}}/holds",
                   '{"account": "{{account|default:PRIMARY}}", "amount": "{{amount}}", "currency": "{{currency}}", "reason": "{{reason}}"}'),
    "BLOCK_DIGITAL_BANKING": ("POST", "/digital/customers/{{customer_id}}/suspend", '{"reason": "{{reason}}"}'),
    "GET_TRANSACTION_STATUS": ("GET", "/transactions/{{transaction_id}}", ""),
    "GET_BALANCE": ("GET", "/customers/{{customer_id}}/balance", ""),
}


def seed_examples(db: Session, tenant_id: str, actor: str = "installer") -> int:
    """A mock connection and one example operation per built-in purpose (Demo only until the IT Admin edits them)."""
    if db.execute(select(CoreConnection.id).where(CoreConnection.tenant_id == tenant_id)).first():
        return 0
    conn = CoreConnection(tenant_id=tenant_id, name="Core banking - built-in mock (demo & template testing)",
                          base_url="mock://core", auth_type="NONE", created_by=actor, timeout_seconds=5, retries=1)
    db.add(conn)
    db.flush()
    n = 0
    for purpose, (method, path, body) in EXAMPLES.items():
        p = BUILTIN_PURPOSES[purpose]
        db.add(CoreApi(tenant_id=tenant_id, name=f"{p['label']} (example)", purpose=purpose, connection_id=conn.id,
                       description=p["description"], method=method, path=path, body_type="JSON" if body else "NONE",
                       body_template=body, inputs=copy.deepcopy(p["inputs"]),
                       output_mapping={"reference": "data.reference", "status": "status", "message": "message",
                                       **({"available": "data.available"} if purpose == "GET_BALANCE" else {}),
                                       **({"core_status": "data.result"} if purpose == "GET_TRANSACTION_STATUS" else {})},
                       success_codes=[200, 201, 202, 204], success_field="status", success_values=["OK", "ACCEPTED"],
                       requires_approval=p["approval"], allowed_roles=list(DEFAULT_ROLES), live_enabled=False,
                       demo_enabled=True, created_by=actor, updated_by=actor))
        n += 1
    db.commit()
    return n


def as_api(a: CoreApi) -> dict:
    return {k: getattr(a, k) for k in ("id", "name", "purpose", "description", "connection_id", "method", "path",
                                        "query", "headers", "body_type", "body_template", "inputs", "output_mapping",
                                        "success_codes", "success_field", "success_values", "idempotency_header",
                                        "timeout_seconds", "retries", "requires_approval", "allowed_roles",
                                        "live_enabled", "demo_enabled", "priority", "version", "created_by",
                                        "updated_by", "created_at", "updated_at")}


def as_connection(c: CoreConnection) -> dict:
    out = {k: getattr(c, k) for k in ("id", "name", "base_url", "auth_type", "auth", "verify_tls", "ca_file",
                                       "client_cert_file", "client_key_file", "timeout_seconds", "retries",
                                       "default_headers", "enabled", "last_test_at", "last_test_ok", "last_test_detail",
                                       "created_by", "created_at", "updated_at")}
    out["secret_configured"] = bool(c.secret_ref)
    return out


def as_call(c: CoreApiCall) -> dict:
    return {k: getattr(c, k) for k in ("id", "api_id", "purpose", "trigger", "status", "inputs", "target_ref", "request",
                                        "http_status", "response_excerpt", "result", "error", "attempts", "duration_ms",
                                        "idempotency_key", "reason", "requested_by", "approved_by", "decided_at",
                                        "created_at", "space")}


__all__ = ["execute", "request_operation", "decide", "purposes", "save_purpose", "delete_purpose", "render_str",
           "render_tree", "extract", "validate_api", "test_connection", "seed_examples", "available", "select_api",
           "as_api", "as_connection", "as_call", "AUTH_TYPES", "METHODS", "BODY_TYPES", "CATEGORIES"]
