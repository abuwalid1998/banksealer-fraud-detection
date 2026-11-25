"""1.3 endpoints: screening, watch list, customer confirmations, fraud reports, network & timeline, STR, AI summary,
login events, periodic reviews, scheduled reports, data quality, archiving, value, challenger, learned model,
what-if, case automation and Active Directory settings."""
from __future__ import annotations

import copy
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import Principal, require, require_user
from ..models import (Alert, Case, Customer, CustomerConfirmation, CustomerReview, LoginEvent, ModelVersion,
                      ReportSchedule, SanctionEntry, ScreeningMatch, StrReport, Tenant, User, WatchlistEntry, utcnow)
from ..services import cases as case_service
from ..services.common import row_dict
from ..settings import settings

router = APIRouter(prefix="/api/v1", tags=["intelligence"])


def _bad(status: int, message: str, code: str = "INVALID"):
    return HTTPException(status, {"code": code, "message": message})


def _visible_customer(db: Session, p: Principal, ref: str) -> Customer:
    from .core import _visible_customer as vc
    return vc(db, p, ref)


# =========================================================================== screening
@router.get("/screening/lists", tags=["screening"])
def screening_lists(p: Principal = Depends(require("screening.manage", "screening.view")), db: Session = Depends(get_db)):
    from ..services.screening import LIST_CODES
    rows = db.execute(select(SanctionEntry.list_code, SanctionEntry.list_name, func.count(SanctionEntry.id),
                             func.max(SanctionEntry.loaded_at)).where(SanctionEntry.tenant_id == p.tenant_id)
                      .group_by(SanctionEntry.list_code, SanctionEntry.list_name)).all()
    return {"lists": [{"code": c, "name": n, "entries": k, "loaded_at": t.isoformat() + "Z" if t else None}
                      for c, n, k, t in rows], "codes": LIST_CODES,
            "formats": {"csv": "CSV: name, aliases (; separated), type, country, birth_date, identifiers, ref",
                        "ofac_sdn": "OFAC SDN list - sdn.csv", "un_xml": "UN consolidated list - XML",
                        "eu_xml": "EU consolidated financial sanctions - XML"},
            "can_manage": p.can("screening.manage")}


@router.post("/screening/lists", tags=["screening"])
async def screening_import(list_code: str = Form(...), fmt: str = Form("csv"), list_name: str = Form(""),
                           replace: bool = Form(True), file: UploadFile = File(...),
                           p: Principal = Depends(require_user("screening.manage")), db: Session = Depends(get_db)):
    from ..services import screening
    content = await file.read()
    if len(content) > 60 * 1024 * 1024:
        raise _bad(413, "list file too large (60 MB max)")
    try:
        res = screening.import_list(db, p.tenant_id, list_code.upper(), fmt, content, list_name, replace)
    except ValueError as exc:
        raise _bad(422, str(exc)) from exc
    audit.record(db, p.tenant_id, "screening.list_import", actor=p, resource_type="sanctions_list",
                 resource_id=list_code.upper(), after={**res, "file": file.filename})
    return res


@router.post("/screening/run", tags=["screening"])
def screening_run(body: dict = None, p: Principal = Depends(require_user("screening.manage")),
                  db: Session = Depends(get_db)):
    from ..services import screening
    res = screening.screen_customers(db, p.tenant_id, p.space, float((body or {}).get("min_score") or 0.88))
    audit.record(db, p.tenant_id, "screening.run", actor=p, resource_type="screening", space=p.space, after=res)
    return res


@router.post("/screening/check", tags=["screening"])
def screening_check(body: dict, p: Principal = Depends(require("screening.manage", "screening.view")),
                    db: Session = Depends(get_db)):
    from ..services import screening
    name = str(body.get("name") or "").strip()
    if len(name) < 3:
        raise _bad(422, "enter a name (3 characters or more)")
    matches = screening.screen_name(db, p.tenant_id, name, float(body.get("min_score") or 0.8),
                                    [body["identifier"]] if body.get("identifier") else [])
    audit.record(db, p.tenant_id, "screening.check", actor=p, resource_type="screening",
                 after={"name": name, "matches": len(matches)})
    return {"name": name, "normalised": screening.normalise(name), "matches": matches[:20]}


@router.get("/screening/matches", tags=["screening"])
def screening_matches(status: str = "OPEN", p: Principal = Depends(require("screening.manage", "screening.view")),
                      db: Session = Depends(get_db)):
    q = select(ScreeningMatch).where(ScreeningMatch.tenant_id == p.tenant_id, ScreeningMatch.space == p.space)
    if status:
        q = q.where(ScreeningMatch.status == status)
    rows = db.execute(q.order_by(ScreeningMatch.score.desc(), ScreeningMatch.created_at.desc()).limit(500)).scalars()
    counts = dict(db.execute(select(ScreeningMatch.status, func.count()).where(
        ScreeningMatch.tenant_id == p.tenant_id, ScreeningMatch.space == p.space).group_by(ScreeningMatch.status)).all())
    return {"matches": [row_dict(m) for m in rows], "counts": counts}


@router.post("/screening/matches/{match_id}/resolve", tags=["screening"])
def screening_resolve(match_id: str, body: dict, p: Principal = Depends(require_user("screening.manage", "alerts.triage")),
                      db: Session = Depends(get_db)):
    from ..services import operations, signals
    m = db.get(ScreeningMatch, match_id)
    if m is None or m.tenant_id != p.tenant_id:
        raise _bad(404, "match not found", "NOT_FOUND")
    status = str(body.get("status") or "").upper()
    if status not in ("TRUE_MATCH", "FALSE_POSITIVE"):
        raise _bad(422, "status must be TRUE_MATCH or FALSE_POSITIVE")
    note = str(body.get("note") or "").strip()
    if len(note) < 5:
        raise _bad(422, "record why (at least 5 characters)")
    m.status, m.note, m.reviewed_by, m.reviewed_at = status, note, p.display, utcnow()
    alert = db.get(Alert, m.alert_id) if m.alert_id else None
    if status == "FALSE_POSITIVE" and alert and alert.status in ("OPEN", "TRIAGED"):
        alert.status, alert.closed_reason, alert.closed_by, alert.closed_at = \
            "CLOSED", f"Screening false positive: {note}", p.user_id, utcnow()
    if status == "TRUE_MATCH":
        if m.subject_type == "CUSTOMER":
            operations.trigger_review(db, p.tenant_id, m.space, m.customer_ref, "SCREENING", days=3)
        elif m.subject_ref:
            kind = "IBAN" if m.subject_ref[:2].isalpha() and m.subject_ref[2:4].isdigit() else "NAME"
            signals.add_to_watchlist(db, p.tenant_id, m.space, kind, m.subject_ref,
                                     f"True sanctions match {m.list_code}: {m.matched_name}", p.display,
                                     "SCREENING", m.id)
    db.commit()
    audit.record(db, p.tenant_id, "screening.resolve", actor=p, resource_type="screening_match", resource_id=m.id,
                 after={"status": status, "note": note})
    return row_dict(m)


# =========================================================================== watch list
@router.get("/watchlist", tags=["watchlist"])
def watchlist(active: bool = True, p: Principal = Depends(require("watchlist.manage", "alerts.view_all")),
              db: Session = Depends(get_db)):
    from ..services.signals import KINDS
    q = select(WatchlistEntry).where(WatchlistEntry.tenant_id == p.tenant_id, WatchlistEntry.space == p.space)
    if active:
        q = q.where(WatchlistEntry.active.is_(True))
    return {"entries": [row_dict(w) for w in db.execute(q.order_by(WatchlistEntry.created_at.desc())
                                                         .limit(1000)).scalars()],
            "kinds": KINDS, "can_manage": p.can("watchlist.manage")}


@router.post("/watchlist", status_code=201, tags=["watchlist"])
def watchlist_add(body: dict, p: Principal = Depends(require_user("watchlist.manage")), db: Session = Depends(get_db)):
    from ..services.signals import add_to_watchlist
    if len(str(body.get("reason") or "").strip()) < 5:
        raise _bad(422, "a reason is required")
    try:
        w = add_to_watchlist(db, p.tenant_id, p.space, str(body.get("kind") or ""), str(body.get("value") or ""),
                             str(body["reason"]), p.display, "MANUAL", "", str(body.get("action") or "HOLD"),
                             body.get("days"))
    except ValueError as exc:
        raise _bad(422, str(exc)) from exc
    db.commit()
    audit.record(db, p.tenant_id, "watchlist.add", actor=p, resource_type="watchlist", resource_id=w.id,
                 after={"kind": w.kind, "value": w.value, "action": w.action})
    return row_dict(w)


@router.delete("/watchlist/{entry_id}", tags=["watchlist"])
def watchlist_remove(entry_id: str, p: Principal = Depends(require_user("watchlist.manage")),
                     db: Session = Depends(get_db)):
    w = db.get(WatchlistEntry, entry_id)
    if w is None or w.tenant_id != p.tenant_id:
        raise _bad(404, "entry not found", "NOT_FOUND")
    w.active = False
    db.commit()
    audit.record(db, p.tenant_id, "watchlist.remove", actor=p, resource_type="watchlist", resource_id=w.id)
    return {"ok": True}


# =========================================================================== customer confirmations
@router.get("/confirmations", tags=["confirmations"])
def confirmations(status: str = "", p: Principal = Depends(require("alerts.view_all", "confirmations.answer")),
                  db: Session = Depends(get_db)):
    q = select(CustomerConfirmation).where(CustomerConfirmation.tenant_id == p.tenant_id,
                                           CustomerConfirmation.space == p.space)
    if status:
        q = q.where(CustomerConfirmation.status == status)
    rows = db.execute(q.order_by(CustomerConfirmation.sent_at.desc()).limit(300)).scalars().all()
    counts = dict(db.execute(select(CustomerConfirmation.status, func.count()).where(
        CustomerConfirmation.tenant_id == p.tenant_id, CustomerConfirmation.space == p.space)
        .group_by(CustomerConfirmation.status)).all())
    out = []
    for c in rows:
        d = row_dict(c, exclude=("code",))
        d["phone"] = c.phone[:-4].replace(c.phone[4:-4], "•" * max(0, len(c.phone) - 8)) + c.phone[-4:] \
            if len(c.phone) > 8 else c.phone
        out.append(d)
    return {"confirmations": out, "counts": counts, "simulated": p.space == "DEMO"}


@router.post("/confirmations/{conf_id}/answer", tags=["confirmations"])
def confirmation_answer(conf_id: str, body: dict, p: Principal = Depends(require_user("confirmations.answer")),
                        db: Session = Depends(get_db)):
    from ..services import confirmations as svc
    conf = db.get(CustomerConfirmation, conf_id)
    if conf is None or conf.tenant_id != p.tenant_id:
        raise _bad(404, "confirmation not found", "NOT_FOUND")
    try:
        return svc.answer(db, conf, str(body.get("reply") or ""),
                          actor=f"{p.display} ({'demo simulation' if p.space == 'DEMO' else 'call centre'})")
    except ValueError as exc:
        raise _bad(409, str(exc)) from exc


@router.post("/confirmations/reply", tags=["confirmations"])
def confirmation_inbound(body: dict, p: Principal = Depends(require("events.ingest", "confirmations.answer")),
                         db: Session = Depends(get_db)):
    """Inbound SMS webhook for the bank's SMS gateway: ``{"from": "+97059...", "text": "1 4821"}``."""
    from ..services import confirmations as svc
    try:
        return svc.inbound(db, p.tenant_id, str(body.get("from") or ""), str(body.get("text") or ""))
    except LookupError as exc:
        raise _bad(404, str(exc), "NOT_FOUND") from exc
    except ValueError as exc:
        raise _bad(422, str(exc)) from exc


# =========================================================================== customer fraud reports
@router.post("/fraud-reports", status_code=201, tags=["fraud reports"])
def fraud_report(body: dict, p: Principal = Depends(require_user("fraud.report")), db: Session = Depends(get_db)):
    try:
        return case_service.report_fraud(db, p, body)
    except case_service.CaseError as exc:
        raise _bad(exc.status, exc.message) from exc


# =========================================================================== network & timeline
@router.get("/customers/{ref}/network", tags=["customers"])
def customer_network(ref: str, days: int = Query(90, le=365), hops: int = Query(2, ge=1, le=3),
                     p: Principal = Depends(require("customers.view_full", "customers.view_assigned")),
                     db: Session = Depends(get_db)):
    from ..services.intel import network
    _visible_customer(db, p, ref)
    return network(db, p.tenant_id, p.space, ref, days, hops)


@router.get("/customers/{ref}/timeline", tags=["customers"])
def customer_timeline(ref: str, p: Principal = Depends(require("customers.view_full", "customers.view_masked",
                                                               "customers.view_assigned")),
                      db: Session = Depends(get_db)):
    from ..services.intel import timeline
    _visible_customer(db, p, ref)
    return {"events": timeline(db, p.tenant_id, p.space, ref, masked=not p.can("customers.view_full",
                                                                                "customers.view_assigned"))}


# =========================================================================== STR & AI narrative
def _case(db: Session, p: Principal, case_id: str) -> Case:
    try:
        return case_service.get_case(db, p, case_id)
    except case_service.CaseError as exc:
        raise _bad(exc.status, exc.message) from exc


@router.post("/cases/{case_id}/ai-summary", tags=["cases"])
def case_ai_summary(case_id: str, body: dict = None, p: Principal = Depends(require("cases.view_all", "cases.view_assigned")),
                    db: Session = Depends(get_db)):
    from ..services import ai_assist
    case = _case(db, p, case_id)
    res = ai_assist.narrative(db, case, (body or {}).get("purpose", "summary"))
    audit.record(db, p.tenant_id, "case.ai_summary", actor=p, resource_type="case", resource_id=case.id,
                 after={"source": res["source"], "model": res["model"]})
    return res


@router.post("/cases/{case_id}/str", status_code=201, tags=["str"])
def str_create(case_id: str, p: Principal = Depends(require_user("str.prepare")), db: Session = Depends(get_db)):
    from ..services import str_report
    case = _case(db, p, case_id)
    rep = str_report.draft(db, case, p.display)
    audit.record(db, p.tenant_id, "str.draft", actor=p, resource_type="str", resource_id=rep.id,
                 after={"case": case.id})
    return row_dict(rep)


def _str(db: Session, p: Principal, sid: str) -> StrReport:
    rep = db.get(StrReport, sid)
    if rep is None or rep.tenant_id != p.tenant_id or rep.space != p.space:
        raise _bad(404, "report not found", "NOT_FOUND")
    _case(db, p, rep.case_id)
    return rep


@router.get("/str", tags=["str"])
def str_list(p: Principal = Depends(require("str.prepare", "str.approve", "cases.view_all")),
             db: Session = Depends(get_db)):
    rows = db.execute(select(StrReport).where(StrReport.tenant_id == p.tenant_id, StrReport.space == p.space)
                      .order_by(StrReport.created_at.desc()).limit(300)).scalars().all()
    if not p.can("cases.view_all"):
        mine = case_service.assigned_entity_refs(db, p)
        rows = [r for r in rows if r.case_id in mine.get("cases", set()) or r.prepared_by == p.display]
    return {"reports": [{k: v for k, v in row_dict(r).items() if k not in ("transactions", "subjects")} |
                        {"transactions": len(r.transactions or []), "subjects": len(r.subjects or [])} for r in rows]}


@router.get("/str/{sid}", tags=["str"])
def str_get(sid: str, p: Principal = Depends(require("str.prepare", "str.approve", "cases.view_all")),
            db: Session = Depends(get_db)):
    return row_dict(_str(db, p, sid))


@router.put("/str/{sid}", tags=["str"])
def str_update(sid: str, body: dict, p: Principal = Depends(require_user("str.prepare", "str.approve")),
               db: Session = Depends(get_db)):
    from ..services import str_report
    rep = _str(db, p, sid)
    try:
        str_report.update(db, rep, body)
    except str_report.StrError as exc:
        raise _bad(exc.status, exc.message) from exc
    audit.record(db, p.tenant_id, "str.update", actor=p, resource_type="str", resource_id=rep.id)
    return row_dict(rep)


@router.post("/str/{sid}/{action}", tags=["str"])
def str_action(sid: str, action: str, body: dict = None, p: Principal = Depends(require_user("str.approve")),
               db: Session = Depends(get_db)):
    from ..services import str_report
    rep = _str(db, p, sid)
    try:
        if action == "approve":
            str_report.approve(db, rep, p.display)
        elif action == "submit":
            str_report.submit(db, rep, str((body or {}).get("reference") or ""))
        else:
            raise _bad(404, "unknown action", "NOT_FOUND")
    except str_report.StrError as exc:
        raise _bad(exc.status, exc.message) from exc
    audit.record(db, p.tenant_id, f"str.{action}", actor=p, resource_type="str", resource_id=rep.id,
                 after={"status": rep.status, "reference": rep.submitted_ref})
    return row_dict(rep)


@router.get("/str/{sid}/export", tags=["str"])
def str_export(sid: str, fmt: str = "xml", p: Principal = Depends(require("str.prepare", "str.approve", "cases.view_all")),
               db: Session = Depends(get_db)):
    from ..services import str_report
    rep = _str(db, p, sid)
    tenant = db.get(Tenant, p.tenant_id)
    audit.record(db, p.tenant_id, "str.export", actor=p, resource_type="str", resource_id=rep.id, after={"fmt": fmt})
    if fmt == "pdf":
        return Response(str_report.to_pdf(rep, tenant, p.display), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{rep.id}.pdf"'})
    return Response(str_report.to_xml(rep, tenant), media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{rep.id}.xml"'})


@router.get("/ai/settings", tags=["ai"])
def ai_settings(p: Principal = Depends(require("mcp.manage")), db: Session = Depends(get_db)):
    from ..services import ai_assist
    return {"config": ai_assist.public_config(db.get(Tenant, p.tenant_id)),
            "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]}


@router.put("/ai/settings", tags=["ai"])
def ai_settings_update(body: dict, p: Principal = Depends(require_user("mcp.manage")), db: Session = Depends(get_db)):
    from ..services import ai_assist
    t = db.get(Tenant, p.tenant_id)
    cur = copy.deepcopy((t.settings or {}).get("ai") or {})
    for k in ("enabled", "mask_identifiers", "fallbacks"):
        if k in body:
            cur[k] = bool(body[k])
    for k in ("model", "effort", "base_url"):
        if k in body:
            cur[k] = str(body[k] or "").strip()[:200]
    if cur.get("effort") and cur["effort"] not in ("low", "medium", "high", "xhigh", "max"):
        raise _bad(422, "effort must be low, medium, high, xhigh or max")
    if body.get("api_key"):
        from ..services.connectors import store_secret
        cur["api_key_ref"] = store_secret(db, p.tenant_id, "AI assistant API key", str(body["api_key"]), p.user_id,
                                          cur.get("api_key_ref"))
    t.settings = {**(t.settings or {}), "ai": cur}
    db.commit()
    out = ai_assist.public_config(t)
    audit.record(db, p.tenant_id, "ai.settings_update", actor=p, resource_type="tenant", resource_id=t.id, after=out)
    return {"config": out}


# =========================================================================== login events
@router.post("/logins/events", tags=["logins"])
def login_events(body: dict, p: Principal = Depends(require("events.ingest", "decisions.test")),
                 db: Session = Depends(get_db)):
    """Digital-banking session events (LOGIN, LOGIN_FAILED, PASSWORD_RESET, PHONE_CHANGE, DEVICE_ENROL)."""
    from ..services.signals import record_login
    events = body.get("events") if isinstance(body.get("events"), list) else [body]
    out = []
    for e in events[:1000]:
        try:
            ev = record_login(db, p.tenant_id, p.space, e)
            out.append({"customer_id": ev.customer_ref, "risk_score": ev.risk_score, "risk_factors": ev.risk_factors})
        except ValueError as exc:
            out.append({"error": str(exc)})
    db.commit()
    return {"accepted": sum(1 for o in out if "error" not in o), "results": out}


@router.get("/logins", tags=["logins"])
def login_list(customer_ref: str = "", risky: bool = False,
               p: Principal = Depends(require("customers.view_full", "system.view")), db: Session = Depends(get_db)):
    q = select(LoginEvent).where(LoginEvent.tenant_id == p.tenant_id, LoginEvent.space == p.space)
    if customer_ref:
        q = q.where(LoginEvent.customer_ref == customer_ref)
    if risky:
        q = q.where(LoginEvent.risk_score >= 0.5)
    return {"events": [row_dict(e) for e in db.execute(q.order_by(LoginEvent.ts.desc()).limit(300)).scalars()]}


# =========================================================================== periodic reviews
def _review(db: Session, p: Principal, rid: str) -> CustomerReview:
    r = db.get(CustomerReview, rid)
    if r is None or r.tenant_id != p.tenant_id or r.space != p.space:
        raise _bad(404, "review not found", "NOT_FOUND")
    if not p.can("reviews.manage") and r.assignee_id != p.user_id:
        raise _bad(404, "review not found", "NOT_FOUND")
    return r


@router.get("/reviews", tags=["reviews"])
def reviews(status: str = "OPEN", p: Principal = Depends(require("reviews.manage", "reviews.work")),
            db: Session = Depends(get_db)):
    q = select(CustomerReview).where(CustomerReview.tenant_id == p.tenant_id, CustomerReview.space == p.space)
    if status == "OPEN":
        q = q.where(CustomerReview.status != "COMPLETED")
    elif status:
        q = q.where(CustomerReview.status == status)
    if not p.can("reviews.manage"):
        q = q.where(CustomerReview.assignee_id == p.user_id)
    rows = db.execute(q.order_by(CustomerReview.due_at).limit(500)).scalars().all()
    names = dict(db.execute(select(Customer.customer_ref, Customer.display_name).where(
        Customer.tenant_id == p.tenant_id, Customer.space == p.space,
        Customer.customer_ref.in_([r.customer_ref for r in rows]))).all())
    users = {u.id: u.full_name for u in db.execute(select(User).where(User.tenant_id == p.tenant_id)).scalars()}
    now = utcnow()
    out = []
    for r in rows:
        d = row_dict(r)
        d.update({"customer_name": names.get(r.customer_ref, ""), "assignee_name": users.get(r.assignee_id, ""),
                  "overdue": r.status != "COMPLETED" and r.due_at < now})
        out.append(d)
    from ..services.operations import review_settings
    return {"reviews": out, "settings": review_settings(db.get(Tenant, p.tenant_id)),
            "can_manage": p.can("reviews.manage")}


@router.post("/reviews/schedule", tags=["reviews"])
def reviews_schedule(p: Principal = Depends(require_user("reviews.manage")), db: Session = Depends(get_db)):
    from ..services.operations import schedule_reviews
    n = schedule_reviews(db, p.tenant_id, p.space)
    audit.record(db, p.tenant_id, "reviews.schedule", actor=p, resource_type="reviews", after={"created": n})
    return {"created": n}


@router.post("/customers/{ref}/review", status_code=201, tags=["reviews"])
def review_trigger(ref: str, body: dict = None, p: Principal = Depends(require_user("reviews.manage")),
                   db: Session = Depends(get_db)):
    from ..services.operations import trigger_review
    try:
        r = trigger_review(db, p.tenant_id, p.space, ref, "MANUAL", int((body or {}).get("days") or 14))
    except LookupError as exc:
        raise _bad(404, str(exc), "NOT_FOUND") from exc
    db.commit()
    return row_dict(r)


@router.post("/reviews/{rid}/{action}", tags=["reviews"])
def review_action(rid: str, action: str, body: dict = None,
                  p: Principal = Depends(require_user("reviews.manage", "reviews.work")), db: Session = Depends(get_db)):
    from ..services.operations import complete_review
    r = _review(db, p, rid)
    body = body or {}
    if action == "assign":
        if not p.can("reviews.manage"):
            raise _bad(403, "only the AML Manager assigns reviews", "FORBIDDEN")
        u = db.get(User, body.get("assignee_id") or "")
        if u is None or u.tenant_id != p.tenant_id or u.role not in ("AML_EMPLOYEE", "AML_MANAGER"):
            raise _bad(422, "choose an AML Employee")
        r.assignee_id, r.status = u.id, "IN_PROGRESS"
    elif action == "checklist":
        r.checklist = [{"item": str(i.get("item"))[:200], "done": bool(i.get("done"))} for i in body.get("checklist", [])]
        if r.status == "DUE":
            r.status = "IN_PROGRESS"
    elif action == "complete":
        try:
            res = complete_review(db, r, str(body.get("outcome") or "REVIEWED"), str(body.get("new_risk") or ""),
                                  str(body.get("notes") or ""), p.display)
        except ValueError as exc:
            raise _bad(422, str(exc)) from exc
        audit.record(db, p.tenant_id, "review.complete", actor=p, resource_type="customer", resource_id=r.customer_ref,
                     after=res)
        return row_dict(r)
    else:
        raise _bad(404, "unknown action", "NOT_FOUND")
    db.commit()
    audit.record(db, p.tenant_id, f"review.{action}", actor=p, resource_type="review", resource_id=r.id)
    return row_dict(r)


# =========================================================================== scheduled reports
@router.get("/report-schedules", tags=["reports"])
def schedules(p: Principal = Depends(require("reports.system", "reports.aml", "reports.executive", "reports.board")),
              db: Session = Depends(get_db)):
    q = select(ReportSchedule).where(ReportSchedule.tenant_id == p.tenant_id, ReportSchedule.space == p.space)
    if not p.can("users.manage"):
        q = q.where(ReportSchedule.owner_id == p.user_id)
    return {"schedules": [row_dict(s) for s in db.execute(q.order_by(ReportSchedule.created_at)).scalars()]}


@router.post("/report-schedules", status_code=201, tags=["reports"])
def schedule_create(body: dict, p: Principal = Depends(require_user("reports.system", "reports.aml",
                                                                    "reports.executive", "reports.board")),
                    db: Session = Depends(get_db)):
    from ..services import reports
    from ..services.operations import next_run
    rtype = body.get("report_type")
    spec = reports.CATALOGUE.get(rtype)
    if spec is None or not p.can(*spec["perms"]) or spec.get("params"):
        raise _bad(422, "choose a report you may generate that needs no case / customer / transaction parameter")
    freq = str(body.get("frequency") or "WEEKLY").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY"):
        raise _bad(422, "frequency must be DAILY, WEEKLY or MONTHLY")
    recipients = [r.strip() for r in (body.get("recipients") or []) if "@" in str(r)][:20]
    if not recipients:
        raise _bad(422, "add at least one e-mail recipient")
    fmt = body.get("format") if body.get("format") in ("pdf", "xlsx", "csv") else "pdf"
    s = ReportSchedule(tenant_id=p.tenant_id, space=p.space, report_type=rtype, fmt=fmt, frequency=freq,
                       recipients=recipients, owner_id=p.user_id, owner_name=p.display, owner_role=p.role,
                       next_run_at=next_run(freq))
    db.add(s)
    db.commit()
    audit.record(db, p.tenant_id, "report_schedule.create", actor=p, resource_type="report_schedule",
                 resource_id=s.id, after={"report": rtype, "frequency": freq, "recipients": recipients})
    return row_dict(s)


@router.post("/report-schedules/{sid}/{action}", tags=["reports"])
def schedule_action(sid: str, action: str, p: Principal = Depends(require_user()), db: Session = Depends(get_db)):
    from ..services.operations import run_schedule
    s = db.get(ReportSchedule, sid)
    if s is None or s.tenant_id != p.tenant_id or (s.owner_id != p.user_id and not p.can("users.manage")):
        raise _bad(404, "schedule not found", "NOT_FOUND")
    if action == "run":
        s.last_status, s.last_run_at = run_schedule(db, s)[:200], utcnow()
    elif action in ("pause", "resume"):
        s.active = action == "resume"
    elif action == "delete":
        db.delete(s)
        db.commit()
        return {"ok": True}
    else:
        raise _bad(404, "unknown action", "NOT_FOUND")
    db.commit()
    audit.record(db, p.tenant_id, f"report_schedule.{action}", actor=p, resource_type="report_schedule", resource_id=sid)
    return row_dict(s)


# =========================================================================== data quality & archiving
@router.get("/connectors-quality", tags=["connectors"])
def connectors_quality(hours: int = Query(24, ge=1, le=168), p: Principal = Depends(require("connectors.view")),
                       db: Session = Depends(get_db)):
    from ..services.operations import data_quality
    return {"channels": data_quality(db, p.tenant_id, p.space, hours), "hours": hours}


@router.get("/archive", tags=["operations"])
def archive_status(p: Principal = Depends(require("tenant.manage")), db: Session = Depends(get_db)):
    from ..services.operations import archive, archives
    return {"preview": archive(db, p.tenant_id, p.space, p.display, dry_run=True), "files": archives(p.tenant_id)}


@router.post("/archive/run", tags=["operations"])
def archive_run(body: dict, p: Principal = Depends(require_user("tenant.manage")), db: Session = Depends(get_db)):
    from ..services.operations import archive
    if body.get("confirm") != "ARCHIVE":
        raise _bad(422, 'send {"confirm": "ARCHIVE"}')
    return archive(db, p.tenant_id, p.space, p.display)


# =========================================================================== value, challenger, learned model, what-if
@router.get("/value", tags=["executive"])
def value(days: int = Query(30, ge=1, le=366), p: Principal = Depends(require("executive.overview", "reports.aml",
                                                                             "reports.executive")),
          db: Session = Depends(get_db)):
    from ..services.operations import value_metrics
    return value_metrics(db, p.tenant_id, p.space, days)


@router.put("/value/assumptions", tags=["executive"])
def value_assumptions(body: dict, p: Principal = Depends(require_user("executive.overview", "policies.edit")),
                      db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    cur = dict((t.settings or {}).get("value") or {})
    for k in ("minutes_per_alert", "analyst_hourly_cost"):
        if body.get(k) is not None:
            v = float(body[k])
            if v <= 0:
                raise _bad(422, f"{k} must be positive")
            cur[k] = v
    t.settings = {**(t.settings or {}), "value": cur}
    db.commit()
    return {"assumptions": cur}


@router.get("/models/challenger", tags=["models"])
def model_challenger(p: Principal = Depends(require("models.view")), db: Session = Depends(get_db)):
    from ..services.operations import challenger
    return challenger(db, p.tenant_id, p.space)


@router.get("/models/learned", tags=["models"])
def learned_models(p: Principal = Depends(require("models.view")), db: Session = Depends(get_db)):
    from ..services.signals import labelled_counts
    from ..services.supervised import MIN_NEGATIVES, MIN_POSITIVES
    rows = db.execute(select(ModelVersion).where(ModelVersion.tenant_id == p.tenant_id, ModelVersion.space == p.space,
                                                 ModelVersion.kind == "SUPERVISED").order_by(ModelVersion.seq.desc())
                      ).scalars().all()
    return {"models": [{k: (v if k != "metrics" else {a: b for a, b in v.items() if a != "artifact"})
                        for k, v in row_dict(m, exclude=("artifact_path",)).items()} for m in rows],
            "labels": labelled_counts(db, p.tenant_id, p.space), "minimum": {"fraud": MIN_POSITIVES,
                                                                              "legitimate": MIN_NEGATIVES},
            "demo_ground_truth": p.space == "DEMO", "can_train": p.can("models.train")}


@router.post("/models/learned/{action}", tags=["models"])
def learned_action(action: str, body: dict = None, p: Principal = Depends(require_user("models.train")),
                   db: Session = Depends(get_db)):
    from ..services import supervised
    try:
        if action == "train":
            mv = supervised.train(db, p.tenant_id, p.space, p.display, activate=(body or {}).get("activate", True))
            out = {k: v for k, v in row_dict(mv, exclude=("artifact_path",)).items() if k != "metrics"}
            out["metrics"] = {a: b for a, b in mv.metrics.items() if a != "artifact"}
        elif action == "activate":
            supervised.activate_model(db, str((body or {}).get("model_id")))
            out = {"ok": True}
        elif action == "deactivate":
            supervised.deactivate(db, p.tenant_id, p.space)
            out = {"ok": True}
        else:
            raise _bad(404, "unknown action", "NOT_FOUND")
    except ValueError as exc:
        raise _bad(422, str(exc)) from exc
    audit.record(db, p.tenant_id, f"model.learned_{action}", actor=p, resource_type="model", space=p.space)
    return out


@router.get("/what-if", tags=["executive"])
def what_if(strictness: float = Query(1.0, ge=0.5, le=1.5), days: int = Query(14, ge=1, le=60),
            p: Principal = Depends(require("executive.overview", "policies.view")), db: Session = Depends(get_db)):
    from ..services.operations import what_if as svc
    return svc(db, p.tenant_id, p.space, strictness, days)


# =========================================================================== alert grouping
@router.get("/alerts/groups", tags=["alerts"])
def alert_groups(hours: int = Query(72, ge=1, le=720), p: Principal = Depends(require("alerts.view_all")),
                 db: Session = Depends(get_db)):
    """Open alerts grouped by customer and main reason - one item for the analyst instead of many."""
    rows = db.execute(select(Alert).where(Alert.tenant_id == p.tenant_id, Alert.space == p.space,
                                          Alert.status.in_(("OPEN", "TRIAGED")),
                                          Alert.created_at >= utcnow() - timedelta(hours=hours))
                      .order_by(Alert.created_at.desc())).scalars().all()
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    groups: dict = defaultdict(list)
    for a in rows:
        key = (a.customer_ref, a.kind, (a.reason_codes or ["OTHER"])[0])
        groups[key].append(a)
    names = dict(db.execute(select(Customer.customer_ref, Customer.display_name).where(
        Customer.tenant_id == p.tenant_id, Customer.space == p.space,
        Customer.customer_ref.in_({k[0] for k in groups}))).all())
    out = []
    for (cref, kind, code), items in groups.items():
        sev = min((a.severity for a in items), key=lambda s: order.get(s, 9))
        out.append({"key": f"{cref}|{kind}|{code}", "customer_ref": cref, "customer_name": names.get(cref, ""),
                    "kind": kind, "reason": code, "count": len(items), "severity": sev,
                    "max_score": round(max(a.score for a in items), 3),
                    "first": min(a.created_at for a in items).isoformat() + "Z",
                    "last": max(a.created_at for a in items).isoformat() + "Z",
                    "decisions": dict(Counter(a.decision for a in items)),
                    "codes": sorted({c for a in items for c in (a.reason_codes or [])})[:8],
                    "alert_ids": [a.id for a in items], "tx_refs": [a.tx_ref for a in items if a.tx_ref][:50]})
    out.sort(key=lambda g: (order.get(g["severity"], 9), -g["count"]))
    return {"groups": out, "alerts": len(rows), "hours": hours}


@router.post("/alerts/bulk-close", tags=["alerts"])
def alerts_bulk_close(body: dict, p: Principal = Depends(require_user("alerts.triage")), db: Session = Depends(get_db)):
    ids = list(body.get("alert_ids") or [])[:500]
    reason = str(body.get("reason") or "")
    note = str(body.get("note") or "")
    closed, errors = 0, []
    for aid in ids:
        try:
            case_service.close_alert(db, p, aid, reason, note)
            closed += 1
        except case_service.CaseError as exc:
            errors.append(f"{aid}: {exc.message}")
    return {"closed": closed, "errors": errors[:20]}


# =========================================================================== case automation & directory
@router.get("/case-automation", tags=["cases"])
def case_automation(p: Principal = Depends(require("cases.assign", "users.manage")), db: Session = Depends(get_db)):
    return {"config": case_service.automation(db.get(Tenant, p.tenant_id))}


@router.put("/case-automation", tags=["cases"])
def case_automation_update(body: dict, p: Principal = Depends(require_user("cases.assign", "users.manage")),
                           db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    cur = dict(case_service.automation(t))
    for k in ("auto_assign", "auto_case_critical", "escalate_bump_priority"):
        if k in body:
            cur[k] = bool(body[k])
    if body.get("strategy"):
        if body["strategy"] not in ("LEAST_LOADED", "ROUND_ROBIN"):
            raise _bad(422, "strategy must be LEAST_LOADED or ROUND_ROBIN")
        cur["strategy"] = body["strategy"]
    if body.get("escalate_hours_before_due") is not None:
        cur["escalate_hours_before_due"] = max(1, min(240, int(body["escalate_hours_before_due"])))
    t.settings = {**(t.settings or {}), "case_automation": cur}
    db.commit()
    audit.record(db, p.tenant_id, "cases.automation_update", actor=p, resource_type="tenant", after=cur)
    return {"config": cur}


@router.get("/directory", tags=["administration"])
def directory_settings(p: Principal = Depends(require("users.manage", "tenant.manage")), db: Session = Depends(get_db)):
    from ..services import directory
    c = directory.config(db.get(Tenant, p.tenant_id))
    try:
        import ldap3  # noqa: F401
        c["ldap_installed"] = True
    except ImportError:
        c["ldap_installed"] = False
    c["ad_users"] = db.execute(select(func.count(User.id)).where(User.tenant_id == p.tenant_id,
                                                                 User.auth_source == "AD")).scalar()
    return {"config": c}


@router.put("/directory", tags=["administration"])
def directory_update(body: dict, p: Principal = Depends(require_user("tenant.manage")), db: Session = Depends(get_db)):
    from ..services import directory
    from ..models import ROLES
    t = db.get(Tenant, p.tenant_id)
    cur = dict(directory.config(t))
    for k in ("enabled", "use_starttls", "verify_tls", "sync_roles", "allow_local_fallback"):
        if k in body:
            cur[k] = bool(body[k])
    for k in ("server_url", "bind_format", "base_dn", "user_filter", "ca_file"):
        if k in body:
            cur[k] = str(body[k] or "").strip()[:400]
    if cur["server_url"] and not cur["server_url"].lower().startswith(("ldap://", "ldaps://")):
        raise _bad(422, "server URL must start with ldaps:// (recommended) or ldap://")
    if cur["enabled"] and cur["server_url"].lower().startswith("ldap://") and not cur["use_starttls"]:
        raise _bad(422, "plain ldap:// sends passwords unencrypted - use ldaps:// or enable StartTLS")
    if isinstance(body.get("group_role_map"), dict):
        bad = [r for r in body["group_role_map"].values() if r not in ROLES]
        if bad:
            raise _bad(422, f"unknown role(s): {', '.join(bad)}")
        cur["group_role_map"] = {str(k)[:300]: v for k, v in body["group_role_map"].items()}
    t.settings = {**(t.settings or {}), "directory": cur}
    db.commit()
    audit.record(db, p.tenant_id, "directory.update", actor=p, resource_type="tenant", after=cur)
    return {"config": cur}


@router.post("/directory/test", tags=["administration"])
def directory_test(body: dict, p: Principal = Depends(require_user("tenant.manage")), db: Session = Depends(get_db)):
    from ..services import directory
    res = directory.test(db.get(Tenant, p.tenant_id), str(body.get("account") or ""), str(body.get("password") or ""))
    audit.record(db, p.tenant_id, "directory.test", actor=p, outcome="SUCCESS" if res.get("ok") else "FAILURE",
                 reason=res.get("message", ""))
    return res


@router.post("/users/{user_id}/directory", tags=["administration"])
def user_directory_link(user_id: str, body: dict, p: Principal = Depends(require_user("users.manage")),
                        db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if u is None or u.tenant_id != p.tenant_id:
        raise _bad(404, "user not found", "NOT_FOUND")
    account = str(body.get("ad_account") or "").strip()
    if body.get("auth_source") == "AD" and not account:
        raise _bad(422, "enter the domain account (sAMAccountName or UPN)")
    u.auth_source = "AD" if body.get("auth_source") == "AD" else "LOCAL"
    u.ad_account = account[:120]
    db.commit()
    audit.record(db, p.tenant_id, "user.directory_link", actor=p, resource_type="user", resource_id=u.id,
                 after={"auth_source": u.auth_source, "ad_account": u.ad_account})
    return {"auth_source": u.auth_source, "ad_account": u.ad_account}


__all__ = ["router", "Optional", "settings"]
