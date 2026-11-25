"""Customer risk assessment (CRA): the institution's AML risk rating of each customer.

The rating combines weighted factors - geography, customer type, occupation / business activity, products,
onboarding channel, PEP and sanctions status, behaviour against the declared profile, adverse history, network
risk and tenure - into a score of 0-100 and a level (LOW / MEDIUM / HIGH). Some facts force a minimum level
(a confirmed sanctions or PEP match, an STR filed on the customer).

* **Onboarding**: the bank's onboarding system calls ``POST /api/v1/customers/risk-assessment`` with the KYC data
  and receives the rating and its factors before the account is opened.
* **Continuous**: the leader node re-assesses every customer daily (and on demand). Upgrades apply at once and
  open a KYC review when a customer becomes HIGH; downgrades wait for a second person's approval (four eyes).
* **Overrides**: the AML Manager can set a level with a reason; lowering a level always needs approval by another
  manager or the Chief AML. While an override is in force, automatic assessments are recorded but not applied.
"""
from __future__ import annotations

import copy
import math
from datetime import timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..models import (Alert, Case, ChequeReturn, Customer, NetworkScore, RiskAssessment, ScreeningMatch, StrReport,
                      Tenant, Transaction, utcnow)

LEVELS = ("LOW", "MEDIUM", "HIGH")
MODEL_VERSION = "CRA-1"

DEFAULT_MODEL = {
    "version": MODEL_VERSION,
    "thresholds": {"MEDIUM": 30, "HIGH": 55},
    "auto_downgrade": False,
    "override_valid_days": 365,
    "home_countries": ["PS"],
    "weights": {"geography": 15, "customer_type": 8, "occupation": 15, "products": 8, "onboarding": 6,
                "pep_sanctions": 18, "behaviour": 12, "adverse": 10, "network": 5, "tenure": 3},
    "occupation_risk": {
        "high": ["money exchange", "money exchanger", "exchange", "jewel", "gold", "precious", "real estate",
                 "car dealer", "used cars", "charity", "ngo", "non-profit", "casino", "gambling", "crypto",
                 "virtual asset", "arms", "remittance", "hawala"],
        "medium": ["import/export", "import", "export", "construction", "wholesale", "self-employed", "lawyer",
                   "accountant", "notary", "travel agency", "cash-intensive", "fuel", "restaurant"],
    },
    "product_risk": {"high": ["trade_finance", "private_banking", "correspondent", "safe_deposit"],
                     "medium": ["foreign_transfer", "prepaid_card", "cash_management", "pos_acquiring"]},
    "minimum_level": {"sanctions_match": "HIGH", "pep": "HIGH", "str_filed": "HIGH"},
}


def model(tenant: Tenant) -> dict:
    m = copy.deepcopy(DEFAULT_MODEL)
    custom = (tenant.settings or {}).get("cra") or {}
    for k, v in custom.items():
        if isinstance(v, dict) and isinstance(m.get(k), dict):
            m[k].update(v)
        else:
            m[k] = v
    return m


def level_of(score: float, m: dict) -> str:
    th = m["thresholds"]
    return "HIGH" if score >= float(th["HIGH"]) else "MEDIUM" if score >= float(th["MEDIUM"]) else "LOW"


def _rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


def _contains(text: str, words: list[str]) -> Optional[str]:
    t = (text or "").lower()
    return next((w for w in words if w and w.lower() in t), None)


# --------------------------------------------------------------------------- factors
def _static_factors(m: dict, data: dict, high_risk: set) -> list[dict]:
    """Factors that only need KYC data (also used at onboarding)."""
    f = []
    country = (data.get("country") or "").upper()
    nationality = (data.get("nationality") or "").upper()
    geo, detail = 5, "domestic customer"
    if country in high_risk or nationality in high_risk:
        geo, detail = 100, f"residence / nationality in a high-risk jurisdiction ({country or nationality})"
    elif country and country not in m["home_countries"]:
        geo, detail = 55, f"non-resident customer ({country})"
    elif nationality and nationality not in m["home_countries"]:
        geo, detail = 35, f"foreign national ({nationality})"
    f.append({"key": "geography", "label": "Geography", "points": geo, "detail": detail})

    ctype = (data.get("customer_type") or "INDIVIDUAL").upper()
    f.append({"key": "customer_type", "label": "Customer type", "points": 45 if ctype == "BUSINESS" else 15,
              "detail": "legal entity" if ctype == "BUSINESS" else "individual"})

    activity = " ".join(str(data.get(k) or "") for k in ("business_activity", "occupation"))
    hit_h = _contains(activity, m["occupation_risk"]["high"])
    hit_m = _contains(activity, m["occupation_risk"]["medium"])
    occ = 95 if hit_h else 50 if hit_m else 10
    f.append({"key": "occupation", "label": "Occupation / business activity", "points": occ,
              "detail": f"{activity.strip() or 'not declared'}" + (f" (high-risk sector: {hit_h})" if hit_h else
                                                                    f" (medium-risk sector: {hit_m})" if hit_m else "")})

    products = [str(p).lower() for p in data.get("products") or []]
    ph = [p for p in products if p in m["product_risk"]["high"]]
    pm = [p for p in products if p in m["product_risk"]["medium"]]
    f.append({"key": "products", "label": "Products and services", "points": 85 if ph else 45 if pm else 10,
              "detail": ", ".join(ph or pm) or "standard retail products"})

    channel = str((data.get("attributes") or {}).get("onboarding_channel") or data.get("onboarding_channel") or "branch")
    digital = channel.lower() in ("digital", "online", "mobile", "remote", "non_face_to_face")
    f.append({"key": "onboarding", "label": "Onboarding channel", "points": 60 if digital else 15,
              "detail": "non face-to-face onboarding" if digital else "identified in a branch"})

    months = int(data.get("tenure_months") or 0)
    f.append({"key": "tenure", "label": "Length of relationship", "points": 60 if months < 6 else 30 if months < 24 else 5,
              "detail": f"{months} months with the bank"})
    return f


def _dynamic_factors(db: Session, tenant_id: str, space: str, c: Customer, m: dict, high_risk: set) -> tuple[list, list]:
    """Factors from the customer's activity and investigation history. Returns (factors, forced minimum levels)."""
    now = utcnow()
    forced: list[tuple[str, str]] = []
    f = []
    # ---- PEP / sanctions
    matches = db.execute(select(ScreeningMatch).where(ScreeningMatch.tenant_id == tenant_id, ScreeningMatch.space == space,
                                                      ScreeningMatch.customer_ref == c.customer_ref)).scalars().all()
    true_sanction = [x for x in matches if x.status == "TRUE_MATCH" and x.list_code != "PEP"]
    true_pep = [x for x in matches if x.status == "TRUE_MATCH" and x.list_code == "PEP"]
    open_m = [x for x in matches if x.status in ("OPEN", "ESCALATED")]
    pts, detail = 0, "no screening matches"
    if true_sanction:
        pts, detail = 100, f"confirmed {true_sanction[0].list_code} sanctions match"
        forced.append((m["minimum_level"].get("sanctions_match", "HIGH"), "confirmed sanctions match"))
    elif true_pep:
        pts, detail = 90, "politically exposed person (confirmed)"
        forced.append((m["minimum_level"].get("pep", "HIGH"), "politically exposed person"))
    elif open_m:
        pts, detail = 50, f"{len(open_m)} unresolved screening match(es)"
    f.append({"key": "pep_sanctions", "label": "PEP and sanctions", "points": pts, "detail": detail})

    # ---- behaviour vs declared profile (last 30 days)
    since = now - timedelta(days=30)
    rows = db.execute(select(Transaction.tx_type, Transaction.amount, Transaction.beneficiary_country).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.customer_ref == c.customer_ref,
        Transaction.ts >= since)).all()
    total = sum(float(a) for _, a, _ in rows)
    cash = sum(float(a) for t, a, _ in rows if t in ("cash_deposit", "cash_withdrawal"))
    hr = sum(float(a) for _, a, cc in rows if cc and cc.upper() in high_risk)
    expected = float(c.expected_monthly_turnover or 0)
    ratio = total / expected if expected > 0 else (2.0 if total > 0 else 0.0)
    pts, notes = 5, []
    if ratio >= 3:
        pts = 90
        notes.append(f"turnover {ratio:.1f}x the declared monthly amount")
    elif ratio >= 1.5:
        pts = 50
        notes.append(f"turnover {ratio:.1f}x the declared monthly amount")
    if total > 0 and cash / total > 0.6 and cash > 5000:
        pts = max(pts, 60)
        notes.append(f"{100 * cash / total:.0f}% of value in cash")
    f.append({"key": "behaviour", "label": "Activity against the declared profile", "points": pts,
              "detail": "; ".join(notes) or "activity consistent with the declared profile"})
    # high-risk counterparties raise geography
    if total > 0 and hr / total > 0.05:
        f.append({"key": "geography_flows", "label": "Flows with high-risk jurisdictions",
                  "points": 85 if hr / total > 0.2 else 50,
                  "detail": f"{100 * hr / total:.0f}% of value with high-risk countries in 30 days", "weight_of": "geography"})

    # ---- adverse history
    alerts = db.execute(select(func.count()).select_from(Alert).where(
        Alert.tenant_id == tenant_id, Alert.space == space, Alert.customer_ref == c.customer_ref,
        Alert.created_at >= now - timedelta(days=180))).scalar() or 0
    strs = db.execute(select(StrReport).where(StrReport.tenant_id == tenant_id, StrReport.space == space,
                                              StrReport.status.in_(("APPROVED", "SUBMITTED")))).scalars().all()
    str_hit = any(c.customer_ref == (s.get("ref") or "") for r in strs for s in (r.subjects or []))
    returned = db.execute(select(func.count()).select_from(ChequeReturn).where(
        ChequeReturn.tenant_id == tenant_id, ChequeReturn.space == space, ChequeReturn.customer_ref == c.customer_ref,
        ChequeReturn.role == "DRAWER", ChequeReturn.returned_at >= now - timedelta(days=365))).scalar() or 0
    pts, notes = 0, []
    if str_hit:
        pts = 100
        notes.append("suspicious transaction report filed")
        forced.append((m["minimum_level"].get("str_filed", "HIGH"), "STR filed on the customer"))
    if alerts >= 5:
        pts = max(pts, 70)
    elif alerts >= 2:
        pts = max(pts, 35)
    if alerts:
        notes.append(f"{alerts} alert(s) in 180 days")
    if returned >= 3:
        pts = max(pts, 75)
        notes.append(f"{returned} returned cheque(s) in 12 months")
    elif returned:
        pts = max(pts, 35)
        notes.append(f"{returned} returned cheque(s) in 12 months")
    f.append({"key": "adverse", "label": "Alerts, reports and returned cheques", "points": pts,
              "detail": "; ".join(notes) or "no adverse history"})

    # ---- network
    ns = db.execute(select(NetworkScore).where(NetworkScore.tenant_id == tenant_id, NetworkScore.space == space,
                                               NetworkScore.customer_ref == c.customer_ref)).scalar()
    score = ns.score if ns else 0.0
    f.append({"key": "network", "label": "Links to risky networks", "points": round(100 * min(1.0, score)),
              "detail": (f"network risk {score:.2f}, ring of {ns.ring_size}" if ns and score else "no risky links")})
    return f, forced


def _combine(m: dict, factors: list[dict]) -> float:
    weights = m["weights"]
    by_key: dict[str, dict] = {}
    for x in factors:
        key = x.get("weight_of") or x["key"]
        if key not in by_key or x["points"] > by_key[key]["points"]:
            by_key[key] = x
    total_w = sum(float(weights.get(k, 0)) for k in by_key) or 1.0
    for x in factors:
        key = x.get("weight_of") or x["key"]
        w = float(weights.get(key, 0))
        x["weight"] = w
        x["contribution"] = round(w * x["points"] / total_w, 1) if by_key.get(key) is x else 0.0
    return round(sum(x["contribution"] for x in factors), 1)


def assess(db: Session, tenant: Tenant, space: str, c: Customer) -> dict:
    from .policy import active_policy
    m = model(tenant)
    high_risk = {x.upper() for x in active_policy(db, tenant.id).document.get("engine_thresholds", {})
                 .get("high_risk_countries", [])}
    data = {"country": c.country, "nationality": (c.attributes or {}).get("nationality"), "customer_type": c.customer_type,
            "business_activity": c.business_activity, "occupation": (c.attributes or {}).get("occupation"),
            "products": c.products, "attributes": c.attributes, "tenure_months": c.tenure_months}
    factors = _static_factors(m, data, high_risk)
    dyn, forced = _dynamic_factors(db, tenant.id, space, c, m, high_risk)
    factors += dyn
    score = _combine(m, factors)
    level = level_of(score, m)
    reasons = []
    for lvl, why in forced:
        if _rank(lvl) > _rank(level):
            level = lvl
            reasons.append(why)
    factors.sort(key=lambda x: -x["contribution"])
    return {"score": score, "level": level, "factors": factors, "forced": reasons, "model_version": m["version"]}


def assess_onboarding(db: Session, tenant: Tenant, data: dict) -> dict:
    """Rating for a prospective customer from KYC data only (no transaction history)."""
    from .policy import active_policy
    m = model(tenant)
    high_risk = {x.upper() for x in active_policy(db, tenant.id).document.get("engine_thresholds", {})
                 .get("high_risk_countries", [])}
    factors = _static_factors(m, data, high_risk)
    forced = []
    if data.get("pep"):
        factors.append({"key": "pep_sanctions", "label": "PEP and sanctions", "points": 90,
                        "detail": "declared politically exposed person"})
        forced.append((m["minimum_level"].get("pep", "HIGH"), "politically exposed person"))
    else:
        from . import screening
        name = data.get("display_name") or data.get("name") or ""
        hits = screening.screen_name(db, tenant.id, name, 0.88)[:1] if name else []
        if hits:
            h = hits[0]
            factors.append({"key": "pep_sanctions", "label": "PEP and sanctions", "points": 80,
                            "detail": f"possible {h['list_code']} match '{h['name']}' ({h['score']:.2f}) - verify"})
        else:
            factors.append({"key": "pep_sanctions", "label": "PEP and sanctions", "points": 0,
                            "detail": "no screening match"})
    score = _combine(m, factors)
    level = level_of(score, m)
    for lvl, why in forced:
        if _rank(lvl) > _rank(level):
            level = lvl
    factors.sort(key=lambda x: -x["contribution"])
    return {"score": score, "level": level, "factors": factors, "model_version": m["version"],
            "edd_required": level == "HIGH"}


# --------------------------------------------------------------------------- applying ratings
def latest(db: Session, tenant_id: str, space: str, ref: str, applied_only: bool = False) -> Optional[RiskAssessment]:
    q = select(RiskAssessment).where(RiskAssessment.tenant_id == tenant_id, RiskAssessment.space == space,
                                     RiskAssessment.customer_ref == ref)
    if applied_only:
        q = q.where(RiskAssessment.status == "APPLIED")
    return db.execute(q.order_by(RiskAssessment.created_at.desc())).scalars().first()


def _override_in_force(db: Session, tenant_id: str, space: str, ref: str, m: dict) -> Optional[RiskAssessment]:
    ov = db.execute(select(RiskAssessment).where(
        RiskAssessment.tenant_id == tenant_id, RiskAssessment.space == space, RiskAssessment.customer_ref == ref,
        RiskAssessment.method == "OVERRIDE", RiskAssessment.status == "APPLIED")
        .order_by(RiskAssessment.created_at.desc())).scalars().first()
    if ov and ov.created_at > utcnow() - timedelta(days=int(m["override_valid_days"])):
        return ov
    return None


def _apply_level(db: Session, tenant: Tenant, space: str, c: Customer, level: str, why: str) -> None:
    previous = c.risk_category
    c.risk_category = level
    c.updated_at = utcnow()
    if level == "HIGH" and previous != "HIGH":
        from .operations import trigger_review
        trigger_review(db, tenant.id, space, c.customer_ref, "RISK_UPGRADE", days=30)


def reassess(db: Session, tenant: Tenant, space: str, c: Customer, actor: str = "CRA engine") -> Optional[RiskAssessment]:
    """Assess and record; upgrades apply, downgrades wait for approval (unless auto_downgrade)."""
    m = model(tenant)
    r = assess(db, tenant, space, c)
    prev = latest(db, tenant.id, space, c.customer_ref)
    if prev is not None and prev.method == "AUTO" and prev.level == r["level"] and abs(prev.score - r["score"]) < 5 \
            and prev.status in ("APPLIED", "INFO"):
        return None                     # nothing material changed since the last assessment
    row = RiskAssessment(tenant_id=tenant.id, space=space, customer_ref=c.customer_ref, score=r["score"],
                         level=r["level"], previous_level=c.risk_category or "", factors=r["factors"], method="AUTO",
                         requested_by=actor, model_version=r["model_version"],
                         reason="; ".join(r["forced"]) or "periodic automatic assessment")
    current = c.risk_category or "LOW"
    if _override_in_force(db, tenant.id, space, c.customer_ref, m):
        row.status = "INFO"
        row.reason = "manual override in force - recorded, not applied"
    elif _rank(r["level"]) > _rank(current):
        row.status = "APPLIED"
        _apply_level(db, tenant, space, c, r["level"], "upgrade")
    elif _rank(r["level"]) < _rank(current):
        if m["auto_downgrade"]:
            row.status = "APPLIED"
            _apply_level(db, tenant, space, c, r["level"], "downgrade")
        else:
            pending = db.execute(select(RiskAssessment).where(
                RiskAssessment.tenant_id == tenant.id, RiskAssessment.space == space,
                RiskAssessment.customer_ref == c.customer_ref, RiskAssessment.status == "PENDING_APPROVAL")).scalars().first()
            if pending:
                return None
            row.status = "PENDING_APPROVAL"
            row.reason = f"downgrade {current} -> {r['level']} needs approval"
    else:
        row.status = "APPLIED"
    db.add(row)
    db.commit()
    return row


def override(db: Session, tenant: Tenant, space: str, ref: str, level: str, reason: str, actor: str) -> RiskAssessment:
    if level not in LEVELS:
        raise ValueError("level must be LOW, MEDIUM or HIGH")
    if len((reason or "").strip()) < 15:
        raise ValueError("give a reason of at least 15 characters")
    c = _customer(db, tenant.id, space, ref)
    r = assess(db, tenant, space, c)
    row = RiskAssessment(tenant_id=tenant.id, space=space, customer_ref=ref, score=r["score"], level=level,
                         previous_level=c.risk_category or "", factors=r["factors"], method="OVERRIDE",
                         requested_by=actor, reason=reason.strip()[:400], model_version=r["model_version"])
    if _rank(level) >= _rank(c.risk_category or "LOW"):
        row.status, row.approved_by = "APPLIED", actor
        _apply_level(db, tenant, space, c, level, "override")
    else:
        row.status = "PENDING_APPROVAL"
    db.add(row)
    db.commit()
    audit.record(db, tenant.id, "cra.override", actor=actor, resource_type="customer", resource_id=ref, space=space,
                 before={"level": row.previous_level}, after={"level": level, "status": row.status}, reason=reason)
    return row


def decide(db: Session, tenant: Tenant, space: str, rid: str, approve: bool, actor: str, actor_name: str) -> RiskAssessment:
    row = db.get(RiskAssessment, rid)
    if row is None or row.tenant_id != tenant.id or row.space != space:
        raise LookupError("assessment not found")
    if row.status != "PENDING_APPROVAL":
        raise ValueError(f"assessment is {row.status}")
    if row.method == "OVERRIDE" and row.requested_by == actor_name:
        raise PermissionError("a downgrade must be approved by another person (four eyes)")
    c = _customer(db, tenant.id, space, row.customer_ref)
    if approve:
        row.status, row.approved_by = "APPLIED", actor_name
        _apply_level(db, tenant, space, c, row.level, "approved downgrade")
    else:
        row.status, row.approved_by = "REJECTED", actor_name
    db.commit()
    audit.record(db, tenant.id, "cra.approve" if approve else "cra.reject", actor=actor, resource_type="customer",
                 resource_id=row.customer_ref, space=space, after={"level": row.level, "status": row.status})
    return row


def run_all(db: Session, tenant: Tenant, space: str) -> dict:
    out = {"assessed": 0, "upgraded": 0, "pending_downgrades": 0}
    for c in db.execute(select(Customer).where(Customer.tenant_id == tenant.id, Customer.space == space)).scalars().all():
        row = reassess(db, tenant, space, c)
        out["assessed"] += 1
        if row is None:
            continue
        if row.status == "APPLIED" and _rank(row.level) > _rank(row.previous_level or "LOW"):
            out["upgraded"] += 1
        elif row.status == "PENDING_APPROVAL":
            out["pending_downgrades"] += 1
    return out


def _customer(db: Session, tenant_id: str, space: str, ref: str) -> Customer:
    c = db.execute(select(Customer).where(Customer.tenant_id == tenant_id, Customer.space == space,
                                          Customer.customer_ref == ref)).scalar()
    if c is None:
        raise LookupError("customer not found")
    return c


def distribution(db: Session, tenant_id: str, space: str) -> dict:
    return dict(db.execute(select(Customer.risk_category, func.count()).where(
        Customer.tenant_id == tenant_id, Customer.space == space).group_by(Customer.risk_category)).all())


def as_dict(r: RiskAssessment) -> dict:
    return {"id": r.id, "customer_ref": r.customer_ref, "score": r.score, "level": r.level,
            "previous_level": r.previous_level, "factors": r.factors, "method": r.method, "status": r.status,
            "reason": r.reason, "requested_by": r.requested_by, "approved_by": r.approved_by,
            "model_version": r.model_version, "created_at": r.created_at}


__all__ = ["assess", "assess_onboarding", "reassess", "override", "decide", "run_all", "latest", "model",
           "distribution", "as_dict", "DEFAULT_MODEL", "math", "Case"]


def initialise(db: Session, tenant: Tenant, space: str) -> dict:
    """First rating of every customer without an assessment (go-live / data import): the computed level is
    applied directly, as an onboarding rating would be."""
    counts = {"LOW": 0, "MEDIUM": 0, "HIGH": 0}
    rated = {r for (r,) in db.execute(select(RiskAssessment.customer_ref).where(
        RiskAssessment.tenant_id == tenant.id, RiskAssessment.space == space).distinct()).all()}
    for c in db.execute(select(Customer).where(Customer.tenant_id == tenant.id, Customer.space == space)).scalars().all():
        if c.customer_ref in rated:
            continue
        r = assess(db, tenant, space, c)
        db.add(RiskAssessment(tenant_id=tenant.id, space=space, customer_ref=c.customer_ref, score=r["score"],
                              level=r["level"], previous_level=c.risk_category or "", factors=r["factors"],
                              method="ONBOARDING", status="APPLIED", requested_by="initial rating",
                              reason="initial risk rating at go-live", model_version=r["model_version"]))
        c.risk_category = r["level"]
        counts[r["level"]] += 1
    db.commit()
    return counts
