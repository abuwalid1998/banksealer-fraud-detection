"""Institution-wide executive overview (CEO role) - shared by the dashboard and the board reports."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Alert, Case, Connector, Customer, Decision, SystemIncident, Transaction, utcnow
from .metrics import metrics

INTERVENTIONS = ("REVIEW", "HOLD", "DECLINE")


def overview(db: Session, tenant_id: str, space: str, days: int = 30) -> dict:
    from . import engine_registry
    from .connectors import runtime

    now = utcnow()
    since = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.execute(select(Transaction.ts, Transaction.amount, Transaction.tx_type, Transaction.channel,
                             Transaction.details, Transaction.mode, Transaction.customer_ref, Decision.decision,
                             Decision.fraud_score, Decision.risk_level, Decision.aml_signal_level,
                             Decision.issued_at).select_from(Decision).join(
        Transaction, (Transaction.tenant_id == Decision.tenant_id) & (Transaction.space == Decision.space)
        & (Transaction.tx_ref == Decision.tx_ref)).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space, Decision.issued_at >= since)
        .limit(300_000)).all()

    by_day: dict[str, dict] = {}
    for i in range(days):
        d = (since + timedelta(days=i)).strftime("%Y-%m-%d")
        by_day[d] = {"day": d, "count": 0, "value": 0.0, "interventions": 0}
    decisions, channels, types, cities = Counter(), defaultdict(lambda: [0, 0.0]), defaultdict(lambda: [0, 0.0]), \
        Counter()
    risk, aml = Counter(), Counter()
    protected = reviewed = 0.0
    cust_score: dict[str, float] = defaultdict(float)
    cust_hits: Counter = Counter()
    total_value = 0.0
    last24 = now - timedelta(hours=24)
    n24 = v24 = 0
    for ts, amount, tx_type, channel, details, mode, cref, dec, score, lvl, aml_lvl, issued in rows:
        key = min(ts, issued).strftime("%Y-%m-%d")      # transaction date (catch-up streams can score later)
        bucket = by_day.get(key)
        if bucket is not None:
            bucket["count"] += 1
            bucket["value"] += amount
            if dec in INTERVENTIONS:
                bucket["interventions"] += 1
        total_value += amount
        decisions[dec] += 1
        channels[channel or "other"][0] += 1
        channels[channel or "other"][1] += amount
        types[tx_type][0] += 1
        types[tx_type][1] += amount
        cities[(details or {}).get("city") or "Not provided"] += 1
        risk[lvl] += 1
        if aml_lvl and aml_lvl != "NONE":
            aml[aml_lvl] += 1
        if mode == "PRE_AUTH" and dec in ("HOLD", "DECLINE"):
            protected += amount
        if dec == "REVIEW":
            reviewed += amount
        cust_score[cref] = max(cust_score[cref], score or 0.0)
        if dec in INTERVENTIONS:
            cust_hits[cref] += 1
        if issued >= last24:
            n24 += 1
            v24 += amount
    for b in by_day.values():
        b["value"] = round(b["value"], 2)

    alerts = db.execute(select(Alert.status, Alert.severity, Alert.created_at).where(
        Alert.tenant_id == tenant_id, Alert.space == space)).all()
    cases = db.execute(select(Case.status, Case.priority, Case.due_at, Case.escalation, Case.disposition).where(
        Case.tenant_id == tenant_id, Case.space == space)).all()
    open_cases = [c for c in cases if c.status != "CLOSED"]

    customers = db.execute(select(Customer.customer_ref, Customer.display_name, Customer.risk_category,
                                  Customer.segment_label, Customer.customer_type, Customer.tenure_months,
                                  Customer.attributes).where(Customer.tenant_id == tenant_id,
                                                             Customer.space == space)).all()
    names = {c.customer_ref: c.display_name for c in customers}
    seg = Counter((c.segment_label or "Unassigned").split(" · ")[0] for c in customers)
    top = sorted(cust_score.items(), key=lambda kv: (cust_hits[kv[0]], kv[1]), reverse=True)[:10]

    conns = db.execute(select(Connector).where(Connector.tenant_id == tenant_id, Connector.space == space,
                                               Connector.status != "RETIRED")).scalars().all()
    ws = engine_registry.status(db, tenant_id, space)
    open_inc = db.execute(select(func.count(SystemIncident.id)).where(
        SystemIncident.tenant_id == tenant_id, SystemIncident.status.notin_(("RESOLVED", "POSTMORTEM")))).scalar()
    total = sum(decisions.values())
    interventions = sum(decisions[d] for d in INTERVENTIONS)
    return {
        "window_days": days, "generated_at": now.isoformat(timespec="seconds") + "Z",
        "kpis": {
            "transactions": total, "value": round(total_value, 2), "transactions_24h": n24, "value_24h": round(v24, 2),
            "intervention_rate": round(100 * interventions / total, 2) if total else 0.0,
            "protected_value": round(protected, 2), "under_review_value": round(reviewed, 2),
            "alerts_open": sum(1 for a in alerts if a.status in ("OPEN", "TRIAGED")),
            "alerts_period": sum(1 for a in alerts if a.created_at >= since),
            "cases_open": len(open_cases),
            "cases_breached": sum(1 for c in open_cases if c.due_at and c.due_at < now),
            "escalations": sum(1 for c in cases if c.escalation),
            "customers": len(customers),
            "customers_active": len(cust_score),
            "customers_high_risk": sum(1 for c in customers if c.risk_category == "HIGH"),
            "aml_signals": sum(aml.values()),
        },
        "trend": list(by_day.values()),
        "decisions": dict(decisions),
        "risk_levels": dict(risk),
        "aml_levels": dict(aml),
        "by_channel": [{"channel": k, "count": v[0], "value": round(v[1], 2)} for k, v in
                       sorted(channels.items(), key=lambda kv: -kv[1][0])],
        "by_type": [{"type": k, "count": v[0], "value": round(v[1], 2)} for k, v in
                    sorted(types.items(), key=lambda kv: -kv[1][0])],
        "by_city": [{"city": k, "count": v} for k, v in cities.most_common(12)],
        "customer_risk": dict(Counter(c.risk_category for c in customers)),
        "customer_types": dict(Counter(c.customer_type for c in customers)),
        "segments": [{"segment": k, "customers": v} for k, v in seg.most_common(10)],
        "top_risk_customers": [{"customer_ref": ref, "name": names.get(ref, ""), "max_score": round(s, 3),
                                "interventions": cust_hits[ref]} for ref, s in top],
        "cases": {"by_status": dict(Counter(c.status for c in open_cases)),
                  "by_priority": dict(Counter(c.priority for c in open_cases)),
                  "dispositions": dict(Counter((c.disposition or {}).get("label", "-") for c in cases
                                               if c.status == "CLOSED"))},
        "alerts_by_severity": dict(Counter(a.severity for a in alerts if a.status in ("OPEN", "TRIAGED"))),
        "system": {"decision_service_ready": ws["ready"], "active_model": ws.get("active_model"),
                   "channels_total": len(conns),
                   "channels_running": sum(1 for c in conns if runtime.status(c.id).get("running")),
                   "channels_degraded": sum(1 for c in conns if c.status == "DEGRADED"),
                   "open_incidents": open_inc,
                   "decision_p95_ms": metrics.latency_summary("decision")["p95"],
                   "decision_p50_ms": metrics.latency_summary("decision")["p50"]},
    }
