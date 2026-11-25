"""1.4 endpoints: cluster, security posture and key rotation, SIEM, cash transaction reports and regulator
statistics, customer risk assessment, cheques, network risk, card controls and in-app payment confirmation."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import Principal, require, require_user
from ..models import ApiClient, CardControl, CtrReport, CustomerConfirmation, RiskAssessment, Tenant, utcnow
from ..services.common import row_dict

router = APIRouter(prefix="/api/v1", tags=["platform"])


def _bad(status: int, message: str, code: str = "INVALID"):
    return HTTPException(status, {"code": code, "message": message})


def _tenant(db: Session, p: Principal) -> Tenant:
    return db.get(Tenant, p.tenant_id)


def _visible_customer(db: Session, p: Principal, ref: str):
    from .core import _visible_customer as vc
    return vc(db, p, ref)


# =========================================================================== cluster
@router.get("/system/cluster", tags=["operations"])
def cluster_status(p: Principal = Depends(require("system.view", "system.summary")), db: Session = Depends(get_db)):
    from ..services import cluster
    return cluster.summary(db)


# =========================================================================== security
@router.get("/security/posture", tags=["security"])
def security_posture(p: Principal = Depends(require_user("security.manage")), db: Session = Depends(get_db)):
    from ..services.hardening import posture
    return posture(db, _tenant(db, p))


@router.post("/security/vault/rotate", tags=["security"])
def rotate_vault(p: Principal = Depends(require_user("security.manage")), db: Session = Depends(get_db)):
    from ..security import rotate_vault_key
    out = rotate_vault_key(db)
    audit.record(db, p.tenant_id, "security.vault_rotate", actor=p, resource_type="vault", resource_id=out["primary"],
                 after=out)
    return out


@router.post("/security/vault/retire", tags=["security"])
def retire_vault_keys(p: Principal = Depends(require_user("security.manage")), db: Session = Depends(get_db)):
    from ..security import retire_old_keys
    try:
        out = retire_old_keys(db)
    except ValueError as exc:
        raise _bad(409, str(exc)) from exc
    audit.record(db, p.tenant_id, "security.vault_retire", actor=p, resource_type="vault", resource_id=out["primary"],
                 after=out)
    return out


@router.post("/api-clients/{client_id}/rotate", tags=["security"])
def rotate_api_key(client_id: str, body: Optional[dict] = None, p: Principal = Depends(require_user("api_clients.manage")),
                   db: Session = Depends(get_db)):
    """New key for a channel; the old key keeps working for ``grace_hours`` (default 24) so the bank can switch."""
    from ..security import new_api_key
    c = db.get(ApiClient, client_id)
    if c is None or c.tenant_id != p.tenant_id or not c.active:
        raise _bad(404, "API client not found", "NOT_FOUND")
    hours = max(0, min(720, int((body or {}).get("grace_hours", 24))))
    key, prefix, hashed = new_api_key(c.key_prefix)
    c.previous_key_hash, c.previous_expires_at = (c.key_hash, utcnow() + timedelta(hours=hours)) if hours else (None, None)
    c.key_hash, c.rotated_at = hashed, utcnow()
    db.commit()
    audit.record(db, p.tenant_id, "api_client.rotate", actor=p, resource_type="api_client", resource_id=c.id,
                 after={"grace_hours": hours})
    return {"client": row_dict(c, exclude=("key_hash", "previous_key_hash")), "api_key": key,
            "note": f"Copy the new key now - it is shown only once. The previous key works until "
                    f"{c.previous_expires_at:%d %b %Y %H:%M} UTC." if hours else "The previous key stopped working."}


# =========================================================================== SIEM
@router.get("/siem", tags=["monitoring"])
def siem_get(p: Principal = Depends(require_user("siem.manage")), db: Session = Depends(get_db)):
    from ..services import siem
    return siem.public(db, _tenant(db, p))


@router.put("/siem", tags=["monitoring"])
def siem_put(body: dict, p: Principal = Depends(require_user("siem.manage")), db: Session = Depends(get_db)):
    from ..services import siem
    t = _tenant(db, p)
    try:
        out = siem.update(db, t, body)
    except ValueError as exc:
        raise _bad(422, str(exc)) from exc
    audit.record(db, p.tenant_id, "siem.update", actor=p, resource_type="tenant", resource_id=t.id, after=out["config"])
    return out


@router.post("/siem/secret", tags=["monitoring"])
def siem_secret(body: dict, p: Principal = Depends(require_user("siem.manage")), db: Session = Depends(get_db)):
    import copy
    from ..services import siem
    from ..services.connectors import store_secret
    value = str(body.get("value") or "")
    if not value:
        raise _bad(422, "send {value}")
    t = _tenant(db, p)
    cur = copy.deepcopy((t.settings or {}).get("siem") or {})
    cur["token_ref"] = store_secret(db, p.tenant_id, "SIEM collector token", value, p.user_id, cur.get("token_ref"))
    t.settings = {**(t.settings or {}), "siem": cur}
    db.commit()
    audit.record(db, p.tenant_id, "siem.secret_set", actor=p, resource_type="tenant", resource_id=t.id)
    return siem.public(db, t)


@router.post("/siem/test", tags=["monitoring"])
def siem_test(p: Principal = Depends(require_user("siem.manage")), db: Session = Depends(get_db)):
    from ..services import siem
    ok, detail = siem.test(db, _tenant(db, p))
    audit.record(db, p.tenant_id, "siem.test", actor=p, resource_type="siem", resource_id="test",
                 outcome="SUCCESS" if ok else "FAILURE", reason=detail)
    return {"ok": ok, "detail": detail}


# =========================================================================== CTR & statistics
def _ctr_error(exc):
    return _bad(exc.status, exc.message)


@router.get("/ctr", tags=["regulatory"])
def ctr_list(business_date: str = "", status: str = "", p: Principal = Depends(require("ctr.view", "ctr.manage")),
             db: Session = Depends(get_db)):
    from ..services import ctr
    q = select(CtrReport).where(CtrReport.tenant_id == p.tenant_id, CtrReport.space == p.space)
    if business_date:
        q = q.where(CtrReport.business_date == business_date)
    if status:
        q = q.where(CtrReport.status == status)
    rows = db.execute(q.order_by(CtrReport.business_date.desc(), CtrReport.number.desc()).limit(500)).scalars().all()
    from ..services.policy import active_policy
    cfg = ctr.config(active_policy(db, p.tenant_id).document)
    days = [d for (d,) in db.execute(select(CtrReport.business_date).where(
        CtrReport.tenant_id == p.tenant_id, CtrReport.space == p.space).distinct()
        .order_by(CtrReport.business_date.desc()).limit(60)).all()]
    return {"reports": [ctr.as_dict(db, r) for r in rows], "threshold": cfg["threshold"],
            "aggregate_daily": cfg["aggregate_daily"], "dates": days,
            "can_manage": p.can("ctr.manage")}


@router.post("/ctr/generate", tags=["regulatory"])
def ctr_generate(body: dict, p: Principal = Depends(require_user("ctr.manage")), db: Session = Depends(get_db)):
    from ..services import ctr
    try:
        day = date.fromisoformat(str(body.get("business_date") or (utcnow() - timedelta(days=1)).date().isoformat()))
    except ValueError as exc:
        raise _bad(422, "business_date must be YYYY-MM-DD") from exc
    return ctr.generate(db, _tenant(db, p), p.space, day, p.display)


@router.post("/ctr/submit", tags=["regulatory"])
def ctr_submit(body: dict, p: Principal = Depends(require_user("ctr.manage")), db: Session = Depends(get_db)):
    from ..services import ctr
    try:
        n = ctr.submit(db, p.tenant_id, p.space, list(body.get("ids") or []), str(body.get("reference") or ""), p.display)
    except ctr.CtrError as exc:
        raise _ctr_error(exc) from exc
    return {"submitted": n}


@router.get("/ctr/export", tags=["regulatory"])
def ctr_export(fmt: str = "xml", business_date: str = "", ids: str = "",
               p: Principal = Depends(require("ctr.view", "ctr.manage")), db: Session = Depends(get_db)):
    from ..services import ctr
    q = select(CtrReport).where(CtrReport.tenant_id == p.tenant_id, CtrReport.space == p.space)
    if ids:
        q = q.where(CtrReport.id.in_([i for i in ids.split(",") if i]))
    elif business_date:
        q = q.where(CtrReport.business_date == business_date)
    q = q.where(CtrReport.status != "EXEMPT")
    reps = db.execute(q.order_by(CtrReport.number)).scalars().all()
    if not reps:
        raise _bad(404, "no reports to export", "NOT_FOUND")
    t = _tenant(db, p)
    label = business_date or reps[0].business_date
    audit.record(db, p.tenant_id, "ctr.export", actor=p, resource_type="ctr", resource_id=label,
                 after={"format": fmt, "reports": len(reps)})
    if fmt == "csv":
        return Response(ctr.to_csv(db, reps), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="CTR-{label}.csv"'})
    if fmt == "pdf":
        return Response(ctr.to_pdf(db, reps, t, p.display, label), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="CTR-{label}.pdf"'})
    return Response(ctr.to_xml(db, reps, t, p.display), media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="CTR-{label}.xml"'})


@router.post("/ctr/{rid}/{action}", tags=["regulatory"])
def ctr_action(rid: str, action: str, body: Optional[dict] = None, p: Principal = Depends(require_user("ctr.manage")),
               db: Session = Depends(get_db)):
    from ..services import ctr
    try:
        if action == "review":
            rep = ctr.review(db, p.tenant_id, p.space, rid, p.display)
        elif action == "exempt":
            rep = ctr.exempt(db, p.tenant_id, p.space, rid, str((body or {}).get("reason") or ""), p.display)
        else:
            raise _bad(404, "unknown action", "NOT_FOUND")
    except ctr.CtrError as exc:
        raise _ctr_error(exc) from exc
    return ctr.as_dict(db, rep)


@router.get("/regulator/statistics", tags=["regulatory"])
def regulator_statistics(date_from: str = "", date_to: str = "",
                         p: Principal = Depends(require("ctr.view", "str.approve", "reports.board")),
                         db: Session = Depends(get_db)):
    from ..services.ctr import statistics
    if date_from:
        start = datetime.fromisoformat(date_from)
        end = datetime.fromisoformat(date_to) + timedelta(days=1) if date_to else utcnow()
    else:
        first = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start, end = (first - timedelta(days=1)).replace(day=1), first
    return statistics(db, p.tenant_id, p.space, start, end)


# =========================================================================== customer risk assessment
@router.post("/customers/risk-assessment", tags=["customers"])
def onboarding_assessment(body: dict, p: Principal = Depends(require("customers.risk_assess", "cra.manage")),
                          db: Session = Depends(get_db)):
    """Risk rating of a prospective customer from KYC data (called by the onboarding system)."""
    from ..services import cra
    r = cra.assess_onboarding(db, _tenant(db, p), body or {})
    audit.record(db, p.tenant_id, "cra.onboarding", actor=p, resource_type="customer",
                 resource_id=str((body or {}).get("customer_id") or (body or {}).get("display_name") or "prospect")[:60],
                 after={"level": r["level"], "score": r["score"]})
    return r


@router.get("/customers/{ref}/risk-assessment", tags=["customers"])
def customer_assessment(ref: str, p: Principal = Depends(require("cra.view", "cra.manage")), db: Session = Depends(get_db)):
    from ..services import cra
    c = _visible_customer(db, p, ref)
    t = _tenant(db, p)
    current = cra.assess(db, t, p.space, c)
    hist = db.execute(select(RiskAssessment).where(RiskAssessment.tenant_id == p.tenant_id,
                                                   RiskAssessment.space == p.space, RiskAssessment.customer_ref == ref)
                      .order_by(RiskAssessment.created_at.desc()).limit(30)).scalars().all()
    return {"customer_ref": ref, "rating": c.risk_category, "current": current, "history": [cra.as_dict(h) for h in hist],
            "pending": next((cra.as_dict(h) for h in hist if h.status == "PENDING_APPROVAL"), None),
            "can_manage": p.can("cra.manage"), "can_approve": p.can("cra.approve")}


@router.post("/customers/{ref}/risk-assessment/run", tags=["customers"])
def customer_reassess(ref: str, p: Principal = Depends(require_user("cra.manage")), db: Session = Depends(get_db)):
    from ..services import cra
    c = _visible_customer(db, p, ref)
    row = cra.reassess(db, _tenant(db, p), p.space, c, p.display)
    return {"assessment": cra.as_dict(row) if row else None, "rating": c.risk_category,
            "message": "no material change since the last assessment" if row is None else f"assessment {row.status.lower()}"}


@router.post("/customers/{ref}/risk-assessment/override", tags=["customers"])
def customer_override(ref: str, body: dict, p: Principal = Depends(require_user("cra.manage")),
                      db: Session = Depends(get_db)):
    from ..services import cra
    _visible_customer(db, p, ref)
    try:
        row = cra.override(db, _tenant(db, p), p.space, ref, str(body.get("level") or "").upper(),
                           str(body.get("reason") or ""), p.display)
    except ValueError as exc:
        raise _bad(422, str(exc)) from exc
    return cra.as_dict(row)


@router.get("/risk-assessments", tags=["customers"])
def risk_assessments(status: str = "", p: Principal = Depends(require("cra.view", "cra.manage")),
                     db: Session = Depends(get_db)):
    from ..models import Customer
    from ..services import cra
    q = select(RiskAssessment).where(RiskAssessment.tenant_id == p.tenant_id, RiskAssessment.space == p.space)
    if status:
        q = q.where(RiskAssessment.status == status)
    else:
        q = q.where(RiskAssessment.status.in_(("APPLIED", "PENDING_APPROVAL", "REJECTED")),
                    RiskAssessment.previous_level != RiskAssessment.level)
    rows = db.execute(q.order_by(RiskAssessment.created_at.desc()).limit(300)).scalars().all()
    names = {c.customer_ref: c.display_name for c in db.execute(select(Customer).where(
        Customer.tenant_id == p.tenant_id, Customer.space == p.space,
        Customer.customer_ref.in_([r.customer_ref for r in rows]))).scalars()} if rows else {}
    return {"assessments": [{**cra.as_dict(r), "customer_name": names.get(r.customer_ref, "")} for r in rows],
            "distribution": cra.distribution(db, p.tenant_id, p.space),
            "pending": sum(1 for r in rows if r.status == "PENDING_APPROVAL"),
            "can_manage": p.can("cra.manage"), "can_approve": p.can("cra.approve")}


@router.post("/risk-assessments/run-all", tags=["customers"])
def risk_run_all(p: Principal = Depends(require_user("cra.manage")), db: Session = Depends(get_db)):
    from ..services import cra
    out = cra.run_all(db, _tenant(db, p), p.space)
    audit.record(db, p.tenant_id, "cra.run_all", actor=p, resource_type="cra", resource_id=p.space, after=out)
    return out


@router.post("/risk-assessments/{rid}/{action}", tags=["customers"])
def risk_decide(rid: str, action: str, p: Principal = Depends(require_user("cra.approve")), db: Session = Depends(get_db)):
    from ..services import cra
    if action not in ("approve", "reject"):
        raise _bad(404, "unknown action", "NOT_FOUND")
    try:
        row = cra.decide(db, _tenant(db, p), p.space, rid, action == "approve", p, p.display)
    except LookupError as exc:
        raise _bad(404, str(exc), "NOT_FOUND") from exc
    except PermissionError as exc:
        raise _bad(403, str(exc), "FOUR_EYES") from exc
    except ValueError as exc:
        raise _bad(409, str(exc)) from exc
    return cra.as_dict(row)


@router.get("/cra/model", tags=["customers"])
def cra_model(p: Principal = Depends(require("cra.view", "cra.manage")), db: Session = Depends(get_db)):
    from ..services import cra
    return {"model": cra.model(_tenant(db, p)), "default": cra.DEFAULT_MODEL, "can_manage": p.can("cra.manage")}


@router.put("/cra/model", tags=["customers"])
def cra_model_put(body: dict, p: Principal = Depends(require_user("cra.manage")), db: Session = Depends(get_db)):
    from ..services import cra
    t = _tenant(db, p)
    before = cra.model(t)
    custom = {}
    if "weights" in body:
        w = {k: float(v) for k, v in (body["weights"] or {}).items() if k in cra.DEFAULT_MODEL["weights"]}
        if any(v < 0 or v > 100 for v in w.values()):
            raise _bad(422, "weights must be between 0 and 100")
        custom["weights"] = w
    if "thresholds" in body:
        th = {k: float(v) for k, v in (body["thresholds"] or {}).items() if k in ("MEDIUM", "HIGH")}
        if th and not 0 < th.get("MEDIUM", before["thresholds"]["MEDIUM"]) < th.get("HIGH", before["thresholds"]["HIGH"]) <= 100:
            raise _bad(422, "thresholds must satisfy 0 < MEDIUM < HIGH <= 100")
        custom["thresholds"] = th
    for key in ("auto_downgrade", "override_valid_days", "home_countries", "occupation_risk", "product_risk"):
        if key in body:
            custom[key] = body[key]
    t.settings = {**(t.settings or {}), "cra": {**((t.settings or {}).get("cra") or {}), **custom}}
    db.commit()
    audit.record(db, p.tenant_id, "cra.model_update", actor=p, resource_type="cra", resource_id="model",
                 before={k: before.get(k) for k in custom}, after=custom)
    return {"model": cra.model(t)}


# =========================================================================== cheques
@router.get("/cheques/summary", tags=["cheques"])
def cheque_summary(days: int = 90, p: Principal = Depends(require("cheques.view")), db: Session = Depends(get_db)):
    from ..services import cheques
    return {**cheques.summary(db, p.tenant_id, p.space, max(1, min(days, 730))), "reasons_catalogue": cheques.RETURN_REASONS,
            "can_manage": p.can("cheques.manage")}


@router.post("/cheques/returns", tags=["cheques"])
def cheque_return(body: dict, p: Principal = Depends(require("cheques.manage", "events.ingest")),
                  db: Session = Depends(get_db)):
    from ..services import cheques
    if not (body.get("transaction_id") or body.get("drawer_account")):
        raise _bad(422, "give the deposit transaction_id or the drawer_account")
    ret = cheques.record_return(db, p.tenant_id, p.space, body)
    audit.record(db, p.tenant_id, "cheque.return", actor=p, resource_type="cheque", resource_id=ret.cheque_number,
                 space=p.space, after={"reason": ret.reason, "drawer": ret.drawer_account, "tx": ret.tx_ref})
    return row_dict(ret)


# =========================================================================== networks
@router.get("/networks/rings", tags=["networks"])
def network_rings(p: Principal = Depends(require("network.view")), db: Session = Depends(get_db)):
    from ..services import network
    rings = network.rings(db, p.tenant_id, p.space)
    return {"rings": rings, "risky": sum(1 for r in rings if r["seeded"]),
            "customers_in_risky_rings": sum(r["size"] for r in rings if r["seeded"]),
            "computed_at": rings[0]["computed_at"] if rings else None, "can_manage": p.can("network.manage")}


@router.get("/networks/mules", tags=["networks"])
def network_mules(days: int = 30, p: Principal = Depends(require("network.view")), db: Session = Depends(get_db)):
    from ..services import network
    return {"accounts": network.mule_accounts(db, p.tenant_id, p.space, max(1, min(days, 180)))}


@router.post("/networks/recompute", tags=["networks"])
def network_recompute(p: Principal = Depends(require_user("network.manage")), db: Session = Depends(get_db)):
    from ..services import network
    out = network.compute(db, p.tenant_id, p.space)
    audit.record(db, p.tenant_id, "network.recompute", actor=p, resource_type="network", resource_id=p.space,
                 after={k: v for k, v in out.items() if k != "computed_at"})
    return out


# =========================================================================== cards
@router.get("/customers/{ref}/cards", tags=["cards"])
def customer_cards(ref: str, p: Principal = Depends(require("cards.view", "cards.manage")), db: Session = Depends(get_db)):
    from ..services import cards
    _visible_customer(db, p, ref)
    return {"cards": cards.customer_cards(db, p.tenant_id, p.space, ref), "can_manage": p.can("cards.manage")}


@router.get("/cards/frozen", tags=["cards"])
def frozen_cards(p: Principal = Depends(require("cards.view", "cards.manage")), db: Session = Depends(get_db)):
    from ..services import cards
    rows = db.execute(select(CardControl).where(CardControl.tenant_id == p.tenant_id, CardControl.space == p.space)
                      .order_by(CardControl.frozen_at.desc()).limit(300)).scalars().all()
    return {"controls": [cards.as_dict(r) for r in rows], "frozen": sum(1 for r in rows if r.status == "FROZEN"),
            "can_manage": p.can("cards.manage")}


@router.post("/cards/{card_ref}/freeze", tags=["cards"])
def card_freeze(card_ref: str, body: dict, p: Principal = Depends(require("cards.manage", "cards.customer")),
                db: Session = Depends(get_db)):
    from ..services import cards
    customer = str(body.get("customer_id") or body.get("customer_ref") or "")
    if not customer:
        raise _bad(422, "customer_id is required")
    by_app = p.kind == "api"
    reason = str(body.get("reason") or ("Frozen by the customer in the mobile app" if by_app else ""))
    if not by_app and len(reason.strip()) < 10:
        raise _bad(422, "give a reason of at least 10 characters")
    ctl = cards.freeze(db, p.tenant_id, p.space, card_ref, customer, reason, "CUSTOMER_APP" if by_app else "AML",
                       p.display, str(body.get("transaction_id") or ""), str(body.get("case_id") or ""))
    return cards.as_dict(ctl)


@router.post("/cards/{card_ref}/unfreeze", tags=["cards"])
def card_unfreeze(card_ref: str, body: Optional[dict] = None, p: Principal = Depends(require("cards.manage", "cards.customer")),
                  db: Session = Depends(get_db)):
    from ..services import cards
    try:
        ctl = cards.unfreeze(db, p.tenant_id, p.space, card_ref, p.display, by_customer=p.kind == "api",
                             reason=str((body or {}).get("reason") or ""))
    except LookupError as exc:
        raise _bad(404, str(exc), "NOT_FOUND") from exc
    except PermissionError as exc:
        raise _bad(403, str(exc), "BANK_FREEZE") from exc
    return cards.as_dict(ctl)


# =========================================================================== in-app confirmation
@router.post("/confirmations/{cid}/respond", tags=["confirmations"])
def confirmation_respond(cid: str, body: dict, p: Principal = Depends(require("confirmations.respond", "confirmations.answer")),
                         db: Session = Depends(get_db)):
    """The bank's app backend posts the customer's answer (YES / NO) to a payment confirmation."""
    from ..services import confirmations
    conf = db.get(CustomerConfirmation, cid)
    if conf is None or conf.tenant_id != p.tenant_id or conf.space != p.space:
        raise _bad(404, "confirmation not found", "NOT_FOUND")
    if body.get("customer_id") and str(body["customer_id"]) != conf.customer_ref:
        raise _bad(403, "the confirmation belongs to another customer", "WRONG_CUSTOMER")
    word = str(body.get("answer") or "").strip().upper()
    if word not in ("YES", "NO"):
        raise _bad(422, "answer must be YES or NO")
    via = "APP" if p.kind == "api" else "CALL_CENTRE"
    try:
        out = confirmations.answer(db, conf, word, "customer app" if via == "APP" else p.display, via,
                                   freeze_card=body.get("freeze_card"))
    except ValueError as exc:
        raise _bad(409, str(exc)) from exc
    return out


__all__ = ["router"]
