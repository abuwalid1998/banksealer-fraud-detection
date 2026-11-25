"""Investigation intelligence: network graph of linked accounts and the customer 360 timeline."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (Alert, Case, CaseEntityLink, CaseEvent, CaseNote, Customer, CustomerConfirmation,
                      CustomerReview, Decision, LoginEvent, ScreeningMatch, Transaction, WatchlistEntry, utcnow)


# --------------------------------------------------------------------------- network graph
GENERIC_DEVICES = {"", "Branch counter", "ATM / kiosk", "Card terminal", "Unknown device"}
# money-transfer types whose counterparty can tie customers together (shops, ATMs and billers do not)
LINK_TYPES = ("bank_transfer", "incoming_transfer")
HUB_LIMIT = 25      # a value shared by more customers than this is a public hub (popular payee, shared NAT IP)

def network(db: Session, tenant_id: str, space: str, customer_ref: str, days: int = 90, hops: int = 2,
            max_nodes: int = 160) -> dict:
    """Customers linked through shared counterparties, devices and IP addresses (mule-ring view)."""
    since = utcnow() - timedelta(days=days)
    base = (Transaction.tenant_id == tenant_id) & (Transaction.space == space) & (Transaction.ts >= since)
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}
    frontier = {customer_ref}
    seen_customers: set = set()
    watched = {w.value for w in db.execute(select(WatchlistEntry).where(
        WatchlistEntry.tenant_id == tenant_id, WatchlistEntry.space == space,
        WatchlistEntry.active.is_(True))).scalars()}

    def node(key: str, kind: str, label: str, **extra) -> None:
        if key not in nodes:
            nodes[key] = {"id": key, "kind": kind, "label": label, **extra}

    for depth in range(hops + 1):
        if not frontier or len(nodes) > max_nodes:
            break
        rows = db.execute(select(Transaction.customer_ref, Transaction.beneficiary, Transaction.tx_type,
                                 Transaction.amount, Transaction.device_ref, Transaction.ip, Transaction.details,
                                 Transaction.label).where(base, Transaction.customer_ref.in_(list(frontier)))
                          .limit(5000)).all()
        seen_customers |= frontier
        links: dict[str, set] = defaultdict(set)
        for r in rows:
            cnode = f"C:{r.customer_ref}"
            node(cnode, "customer", r.customer_ref, depth=depth)
            for kind, value in (("counterparty", r.beneficiary), ("device", r.device_ref), ("ip", r.ip)):
                if not value or value in GENERIC_DEVICES:
                    continue
                if kind == "counterparty" and r.tx_type not in LINK_TYPES:
                    continue
                key = f"{kind[0].upper()}:{value}"
                label = (r.details or {}).get("beneficiary_name") if kind == "counterparty" else None
                node(key, kind, label or value, value=value, watched=str(value).upper() in watched)
                e = edges.setdefault((cnode, key), {"source": cnode, "target": key, "kind": kind, "count": 0,
                                                    "amount": 0.0, "fraud": False,
                                                    "direction": "in" if r.tx_type == "incoming_transfer" else "out"})
                e["count"] += 1
                e["amount"] += r.amount if kind == "counterparty" else 0.0
                e["fraud"] = e["fraud"] or r.label == 1
                links[kind].add(value)
        nxt = set()
        for kind, col in (("counterparty", Transaction.beneficiary), ("device", Transaction.device_ref),
                          ("ip", Transaction.ip)):
            values = [v for v in links.get(kind, set()) if v not in GENERIC_DEVICES][:300]
            if not values:
                continue
            scope = base & (Transaction.tx_type.in_(LINK_TYPES)) if kind == "counterparty" else base
            spread = dict(db.execute(select(col, func.count(func.distinct(Transaction.customer_ref)))
                                     .where(scope, col.in_(values)).group_by(col)).all())
            values = [v for v in values if spread.get(v, 0) <= HUB_LIMIT]
            if not values:
                continue
            for (other, value) in db.execute(select(Transaction.customer_ref, col).where(
                    scope, col.in_(values), Transaction.customer_ref.notin_(list(seen_customers)))
                    .distinct().limit(400)).all():
                key = f"{kind[0].upper()}:{value}"
                cnode = f"C:{other}"
                node(cnode, "customer", other, depth=depth + 1)
                e = edges.setdefault((cnode, key), {"source": cnode, "target": key, "kind": kind, "count": 0,
                                                    "amount": 0.0, "fraud": False, "direction": "out"})
                e["count"] += 1
                nxt.add(other)
        frontier = nxt if depth < hops else set()
    refs = [n["label"] for n in nodes.values() if n["kind"] == "customer"]
    info = {c.customer_ref: c for c in db.execute(select(Customer).where(
        Customer.tenant_id == tenant_id, Customer.space == space, Customer.customer_ref.in_(refs))).scalars()}
    alerts = Counter(r for (r,) in db.execute(select(Alert.customer_ref).where(
        Alert.tenant_id == tenant_id, Alert.space == space, Alert.customer_ref.in_(refs))).all())
    for n in nodes.values():
        if n["kind"] == "customer":
            c = info.get(n["label"])
            n.update({"name": c.display_name if c else "", "risk": c.risk_category if c else "",
                      "alerts": alerts.get(n["label"], 0), "focus": n["label"] == customer_ref})
    # keep counterparties / devices that connect at least two customers, plus those of the focus customer
    degree = Counter()
    for (s, t) in edges:
        degree[t] += 1
    keep = {k for k, n in nodes.items() if n["kind"] == "customer" or degree[k] >= 2 or
            (f"C:{customer_ref}", k) in edges}
    e_out = [e for (s, t), e in edges.items() if s in keep and t in keep]
    shared = sorted(((nodes[k], degree[k]) for k in keep if nodes[k]["kind"] != "customer" and degree[k] >= 2),
                    key=lambda x: -x[1])
    return {"focus": customer_ref, "days": days, "nodes": [nodes[k] for k in keep][:max_nodes], "edges": e_out[:600],
            "shared": [{"kind": n["kind"], "value": n.get("value", n["label"]), "label": n["label"], "customers": d,
                        "watched": n.get("watched", False)}
                       for n, d in shared[:20]],
            "linked_customers": len([n for n in nodes.values() if n["kind"] == "customer"]) - 1}


# --------------------------------------------------------------------------- customer 360 timeline
def timeline(db: Session, tenant_id: str, space: str, customer_ref: str, limit: int = 300,
             masked: bool = False) -> list[dict]:
    out: list[dict] = []
    for t, d in db.execute(select(Transaction, Decision).outerjoin(Decision, Decision.id == Transaction.decision_id)
                           .where(Transaction.tenant_id == tenant_id, Transaction.space == space,
                                  Transaction.customer_ref == customer_ref).order_by(Transaction.ts.desc())
                           .limit(limit)).all():
        out.append({"at": t.ts, "kind": "TRANSACTION", "title": f"{t.tx_type.replace('_', ' ')} "
                    + ("" if masked else f"{t.amount:,.2f} {t.currency}"),
                    "detail": f"{t.channel or ''} · {(t.details or {}).get('beneficiary_name') or ''}".strip(" ·"),
                    "ref": t.tx_ref, "status": d.decision if d else ("HISTORY" if t.mode == "HISTORY" else ""),
                    "score": round(d.fraud_score, 2) if d else None})
    for a in db.execute(select(Alert).where(Alert.tenant_id == tenant_id, Alert.space == space,
                                            Alert.customer_ref == customer_ref)).scalars():
        out.append({"at": a.created_at, "kind": "ALERT", "title": f"{a.severity} alert", "detail": a.summary,
                    "ref": a.id, "status": a.status})
    case_ids = [l.case_id for l in db.execute(select(CaseEntityLink).where(
        CaseEntityLink.entity_type == "CUSTOMER", CaseEntityLink.entity_ref == customer_ref)).scalars()]
    for c in db.execute(select(Case).where(Case.id.in_(case_ids), Case.space == space)).scalars():
        out.append({"at": c.created_at, "kind": "CASE", "title": f"Case opened: {c.title}", "detail": c.purpose,
                    "ref": c.id, "status": c.status})
        for ev in db.execute(select(CaseEvent).where(CaseEvent.case_id == c.id)).scalars():
            if ev.action in ("CREATED",):
                continue
            out.append({"at": ev.created_at, "kind": "CASE_EVENT", "title": f"{c.id}: {ev.action.replace('_', ' ').lower()}",
                        "detail": ev.detail, "ref": c.id, "status": ev.to_status or ""})
        for n in db.execute(select(CaseNote).where(CaseNote.case_id == c.id, CaseNote.restricted.is_(False))).scalars():
            out.append({"at": n.created_at, "kind": "NOTE", "title": f"{n.kind.title()} by {n.author_name}",
                        "detail": n.body[:400], "ref": c.id, "status": ""})
    for s in db.execute(select(ScreeningMatch).where(ScreeningMatch.tenant_id == tenant_id, ScreeningMatch.space == space,
                                                     ScreeningMatch.customer_ref == customer_ref)).scalars():
        out.append({"at": s.created_at, "kind": "SCREENING", "title": f"{s.list_code} screening match",
                    "detail": f"{s.subject_name} ~ {s.matched_name} ({s.score:.2f})", "ref": s.id, "status": s.status})
    for cf in db.execute(select(CustomerConfirmation).where(CustomerConfirmation.tenant_id == tenant_id,
                                                            CustomerConfirmation.space == space,
                                                            CustomerConfirmation.customer_ref == customer_ref)).scalars():
        out.append({"at": cf.sent_at, "kind": "CONFIRMATION", "title": "SMS confirmation request",
                    "detail": f"payment {cf.tx_ref} · reply {cf.reply or '-'}", "ref": cf.id, "status": cf.status})
    for lg in db.execute(select(LoginEvent).where(LoginEvent.tenant_id == tenant_id, LoginEvent.space == space,
                                                  LoginEvent.customer_ref == customer_ref)
                         .order_by(LoginEvent.ts.desc()).limit(100)).scalars():
        out.append({"at": lg.ts, "kind": "LOGIN", "title": f"{lg.event.replace('_', ' ').lower()} ({lg.channel or 'digital'})",
                    "detail": f"{lg.device_ref or '-'} · {lg.country or '-'}" + (f" · risk {lg.risk_score:.2f}: "
                              + "; ".join(lg.risk_factors) if lg.risk_factors else ""), "ref": "",
                    "status": "OK" if lg.success else "FAILED"})
    for r in db.execute(select(CustomerReview).where(CustomerReview.tenant_id == tenant_id,
                                                     CustomerReview.space == space,
                                                     CustomerReview.customer_ref == customer_ref)).scalars():
        out.append({"at": r.completed_at or r.created_at, "kind": "REVIEW", "title": f"{r.reason.title()} KYC review",
                    "detail": r.outcome or f"due {r.due_at:%d %b %Y}", "ref": r.id, "status": r.status})
    out.sort(key=lambda e: e["at"], reverse=True)
    for e in out:
        e["at"] = e["at"].isoformat(timespec="seconds") + "Z"
    return out[:limit * 2]


__all__ = ["network", "timeline", "Optional"]
