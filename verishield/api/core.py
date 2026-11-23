"""Decision, ingestion, transaction, customer, alert and dashboard endpoints."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import Principal, require
from ..models import (Alert, Case, CaseEntityLink, Connector, Customer, DeadLetterEvent, Decision,
                      ExecutionStatusEvent, ModelVersion, PolicyVersion, SystemIncident, Transaction, User, utcnow)
from ..security import mask, mask_ip
from ..services import cases as case_service
from ..services import decision as decision_service
from ..services import engine_registry, segmentation
from ..services.common import iso, row_dict
from ..services.metrics import metrics
from ..services.policy import active_policy
from ..services.reports import performance

router = APIRouter(prefix="/api/v1", tags=["transactions"])


# --------------------------------------------------------------------------- schemas
class DecisionRequest(BaseModel):
    """Canonical synchronous decision request (BRD 15.3). Extra fields are accepted and mapped."""
    model_config = {"extra": "allow", "json_schema_extra": {"example": {
        "transaction_id": "TX-20261123-000001", "idempotency_key": "TX-20261123-000001-v1",
        "customer_id": "C00012", "account_reference": "ACCT-00456", "timestamp": "2026-11-23T09:30:00+02:00",
        "transaction_type": "bank_transfer", "amount": 1250.0, "currency": "ILS", "channel": "mobile_banking",
        "beneficiary_reference": "PS92PALS000000000400123456702", "beneficiary_country": "PS",
        "ip_address": "213.6.10.20", "ip_country": "PS", "asn": "AS12975", "session_reference": "SESSION-ABC",
        "device_reference": "DEVICE-XYZ"}}}
    transaction_id: str = Field(..., max_length=80)
    idempotency_key: Optional[str] = Field(None, max_length=120)
    customer_id: str = Field(..., max_length=64)
    timestamp: Optional[str] = None
    transaction_type: str = Field(..., description="bank_transfer | phone_recharge | prepaid_card (aliases accepted)")
    amount: float = Field(..., gt=0)
    currency: Optional[str] = Field(None, max_length=3)
    channel: Optional[str] = Field(None, max_length=40)
    account_reference: Optional[str] = None
    beneficiary_reference: Optional[str] = None
    beneficiary_country: Optional[str] = Field(None, max_length=2)
    ip_address: Optional[str] = None
    ip_country: Optional[str] = None
    asn: Optional[str] = None
    session_reference: Optional[str] = None
    device_reference: Optional[str] = None


class EventsRequest(BaseModel):
    events: list[dict] = Field(..., max_length=5000)
    mode: str = Field("POST_EVENT", description="POST_EVENT (monitoring) or HISTORY (training import)")
    source: Optional[str] = None


class ExecutionResult(BaseModel):
    state: str = Field(..., description="PENDING | HELD | AUTHORISED | DECLINED | SETTLED | RECALLED | EXPIRED")
    reference: Optional[str] = ""
    detail: Optional[str] = ""


class BankAction(BaseModel):
    rationale: str = Field(..., min_length=10, max_length=2000)


def _err(exc: decision_service.DecisionError):
    return HTTPException(exc.status, exc.body())


# --------------------------------------------------------------------------- decisions & events
@router.post("/decisions/evaluate", tags=["decisions"])
def evaluate(body: DecisionRequest, response: Response,
             p: Principal = Depends(require("decisions.evaluate", "decisions.test")), db: Session = Depends(get_db)):
    """Synchronous pre-authorisation decision: PASS / REVIEW / HOLD / DECLINE (or ERROR_POLICY + fallback)."""
    try:
        dec, tx, created = decision_service.evaluate(db, p.tenant_id, p.space, body.model_dump(exclude_none=True),
                                                     mode="PRE_AUTH", source="Decision API" if p.kind == "api"
                                                     else "Decision Console", caller=p.display)
    except decision_service.DecisionError as exc:
        raise _err(exc) from exc
    response.status_code = 201 if created else 200
    if p.kind == "user":
        audit.record(db, p.tenant_id, "decision.test", actor=p, resource_type="transaction", resource_id=dec.tx_ref,
                     after={"decision": dec.decision})
    out = decision_service.response(dec, tx)
    out["idempotent_replay"] = not created
    if p.kind == "user":
        out["explanation"] = dec.explanation
        out["reason_details"] = decision_service.explain_codes(dec.reason_codes)
    return out


@router.post("/transactions/events", tags=["ingestion"])
def ingest(body: EventsRequest, p: Principal = Depends(require("events.ingest")), db: Session = Depends(get_db)):
    """Asynchronous ingestion (post-event monitoring or historical import). Never blocks the payment path."""
    mode = body.mode.upper()
    if mode not in ("POST_EVENT", "HISTORY"):
        raise HTTPException(422, {"code": "INVALID_MODE", "message": "mode must be POST_EVENT or HISTORY"})
    return decision_service.ingest_events(db, p.tenant_id, p.space, body.events, body.source or p.display, mode,
                                          caller=p.display)


@router.post("/transactions/{tx_ref}/execution-result", tags=["decisions"])
def execution_result(tx_ref: str, body: ExecutionResult, p: Principal = Depends(require("execution.report")),
                     db: Session = Depends(get_db)):
    """The bank reports the real authorisation/hold/release/decline/settlement result."""
    try:
        tx = decision_service.report_execution(db, p.tenant_id, p.space, tx_ref, body.state, body.reference or "",
                                               body.detail or "", p.display)
    except decision_service.DecisionError as exc:
        raise _err(exc) from exc
    return {"transaction_id": tx.tx_ref, "execution_state": tx.execution_state}


@router.get("/transactions/{tx_ref}/decision", tags=["decisions"])
def get_decision(tx_ref: str, p: Principal = Depends(require("tx.view_full", "tx.view_masked", "decisions.evaluate",
                                                             "tx.view_assigned")),
                 db: Session = Depends(get_db)):
    tx = _visible_tx(db, p, tx_ref)
    dec = db.get(Decision, tx.decision_id) if tx.decision_id else None
    if dec is None:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "no decision for this transaction"})
    return decision_service.response(dec, tx)


# --------------------------------------------------------------------------- transactions
def _visible_tx(db: Session, p: Principal, tx_ref: str) -> Transaction:
    tx = db.execute(select(Transaction).where(Transaction.tenant_id == p.tenant_id, Transaction.space == p.space,
                                              Transaction.tx_ref == tx_ref)).scalar()
    if tx is None:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "transaction not found"})
    if not p.can("tx.view_full", "tx.view_masked", "decisions.evaluate"):
        scope = case_service.assigned_entity_refs(db, p)
        if tx.tx_ref not in scope["tx"] and tx.customer_ref not in scope["customers"]:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "transaction not found"})
    return tx


def _masked(p: Principal) -> bool:
    return not p.can("tx.view_full", "tx.view_assigned")


def tx_out(tx: Transaction, dec: Optional[Decision], masked: bool) -> dict:
    d = {"tx_ref": tx.tx_ref, "customer_ref": tx.customer_ref, "ts": iso(tx.ts), "tx_type": tx.tx_type,
         "amount": tx.amount, "currency": tx.currency, "channel": tx.channel, "source": tx.source, "mode": tx.mode,
         "beneficiary": tx.beneficiary, "beneficiary_country": tx.beneficiary_country, "ip": tx.ip,
         "connection": tx.cc_asn, "execution_state": tx.execution_state, "decision_id": tx.decision_id,
         "decision": dec.decision if dec else None, "fraud_score": dec.fraud_score if dec else None,
         "risk_level": dec.risk_level if dec else None, "aml_signal_level": dec.aml_signal_level if dec else None,
         "reason_codes": dec.reason_codes if dec else [], "issued_at": iso(dec.issued_at) if dec else None,
         "scenario": tx.scenario if tx.space == "DEMO" else None, "label": tx.label if tx.space == "DEMO" else None}
    if masked:
        d.update({"customer_ref": mask(tx.customer_ref, 2), "beneficiary": mask(tx.beneficiary), "ip": mask_ip(tx.ip),
                  "amount": None, "masked": True})
    return d


@router.get("/transactions")
def list_transactions(search: str = "", decision: str = "", risk_level: str = "", mode: str = "", tx_type: str = "",
                      channel: str = "", execution_state: str = "", customer_ref: str = "", date_from: str = "",
                      date_to: str = "", include_history: bool = False, page: int = 1,
                      page_size: int = Query(50, le=500),
                      p: Principal = Depends(require("tx.view_full", "tx.view_masked", "tx.view_assigned")),
                      db: Session = Depends(get_db)):
    q = select(Transaction, Decision).outerjoin(Decision, Decision.id == Transaction.decision_id).where(
        Transaction.tenant_id == p.tenant_id, Transaction.space == p.space)
    if not p.can("tx.view_full", "tx.view_masked"):
        scope = case_service.assigned_entity_refs(db, p)
        if not scope["tx"] and not scope["customers"]:
            return {"total": 0, "items": [], "page": page}
        q = q.where(or_(Transaction.tx_ref.in_(scope["tx"] or [""]),
                        Transaction.customer_ref.in_(scope["customers"] or [""])))
    if not include_history:
        q = q.where(Transaction.mode != "HISTORY")
    if search:
        like = f"%{search.strip()}%"
        q = q.where(or_(Transaction.tx_ref.like(like), Transaction.customer_ref.like(like),
                        Transaction.beneficiary.like(like)))
    for col, val in ((Decision.decision, decision), (Decision.risk_level, risk_level), (Transaction.mode, mode),
                     (Transaction.tx_type, tx_type), (Transaction.channel, channel),
                     (Transaction.execution_state, execution_state), (Transaction.customer_ref, customer_ref)):
        if val:
            q = q.where(col == val)
    if date_from:
        q = q.where(Transaction.ts >= datetime.fromisoformat(date_from.replace("Z", "")))
    if date_to:
        end = datetime.fromisoformat(date_to.replace("Z", ""))
        q = q.where(Transaction.ts < (end + timedelta(days=1) if len(date_to) <= 10 else end))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar()
    rows = db.execute(q.order_by(Transaction.ts.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    masked = _masked(p)
    return {"total": total, "page": page, "page_size": page_size, "items": [tx_out(t, d, masked) for t, d in rows]}


@router.get("/transactions/{tx_ref}")
def transaction_detail(tx_ref: str, p: Principal = Depends(require("tx.view_full", "tx.view_masked",
                                                                   "tx.view_assigned")),
                       db: Session = Depends(get_db)):
    tx = _visible_tx(db, p, tx_ref)
    dec = db.get(Decision, tx.decision_id) if tx.decision_id else None
    masked = _masked(p)
    out: dict[str, Any] = {"transaction": tx_out(tx, dec, masked)}
    events = db.execute(select(ExecutionStatusEvent).where(ExecutionStatusEvent.tenant_id == p.tenant_id,
                                                           ExecutionStatusEvent.space == p.space,
                                                           ExecutionStatusEvent.tx_ref == tx_ref)
                        .order_by(ExecutionStatusEvent.id)).scalars().all()
    out["execution_events"] = [row_dict(e) for e in events]
    if dec:
        d = row_dict(dec)
        if masked:
            for k in ("reasons", "explanation", "aml_indicators", "rule_hits"):
                d[k] = [] if k != "explanation" else ""
            d["customer_ref"] = mask(dec.customer_ref, 2)
        d["reason_details"] = decision_service.explain_codes(dec.reason_codes)
        out["decision"] = d
    alerts = db.execute(select(Alert).where(Alert.tenant_id == p.tenant_id, Alert.space == p.space,
                                            Alert.tx_ref == tx_ref)).scalars().all()
    out["alerts"] = [row_dict(a) for a in alerts] if not masked else []
    links = db.execute(select(CaseEntityLink.case_id).where(CaseEntityLink.entity_type == "TRANSACTION",
                                                            CaseEntityLink.entity_ref == tx_ref)).scalars().all()
    out["cases"] = [c for c in links if case_service.can_view_case(p, db.get(Case, c))]
    out["can_bank_action"] = p.can("cases.dispose") and tx.mode == "PRE_AUTH" and \
        tx.execution_state in ("PENDING", "HELD")
    return out


@router.post("/transactions/{tx_ref}/{action}")
def bank_action(tx_ref: str, action: str, body: BankAction, p: Principal = Depends(require("cases.dispose")),
                db: Session = Depends(get_db)):
    """AML Manager release/reject of a pending or held payment through the bank's authorised adapter."""
    if action not in ("release", "reject"):
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "unknown action"})
    _visible_tx(db, p, tx_ref)
    try:
        res = decision_service.request_bank_action(db, p.tenant_id, p.space, tx_ref, action.upper(), p, body.rationale)
    except decision_service.DecisionError as exc:
        raise _err(exc) from exc
    audit.record(db, p.tenant_id, f"transaction.{action}", actor=p, resource_type="transaction", resource_id=tx_ref,
                 after=res, reason=body.rationale)
    return res


# --------------------------------------------------------------------------- customers
def _visible_customer(db: Session, p: Principal, ref: str) -> Customer:
    c = db.execute(select(Customer).where(Customer.tenant_id == p.tenant_id, Customer.space == p.space,
                                          Customer.customer_ref == ref)).scalar()
    if c is None:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "customer not found"})
    if not p.can("customers.view_full", "customers.view_masked"):
        if ref not in case_service.assigned_entity_refs(db, p)["customers"]:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "customer not found"})
    return c


def _customer_masked(p: Principal) -> bool:
    return not p.can("customers.view_full", "customers.view_assigned")


@router.get("/customers")
def list_customers(search: str = "", segment: Optional[int] = None, risk: str = "", customer_type: str = "",
                   history: str = "", sort: str = "", page: int = 1, page_size: int = Query(50, le=500),
                   p: Principal = Depends(require("customers.view_full", "customers.view_masked",
                                                  "customers.view_assigned")),
                   db: Session = Depends(get_db)):
    q = select(Customer).where(Customer.tenant_id == p.tenant_id, Customer.space == p.space)
    if not p.can("customers.view_full", "customers.view_masked"):
        q = q.where(Customer.customer_ref.in_(case_service.assigned_entity_refs(db, p)["customers"] or [""]))
    if search:
        like = f"%{search.strip()}%"
        q = q.where(or_(Customer.customer_ref.like(like), Customer.display_name.like(like)))
    if segment is not None:
        q = q.where(Customer.segment_key == segment)
    if risk:
        q = q.where(Customer.risk_category == risk)
    if customer_type:
        q = q.where(Customer.customer_type == customer_type)
    counts = dict(db.execute(select(Transaction.customer_ref, func.count()).where(
        Transaction.tenant_id == p.tenant_id, Transaction.space == p.space).group_by(Transaction.customer_ref)).all())
    alerts = dict(db.execute(select(Alert.customer_ref, func.count()).where(
        Alert.tenant_id == p.tenant_id, Alert.space == p.space, Alert.status.in_(("OPEN", "TRIAGED", "IN_CASE")))
        .group_by(Alert.customer_ref)).all())
    since30 = utcnow() - timedelta(days=30)
    live: dict = {}
    for ref, ts, score, dec in db.execute(select(Decision.customer_ref, Decision.issued_at, Decision.fraud_score,
                                                 Decision.decision).where(
            Decision.tenant_id == p.tenant_id, Decision.space == p.space, Decision.issued_at >= since30,
            Decision.decision != "ERROR_POLICY").order_by(Decision.issued_at.desc())):
        entry = live.get(ref)
        if entry is None:
            live[ref] = {"latest_score": score, "latest_decision": dec, "latest_at": ts, "max_score_30d": score,
                         "decisions_30d": 1}
        else:
            entry["max_score_30d"] = max(entry["max_score_30d"], score)
            entry["decisions_30d"] += 1
    rows = db.execute(q.order_by(Customer.customer_ref)).scalars().all()
    if sort == "risk":
        rows.sort(key=lambda c: -(live.get(c.customer_ref) or {}).get("max_score_30d", -1))
    elif sort == "recent":
        rows.sort(key=lambda c: (live.get(c.customer_ref) or {}).get("latest_at") or datetime.min, reverse=True)
    if history == "limited":
        rows = [c for c in rows if counts.get(c.customer_ref, 0) < segmentation.MIN_HISTORY]
    elif history == "established":
        rows = [c for c in rows if counts.get(c.customer_ref, 0) >= segmentation.MIN_HISTORY]
    total = len(rows)
    rows = rows[(page - 1) * page_size: page * page_size]
    masked = _customer_masked(p)
    items = []
    for c in rows:
        n = counts.get(c.customer_ref, 0)
        items.append({"customer_ref": mask(c.customer_ref, 2) if masked else c.customer_ref,
                      "display_name": "" if masked else c.display_name, "customer_type": c.customer_type,
                      "risk_category": c.risk_category if not masked else None, "segment_key": c.segment_key,
                      "segment_label": c.segment_label, "segment_confidence": c.segment_confidence,
                      "transactions": n, "open_alerts": alerts.get(c.customer_ref, 0) if not masked else None,
                      "limited_history": n < segmentation.MIN_HISTORY, "kyc_review_status": c.kyc_review_status,
                      **({k: iso(v) for k, v in (live.get(c.customer_ref) or {}).items()} if not masked else
                         {"max_score_30d": (live.get(c.customer_ref) or {}).get("max_score_30d")})})
    return {"total": total, "page": page, "items": items}


@router.get("/customers/{ref}/profile")
def customer_profile(ref: str, p: Principal = Depends(require("customers.view_full", "customers.view_masked",
                                                               "customers.view_assigned")),
                     db: Session = Depends(get_db)):
    c = _visible_customer(db, p, ref)
    segmentation.ensure_assignment(db, c)
    db.commit()
    masked = _customer_masked(p)
    txs = db.execute(select(Transaction, Decision).outerjoin(Decision, Decision.id == Transaction.decision_id).where(
        Transaction.tenant_id == p.tenant_id, Transaction.space == p.space, Transaction.customer_ref == ref)
        .order_by(Transaction.ts.desc()).limit(400)).all()
    n_total = db.execute(select(func.count(Transaction.id)).where(
        Transaction.tenant_id == p.tenant_id, Transaction.space == p.space, Transaction.customer_ref == ref)).scalar()
    amounts = [t.amount for t, _ in txs]
    channels = Counter(t.channel or "-" for t, _ in txs)
    hours = [0] * 24
    for t, _ in txs:
        hours[t.ts.hour] += 1
    bens = Counter(t.beneficiary for t, _ in txs if t.beneficiary)
    countries = Counter(t.beneficiary_country for t, _ in txs if t.beneficiary_country)
    types = Counter(t.tx_type for t, _ in txs)
    month = utcnow() - timedelta(days=30)
    month_total = sum(t.amount for t, _ in txs if t.ts >= month)
    limited = n_total < segmentation.MIN_HISTORY
    profile = row_dict(c)
    if masked:
        profile = {k: profile[k] for k in ("customer_type", "segment_label", "segment_key", "segment_confidence",
                                           "segment_basis", "segment_model", "kyc_review_status")}
        profile["customer_ref"] = mask(c.customer_ref, 2)
    out: dict[str, Any] = {
        "profile": profile,
        "limited_history": limited,
        "monitoring_treatment": ("Limited history: compared with the peer segment; decisions capped at REVIEW unless "
                                 "a hard rule applies. Personal profile learns as activity grows.") if limited else
                                "Established personal behavioural baseline (local + temporal) plus peer comparison.",
        "aggregates": {"transactions": n_total, "last_30d_amount": round(month_total, 2),
                       "median_amount": round(sorted(amounts)[len(amounts) // 2], 2) if amounts else None,
                       "max_amount": max(amounts) if amounts else None,
                       "expected_monthly_turnover": c.expected_monthly_turnover if not masked else None,
                       "turnover_ratio": round(month_total / c.expected_monthly_turnover, 2)
                       if c.expected_monthly_turnover and not masked else None},
        "usual": {"channels": channels.most_common(5), "hours": hours, "types": types.most_common(),
                  "beneficiaries": [(mask(b), n) for b, n in bens.most_common(5)],
                  "countries": countries.most_common(5)},
    }
    if not masked:
        out["engine_profile"] = engine_registry.customer_profile_summary(p.tenant_id, p.space, ref)
        out["transactions"] = [tx_out(t, d, False) for t, d in txs[:100]]
        out["alerts"] = [row_dict(a) for a in db.execute(select(Alert).where(
            Alert.tenant_id == p.tenant_id, Alert.space == p.space, Alert.customer_ref == ref)
            .order_by(Alert.created_at.desc())).scalars()]
        case_ids = db.execute(select(CaseEntityLink.case_id).where(CaseEntityLink.entity_type == "CUSTOMER",
                                                                   CaseEntityLink.entity_ref == ref)).scalars().all()
        out["cases"] = [case_service.serialize(db, cs) for cs in (db.get(Case, i) for i in set(case_ids))
                        if cs and case_service.can_view_case(p, cs)]
        out["score_timeline"] = [{"ts": iso(t.ts), "score": d.fraud_score, "decision": d.decision}
                                 for t, d in reversed(txs) if d is not None][-120:]
    if c.segment_key is not None:
        from ..models import CustomerSegment
        seg = db.execute(select(CustomerSegment).where(CustomerSegment.model_version == c.segment_model,
                                                       CustomerSegment.segment_key == c.segment_key)).scalar()
        out["segment"] = {"label": seg.label, "size": seg.size, "description": seg.description,
                          "profile": seg.profile} if seg else None
    return out


@router.get("/customers/{ref}/alerts")
def customer_alerts(ref: str, p: Principal = Depends(require("alerts.view_all")), db: Session = Depends(get_db)):
    _visible_customer(db, p, ref)
    rows = db.execute(select(Alert).where(Alert.tenant_id == p.tenant_id, Alert.space == p.space,
                                          Alert.customer_ref == ref).order_by(Alert.created_at.desc())).scalars().all()
    return {"alerts": [row_dict(a) for a in rows]}


# --------------------------------------------------------------------------- alerts
@router.get("/alerts")
def list_alerts(status: str = "", severity: str = "", timing: str = "", decision: str = "", search: str = "",
                page: int = 1, page_size: int = Query(50, le=500), p: Principal = Depends(require("alerts.view_all")),
                db: Session = Depends(get_db)):
    q = select(Alert).where(Alert.tenant_id == p.tenant_id, Alert.space == p.space)
    if status == "ACTIVE":
        q = q.where(Alert.status.in_(("OPEN", "TRIAGED")))
    elif status:
        q = q.where(Alert.status == status)
    for col, val in ((Alert.severity, severity), (Alert.timing, timing), (Alert.decision, decision)):
        if val:
            q = q.where(col == val)
    if search:
        like = f"%{search}%"
        q = q.where(or_(Alert.id.like(like), Alert.customer_ref.like(like), Alert.tx_ref.like(like)))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar()
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    rows = db.execute(q.order_by(Alert.created_at.desc()).offset((page - 1) * page_size).limit(page_size))\
        .scalars().all()
    items = [row_dict(a) for a in rows]
    for i in items:
        i["severity_rank"] = order.get(i["severity"], 9)
        if p.kind == "api":          # system/MCP callers get a minimised view
            i["customer_ref"], i["tx_ref"] = mask(i["customer_ref"], 2), mask(i["tx_ref"])
    return {"total": total, "page": page, "items": items, "close_reasons": case_service.ALERT_CLOSE_REASONS}


@router.post("/alerts", status_code=201)
def create_alert(body: dict, p: Principal = Depends(require("cases.create")), db: Session = Depends(get_db)):
    if not body.get("customer_ref") or not body.get("summary"):
        raise HTTPException(422, {"code": "INVALID", "message": "customer_ref and summary are required"})
    _visible_customer(db, p, body["customer_ref"])
    a = case_service.create_manual_alert(db, p, body["customer_ref"], body["summary"], body.get("tx_ref"),
                                         body.get("severity", "MEDIUM"))
    return {"alert": row_dict(a)}


@router.post("/alerts/{alert_id}/close")
def close_alert(alert_id: str, body: dict, p: Principal = Depends(require("alerts.triage")),
                db: Session = Depends(get_db)):
    try:
        a = case_service.close_alert(db, p, alert_id, body.get("reason_code", ""), body.get("note", ""))
    except case_service.CaseError as exc:
        raise HTTPException(exc.status, {"code": "CASE_ERROR", "message": exc.message}) from exc
    return {"alert": row_dict(a)}


@router.post("/alerts/{alert_id}/triage")
def triage_alert(alert_id: str, body: dict, p: Principal = Depends(require("alerts.triage")),
                 db: Session = Depends(get_db)):
    try:
        a = case_service.triage_alert(db, p, alert_id, body.get("note", ""))
    except case_service.CaseError as exc:
        raise HTTPException(exc.status, {"code": "CASE_ERROR", "message": exc.message}) from exc
    return {"alert": row_dict(a)}


# --------------------------------------------------------------------------- live pulse
@router.get("/live/pulse")
def live_pulse(p: Principal = Depends(require()), db: Session = Depends(get_db)):
    """Lightweight real-time indicator: decisions scored in the last minutes and running channels."""
    from ..services.connectors import runtime
    now = utcnow()
    base = (Decision.tenant_id == p.tenant_id) & (Decision.space == p.space)
    last_min = db.execute(select(func.count(Decision.id)).where(base, Decision.issued_at >= now - timedelta(minutes=1)))\
        .scalar()
    last_5 = db.execute(select(func.count(Decision.id)).where(base, Decision.issued_at >= now - timedelta(minutes=5)))\
        .scalar()
    last = db.execute(select(Decision).where(base).order_by(Decision.issued_at.desc()).limit(1)).scalar()
    conns = db.execute(select(Connector.id).where(Connector.tenant_id == p.tenant_id, Connector.space == p.space,
                                                  Connector.status.in_(("ENABLED", "DEGRADED")))).scalars().all()
    running = sum(1 for cid in conns if runtime.status(cid).get("running"))
    open_alerts = db.execute(select(func.count(Alert.id)).where(
        Alert.tenant_id == p.tenant_id, Alert.space == p.space, Alert.status.in_(("OPEN", "TRIAGED")))).scalar() \
        if p.can("alerts.view_all") else None
    return {"space": p.space, "now": iso(now), "decisions_last_minute": last_min, "decisions_last_5_minutes": last_5,
            "channels_running": running, "channels_enabled": len(conns), "open_alerts": open_alerts,
            "model_ready": engine_registry.workspace(p.tenant_id, p.space).ready,
            "last_decision": None if last is None or not p.can("tx.view_full", "tx.view_masked") else
            {"at": iso(last.issued_at), "decision": last.decision, "fraud_score": last.fraud_score,
             "tx_ref": last.tx_ref if p.can("tx.view_full") else mask(last.tx_ref)}}


# --------------------------------------------------------------------------- dashboards
def _trend(db: Session, p: Principal, days: int = 14) -> list[dict]:
    since = (utcnow() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.execute(select(Decision.issued_at, Decision.decision, Decision.mode).where(
        Decision.tenant_id == p.tenant_id, Decision.space == p.space, Decision.issued_at >= since)).all()
    by: dict[str, Counter] = defaultdict(Counter)
    for ts, dec, mode in rows:
        by[ts.strftime("%Y-%m-%d")][dec] += 1
    out = []
    for i in range(days):
        day = (since + timedelta(days=i)).strftime("%Y-%m-%d")
        c = by.get(day, Counter())
        out.append({"day": day, **{k: c.get(k, 0) for k in ("PASS", "REVIEW", "HOLD", "DECLINE", "ERROR_POLICY")}})
    return out


def _case_stats(db: Session, p: Principal, cases: list[Case]) -> dict:
    now = utcnow()
    open_ = [c for c in cases if c.status != "CLOSED"]
    ageing = Counter()
    for c in open_:
        age = (now - c.created_at).days
        ageing["0-2d" if age <= 2 else "3-7d" if age <= 7 else "8-30d" if age <= 30 else ">30d"] += 1
    return {"open": len(open_), "breached": sum(1 for c in open_ if c.due_at and c.due_at < now),
            "by_status": dict(Counter(c.status for c in open_)), "by_priority": dict(Counter(c.priority for c in open_)),
            "ageing": {k: ageing.get(k, 0) for k in ("0-2d", "3-7d", "8-30d", ">30d")},
            "awaiting_manager": sum(1 for c in open_ if c.status in ("RECOMMENDATION_SUBMITTED", "MANAGER_REVIEW")),
            "dispositions": dict(Counter((c.disposition or {}).get("label", "-") for c in cases
                                         if c.status == "CLOSED"))}


@router.get("/dashboard")
def dashboard(p: Principal = Depends(require()), db: Session = Depends(get_db)):
    role = p.role
    out: dict[str, Any] = {"role": role, "space": p.space, "generated_at": iso(utcnow())}
    since24 = utcnow() - timedelta(hours=24)

    if role in ("AML_MANAGER", "CHIEF_AML"):
        alerts = db.execute(select(Alert).where(Alert.tenant_id == p.tenant_id, Alert.space == p.space)).scalars().all()
        cases = db.execute(select(Case).where(Case.tenant_id == p.tenant_id, Case.space == p.space)).scalars().all()
        active = [a for a in alerts if a.status in ("OPEN", "TRIAGED")]
        held = db.execute(select(Transaction, Decision).join(Decision, Decision.id == Transaction.decision_id).where(
            Transaction.tenant_id == p.tenant_id, Transaction.space == p.space,
            Transaction.execution_state.in_(("HELD", "PENDING", "RELEASE_REQUESTED", "DECLINE_REQUESTED")),
            Transaction.mode == "PRE_AUTH", Decision.decision.in_(("HOLD", "DECLINE", "ERROR_POLICY")))
            .order_by(Decision.issued_at)).all()
        expiry = int(active_policy(db, p.tenant_id).document.get("hold_expiry_minutes", 240))
        out["kpis"] = {
            "alerts_total": len(alerts), "alerts_open": len(active),
            "alerts_24h": sum(1 for a in alerts if a.created_at >= since24),
            "cases_open": sum(1 for c in cases if c.status != "CLOSED"),
            "held_count": len(held), "held_amount": round(sum(t.amount for t, _ in held), 2),
            "decisions_24h": db.execute(select(func.count(Decision.id)).where(
                Decision.tenant_id == p.tenant_id, Decision.space == p.space, Decision.issued_at >= since24)).scalar(),
        }
        out["alerts_by_severity"] = dict(Counter(a.severity for a in active))
        out["alerts_by_timing"] = dict(Counter(a.timing for a in active))
        out["cases"] = _case_stats(db, p, cases)
        out["trend"] = _trend(db, p)
        out["held"] = [{"tx_ref": t.tx_ref, "customer_ref": t.customer_ref, "amount": t.amount, "currency": t.currency,
                        "decision": d.decision, "state": t.execution_state, "issued_at": iso(d.issued_at),
                        "minutes_left": round((d.issued_at + timedelta(minutes=expiry) - utcnow()).total_seconds() / 60)}
                       for t, d in held[:15]]
        names = {u.id: u.full_name for u in db.execute(select(User).where(User.tenant_id == p.tenant_id,
                                                                          User.role == "AML_EMPLOYEE")).scalars()}
        workload = []
        for uid, name in names.items():
            mine = [c for c in cases if c.assignee_id == uid]
            open_ = [c for c in mine if c.status != "CLOSED"]
            turn = [(c.recommended_at - c.assigned_at).total_seconds() / 3600 for c in mine
                    if c.recommended_at and c.assigned_at]
            workload.append({"user_id": uid, "name": name, "open": len(open_),
                             "breached": sum(1 for c in open_ if c.due_at and c.due_at < utcnow()),
                             "submitted": sum(1 for c in mine if c.recommendation),
                             "avg_turnaround_h": round(sum(turn) / len(turn), 1) if turn else None})
        out["workload"] = workload
        since14 = utcnow() - timedelta(days=14)
        det = db.execute(select(Transaction.channel, Transaction.tx_type, Decision.decision).join(
            Decision, Decision.id == Transaction.decision_id).where(
            Transaction.tenant_id == p.tenant_id, Transaction.space == p.space, Decision.issued_at >= since14)).all()
        by_channel: dict[str, Counter] = defaultdict(Counter)
        by_type: dict[str, Counter] = defaultdict(Counter)
        for ch, tt, dec in det:
            by_channel[ch or "-"][dec] += 1
            by_type[tt][dec] += 1
        out["by_channel"] = {k: dict(v) for k, v in by_channel.items()}
        out["by_type"] = {k: dict(v) for k, v in by_type.items()}
        seg = db.execute(select(Customer.segment_label, func.count(Alert.id)).join(
            Customer, (Customer.tenant_id == Alert.tenant_id) & (Customer.space == Alert.space)
            & (Customer.customer_ref == Alert.customer_ref)).where(
            Alert.tenant_id == p.tenant_id, Alert.space == p.space, Alert.created_at >= since14)
            .group_by(Customer.segment_label)).all()
        out["alerts_by_segment"] = [{"segment": s or "Unassigned", "alerts": n} for s, n in seg]
        closed_alerts = [a for a in alerts if a.status == "CLOSED"]
        out["alert_outcomes"] = {"closed": len(closed_alerts),
                                 "no_concern_or_false_positive": sum(1 for a in closed_alerts if any(
                                     k in (a.closed_reason or "") for k in ("No concern", "False positive",
                                                                            "false positive"))),
                                 "escalations": sum(1 for c in cases if c.escalation)}
        out["policy"] = {"active": active_policy(db, p.tenant_id).id}
        policy_perf = db.execute(select(Decision.policy_version, Decision.decision, func.count()).where(
            Decision.tenant_id == p.tenant_id, Decision.space == p.space).group_by(Decision.policy_version,
                                                                                   Decision.decision)).all()
        pp: dict[str, Counter] = defaultdict(Counter)
        for pv, dec, n in policy_perf:
            pp[pv][dec] += n
        out["policy_performance"] = {k: dict(v) for k, v in pp.items()}
        out["recent_alerts"] = [row_dict(a) for a in sorted(active, key=lambda a: a.created_at, reverse=True)[:8]]
        if role == "CHIEF_AML":
            out["model_effectiveness"] = performance(db, p.tenant_id, p.space)
            out["high_risk_cases"] = [case_service.serialize(db, c) for c in cases
                                      if c.status != "CLOSED" and c.priority in ("HIGH", "URGENT")][:10]
            open_inc = db.execute(select(func.count(SystemIncident.id)).where(
                SystemIncident.tenant_id == p.tenant_id, SystemIncident.status.notin_(("RESOLVED", "POSTMORTEM"))))\
                .scalar()
            ws = engine_registry.status(db, p.tenant_id, p.space)
            out["system_summary"] = {"decision_service_ready": ws["ready"], "open_incidents": open_inc,
                                     "decision_p95_ms": metrics.latency_summary("decision")["p95"]}
            risk_trend = db.execute(select(Decision.issued_at, Decision.risk_level).where(
                Decision.tenant_id == p.tenant_id, Decision.space == p.space, Decision.issued_at >= since14)).all()
            rt: dict[str, Counter] = defaultdict(Counter)
            for ts, lvl in risk_trend:
                rt[ts.strftime("%Y-%m-%d")][lvl] += 1
            out["risk_trend"] = [{"day": d, **dict(c)} for d, c in sorted(rt.items())]

    elif role == "AML_EMPLOYEE":
        mine = db.execute(case_service.case_query(p)).scalars().all()
        open_ = [c for c in mine if c.status != "CLOSED"]
        now = utcnow()
        out["kpis"] = {"assigned_open": len(open_), "due_24h": sum(1 for c in open_ if c.due_at and
                                                                  now <= c.due_at <= now + timedelta(hours=24)),
                       "breached": sum(1 for c in open_ if c.due_at and c.due_at < now),
                       "returned": sum(1 for c in open_ if c.status == "RETURNED_FOR_MORE_INFO"),
                       "awaiting_manager": sum(1 for c in open_ if c.status in ("RECOMMENDATION_SUBMITTED",
                                                                                "MANAGER_REVIEW")),
                       "completed": sum(1 for c in mine if c.status == "CLOSED")}
        turn = [(c.recommended_at - c.assigned_at).total_seconds() / 3600 for c in mine
                if c.recommended_at and c.assigned_at]
        out["my_metrics"] = {"recommendations": sum(1 for c in mine if c.recommendation),
                             "avg_turnaround_h": round(sum(turn) / len(turn), 1) if turn else None}
        out["cases"] = [case_service.serialize(db, c) for c in sorted(
            open_, key=lambda c: (c.due_at or now + timedelta(days=999)))]

    if role == "IT_ADMIN":
        conns = db.execute(select(Connector).where(Connector.tenant_id == p.tenant_id)).scalars().all()
        from ..services.connectors import public
        tx24 = db.execute(select(Transaction.mode, func.count()).where(
            Transaction.tenant_id == p.tenant_id, Transaction.space == p.space, Transaction.created_at >= since24,
            Transaction.mode != "HISTORY").group_by(Transaction.mode)).all()
        dlq = db.execute(select(DeadLetterEvent.category, func.count()).where(
            DeadLetterEvent.tenant_id == p.tenant_id, DeadLetterEvent.space == p.space,
            DeadLetterEvent.created_at >= since24).group_by(DeadLetterEvent.category)).all()
        dlq = dict(dlq)
        accepted = sum(n for _, n in tx24)
        out["kpis"] = {"received_24h": accepted + sum(dlq.values()), "accepted_24h": accepted,
                       "rejected_24h": dlq.get("VALIDATION", 0) + dlq.get("PROCESSING", 0),
                       "duplicates_24h": dlq.get("DUPLICATE", 0),
                       "connectors_enabled": sum(1 for c in conns if c.status == "ENABLED"),
                       "connectors_degraded": sum(1 for c in conns if c.status == "DEGRADED")}
        out["connectors"] = [public(c) for c in conns if c.status != "RETIRED"]
        out["latency"] = {"api": metrics.latency_summary("api"), "decision": metrics.latency_summary("decision")}
        out["models"] = {"live": engine_registry.status(db, p.tenant_id, "LIVE"),
                         "demo": engine_registry.status(db, p.tenant_id, "DEMO")}
        out["incidents"] = [row_dict(i) for i in db.execute(select(SystemIncident).where(
            SystemIncident.tenant_id == p.tenant_id).order_by(SystemIncident.last_seen_at.desc()).limit(8)).scalars()]
        from ..models import AuditEvent
        out["config_changes"] = [row_dict(e, exclude=("before", "after", "prev_hash", "hash")) for e in db.execute(
            select(AuditEvent).where(AuditEvent.tenant_id == p.tenant_id, AuditEvent.action.notlike("auth.%"))
            .order_by(AuditEvent.id.desc()).limit(10)).scalars()]
        out["errors"] = metrics.snapshot()["recent_errors"][-10:]
        users = db.execute(select(User.status, func.count()).where(User.tenant_id == p.tenant_id)
                           .group_by(User.status)).all()
        out["users"] = dict(users)
    out["models_summary"] = {"fraud": next((m.id for m in db.execute(select(ModelVersion).where(
        ModelVersion.tenant_id == p.tenant_id, ModelVersion.space == p.space, ModelVersion.kind == "FRAUD",
        ModelVersion.status == "ACTIVE")).scalars()), None),
        "policy": db.execute(select(PolicyVersion.id).where(PolicyVersion.tenant_id == p.tenant_id,
                                                           PolicyVersion.status == "ACTIVE")
                             .order_by(PolicyVersion.version.desc())).scalars().first()}
    return out
