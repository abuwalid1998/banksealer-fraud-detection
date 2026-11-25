"""Cash transaction reports (CTR) and periodic AML statistics for the regulator.

A CTR is created for every customer whose cash deposits or cash withdrawals on one business day (bank local
date) reach the reporting threshold - either in a single operation or aggregated over the day, so that cash
split into several smaller operations is reported too. The threshold defaults to the AML ``reporting_threshold``
of the active policy (the same amount the structuring indicator protects); the ``ctr`` policy section can override
it, list exempt customers and choose the operation types.

Workflow: DRAFT (generated) -> REVIEWED (AML) -> SUBMITTED (regulator reference recorded) or EXEMPT (with reason).
"""
from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET  # nosec B405 - used to build XML, never to parse input
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..models import CtrReport, Customer, Tenant, Transaction, utcnow
from .common import space_ref

DEFAULT_CTR = {"enabled": True, "threshold": None, "aggregate_daily": True, "cash_in_types": ["cash_deposit"],
               "cash_out_types": ["cash_withdrawal"], "exempt_customers": [], "lookback_days": 3}


class CtrError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def config(doc: dict) -> dict:
    c = {**DEFAULT_CTR, **(doc.get("ctr") or {})}
    if not c.get("threshold"):
        c["threshold"] = float((doc.get("aml") or {}).get("reporting_threshold", 10000))
    return c


def _tz(tenant: Tenant):
    from .demo import _tz as tz
    return tz(tenant)


def day_window(tenant: Tenant, business_date: date) -> tuple[datetime, datetime]:
    from .demo import local_to_utc
    start = datetime(business_date.year, business_date.month, business_date.day)
    tz = _tz(tenant)
    return local_to_utc(start, tz), local_to_utc(start + timedelta(days=1), tz)


def generate(db: Session, tenant: Tenant, space: str, business_date: date, actor: str = "system") -> dict:
    """Create / refresh the CTRs of one business day. Already reviewed or submitted reports are not changed."""
    from .policy import active_policy
    c = config(active_policy(db, tenant.id).document)
    if not c["enabled"]:
        return {"created": 0, "updated": 0, "skipped": "CTR generation is switched off in the policy"}
    th = float(c["threshold"])
    cash_in, cash_out = set(c["cash_in_types"]), set(c["cash_out_types"])
    start, end = day_window(tenant, business_date)
    per: dict[str, dict] = defaultdict(lambda: {"in": 0.0, "out": 0.0, "refs": [], "max": 0.0, "currency": ""})
    for t in db.execute(select(Transaction).where(Transaction.tenant_id == tenant.id, Transaction.space == space,
                                                  Transaction.ts >= start, Transaction.ts < end,
                                                  Transaction.tx_type.in_(cash_in | cash_out))).scalars():
        p = per[t.customer_ref]
        p["in" if t.tx_type in cash_in else "out"] += float(t.amount)
        p["refs"].append(t.tx_ref)
        p["max"] = max(p["max"], float(t.amount))
        p["currency"] = t.currency or tenant.base_currency
    day = business_date.isoformat()
    created = updated = 0
    exempt = {str(x).strip() for x in c.get("exempt_customers") or []}
    for ref, p in per.items():
        single = p["max"] >= th
        aggregated = c["aggregate_daily"] and (p["in"] >= th or p["out"] >= th)
        if not (single or aggregated):
            continue
        rep = db.execute(select(CtrReport).where(CtrReport.tenant_id == tenant.id, CtrReport.space == space,
                                                 CtrReport.customer_ref == ref, CtrReport.business_date == day)).scalar()
        if rep is None:
            rep = CtrReport(tenant_id=tenant.id, space=space, customer_ref=ref, business_date=day,
                            number=space_ref(db, tenant.id, space, "CTR", 6))
            db.add(rep)
            created += 1
        elif rep.status != "DRAFT":
            continue
        else:
            updated += 1
        rep.cash_in, rep.cash_out = round(p["in"], 2), round(p["out"], 2)
        rep.tx_refs, rep.currency = p["refs"], p["currency"]
        rep.trigger = "SINGLE" if single else "AGGREGATED"
        if ref in exempt:
            rep.status, rep.exempt_reason = "EXEMPT", "Customer on the CTR exemption list of the policy"
    db.commit()
    if created or updated:
        audit.record(db, tenant.id, "ctr.generate", actor=actor, resource_type="ctr", resource_id=day, space=space,
                     after={"created": created, "updated": updated, "threshold": th})
    return {"created": created, "updated": updated, "threshold": th, "business_date": day}


def run_due(db: Session) -> int:
    """Leader job: (re)generate the last few business days for every institution and workspace."""
    from .demo import local_today
    from .policy import active_policy
    n = 0
    for tenant in db.execute(select(Tenant)).scalars():
        days = int(config(active_policy(db, tenant.id).document).get("lookback_days", 3))
        today = local_today(_tz(tenant)).date()
        for space in ("LIVE", "DEMO"):
            for k in range(1, days + 1):
                n += generate(db, tenant, space, today - timedelta(days=k))["created"]
            n += generate(db, tenant, space, today)["created"]      # today so far (drafts)
    return n


def _get(db: Session, tenant_id: str, space: str, rid: str) -> CtrReport:
    rep = db.get(CtrReport, rid)
    if rep is None or rep.tenant_id != tenant_id or rep.space != space:
        raise CtrError(404, "CTR not found")
    return rep


def review(db: Session, tenant_id: str, space: str, rid: str, actor: str) -> CtrReport:
    rep = _get(db, tenant_id, space, rid)
    if rep.status != "DRAFT":
        raise CtrError(409, f"CTR is {rep.status}")
    rep.status, rep.reviewed_by = "REVIEWED", actor
    db.commit()
    audit.record(db, tenant_id, "ctr.review", actor=actor, resource_type="ctr", resource_id=rep.number, space=space)
    return rep


def exempt(db: Session, tenant_id: str, space: str, rid: str, reason: str, actor: str) -> CtrReport:
    rep = _get(db, tenant_id, space, rid)
    if rep.status == "SUBMITTED":
        raise CtrError(409, "CTR already submitted")
    if len((reason or "").strip()) < 10:
        raise CtrError(422, "give the exemption reason (at least 10 characters)")
    rep.status, rep.exempt_reason, rep.reviewed_by = "EXEMPT", reason.strip()[:200], actor
    db.commit()
    audit.record(db, tenant_id, "ctr.exempt", actor=actor, resource_type="ctr", resource_id=rep.number, space=space,
                 reason=reason)
    return rep


def submit(db: Session, tenant_id: str, space: str, ids: list[str], reference: str, actor: str) -> int:
    if len((reference or "").strip()) < 3:
        raise CtrError(422, "record the regulator's submission reference")
    n = 0
    for rid in ids:
        rep = _get(db, tenant_id, space, rid)
        if rep.status not in ("DRAFT", "REVIEWED"):
            continue
        rep.status, rep.submitted_ref, rep.submitted_at = "SUBMITTED", reference.strip()[:80], utcnow()
        rep.reviewed_by = rep.reviewed_by or actor
        n += 1
    db.commit()
    audit.record(db, tenant_id, "ctr.submit", actor=actor, resource_type="ctr", resource_id=reference, space=space,
                 after={"reports": n})
    return n


def as_dict(db: Session, rep: CtrReport, masked: bool = False) -> dict:
    from ..security import mask
    c = db.execute(select(Customer).where(Customer.tenant_id == rep.tenant_id, Customer.space == rep.space,
                                          Customer.customer_ref == rep.customer_ref)).scalar()
    return {"id": rep.id, "number": rep.number, "customer_ref": rep.customer_ref,
            "customer_name": (mask(c.display_name) if masked else c.display_name) if c else "",
            "customer_type": c.customer_type if c else "", "business_date": rep.business_date,
            "currency": rep.currency, "cash_in": rep.cash_in, "cash_out": rep.cash_out,
            "transactions": len(rep.tx_refs or []), "tx_refs": rep.tx_refs, "trigger": rep.trigger,
            "status": rep.status, "exempt_reason": rep.exempt_reason, "reviewed_by": rep.reviewed_by,
            "submitted_ref": rep.submitted_ref, "submitted_at": rep.submitted_at, "created_at": rep.created_at}


# --------------------------------------------------------------------------- exports
def _txs(db: Session, rep: CtrReport) -> list[Transaction]:
    return db.execute(select(Transaction).where(Transaction.tenant_id == rep.tenant_id, Transaction.space == rep.space,
                                                Transaction.tx_ref.in_(rep.tx_refs or [])).order_by(Transaction.ts)
                      ).scalars().all()


def to_xml(db: Session, reps: list[CtrReport], tenant: Tenant, by: str) -> bytes:
    """goAML-style CTR batch (validate against the regulator's schema version before upload)."""
    batch = ET.Element("reports")
    for rep in reps:
        c = db.execute(select(Customer).where(Customer.tenant_id == rep.tenant_id, Customer.space == rep.space,
                                              Customer.customer_ref == rep.customer_ref)).scalar()
        root = ET.SubElement(batch, "report")
        for tag, value in (("rentity_id", tenant.id), ("submission_code", "E"), ("report_code", "CTR"),
                           ("entity_reference", rep.number), ("submission_date", utcnow().isoformat(timespec="seconds")),
                           ("currency_code_local", tenant.base_currency)):
            ET.SubElement(root, tag).text = value
        ET.SubElement(ET.SubElement(root, "reporting_person"), "first_name").text = by
        ET.SubElement(root, "reason").text = (f"Cash {'deposits' if rep.cash_in else 'withdrawals'} on {rep.business_date} "
                                              f"reached the reporting threshold ({rep.trigger.lower()})")
        for t in _txs(db, rep):
            tx = ET.SubElement(root, "transaction")
            ET.SubElement(tx, "transactionnumber").text = t.tx_ref
            ET.SubElement(tx, "transaction_location").text = (t.details or {}).get("location") or t.channel or ""
            ET.SubElement(tx, "date_transaction").text = t.ts.isoformat(timespec="seconds")
            ET.SubElement(tx, "transmode_code").text = "CASH"
            ET.SubElement(tx, "amount_local").text = f"{t.amount:.2f}"
            mine = ET.SubElement(tx, "t_to_my_client" if t.tx_type == "cash_deposit" else "t_from_my_client")
            ET.SubElement(mine, "funds_code").text = "K"
            acc = ET.SubElement(mine, "to_account" if t.tx_type == "cash_deposit" else "from_account")
            ET.SubElement(acc, "account").text = rep.customer_ref
            ET.SubElement(acc, "account_name").text = c.display_name if c else ""
            ET.SubElement(mine, "country").text = (c.country if c else "") or ""
    ET.indent(batch)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(batch, encoding="utf-8")


def to_csv(db: Session, reps: list[CtrReport]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["CTR number", "Business date", "Customer", "Name", "Cash in", "Cash out", "Currency", "Transactions",
                "Trigger", "Status", "Regulator reference"])
    for rep in reps:
        d = as_dict(db, rep)
        w.writerow([d["number"], d["business_date"], d["customer_ref"], d["customer_name"], f"{d['cash_in']:.2f}",
                    f"{d['cash_out']:.2f}", d["currency"], d["transactions"], d["trigger"], d["status"],
                    d["submitted_ref"]])
    return out.getvalue().encode("utf-8-sig")


def to_pdf(db: Session, reps: list[CtrReport], tenant: Tenant, by: str, title_date: str) -> bytes:
    from .reports import Doc, branding, render_pdf
    doc = Doc("Cash Transaction Reports (CTR)", f"Business date {title_date} · {len(reps)} report(s)")
    doc.meta = {"eyebrow": "REGULATORY REPORTING"}
    total_in = sum(r.cash_in for r in reps)
    total_out = sum(r.cash_out for r in reps)
    doc.kpi("Reports", len(reps)).kpi("Cash in", f"{total_in:,.2f} {tenant.base_currency}") \
        .kpi("Cash out", f"{total_out:,.2f} {tenant.base_currency}")
    rows = []
    for rep in reps:
        d = as_dict(db, rep)
        rows.append([d["number"], d["customer_ref"], d["customer_name"], f"{d['cash_in']:,.2f}", f"{d['cash_out']:,.2f}",
                     d["transactions"], d["trigger"].title(), d["status"]])
    doc.table("Reports", ["CTR", "Customer", "Name", "Cash in", "Cash out", "Ops", "Trigger", "Status"], rows, main=True)
    for rep in reps[:40]:
        doc.table(f"{rep.number} - {rep.customer_ref}", ["Time", "Reference", "Type", "Amount", "Location"],
                  [[t.ts.strftime("%H:%M"), t.tx_ref, t.tx_type.replace("_", " "), f"{t.amount:,.2f} {t.currency}",
                    (t.details or {}).get("location") or t.channel or ""] for t in _txs(db, rep)])
    return render_pdf(doc, branding(tenant), f"CTR-{title_date}", by, reps[0].space == "DEMO" if reps else False, True)


# --------------------------------------------------------------------------- regulator statistics
def statistics(db: Session, tenant_id: str, space: str, start: datetime, end: datetime) -> dict:
    """Periodic AML statistics for the regulator (monthly / quarterly return)."""
    from sqlalchemy import func
    from ..models import (Alert, Case, CustomerConfirmation, CustomerReview, Decision, ScreeningMatch, StrReport)

    def count(model, *conds):
        return int(db.execute(select(func.count()).select_from(model).where(*conds)).scalar() or 0)

    tx = dict(db.execute(select(Transaction.tx_type, func.count()).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.ts >= start, Transaction.ts < end,
        Transaction.mode != "HISTORY").group_by(Transaction.tx_type)).all())
    dec = dict(db.execute(select(Decision.decision, func.count()).where(
        Decision.tenant_id == tenant_id, Decision.space == space, Decision.issued_at >= start,
        Decision.issued_at < end).group_by(Decision.decision)).all())
    alerts = dict(db.execute(select(Alert.severity, func.count()).where(
        Alert.tenant_id == tenant_id, Alert.space == space, Alert.created_at >= start, Alert.created_at < end)
        .group_by(Alert.severity)).all())
    closed_cases = db.execute(select(Case).where(Case.tenant_id == tenant_id, Case.space == space,
                                                 Case.closed_at.is_not(None), Case.closed_at >= start,
                                                 Case.closed_at < end)).scalars().all()
    days = [(c.closed_at - c.created_at).total_seconds() / 86400 for c in closed_cases if c.closed_at and c.created_at]
    strs = dict(db.execute(select(StrReport.status, func.count()).where(
        StrReport.tenant_id == tenant_id, StrReport.space == space, StrReport.created_at >= start,
        StrReport.created_at < end).group_by(StrReport.status)).all())
    ctrs = db.execute(select(CtrReport).where(CtrReport.tenant_id == tenant_id, CtrReport.space == space,
                                              CtrReport.business_date >= start.date().isoformat(),
                                              CtrReport.business_date < end.date().isoformat())).scalars().all()
    scr = dict(db.execute(select(ScreeningMatch.status, func.count()).where(
        ScreeningMatch.tenant_id == tenant_id, ScreeningMatch.space == space, ScreeningMatch.created_at >= start,
        ScreeningMatch.created_at < end).group_by(ScreeningMatch.status)).all())
    conf = dict(db.execute(select(CustomerConfirmation.status, func.count()).where(
        CustomerConfirmation.tenant_id == tenant_id, CustomerConfirmation.space == space,
        CustomerConfirmation.sent_at >= start, CustomerConfirmation.sent_at < end)
        .group_by(CustomerConfirmation.status)).all())
    risk = dict(db.execute(select(Customer.risk_category, func.count()).where(
        Customer.tenant_id == tenant_id, Customer.space == space).group_by(Customer.risk_category)).all())
    reviews_done = count(CustomerReview, CustomerReview.tenant_id == tenant_id, CustomerReview.space == space,
                         CustomerReview.completed_at.is_not(None), CustomerReview.completed_at >= start,
                         CustomerReview.completed_at < end)
    reviews_overdue = count(CustomerReview, CustomerReview.tenant_id == tenant_id, CustomerReview.space == space,
                            CustomerReview.status != "COMPLETED", CustomerReview.due_at < end)
    return {
        "period": {"from": start.date().isoformat(), "to": (end - timedelta(days=1)).date().isoformat()},
        "transactions": {"total": sum(tx.values()), "by_type": tx},
        "decisions": dec,
        "alerts": {"total": sum(alerts.values()), "by_severity": alerts},
        "cases": {"opened": count(Case, Case.tenant_id == tenant_id, Case.space == space, Case.created_at >= start,
                                  Case.created_at < end),
                  "closed": len(closed_cases),
                  "avg_days_to_close": round(sum(days) / len(days), 1) if days else None,
                  "open_at_end": count(Case, Case.tenant_id == tenant_id, Case.space == space, Case.created_at < end,
                                       Case.closed_at.is_(None))},
        "str": {"total": sum(strs.values()), "by_status": strs},
        "ctr": {"total": len(ctrs), "submitted": sum(1 for r in ctrs if r.status == "SUBMITTED"),
                "exempt": sum(1 for r in ctrs if r.status == "EXEMPT"),
                "cash_in": round(sum(r.cash_in for r in ctrs), 2), "cash_out": round(sum(r.cash_out for r in ctrs), 2)},
        "screening": {"total": sum(scr.values()), "by_status": scr},
        "confirmations": conf,
        "customers_by_risk": risk,
        "kyc_reviews": {"completed": reviews_done, "overdue": reviews_overdue},
    }


__all__ = ["generate", "run_due", "review", "exempt", "submit", "as_dict", "to_xml", "to_csv", "to_pdf", "statistics",
           "config", "CtrError", "Optional"]
