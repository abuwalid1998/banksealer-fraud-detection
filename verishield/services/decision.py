"""Decision orchestrator (BRD 7.2, 8, 15): canonical event -> rules + fraud + AML -> PASS/REVIEW/HOLD/DECLINE.

The synchronous decision path and the asynchronous ingestion path share this module but are
exposed on separate endpoints. ``REVIEW`` never stops a payment; ``HOLD`` and ``DECLINE`` only
take effect once the bank's authorisation system acknowledges them (execution events). A
post-event transaction is labelled ``POST_EVENT`` and is never reported as blocked.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from banksealer.engine.banksealer import PROFILE_NAMES
from banksealer.models import Transaction as ETx
from banksealer.preparation import TransactionPreparer, parse_timestamp

from .. import audit
from ..models import (Alert, Customer, Decision, DeadLetterEvent, ExecutionStatusEvent, SystemIncident, Tenant,
                      Transaction, User, utcnow)
from . import aml as aml_service
from .common import space_ref
from .engine_registry import engine_tx_fields, workspace
from .metrics import metrics
from .policy import SEVERITY, active_policy, combine, rule_matches

log = logging.getLogger("verishield.decision")

LOCAL_CODES = {"amount": "UNUSUAL_AMOUNT", "iban": "NEW_BENEFICIARY", "phone_number": "NEW_BENEFICIARY",
               "card_id": "NEW_CARD", "iban_cc": "UNUSUAL_BENEFICIARY_COUNTRY", "cc_asn": "UNUSUAL_CONNECTION_ORIGIN",
               "ip": "NEW_IP_ADDRESS", "hour": "UNUSUAL_TIME_OF_DAY", "operator": "UNUSUAL_OPERATOR"}
TEMPORAL_CODES = {"tx_count": "VELOCITY_ABOVE_PROFILE", "total_amount": "DAILY_AMOUNT_ABOVE_PROFILE",
                  "max_amount": "LARGEST_AMOUNT_ABOVE_PROFILE", "distinct_beneficiaries": "MANY_BENEFICIARIES",
                  "distinct_cc_asn": "MULTIPLE_CONNECTION_ORIGINS", "foreign_count": "FOREIGN_ACTIVITY_ABOVE_PROFILE"}
RULE_CODES = {"high_risk_country": "HIGH_RISK_COUNTRY", "night": "NIGHT_ACTIVITY", "max_amount": "AMOUNT_ABOVE_LIMIT",
              "local_alert": "LOCAL_PROFILE_ALERT", "temporal_alert": "TEMPORAL_PROFILE_ALERT",
              "global_alert": "PEER_PROFILE_ALERT"}
REASON_CODE_TEXT = {
    "UNUSUAL_AMOUNT": "Amount unusual for this customer",
    "NEW_BENEFICIARY": "Beneficiary not used before by this customer",
    "RARE_BENEFICIARY": "Beneficiary rarely used by this customer",
    "NEW_CARD": "Card not used before by this customer",
    "UNUSUAL_BENEFICIARY_COUNTRY": "Unusual beneficiary country",
    "UNUSUAL_CONNECTION_ORIGIN": "Connection from an unusual country/network",
    "NEW_IP_ADDRESS": "New IP address",
    "UNUSUAL_TIME_OF_DAY": "Unusual time of day",
    "UNUSUAL_OPERATOR": "Unusual phone operator",
    "VELOCITY_ABOVE_PROFILE": "More operations than usual in 24h",
    "DAILY_AMOUNT_ABOVE_PROFILE": "24h total above the customer's profile",
    "LARGEST_AMOUNT_ABOVE_PROFILE": "Largest single amount above profile",
    "MANY_BENEFICIARIES": "Many distinct beneficiaries in 24h",
    "MULTIPLE_CONNECTION_ORIGINS": "Several connection origins in 24h",
    "FOREIGN_ACTIVITY_ABOVE_PROFILE": "Foreign activity above profile",
    "PEER_GROUP_OUTLIER": "Spending differs from peer group",
    "HIGH_RISK_COUNTRY": "Involves a high-risk country",
    "NIGHT_ACTIVITY": "Night-time activity",
    "AMOUNT_ABOVE_LIMIT": "Amount above the institution limit",
    "LOCAL_PROFILE_ALERT": "Customer-profile score above alert level",
    "TEMPORAL_PROFILE_ALERT": "Activity-window score above alert level",
    "PEER_PROFILE_ALERT": "Peer-group score above alert level",
    "MODEL_NOT_READY": "No trained model available",
    "DECISION_SERVICE_ERROR": "Decision service error",
}
EXECUTION_TRANSITIONS = {
    "RECEIVED": {"PENDING", "HELD", "AUTHORISED", "DECLINED", "SETTLED"},
    "PENDING": {"HELD", "AUTHORISED", "DECLINED", "EXPIRED"},
    "HELD": {"AUTHORISED", "DECLINED", "EXPIRED", "RELEASE_REQUESTED", "DECLINE_REQUESTED"},
    "RELEASE_REQUESTED": {"AUTHORISED", "DECLINED", "HELD", "EXPIRED"},
    "DECLINE_REQUESTED": {"DECLINED", "AUTHORISED", "HELD", "EXPIRED"},
    "AUTHORISED": {"SETTLED", "RECALLED"},
    "SETTLED": {"RECALLED"},
    "DECLINED": set(),
    "EXPIRED": set(),
    "RECALLED": set(),
}
BANK_STATES = ("PENDING", "HELD", "AUTHORISED", "DECLINED", "SETTLED", "RECALLED", "EXPIRED")


class DecisionError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: Optional[dict] = None):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}

    def body(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


# --------------------------------------------------------------------------- request mapping
def _normalise_ts(value: Any) -> datetime:
    if value in (None, ""):
        return utcnow()
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo:
                return parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except ValueError:
            pass
    return parse_timestamp(value)


def map_request(payload: dict, tenant_id: str) -> dict:
    """Canonical VeriShield request -> BankSealer raw record (the preparer also accepts aliases)."""
    p = {k: v for k, v in payload.items() if v not in (None, "")}
    tx_type = str(p.get("transaction_type") or p.get("tx_type") or "").strip().lower()
    beneficiary = p.get("beneficiary_reference")
    record = dict(p)
    record.update({
        "tx_id": p.get("transaction_id") or p.get("tx_id"),
        "user_id": p.get("customer_id") or p.get("user_id"),
        "timestamp": _normalise_ts(p.get("timestamp")),
        "tx_type": tx_type,
        "amount": p.get("amount"),
        "currency": p.get("currency"),
        "bank_id": tenant_id,
        "iban_cc": p.get("beneficiary_country") or p.get("iban_cc"),
        "ip": p.get("ip_address") or p.get("ip"),
        "session_id": p.get("session_reference") or p.get("session_id"),
    })
    if p.get("ip_country") and not p.get("cc_asn"):
        asn = str(p.get("asn") or "AS0")
        record["cc_asn"] = f"{str(p['ip_country']).upper()}-{asn if asn.upper().startswith('AS') else 'AS' + asn}"
    if beneficiary:
        if tx_type in ("phone_recharge", "recharge", "topup", "mobile_topup"):
            record.setdefault("phone_number", beneficiary)
        elif tx_type in ("prepaid_card", "prepaid", "card_topup"):
            record.setdefault("card_id", beneficiary)
        else:
            record.setdefault("iban", beneficiary)
    for k in ("transaction_id", "customer_id", "transaction_type", "beneficiary_reference", "beneficiary_country",
              "ip_address", "session_reference", "label", "is_fraud", "fraud"):
        record.pop(k, None)
    if "is_fraud" in payload or "label" in payload:     # synthetic ground truth (demo only)
        record["label"] = payload.get("is_fraud", payload.get("label"))
    return record


def reason_codes(result, rule_hits: list[dict], aml: dict, max_reasons: int = 6) -> list[str]:
    codes: list[str] = []

    def add(code: str) -> None:
        if code and code not in codes:
            codes.append(code)

    for r in result.rule_reasons:
        add(RULE_CODES.get(r.feature, "BANK_RULE"))
    profile = sorted(result.reasons, key=lambda r: r.contribution, reverse=True)
    for r in profile[:max_reasons]:
        if r.contribution < 0.01 and result.risk_level == "LOW":
            continue
        if r.profile == "local":
            code = LOCAL_CODES.get(r.feature, "UNUSUAL_BEHAVIOUR")
            if code == "NEW_BENEFICIARY" and "rarely" in r.message:
                code = "RARE_BENEFICIARY"
            add(code)
        elif r.profile == "temporal":
            add(TEMPORAL_CODES.get(r.feature, "UNUSUAL_ACTIVITY_WINDOW"))
        elif r.profile == "global":
            add("PEER_GROUP_OUTLIER")
    for hit in rule_hits:
        add(hit.get("reason_code") or hit["id"])
    for ind in aml.get("indicators", []):
        add(ind["code"])
    return codes


# --------------------------------------------------------------------------- helpers
def _customer(db: Session, tenant_id: str, space: str, ref: str) -> Customer:
    cust = db.execute(select(Customer).where(Customer.tenant_id == tenant_id, Customer.space == space,
                                             Customer.customer_ref == ref)).scalar()
    if cust is None:
        cust = Customer(tenant_id=tenant_id, space=space, customer_ref=ref, risk_category="UNKNOWN",
                        kyc_review_status="NOT_PROVIDED", attributes={"source": "auto-created from transaction"},
                        segment_basis="No KYC record - first seen in a transaction")
        db.add(cust)
        db.flush()
    return cust


def open_incident(db: Session, tenant_id: str, category: str, title: str, severity: str = "SEV2",
                  affected: str = "") -> SystemIncident:
    inc = db.execute(select(SystemIncident).where(SystemIncident.tenant_id == tenant_id,
                                                  SystemIncident.category == category,
                                                  SystemIncident.status.notin_(("RESOLVED", "POSTMORTEM")))).scalar()
    now = utcnow()
    if inc:
        inc.occurrences += 1
        inc.last_seen_at = now
        return inc
    inc = SystemIncident(tenant_id=tenant_id, category=category, title=title, severity=severity, affected=affected,
                         timeline=[{"at": now.isoformat(timespec="seconds"), "status": "DETECTED", "note": title}])
    db.add(inc)
    db.flush()
    metrics.error(category, title)
    return inc


def _execution(db: Session, tx: Transaction, state: str, source: str, detail: str = "", actor: str = "",
               reference: str = "") -> None:
    tx.execution_state = state
    db.add(ExecutionStatusEvent(tenant_id=tx.tenant_id, space=tx.space, tx_ref=tx.tx_ref, state=state,
                                source=source, detail=detail, actor=actor, reference=reference))


def _initial_execution(db: Session, tx: Transaction, dec: Decision, demo: bool, fallback: str = "") -> None:
    if dec.mode == "POST_EVENT":
        _execution(db, tx, "SETTLED", "SOURCE", "Received after execution (post-event monitoring); "
                                                "cannot be blocked retroactively")
        return
    _execution(db, tx, "PENDING", "VERISHIELD", f"Decision {dec.decision} returned to the bank")
    if not demo:
        return       # the bank reports the real execution result through /execution-result
    target = {"PASS": "AUTHORISED", "REVIEW": "AUTHORISED", "HOLD": "HELD", "DECLINE": "DECLINED"}.get(dec.decision)
    if dec.decision == "ERROR_POLICY":
        target = {"FAIL_OPEN": "AUTHORISED", "FAIL_CLOSED": "PENDING", "FALLBACK_HOLD": "HELD"}.get(fallback, "PENDING")
    if target and target != "PENDING":
        _execution(db, tx, target, "DEMO_BANK_SIMULATOR", "Simulated bank acknowledgement (Demo Mode)")


def _alert(db: Session, tx: Transaction, dec: Decision, explanation: str) -> Optional[Alert]:
    if dec.decision == "PASS" and dec.aml_signal_level not in ("ELEVATED", "HIGH"):
        return None
    sev_by_outcome = {"REVIEW": "MEDIUM", "HOLD": "HIGH", "DECLINE": "CRITICAL", "ERROR_POLICY": "MEDIUM",
                      "PASS": "MEDIUM"}
    order = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    severity = max(sev_by_outcome[dec.decision], dec.risk_level if dec.risk_level in order else "LOW",
                   key=order.index)
    related = db.execute(select(Alert.id).where(
        Alert.tenant_id == tx.tenant_id, Alert.space == tx.space, Alert.customer_ref == tx.customer_ref,
        Alert.status.in_(("OPEN", "TRIAGED")), Alert.created_at >= utcnow() - timedelta(hours=24))
        .order_by(Alert.created_at).limit(1)).scalar()
    summary_codes = ", ".join(REASON_CODE_TEXT.get(c, c.replace("_", " ").title()) for c in dec.reason_codes[:3])
    alert = Alert(id=space_ref(db, tx.tenant_id, tx.space, "ALR"), tenant_id=tx.tenant_id, space=tx.space,
                  kind="TRANSACTION", tx_ref=tx.tx_ref, customer_ref=tx.customer_ref, decision_id=dec.id,
                  decision=dec.decision, timing="POST_EVENT" if dec.mode == "POST_EVENT" else "PRE_EVENT",
                  severity=severity, score=dec.fraud_score, aml_signal_level=dec.aml_signal_level,
                  reason_codes=dec.reason_codes, related_alert_id=related,
                  summary=f"{tx.tx_type.replace('_', ' ')} {tx.amount:,.2f} {tx.currency}: "
                          f"{summary_codes or explanation[:120] or dec.decision}",
                  policy_version=dec.policy_version, model_version=dec.fraud_model_version)
    db.add(alert)
    if severity == "CRITICAL":
        _notify_critical(db, tx, alert)
    return alert


def _notify_critical(db: Session, tx: Transaction, alert: Alert) -> None:
    from . import notify
    tenant = db.get(Tenant, tx.tenant_id)
    if not notify.event_enabled(tenant, "critical_alert") or not (notify.email_ready(tenant) or
                                                                  notify.sms_ready(tenant)):
        return
    for u in db.execute(select(User).where(User.tenant_id == tx.tenant_id, User.role.in_(("AML_MANAGER", "CHIEF_AML")),
                                           User.status == "ACTIVE")).scalars():
        notify.enqueue(tenant, "critical_alert", email=u.email, phone=u.phone,
                       subject=f"CRITICAL alert {alert.id}" + (" (Demo workspace)" if tx.space == "DEMO" else ""),
                       text=f"{alert.summary}. Customer {tx.customer_ref}, decision {alert.decision}.")


def response(dec: Decision, tx: Optional[Transaction] = None) -> dict:
    """Public decision contract (BRD 15.4)."""
    out = {
        "transaction_id": dec.tx_ref, "decision_id": dec.id, "decision": dec.decision, "mode": dec.mode,
        "execution_required": dec.execution_required, "fraud_score": round(dec.fraud_score, 4),
        "risk_level": dec.risk_level, "aml_signal_level": dec.aml_signal_level,
        "customer_risk_context": dec.customer_risk_context, "reason_codes": dec.reason_codes,
        "policy_version": dec.policy_version, "fraud_model_version": dec.fraud_model_version,
        "issued_at": dec.issued_at.isoformat(timespec="seconds") + "Z",
        "expires_at": dec.expires_at.isoformat(timespec="seconds") + "Z" if dec.expires_at else None,
        "correlation_id": dec.correlation_id,
    }
    if dec.decision == "ERROR_POLICY":
        out["fallback"] = (dec.shadow or {}).get("fallback")
        out["error"] = dec.error
    if dec.mode == "POST_EVENT":
        out["note"] = "Post-event monitoring: the transaction was already executed and was not blocked."
    if tx is not None:
        out["execution_state"] = tx.execution_state
    return out


# --------------------------------------------------------------------------- evaluation
DETAIL_FIELDS = ("beneficiary_name", "purpose", "narrative", "city", "branch", "merchant", "device", "gateway",
                 "terminal_id")


def _enrich(tx_row: Transaction, payload: dict) -> None:
    """Keep descriptive payment-gateway fields (beneficiary name, purpose, city, device...) with the record."""
    extra = {k: str(payload[k])[:120] for k in DETAIL_FIELDS if payload.get(k) not in (None, "")}
    if extra:
        tx_row.details = {**(tx_row.details or {}), **extra}
    dev = payload.get("device_reference") or payload.get("device")
    if dev and not tx_row.device_ref:
        tx_row.device_ref = str(dev)[:64]


def evaluate(db: Session, tenant_id: str, space: str, payload: dict, mode: str = "PRE_AUTH",
             source: str = "decision-api", caller: str = "", field_mapping: Optional[dict] = None,
             commit: bool = True) -> tuple[Decision, Transaction, bool]:
    """Score one transaction; returns ``(decision, transaction, created)``.

    Idempotent: the same ``idempotency_key`` (default: transaction id) returns the stored decision.
    """
    started = time.perf_counter()
    tenant = db.get(Tenant, tenant_id)
    record = map_request(payload, tenant_id)
    tx_ref = str(record.get("tx_id") or "").strip()
    idem = str(payload.get("idempotency_key") or tx_ref or "").strip()
    if idem:
        existing = db.execute(select(Decision).where(Decision.tenant_id == tenant_id, Decision.space == space,
                                                     Decision.idempotency_key == idem)).scalar()
        if existing:
            tx = db.execute(select(Transaction).where(Transaction.tenant_id == tenant_id,
                                                      Transaction.space == space,
                                                      Transaction.tx_ref == existing.tx_ref)).scalar()
            metrics.inc("decisions_idempotent_replay")
            return existing, tx, False

    prep = TransactionPreparer(field_mapping or {}, default_bank=tenant_id)
    etx: Optional[ETx] = prep.prepare(record, source)
    if etx is None:
        reason = prep.errors[-1] if prep.errors else "invalid transaction"
        metrics.inc("decisions_rejected")
        raise DecisionError(422, "INVALID_TRANSACTION", reason.split("] ", 1)[-1])
    if not etx.currency or etx.currency == "EUR" and not payload.get("currency"):
        etx.currency = tenant.base_currency
    if db.execute(select(Transaction.id).where(Transaction.tenant_id == tenant_id, Transaction.space == space,
                                               Transaction.tx_ref == etx.tx_id)).scalar():
        raise DecisionError(409, "DUPLICATE_TRANSACTION",
                            f"transaction {etx.tx_id} was already evaluated under a different idempotency key")
    idem = idem or etx.tx_id

    policy = active_policy(db, tenant_id)
    doc = policy.document
    ws = workspace(tenant_id, space)
    ws.apply_policy(policy.id, doc.get("engine_thresholds", {}), tenant.display_name)
    customer = _customer(db, tenant_id, space, etx.user_id)
    ctx = aml_service.TxContext(db, tenant_id, space, etx)
    correlation = "CORR-" + uuid.uuid4().hex[:10].upper()
    now = utcnow()

    tx_row = Transaction(tenant_id=tenant_id, space=space, tx_ref=etx.tx_id, mode=mode, source=source,
                         channel=str(payload.get("channel") or etx.channel or ""),
                         account_ref=str(payload.get("account_reference") or ""),
                         device_ref=payload.get("device_reference"), **engine_tx_fields(etx))
    tx_row.channel = str(payload.get("channel") or "")
    _enrich(tx_row, payload)
    dec = Decision(id=space_ref(db, tenant_id, space, "DEC", 7), tenant_id=tenant_id, space=space,
                   tx_ref=etx.tx_id, customer_ref=etx.user_id, idempotency_key=idem, mode=mode,
                   policy_version=policy.id, correlation_id=correlation, issued_at=now, caller=caller,
                   customer_risk_context=customer.risk_category or "UNKNOWN")
    explanation = ""
    fallback = ""
    try:
        if not ws.ready:
            raise RuntimeError("MODEL_NOT_READY")
        with ws.lock:
            result = ws.engine.score(etx)
            shadow = None
            if ws.shadow is not None:
                try:
                    s = ws.shadow.score(etx, learn=False)
                    shadow = {"model": ws.shadow_id, "fraud_score": s.combined_score, "risk_level": s.risk_level}
                except Exception as exc:  # noqa: BLE001 - shadow never affects the decision
                    shadow = {"model": ws.shadow_id, "error": str(exc)[:200]}
            ws.scored_since_save += 1
            ranked = ws.engine.ranked_reasons(result)
            max_reasons = int(ws.engine.config.section("engine").get("max_reasons", 6))
            model_id = ws.model_id
        aml = aml_service.evaluate(ctx, customer, doc.get("aml", {}),
                                   doc.get("engine_thresholds", {}).get("high_risk_countries", []))
        rule_ctx = {"tx_type": etx.tx_type, "channel": tx_row.channel, "mode": mode, "amount": etx.amount,
                    "segment_key": customer.segment_key, "customer_type": customer.customer_type,
                    "customer_risk": customer.risk_category, "beneficiary_country": etx.iban_cc,
                    "ip_country": etx.ip_country, "hour": etx.timestamp.hour,
                    "new_beneficiary": ctx.new_beneficiary(), "velocity": ctx.velocity}
        hits = [{"id": r["id"], "name": r.get("name", r["id"]), "action": r["action"], "kind": "policy_rule",
                 "reason_code": r.get("reason_code") or r["id"]}
                for r in doc.get("rules", []) if rule_matches(r, rule_ctx)]
        hard = any(r.feature in ("max_amount", "local_alert", "temporal_alert", "global_alert")
                   for r in result.rule_reasons)
        outcome, drivers = combine(doc, result.risk_level, hits, aml["level"], result.undertrained, hard)
        dec.decision = outcome
        dec.fraud_score = result.combined_score
        dec.local_score, dec.temporal_score, dec.global_score = (result.local_score, result.temporal_score,
                                                                 result.global_score)
        dec.risk_level = result.risk_level
        dec.aml_signal_level, dec.aml_score, dec.aml_indicators = aml["level"], aml["score"], aml["indicators"]
        dec.reasons = [r.to_dict() for r in ranked]
        dec.rule_hits = hits + [{"id": r.feature, "kind": "engine_rule", "message": r.message}
                                for r in result.rule_reasons]
        dec.reason_codes = reason_codes(result, hits, aml, max_reasons)
        dec.undertrained = result.undertrained
        dec.peer_group = result.cluster
        dec.fraud_model_version = model_id or ""
        dec.shadow = {"model_shadow": shadow, "drivers": drivers} if shadow else {"drivers": drivers}
        explanation = result.explanation
        if aml["indicators"]:
            explanation = (explanation + "\nAML indicators:\n" if explanation else "AML indicators:\n") + \
                "\n".join(f"- {i['message']}" for i in aml["indicators"])
        if drivers:
            explanation = (explanation + "\n" if explanation else "") + "Decision drivers: " + "; ".join(drivers)
        dec.explanation = explanation
    except Exception as exc:  # noqa: BLE001 - defined error state, never a silent fallback
        code = "MODEL_NOT_READY" if str(exc) == "MODEL_NOT_READY" else "DECISION_SERVICE_ERROR"
        fb = doc.get("fallback", {})
        fallback = fb.get(etx.tx_type) or fb.get("default", "FAIL_OPEN")
        dec.decision = "ERROR_POLICY"
        dec.reason_codes = [code]
        dec.error = code if code == "MODEL_NOT_READY" else f"{type(exc).__name__}: {str(exc)[:300]}"
        dec.shadow = {"fallback": fallback}
        dec.explanation = f"No reliable decision could be produced ({code}). Institution fallback: {fallback}."
        open_incident(db, tenant_id, f"decision:{code}", f"Decision service returned ERROR_POLICY ({code})",
                      "SEV2", affected=f"{space} decisions")
        log.warning("decision error for %s: %s", etx.tx_id, exc)

    dec.execution_required = mode == "PRE_AUTH" and dec.decision in ("HOLD", "DECLINE") or \
        (dec.decision == "ERROR_POLICY" and fallback in ("FAIL_CLOSED", "FALLBACK_HOLD"))
    ttl = int(doc.get("decision_ttl_minutes", 15))
    dec.expires_at = now + timedelta(minutes=ttl) if mode == "PRE_AUTH" else None
    dec.latency_ms = round((time.perf_counter() - started) * 1000, 2)
    tx_row.decision_id = dec.id
    db.add(tx_row)
    db.add(dec)
    db.flush()
    _initial_execution(db, tx_row, dec, space == "DEMO", fallback)
    _alert(db, tx_row, dec, explanation)
    if commit:
        db.commit()
    metrics.inc(f"decisions_total:{dec.decision}")
    metrics.observe("decision", dec.latency_ms)
    return dec, tx_row, True


def ingest_events(db: Session, tenant_id: str, space: str, records: list[dict], source: str,
                  mode: str = "POST_EVENT", connector_id: Optional[str] = None,
                  field_mapping: Optional[dict] = None, caller: str = "") -> dict:
    """Asynchronous / batch ingestion. Rejected and duplicate records go to the dead-letter store."""
    stats = {"received": 0, "accepted": 0, "rejected": 0, "duplicates": 0, "decisions": {}}
    pending = 0
    for raw in records:
        stats["received"] += 1
        try:
            if mode == "HISTORY":
                created = store_history(db, tenant_id, space, raw, source, field_mapping)
                stats["accepted" if created else "duplicates"] += 1
                pending += 1
                if pending >= 500:
                    db.commit()
                    pending = 0
                continue
            dec, _, created = evaluate(db, tenant_id, space, raw, mode=mode, source=source, caller=caller,
                                       field_mapping=field_mapping, commit=True)
            if created:
                stats["accepted"] += 1
                stats["decisions"][dec.decision] = stats["decisions"].get(dec.decision, 0) + 1
            else:
                stats["duplicates"] += 1
                db.add(DeadLetterEvent(tenant_id=tenant_id, space=space, source=source, connector_id=connector_id,
                                       payload=_jsonable(raw), reason="duplicate transaction (idempotent replay)",
                                       category="DUPLICATE", status="DISCARDED"))
        except DecisionError as exc:
            if exc.code == "DUPLICATE_TRANSACTION":
                stats["duplicates"] += 1
                category, status = "DUPLICATE", "DISCARDED"
            else:
                stats["rejected"] += 1
                category, status = "VALIDATION", "PENDING"
            db.add(DeadLetterEvent(tenant_id=tenant_id, space=space, source=source, connector_id=connector_id,
                                   payload=_jsonable(raw), reason=exc.message, category=category, status=status))
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            pending = 0
            stats["rejected"] += 1
            db.add(DeadLetterEvent(tenant_id=tenant_id, space=space, source=source, connector_id=connector_id,
                                   payload=_jsonable(raw), reason=f"processing error: {exc}"[:500],
                                   category="PROCESSING"))
    db.commit()
    metrics.inc("events_received", stats["received"])
    metrics.inc("events_rejected", stats["rejected"])
    return stats


def _jsonable(raw: Any) -> dict:
    if isinstance(raw, dict):
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in raw.items()}
    return {"value": str(raw)}


def store_history(db: Session, tenant_id: str, space: str, raw: dict, source: str,
                  field_mapping: Optional[dict] = None) -> bool:
    """Store an approved historical transaction for model training (no decision)."""
    tenant = db.get(Tenant, tenant_id)
    prep = TransactionPreparer(field_mapping or {}, default_bank=tenant_id)
    etx = prep.prepare(map_request(raw, tenant_id), source)
    if etx is None:
        raise DecisionError(422, "INVALID_TRANSACTION", (prep.errors[-1] if prep.errors else "invalid")
                            .split("] ", 1)[-1])
    if db.execute(select(Transaction.id).where(Transaction.tenant_id == tenant_id, Transaction.space == space,
                                               Transaction.tx_ref == etx.tx_id)).scalar():
        return False
    if not raw.get("currency"):
        etx.currency = tenant.base_currency
    _customer(db, tenant_id, space, etx.user_id)
    row = Transaction(tenant_id=tenant_id, space=space, tx_ref=etx.tx_id, mode="HISTORY", source=source,
                      channel=str(raw.get("channel") or ""), execution_state="SETTLED", **engine_tx_fields(etx))
    _enrich(row, raw)
    db.add(row)
    db.flush()
    return True


# --------------------------------------------------------------------------- execution lifecycle
def get_transaction(db: Session, tenant_id: str, space: str, tx_ref: str) -> Transaction:
    tx = db.execute(select(Transaction).where(Transaction.tenant_id == tenant_id, Transaction.space == space,
                                              Transaction.tx_ref == tx_ref)).scalar()
    if tx is None:
        raise DecisionError(404, "NOT_FOUND", f"transaction {tx_ref} not found")
    return tx


def report_execution(db: Session, tenant_id: str, space: str, tx_ref: str, state: str, reference: str = "",
                     detail: str = "", actor: str = "bank") -> Transaction:
    """The bank reports the real execution status (hold acknowledged, released, declined, settled...)."""
    state = state.upper()
    if state not in BANK_STATES:
        raise DecisionError(422, "INVALID_STATE", f"state must be one of {', '.join(BANK_STATES)}")
    tx = get_transaction(db, tenant_id, space, tx_ref)
    allowed = EXECUTION_TRANSITIONS.get(tx.execution_state, set())
    if state != tx.execution_state and state not in allowed:
        raise DecisionError(409, "INVALID_TRANSITION",
                            f"cannot move execution state from {tx.execution_state} to {state}",
                            {"allowed": sorted(allowed)})
    _execution(db, tx, state, "BANK", detail, actor, reference)
    db.commit()
    metrics.inc(f"execution_results:{state}")
    return tx


def request_bank_action(db: Session, tenant_id: str, space: str, tx_ref: str, action: str, actor: Any,
                        rationale: str) -> dict:
    """Manager release/reject of a pending or held payment, sent through the bank's authorised adapter."""
    from . import connectors

    tx = get_transaction(db, tenant_id, space, tx_ref)
    if tx.mode != "PRE_AUTH":
        return {"sent": False, "state": tx.execution_state,
                "message": "Post-event transaction: it was already executed. Use the bank's recall/recovery "
                           "process; VeriShield cannot block it retroactively."}
    if tx.execution_state not in ("PENDING", "HELD", "RELEASE_REQUESTED", "DECLINE_REQUESTED"):
        return {"sent": False, "state": tx.execution_state,
                "message": f"Payment is {tx.execution_state}; no release/reject command is applicable."}
    command = "RELEASE" if action == "RELEASE" else "REJECT"
    actor_name = getattr(actor, "display", str(actor))
    if space == "DEMO":
        final = "AUTHORISED" if command == "RELEASE" else "DECLINED"
        _execution(db, tx, final, "DEMO_BANK_SIMULATOR",
                   f"{command} by {actor_name} (simulated - Demo Mode never executes real payments)", actor_name)
        db.commit()
        return {"sent": True, "simulated": True, "state": final, "message": f"Demo bank simulator applied {command}"}
    requested = "RELEASE_REQUESTED" if command == "RELEASE" else "DECLINE_REQUESTED"
    result = connectors.send_bank_command(db, tenant_id, {
        "command": command, "transaction_id": tx.tx_ref, "decision_id": tx.decision_id,
        "rationale": rationale, "requested_by": actor_name})
    _execution(db, tx, requested, "VERISHIELD", result["message"], actor_name)
    db.commit()
    return {"sent": result["sent"], "simulated": False, "state": requested, "message": result["message"]}


def expire_holds(db: Session) -> int:
    """Mark pre-authorisation holds past the policy expiry (the bank applies its own expiry policy)."""
    n = 0
    tenants = db.execute(select(Tenant)).scalars().all()
    for t in tenants:
        minutes = int(active_policy(db, t.id).document.get("hold_expiry_minutes", 240))
        cutoff = utcnow() - timedelta(minutes=minutes)
        rows = db.execute(select(Transaction).join(Decision, Decision.id == Transaction.decision_id).where(
            Transaction.tenant_id == t.id, Transaction.execution_state == "HELD", Transaction.mode == "PRE_AUTH",
            Decision.issued_at < cutoff)).scalars().all()
        for tx in rows:
            _execution(db, tx, "EXPIRED", "VERISHIELD", f"Hold exceeded policy expiry ({minutes} min)")
            n += 1
    if n:
        db.commit()
    return n


def explain_codes(codes: list[str]) -> list[dict]:
    return [{"code": c, "text": REASON_CODE_TEXT.get(c, c.replace("_", " ").capitalize())} for c in codes]


__all__ = ["evaluate", "ingest_events", "report_execution", "request_bank_action", "response", "DecisionError",
           "explain_codes", "PROFILE_NAMES", "store_history", "expire_holds", "SEVERITY", "audit"]
