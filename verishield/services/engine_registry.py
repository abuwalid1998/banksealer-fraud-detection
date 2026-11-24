"""BankSealer engine behind a stable service boundary (BRD 3.3).

One :class:`Workspace` per (tenant, space) holds the active fraud model (local HBOS,
temporal and global DBSCAN/CBLOF profiles) and an optional shadow model. Model
artifacts are versioned in ``ModelVersion`` rows and stored per tenant/space, so
tenants never share training data or models, and Demo Mode has its own namespace.
"""
from __future__ import annotations

import copy
import logging
import os
import pickle
import threading
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from banksealer.config import Config
from banksealer.engine import BankSealerEngine
from banksealer.models import Reason, ScoreResult
from banksealer.models import Transaction as ETx

from ..models import Customer, Decision, ModelVersion, Tenant, Transaction, utcnow
from ..settings import settings

log = logging.getLogger("verishield.engine")


# --------------------------------------------------------------------------- conversion
def to_engine(row: Transaction) -> ETx:
    d = row.details or {}
    return ETx(tx_id=row.tx_ref, user_id=row.customer_ref, timestamp=row.ts, tx_type=row.tx_type,
               amount=float(row.amount), bank_id=row.tenant_id, channel=row.channel or "",
               currency=row.currency or "", iban=d.get("iban"), iban_cc=row.beneficiary_country, ip=row.ip,
               cc_asn=row.cc_asn, session_id=row.session_ref, card_id=d.get("card_id"),
               phone_number=d.get("phone_number"), operator=d.get("operator"), label=row.label,
               scenario=row.scenario)


def engine_tx_fields(tx: ETx) -> dict:
    """Columns of a DB Transaction derived from a prepared engine transaction."""
    return {
        "customer_ref": tx.user_id, "ts": tx.timestamp, "tx_type": tx.tx_type, "amount": tx.amount,
        "currency": tx.currency, "beneficiary": tx.beneficiary, "beneficiary_country": tx.iban_cc,
        "ip": tx.ip, "cc_asn": tx.cc_asn, "session_ref": tx.session_id, "label": tx.label,
        "scenario": tx.scenario or None,
        "details": {"iban": tx.iban, "card_id": tx.card_id, "phone_number": tx.phone_number,
                    "operator": tx.operator},
    }


# --------------------------------------------------------------------------- configuration
_BASE_CONFIG: Optional[Config] = None


def base_config() -> Config:
    global _BASE_CONFIG
    if _BASE_CONFIG is None:
        _BASE_CONFIG = Config.load(str(settings.engine_config))
        _BASE_CONFIG.data["channels"] = []
        _BASE_CONFIG.data["banks"] = {}
    return _BASE_CONFIG.copy()


def config_for(thresholds: dict, tenant_id: str, tenant_name: str = "", space: Optional[str] = None,
               params: Optional[dict] = None) -> Config:
    """Engine configuration = bundled defaults + IT Admin parameter overrides (per workspace) + policy thresholds."""
    from . import model_ops
    cfg = base_config()
    model_ops.merge_into(cfg.data, params if params is not None else model_ops.cached(tenant_id, space))
    merged = dict(cfg.data.get("thresholds", {}))
    merged.update(thresholds or {})
    cfg.data["thresholds"] = merged
    cfg.data["banks"] = {tenant_id: {"name": tenant_name or tenant_id}}
    return cfg


class _Decider(BankSealerEngine):
    """Applies thresholds to stored profile scores without the profiles (simulation/what-if)."""

    def __init__(self, config: Config):  # noqa: D401 - intentionally no profile build
        self.config = config

    def level(self, dec: Decision, row: Transaction) -> tuple[str, bool]:
        reasons = [Reason.from_dict(r) for r in (dec.reasons or []) if r.get("profile") != "rule"]
        result = ScoreResult(tx_id=dec.tx_ref, user_id=dec.customer_ref, bank_id=row.tenant_id,
                             local_score=dec.local_score, temporal_score=dec.temporal_score,
                             global_score=dec.global_score, reasons=reasons)
        self.decide(result, to_engine(row))
        hard = any(r.feature in ("max_amount", "local_alert", "temporal_alert", "global_alert")
                   for r in result.rule_reasons)
        return result.risk_level, hard


def make_decider(thresholds: dict, tenant_id: str) -> _Decider:
    return _Decider(config_for(thresholds, tenant_id))


# --------------------------------------------------------------------------- workspace
class Workspace:
    def __init__(self, tenant_id: str, space: str):
        self.tenant_id = tenant_id
        self.space = space
        self.lock = threading.RLock()
        self.engine: Optional[BankSealerEngine] = None
        self.model_id: Optional[str] = None
        self.shadow: Optional[BankSealerEngine] = None
        self.shadow_id: Optional[str] = None
        self.policy_id: Optional[str] = None
        self.params: dict = {}             # parameter overrides the active model was trained with
        self.scored_since_save = 0
        self.last_error = ""

    @property
    def ready(self) -> bool:
        return self.engine is not None and self.engine.trained

    def apply_policy(self, policy_id: str, thresholds: dict, tenant_name: str = "") -> None:
        with self.lock:
            if self.policy_id == policy_id:
                return
            cfg = config_for(thresholds, self.tenant_id, tenant_name, self.space, self.params)
            for eng in (self.engine, self.shadow):
                if eng is not None:
                    eng.apply_config(cfg)
            self.policy_id = policy_id

    def state_path(self, model_id: str) -> str:
        return str(model_dir(self.tenant_id, self.space) / f"{model_id}.state.pkl")

    def save_state(self) -> None:
        with self.lock:
            if self.engine is None or not self.engine.trained or not self.model_id:
                return
            path = self.state_path(self.model_id)
            tmp = path + ".tmp"
            self.engine.save_model(tmp)
            os.replace(tmp, path)
            self.scored_since_save = 0


_workspaces: dict[tuple[str, str], Workspace] = {}
_ws_lock = threading.Lock()


def model_dir(tenant_id: str, space: str):
    path = settings.models_dir / tenant_id / space.lower()
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspace(tenant_id: str, space: str) -> Workspace:
    key = (tenant_id, space)
    with _ws_lock:
        ws = _workspaces.get(key)
        if ws is None:
            ws = _workspaces[key] = Workspace(tenant_id, space)
        return ws


def reset_workspace(tenant_id: str, space: str) -> None:
    with _ws_lock:
        _workspaces.pop((tenant_id, space), None)


def all_workspaces() -> list[Workspace]:
    with _ws_lock:
        return list(_workspaces.values())


def _load_engine(path: str, cfg: Config) -> BankSealerEngine:
    eng = BankSealerEngine(cfg)
    eng.load_model(path)
    return eng


def load_active_models(db: Session) -> None:
    """Restore ACTIVE / SHADOW model versions into memory (startup)."""
    from .policy import active_policy

    rows = db.execute(select(ModelVersion).where(ModelVersion.kind == "FRAUD",
                                                  ModelVersion.status.in_(("ACTIVE", "SHADOW")))).scalars().all()
    for mv in rows:
        tenant = db.get(Tenant, mv.tenant_id)
        policy = active_policy(db, mv.tenant_id)
        ws = workspace(mv.tenant_id, mv.space)
        params = (mv.metrics or {}).get("parameter_overrides") or {}
        cfg = config_for(policy.document.get("engine_thresholds", {}), mv.tenant_id, tenant.display_name, mv.space,
                         params)
        try:
            state = ws.state_path(mv.id)
            path = state if (mv.status == "ACTIVE" and os.path.exists(state)) else mv.artifact_path
            eng = _load_engine(path, cfg)
        except Exception as exc:  # noqa: BLE001
            ws.last_error = f"could not load model {mv.id}: {exc}"
            log.error(ws.last_error)
            continue
        with ws.lock:
            if mv.status == "ACTIVE":
                ws.engine, ws.model_id, ws.params = eng, mv.id, params
            else:
                ws.shadow, ws.shadow_id = eng, mv.id
            ws.policy_id = policy.id


# --------------------------------------------------------------------------- training
def training_rows(db: Session, tenant_id: str, space: str, limit: int = 250_000) -> list[Transaction]:
    """History plus scored transactions that were not alerted (or were cleared by an analyst)."""
    q = (select(Transaction).outerjoin(Decision, (Decision.tenant_id == Transaction.tenant_id)
                                       & (Decision.space == Transaction.space)
                                       & (Decision.id == Transaction.decision_id))
         .where(Transaction.tenant_id == tenant_id, Transaction.space == space,
                (Transaction.mode == "HISTORY") | (Decision.decision == "PASS"),
                (Transaction.label.is_(None)) | (Transaction.label == 0))
         .order_by(Transaction.ts.desc()).limit(limit))
    rows = db.execute(q).scalars().all()
    rows.reverse()
    return rows


def train_fraud_model(db: Session, tenant_id: str, space: str, actor: str = "system", activate: bool = True,
                      shadow: bool = False, notes: str = "", params: Optional[dict] = None,
                      window_days: Optional[int] = None) -> ModelVersion:
    from . import model_ops
    from .policy import active_policy

    tenant = db.get(Tenant, tenant_id)
    policy = active_policy(db, tenant_id)
    rows = training_rows(db, tenant_id, space)
    if window_days:
        cutoff = utcnow() - timedelta(days=int(window_days))
        rows = [r for r in rows if r.ts >= cutoff]
    if len(rows) < 50:
        raise ValueError(f"not enough eligible transactions to train ({len(rows)}); import history first")
    used = params if params is not None else model_ops.overrides(tenant, space)
    cfg = config_for(policy.document.get("engine_thresholds", {}), tenant_id, tenant.display_name, space, used)
    eng = BankSealerEngine(cfg)
    txs = [to_engine(r) for r in rows]
    eng.fit(txs)

    seq = int(db.execute(select(func.max(ModelVersion.seq)).where(
        ModelVersion.tenant_id == tenant_id, ModelVersion.space == space, ModelVersion.kind == "FRAUD")).scalar()
        or 0) + 1
    model_id = f"{tenant_id}-{'DEMO-' if space == 'DEMO' else ''}FRAUD-{seq:02d}"
    path = str(model_dir(tenant_id, space) / f"{model_id}.pkl")
    eng.save_model(path)

    min_hist = int(cfg.section("local").get("min_history", 10))
    counts: dict[str, int] = {}
    for t in txs:
        counts[t.user_id] = counts.get(t.user_id, 0) + 1
    gl = eng.global_
    sizes = {}
    for label in gl.labels.values():
        sizes[label] = sizes.get(label, 0) + 1
    metrics = {
        "training_transactions": len(txs),
        "customers": len(counts),
        "undertrained_customers": sum(1 for c in counts.values() if c < min_hist),
        "peer_groups": len([k for k in sizes if k >= 0]),
        "peer_group_sizes": {str(k): v for k, v in sorted(sizes.items())},
        "global_outliers": sum(1 for s in gl.user_scores.values() if s is not None and s >= 0.5),
        "tx_types": {k: sum(1 for t in txs if t.tx_type == k) for k in {t.tx_type for t in txs}},
        "policy_version": policy.id,
        "profile_weights": cfg.section("engine").get("profile_weights"),
        "hyperparameters": {s: copy.deepcopy(cfg.section(s)) for s in ("engine", "local", "temporal", "global")},
        "parameter_overrides": used,
        "training_window_days": window_days,
        "training_eligibility": "historical import + scored transactions decided PASS, excluding confirmed fraud",
    }
    mv = ModelVersion(id=model_id, tenant_id=tenant_id, space=space, kind="FRAUD", seq=seq, status="TRAINED",
                      artifact_path=path, metrics=metrics,
                      features=["local:HBOS(amount, beneficiary, beneficiary country, connection, ip, hour)",
                                "temporal:24h window(count, total, max, beneficiaries, connections, foreign)",
                                "global:DBSCAN+CBLOF peer groups over 30-day spending vectors"],
                      training_size=len(txs), training_from=txs[0].timestamp, training_to=txs[-1].timestamp,
                      trained_by=actor, notes=notes)
    db.add(mv)
    db.commit()
    if shadow:
        set_shadow(db, model_id, actor)
    elif activate:
        activate_model(db, model_id, actor)
    return mv


def activate_model(db: Session, model_id: str, actor: str = "system") -> ModelVersion:
    from .policy import active_policy

    mv = db.get(ModelVersion, model_id)
    if mv is None or mv.kind != "FRAUD":
        raise LookupError(model_id)
    tenant = db.get(Tenant, mv.tenant_id)
    policy = active_policy(db, mv.tenant_id)
    cfg = config_for(policy.document.get("engine_thresholds", {}), mv.tenant_id, tenant.display_name, mv.space,
                     (mv.metrics or {}).get("parameter_overrides"))
    eng = _load_engine(mv.artifact_path, cfg)
    ws = workspace(mv.tenant_id, mv.space)
    with ws.lock:
        if ws.engine is not None and ws.model_id and ws.model_id != model_id:
            ws.save_state()
        db.execute(update(ModelVersion).where(ModelVersion.tenant_id == mv.tenant_id, ModelVersion.space == mv.space,
                                              ModelVersion.kind == "FRAUD", ModelVersion.status == "ACTIVE",
                                              ModelVersion.id != model_id).values(status="RETIRED"))
        if ws.shadow_id == model_id:
            ws.shadow, ws.shadow_id = None, None
        mv.status = "ACTIVE"
        mv.activated_at = utcnow()
        mv.approved_by = actor
        db.commit()
        ws.engine, ws.model_id, ws.policy_id = eng, model_id, policy.id
        ws.params = (mv.metrics or {}).get("parameter_overrides") or {}
    return mv


def set_shadow(db: Session, model_id: str, actor: str = "system") -> ModelVersion:
    from .policy import active_policy

    mv = db.get(ModelVersion, model_id)
    if mv is None or mv.kind != "FRAUD":
        raise LookupError(model_id)
    if mv.status == "ACTIVE":
        raise ValueError("the active model cannot also be the shadow model")
    tenant = db.get(Tenant, mv.tenant_id)
    policy = active_policy(db, mv.tenant_id)
    eng = _load_engine(mv.artifact_path, config_for(policy.document.get("engine_thresholds", {}), mv.tenant_id,
                                                    tenant.display_name, mv.space,
                                                    (mv.metrics or {}).get("parameter_overrides") or {}))
    ws = workspace(mv.tenant_id, mv.space)
    with ws.lock:
        db.execute(update(ModelVersion).where(ModelVersion.tenant_id == mv.tenant_id, ModelVersion.space == mv.space,
                                              ModelVersion.kind == "FRAUD", ModelVersion.status == "SHADOW")
                   .values(status="TRAINED"))
        mv.status = "SHADOW"
        db.commit()
        ws.shadow, ws.shadow_id = eng, model_id
    return mv


def clear_shadow(db: Session, tenant_id: str, space: str) -> None:
    ws = workspace(tenant_id, space)
    with ws.lock:
        db.execute(update(ModelVersion).where(ModelVersion.tenant_id == tenant_id, ModelVersion.space == space,
                                              ModelVersion.kind == "FRAUD", ModelVersion.status == "SHADOW")
                   .values(status="TRAINED"))
        db.commit()
        ws.shadow, ws.shadow_id = None, None


def customer_profile_summary(tenant_id: str, space: str, customer_ref: str) -> dict:
    ws = workspace(tenant_id, space)
    with ws.lock:
        if not ws.ready:
            return {}
        try:
            return ws.engine.customer_summary(customer_ref)
        except Exception:  # noqa: BLE001
            return {}


def status(db: Session, tenant_id: str, space: str) -> dict:
    ws = workspace(tenant_id, space)
    min_hist = int(base_config().section("local").get("min_history", 10))
    undertrained = 0
    if ws.ready:
        with ws.lock:
            users = ws.engine.local.users
            undertrained = sum(1 for prof in users.values() if sum(p.n for p in prof.values()) < min_hist)
    return {
        "ready": ws.ready,
        "active_model": ws.model_id,
        "shadow_model": ws.shadow_id,
        "policy_version": ws.policy_id,
        "state": ws.engine.state if ws.engine is not None else "no model trained",
        "last_error": ws.last_error,
        "undertrained_profiles": undertrained,
        "customers_known": db.execute(select(func.count(Customer.id)).where(
            Customer.tenant_id == tenant_id, Customer.space == space)).scalar(),
    }


def save_all() -> None:
    for ws in all_workspaces():
        try:
            ws.save_state()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not save model state for %s/%s: %s", ws.tenant_id, ws.space, exc)


__all__ = ["Workspace", "workspace", "to_engine", "engine_tx_fields", "train_fraud_model", "activate_model",
           "set_shadow", "clear_shadow", "load_active_models", "status", "make_decider", "save_all",
           "customer_profile_summary", "reset_workspace", "datetime"]
