"""Model operations: tunable parameters, health measurements, retraining recommendation and cluster views.

* **Parameters** - the IT Admin can tune the fraud engine (profile weights, HBOS, temporal window, DBSCAN /
  CBLOF) and the customer segmentation (k range, behaviour weight) per workspace. Overrides are stored in the
  tenant settings, applied to the next training run and recorded in the model version.
* **Health** - measurements computed from live data: model age, new data since training, new customers,
  under-trained profiles, score drift (PSI), intervention-rate drift, analyst feedback, amount drift and
  segmentation quality. They produce a recommendation: OK / CONSIDER / RETRAIN RECOMMENDED.
* **Clusters** - 2-D projections (PCA) of the customer segments and of the fraud engine's peer groups
  (iterative DBSCAN + CBLOF), with each cluster's profile, so the AML Manager can see how customers are grouped.
"""
from __future__ import annotations

import copy
import math
import statistics
import threading
from collections import Counter
from datetime import timedelta
from typing import Optional

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Alert, Customer, CustomerSegment, Decision, ModelVersion, Tenant, Transaction, utcnow

# (section, key, type, min, max, label, help)
FRAUD_PARAMS = [
    ("engine", "profile_weights.local", float, 0.0, 1.0, "Weight - local profile",
     "Share of the combined score from the customer's own history (HBOS)."),
    ("engine", "profile_weights.temporal", float, 0.0, 1.0, "Weight - temporal profile",
     "Share from the 24-hour behaviour window (counts, totals, beneficiaries)."),
    ("engine", "profile_weights.global", float, 0.0, 1.0, "Weight - global profile",
     "Share from the comparison with peer customers (DBSCAN + CBLOF)."),
    ("engine", "update_profiles_online", bool, None, None, "Online learning",
     "Update customer profiles with every scored (non-suspicious) transaction."),
    ("engine", "retrain_global_every", int, 100, 100000, "Refit peer groups every N transactions",
     "Online refit frequency of the global profile."),
    ("local", "min_history", int, 3, 100, "Minimum history per customer",
     "Fewer past transactions of a type = under-trained profile (falls back to peers)."),
    ("local", "shrinkage_k", float, 1.0, 100.0, "Shrinkage towards peers (k)",
     "Personal histogram weight = n / (n + k)."),
    ("local", "amount_bins", int, 8, 64, "Amount histogram bins", "Log-spaced bins for amounts."),
    ("local", "score_scale", float, 1.0, 30.0, "Local score scale", "Normalised score = 1 - exp(-raw / scale)."),
    ("temporal", "window_hours", int, 1, 168, "Behaviour window (hours)", "Sliding aggregation window."),
    ("temporal", "sigma_multiplier", float, 1.0, 6.0, "Personal threshold (sigma)",
     "Personal threshold = mean + k x standard deviation."),
    ("temporal", "score_scale", float, 1.0, 20.0, "Temporal score scale", ""),
    ("global", "eps", float, 0.3, 5.0, "DBSCAN radius (eps)", "Radius in Mahalanobis units."),
    ("global", "min_samples", int, 2, 50, "DBSCAN min samples", "Customers needed to form a dense group."),
    ("global", "eps_growth", float, 1.0, 3.0, "Radius growth per iteration", "Iterative DBSCAN for leftover noise."),
    ("global", "max_iterations", int, 1, 10, "DBSCAN iterations", ""),
    ("global", "cblof_alpha", float, 0.5, 0.99, "CBLOF alpha", "Large clusters hold at least alpha of customers."),
    ("global", "cblof_beta", float, 1.5, 20.0, "CBLOF beta", "...or are beta times bigger than the next one."),
    ("global", "score_quantile", float, 0.8, 0.999, "Outlier score quantile", "Quantile mapped to score ~0.63."),
]
SEGMENT_PARAMS = [
    ("segmentation", "k_min", int, 2, 12, "Minimum clusters", "Smallest number of segments tried."),
    ("segmentation", "k_max", int, 3, 16, "Maximum clusters", "Largest number of segments tried (best silhouette wins)."),
    ("segmentation", "behaviour_weight", float, 0.2, 3.0, "Behaviour weight",
     "Weight of observed behaviour versus KYC features."),
]
SEGMENT_DEFAULTS = {"k_min": 3, "k_max": 8, "behaviour_weight": 1.2}

_params: dict[tuple[str, str], dict] = {}
_lock = threading.Lock()


# --------------------------------------------------------------------------- parameters
def overrides(tenant: Tenant, space: str) -> dict:
    return copy.deepcopy(((tenant.settings or {}).get("model_params") or {}).get(space) or {})


def cache_overrides(tenant: Tenant) -> None:
    for space in ("LIVE", "DEMO"):
        with _lock:
            _params[(tenant.id, space)] = overrides(tenant, space)


def cached(tenant_id: str, space: Optional[str]) -> dict:
    if not space:
        return {}
    with _lock:
        return copy.deepcopy(_params.get((tenant_id, space)) or {})


def _get(d: dict, dotted: str):
    for part in dotted.split("."):
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    return d


def _set(d: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        d = d.setdefault(part, {})
    d[parts[-1]] = value


def merge_into(config_data: dict, ov: dict) -> None:
    for section in ("engine", "local", "temporal", "global"):
        for key, value in (ov.get(section) or {}).items():
            if isinstance(value, dict):
                config_data.setdefault(section, {}).setdefault(key, {}).update(value)
            else:
                config_data.setdefault(section, {})[key] = value


def describe(tenant: Tenant, space: str) -> dict:
    from .engine_registry import base_config
    base = base_config().data
    ov = overrides(tenant, space)
    out = []
    for section, key, typ, lo, hi, label, help_ in FRAUD_PARAMS + SEGMENT_PARAMS:
        default = SEGMENT_DEFAULTS.get(key) if section == "segmentation" else _get(base.get(section, {}), key)
        cur = _get(ov.get(section, {}), key)
        out.append({"id": f"{section}.{key}", "section": section, "key": key, "label": label, "help": help_,
                    "type": typ.__name__, "min": lo, "max": hi, "default": default,
                    "value": default if cur is None else cur, "overridden": cur is not None})
    return {"parameters": out, "overrides": ov}


def validate(values: dict) -> dict:
    """``{"global.eps": 1.4, ...}`` -> nested overrides; raises ValueError on bad input."""
    spec = {f"{s}.{k}": (s, k, t, lo, hi) for s, k, t, lo, hi, *_ in FRAUD_PARAMS + SEGMENT_PARAMS}
    out: dict = {}
    for pid, raw in values.items():
        if pid not in spec:
            raise ValueError(f"unknown parameter {pid}")
        section, key, typ, lo, hi = spec[pid]
        if raw is None or raw == "":
            continue
        if typ is bool:
            value = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes", "on")
        else:
            value = typ(raw)
            if lo is not None and not lo <= value <= hi:
                raise ValueError(f"{pid} must be between {lo} and {hi}")
        _set(out.setdefault(section, {}), key, value)
    w = (out.get("engine") or {}).get("profile_weights")
    if w:
        from .engine_registry import base_config
        full = dict(base_config().section("engine").get("profile_weights") or {})
        full.update(w)
        if abs(sum(full.values()) - 1.0) > 0.011:
            raise ValueError(f"profile weights must add up to 1.0 (now {sum(full.values()):.2f})")
        out["engine"]["profile_weights"] = full
    seg = out.get("segmentation") or {}
    if seg.get("k_min") and seg.get("k_max") and seg["k_min"] > seg["k_max"]:
        raise ValueError("minimum clusters cannot exceed maximum clusters")
    return out


def save(db: Session, tenant: Tenant, space: str, new: dict) -> dict:
    s = copy.deepcopy(tenant.settings or {})
    s.setdefault("model_params", {})[space] = new
    tenant.settings = s
    db.commit()
    cache_overrides(tenant)
    return new


# --------------------------------------------------------------------------- health
def _psi(a: list[float], b: list[float], bins: int = 10) -> Optional[float]:
    if len(a) < 50 or len(b) < 50:
        return None
    edges = np.linspace(0, 1, bins + 1)
    pa, _ = np.histogram(np.clip(a, 0, 1), edges)
    pb, _ = np.histogram(np.clip(b, 0, 1), edges)
    pa = (pa + 0.5) / (pa.sum() + 0.5 * bins)
    pb = (pb + 0.5) / (pb.sum() + 0.5 * bins)
    return round(float(((pb - pa) * np.log(pb / pa)).sum()), 4)


def health(db: Session, tenant_id: str, space: str) -> dict:
    from . import engine_registry
    now = utcnow()
    ws = engine_registry.workspace(tenant_id, space)
    mv = db.execute(select(ModelVersion).where(ModelVersion.tenant_id == tenant_id, ModelVersion.space == space,
                                               ModelVersion.kind == "FRAUD", ModelVersion.status == "ACTIVE")).scalar()
    seg = db.execute(select(ModelVersion).where(ModelVersion.tenant_id == tenant_id, ModelVersion.space == space,
                                                ModelVersion.kind == "SEGMENTATION",
                                                ModelVersion.status == "ACTIVE")).scalar()
    checks: list[dict] = []

    def check(cid, label, value, status, detail, threshold=""):
        checks.append({"id": cid, "label": label, "value": value, "status": status, "detail": detail,
                       "threshold": threshold})

    if mv is None:
        return {"status": "RETRAIN_RECOMMENDED", "model": None, "segmentation": seg.id if seg else None,
                "checks": [{"id": "model", "label": "Active fraud model", "value": "none", "status": "CRITICAL",
                            "detail": "No active fraud model - import history and train one.", "threshold": ""}],
                "reasons": ["No active fraud model"], "measured_at": now.isoformat(timespec="seconds") + "Z"}

    age = (now - (mv.activated_at or mv.trained_at)).days
    check("age", "Model age", f"{age} days", "WARN" if age > 30 else "OK",
          f"Trained {mv.trained_at:%d %b %Y}, active since {(mv.activated_at or mv.trained_at):%d %b %Y}.",
          "retrain every 30 days")

    since = mv.training_to or mv.trained_at
    new_tx = db.execute(select(func.count(Transaction.id)).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.ts > since)).scalar() or 0
    ratio = new_tx / max(mv.training_size, 1)
    check("new_data", "New transactions since training", f"{new_tx:,} ({ratio:.0%} of training set)",
          "WARN" if ratio >= 0.5 else "OK",
          "Profiles learn online, but peer groups and value histograms are only rebuilt by retraining.", ">= 50%")

    trained_customers = int((mv.metrics or {}).get("customers") or 0)
    active_customers = db.execute(select(func.count(func.distinct(Transaction.customer_ref))).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.ts > since)).scalar() or 0
    known = set()
    if ws.ready:
        with ws.lock:
            known = set(ws.engine.global_.labels)
    new_customers = 0
    if active_customers:
        refs = db.execute(select(func.distinct(Transaction.customer_ref)).where(
            Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.ts > since)).scalars().all()
        new_customers = sum(1 for r in refs if r not in known)
    share = new_customers / max(trained_customers, 1)
    check("new_customers", "Customers unseen at training", f"{new_customers:,} ({share:.0%})",
          "WARN" if share >= 0.10 else "OK", "New customers are compared with the nearest peer group until the next "
                                              "retraining assigns them properly.", ">= 10%")

    st = engine_registry.status(db, tenant_id, space)
    under = st.get("undertrained_profiles", 0) / max(len(known) or trained_customers, 1)
    check("undertrained", "Under-trained customer profiles", f"{st.get('undertrained_profiles', 0):,} ({under:.0%})",
          "WARN" if under >= 0.30 else "OK", "Profiles with fewer transactions than the minimum history.", ">= 30%")

    base_q = select(Decision.fraud_score, Decision.decision).where(
        Decision.tenant_id == tenant_id, Decision.space == space, Decision.fraud_model_version == mv.id)
    first = db.execute(base_q.order_by(Decision.issued_at).limit(600)).all()
    last = db.execute(base_q.order_by(Decision.issued_at.desc()).limit(600)).all()
    psi = _psi([r[0] for r in first], [r[0] for r in last])
    if psi is None:
        check("psi", "Score drift (PSI)", "not enough decisions yet", "INFO",
              "Needs at least 50 decisions scored by this model; start a channel to collect them.", "> 0.25")
    else:
        check("psi", "Score drift (PSI)", f"{psi:.3f}", "CRITICAL" if psi > 0.25 else "WARN" if psi > 0.10 else "OK",
              "Population Stability Index between the first and the latest decisions of this model.",
              "0.10 moderate / 0.25 significant")

    def irate(rows):
        return sum(1 for _, d in rows if d in ("REVIEW", "HOLD", "DECLINE")) / len(rows) if rows else None
    r0, r1 = irate(first), irate(last)
    if r0 is not None and r1 is not None and len(first) >= 100 and len(last) >= 100:
        change = (r1 + 0.005) / (r0 + 0.005)
        check("intervention", "Intervention rate drift", f"{r0:.1%} -> {r1:.1%}",
              "WARN" if change >= 2 or change <= 0.5 else "OK",
              "Share of REVIEW / HOLD / DECLINE, early versus recent decisions.", "x2 change")
    else:
        check("intervention", "Intervention rate drift", "not enough decisions yet", "INFO", "", "x2 change")

    closed = db.execute(select(Alert.closed_reason).where(
        Alert.tenant_id == tenant_id, Alert.space == space, Alert.status == "CLOSED",
        Alert.model_version == mv.id)).scalars().all()
    if len(closed) >= 10:
        fp = sum(1 for r in closed if any(k in (r or "").lower() for k in ("false positive", "no concern"))) / len(closed)
        check("feedback", "Analyst feedback (false positives)", f"{fp:.0%} of {len(closed)} closed alerts",
              "WARN" if fp >= 0.7 else "OK", "Closed alerts dispositioned as false positive / no concern.", ">= 70%")
    else:
        check("feedback", "Analyst feedback (false positives)", f"{len(closed)} closed alerts", "INFO",
              "Needs at least 10 closed alerts of this model.", ">= 70%")

    hist = db.execute(select(Transaction.amount).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.ts <= since,
        Transaction.tx_type == "bank_transfer").order_by(Transaction.ts.desc()).limit(3000)).scalars().all()
    recent = db.execute(select(Transaction.amount).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.ts > since,
        Transaction.tx_type == "bank_transfer").order_by(Transaction.ts.desc()).limit(3000)).scalars().all()
    if len(hist) >= 50 and len(recent) >= 50:
        m0, m1 = statistics.median(hist), statistics.median(recent)
        shift = abs(math.log((m1 + 1) / (m0 + 1)))
        check("amounts", "Transfer amount drift (median)", f"{m0:,.0f} -> {m1:,.0f}",
              "WARN" if shift >= math.log(1.5) else "OK", "Median transfer amount at training versus now.", "x1.5")
    else:
        check("amounts", "Transfer amount drift (median)", "not enough recent transfers", "INFO", "", "x1.5")

    if seg is not None:
        sil = (seg.metrics or {}).get("silhouette")
        seg_age = (now - (seg.activated_at or seg.trained_at)).days
        n_cust = db.execute(select(func.count(Customer.id)).where(Customer.tenant_id == tenant_id,
                                                                  Customer.space == space)).scalar() or 0
        grown = (n_cust - seg.training_size) / max(seg.training_size, 1)
        status = "WARN" if (sil is not None and sil < 0.15) or grown >= 0.15 or seg_age > 90 else "OK"
        check("segmentation", "Customer segmentation", f"silhouette {sil} · {seg_age} days · {grown:+.0%} customers",
              status, f"{seg.id}: k = {(seg.metrics or {}).get('k')}; rebuild when customers grow 15% or quality drops.",
              "silhouette < 0.15 / +15% customers / 90 days")
    warn = [c for c in checks if c["status"] in ("WARN", "CRITICAL")]
    crit = [c for c in checks if c["status"] == "CRITICAL"]
    status = "RETRAIN_RECOMMENDED" if crit or len(warn) >= 2 else "CONSIDER" if warn else "OK"
    return {"status": status, "model": mv.id, "segmentation": seg.id if seg else None, "checks": checks,
            "reasons": [f"{c['label']}: {c['value']}" for c in warn],
            "measured_at": now.isoformat(timespec="seconds") + "Z"}


def check_all(db: Session) -> None:
    """Background job: raise an operations incident when a workspace's model needs retraining."""
    from .decision import open_incident
    rows = db.execute(select(ModelVersion.tenant_id, ModelVersion.space).where(
        ModelVersion.kind == "FRAUD", ModelVersion.status == "ACTIVE")).all()
    for tenant_id, space in rows:
        h = health(db, tenant_id, space)
        if h["status"] == "RETRAIN_RECOMMENDED":
            open_incident(db, tenant_id, f"model:retrain:{space}",
                          f"Retraining recommended for {h['model']} ({space.title()} workspace)", "SEV4",
                          affected="; ".join(h["reasons"])[:200])


# --------------------------------------------------------------------------- clusters
def _pca2(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, list[float]]:
    xc = x - x.mean(0)
    _, s, vt = np.linalg.svd(xc, full_matrices=False)
    comps = vt[:2]
    var = (s ** 2) / max((s ** 2).sum(), 1e-12)
    return xc @ comps.T, comps, [round(float(v), 3) for v in var[:2]]


def segment_clusters(db: Session, tenant_id: str, space: str, show_names: bool) -> Optional[dict]:
    from . import segmentation as seg_service
    mv = db.execute(select(ModelVersion).where(ModelVersion.tenant_id == tenant_id, ModelVersion.space == space,
                                               ModelVersion.kind == "SEGMENTATION",
                                               ModelVersion.status == "ACTIVE")).scalar()
    if mv is None:
        return None
    art = mv.metrics["artifact"]
    mu, sd, w = np.asarray(art["mu"]), np.asarray(art["sd"]), np.asarray(art["weights"])
    customers = db.execute(select(Customer).where(Customer.tenant_id == tenant_id, Customer.space == space)
                           .limit(3000)).scalars().all()
    beh = seg_service.behaviour_stats(db, tenant_id, space)
    beh_mean = {f: float(np.mean([b[f] for b in beh.values()])) if beh else 0.0 for f in seg_service.BEHAVIOUR_FEATURES}
    rows = []
    for c in customers:
        b = beh.get(c.customer_ref)
        rows.append(seg_service.kyc_vector(c) + [(b[f] if b else beh_mean[f]) for f in seg_service.BEHAVIOUR_FEATURES])
    if len(rows) < 3:
        return None
    x = ((np.asarray(rows, dtype=float) - mu) / sd) * w
    pts, comps, var = _pca2(x)
    centers = (np.asarray(art["centers"]) - x.mean(0)) @ comps.T
    alerts = dict(db.execute(select(Alert.customer_ref, func.count(Alert.id)).where(
        Alert.tenant_id == tenant_id, Alert.space == space).group_by(Alert.customer_ref)).all())
    segs = db.execute(select(CustomerSegment).where(CustomerSegment.tenant_id == tenant_id,
                                                    CustomerSegment.space == space,
                                                    CustomerSegment.model_version == mv.id)
                      .order_by(CustomerSegment.segment_key)).scalars().all()
    features = art["features"]
    loadings = sorted(((abs(float(comps[0][i])) + abs(float(comps[1][i])), seg_service.FEATURE_LABELS.get(f, f))
                       for i, f in enumerate(features)), reverse=True)[:4]
    risk_by_seg: dict[int, Counter] = {}
    for c in customers:
        risk_by_seg.setdefault(c.segment_key if c.segment_key is not None else -1, Counter())[c.risk_category] += 1
    return {
        "model": mv.id, "algorithm": "k-means over standardised KYC + behaviour features (k chosen by silhouette)",
        "metrics": {k: v for k, v in (mv.metrics or {}).items() if k not in ("artifact", "segment_sizes")},
        "explained_variance": var, "axis_drivers": [lbl for _, lbl in loadings],
        "points": [{"ref": c.customer_ref, "name": c.display_name if show_names else "",
                    "x": round(float(pts[i][0]), 3), "y": round(float(pts[i][1]), 3),
                    "segment": c.segment_key, "risk": c.risk_category, "type": c.customer_type,
                    "alerts": int(alerts.get(c.customer_ref, 0)),
                    "confidence": c.segment_confidence} for i, c in enumerate(customers)],
        "centers": [{"segment": j, "x": round(float(centers[j][0]), 3), "y": round(float(centers[j][1]), 3)}
                    for j in range(len(centers))],
        "segments": [{"key": s.segment_key, "label": s.label, "size": s.size, "description": s.description,
                      "risk_mix": dict(risk_by_seg.get(s.segment_key, Counter())),
                      "alerts": sum(int(alerts.get(c.customer_ref, 0)) for c in customers
                                    if c.segment_key == s.segment_key),
                      "profile": s.profile} for s in segs],
    }


def peer_groups(tenant_id: str, space: str, names: dict, show_names: bool) -> Optional[dict]:
    from banksealer.engine.global_profile import FEATURES, LABELS
    from . import engine_registry
    ws = engine_registry.workspace(tenant_id, space)
    if not ws.ready:
        return None
    with ws.lock:
        gl = ws.engine.global_
        if not gl.fitted:
            return None
        users = list(gl.labels)[:3000]
        period = gl._period(gl.end)
        v = np.vstack([gl.user_stats[u].vector(period) for u in users])
        x = gl._whiten(v)
        labels = [gl.labels[u] for u in users]
        scores = [float(gl.user_scores.get(u) or 0.0) for u in users]
        sizes, large = dict(gl.sizes), set(gl.large)
        raw_centroids = {c: vec.copy() for c, vec in gl.raw_centroids.items()}
        cfg = dict(gl.cfg)
    pts, comps, var = _pca2(x)
    groups = []
    for c, size in sorted(sizes.items(), key=lambda kv: -kv[1]):
        members = [i for i, lab in enumerate(labels) if lab == c]
        prof = raw_centroids.get(c)
        groups.append({"cluster": int(c), "size": int(size), "large": c in large,
                       "outliers": sum(1 for i in members if scores[i] >= 0.5),
                       "profile": {LABELS[f][0]: round(float(prof[j]), 2) for j, f in enumerate(FEATURES)}
                       if prof is not None else {}})
    noise = sum(1 for lab in labels if lab == -1)
    return {"algorithm": "Iterative DBSCAN over whitened 30-day spending vectors; outlier score = CBLOF",
            "parameters": {k: cfg.get(k) for k in ("eps", "min_samples", "eps_growth", "max_iterations",
                                                   "cblof_alpha", "cblof_beta")},
            "model": ws.model_id, "explained_variance": var, "noise_customers": noise,
            "points": [{"ref": u, "name": names.get(u, "") if show_names else "", "x": round(float(pts[i][0]), 3),
                        "y": round(float(pts[i][1]), 3), "cluster": int(labels[i]),
                        "score": round(scores[i], 3)} for i, u in enumerate(users)],
            "groups": groups}


def clusters(db: Session, tenant_id: str, space: str, show_names: bool = True) -> dict:
    names = dict(db.execute(select(Customer.customer_ref, Customer.display_name).where(
        Customer.tenant_id == tenant_id, Customer.space == space)).all())
    return {"segmentation": segment_clusters(db, tenant_id, space, show_names),
            "peer_groups": peer_groups(tenant_id, space, names, show_names),
            "generated_at": utcnow().isoformat(timespec="seconds") + "Z", "window": str(timedelta(days=30))}
