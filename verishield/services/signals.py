"""Extra decision signals: beneficiary watch list, sanctions screening of the payment, device / login risk,
the supervised model trained on analyst outcomes - and the outcome labels that feed it.

``evaluate`` (decision.py) calls :func:`decision_signals` after the behavioural engine; each signal becomes a
``kind="signal"`` rule hit with an action from the active policy (``signals`` section), so it is explained,
versioned and replayed by the policy simulator like any other rule.
"""
from __future__ import annotations

import math
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ..models import LoginEvent, Transaction, WatchlistEntry, utcnow

DEFAULT_SIGNALS = {"watchlist_action": "HOLD", "sanctions_action": "HOLD", "sanctions_min_score": 0.88,
                   "device_risk_review": 0.6, "device_risk_hold": 0.85, "supervised_review": 0.7,
                   "supervised_hold": 0.9, "confirmation_sms": True, "confirmation_minutes": 30}
KINDS = ("IBAN", "PHONE", "CARD", "MERCHANT", "ORIGINATOR", "DEVICE", "IP", "NAME")


def cfg(doc: dict) -> dict:
    return {**DEFAULT_SIGNALS, **(doc.get("signals") or {})}


# --------------------------------------------------------------------------- watch list
def watch_values(etx, tx_row) -> dict[str, str]:
    """Identifiers of a payment that a watch list entry can match."""
    out = {}
    kind = {"bank_transfer": "IBAN", "phone_recharge": "PHONE", "prepaid_card": "CARD", "card_payment": "MERCHANT",
            "incoming_transfer": "ORIGINATOR"}.get(etx.tx_type)
    if kind and etx.beneficiary:
        out[kind] = str(etx.beneficiary).upper()
    if tx_row is not None and tx_row.device_ref and tx_row.device_ref not in ("Branch counter", "ATM / kiosk",
                                                                               "Card terminal", "Unknown device"):
        out["DEVICE"] = str(tx_row.device_ref).upper()
    if etx.ip:
        out["IP"] = str(etx.ip).upper()
    name = ((tx_row.details or {}).get("beneficiary_name") if tx_row is not None else None)
    if name:
        out["NAME"] = str(name).upper()
    return out


def watchlist_hits(db: Session, tenant_id: str, space: str, values: dict[str, str]) -> list[WatchlistEntry]:
    if not values:
        return []
    now = utcnow()
    rows = db.execute(select(WatchlistEntry).where(
        WatchlistEntry.tenant_id == tenant_id, WatchlistEntry.space == space, WatchlistEntry.active.is_(True),
        WatchlistEntry.value.in_(list(set(values.values()))),
        or_(WatchlistEntry.expires_at.is_(None), WatchlistEntry.expires_at > now))).scalars().all()
    hits = [r for r in rows if values.get(r.kind) == r.value]
    for r in hits:
        r.hits = (r.hits or 0) + 1
        r.last_hit_at = now
    return hits


def add_to_watchlist(db: Session, tenant_id: str, space: str, kind: str, value: str, reason: str, actor: str,
                     source: str = "MANUAL", source_ref: str = "", action: str = "HOLD",
                     days: Optional[int] = None) -> WatchlistEntry:
    kind = kind.upper()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    value = str(value or "").strip().upper().replace(" ", "") if kind != "NAME" else str(value or "").strip().upper()
    if not value:
        raise ValueError("value is required")
    existing = db.execute(select(WatchlistEntry).where(
        WatchlistEntry.tenant_id == tenant_id, WatchlistEntry.space == space, WatchlistEntry.kind == kind,
        WatchlistEntry.value == value)).scalar()
    if existing:
        existing.active = True
        existing.reason = (existing.reason + " | " if existing.reason else "") + reason[:300]
        return existing
    entry = WatchlistEntry(tenant_id=tenant_id, space=space, kind=kind, value=value[:200], reason=reason[:1000],
                           action=action if action in ("REVIEW", "HOLD", "DECLINE") else "HOLD", source=source,
                           source_ref=source_ref[:80], created_by=actor[:120],
                           expires_at=utcnow() + timedelta(days=int(days)) if days else None)
    db.add(entry)
    db.flush()
    return entry


def watch_transaction(db: Session, tx: Transaction, reason: str, actor: str, source: str, source_ref: str) -> list:
    """Put the counterparty (and device) of a confirmed-fraud transaction on the watch list."""
    out = []
    kind = {"bank_transfer": "IBAN", "phone_recharge": "PHONE", "prepaid_card": "CARD", "card_payment": "MERCHANT",
            "incoming_transfer": "ORIGINATOR"}.get(tx.tx_type)
    if kind and tx.beneficiary:
        out.append(add_to_watchlist(db, tx.tenant_id, tx.space, kind, tx.beneficiary, reason, actor, source,
                                    source_ref))
    return out


# --------------------------------------------------------------------------- device / login risk
def login_risk(db: Session, tenant_id: str, space: str, customer_ref: str, ts, device: str, ip: str,
               country: str, exclude_id: Optional[int] = None) -> tuple[float, list[str]]:
    """Account-takeover risk of a session (0-1) and the factors behind it."""
    since = ts - timedelta(days=90)
    q = select(LoginEvent).where(LoginEvent.tenant_id == tenant_id, LoginEvent.space == space,
                                 LoginEvent.customer_ref == customer_ref, LoginEvent.ts >= since, LoginEvent.ts <= ts)
    if exclude_id:
        q = q.where(LoginEvent.id != exclude_id)
    past = db.execute(q.order_by(LoginEvent.ts.desc()).limit(500)).scalars().all()
    tx_devices = set()
    if device:
        tx_devices = set(db.execute(select(Transaction.device_ref).where(
            Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.customer_ref == customer_ref,
            Transaction.ts >= since, Transaction.ts < ts, Transaction.device_ref.is_not(None))
            .distinct().limit(50)).scalars().all())
    known_devices = {e.device_ref for e in past if e.success and e.device_ref} | tx_devices
    known_countries = {e.country for e in past if e.success and e.country}
    if device:
        known_countries |= {str(c).split("-")[0] for c in db.execute(select(Transaction.cc_asn).where(
            Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.customer_ref == customer_ref,
            Transaction.ts >= since, Transaction.ts < ts, Transaction.cc_asn.is_not(None)).distinct().limit(20))
            .scalars() if c and "-" in str(c)}
    factors, score = [], 0.0
    history = len(past) + len(tx_devices)
    if device and history and device not in known_devices:
        factors.append("new device never used by this customer")
        score += 0.35
    if country and known_countries and country not in known_countries:
        factors.append(f"first session from country {country}")
        score += 0.35
    last = next((e for e in past if e.success and e.country), None)
    if last and country and last.country != country and (ts - last.ts) < timedelta(hours=3):
        factors.append(f"impossible travel: {last.country} -> {country} within "
                       f"{max(1, int((ts - last.ts).total_seconds() // 60))} min")
        score += 0.4
    failed = sum(1 for e in past if not e.success and e.ts >= ts - timedelta(hours=1))
    if failed >= 3:
        factors.append(f"{failed} failed sign-ins in the last hour")
        score += 0.25
    reset = any(e.event in ("PASSWORD_RESET", "PHONE_CHANGE", "DEVICE_ENROL") and e.ts >= ts - timedelta(hours=24)
                for e in past)
    if reset:
        factors.append("password / phone / device changed in the last 24 hours")
        score += 0.25
    return round(min(score, 1.0), 2), factors


def record_login(db: Session, tenant_id: str, space: str, data: dict) -> LoginEvent:
    from .decision import _normalise_ts
    ref = str(data.get("customer_id") or data.get("customer_ref") or "").strip()
    if not ref:
        raise ValueError("customer_id is required")
    ts = _normalise_ts(data.get("timestamp")) if data.get("timestamp") else utcnow()
    ev = LoginEvent(tenant_id=tenant_id, space=space, customer_ref=ref, ts=ts,
                    event=str(data.get("event") or "LOGIN").upper()[:24], channel=str(data.get("channel") or "")[:40],
                    device_ref=str(data.get("device_id") or data.get("device") or "")[:120],
                    ip=str(data.get("ip_address") or data.get("ip") or "")[:64],
                    country=str(data.get("ip_country") or data.get("country") or "").upper()[:2],
                    success=bool(data.get("success", True)))
    ev.risk_score, ev.risk_factors = login_risk(db, tenant_id, space, ref, ts, ev.device_ref, ev.ip, ev.country)
    db.add(ev)
    db.flush()
    return ev


def session_risk(db: Session, tenant_id: str, space: str, etx, tx_row) -> tuple[float, list[str]]:
    """Risk of the session a payment comes from: the latest login in the last 30 minutes, else the payment's own
    device / IP compared with the customer's history."""
    recent = db.execute(select(LoginEvent).where(
        LoginEvent.tenant_id == tenant_id, LoginEvent.space == space, LoginEvent.customer_ref == etx.user_id,
        LoginEvent.ts <= etx.timestamp, LoginEvent.ts >= etx.timestamp - timedelta(minutes=30),
        LoginEvent.success.is_(True)).order_by(LoginEvent.ts.desc()).limit(1)).scalar()
    if recent is not None:
        return recent.risk_score, list(recent.risk_factors or [])
    device = (tx_row.device_ref or "") if tx_row is not None else ""
    if not device or device in ("Branch counter", "ATM / kiosk", "Card terminal"):
        return 0.0, []
    return login_risk(db, tenant_id, space, etx.user_id, etx.timestamp, device, etx.ip or "", etx.ip_country or "")


# --------------------------------------------------------------------------- all signals for one decision
def decision_signals(db: Session, tenant, space: str, etx, tx_row, doc: dict, result: Any,
                     aml_score: float = 0.0) -> dict:
    """Return ``{"hits": [...], "messages": [...], "supervised": {...}|None, "device": {...}}``."""
    from . import screening, supervised
    c = cfg(doc)
    hits, messages = [], []
    for w in watchlist_hits(db, tenant.id, space, watch_values(etx, tx_row)):
        hits.append({"id": f"WATCHLIST-{w.kind}", "name": f"Watch-listed {w.kind.lower()}", "kind": "signal",
                     "action": w.action or c["watchlist_action"], "reason_code": "WATCHLISTED_COUNTERPARTY",
                     "message": f"{w.kind.title()} {w.value} is on the watch list ({w.reason[:120]})"})
    name = (tx_row.details or {}).get("beneficiary_name") if tx_row is not None else None
    if etx.tx_type != "cash_withdrawal" and (name or etx.beneficiary):
        matches = screening.screen_payment(db, tenant.id, space, etx.user_id, etx.tx_id, name or "",
                                           etx.beneficiary, "ORIGINATOR" if etx.tx_type == "incoming_transfer"
                                           else "BENEFICIARY", float(c["sanctions_min_score"]))
        for m in matches[:1]:
            code = getattr(m, "list_code", None) or m["list_code"]
            matched = getattr(m, "matched_name", None) or m["name"]
            score = getattr(m, "score", None) or m["score"]
            hits.append({"id": "SANCTIONS-MATCH", "name": "Sanctions / PEP screening", "kind": "signal",
                         "action": c["sanctions_action"] if code != "PEP" else "REVIEW",
                         "reason_code": "SANCTIONS_MATCH" if code != "PEP" else "PEP_MATCH",
                         "message": f"Counterparty matches {code} list entry '{matched}' ({score:.2f})"})
    risk, factors = session_risk(db, tenant.id, space, etx, tx_row)
    if risk >= float(c["device_risk_review"]) and etx.is_outflow:
        hits.append({"id": "DEVICE-RISK", "name": "Session / device risk", "kind": "signal",
                     "action": "HOLD" if risk >= float(c["device_risk_hold"]) else "REVIEW",
                     "reason_code": "ACCOUNT_TAKEOVER_RISK",
                     "message": f"Session risk {risk:.2f}: " + "; ".join(factors)})
    elif factors:
        messages.append(f"Session signals ({risk:.2f}): " + "; ".join(factors))
    sup = supervised.predict(db, tenant.id, space, etx, result, aml_score) if result is not None else None
    if sup is not None:
        p = sup["probability"]
        if p >= float(c["supervised_review"]):
            hits.append({"id": "LEARNED-MODEL", "name": "Model trained on analyst outcomes", "kind": "signal",
                         "action": "HOLD" if p >= float(c["supervised_hold"]) else "REVIEW",
                         "reason_code": "LEARNED_FRAUD_PATTERN",
                         "message": f"Model {sup['model']} trained on confirmed outcomes: fraud probability {p:.2f}"})
    return {"hits": hits, "messages": messages, "supervised": sup, "device": {"risk": risk, "factors": factors}}


# --------------------------------------------------------------------------- outcome labels
def label_transactions(db: Session, tenant_id: str, space: str, tx_refs: list[str], label: int, source: str) -> int:
    """Record a confirmed outcome (1 fraud / 0 legitimate) - the training signal of the supervised model."""
    if not tx_refs:
        return 0
    res = db.execute(update(Transaction).where(Transaction.tenant_id == tenant_id, Transaction.space == space,
                                               Transaction.tx_ref.in_(tx_refs)).values(label=int(label)))
    return res.rowcount or 0


def labelled_counts(db: Session, tenant_id: str, space: str) -> dict:
    rows = dict(db.execute(select(Transaction.label, func.count()).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.label.is_not(None),
        Transaction.mode != "HISTORY").group_by(Transaction.label)).all())
    return {"fraud": int(rows.get(1, 0)), "legitimate": int(rows.get(0, 0))}


__all__ = ["decision_signals", "add_to_watchlist", "watch_transaction", "record_login", "label_transactions",
           "login_risk", "cfg", "math"]
