"""Supervised model learned from analyst outcomes ("learning from the team").

The behavioural engine is unsupervised. Every confirmed outcome - a case disposition (confirmed fraud / false
positive), a customer's SMS answer, a customer fraud report - labels the transaction. When enough labels exist,
a regularised logistic regression is trained on the engine's profile scores plus context features; it adds a
``LEARNED_FRAUD_PATTERN`` signal (REVIEW / HOLD by policy) on top of the engine. Versions are stored as
``ModelVersion(kind="SUPERVISED")`` with hold-out metrics; nothing is trained automatically without data.

In the Demo workspace the synthetic ground truth of the gateway simulator is used as labels (clearly marked).
"""
from __future__ import annotations

import math
import threading
from typing import Optional

import numpy as np
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import Decision, ModelVersion, Transaction, utcnow

TYPES = ("bank_transfer", "phone_recharge", "prepaid_card", "card_payment", "cash_withdrawal", "cash_deposit",
         "incoming_transfer")
FEATURES = ["local_score", "temporal_score", "global_score", "fraud_score", "aml_score", "log_amount", "night",
            "foreign_counterparty", "foreign_connection", "undertrained"] + [f"type_{t}" for t in TYPES]
MIN_POSITIVES, MIN_NEGATIVES = 15, 50
_cache: dict[tuple, Optional[dict]] = {}
_lock = threading.Lock()


def _vector(local: float, temporal: float, global_: float, fraud: float, aml: float, amount: float, hour: int,
            cp_country: Optional[str], ip_country: Optional[str], home: str, undertrained: bool, tx_type: str) -> list:
    v = [local or 0.0, temporal or 0.0, global_ or 0.0, fraud or 0.0, aml or 0.0, math.log1p(max(amount, 0.0)),
         1.0 if hour in (0, 1, 2, 3, 4, 5) else 0.0,
         1.0 if cp_country and cp_country != home else 0.0, 1.0 if ip_country and ip_country != home else 0.0,
         1.0 if undertrained else 0.0]
    return v + [1.0 if tx_type == t else 0.0 for t in TYPES]


def _training_set(db: Session, tenant_id: str, space: str, home: str):
    q = select(Decision, Transaction).join(Transaction, (Transaction.tenant_id == Decision.tenant_id)
                                           & (Transaction.space == Decision.space)
                                           & (Transaction.tx_ref == Decision.tx_ref)).where(
        Decision.tenant_id == tenant_id, Decision.space == space, Decision.decision != "ERROR_POLICY")
    rows = db.execute(q.order_by(Decision.issued_at.desc()).limit(100_000)).all()
    x, y = [], []
    for dec, tx in rows:
        label = tx.label
        if label is None and space == "DEMO":
            label = 1 if tx.scenario else 0          # synthetic ground truth of the simulator
        if label is None:
            continue
        ipc = (tx.cc_asn or "").split("-", 1)[0] if tx.cc_asn and "-" in tx.cc_asn else None
        x.append(_vector(dec.local_score, dec.temporal_score, dec.global_score, dec.fraud_score, dec.aml_score,
                         tx.amount, tx.ts.hour, tx.beneficiary_country, ipc, home, dec.undertrained, tx.tx_type))
        y.append(int(label))
    return np.asarray(x, dtype=float), np.asarray(y, dtype=float)


def _fit(x: np.ndarray, y: np.ndarray, l2: float = 0.5, iters: int = 600, lr: float = 0.3):
    mu, sd = x.mean(0), x.std(0)
    sd[sd < 1e-9] = 1.0
    z = (x - mu) / sd
    w, b = np.zeros(z.shape[1]), 0.0
    pos = y.mean()
    cw = np.where(y == 1, 0.5 / max(pos, 1e-6), 0.5 / max(1 - pos, 1e-6))   # balanced class weights
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(z @ w + b)))
        g = (p - y) * cw
        w -= lr * (z.T @ g / len(y) + l2 * w / len(y))
        b -= lr * g.mean()
    return w, b, mu, sd


def _auc(scores: np.ndarray, y: np.ndarray) -> Optional[float]:
    pos, neg = scores[y == 1], scores[y == 0]
    if not len(pos) or not len(neg):
        return None
    order = np.argsort(np.concatenate([pos, neg]))
    ranks = np.empty(len(order))
    ranks[order] = np.arange(1, len(order) + 1)
    return round(float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))), 3)


def train(db: Session, tenant_id: str, space: str, actor: str, home: str = "PS", activate: bool = True) -> ModelVersion:
    x, y = _training_set(db, tenant_id, space, home)
    n_pos, n_neg = int(y.sum()) if len(y) else 0, int(len(y) - (y.sum() if len(y) else 0))
    if n_pos < MIN_POSITIVES or n_neg < MIN_NEGATIVES:
        raise ValueError(f"not enough confirmed outcomes yet ({n_pos} fraud / {n_neg} legitimate; need "
                         f"{MIN_POSITIVES} / {MIN_NEGATIVES}). Labels come from case dispositions, customer SMS "
                         "answers and customer fraud reports.")
    rng = np.random.default_rng(7)
    idx = rng.permutation(len(y))
    cut = int(len(y) * 0.75)
    tr, te = idx[:cut], idx[cut:]
    w, b, mu, sd = _fit(x[tr], y[tr])
    p_te = 1 / (1 + np.exp(-(((x[te] - mu) / sd) @ w + b)))
    pred = p_te >= 0.7
    tp = int(((pred == 1) & (y[te] == 1)).sum())
    fp = int(((pred == 1) & (y[te] == 0)).sum())
    fn = int(((pred == 0) & (y[te] == 1)).sum())
    w, b, mu, sd = _fit(x, y)                      # final model on all labelled data
    seq = int(db.execute(select(func.max(ModelVersion.seq)).where(
        ModelVersion.tenant_id == tenant_id, ModelVersion.space == space, ModelVersion.kind == "SUPERVISED"))
        .scalar() or 0) + 1
    mid = f"{tenant_id}-{'DEMO-' if space == 'DEMO' else ''}LEARN-{seq:02d}"
    weights = sorted(zip(FEATURES, (w / 1).tolist()), key=lambda kv: -abs(kv[1]))
    mv = ModelVersion(id=mid, tenant_id=tenant_id, space=space, kind="SUPERVISED", seq=seq, status="TRAINED",
                      metrics={"positives": n_pos, "negatives": n_neg, "holdout_auc": _auc(p_te, y[te]),
                               "holdout_precision_at_0_7": round(tp / (tp + fp), 3) if tp + fp else None,
                               "holdout_recall_at_0_7": round(tp / (tp + fn), 3) if tp + fn else None,
                               "top_weights": [[k, round(v, 3)] for k, v in weights[:8]],
                               "label_source": "simulator ground truth (Demo)" if space == "DEMO" else
                               "case dispositions, customer confirmations and fraud reports",
                               "artifact": {"w": w.tolist(), "b": float(b), "mu": mu.tolist(), "sd": sd.tolist(),
                                            "home": home}},
                      features=FEATURES, training_size=int(len(y)), trained_by=actor,
                      notes="Logistic regression on engine profile scores + context, class-balanced, L2")
    db.add(mv)
    db.commit()
    if activate:
        activate_model(db, mid)
    return mv


def activate_model(db: Session, model_id: str) -> None:
    mv = db.get(ModelVersion, model_id)
    db.execute(update(ModelVersion).where(ModelVersion.tenant_id == mv.tenant_id, ModelVersion.space == mv.space,
                                          ModelVersion.kind == "SUPERVISED", ModelVersion.status == "ACTIVE")
               .values(status="RETIRED"))
    mv.status, mv.activated_at = "ACTIVE", utcnow()
    db.commit()
    with _lock:
        _cache.pop((mv.tenant_id, mv.space), None)


def deactivate(db: Session, tenant_id: str, space: str) -> None:
    db.execute(update(ModelVersion).where(ModelVersion.tenant_id == tenant_id, ModelVersion.space == space,
                                          ModelVersion.kind == "SUPERVISED", ModelVersion.status == "ACTIVE")
               .values(status="RETIRED"))
    db.commit()
    with _lock:
        _cache.pop((tenant_id, space), None)


def _active(db: Session, tenant_id: str, space: str) -> Optional[dict]:
    key = (tenant_id, space)
    with _lock:
        if key in _cache:
            return _cache[key]
    mv = db.execute(select(ModelVersion).where(ModelVersion.tenant_id == tenant_id, ModelVersion.space == space,
                                               ModelVersion.kind == "SUPERVISED", ModelVersion.status == "ACTIVE")
                    ).scalar()
    model = None
    if mv is not None:
        a = mv.metrics["artifact"]
        model = {"id": mv.id, "w": np.asarray(a["w"]), "b": a["b"], "mu": np.asarray(a["mu"]),
                 "sd": np.asarray(a["sd"]), "home": a.get("home", "PS")}
    with _lock:
        _cache[key] = model
    return model


def predict(db: Session, tenant_id: str, space: str, etx, result, aml_score: float = 0.0) -> Optional[dict]:
    m = _active(db, tenant_id, space)
    if m is None:
        return None
    v = np.asarray(_vector(result.local_score, result.temporal_score, result.global_score, result.combined_score,
                           aml_score, etx.amount, etx.timestamp.hour, etx.counterparty_country, etx.ip_country, m["home"],
                           result.undertrained, etx.tx_type))
    p = 1 / (1 + math.exp(-float(((v - m["mu"]) / m["sd"]) @ m["w"] + m["b"])))
    return {"model": m["id"], "probability": round(p, 4)}
