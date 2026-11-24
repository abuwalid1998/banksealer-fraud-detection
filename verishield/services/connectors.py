"""Connector registry, testing, field-mapping preview and runtime (BRD section 14).

Lifecycle: ``DRAFT -> TESTING -> VALIDATED -> ENABLED -> DEGRADED/DISABLED -> RETIRED``.

Secrets are never stored in connector configuration: a connector holds a ``secret_ref``
(``vault:<id>`` for the encrypted local vault, or ``env:<NAME>`` for an environment variable /
external secrets manager injection). Secret values are never returned by the API.

Every inbound channel works in both workspaces: in Demo Mode, live channels feed the Demo workspace and
every record is scored in real time; only outbound payment actions are simulated there.

Runtime adapters bundled in this build: REST webhook (VeriShield API), REST pull, TCP JSON-lines,
HTTP listener, CSV/JSONL file drop, database read replica (any SQLAlchemy URL) and the Demo synthetic
stream. Kafka / RabbitMQ / JMS / SFTP connectors are registered, tested (network reachability)
and monitored, but need a broker/transfer client adapter before they can be enabled.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from banksealer.preparation import TransactionPreparer

from ..db import SessionLocal
from ..models import ApiClient, Connector, ConnectorLog, SecretRecord, utcnow
from ..security import decrypt_secret, encrypt_secret, mask, mask_ip, sanitize_error
from .metrics import metrics

log = logging.getLogger("verishield.connectors")

CONNECTOR_TYPES: dict[str, dict] = {
    "rest_webhook": {"label": "REST API / webhook (push to VeriShield)", "direction": "INBOUND", "runtime": True,
                     "fields": [], "auth": ["API_KEY"],
                     "help": "The bank posts events to /api/v1/transactions/events or /api/v1/decisions/evaluate "
                             "with an API key."},
    "rest_pull": {"label": "REST API pull (VeriShield polls the bank)", "direction": "INBOUND", "runtime": True,
                  "fields": ["url", "records_path", "poll_seconds"], "auth": ["NONE", "BEARER", "BASIC", "OAUTH2_CC"]},
    "tcp_json": {"label": "TCP JSON-lines feed", "direction": "INBOUND", "runtime": True,
                 "fields": ["host", "port"], "auth": ["NETWORK_ALLOWLIST"]},
    "http_listener": {"label": "HTTP webhook listener (separate port)", "direction": "INBOUND", "runtime": True,
                      "fields": ["host", "port", "path"], "auth": ["NETWORK_ALLOWLIST"]},
    "file_drop": {"label": "CSV / JSONL file drop", "direction": "INBOUND", "runtime": True,
                  "fields": ["path", "follow", "poll_seconds", "delimiter"], "auth": ["NONE"]},
    "db_replica": {"label": "Database read replica / CDC table", "direction": "INBOUND", "runtime": True,
                   "fields": ["url", "table", "id_column", "poll_seconds", "batch"], "auth": ["VAULT_CREDENTIAL"]},
    "kafka": {"label": "Kafka topic", "direction": "INBOUND", "runtime": False,
              "fields": ["host", "port", "topic", "consumer_group"], "auth": ["SASL", "MTLS"]},
    "rabbitmq": {"label": "RabbitMQ queue", "direction": "INBOUND", "runtime": False,
                 "fields": ["host", "port", "queue", "vhost"], "auth": ["BASIC", "MTLS"]},
    "jms": {"label": "JMS / IBM MQ", "direction": "INBOUND", "runtime": False,
            "fields": ["host", "port", "queue"], "auth": ["BASIC", "MTLS"]},
    "sftp": {"label": "SFTP / managed file transfer", "direction": "INBOUND", "runtime": False,
             "fields": ["host", "port", "path", "schedule"], "auth": ["SSH_KEY", "BASIC"]},
    "outbound_webhook": {"label": "Bank execution adapter (outbound REST callback)", "direction": "OUTBOUND",
                         "runtime": True, "fields": ["url"], "auth": ["NONE", "BEARER", "HMAC_SIGNED", "MTLS"]},
    "db_results": {"label": "Fraud engine results database (writes every decision)", "direction": "OUTBOUND",
                   "runtime": True, "fields": ["url", "table", "batch", "poll_seconds", "backfill"],
                   "auth": ["NONE", "VAULT_CREDENTIAL"],
                   "help": "SQLAlchemy URL, e.g. postgresql://vs_writer:{secret}@dbhost:5432/fraud or "
                           "sqlite:///C:/ProgramData/VeriShield/data/fraud_results.db. The table is created if "
                           "missing; each scored transaction is written with its scores, decision and reasons."},
    "demo_stream": {"label": "Payment gateway simulator (Demo workspace)", "direction": "INBOUND", "runtime": True,
                    "fields": ["speed", "fraud_ratio"], "auth": ["NONE"], "demo_only": True},
}
STATUSES = ("DRAFT", "TESTING", "VALIDATED", "ENABLED", "DEGRADED", "DISABLED", "RETIRED")


class ConnectorError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# --------------------------------------------------------------------------- secrets
def store_secret(db: Session, tenant_id: str, name: str, value: str, actor_id: Optional[str],
                 existing_ref: Optional[str] = None) -> str:
    if existing_ref and existing_ref.startswith("vault:"):
        rec = db.get(SecretRecord, existing_ref[6:])
        if rec is not None and rec.tenant_id == tenant_id:
            rec.ciphertext = encrypt_secret(value)
            rec.rotated_at = utcnow()
            db.flush()
            return existing_ref
    rec = SecretRecord(tenant_id=tenant_id, name=name, ciphertext=encrypt_secret(value), created_by=actor_id)
    db.add(rec)
    db.flush()
    return f"vault:{rec.id}"


def resolve_secret(db: Session, tenant_id: str, ref: Optional[str]) -> Optional[str]:
    if not ref:
        return None
    if ref.startswith("env:"):
        return os.environ.get(ref[4:])
    if ref.startswith("vault:"):
        rec = db.get(SecretRecord, ref[6:])
        if rec is None or rec.tenant_id != tenant_id:
            return None
        return decrypt_secret(rec.ciphertext)
    return None


def public(c: Connector) -> dict:
    """Connector as returned by the API: secret references are shown, secret values never."""
    info = CONNECTOR_TYPES.get(c.type, {})
    rt = runtime.status(c.id)
    expires_in = (c.credential_expires_at - utcnow()).days if c.credential_expires_at else None
    return {
        "id": c.id, "name": c.name, "type": c.type, "type_label": info.get("label", c.type), "space": c.space,
        "direction": c.direction, "environment": c.environment, "status": c.status, "ingest_mode": c.ingest_mode,
        "config": {k: (v if k != "url" else sanitize_error(str(v))) for k, v in (c.config or {}).items()},
        "auth_method": c.auth_method, "secret_configured": bool(c.secret_ref),
        "secret_ref_kind": (c.secret_ref or "").split(":", 1)[0] or None,
        "tls_verify": c.tls_verify, "field_mapping": c.field_mapping or {}, "schedule": c.schedule or {},
        "allowed_ips": c.allowed_ips or [], "data_scope": c.data_scope, "permission": c.permission,
        "owner": c.owner, "credential_expires_at": c.credential_expires_at.isoformat() + "Z"
        if c.credential_expires_at else None,
        "credential_expiry_warning": expires_in is not None and expires_in <= 14, "credential_expires_in_days": expires_in,
        "last_test_at": c.last_test_at.isoformat() + "Z" if c.last_test_at else None, "last_test_ok": c.last_test_ok,
        "last_test_message": c.last_test_message, "last_success_at": c.last_success_at.isoformat() + "Z"
        if c.last_success_at else None,
        "last_event_at": c.last_event_at.isoformat() + "Z" if c.last_event_at else None,
        "last_error": c.last_error, "counters": c.counters or {}, "version": c.version,
        "runtime_supported": info.get("runtime", False), "running": rt.get("running", False),
        "runtime": rt, "created_at": c.created_at.isoformat() + "Z",
        "updated_at": c.updated_at.isoformat() + "Z" if c.updated_at else None,
    }


def add_log(db: Session, c: Connector, message: str, level: str = "INFO", actor: str = "system") -> None:
    db.add(ConnectorLog(tenant_id=c.tenant_id, connector_id=c.id, level=level, message=sanitize_error(message),
                        actor=actor))


def validate(data: dict, space: str) -> None:
    ctype = data.get("type")
    if ctype not in CONNECTOR_TYPES:
        raise ConnectorError(422, f"type must be one of {', '.join(CONNECTOR_TYPES)}")
    if CONNECTOR_TYPES[ctype].get("demo_only") and space != "DEMO":
        raise ConnectorError(422, "the synthetic stream connector is only available in Demo Mode")
    cfg = data.get("config") or {}
    for key in ("password", "token", "secret", "api_key", "apikey"):
        if key in {k.lower() for k in cfg}:
            raise ConnectorError(422, "secrets must not be placed in connector configuration; "
                                      "use the secret field (stored in the vault) or an env: reference")
    url = str(cfg.get("url", ""))
    if "@" in url.split("//", 1)[-1].split("/", 1)[0] and ":" in url.split("//", 1)[-1].split("@", 1)[0]:
        raise ConnectorError(422, "remove the password from the URL; use {secret} as a placeholder and set the "
                                  "secret separately")
    if data.get("ingest_mode") and data["ingest_mode"] not in ("PRE_AUTH", "POST_EVENT", "HISTORY"):
        raise ConnectorError(422, "ingest_mode must be PRE_AUTH, POST_EVENT or HISTORY")


# --------------------------------------------------------------------------- testing
def _tcp_reachable(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, f"TCP connection to {host}:{port} succeeded"
    except OSError as exc:
        return False, f"cannot reach {host}:{port}: {exc}"


def _port_free(host: str, port: int) -> tuple[bool, str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, int(port)))
        return True, f"{host}:{port} is available for the listener"
    except OSError as exc:
        return False, f"cannot listen on {host}:{port}: {exc}"


def _url(db: Session, c: Connector) -> str:
    url = str((c.config or {}).get("url", ""))
    if "{secret}" in url:
        url = url.replace("{secret}", resolve_secret(db, c.tenant_id, c.secret_ref) or "")
    return url


def _headers(db: Session, c: Connector) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "VeriShield-Connector/1.0"}
    secret = resolve_secret(db, c.tenant_id, c.secret_ref)
    if secret and c.auth_method in ("BEARER", "OAUTH2_CC"):
        headers["Authorization"] = f"Bearer {secret}"
    elif secret and c.auth_method == "BASIC":
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(secret.encode()).decode()
    return headers


def _http(db: Session, c: Connector, method: str = "GET", body: Optional[dict] = None,
          timeout: float = 5.0) -> tuple[int, Any]:
    url = _url(db, c)
    if not url.startswith(("http://", "https://")):
        raise ConnectorError(422, "url must start with http:// or https://")
    data = json.dumps(body).encode() if body is not None else None
    headers = _headers(db, c)
    if data is not None:
        headers["Content-Type"] = "application/json"
        if c.auth_method == "HMAC_SIGNED":
            import hashlib
            import hmac
            secret = resolve_secret(db, c.tenant_id, c.secret_ref) or ""
            headers["X-VeriShield-Signature"] = hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    ctx = None
    if url.startswith("https://") and not c.tls_verify:
        import ssl
        ctx = ssl._create_unverified_context()  # noqa: S323 - explicit, audited per-connector setting
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310 - admin-configured URL
        raw = resp.read(2_000_000)
        try:
            return resp.status, json.loads(raw.decode() or "null")
        except (ValueError, UnicodeDecodeError):
            return resp.status, raw[:500].decode(errors="replace")


def _records_from(payload: Any, path: str = "") -> list:
    if path:
        for part in path.split("."):
            payload = payload.get(part, []) if isinstance(payload, dict) else []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("transactions", "records", "items", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return [payload]
    return []


def sample_records(db: Session, c: Connector, limit: int = 5) -> list[dict]:
    cfg = c.config or {}
    if c.type == "file_drop":
        from banksealer.cli import read_records
        path = cfg.get("path", "")
        if not os.path.exists(path):
            raise ConnectorError(422, f"file {path} not found")
        out = []
        for rec in read_records(path):
            out.append(rec)
            if len(out) >= limit:
                break
        return out
    if c.type == "db_replica":
        from sqlalchemy import create_engine, text
        eng = create_engine(_url(db, c))
        try:
            with eng.connect() as conn:
                rows = conn.execute(text(f"SELECT * FROM {_safe_ident(cfg.get('table', 'transactions'))} LIMIT :n"),
                                    {"n": limit}).mappings().all()
                return [dict(r) for r in rows]
        finally:
            eng.dispose()
    if c.type == "rest_pull":
        _, payload = _http(db, c)
        return _records_from(payload, cfg.get("records_path", ""))[:limit]
    return []


def _safe_ident(name: str) -> str:
    import re
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\.]{0,62}", name or ""):
        raise ConnectorError(422, "invalid table/column name")
    return name


def test_connection(db: Session, c: Connector, actor: str) -> dict:
    cfg = c.config or {}
    c.status = "TESTING" if c.status in ("DRAFT", "VALIDATED", "DISABLED", "DEGRADED") else c.status
    ok, message = False, ""
    try:
        if c.type == "rest_webhook":
            n = db.execute(select(ApiClient).where(ApiClient.tenant_id == c.tenant_id, ApiClient.space == c.space,
                                                   ApiClient.active.is_(True))).scalars().all()
            n = [a for a in n if {"events:write", "decisions:write"} & set(a.scopes or [])]
            ok = bool(n)
            message = (f"{len(n)} active API client(s) can push events/decisions to this workspace" if ok else
                       "create an API client with events:write or decisions:write scope (Users & Settings > API "
                       "clients) for the bank system")
        elif c.type in ("rest_pull", "outbound_webhook"):
            try:
                status, _ = _http(db, c, "GET")
                ok, message = status < 500, f"HTTP {status} from {_url(db, c).split('?')[0]}"
            except urllib.error.HTTPError as exc:
                ok = exc.code in (401, 403, 404, 405) and c.type == "outbound_webhook"
                message = f"HTTP {exc.code}" + (" - endpoint reachable (method not allowed for GET is expected)"
                                                if ok else "")
        elif c.type in ("tcp_json", "http_listener"):
            if runtime.status(c.id).get("running"):
                ok, message = True, "listener is running"
            else:
                ok, message = _port_free(cfg.get("host", "127.0.0.1"), int(cfg.get("port", 0) or 0))
        elif c.type == "file_drop":
            path = cfg.get("path", "")
            if os.path.exists(path):
                ok, message = os.access(path, os.R_OK), f"{path} is readable ({os.path.getsize(path):,} bytes)"
            else:
                folder = os.path.dirname(os.path.abspath(path))
                ok = os.path.isdir(folder) and bool(cfg.get("follow", True))
                message = f"{path} does not exist yet" + (" - will be followed when created" if ok else "")
        elif c.type == "db_replica":
            from sqlalchemy import create_engine, func as sfunc, text
            eng = create_engine(_url(db, c))
            try:
                with eng.connect() as conn:
                    n = conn.execute(text(f"SELECT COUNT(*) FROM {_safe_ident(cfg.get('table', 'transactions'))}"))\
                        .scalar()
                ok, message = True, f"connected (read-only scope); table has {n:,} rows"
            finally:
                eng.dispose()
            _ = sfunc
        elif c.type in ("kafka", "rabbitmq", "jms", "sftp"):
            ok, message = _tcp_reachable(cfg.get("host", ""), int(cfg.get("port", 0) or 0))
            message += " (network reachability only - a broker/transfer client adapter is required to consume)"
        elif c.type == "db_results":
            from . import results_db
            ok, message = results_db.test(_url(db, c), _safe_ident(cfg.get("table", "fraud_results")))
        elif c.type == "demo_stream":
            ok, message = c.space == "DEMO", "synthetic generator available" if c.space == "DEMO" else \
                "only available in Demo Mode"
        else:
            message = "unsupported connector type"
    except ConnectorError as exc:
        message = exc.message
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
    message = sanitize_error(message)
    c.last_test_at = utcnow()
    c.last_test_ok = ok
    c.last_test_message = message
    if ok:
        c.last_success_at = utcnow()
        if c.status == "TESTING":
            c.status = "VALIDATED"
    elif c.status == "TESTING":
        c.status = "DRAFT"
    add_log(db, c, f"Test connection: {'OK' if ok else 'FAILED'} - {message}", "INFO" if ok else "ERROR", actor)
    db.commit()
    return {"ok": ok, "message": message, "status": c.status}


def preview_mapping(db: Session, c: Connector, samples: Optional[list] = None) -> dict:
    """Validate the field mapping on sample records; outputs are masked."""
    if samples is None:
        samples = sample_records(db, c)
    from .decision import map_request
    prep = TransactionPreparer(c.field_mapping or {}, default_bank=c.tenant_id)
    rows = []
    for raw in samples[:10]:
        tx = prep.prepare(map_request(raw, c.tenant_id) if "transaction_type" in raw or "customer_id" in raw
                          else raw, c.name)
        if tx is None:
            rows.append({"ok": False, "error": (prep.errors[-1] if prep.errors else "rejected").split("] ", 1)[-1],
                         "source_fields": sorted(map(str, raw.keys()))})
        else:
            rows.append({"ok": True, "transaction_id": tx.tx_id, "customer_id": mask(tx.user_id, 2),
                         "timestamp": tx.timestamp.isoformat(sep=" ", timespec="seconds"), "type": tx.tx_type,
                         "amount": tx.amount, "currency": tx.currency, "beneficiary": mask(tx.beneficiary),
                         "beneficiary_country": tx.iban_cc, "ip": mask_ip(tx.ip), "connection": tx.cc_asn,
                         "source_fields": sorted(map(str, raw.keys()))})
    return {"samples": len(rows), "accepted": sum(1 for r in rows if r["ok"]), "rows": rows,
            "mapping": c.field_mapping or {}}


def send_bank_command(db: Session, tenant_id: str, payload: dict) -> dict:
    c = db.execute(select(Connector).where(Connector.tenant_id == tenant_id, Connector.space == "LIVE",
                                           Connector.type == "outbound_webhook", Connector.status == "ENABLED")
                   ).scalars().first()
    if c is None:
        return {"sent": False, "message": "No enabled bank execution adapter - command recorded and awaiting the "
                                          "bank's execution result"}
    try:
        status, _ = _http(db, c, "POST", payload, timeout=float((c.schedule or {}).get("timeout_seconds", 5)))
        c.last_success_at = utcnow()
        add_log(db, c, f"{payload['command']} {payload['transaction_id']} sent (HTTP {status})")
        return {"sent": True, "message": f"{payload['command']} sent to bank adapter {c.name} (HTTP {status})"}
    except Exception as exc:  # noqa: BLE001
        msg = sanitize_error(f"{type(exc).__name__}: {exc}")
        c.last_error = msg
        c.status = "DEGRADED"
        add_log(db, c, f"{payload['command']} {payload['transaction_id']} FAILED: {msg}", "ERROR")
        from .decision import open_incident
        open_incident(db, tenant_id, f"connector:{c.id}", f"Bank execution adapter {c.name} failed", "SEV2",
                      affected=c.name)
        return {"sent": False, "message": f"Bank adapter error - retry required: {msg}"}


# --------------------------------------------------------------------------- runtime
class _Runner(threading.Thread):
    def __init__(self, c: Connector):
        super().__init__(name=f"connector-{c.id}", daemon=True)
        self.cid, self.tenant_id, self.space = c.id, c.tenant_id, c.space
        self.ctype, self.cfg = c.type, dict(c.config or {})
        self.mapping, self.mode, self.name = dict(c.field_mapping or {}), c.ingest_mode, c.name
        self.stop_event = threading.Event()
        self.channel = None
        self.stats = {"received": 0, "accepted": 0, "rejected": 0, "duplicates": 0, "batches": 0}
        self.last_error = ""
        self.started_at = utcnow()
        self.last_batch_ms = 0.0

    # sources ---------------------------------------------------------------
    def _start_channel(self) -> None:
        from banksealer.channels.files import CSVChannel
        from banksealer.channels.network import HTTPChannel, TCPChannel
        cls = {"tcp_json": TCPChannel, "http_listener": HTTPChannel, "file_drop": CSVChannel}[self.ctype]
        self.channel = cls(self.name, self.cfg, None, queue.Queue())
        self.channel.start()

    def _drain_channel(self) -> list:
        out = []
        while len(out) < 2000:
            try:
                _, record, _ = self.channel.queue.get_nowait()
            except queue.Empty:
                break
            out.append(record)
        if self.channel.status == "error":
            self.last_error = self.channel.last_error
        return out

    def _poll_db(self, state: dict) -> list:
        from sqlalchemy import create_engine, text
        with SessionLocal() as db:
            c = db.get(Connector, self.cid)
            url = _url(db, c)
        if "engine" not in state:
            state["engine"] = create_engine(url)
        table = _safe_ident(self.cfg.get("table", "transactions"))
        idcol = _safe_ident(self.cfg.get("id_column", "rowid"))
        with state["engine"].connect() as conn:
            rows = conn.execute(text(f"SELECT {idcol} AS __vs_id, * FROM {table} WHERE {idcol} > :last "
                                     f"ORDER BY {idcol} LIMIT :n"),
                                {"last": state.get("last", self.cfg.get("start_after", 0)),
                                 "n": int(self.cfg.get("batch", 500))}).mappings().all()
        out = []
        for r in rows:
            d = dict(r)
            state["last"] = d.pop("__vs_id")
            out.append(d)
        return out

    def _poll_rest(self) -> list:
        with SessionLocal() as db:
            c = db.get(Connector, self.cid)
            _, payload = _http(db, c)
        return _records_from(payload, self.cfg.get("records_path", ""))

    def _demo_stream(self, state: dict) -> list:
        from .demo import GatewaySimulator
        if "sim" not in state:
            resume = None
            with SessionLocal() as db:
                c = db.get(Connector, self.cid)
                cur = (c.counters or {}).get("sim_cursor")
            if cur:
                resume = datetime.fromisoformat(cur) + timedelta(seconds=1)
            state["sim"] = GatewaySimulator(self.tenant_id, float(self.cfg.get("fraud_ratio", 0.03)),
                                            float(self.cfg.get("speed", 60)), start=resume)
        sim = state["sim"]
        records = sim.due(int(self.cfg.get("max_batch", 200)))
        if records:
            self.stats["sim_cursor"] = sim.cursor.isoformat(timespec="seconds")
        return records

    def _write_results(self, state: dict) -> None:
        from . import results_db
        with SessionLocal() as db:
            if "engine" not in state:
                c = db.get(Connector, self.cid)
                state["engine"] = results_db.make_engine(_url(db, c))
                state["table"] = _safe_ident(self.cfg.get("table", "fraud_results"))
                cur = (c.counters or {}).get("cursor")
                if cur is None:
                    cur = 0 if self.cfg.get("backfill") else results_db.current_cursor(db, self.tenant_id, self.space)
                state["cursor"] = int(cur)
            written, state["cursor"] = results_db.write_new(db, state["engine"], state["table"], self.tenant_id,
                                                            self.space, state["cursor"],
                                                            int(self.cfg.get("batch", 500)))
        self.stats["received"] += written
        self.stats["accepted"] += written
        self.stats["cursor"] = state["cursor"]
        if written:
            self.stats["batches"] += 1
        self._persist()

    # loop ------------------------------------------------------------------
    def run(self) -> None:
        from .decision import ingest_events
        state: dict = {}
        poll = float(self.cfg.get("poll_seconds", 2.0 if self.ctype != "rest_pull" else 10.0))
        try:
            if self.ctype in ("tcp_json", "http_listener", "file_drop"):
                self._start_channel()
                poll = 1.0
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._persist(error=True)
            return
        while not self.stop_event.is_set():
            started = time.perf_counter()
            try:
                if self.ctype == "db_results":
                    self._write_results(state)
                    self.last_error = ""
                    self.stop_event.wait(poll)
                    continue
                if self.ctype in ("tcp_json", "http_listener", "file_drop"):
                    records = self._drain_channel()
                elif self.ctype == "db_replica":
                    records = self._poll_db(state)
                elif self.ctype == "rest_pull":
                    records = self._poll_rest()
                elif self.ctype == "demo_stream":
                    records = self._demo_stream(state)
                else:
                    records = []
                if records:
                    with SessionLocal() as db:
                        res = ingest_events(db, self.tenant_id, self.space, records, source=self.name,
                                            mode=self.mode, connector_id=self.cid, field_mapping=self.mapping)
                    for k in ("received", "accepted", "rejected", "duplicates"):
                        self.stats[k] += res[k]
                    self.stats["batches"] += 1
                    self.last_batch_ms = round((time.perf_counter() - started) * 1000, 1)
                    metrics.observe(f"connector:{self.cid}", self.last_batch_ms)
                    self._persist()
                if self.last_error and self.ctype in ("rest_pull", "db_replica"):
                    self.last_error = ""
            except Exception as exc:  # noqa: BLE001
                self.last_error = sanitize_error(f"{type(exc).__name__}: {exc}")
                self._persist(error=True)
                self.stop_event.wait(min(60.0, poll * 3))
            self.stop_event.wait(poll)
        if self.channel is not None:
            self.channel.stop()
        state.get("engine") and state["engine"].dispose()

    def _persist(self, error: bool = False) -> None:
        try:
            with SessionLocal() as db:
                c = db.get(Connector, self.cid)
                if c is None:
                    return
                counters = dict(c.counters or {})
                for k in ("received", "accepted", "rejected", "duplicates", "batches"):
                    counters[k] = counters.get("base_" + k, 0) + self.stats[k]
                for key in ("cursor", "sim_cursor"):
                    if key in self.stats:
                        counters[key] = self.stats[key]
                c.counters = counters
                if error:
                    c.last_error = self.last_error
                    if c.status == "ENABLED":
                        c.status = "DEGRADED"
                        add_log(db, c, f"Connector degraded: {self.last_error}", "ERROR")
                        from .decision import open_incident
                        open_incident(db, c.tenant_id, f"connector:{c.id}", f"Connector {c.name} degraded", "SEV3",
                                      affected=c.name)
                else:
                    c.last_event_at = utcnow()
                    c.last_success_at = utcnow()
                    if c.status == "DEGRADED":
                        c.status = "ENABLED"
                        add_log(db, c, "Connector recovered")
                db.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not persist connector state: %s", exc)


class RuntimeManager:
    def __init__(self):
        self.runners: dict[str, _Runner] = {}
        self.lock = threading.Lock()

    def start(self, c: Connector) -> None:
        info = CONNECTOR_TYPES.get(c.type, {})
        if not info.get("runtime") or c.type in ("rest_webhook", "outbound_webhook"):
            return
        with self.lock:
            old = self.runners.get(c.id)
            if old and old.is_alive():
                return
            counters = dict(c.counters or {})
            for k in ("received", "accepted", "rejected", "duplicates", "batches"):
                counters["base_" + k] = counters.get(k, 0)
            c.counters = counters
            runner = _Runner(c)
            self.runners[c.id] = runner
            runner.start()

    def stop(self, cid: str) -> None:
        with self.lock:
            runner = self.runners.pop(cid, None)
        if runner:
            runner.stop_event.set()
            runner.join(timeout=5)

    def stop_all(self) -> None:
        for cid in list(self.runners):
            self.stop(cid)

    def status(self, cid: str) -> dict:
        r = self.runners.get(cid)
        if r is None:
            return {"running": False}
        return {"running": r.is_alive(), "since": r.started_at.isoformat(timespec="seconds") + "Z",
                "session_stats": dict(r.stats), "last_error": r.last_error, "last_batch_ms": r.last_batch_ms}


runtime = RuntimeManager()


def start_enabled(db: Session) -> None:
    for c in db.execute(select(Connector).where(Connector.status.in_(("ENABLED", "DEGRADED")))).scalars():
        try:
            runtime.start(c)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not start connector %s: %s", c.id, exc)
    db.commit()


def set_state(db: Session, c: Connector, action: str, actor: str, demo_workspace: bool) -> Connector:
    info = CONNECTOR_TYPES.get(c.type, {})
    if action == "enable":
        if c.status not in ("VALIDATED", "DISABLED", "DEGRADED"):
            raise ConnectorError(409, "test the connection successfully before enabling")
        if not c.last_test_ok:
            raise ConnectorError(409, "the last connection test failed; fix it and test again")
        if not info.get("runtime"):
            raise ConnectorError(409, f"{info.get('label', c.type)} needs a broker/transfer client adapter that is "
                                      "not bundled in this build; it can be monitored but not enabled")
        c.status = "ENABLED"
        runtime.start(c)
        add_log(db, c, "Connector enabled", actor=actor)
    elif action == "disable":
        runtime.stop(c.id)
        c.status = "DISABLED"
        add_log(db, c, "Connector disabled", actor=actor)
    elif action == "retire":
        runtime.stop(c.id)
        c.status = "RETIRED"
        add_log(db, c, "Connector retired", actor=actor)
    elif action == "restart":
        runtime.stop(c.id)
        if c.status in ("ENABLED", "DEGRADED"):
            runtime.start(c)
            c.status = "ENABLED"
        add_log(db, c, "Connector restarted (retry)", actor=actor)
    else:
        raise ConnectorError(422, "unknown action")
    c.updated_at = utcnow()
    db.commit()
    return c


__all__ = ["CONNECTOR_TYPES", "STATUSES", "public", "validate", "test_connection", "preview_mapping",
           "store_secret", "resolve_secret", "send_bank_command", "runtime", "start_enabled", "set_state",
           "ConnectorError", "add_log", "datetime", "timedelta"]
