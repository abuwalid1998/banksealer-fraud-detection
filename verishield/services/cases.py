"""AML alert & case management (BRD sections 7.3, 7.4, 10).

Case lifecycle::

    NEW -> TRIAGED -> ASSIGNED -> IN_PROGRESS -> RECOMMENDATION_SUBMITTED -> MANAGER_REVIEW
        -> (RETURNED_FOR_MORE_INFO | APPROVED_DISPOSITION) -> CLOSED

The Employee investigates and recommends; only the AML Manager records the final internal
disposition. Case status is independent of the payment execution state.
"""
from __future__ import annotations

import hashlib
import mimetypes
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import audit
from ..deps import Principal
from ..models import (Alert, Case, CaseEntityLink, CaseEvent, CaseNote, Customer, EvidenceReference, Tenant,
                      Transaction, User, utcnow)
from ..settings import settings
from .common import row_dict, space_ref

STATUSES = ("NEW", "TRIAGED", "ASSIGNED", "IN_PROGRESS", "RECOMMENDATION_SUBMITTED", "MANAGER_REVIEW",
            "RETURNED_FOR_MORE_INFO", "APPROVED_DISPOSITION", "CLOSED")
OPEN_STATUSES = STATUSES[:-1]
PRIORITIES = ("LOW", "MEDIUM", "HIGH", "URGENT")
SLA_DAYS = {"URGENT": 1, "HIGH": 3, "MEDIUM": 7, "LOW": 14}
RECOMMENDATIONS = {
    "NO_CONCERN": "No concern - close with rationale",
    "MORE_INFORMATION": "Additional information required",
    "ENHANCED_MONITORING": "Continue monitoring / enhanced due diligence",
    "MAINTAIN_HOLD": "Maintain hold pending further review",
    "RECOMMEND_RELEASE": "Recommend release",
    "RECOMMEND_DECLINE": "Recommend decline / reject",
    "ESCALATE": "Escalate to AML Manager / Chief / Legal or Compliance Committee",
    "ASSESS_STR": "Recommend authorised compliance officer assess an STR/SAR filing",
}
DISPOSITIONS = {
    "CLOSE_NO_CONCERN": "Close as no concern",
    "CLOSE_FALSE_POSITIVE": "Close as false positive / model alert",
    "ENHANCED_MONITORING": "Continue enhanced monitoring",
    "RETURN_TO_ANALYST": "Return to analyst for more work",
    "RELEASE_TRANSACTION": "Release transaction (through bank integration if still pending)",
    "REJECT_TRANSACTION": "Reject transaction (through bank integration if still pending)",
    "CONFIRM_FRAUD": "Confirm internal fraud classification",
    "ESCALATE_STR_ASSESSMENT": "Escalate for formal suspicious activity reporting assessment",
}
ALERT_CLOSE_REASONS = {"NO_CONCERN": "No concern after review", "FALSE_POSITIVE": "False positive / model alert",
                       "DUPLICATE": "Duplicate of another alert", "KNOWN_ACTIVITY": "Known / explained activity"}
DEFAULT_CHECKLIST = [
    "Review customer profile, KYC and expected activity",
    "Review linked transactions and recent activity",
    "Check beneficiary / counterparty history",
    "Assess source of funds and purpose",
    "Identify related parties and linked alerts",
    "Document findings and evidence references",
]
ALLOWED_EVIDENCE = {".pdf", ".png", ".jpg", ".jpeg", ".txt", ".csv", ".xlsx", ".docx", ".eml", ".msg", ".json"}
MAX_EVIDENCE_BYTES = 10 * 1024 * 1024


class CaseError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# --------------------------------------------------------------------------- access
def _user_names(db: Session, ids: set) -> dict:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: u.full_name for u in db.execute(select(User).where(User.id.in_(ids))).scalars()}


def can_view_case(p: Principal, case: Case) -> bool:
    if case.tenant_id != p.tenant_id or case.space != p.space:
        return False
    if p.can("cases.view_all"):
        return True
    return p.can("cases.view_assigned") and case.assignee_id == p.user_id


def get_case(db: Session, p: Principal, case_id: str) -> Case:
    case = db.get(Case, case_id)
    if case is None or not can_view_case(p, case):
        raise CaseError(404, "case not found")
    return case


def assigned_entity_refs(db: Session, p: Principal) -> dict:
    """Transactions and customers an AML Employee may see (only through assigned, open-or-closed cases)."""
    case_ids = [c for (c,) in db.execute(select(Case.id).where(Case.tenant_id == p.tenant_id, Case.space == p.space,
                                                               Case.assignee_id == p.user_id))]
    if not case_ids:
        return {"tx": set(), "customers": set(), "cases": []}
    links = db.execute(select(CaseEntityLink).where(CaseEntityLink.case_id.in_(case_ids))).scalars().all()
    tx = {l.entity_ref for l in links if l.entity_type == "TRANSACTION"}
    customers = {l.entity_ref for l in links if l.entity_type == "CUSTOMER"}
    if tx:
        customers |= {c for (c,) in db.execute(select(Transaction.customer_ref).where(
            Transaction.tenant_id == p.tenant_id, Transaction.space == p.space, Transaction.tx_ref.in_(tx)))}
    return {"tx": tx, "customers": customers, "cases": case_ids}


# --------------------------------------------------------------------------- serialisation
def serialize(db: Session, case: Case, detail: bool = False, p: Optional[Principal] = None) -> dict:
    names = _user_names(db, {case.assignee_id, case.owner_id, case.created_by})
    data = row_dict(case)
    data["assignee_name"] = names.get(case.assignee_id, "")
    data["owner_name"] = names.get(case.owner_id, "")
    now = utcnow()
    data["sla_breached"] = bool(case.due_at and case.status in OPEN_STATUSES and case.due_at < now)
    data["age_days"] = round((now - case.created_at).total_seconds() / 86400, 1)
    data["hours_to_due"] = round((case.due_at - now).total_seconds() / 3600, 1) if case.due_at else None
    links = db.execute(select(CaseEntityLink).where(CaseEntityLink.case_id == case.id)).scalars().all()
    data["links"] = [{"type": l.entity_type, "ref": l.entity_ref, "note": l.note} for l in links]
    data["customer_refs"] = sorted({l.entity_ref for l in links if l.entity_type == "CUSTOMER"})
    data["checklist_done"] = sum(1 for i in case.checklist or [] if i.get("done"))
    data["checklist_total"] = len(case.checklist or [])
    if not detail:
        return data
    restricted_ok = p is None or p.can("cases.view_all")
    data["events"] = [row_dict(e) for e in db.execute(select(CaseEvent).where(CaseEvent.case_id == case.id)
                                                      .order_by(CaseEvent.id)).scalars()]
    data["notes"] = [row_dict(n) for n in db.execute(select(CaseNote).where(CaseNote.case_id == case.id)
                                                     .order_by(CaseNote.id)).scalars()
                     if restricted_ok or not n.restricted or n.author_id == (p.user_id if p else None)]
    data["evidence"] = [row_dict(e, exclude=("storage_key",)) for e in db.execute(
        select(EvidenceReference).where(EvidenceReference.case_id == case.id).order_by(EvidenceReference.created_at))
        .scalars()]
    alert_ids = [l.entity_ref for l in links if l.entity_type == "ALERT"]
    data["alerts"] = [row_dict(a) for a in db.execute(select(Alert).where(Alert.id.in_(alert_ids))).scalars()] \
        if alert_ids else []
    data["recommendation_options"] = RECOMMENDATIONS
    data["disposition_options"] = DISPOSITIONS
    return data


def _event(db: Session, case: Case, actor: Any, action: str, detail: str = "", from_status: str = "",
           to_status: str = "") -> None:
    db.add(CaseEvent(case_id=case.id, actor_id=getattr(actor, "user_id", None),
                     actor_name=getattr(actor, "display", str(actor)), action=action, detail=detail,
                     from_status=from_status, to_status=to_status))


def _move(db: Session, case: Case, actor: Any, to: str, action: str, detail: str = "") -> None:
    before = case.status
    case.status = to
    case.updated_at = utcnow()
    _event(db, case, actor, action, detail, before, to)


# --------------------------------------------------------------------------- alerts
def close_alert(db: Session, p: Principal, alert_id: str, reason_code: str, note: str) -> Alert:
    alert = db.get(Alert, alert_id)
    if alert is None or alert.tenant_id != p.tenant_id or alert.space != p.space:
        raise CaseError(404, "alert not found")
    if alert.status == "CLOSED":
        raise CaseError(409, "alert already closed")
    if alert.status == "IN_CASE":
        raise CaseError(409, "alert is part of a case; dispose the case instead")
    if reason_code not in ALERT_CLOSE_REASONS:
        raise CaseError(422, f"reason must be one of {', '.join(ALERT_CLOSE_REASONS)}")
    if not note.strip():
        raise CaseError(422, "a closing rationale is required")
    alert.status = "CLOSED"
    alert.closed_reason = f"{ALERT_CLOSE_REASONS[reason_code]}: {note.strip()}"
    alert.closed_by = p.user_id
    alert.closed_at = utcnow()
    audit.record(db, p.tenant_id, "alert.close", actor=p, resource_type="alert", resource_id=alert.id,
                 after={"reason": reason_code, "note": note}, commit=False)
    db.commit()
    return alert


def triage_alert(db: Session, p: Principal, alert_id: str, note: str = "") -> Alert:
    alert = db.get(Alert, alert_id)
    if alert is None or alert.tenant_id != p.tenant_id or alert.space != p.space:
        raise CaseError(404, "alert not found")
    if alert.status != "OPEN":
        raise CaseError(409, f"alert is {alert.status}")
    alert.status = "TRIAGED"
    audit.record(db, p.tenant_id, "alert.triage", actor=p, resource_type="alert", resource_id=alert.id,
                 after={"note": note}, commit=False)
    db.commit()
    return alert


def create_manual_alert(db: Session, p: Principal, customer_ref: str, summary: str, tx_ref: Optional[str] = None,
                        severity: str = "MEDIUM") -> Alert:
    alert = Alert(id=space_ref(db, p.tenant_id, p.space, "ALR"), tenant_id=p.tenant_id, space=p.space,
                  kind="MANUAL", tx_ref=tx_ref, customer_ref=customer_ref, decision="REVIEW", timing="POST_EVENT",
                  severity=severity, summary=summary, created_by=p.display)
    db.add(alert)
    audit.record(db, p.tenant_id, "alert.create_manual", actor=p, resource_type="alert", resource_id=alert.id,
                 after={"customer": customer_ref, "tx": tx_ref, "summary": summary}, commit=False)
    db.commit()
    return alert


# --------------------------------------------------------------------------- cases
def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "")
    return datetime.fromisoformat(text)


def create_case(db: Session, p: Principal, data: dict) -> Case:
    """Create a case or referral from alerts, transactions and/or customer profiles."""
    alert_ids = list(dict.fromkeys(data.get("alert_ids") or []))
    tx_refs = list(dict.fromkeys(data.get("tx_refs") or []))
    customer_refs = list(dict.fromkeys(data.get("customer_refs") or []))
    if not (alert_ids or tx_refs or customer_refs):
        raise CaseError(422, "select at least one alert, transaction or customer profile")
    alerts = []
    for aid in alert_ids:
        a = db.get(Alert, aid)
        if a is None or a.tenant_id != p.tenant_id or a.space != p.space:
            raise CaseError(404, f"alert {aid} not found")
        if a.status in ("IN_CASE", "CLOSED"):
            raise CaseError(409, f"alert {aid} is {a.status}")
        alerts.append(a)
        if a.tx_ref:
            tx_refs.append(a.tx_ref)
        customer_refs.append(a.customer_ref)
    for ref in set(tx_refs):
        tx = db.execute(select(Transaction).where(Transaction.tenant_id == p.tenant_id, Transaction.space == p.space,
                                                  Transaction.tx_ref == ref)).scalar()
        if tx is None:
            raise CaseError(404, f"transaction {ref} not found")
        customer_refs.append(tx.customer_ref)
    for ref in set(customer_refs):
        if db.execute(select(Customer.id).where(Customer.tenant_id == p.tenant_id, Customer.space == p.space,
                                                Customer.customer_ref == ref)).scalar() is None:
            raise CaseError(404, f"customer {ref} not found")
    priority = (data.get("priority") or "MEDIUM").upper()
    if priority not in PRIORITIES:
        raise CaseError(422, f"priority must be one of {', '.join(PRIORITIES)}")
    case_type = "REFERRAL" if data.get("referral") else ("TRANSACTION" if tx_refs else "CUSTOMER")
    due = _parse_dt(data.get("due_at")) or (utcnow() + timedelta(days=SLA_DAYS[priority]))
    first_customer = next(iter(dict.fromkeys(customer_refs)), "")
    title = data.get("title") or (f"Referral - customer {first_customer}" if case_type == "REFERRAL" else
                                  f"{'Transaction' if tx_refs else 'Customer'} investigation - {first_customer}")
    questions = [q.strip() for q in (data.get("questions") or []) if str(q).strip()]
    case = Case(id=space_ref(db, p.tenant_id, p.space, "CASE"), tenant_id=p.tenant_id, space=p.space,
                title=title[:200], case_type=case_type, priority=priority, owner_id=p.user_id, due_at=due,
                purpose=data.get("purpose") or "", questions=questions,
                classification=data.get("classification") or "AML_SUSPICION",
                window_from=_parse_dt(data.get("window_from")), window_to=_parse_dt(data.get("window_to")),
                checklist=[{"item": i, "done": False} for i in (data.get("checklist") or DEFAULT_CHECKLIST)],
                restricted=bool(data.get("restricted")), created_by=p.user_id)
    db.add(case)
    db.flush()
    for a in alerts:
        a.status, a.case_id = "IN_CASE", case.id
        db.add(CaseEntityLink(case_id=case.id, entity_type="ALERT", entity_ref=a.id))
    for ref in dict.fromkeys(tx_refs):
        db.add(CaseEntityLink(case_id=case.id, entity_type="TRANSACTION", entity_ref=ref))
    for ref in dict.fromkeys(customer_refs):
        db.add(CaseEntityLink(case_id=case.id, entity_type="CUSTOMER", entity_ref=ref))
    _event(db, case, p, "CREATED", f"{case_type.lower()} case created with {len(alerts)} alert(s), "
                                   f"{len(set(tx_refs))} transaction(s), {len(set(customer_refs))} customer(s)",
           "", "NEW")
    if data.get("assignee_id"):
        assign(db, p, case, data["assignee_id"], data.get("assignment_note", ""), commit=False)
    elif data.get("priority"):
        _move(db, case, p, "TRIAGED", "TRIAGED", f"priority {priority}")
    if not data.get("assignee_id") and data.get("auto_assign"):
        db.flush()
        auto_assign(db, case, force=True)
    audit.record(db, p.tenant_id, "case.create", actor=p, resource_type="case", resource_id=case.id,
                 after={"type": case_type, "priority": priority, "alerts": alert_ids, "tx": tx_refs,
                        "customers": customer_refs}, commit=False)
    db.commit()
    return case


def assign(db: Session, p: Principal, case: Case, assignee_id: str, note: str = "", commit: bool = True) -> Case:
    if case.status == "CLOSED":
        raise CaseError(409, "case is closed")
    user = db.get(User, assignee_id)
    if user is None or user.tenant_id != p.tenant_id or user.role != "AML_EMPLOYEE" or user.status != "ACTIVE" and \
            user.status != "INVITED":
        raise CaseError(422, "cases can only be assigned to an active AML Employee")
    previous = case.assignee_id
    case.assignee_id = user.id
    case.assigned_at = utcnow()
    target = "ASSIGNED" if case.status in ("NEW", "TRIAGED", "ASSIGNED") else case.status
    _move(db, case, p, target, "REASSIGNED" if previous else "ASSIGNED",
          f"assigned to {user.full_name}" + (f" - {note}" if note else ""))
    audit.record(db, p.tenant_id, "case.assign", actor=p, resource_type="case", resource_id=case.id,
                 before={"assignee": previous}, after={"assignee": user.id}, commit=False)
    if commit:
        db.commit()
    from . import notify
    tenant = db.get(Tenant, p.tenant_id)
    if tenant is not None:
        notify.enqueue(tenant, "case_assigned", email=user.email, phone=user.phone,
                       subject=f"Case {case.id} assigned to you",
                       text=f"{p.display} assigned AML case {case.id} ({case.priority}) to you: {case.title}. "
                            f"Due {case.due_at:%d %b %Y %H:%M} UTC." if case.due_at else
                       f"{p.display} assigned AML case {case.id} ({case.priority}) to you: {case.title}.")
    return case


def update_case(db: Session, p: Principal, case: Case, data: dict) -> Case:
    changes = {}
    if "priority" in data and data["priority"]:
        pr = data["priority"].upper()
        if pr not in PRIORITIES:
            raise CaseError(422, "invalid priority")
        changes["priority"] = (case.priority, pr)
        case.priority = pr
    if "due_at" in data and data["due_at"]:
        new_due = _parse_dt(data["due_at"])
        changes["due_at"] = (str(case.due_at), str(new_due))
        case.due_at = new_due
    if data.get("triage") and case.status == "NEW":
        _move(db, case, p, "TRIAGED", "TRIAGED")
    if changes:
        _event(db, case, p, "UPDATED", "; ".join(f"{k}: {a} -> {b}" for k, (a, b) in changes.items()))
        audit.record(db, p.tenant_id, "case.update", actor=p, resource_type="case", resource_id=case.id,
                     after={k: b for k, (_, b) in changes.items()}, commit=False)
    case.updated_at = utcnow()
    db.commit()
    return case


def _require_assignee(p: Principal, case: Case) -> None:
    if p.role == "AML_EMPLOYEE" and case.assignee_id != p.user_id:
        raise CaseError(403, "only the assigned investigator can work on this case")


def start(db: Session, p: Principal, case: Case) -> Case:
    _require_assignee(p, case)
    if case.status not in ("ASSIGNED", "RETURNED_FOR_MORE_INFO"):
        raise CaseError(409, f"cannot start investigation from {case.status}")
    _move(db, case, p, "IN_PROGRESS", "INVESTIGATION_STARTED")
    db.commit()
    return case


def add_note(db: Session, p: Principal, case: Case, body: str, kind: str = "NOTE", restricted: bool = False) -> CaseNote:
    if case.status == "CLOSED":
        raise CaseError(409, "case is closed; reopen it first")
    _require_assignee(p, case)
    if not body.strip():
        raise CaseError(422, "note is empty")
    note = CaseNote(case_id=case.id, author_id=p.user_id, author_name=p.display, kind=kind, body=body.strip(),
                    restricted=restricted)
    db.add(note)
    if p.role == "AML_EMPLOYEE" and case.status in ("ASSIGNED", "RETURNED_FOR_MORE_INFO"):
        _move(db, case, p, "IN_PROGRESS", "INVESTIGATION_STARTED", "started by adding a note")
    _event(db, case, p, "NOTE_ADDED", f"{kind.lower().replace('_', ' ')}" + (" (restricted)" if restricted else ""))
    case.updated_at = utcnow()
    db.commit()
    return note


def update_checklist(db: Session, p: Principal, case: Case, checklist: list) -> Case:
    _require_assignee(p, case)
    if case.status == "CLOSED":
        raise CaseError(409, "case is closed")
    case.checklist = [{"item": str(i.get("item", ""))[:200], "done": bool(i.get("done"))} for i in checklist]
    _event(db, case, p, "CHECKLIST_UPDATED", f"{sum(1 for i in case.checklist if i['done'])}/{len(case.checklist)} done")
    db.commit()
    return case


def add_evidence(db: Session, p: Principal, case: Case, filename: str, content: bytes, description: str = "",
                 external_reference: str = "", content_type: str = "") -> EvidenceReference:
    _require_assignee(p, case)
    if case.status == "CLOSED":
        raise CaseError(409, "case is closed")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "evidence")[:120]
    if content:
        ext = ("." + safe.rsplit(".", 1)[-1].lower()) if "." in safe else ""
        if ext not in ALLOWED_EVIDENCE:
            raise CaseError(422, f"file type {ext or '(none)'} not allowed; allowed: {', '.join(sorted(ALLOWED_EVIDENCE))}")
        if len(content) > MAX_EVIDENCE_BYTES:
            raise CaseError(413, "evidence file exceeds 10 MB")
        if content[:2] == b"MZ" or content[:4] == b"\x7fELF":
            raise CaseError(422, "executable content rejected by the upload scanner")
    elif not external_reference:
        raise CaseError(422, "provide a file or an external evidence reference")
    digest = hashlib.sha256(content or external_reference.encode()).hexdigest()
    key = ""
    if content:
        folder = settings.evidence_dir / p.tenant_id / case.id
        folder.mkdir(parents=True, exist_ok=True)
        key = f"{p.tenant_id}/{case.id}/{digest[:16]}_{safe}"
        (settings.evidence_dir / key).write_bytes(content)
    ev = EvidenceReference(case_id=case.id, filename=safe if content else external_reference[:200],
                           content_type=content_type or mimetypes.guess_type(safe)[0] or "application/octet-stream",
                           size=len(content or b""), sha256=digest, storage_key=key, description=description,
                           external_reference=external_reference, uploaded_by=p.user_id, uploaded_by_name=p.display)
    db.add(ev)
    _event(db, case, p, "EVIDENCE_ADDED", f"{ev.filename} (sha256 {digest[:12]}…)")
    audit.record(db, p.tenant_id, "case.evidence_add", actor=p, resource_type="case", resource_id=case.id,
                 after={"file": ev.filename, "sha256": digest, "size": ev.size}, commit=False)
    db.commit()
    return ev


def recommend(db: Session, p: Principal, case: Case, value: str, rationale: str, findings: str = "") -> Case:
    if p.role != "AML_EMPLOYEE" or case.assignee_id != p.user_id:
        raise CaseError(403, "only the assigned AML Employee submits the investigation recommendation")
    if case.status not in ("ASSIGNED", "IN_PROGRESS", "RETURNED_FOR_MORE_INFO"):
        raise CaseError(409, f"cannot submit a recommendation from {case.status}")
    if value not in RECOMMENDATIONS:
        raise CaseError(422, f"recommendation must be one of {', '.join(RECOMMENDATIONS)}")
    if len(rationale.strip()) < 10:
        raise CaseError(422, "a rationale of at least 10 characters is required")
    case.recommendation = {"value": value, "label": RECOMMENDATIONS[value], "rationale": rationale.strip(),
                           "findings": findings.strip(), "submitted_by": p.display, "submitted_by_id": p.user_id,
                           "submitted_at": utcnow().isoformat(timespec="seconds") + "Z"}
    case.recommended_at = utcnow()
    _move(db, case, p, "RECOMMENDATION_SUBMITTED", "RECOMMENDATION_SUBMITTED", RECOMMENDATIONS[value])
    audit.record(db, p.tenant_id, "case.recommend", actor=p, resource_type="case", resource_id=case.id,
                 after={"recommendation": value}, commit=False)
    db.commit()
    return case


def begin_review(db: Session, p: Principal, case: Case) -> Case:
    if case.status != "RECOMMENDATION_SUBMITTED":
        raise CaseError(409, f"case is {case.status}")
    _move(db, case, p, "MANAGER_REVIEW", "MANAGER_REVIEW_STARTED")
    db.commit()
    return case


def dispose(db: Session, p: Principal, case: Case, value: str, rationale: str, close: bool = True) -> dict:
    """Final internal Manager disposition. Release/reject goes through the bank's authorised integration."""
    from .decision import request_bank_action

    if not p.can("cases.dispose"):
        raise CaseError(403, "only the AML Manager records the final disposition")
    if case.status in ("CLOSED", "APPROVED_DISPOSITION"):
        raise CaseError(409, f"case is already {case.status}")
    if value not in DISPOSITIONS:
        raise CaseError(422, f"disposition must be one of {', '.join(DISPOSITIONS)}")
    if len(rationale.strip()) < 10:
        raise CaseError(422, "a Manager rationale of at least 10 characters is mandatory")
    if value == "RETURN_TO_ANALYST":
        if not case.assignee_id:
            raise CaseError(409, "case has no assigned analyst")
        _move(db, case, p, "RETURNED_FOR_MORE_INFO", "RETURNED_FOR_MORE_INFO", rationale.strip())
        audit.record(db, p.tenant_id, "case.return", actor=p, resource_type="case", resource_id=case.id,
                     after={"rationale": rationale}, commit=False)
        db.commit()
        return {"case": case, "execution": []}
    execution = []
    if value in ("RELEASE_TRANSACTION", "REJECT_TRANSACTION"):
        action = "RELEASE" if value == "RELEASE_TRANSACTION" else "REJECT"
        refs = [l.entity_ref for l in db.execute(select(CaseEntityLink).where(
            CaseEntityLink.case_id == case.id, CaseEntityLink.entity_type == "TRANSACTION")).scalars()]
        for ref in refs:
            res = request_bank_action(db, p.tenant_id, p.space, ref, action, p, rationale)
            execution.append({"tx_ref": ref, **res})
    case.disposition = {"value": value, "label": DISPOSITIONS[value], "rationale": rationale.strip(),
                        "decided_by": p.display, "decided_by_id": p.user_id,
                        "decided_at": utcnow().isoformat(timespec="seconds") + "Z", "execution": execution}
    apply_outcome_labels(db, case, value, p.display)
    if value == "CONFIRM_FRAUD":
        case.classification = "CONFIRMED_FRAUD"
    elif value == "ESCALATE_STR_ASSESSMENT":
        case.escalation = "STR_ASSESSMENT"
    _move(db, case, p, "APPROVED_DISPOSITION", "DISPOSITION_RECORDED", DISPOSITIONS[value])
    for a in db.execute(select(Alert).where(Alert.case_id == case.id)).scalars():
        a.status, a.closed_reason, a.closed_by, a.closed_at = "CLOSED", f"Case {case.id}: {DISPOSITIONS[value]}", \
            p.user_id, utcnow()
    if close:
        case.closed_at = utcnow()
        _move(db, case, p, "CLOSED", "CLOSED", "closed with Manager disposition")
    audit.record(db, p.tenant_id, "case.dispose", actor=p, resource_type="case", resource_id=case.id,
                 after={"disposition": value, "rationale": rationale, "execution": execution}, commit=False)
    db.commit()
    return {"case": case, "execution": execution}


def close(db: Session, p: Principal, case: Case) -> Case:
    if case.status != "APPROVED_DISPOSITION":
        raise CaseError(409, "record the Manager disposition before closing")
    case.closed_at = utcnow()
    _move(db, case, p, "CLOSED", "CLOSED")
    db.commit()
    return case


def reopen(db: Session, p: Principal, case: Case, reason: str) -> Case:
    if case.status != "CLOSED":
        raise CaseError(409, "only closed cases can be reopened")
    if len(reason.strip()) < 5:
        raise CaseError(422, "a reason is required to reopen a case")
    case.closed_at = None
    case.disposition = {**(case.disposition or {}), "superseded": True}
    _move(db, case, p, "ASSIGNED" if case.assignee_id else "TRIAGED", "REOPENED", reason.strip())
    audit.record(db, p.tenant_id, "case.reopen", actor=p, resource_type="case", resource_id=case.id,
                 after={"reason": reason}, commit=False)
    db.commit()
    return case


def case_query(p: Principal):
    q = select(Case).where(Case.tenant_id == p.tenant_id, Case.space == p.space)
    if not p.can("cases.view_all"):
        q = q.where(Case.assignee_id == p.user_id)
    return q


def evidence_path(ev: EvidenceReference):
    return settings.evidence_dir / ev.storage_key if ev.storage_key else None


__all__ = ["create_case", "assign", "recommend", "dispose", "close", "reopen", "add_note", "add_evidence",
           "update_checklist", "begin_review", "start", "close_alert", "triage_alert", "create_manual_alert",
           "serialize", "get_case", "case_query", "assigned_entity_refs", "CaseError", "RECOMMENDATIONS",
           "DISPOSITIONS", "ALERT_CLOSE_REASONS", "PRIORITIES", "STATUSES", "OPEN_STATUSES", "or_"]


# --------------------------------------------------------------------------- 1.3 automation & fraud reports
AUTOMATION_DEFAULTS = {"auto_assign": True, "strategy": "LEAST_LOADED", "auto_case_critical": True,
                       "escalate_hours_before_due": 24, "escalate_bump_priority": True}


def automation(tenant: Tenant) -> dict:
    return {**AUTOMATION_DEFAULTS, **((tenant.settings or {}).get("case_automation") or {})}


def system_principal(tenant_id: str, space: str, label: str = "VeriShield automation") -> Principal:
    return Principal(tenant_id=tenant_id, kind="user", role="AML_MANAGER",
                     perms={"cases.view_all", "cases.create", "cases.assign", "cases.dispose"}, space=space,
                     display=label, user_id=None)


def auto_assign(db: Session, case: Case, force: bool = False) -> Optional[User]:
    """Assign an unassigned case to the AML Employee with the lightest open workload (or round robin)."""
    tenant = db.get(Tenant, case.tenant_id)
    conf = automation(tenant)
    if case.assignee_id or not (conf["auto_assign"] or force):
        return None
    analysts = db.execute(select(User).where(User.tenant_id == case.tenant_id, User.role == "AML_EMPLOYEE",
                                             User.status.in_(("ACTIVE", "INVITED"))).order_by(User.username)
                          ).scalars().all()
    if not analysts:
        return None
    if conf["strategy"] == "ROUND_ROBIN":
        last = db.execute(select(Case.assignee_id).where(Case.tenant_id == case.tenant_id, Case.assignee_id.is_not(None))
                          .order_by(Case.assigned_at.desc()).limit(1)).scalar()
        ids = [a.id for a in analysts]
        pick = analysts[(ids.index(last) + 1) % len(ids)] if last in ids else analysts[0]
    else:
        load = dict(db.execute(select(Case.assignee_id, func.count(Case.id)).where(
            Case.tenant_id == case.tenant_id, Case.space == case.space, Case.status.in_(OPEN_STATUSES),
            Case.assignee_id.is_not(None)).group_by(Case.assignee_id)).all())
        pick = min(analysts, key=lambda a: (load.get(a.id, 0), a.username))
    assign(db, system_principal(case.tenant_id, case.space), case, pick.id,
           f"automatic assignment ({conf['strategy'].replace('_', ' ').lower()})", commit=False)
    db.commit()
    return pick


def auto_case_for_alert(db: Session, alert: Alert) -> Optional[Case]:
    tenant = db.get(Tenant, alert.tenant_id)
    if not automation(tenant)["auto_case_critical"] or alert.severity != "CRITICAL" or alert.status != "OPEN":
        return None
    case = create_case(db, system_principal(alert.tenant_id, alert.space),
                       {"alert_ids": [alert.id], "priority": "URGENT",
                        "purpose": "Opened automatically for a CRITICAL alert."})
    auto_assign(db, case)
    return case


def escalate_due(db: Session) -> int:
    """Escalate open cases close to (or past) their due date: notify the Manager and raise the priority."""
    from . import notify
    n = 0
    for tenant in db.execute(select(Tenant)).scalars():
        conf = automation(tenant)
        horizon = utcnow() + timedelta(hours=float(conf["escalate_hours_before_due"]))
        rows = db.execute(select(Case).where(Case.tenant_id == tenant.id, Case.status.in_(OPEN_STATUSES),
                                             Case.due_at.is_not(None), Case.due_at <= horizon,
                                             Case.escalation.is_(None) | (Case.escalation != "SLA_ESCALATED")
                                             )).scalars().all()
        managers = db.execute(select(User).where(User.tenant_id == tenant.id, User.role == "AML_MANAGER",
                                                 User.status == "ACTIVE")).scalars().all()
        for case in rows:
            if case.escalation in ("STR_ASSESSMENT",):
                continue
            before = case.priority
            if conf["escalate_bump_priority"] and case.priority != "URGENT":
                case.priority = PRIORITIES[min(PRIORITIES.index(case.priority) + 1, len(PRIORITIES) - 1)]
            case.escalation = "SLA_ESCALATED"
            _event(db, case, "automation", "ESCALATED",
                   f"due {case.due_at:%d %b %H:%M} UTC - priority {before} -> {case.priority}")
            for m in managers:
                notify.enqueue(tenant, "case_assigned", email=m.email, phone=m.phone,
                               subject=f"Case {case.id} escalated (SLA)",
                               text=f"Case {case.id} '{case.title}' is due {case.due_at:%d %b %Y %H:%M} UTC.")
            n += 1
        db.commit()
    return n


def apply_outcome_labels(db: Session, case: Case, value: str, actor: str) -> None:
    """Dispositions label the linked transactions - the training signal of the learning model."""
    from .signals import label_transactions, watch_transaction
    refs = [l.entity_ref for l in db.execute(select(CaseEntityLink).where(
        CaseEntityLink.case_id == case.id, CaseEntityLink.entity_type == "TRANSACTION")).scalars()]
    if value in ("CONFIRM_FRAUD", "REJECT_TRANSACTION"):
        label_transactions(db, case.tenant_id, case.space, refs, 1, "disposition")
        for tx in db.execute(select(Transaction).where(Transaction.tenant_id == case.tenant_id,
                                                       Transaction.space == case.space,
                                                       Transaction.tx_ref.in_(refs))).scalars():
            if value == "CONFIRM_FRAUD" and tx.is_outflow_row:
                watch_transaction(db, tx, f"Confirmed fraud in case {case.id}", actor, "CASE", case.id)
    elif value in ("CLOSE_FALSE_POSITIVE", "CLOSE_NO_CONCERN", "RELEASE_TRANSACTION"):
        label_transactions(db, case.tenant_id, case.space, refs, 0, "disposition")


def report_fraud(db: Session, p: Principal, data: dict) -> dict:
    """Customer-reported fraud (branch / call centre): case, labels, watch list and optional reject."""
    from .decision import request_bank_action
    from .signals import label_transactions, watch_transaction
    customer_ref = str(data.get("customer_ref") or "").strip()
    tx_refs = [t for t in (data.get("tx_refs") or []) if t]
    if not customer_ref and not tx_refs:
        raise CaseError(422, "a customer or at least one transaction is required")
    description = str(data.get("description") or "").strip()
    if len(description) < 10:
        raise CaseError(422, "describe what the customer reported (at least 10 characters)")
    txs = db.execute(select(Transaction).where(Transaction.tenant_id == p.tenant_id, Transaction.space == p.space,
                                               Transaction.tx_ref.in_(tx_refs))).scalars().all() if tx_refs else []
    if len(txs) != len(set(tx_refs)):
        raise CaseError(404, "one or more transactions were not found")
    customer_ref = customer_ref or txs[0].customer_ref
    channel = str(data.get("reported_via") or "CALL_CENTRE").upper()
    sp = system_principal(p.tenant_id, p.space, f"Fraud report recorded by {p.display}")
    case = create_case(db, sp, {"tx_refs": tx_refs, "customer_refs": [customer_ref], "priority": "URGENT",
                               "title": f"Customer-reported fraud - {customer_ref}",
                               "purpose": f"Reported via {channel.replace('_', ' ').lower()}: {description}",
                               "classification": "CUSTOMER_REPORTED_FRAUD"})
    label_transactions(db, p.tenant_id, p.space, tx_refs, 1, "customer_report")
    watched, actions = [], []
    for tx in txs:
        if data.get("watchlist", True) and tx.is_outflow_row:
            watched += [w.value for w in watch_transaction(db, tx, f"Customer reported fraud (case {case.id})",
                                                           p.display, "CUSTOMER_REPORT", case.id)]
        if data.get("reject_pending", True) and tx.execution_state in ("PENDING", "HELD"):
            actions.append(request_bank_action(db, p.tenant_id, p.space, tx.tx_ref, "REJECT", p,
                                               "Customer reported the payment as fraud"))
    auto_assign(db, case, force=True)
    db.add(CaseNote(case_id=case.id, author_id=p.user_id or "system", author_name=p.display, kind="FINDING",
                    body=f"Customer report ({channel}): {description}"))
    audit.record(db, p.tenant_id, "fraud.customer_report", actor=p, resource_type="case", resource_id=case.id,
                 space=p.space, after={"tx": tx_refs, "watchlist": watched, "channel": channel})
    db.commit()
    return {"case_id": case.id, "watchlisted": watched, "bank_actions": actions}
