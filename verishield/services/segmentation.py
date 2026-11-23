"""Customer onboarding intelligence (BRD section 9): data quality, peer segmentation, cold start.

* **Data-quality report** over imported customers and history.
* **Segmentation model**: k-means (k chosen by silhouette) over standardised, approved features -
  declared KYC profile (customer type, expected turnover/volume, tenure, product count) plus
  behavioural aggregates when history exists. Protected attributes are never used.
* **Cold-start assignment**: a customer with no history is placed in the nearest segment using the
  KYC subspace only; the basis and confidence are shown and the customer is marked limited-history.

A segment is a peer-comparison aid - not a risk grade and never a standalone reason to block.
"""
from __future__ import annotations

import math
import threading
from collections import Counter, defaultdict
from typing import Optional

import numpy as np
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..models import Customer, CustomerSegment, DeadLetterEvent, ModelVersion, Transaction, utcnow

KYC_FEATURES = ("is_business", "log_expected_turnover", "log_expected_tx", "tenure_years", "product_count")
BEHAVIOUR_FEATURES = ("log_tx_per_month", "log_median_amount", "log_distinct_beneficiaries", "foreign_share",
                      "transfer_share", "recharge_share", "prepaid_share", "night_share")
FEATURE_LABELS = {
    "is_business": "business customer share", "log_expected_turnover": "declared monthly turnover",
    "log_expected_tx": "declared monthly transactions", "tenure_years": "relationship tenure (years)",
    "product_count": "products held", "log_tx_per_month": "transactions per month",
    "log_median_amount": "median amount", "log_distinct_beneficiaries": "distinct beneficiaries",
    "foreign_share": "cross-border share", "transfer_share": "transfer share", "recharge_share": "phone recharge share",
    "prepaid_share": "prepaid card share", "night_share": "night activity share",
}
DISTINCT_TEXT = {"recharge_share": "phone recharges", "prepaid_share": "prepaid card top-ups",
                 "product_count": "products", "log_distinct_beneficiaries": "beneficiaries",
                 "log_expected_tx": "declared transactions"}
LOG_FEATURES = {"log_expected_turnover", "log_expected_tx", "log_tx_per_month", "log_median_amount",
                "log_distinct_beneficiaries"}
MIN_HISTORY = 10

_cache: dict[tuple[str, str], dict] = {}
_cache_lock = threading.Lock()


# --------------------------------------------------------------------------- features
def _home_country(db: Session, tenant_id: str, space: str) -> Optional[str]:
    row = db.execute(select(Transaction.beneficiary_country, func.count()).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.beneficiary_country.isnot(None))
        .group_by(Transaction.beneficiary_country).order_by(func.count().desc()).limit(1)).first()
    return row[0] if row else None


def behaviour_stats(db: Session, tenant_id: str, space: str, customer_ref: Optional[str] = None) -> dict:
    q = select(Transaction.customer_ref, Transaction.ts, Transaction.amount, Transaction.tx_type,
               Transaction.beneficiary, Transaction.beneficiary_country).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space)
    if customer_ref:
        q = q.where(Transaction.customer_ref == customer_ref)
    home = _home_country(db, tenant_id, space)
    acc: dict[str, dict] = defaultdict(lambda: {"amounts": [], "bens": set(), "types": Counter(), "foreign": 0,
                                                "night": 0, "first": None, "last": None})
    for ref, ts, amount, tx_type, ben, country in db.execute(q):
        a = acc[ref]
        a["amounts"].append(amount)
        if ben:
            a["bens"].add(ben)
        a["types"][tx_type] += 1
        if country and home and country != home:
            a["foreign"] += 1
        if ts.hour < 5:
            a["night"] += 1
        a["first"] = ts if a["first"] is None or ts < a["first"] else a["first"]
        a["last"] = ts if a["last"] is None or ts > a["last"] else a["last"]
    out = {}
    for ref, a in acc.items():
        n = len(a["amounts"])
        months = max(1.0, (a["last"] - a["first"]).days / 30.0) if n > 1 else 1.0
        out[ref] = {
            "n": n,
            "log_tx_per_month": math.log1p(n / months),
            "log_median_amount": math.log1p(float(np.median(a["amounts"]))),
            "log_distinct_beneficiaries": math.log1p(len(a["bens"])),
            "foreign_share": a["foreign"] / n,
            "transfer_share": a["types"]["bank_transfer"] / n,
            "recharge_share": a["types"]["phone_recharge"] / n,
            "prepaid_share": a["types"]["prepaid_card"] / n,
            "night_share": a["night"] / n,
        }
    return out


def kyc_vector(c: Customer) -> list[float]:
    return [1.0 if c.customer_type == "BUSINESS" else 0.0,
            math.log1p(max(0.0, c.expected_monthly_turnover or 0.0)),
            math.log1p(max(0.0, c.expected_monthly_tx or 0.0)),
            (c.tenure_months or 0) / 12.0,
            float(len(c.products or []))]


# --------------------------------------------------------------------------- k-means
def _kmeans(x: np.ndarray, k: int, seed: int, iters: int = 60) -> tuple[np.ndarray, np.ndarray, float]:
    rng = np.random.default_rng(seed)
    n = len(x)
    centers = [x[rng.integers(n)]]
    for _ in range(1, k):
        d2 = np.min(((x[:, None, :] - np.array(centers)[None]) ** 2).sum(-1), axis=1)
        probs = d2 / d2.sum() if d2.sum() > 0 else np.full(n, 1 / n)
        centers.append(x[rng.choice(n, p=probs)])
    c = np.array(centers)
    labels = np.zeros(n, dtype=int)
    for _ in range(iters):
        d = ((x[:, None, :] - c[None]) ** 2).sum(-1)
        new = d.argmin(1)
        if np.array_equal(new, labels) and _ > 0:
            break
        labels = new
        for j in range(k):
            members = x[labels == j]
            c[j] = members.mean(0) if len(members) else x[rng.integers(n)]
    inertia = float(((x - c[labels]) ** 2).sum())
    return labels, c, inertia


def _silhouette(x: np.ndarray, labels: np.ndarray, sample: int = 1200, seed: int = 0) -> float:
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(x), size=min(sample, len(x)), replace=False)
    xs, ls = x[idx], labels[idx]
    d = np.sqrt(((xs[:, None, :] - xs[None]) ** 2).sum(-1))
    scores = []
    for i in range(len(xs)):
        same = ls == ls[i]
        if same.sum() <= 1:
            continue
        a = d[i, same].sum() / (same.sum() - 1)
        b = min((d[i, ls == j].mean() for j in set(ls.tolist()) if j != ls[i]), default=0.0)
        scores.append((b - a) / max(a, b) if max(a, b) > 0 else 0.0)
    return float(np.mean(scores)) if scores else 0.0


def _rand_index(a: np.ndarray, b: np.ndarray, sample: int = 600, seed: int = 1) -> float:
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(a), size=min(sample, len(a)), replace=False)
    sa, sb = a[idx], b[idx]
    same_a = sa[:, None] == sa[None]
    same_b = sb[:, None] == sb[None]
    return float((same_a == same_b).mean())


# --------------------------------------------------------------------------- data quality
def data_quality(db: Session, tenant_id: str, space: str) -> dict:
    customers = db.execute(select(Customer).where(Customer.tenant_id == tenant_id, Customer.space == space)
                           ).scalars().all()
    tx_q = select(func.count(Transaction.id), func.min(Transaction.ts), func.max(Transaction.ts),
                  func.count(func.distinct(Transaction.customer_ref))).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space)
    n_tx, first, last, tx_customers = db.execute(tx_q).one()
    hist = db.execute(select(func.count(Transaction.id)).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.mode == "HISTORY")).scalar()
    missing_fields = {}
    fields = {"customer_type": lambda c: c.customer_type, "risk_category": lambda c: c.risk_category not in
              ("", "UNKNOWN", None) and c.risk_category, "expected_monthly_turnover": lambda c: c.expected_monthly_turnover,
              "expected_monthly_tx": lambda c: c.expected_monthly_tx, "tenure_months": lambda c: c.tenure_months,
              "products": lambda c: c.products, "business_activity": lambda c: c.business_activity,
              "country": lambda c: c.country}
    for name, getter in fields.items():
        missing = sum(1 for c in customers if not getter(c))
        missing_fields[name] = {"missing": missing, "pct": round(100 * missing / len(customers), 1) if customers else 0}
    kyc_refs = {c.customer_ref for c in customers if c.kyc_review_status != "NOT_PROVIDED"}
    counts = dict(db.execute(select(Transaction.customer_ref, func.count()).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space).group_by(Transaction.customer_ref)).all())
    rejected = dict(db.execute(select(DeadLetterEvent.category, func.count()).where(
        DeadLetterEvent.tenant_id == tenant_id, DeadLetterEvent.space == space).group_by(DeadLetterEvent.category))
        .all())
    tx_types = dict(db.execute(select(Transaction.tx_type, func.count()).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space).group_by(Transaction.tx_type)).all())
    days = (last - first).days + 1 if first and last else 0
    issues = []
    if not customers:
        issues.append("No customer master / KYC records imported.")
    if n_tx == 0:
        issues.append("No transaction history imported.")
    no_kyc = len([r for r in counts if r not in kyc_refs])
    if no_kyc:
        issues.append(f"{no_kyc} customers appear in transactions without a KYC record.")
    for name, m in missing_fields.items():
        if m["pct"] >= 20:
            issues.append(f"{m['pct']}% of customers are missing '{name}'.")
    if days and days < 30:
        issues.append(f"History covers only {days} days (at least 30 recommended).")
    return {
        "generated_at": utcnow().isoformat(timespec="seconds") + "Z",
        "customers": len(customers),
        "customers_with_kyc": len(kyc_refs),
        "transactions": n_tx, "history_transactions": hist,
        "customers_in_transactions": tx_customers,
        "customers_without_history": sum(1 for c in customers if counts.get(c.customer_ref, 0) == 0),
        "customers_limited_history": sum(1 for c in customers if 0 < counts.get(c.customer_ref, 0) < MIN_HISTORY),
        "transactions_without_kyc_customer": sum(v for r, v in counts.items() if r not in kyc_refs),
        "time_coverage": {"from": first.isoformat(timespec="seconds") + "Z" if first else None,
                          "to": last.isoformat(timespec="seconds") + "Z" if last else None, "days": days},
        "missing_fields": missing_fields,
        "rejected_records": rejected,
        "transaction_types": tx_types,
        "issues": issues,
        "ready_for_modelling": bool(customers) and n_tx >= 50,
    }


# --------------------------------------------------------------------------- build
def build_segmentation(db: Session, tenant_id: str, space: str, actor: str = "system") -> ModelVersion:
    customers = db.execute(select(Customer).where(Customer.tenant_id == tenant_id, Customer.space == space)
                           ).scalars().all()
    if len(customers) < 12:
        raise ValueError("at least 12 customers are needed to build peer segments")
    beh = behaviour_stats(db, tenant_id, space)
    beh_mean = {f: float(np.mean([b[f] for b in beh.values()])) if beh else 0.0 for f in BEHAVIOUR_FEATURES}
    rows, refs, has_hist = [], [], []
    for c in customers:
        b = beh.get(c.customer_ref)
        vec = kyc_vector(c) + [(b[f] if b else beh_mean[f]) for f in BEHAVIOUR_FEATURES]
        rows.append(vec)
        refs.append(c.customer_ref)
        has_hist.append(bool(b and b["n"] >= MIN_HISTORY))
    raw = np.asarray(rows, dtype=float)
    mu, sd = raw.mean(0), raw.std(0)
    sd[sd < 1e-9] = 1.0
    x = (raw - mu) / sd
    nk = len(KYC_FEATURES)
    # behaviour has more weight when it exists; KYC drives cold start
    weights = np.array([1.0] * nk + [1.2] * len(BEHAVIOUR_FEATURES))
    xw = x * weights

    best = None
    for k in range(3, min(8, len(customers) // 4) + 1):
        labels, centers, inertia = _kmeans(xw, k, seed=7)
        sil = _silhouette(xw, labels)
        if best is None or sil > best["silhouette"] + 0.01:
            best = {"k": k, "labels": labels, "centers": centers, "silhouette": sil, "inertia": inertia}
    if best is None:
        labels, centers, inertia = _kmeans(xw, 2, seed=7)
        best = {"k": 2, "labels": labels, "centers": centers, "silhouette": _silhouette(xw, labels), "inertia": inertia}
    stab = []
    for seed in (11, 23):
        alt, _, _ = _kmeans(xw, best["k"], seed=seed)
        stab.append(_rand_index(best["labels"], alt))
    labels, centers = best["labels"], best["centers"]

    seq = int(db.execute(select(func.max(ModelVersion.seq)).where(
        ModelVersion.tenant_id == tenant_id, ModelVersion.space == space, ModelVersion.kind == "SEGMENTATION"))
        .scalar() or 0) + 1
    model_id = f"{tenant_id}-{'DEMO-' if space == 'DEMO' else ''}SEG-{seq:02d}"
    features = list(KYC_FEATURES) + list(BEHAVIOUR_FEATURES)
    centers_raw = (centers / weights) * sd + mu

    turnover_q = np.quantile(raw[:, 1], [0.33, 0.66])
    activity_q = np.quantile(raw[:, nk], [0.33, 0.66])
    segments = []
    for j in range(best["k"]):
        members = labels == j
        cr = centers_raw[j]
        kind = "Business" if cr[0] > 0.6 else "Individual" if cr[0] < 0.25 else "Mixed"
        turnover = "high" if cr[1] > turnover_q[1] else "low" if cr[1] < turnover_q[0] else "medium"
        activity = "high" if cr[nk] > activity_q[1] else "low" if cr[nk] < activity_q[0] else "regular"
        extra = " · cross-border" if cr[nk + 3] > 0.25 else ""
        z = centers[j] / weights
        skip = {0, 1, nk}
        order = [i for i in np.argsort(-np.abs(z)) if i not in skip]
        if order and abs(z[order[0]]) >= 0.5:
            i = order[0]
            extra += f" · {'more' if z[i] > 0 else 'fewer'} {DISTINCT_TEXT.get(features[i], FEATURE_LABELS[features[i]])}" \
                if features[i] in DISTINCT_TEXT else f" · {'higher' if z[i] > 0 else 'lower'} {FEATURE_LABELS[features[i]]}"
        label = f"S{j + 1} · {kind} · {turnover} declared turnover · {activity} activity{extra}"
        profile = {}
        for i, f in enumerate(features):
            vals = raw[members, i]
            conv = (lambda v: math.expm1(v)) if f in LOG_FEATURES else (lambda v: v)
            profile[f] = {"label": FEATURE_LABELS[f], "centre": round(conv(float(cr[i])), 3),
                          "p10": round(conv(float(np.percentile(vals, 10))), 3) if len(vals) else None,
                          "p90": round(conv(float(np.percentile(vals, 90))), 3) if len(vals) else None}
        segments.append({"key": j, "label": label, "size": int(members.sum()), "profile": profile,
                         "with_history": int(np.asarray(has_hist)[members].sum())})

    artifact = {"features": features, "mu": mu.tolist(), "sd": sd.tolist(), "weights": weights.tolist(),
                "centers": centers.tolist(), "kyc_dims": nk, "labels": {s["key"]: s["label"] for s in segments}}
    mv = ModelVersion(id=model_id, tenant_id=tenant_id, space=space, kind="SEGMENTATION", seq=seq, status="TRAINED",
                      metrics={"k": best["k"], "silhouette": round(best["silhouette"], 3),
                               "stability_rand_index": round(float(np.mean(stab)), 3),
                               "customers": len(customers), "with_history": int(sum(has_hist)),
                               "cold_start": int(len(customers) - sum(has_hist)),
                               "segment_sizes": {s["label"]: s["size"] for s in segments},
                               "artifact": artifact},
                      features=features, training_size=len(customers), trained_by=actor,
                      notes="k-means over standardised KYC + behavioural features; k selected by silhouette.")
    db.add(mv)
    for s in segments:
        db.add(CustomerSegment(tenant_id=tenant_id, space=space, model_version=model_id, segment_key=s["key"],
                               label=s["label"], size=s["size"], profile=s["profile"],
                               description=f"{s['size']} customers ({s['with_history']} with established history). "
                                           "Neutral peer-comparison group - not a risk classification."))
    db.commit()
    return mv


def activate_segmentation(db: Session, model_id: str, actor: str = "system") -> ModelVersion:
    mv = db.get(ModelVersion, model_id)
    if mv is None or mv.kind != "SEGMENTATION":
        raise LookupError(model_id)
    db.execute(update(ModelVersion).where(ModelVersion.tenant_id == mv.tenant_id, ModelVersion.space == mv.space,
                                          ModelVersion.kind == "SEGMENTATION", ModelVersion.status == "ACTIVE",
                                          ModelVersion.id != model_id).values(status="RETIRED"))
    mv.status = "ACTIVE"
    mv.activated_at = utcnow()
    mv.approved_by = actor
    db.commit()
    with _cache_lock:
        _cache.pop((mv.tenant_id, mv.space), None)
    assign_all(db, mv.tenant_id, mv.space)
    return mv


def _active(db: Session, tenant_id: str, space: str) -> Optional[dict]:
    with _cache_lock:
        cached = _cache.get((tenant_id, space))
    mv = db.execute(select(ModelVersion).where(ModelVersion.tenant_id == tenant_id, ModelVersion.space == space,
                                               ModelVersion.kind == "SEGMENTATION", ModelVersion.status == "ACTIVE")
                    ).scalar()
    if mv is None:
        return None
    if cached and cached["id"] == mv.id:
        return cached
    art = mv.metrics["artifact"]
    model = {"id": mv.id, "mu": np.asarray(art["mu"]), "sd": np.asarray(art["sd"]), "w": np.asarray(art["weights"]),
             "centers": np.asarray(art["centers"]), "nk": art["kyc_dims"],
             "labels": {int(k): v for k, v in art["labels"].items()}}
    with _cache_lock:
        _cache[(tenant_id, space)] = model
    return model


def _assign(model: dict, kyc: list[float], beh: Optional[dict]) -> tuple[int, float, str]:
    nk = model["nk"]
    if beh and beh["n"] >= MIN_HISTORY:
        raw = np.asarray(kyc + [beh[f] for f in BEHAVIOUR_FEATURES])
        x = (raw - model["mu"]) / model["sd"] * model["w"]
        d = np.sqrt(((model["centers"] - x) ** 2).sum(1))
        basis = f"KYC profile + {beh['n']} transactions"
    else:
        raw = np.asarray(kyc)
        x = (raw - model["mu"][:nk]) / model["sd"][:nk] * model["w"][:nk]
        d = np.sqrt(((model["centers"][:, :nk] - x) ** 2).sum(1))
        basis = "KYC profile only (cold start - limited history)" if not beh else \
            f"KYC profile (limited history: {beh['n']} transactions)"
    order = np.argsort(d)
    d1 = float(d[order[0]])
    d2 = float(d[order[1]]) if len(order) > 1 else d1 + 1.0
    confidence = 0.5 + 0.5 * (1 - d1 / d2) if d2 > 0 else 1.0
    if "KYC profile only" in basis or "limited" in basis:
        confidence *= 0.85
    return int(order[0]), round(float(confidence), 3), basis


def assign_customer(db: Session, customer: Customer, model: Optional[dict] = None,
                    beh: Optional[dict] = None) -> None:
    model = model or _active(db, customer.tenant_id, customer.space)
    if model is None:
        return
    if beh is None:
        beh = behaviour_stats(db, customer.tenant_id, customer.space, customer.customer_ref).get(customer.customer_ref)
    key, conf, basis = _assign(model, kyc_vector(customer), beh)
    customer.segment_key = key
    customer.segment_label = model["labels"].get(key, f"S{key + 1}")
    customer.segment_confidence = conf
    customer.segment_basis = basis
    customer.segment_model = model["id"]
    customer.segment_assigned_at = utcnow()


def assign_all(db: Session, tenant_id: str, space: str) -> int:
    model = _active(db, tenant_id, space)
    if model is None:
        return 0
    beh = behaviour_stats(db, tenant_id, space)
    customers = db.execute(select(Customer).where(Customer.tenant_id == tenant_id, Customer.space == space)
                           ).scalars().all()
    for c in customers:
        assign_customer(db, c, model, beh.get(c.customer_ref))
    db.commit()
    return len(customers)


def ensure_assignment(db: Session, customer: Customer) -> None:
    model = _active(db, customer.tenant_id, customer.space)
    if model is not None and customer.segment_model != model["id"]:
        assign_customer(db, customer, model)


def clear(db: Session, tenant_id: str, space: str) -> None:
    db.execute(delete(CustomerSegment).where(CustomerSegment.tenant_id == tenant_id, CustomerSegment.space == space))
    with _cache_lock:
        _cache.pop((tenant_id, space), None)
