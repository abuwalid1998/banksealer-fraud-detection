"""Policies & thresholds, models & onboarding, connectors & dead letters, reports."""
from __future__ import annotations

import copy
import csv
import io
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import Principal, require, require_user
from ..models import (ConnectorLog, Connector, Customer, CustomerSegment, DeadLetterEvent, ModelVersion,
                      PolicyVersion, ReportJob, Tenant, utcnow)
from ..services import connectors as con_service
from ..services import engine_registry, segmentation
from ..services import policy as policy_service
from ..services import reports as report_service
from ..services.common import row_dict
from ..services.decision import ingest_events

router = APIRouter(prefix="/api/v1", tags=["governance"])


def _bad(status: int, message: str, code: str = "INVALID"):
    return HTTPException(status, {"code": code, "message": message})


# --------------------------------------------------------------------------- policies
def _policy_out(pv: PolicyVersion) -> dict:
    return row_dict(pv)


def _get_policy(db: Session, p: Principal, policy_id: str) -> PolicyVersion:
    pv = db.get(PolicyVersion, policy_id)
    if pv is None or pv.tenant_id != p.tenant_id:
        raise _bad(404, "policy version not found", "NOT_FOUND")
    return pv


@router.get("/policies", tags=["policies"])
def list_policies(p: Principal = Depends(require("policies.view")), db: Session = Depends(get_db)):
    active = policy_service.active_policy(db, p.tenant_id)
    rows = db.execute(select(PolicyVersion).where(PolicyVersion.tenant_id == p.tenant_id)
                      .order_by(PolicyVersion.version.desc())).scalars().all()
    t = db.get(Tenant, p.tenant_id)
    return {"active": _policy_out(active), "versions": [{k: v for k, v in _policy_out(r).items() if k != "document"}
                                                        for r in rows],
            "outcomes": policy_service.OUTCOMES, "fallbacks": policy_service.FALLBACKS,
            "condition_keys": policy_service.RULE_CONDITION_KEYS,
            "maker_checker": bool((t.settings.get("governance") or {}).get("maker_checker")),
            "can_edit": p.can("policies.edit"), "can_approve": p.can("policies.approve")}


@router.get("/policies/{policy_id}", tags=["policies"])
def get_policy(policy_id: str, p: Principal = Depends(require("policies.view")), db: Session = Depends(get_db)):
    pv = _get_policy(db, p, policy_id)
    out = _policy_out(pv)
    base = db.get(PolicyVersion, pv.based_on) if pv.based_on else None
    out["changes"] = policy_service.diff(base.document, pv.document) if base else []
    return out


@router.post("/policies", status_code=201, tags=["policies"])
def create_policy(body: dict, p: Principal = Depends(require_user("policies.edit")), db: Session = Depends(get_db)):
    base = db.get(PolicyVersion, body["based_on"]) if body.get("based_on") else policy_service.active_policy(
        db, p.tenant_id)
    if base is None or base.tenant_id != p.tenant_id:
        raise _bad(404, "base version not found", "NOT_FOUND")
    doc = policy_service.merge_document(body.get("document") or base.document)
    errors = policy_service.validate_document(doc)
    if errors:
        raise HTTPException(422, {"code": "INVALID_POLICY", "message": "; ".join(errors), "errors": errors})
    version = policy_service.next_version(db, p.tenant_id)
    pv = PolicyVersion(id=f"{p.tenant_id}-POLICY-{version:02d}", tenant_id=p.tenant_id, version=version,
                       name=str(body.get("name") or f"Draft v{version}")[:160], status="DRAFT", document=doc,
                       justification=str(body.get("justification") or ""), based_on=base.id, created_by=p.user_id,
                       created_by_name=p.display)
    db.add(pv)
    db.commit()
    audit.record(db, p.tenant_id, "policy.draft", actor=p, resource_type="policy", resource_id=pv.id,
                 after={"based_on": base.id, "changes": policy_service.diff(base.document, doc)})
    return get_policy(pv.id, p, db)


@router.put("/policies/{policy_id}", tags=["policies"])
def edit_policy(policy_id: str, body: dict, p: Principal = Depends(require_user("policies.edit")),
                db: Session = Depends(get_db)):
    pv = _get_policy(db, p, policy_id)
    if pv.status != "DRAFT":
        raise _bad(409, "only DRAFT versions can be edited; published versions are immutable", "IMMUTABLE")
    before = copy.deepcopy(pv.document)
    if body.get("document") is not None:
        doc = policy_service.merge_document(body["document"])
        errors = policy_service.validate_document(doc)
        if errors:
            raise HTTPException(422, {"code": "INVALID_POLICY", "message": "; ".join(errors), "errors": errors})
        pv.document = doc
        pv.simulation = {}
    if body.get("name"):
        pv.name = str(body["name"])[:160]
    if body.get("justification") is not None:
        pv.justification = str(body["justification"])
    for key in ("effective_from", "effective_to"):
        if key in body:
            setattr(pv, key, datetime.fromisoformat(str(body[key]).replace("Z", "")) if body[key] else None)
    db.commit()
    audit.record(db, p.tenant_id, "policy.edit", actor=p, resource_type="policy", resource_id=pv.id,
                 after={"changes": policy_service.diff(before, pv.document)})
    return get_policy(pv.id, p, db)


@router.post("/policies/{policy_id}/simulate", tags=["policies"])
def simulate(policy_id: str, body: dict = None, p: Principal = Depends(require("policies.view", "policies.simulate")),
             db: Session = Depends(get_db)):
    pv = _get_policy(db, p, policy_id)
    days = int((body or {}).get("days", 30))
    result = policy_service.simulate(db, p.tenant_id, p.space, pv.document, days=min(max(days, 1), 365))
    result["policy_version"] = pv.id
    result["space"] = p.space
    if pv.status == "DRAFT":
        pv.simulation = {k: v for k, v in result.items() if k != "changed_examples"}
        db.commit()
    return result


def _activate(db: Session, p: Principal, pv: PolicyVersion, reason: str) -> None:
    policy_service.invalidate_policy_cache(p.tenant_id)
    now = utcnow()
    pv.status = "ACTIVE"
    pv.activated_at = now
    if pv.effective_from is None or pv.effective_from <= now:
        db.execute(update(PolicyVersion).where(PolicyVersion.tenant_id == p.tenant_id, PolicyVersion.status == "ACTIVE",
                                               PolicyVersion.id != pv.id, PolicyVersion.version < pv.version)
                   .values(status="SUPERSEDED"))
    db.commit()
    policy_service.invalidate_policy_cache(p.tenant_id)
    audit.record(db, p.tenant_id, "policy.publish", actor=p, resource_type="policy", resource_id=pv.id,
                 after={"effective_from": str(pv.effective_from or now), "reason": reason})


@router.post("/policies/{policy_id}/{action}", tags=["policies"])
def policy_action(policy_id: str, action: str, body: dict = None, p: Principal = Depends(require_user()),
                  db: Session = Depends(get_db)):
    body = body or {}
    pv = _get_policy(db, p, policy_id)
    t = db.get(Tenant, p.tenant_id)
    maker_checker = bool((t.settings.get("governance") or {}).get("maker_checker"))
    if action == "submit":
        if not p.can("policies.edit"):
            raise _bad(403, "not permitted", "FORBIDDEN")
        if pv.status != "DRAFT":
            raise _bad(409, f"version is {pv.status}")
        pv.status, pv.submitted_at = "PENDING_APPROVAL", utcnow()
        db.commit()
        audit.record(db, p.tenant_id, "policy.submit", actor=p, resource_type="policy", resource_id=pv.id)
    elif action == "publish":
        if not p.can("policies.edit"):
            raise _bad(403, "not permitted", "FORBIDDEN")
        if pv.status != "DRAFT":
            raise _bad(409, f"version is {pv.status}")
        if maker_checker:
            raise _bad(409, "maker-checker is enabled: submit the version for approval by a second approver",
                       "APPROVAL_REQUIRED")
        if not str(pv.justification or body.get("justification") or "").strip():
            raise _bad(422, "a justification is required to publish a policy change")
        if body.get("justification"):
            pv.justification = str(body["justification"])
        _activate(db, p, pv, "published")
    elif action in ("approve", "reject"):
        if not p.can("policies.approve"):
            raise _bad(403, "approval permission required (maker-checker)", "FORBIDDEN")
        if pv.status != "PENDING_APPROVAL":
            raise _bad(409, f"version is {pv.status}")
        if pv.created_by == p.user_id:
            raise _bad(409, "the maker cannot approve their own change", "SEGREGATION_OF_DUTIES")
        if action == "reject":
            pv.status = "REJECTED"
            db.commit()
            audit.record(db, p.tenant_id, "policy.reject", actor=p, resource_type="policy", resource_id=pv.id,
                         reason=str(body.get("reason") or ""))
        else:
            pv.approved_by_name = p.display
            _activate(db, p, pv, f"approved by {p.display}")
    elif action == "rollback":
        if not p.can("policies.edit"):
            raise _bad(403, "not permitted", "FORBIDDEN")
        if pv.status not in ("SUPERSEDED", "ACTIVE"):
            raise _bad(409, "only previously published versions can be restored")
        reason = str(body.get("justification") or "").strip()
        if not reason:
            raise _bad(422, "a justification is required for an emergency rollback")
        version = policy_service.next_version(db, p.tenant_id)
        new = PolicyVersion(id=f"{p.tenant_id}-POLICY-{version:02d}", tenant_id=p.tenant_id, version=version,
                            name=f"Rollback to {pv.id}", status="DRAFT", document=copy.deepcopy(pv.document),
                            justification=f"Emergency rollback: {reason}", based_on=policy_service.active_policy(
                                db, p.tenant_id).id, created_by=p.user_id, created_by_name=p.display)
        db.add(new)
        db.commit()
        _activate(db, p, new, f"emergency rollback to {pv.id}")
        pv = new
    elif action == "discard":
        if not p.can("policies.edit") or pv.status != "DRAFT":
            raise _bad(409, "only drafts can be discarded")
        pv.status = "REJECTED"
        db.commit()
        audit.record(db, p.tenant_id, "policy.discard", actor=p, resource_type="policy", resource_id=pv.id)
    else:
        raise _bad(404, "unknown action", "NOT_FOUND")
    return get_policy(pv.id, p, db)


# --------------------------------------------------------------------------- models & onboarding
@router.get("/models/status", tags=["models"])
def models_status(p: Principal = Depends(require("models.view", "system.view")), db: Session = Depends(get_db)):
    return {"fraud": engine_registry.status(db, p.tenant_id, p.space),
            "performance": report_service.performance(db, p.tenant_id, p.space), "space": p.space}


@router.get("/models", tags=["models"])
def list_models(p: Principal = Depends(require("models.view")), db: Session = Depends(get_db)):
    rows = db.execute(select(ModelVersion).where(ModelVersion.tenant_id == p.tenant_id, ModelVersion.space == p.space)
                      .order_by(ModelVersion.trained_at.desc())).scalars().all()
    out = []
    for m in rows:
        d = row_dict(m, exclude=("artifact_path",))
        d["metrics"] = {k: v for k, v in (m.metrics or {}).items() if k != "artifact"}
        out.append(d)
    return {"models": out, "status": engine_registry.status(db, p.tenant_id, p.space),
            "performance": report_service.performance(db, p.tenant_id, p.space),
            "can_train": p.can("models.train"), "can_activate": p.can("models.activate")}


@router.get("/models/segments", tags=["models"])
def segments(p: Principal = Depends(require("models.view", "customers.view_full")), db: Session = Depends(get_db)):
    mv = db.execute(select(ModelVersion).where(ModelVersion.tenant_id == p.tenant_id, ModelVersion.space == p.space,
                                               ModelVersion.kind == "SEGMENTATION", ModelVersion.status == "ACTIVE")
                    ).scalar()
    if mv is None:
        return {"model": None, "segments": []}
    segs = db.execute(select(CustomerSegment).where(CustomerSegment.model_version == mv.id)
                      .order_by(CustomerSegment.segment_key)).scalars().all()
    counts = dict(db.execute(select(Customer.segment_key, func.count()).where(
        Customer.tenant_id == p.tenant_id, Customer.space == p.space).group_by(Customer.segment_key)).all())
    low_conf = db.execute(select(func.count(Customer.id)).where(
        Customer.tenant_id == p.tenant_id, Customer.space == p.space, Customer.segment_confidence < 0.6)).scalar()
    return {"model": {k: v for k, v in row_dict(mv, exclude=("artifact_path",)).items() if k != "metrics"} |
            {"metrics": {k: v for k, v in mv.metrics.items() if k != "artifact"}},
            "segments": [row_dict(s) | {"customers_now": counts.get(s.segment_key, 0)} for s in segs],
            "low_confidence_assignments": low_conf}


@router.get("/models/clusters", tags=["models"])
def model_clusters(p: Principal = Depends(require("models.clusters", "models.view")), db: Session = Depends(get_db)):
    from ..services import model_ops
    return model_ops.clusters(db, p.tenant_id, p.space, show_names=p.can("customers.view_full"))


@router.get("/models/health", tags=["models"])
def model_health(p: Principal = Depends(require("models.view", "models.train")), db: Session = Depends(get_db)):
    from ..services import model_ops
    return model_ops.health(db, p.tenant_id, p.space)


@router.get("/models/params", tags=["models"])
def model_params(p: Principal = Depends(require("models.view", "models.train")), db: Session = Depends(get_db)):
    from ..services import model_ops
    t = db.get(Tenant, p.tenant_id)
    out = model_ops.describe(t, p.space)
    out["editable"] = p.can("models.train")
    return out


@router.put("/models/params", tags=["models"])
def update_model_params(body: dict, p: Principal = Depends(require_user("models.train")), db: Session = Depends(get_db)):
    from ..services import model_ops
    t = db.get(Tenant, p.tenant_id)
    before = model_ops.overrides(t, p.space)
    try:
        new = {} if body.get("reset") else model_ops.validate(body.get("values") or {})
    except (TypeError, ValueError) as exc:
        raise _bad(422, str(exc)) from exc
    model_ops.save(db, t, p.space, new)
    audit.record(db, p.tenant_id, "model.parameters_update", actor=p, resource_type="model", space=p.space,
                 before=before, after=new)
    out = model_ops.describe(t, p.space)
    out["editable"] = True
    out["note"] = "Saved. The new parameters apply to the next training run; retrain to use them."
    return out


@router.post("/models/fraud/train", tags=["models"])
def train(body: dict = None, p: Principal = Depends(require_user("models.train")), db: Session = Depends(get_db)):
    from ..services import model_ops
    body = body or {}
    mode = body.get("mode", "activate")
    params = None
    if body.get("params"):
        try:
            params = {**model_ops.overrides(db.get(Tenant, p.tenant_id), p.space), **model_ops.validate(body["params"])}
        except (TypeError, ValueError) as exc:
            raise _bad(422, str(exc)) from exc
    try:
        mv = engine_registry.train_fraud_model(db, p.tenant_id, p.space, p.display, activate=mode == "activate",
                                               shadow=mode == "shadow", notes=str(body.get("notes", "")),
                                               params=params, window_days=body.get("window_days") or None)
    except ValueError as exc:
        raise _bad(422, str(exc)) from exc
    audit.record(db, p.tenant_id, "model.train", actor=p, resource_type="model", resource_id=mv.id,
                 after={"mode": mode, "training_size": mv.training_size, "params": params,
                        "window_days": body.get("window_days")})
    return row_dict(mv, exclude=("artifact_path",))


@router.post("/models/segmentation/build", tags=["models"])
def build_segmentation(body: dict = None, p: Principal = Depends(require_user("models.train")),
                       db: Session = Depends(get_db)):
    try:
        mv = segmentation.build_segmentation(db, p.tenant_id, p.space, p.display)
    except ValueError as exc:
        raise _bad(422, str(exc)) from exc
    if (body or {}).get("activate", True):
        segmentation.activate_segmentation(db, mv.id, p.display)
    audit.record(db, p.tenant_id, "model.segmentation_build", actor=p, resource_type="model", resource_id=mv.id,
                 after={k: v for k, v in mv.metrics.items() if k != "artifact"})
    return {k: v for k, v in row_dict(mv, exclude=("artifact_path",)).items() if k != "metrics"}


@router.post("/models/{model_id}/{action}", tags=["models"])
def model_action(model_id: str, action: str, p: Principal = Depends(require_user("models.activate")),
                 db: Session = Depends(get_db)):
    mv = db.get(ModelVersion, model_id)
    if mv is None or mv.tenant_id != p.tenant_id or mv.space != p.space:
        raise _bad(404, "model not found", "NOT_FOUND")
    try:
        if action == "activate":
            if mv.kind == "FRAUD":
                engine_registry.activate_model(db, model_id, p.display)
            else:
                segmentation.activate_segmentation(db, model_id, p.display)
        elif action == "shadow" and mv.kind == "FRAUD":
            engine_registry.set_shadow(db, model_id, p.display)
        elif action == "clear-shadow":
            engine_registry.clear_shadow(db, p.tenant_id, p.space)
        else:
            raise _bad(404, "unknown action", "NOT_FOUND")
    except (LookupError, ValueError) as exc:
        raise _bad(422, str(exc)) from exc
    audit.record(db, p.tenant_id, f"model.{action}", actor=p, resource_type="model", resource_id=model_id)
    return {"ok": True, "status": engine_registry.status(db, p.tenant_id, p.space)}


@router.get("/onboarding/data-quality", tags=["onboarding"])
def data_quality(p: Principal = Depends(require("models.view", "models.train")), db: Session = Depends(get_db)):
    return segmentation.data_quality(db, p.tenant_id, p.space)


CUSTOMER_TEMPLATE = ["customer_id", "display_name", "customer_type", "business_activity", "risk_category",
                     "kyc_review_status", "country", "tenure_months", "products", "expected_monthly_turnover",
                     "expected_monthly_tx", "account_purpose"]
TX_TEMPLATE = ["transaction_id", "customer_id", "timestamp", "transaction_type", "amount", "currency", "channel",
               "beneficiary_reference", "beneficiary_country", "ip_address", "ip_country", "asn"]


@router.get("/onboarding/templates/{kind}", tags=["onboarding"])
def template(kind: str, p: Principal = Depends(require("models.train"))):
    cols = CUSTOMER_TEMPLATE if kind == "customers" else TX_TEMPLATE if kind == "transactions" else None
    if cols is None:
        raise _bad(404, "unknown template", "NOT_FOUND")
    example = (["C90001", "Example Customer", "INDIVIDUAL", "Salaried employee", "LOW", "CURRENT", "PS", "24",
                "current_account;mobile_banking", "4500", "12", "Salary and expenses"] if kind == "customers" else
               ["TX-1", "C90001", "2026-10-01 10:15:00", "bank_transfer", "350.00", "ILS", "mobile_banking",
                "PS92PALS000000000400123456702", "PS", "213.6.1.20", "PS", "AS12975"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerow(example)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=verishield_{kind}_template.csv"})


def _read_upload(content: bytes, filename: str) -> list[dict]:
    text = content.decode("utf-8-sig", errors="replace")
    if filename.lower().endswith((".jsonl", ".ndjson")):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if filename.lower().endswith(".json"):
        data = json.loads(text)
        return data if isinstance(data, list) else data.get("records", [])
    return list(csv.DictReader(io.StringIO(text)))


@router.post("/onboarding/customers", tags=["onboarding"])
async def import_customers(file: UploadFile = File(...), p: Principal = Depends(require_user("models.train")),
                           db: Session = Depends(get_db)):
    content = await file.read(50_000_001)
    if len(content) > 50_000_000:
        raise _bad(413, "file exceeds 50 MB")
    rows = _read_upload(content, file.filename or "upload.csv")
    created = updated = rejected = 0
    errors = []
    for i, r in enumerate(rows, 1):
        ref = str(r.get("customer_id") or r.get("customer_ref") or "").strip()
        if not ref:
            rejected += 1
            errors.append(f"row {i}: missing customer_id")
            continue
        c = db.execute(select(Customer).where(Customer.tenant_id == p.tenant_id, Customer.space == p.space,
                                              Customer.customer_ref == ref)).scalar()
        if c is None:
            c = Customer(tenant_id=p.tenant_id, space=p.space, customer_ref=ref)
            db.add(c)
            created += 1
        else:
            updated += 1
        try:
            c.display_name = str(r.get("display_name") or c.display_name or "")[:160]
            ctype = str(r.get("customer_type") or "INDIVIDUAL").upper()
            c.customer_type = "BUSINESS" if ctype.startswith("B") or ctype in ("CORPORATE", "COMPANY") else "INDIVIDUAL"
            c.business_activity = str(r.get("business_activity") or "")[:120]
            c.risk_category = str(r.get("risk_category") or "UNKNOWN").upper()[:12]
            c.kyc_review_status = str(r.get("kyc_review_status") or "CURRENT").upper()[:20]
            c.country = str(r.get("country") or "")[:2].upper()
            c.tenure_months = int(float(r.get("tenure_months") or 0))
            products = r.get("products") or ""
            c.products = products if isinstance(products, list) else [x.strip() for x in str(products).replace(
                ",", ";").split(";") if x.strip()]
            c.expected_monthly_turnover = float(r.get("expected_monthly_turnover") or 0)
            c.expected_monthly_tx = float(r.get("expected_monthly_tx") or 0)
            c.account_purpose = str(r.get("account_purpose") or "")[:160]
            c.attributes = {**(c.attributes or {}), "source": file.filename}
            c.updated_at = utcnow()
        except (TypeError, ValueError) as exc:
            rejected += 1
            errors.append(f"row {i} ({ref}): {exc}")
    db.commit()
    audit.record(db, p.tenant_id, "onboarding.customers_import", actor=p, resource_type="onboarding",
                 after={"file": file.filename, "created": created, "updated": updated, "rejected": rejected})
    return {"rows": len(rows), "created": created, "updated": updated, "rejected": rejected, "errors": errors[:50],
            "data_quality": segmentation.data_quality(db, p.tenant_id, p.space)}


@router.post("/onboarding/transactions", tags=["onboarding"])
async def import_transactions(file: UploadFile = File(...), field_mapping: str = Form(""),
                              p: Principal = Depends(require_user("models.train")), db: Session = Depends(get_db)):
    content = await file.read(100_000_001)
    if len(content) > 100_000_000:
        raise _bad(413, "file exceeds 100 MB")
    try:
        mapping = json.loads(field_mapping) if field_mapping.strip() else {}
    except ValueError as exc:
        raise _bad(422, "field_mapping must be JSON like {\"source_field\": \"user_id\"}") from exc
    rows = _read_upload(content, file.filename or "upload.csv")
    res = ingest_events(db, p.tenant_id, p.space, rows, source=f"History import {file.filename}", mode="HISTORY",
                        field_mapping=mapping, caller=p.display)
    audit.record(db, p.tenant_id, "onboarding.history_import", actor=p, resource_type="onboarding",
                 after={"file": file.filename, **{k: v for k, v in res.items() if k != "decisions"}})
    res["data_quality"] = segmentation.data_quality(db, p.tenant_id, p.space)
    return res


# --------------------------------------------------------------------------- connectors
def _connector(db: Session, p: Principal, cid: str) -> Connector:
    c = db.get(Connector, cid)
    if c is None or c.tenant_id != p.tenant_id:
        raise _bad(404, "connector not found", "NOT_FOUND")
    return c


def _con_wrap(fn):
    try:
        return fn()
    except con_service.ConnectorError as exc:
        raise HTTPException(exc.status, {"code": "CONNECTOR_ERROR", "message": exc.message}) from exc


@router.get("/connectors/types", tags=["connectors"])
def connector_types(p: Principal = Depends(require("connectors.view"))):
    return {"types": con_service.CONNECTOR_TYPES, "statuses": con_service.STATUSES}


@router.get("/connectors", tags=["connectors"])
def list_connectors(include_retired: bool = False, p: Principal = Depends(require("connectors.view")),
                    db: Session = Depends(get_db)):
    q = select(Connector).where(Connector.tenant_id == p.tenant_id, Connector.space == p.space)
    if not include_retired:
        q = q.where(Connector.status != "RETIRED")
    rows = db.execute(q.order_by(Connector.created_at)).scalars().all()
    return {"connectors": [con_service.public(c) for c in rows], "space": p.space,
            "can_manage": p.can("connectors.manage")}


@router.post("/connectors", status_code=201, tags=["connectors"])
def create_connector(body: dict, p: Principal = Depends(require_user("connectors.manage")),
                     db: Session = Depends(get_db)):
    _con_wrap(lambda: con_service.validate(body, p.space))
    info = con_service.CONNECTOR_TYPES[body["type"]]
    c = Connector(tenant_id=p.tenant_id, space=p.space, name=str(body.get("name") or info["label"])[:120],
                  type=body["type"], direction=info["direction"], environment=body.get("environment") or "DEV",
                  ingest_mode=body.get("ingest_mode") or "POST_EVENT", config=body.get("config") or {},
                  auth_method=body.get("auth_method") or info["auth"][0], tls_verify=body.get("tls_verify", True),
                  field_mapping=body.get("field_mapping") or {}, schedule=body.get("schedule") or {},
                  allowed_ips=body.get("allowed_ips") or [], data_scope=str(body.get("data_scope") or ""),
                  permission="READ_WRITE" if info["direction"] == "OUTBOUND" else "READ_ONLY",
                  owner=str(body.get("owner") or p.display), created_by=p.user_id)
    if body.get("credential_expires_at"):
        c.credential_expires_at = datetime.fromisoformat(str(body["credential_expires_at"]).replace("Z", ""))
    db.add(c)
    db.flush()
    con_service.add_log(db, c, "Connector created (DRAFT)", actor=p.display)
    db.commit()
    audit.record(db, p.tenant_id, "connector.create", actor=p, resource_type="connector", resource_id=c.id,
                 after=con_service.public(c))
    return con_service.public(c)


@router.get("/connectors/{cid}", tags=["connectors"])
def get_connector(cid: str, p: Principal = Depends(require("connectors.view")), db: Session = Depends(get_db)):
    return con_service.public(_connector(db, p, cid))


@router.put("/connectors/{cid}", tags=["connectors"])
def update_connector(cid: str, body: dict, p: Principal = Depends(require_user("connectors.manage")),
                     db: Session = Depends(get_db)):
    c = _connector(db, p, cid)
    if c.status == "RETIRED":
        raise _bad(409, "connector is retired")
    _con_wrap(lambda: con_service.validate({**con_service.public(c), **body, "type": c.type}, c.space))
    before = con_service.public(c)
    for key in ("name", "environment", "ingest_mode", "config", "auth_method", "tls_verify", "field_mapping",
                "schedule", "allowed_ips", "data_scope", "owner"):
        if key in body:
            setattr(c, key, body[key])
    if "credential_expires_at" in body:
        c.credential_expires_at = datetime.fromisoformat(str(body["credential_expires_at"]).replace("Z", "")) \
            if body["credential_expires_at"] else None
    c.version += 1
    c.updated_at = utcnow()
    if c.status in ("VALIDATED", "ENABLED", "DEGRADED") and any(k in body for k in ("config", "auth_method")):
        con_service.runtime.stop(c.id)
        c.status, c.last_test_ok = "DRAFT", None
        con_service.add_log(db, c, "Configuration changed - re-test required", actor=p.display)
    db.commit()
    audit.record(db, p.tenant_id, "connector.update", actor=p, resource_type="connector", resource_id=c.id,
                 before=before, after=con_service.public(c))
    return con_service.public(c)


@router.post("/connectors/{cid}/test", tags=["connectors"])
def test_connector(cid: str, p: Principal = Depends(require_user("connectors.manage")), db: Session = Depends(get_db)):
    c = _connector(db, p, cid)
    res = con_service.test_connection(db, c, p.display)
    audit.record(db, p.tenant_id, "connector.test", actor=p, resource_type="connector", resource_id=c.id,
                 outcome="SUCCESS" if res["ok"] else "FAILURE", reason=res["message"])
    return res


@router.post("/connectors/{cid}/preview", tags=["connectors"])
def preview(cid: str, body: dict = None, p: Principal = Depends(require_user("connectors.manage")),
            db: Session = Depends(get_db)):
    c = _connector(db, p, cid)
    samples = (body or {}).get("samples")
    return _con_wrap(lambda: con_service.preview_mapping(db, c, samples if isinstance(samples, list) else None))


@router.post("/connectors/{cid}/secret", tags=["connectors"])
def set_secret(cid: str, body: dict, p: Principal = Depends(require_user("connectors.manage")),
               db: Session = Depends(get_db)):
    c = _connector(db, p, cid)
    if body.get("env_ref"):
        name = str(body["env_ref"]).strip()
        if not name.replace("_", "").isalnum():
            raise _bad(422, "env_ref must be an environment variable name")
        c.secret_ref = f"env:{name}"
    elif body.get("value"):
        c.secret_ref = con_service.store_secret(db, p.tenant_id, f"{c.name} credential", str(body["value"]),
                                                p.user_id, c.secret_ref)
    else:
        raise _bad(422, "provide value or env_ref")
    if body.get("expires_at"):
        c.credential_expires_at = datetime.fromisoformat(str(body["expires_at"]).replace("Z", ""))
    c.updated_at = utcnow()
    con_service.add_log(db, c, "Credential set/rotated (value not shown)", actor=p.display)
    db.commit()
    audit.record(db, p.tenant_id, "connector.secret_rotate", actor=p, resource_type="connector", resource_id=c.id,
                 after={"secret_ref_kind": c.secret_ref.split(":")[0]})
    return con_service.public(c)


@router.get("/connectors/{cid}/logs", tags=["connectors"])
def connector_logs(cid: str, p: Principal = Depends(require("connectors.view")), db: Session = Depends(get_db)):
    c = _connector(db, p, cid)
    rows = db.execute(select(ConnectorLog).where(ConnectorLog.connector_id == c.id).order_by(ConnectorLog.id.desc())
                      .limit(200)).scalars().all()
    return {"logs": [row_dict(r) for r in rows]}


@router.post("/connectors/{cid}/{action}", tags=["connectors"])
def connector_action(cid: str, action: str, body: dict = None,
                     p: Principal = Depends(require_user("connectors.manage")), db: Session = Depends(get_db)):
    c = _connector(db, p, cid)
    if action == "retire" and (body or {}).get("confirm") != c.name:
        raise _bad(422, "type the connector name to confirm retirement", "CONFIRMATION_REQUIRED")
    t = db.get(Tenant, p.tenant_id)
    before = c.status
    _con_wrap(lambda: con_service.set_state(db, c, action, p.display, t.demo_mode))
    audit.record(db, p.tenant_id, f"connector.{action}", actor=p, resource_type="connector", resource_id=c.id,
                 before={"status": before}, after={"status": c.status})
    return con_service.public(c)


@router.get("/dead-letters", tags=["connectors"])
def dead_letters(status: str = "PENDING", category: str = "", limit: int = Query(100, le=1000),
                 p: Principal = Depends(require("connectors.view")), db: Session = Depends(get_db)):
    q = select(DeadLetterEvent).where(DeadLetterEvent.tenant_id == p.tenant_id, DeadLetterEvent.space == p.space)
    if status:
        q = q.where(DeadLetterEvent.status == status)
    if category:
        q = q.where(DeadLetterEvent.category == category)
    rows = db.execute(q.order_by(DeadLetterEvent.created_at.desc()).limit(limit)).scalars().all()
    masked = not p.can("tx.view_full")
    out = []
    for r in rows:
        d = row_dict(r)
        if masked:
            from ..security import mask
            d["payload"] = {k: (mask(str(v)) if k.lower() in ("customer_id", "user_id", "iban", "beneficiary_reference",
                                                              "ip", "ip_address", "phone_number", "card_id")
                                else v) for k, v in (r.payload or {}).items()}
        out.append(d)
    totals = dict(db.execute(select(DeadLetterEvent.status, func.count()).where(
        DeadLetterEvent.tenant_id == p.tenant_id, DeadLetterEvent.space == p.space).group_by(DeadLetterEvent.status))
        .all())
    return {"items": out, "totals": totals}


@router.post("/dead-letters/{dlq_id}/{action}", tags=["connectors"])
def dead_letter_action(dlq_id: str, action: str, body: dict = None,
                       p: Principal = Depends(require_user("connectors.manage")), db: Session = Depends(get_db)):
    r = db.get(DeadLetterEvent, dlq_id)
    if r is None or r.tenant_id != p.tenant_id or r.space != p.space:
        raise _bad(404, "record not found", "NOT_FOUND")
    if action == "discard":
        r.status = "DISCARDED"
        db.commit()
        result = {"status": r.status}
    elif action == "replay":
        if r.status != "PENDING":
            raise _bad(409, f"record is {r.status}")
        payload = dict(r.payload or {})
        if isinstance((body or {}).get("patch"), dict):
            payload.update(body["patch"])          # operator correction (audited)
        mapping = {}
        if r.connector_id:
            c = db.get(Connector, r.connector_id)
            mapping = c.field_mapping if c else {}
        r.retries += 1
        db.commit()
        res = ingest_events(db, p.tenant_id, p.space, [payload], source=f"Replay of {r.id} ({r.source})",
                            connector_id=r.connector_id, field_mapping=mapping, caller=p.display)
        if res["accepted"]:
            r.status, r.replayed_at = "REPLAYED", utcnow()
            db.commit()
        result = {"status": r.status, "result": res}
    else:
        raise _bad(404, "unknown action", "NOT_FOUND")
    audit.record(db, p.tenant_id, f"dead_letter.{action}", actor=p, resource_type="dead_letter", resource_id=r.id,
                 after=result)
    return result


# --------------------------------------------------------------------------- reports
@router.get("/reports/catalogue", tags=["reports"])
def catalogue(p: Principal = Depends(require("reports.system", "reports.aml", "reports.assigned",
                                             "reports.executive"))):
    return {"reports": report_service.catalogue_for(p), "formats": ["pdf", "csv", "xlsx"]}


@router.get("/reports", tags=["reports"])
def list_reports(p: Principal = Depends(require("reports.system", "reports.aml", "reports.assigned",
                                                "reports.executive")), db: Session = Depends(get_db)):
    q = select(ReportJob).where(ReportJob.tenant_id == p.tenant_id, ReportJob.space == p.space)
    if not p.can("reports.aml", "reports.executive"):
        q = q.where(ReportJob.created_by == p.user_id)
    rows = db.execute(q.order_by(ReportJob.created_at.desc()).limit(200)).scalars().all()
    return {"reports": [row_dict(r, exclude=("storage_key",)) for r in rows]}


@router.post("/reports", status_code=201, tags=["reports"])
def generate_report(body: dict, p: Principal = Depends(require("reports.system", "reports.aml", "reports.assigned",
                                                               "reports.executive")), db: Session = Depends(get_db)):
    try:
        job = report_service.generate(db, p, body.get("type", ""), (body.get("format") or "pdf").lower(),
                                      body.get("params") or {})
    except PermissionError as exc:
        raise _bad(403, str(exc), "FORBIDDEN") from exc
    except LookupError as exc:
        raise _bad(404, str(exc), "NOT_FOUND") from exc
    except ValueError as exc:
        raise _bad(422, str(exc)) from exc
    audit.record(db, p.tenant_id, "report.generate", actor=p, resource_type="report", resource_id=job.id,
                 after={"type": job.report_type, "format": job.fmt, "params": job.params, "status": job.status},
                 outcome="SUCCESS" if job.status == "DONE" else "FAILURE")
    if job.status != "DONE":
        raise _bad(500, job.error or "report failed", "REPORT_FAILED")
    return row_dict(job, exclude=("storage_key",))


@router.get("/reports/{job_id}/download", tags=["reports"])
def download_report(job_id: str, p: Principal = Depends(require("reports.system", "reports.aml", "reports.assigned",
                                                                "reports.executive")),
                    db: Session = Depends(get_db)):
    job = db.get(ReportJob, job_id)
    if job is None or job.tenant_id != p.tenant_id or job.space != p.space or \
            (not p.can("reports.aml", "reports.executive") and job.created_by != p.user_id):
        raise _bad(404, "report not found", "NOT_FOUND")
    path = report_service.report_path(job)
    if path is None:
        raise _bad(404, "report file is not available", "NOT_FOUND")
    audit.record(db, p.tenant_id, "report.download", actor=p, resource_type="report", resource_id=job.id)
    media = {"pdf": "application/pdf", "csv": "text/csv",
             "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[job.fmt]
    return FileResponse(path, media_type=media, filename=f"{job.number}_{job.report_type}.{job.fmt}")


__all__ = ["router", "Optional"]
