"""Suspicious transaction reports (STR) for the Financial Follow-Up Unit (FFU).

A report is prepared from a case: reporting entity, subjects (customers and counterparties), the linked
transactions, the indicators, the narrative (generated from the case facts or by the AI assistant, then edited
by the analyst) and the action taken. Workflow: DRAFT -> APPROVED (AML Manager / authorised compliance officer)
-> SUBMITTED (FFU reference recorded). Exports: a branded PDF and an XML file structured like the goAML
schema (report / transaction / t_from / t_to / reason / action). The XML must be validated against the schema
version the FFU publishes before it is uploaded; field names can be adapted in ``to_xml``.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Alert, Case, CaseEntityLink, Customer, Decision, StrReport, Tenant, Transaction, utcnow
from .common import next_seq


class StrError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _links(db: Session, case: Case, kind: str) -> list[str]:
    return [l.entity_ref for l in db.execute(select(CaseEntityLink).where(
        CaseEntityLink.case_id == case.id, CaseEntityLink.entity_type == kind)).scalars()]


def draft(db: Session, case: Case, actor: str) -> StrReport:
    from . import ai_assist
    existing = db.execute(select(StrReport).where(StrReport.case_id == case.id,
                                                  StrReport.status != "SUBMITTED")).scalar()
    if existing:
        return existing
    customers = db.execute(select(Customer).where(Customer.tenant_id == case.tenant_id, Customer.space == case.space,
                                                  Customer.customer_ref.in_(_links(db, case, "CUSTOMER")))).scalars()
    txs = db.execute(select(Transaction, Decision).outerjoin(Decision, Decision.id == Transaction.decision_id).where(
        Transaction.tenant_id == case.tenant_id, Transaction.space == case.space,
        Transaction.tx_ref.in_(_links(db, case, "TRANSACTION"))).order_by(Transaction.ts)).all()
    alerts = db.execute(select(Alert).where(Alert.case_id == case.id)).scalars().all()
    indicators = sorted({c for _, d in txs if d for c in (d.reason_codes or [])} |
                        {c for a in alerts for c in (a.reason_codes or [])})
    subjects = [{"role": "SUBJECT", "type": c.customer_type, "ref": c.customer_ref, "name": c.display_name,
                 "country": c.country, "activity": c.business_activity, "risk": c.risk_category,
                 "national_id": (c.attributes or {}).get("national_id", ""),
                 "phone": (c.attributes or {}).get("phone", "")} for c in customers]
    for t, _ in txs:
        name = (t.details or {}).get("beneficiary_name")
        if t.beneficiary and not any(s.get("account") == t.beneficiary for s in subjects):
            subjects.append({"role": "ORIGINATOR" if t.tx_type == "incoming_transfer" else "COUNTERPARTY",
                             "type": "UNKNOWN", "name": name or "", "account": t.beneficiary,
                             "country": t.beneficiary_country or ""})
    transactions = [{"ref": t.tx_ref, "date": t.ts.isoformat(timespec="seconds"), "type": t.tx_type,
                     "amount": t.amount, "currency": t.currency, "channel": t.channel, "from_customer": t.customer_ref,
                     "counterparty": t.beneficiary, "counterparty_country": t.beneficiary_country,
                     "decision": d.decision if d else None, "execution": t.execution_state,
                     "purpose": (t.details or {}).get("purpose", "")} for t, d in txs]
    year = utcnow().year
    rid = f"{case.tenant_id}-STR-{year}-{next_seq(db, case.tenant_id, f'str:{year}'):05d}"
    text = ai_assist.narrative(db, case, "str")
    action = (case.disposition or {}).get("label") or "Under review by the AML team"
    rep = StrReport(id=rid, tenant_id=case.tenant_id, space=case.space, case_id=case.id,
                    reason="; ".join(indicators[:8]) or case.purpose[:500], narrative=text["text"],
                    action_taken=action, indicators=indicators, transactions=transactions, subjects=subjects,
                    prepared_by=actor)
    db.add(rep)
    db.commit()
    return rep


def update(db: Session, rep: StrReport, data: dict) -> StrReport:
    if rep.status != "DRAFT":
        raise StrError(409, f"report is {rep.status}; only drafts can be edited")
    for k in ("reason", "narrative", "action_taken", "report_code"):
        if k in data and data[k] is not None:
            setattr(rep, k, str(data[k])[:20000])
    rep.updated_at = utcnow()
    db.commit()
    return rep


def approve(db: Session, rep: StrReport, actor: str) -> StrReport:
    if rep.status != "DRAFT":
        raise StrError(409, f"report is already {rep.status}")
    if len(rep.narrative.strip()) < 50:
        raise StrError(422, "the narrative must describe the suspicion (at least 50 characters)")
    rep.status, rep.approved_by, rep.approved_at = "APPROVED", actor, utcnow()
    db.commit()
    return rep


def submit(db: Session, rep: StrReport, reference: str) -> StrReport:
    if rep.status != "APPROVED":
        raise StrError(409, "approve the report before recording its submission")
    if not reference.strip():
        raise StrError(422, "enter the FFU submission reference")
    rep.status, rep.submitted_ref, rep.submitted_at = "SUBMITTED", reference.strip()[:80], utcnow()
    case = db.get(Case, rep.case_id)
    if case is not None:
        case.escalation = "STR_SUBMITTED"
    db.commit()
    return rep


def to_xml(rep: StrReport, tenant: Tenant) -> bytes:
    """goAML-style XML (validate against the FFU's published schema before upload)."""
    root = ET.Element("report")
    for tag, value in (("rentity_id", tenant.id), ("rentity_branch", ""), ("submission_code", "E"),
                       ("report_code", rep.report_code or "STR"), ("entity_reference", rep.id),
                       ("submission_date", (rep.approved_at or utcnow()).isoformat(timespec="seconds")),
                       ("currency_code_local", tenant.base_currency)):
        ET.SubElement(root, tag).text = value
    rp = ET.SubElement(root, "reporting_person")
    ET.SubElement(rp, "first_name").text = rep.approved_by or rep.prepared_by
    ET.SubElement(root, "reason").text = rep.reason
    ET.SubElement(root, "action").text = rep.action_taken
    subj = {s.get("ref") or s.get("account"): s for s in rep.subjects}
    for t in rep.transactions:
        tx = ET.SubElement(root, "transaction")
        ET.SubElement(tx, "transactionnumber").text = t["ref"]
        ET.SubElement(tx, "transaction_location").text = t.get("channel") or ""
        ET.SubElement(tx, "transaction_description").text = (t.get("purpose") or t["type"]).replace("_", " ")
        ET.SubElement(tx, "date_transaction").text = t["date"]
        ET.SubElement(tx, "transmode_code").text = t["type"].upper()
        ET.SubElement(tx, "amount_local").text = f"{t['amount']:.2f}"
        incoming = t["type"] == "incoming_transfer"
        own = subj.get(t["from_customer"], {})
        other = subj.get(t.get("counterparty"), {})
        sides = (("t_from", other, t.get("counterparty")), ("t_to", own, t["from_customer"])) if incoming else \
            (("t_from", own, t["from_customer"]), ("t_to", other, t.get("counterparty")))
        for tag, party, account in sides:
            side = ET.SubElement(tx, f"{tag}_my_client" if party.get("role") == "SUBJECT" else tag)
            ET.SubElement(side, "funds_code").text = "K" if t["type"].startswith("cash") else "E"
            acc = ET.SubElement(side, "from_account" if tag == "t_from" else "to_account")
            ET.SubElement(acc, "account").text = account or ""
            ET.SubElement(acc, "account_name").text = party.get("name", "")
            ET.SubElement(side, "country").text = party.get("country") or t.get("counterparty_country") or ""
    ET.SubElement(root, "report_indicators").text = " ".join(rep.indicators or [])
    narrative = ET.SubElement(root, "narrative")
    narrative.text = rep.narrative
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


def to_pdf(rep: StrReport, tenant: Tenant, by: str) -> bytes:
    from .reports import Doc, branding, render_pdf
    doc = Doc("Suspicious Transaction Report (STR)", f"Case {rep.case_id} · status {rep.status}")
    doc.meta = {"eyebrow": "FOR THE FINANCIAL FOLLOW-UP UNIT", "STR reference": rep.id,
                "FFU submission": rep.submitted_ref or "not yet submitted"}
    doc.text("Reason for suspicion", rep.reason or "-")
    doc.text("Narrative", rep.narrative)
    doc.table("Subjects", ["Role", "Name", "Reference / account", "Type", "Country"],
              [[s.get("role"), s.get("name"), s.get("ref") or s.get("account"), s.get("type"), s.get("country")]
               for s in rep.subjects])
    doc.table("Transactions", ["Date", "Reference", "Type", "Amount", "Counterparty", "Decision", "Execution"],
              [[t["date"][:16], t["ref"], t["type"].replace("_", " "), f"{t['amount']:,.2f} {t['currency']}",
                t.get("counterparty") or "", t.get("decision") or "", t.get("execution") or ""]
               for t in rep.transactions], main=True)
    doc.text("Action taken by the institution", rep.action_taken or "-")
    doc.text("Approval", f"Prepared by {rep.prepared_by}. Approved by {rep.approved_by or '-'} "
                         f"{('on ' + rep.approved_at.strftime('%d %b %Y %H:%M') + ' UTC') if rep.approved_at else ''}.")
    return render_pdf(doc, branding(tenant), rep.id, by, rep.space == "DEMO", True)


__all__ = ["draft", "update", "approve", "submit", "to_xml", "to_pdf", "StrError", "Optional"]
