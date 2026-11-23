"""Decision policies (BRD section 8): versioned thresholds, rules, actions, fallback.

A policy document is JSON. It contains:

* ``engine_thresholds`` - BankSealer thresholds (suspicious/high/critical, per-profile alert
  levels, hard amount limits, high-risk countries, night hours) applied to the fraud scores.
* ``actions`` - risk level -> outcome (``PASS`` / ``REVIEW`` / ``HOLD`` / ``DECLINE``).
* ``aml`` - AML unusual-activity indicator parameters and the outcomes they trigger.
* ``rules`` - scoped business rules (product, channel, segment, customer risk, amount,
  country, velocity, new beneficiary, hours) with an action and a reason code.
* ``undertrained`` - conservative treatment of new customers with limited history.
* ``fallback`` - institution-approved behaviour when no reliable decision can be produced.
* ``hold_expiry_minutes`` / ``decision_ttl_minutes``.

Published versions are immutable; a change creates a new version. Historical decisions
keep the policy version they were made under.
"""
from __future__ import annotations

import copy
from collections import Counter as Count
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Decision, PolicyVersion, Transaction, utcnow

OUTCOMES = ("PASS", "REVIEW", "HOLD", "DECLINE")
SEVERITY = {o: i for i, o in enumerate(OUTCOMES)}
FALLBACKS = ("FAIL_OPEN", "FAIL_CLOSED", "FALLBACK_HOLD")
RULE_CONDITION_KEYS = ("tx_types", "channels", "segments", "customer_types", "customer_risk", "countries",
                       "min_amount", "max_amount", "new_beneficiary", "velocity_count", "velocity_hours",
                       "hours", "modes")

DEFAULT_POLICY: dict[str, Any] = {
    "engine_thresholds": {
        "suspicious": 0.50, "high": 0.65, "critical": 0.80,
        "local_alert": 0.92, "temporal_alert": 0.92, "global_alert": 0.97,
        "max_amount": {"bank_transfer": 15000, "phone_recharge": 150, "prepaid_card": 2500},
        "hard_rule_score": 0.85,
        "high_risk_countries": ["KY", "PA", "VG", "BZ", "SC", "RU", "NG", "IR", "KP"],
        "high_risk_country_boost": 0.10,
        "night_hours": [0, 1, 2, 3, 4],
        "night_boost": 0.03,
    },
    "actions": {"LOW": "PASS", "MEDIUM": "REVIEW", "HIGH": "HOLD", "CRITICAL": "DECLINE"},
    "aml": {
        "enabled": True,
        "window_hours": 24,
        "reporting_threshold": 10000,
        "structuring_band": 0.15,
        "structuring_count": 3,
        "velocity_count": 8,
        "turnover_multiplier": 2.5,
        "shared_counterparty_customers": 3,
        "shared_counterparty_days": 7,
        "high_risk_customer_amount": 5000,
        "elevated_action": "REVIEW",
        "high_action": "HOLD",
    },
    "rules": [
        {"id": "R-HRC-HOLD", "name": "Large transfer to high-risk jurisdiction", "enabled": True,
         "conditions": {"tx_types": ["bank_transfer"], "countries": ["KY", "PA", "VG", "IR", "KP"],
                        "min_amount": 3000},
         "action": "HOLD", "reason_code": "HIGH_RISK_JURISDICTION_LARGE"},
        {"id": "R-NEWBEN-BIG", "name": "New beneficiary with large amount", "enabled": True,
         "conditions": {"tx_types": ["bank_transfer"], "new_beneficiary": True, "min_amount": 10000},
         "action": "REVIEW", "reason_code": "NEW_BENEFICIARY_LARGE_AMOUNT"},
        {"id": "R-RECHARGE-BURST", "name": "Burst of phone recharges", "enabled": True,
         "conditions": {"tx_types": ["phone_recharge"], "velocity_count": 5, "velocity_hours": 1},
         "action": "HOLD", "reason_code": "RECHARGE_VELOCITY"},
    ],
    "undertrained": {"cap_action": "REVIEW", "note": "Limited history alone never triggers HOLD/DECLINE"},
    "fallback": {"default": "FAIL_OPEN", "bank_transfer": "FALLBACK_HOLD", "phone_recharge": "FAIL_OPEN",
                 "prepaid_card": "FAIL_OPEN"},
    "hold_expiry_minutes": 240,
    "decision_ttl_minutes": 15,
}


def merge_document(doc: Optional[dict]) -> dict:
    out = copy.deepcopy(DEFAULT_POLICY)
    for key, value in (doc or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            merged = copy.deepcopy(out[key])
            merged.update(copy.deepcopy(value))
            out[key] = merged
        else:
            out[key] = copy.deepcopy(value)
    return out


def validate_document(doc: dict) -> list[str]:
    errors = []
    th = doc.get("engine_thresholds", {})
    try:
        s, h, c = float(th["suspicious"]), float(th["high"]), float(th["critical"])
        if not (0 < s <= h <= c <= 1):
            errors.append("engine thresholds must satisfy 0 < suspicious <= high <= critical <= 1")
    except (KeyError, TypeError, ValueError):
        errors.append("engine thresholds suspicious/high/critical must be numbers")
    for key in ("local_alert", "temporal_alert", "global_alert", "hard_rule_score", "high_risk_country_boost",
                "night_boost"):
        if key in th and th[key] is not None:
            try:
                v = float(th[key])
                if not 0 <= v <= 1:
                    errors.append(f"{key} must be between 0 and 1")
            except (TypeError, ValueError):
                errors.append(f"{key} must be a number")
    for tx_type, limit in (th.get("max_amount") or {}).items():
        try:
            if float(limit) <= 0:
                errors.append(f"max_amount for {tx_type} must be positive")
        except (TypeError, ValueError):
            errors.append(f"max_amount for {tx_type} must be a number")
    for level, action in (doc.get("actions") or {}).items():
        if action not in OUTCOMES:
            errors.append(f"action for {level} must be one of {', '.join(OUTCOMES)}")
    aml = doc.get("aml", {})
    for key in ("elevated_action", "high_action"):
        if aml.get(key) and aml[key] not in OUTCOMES:
            errors.append(f"aml.{key} must be one of {', '.join(OUTCOMES)}")
    ids = set()
    for rule in doc.get("rules", []):
        rid = rule.get("id") or ""
        if not rid:
            errors.append("every rule needs an id")
        if rid in ids:
            errors.append(f"duplicate rule id {rid}")
        ids.add(rid)
        if rule.get("action") not in OUTCOMES:
            errors.append(f"rule {rid}: action must be one of {', '.join(OUTCOMES)}")
        unknown = set((rule.get("conditions") or {})) - set(RULE_CONDITION_KEYS)
        if unknown:
            errors.append(f"rule {rid}: unknown condition(s) {', '.join(sorted(unknown))}")
    for k, v in (doc.get("fallback") or {}).items():
        if v not in FALLBACKS:
            errors.append(f"fallback for {k} must be one of {', '.join(FALLBACKS)}")
    cap = (doc.get("undertrained") or {}).get("cap_action")
    if cap and cap not in OUTCOMES:
        errors.append("undertrained.cap_action must be an outcome")
    return errors


# --------------------------------------------------------------------------- persistence
def active_policy(db: Session, tenant_id: str) -> PolicyVersion:
    now = utcnow()
    rows = db.execute(select(PolicyVersion).where(PolicyVersion.tenant_id == tenant_id,
                                                   PolicyVersion.status == "ACTIVE")
                      .order_by(PolicyVersion.version.desc())).scalars().all()
    for row in rows:
        if (row.effective_from is None or row.effective_from <= now) and \
                (row.effective_to is None or row.effective_to > now):
            return row
    if rows:
        return rows[0]
    return ensure_default_policy(db, tenant_id)


def ensure_default_policy(db: Session, tenant_id: str) -> PolicyVersion:
    existing = db.execute(select(PolicyVersion).where(PolicyVersion.tenant_id == tenant_id)
                          .order_by(PolicyVersion.version.desc()).limit(1)).scalar()
    if existing:
        return existing
    pv = PolicyVersion(id=f"{tenant_id}-POLICY-01", tenant_id=tenant_id, version=1, name="Initial baseline policy",
                       status="ACTIVE", document=copy.deepcopy(DEFAULT_POLICY),
                       justification="Repository defaults - must be calibrated with institution-approved data "
                                     "before production enforcement.",
                       created_by_name="system", activated_at=utcnow())
    db.add(pv)
    db.commit()
    return pv


def next_version(db: Session, tenant_id: str) -> int:
    return int(db.execute(select(func.max(PolicyVersion.version)).where(PolicyVersion.tenant_id == tenant_id))
               .scalar() or 0) + 1


def diff(before: dict, after: dict, prefix: str = "") -> list[dict]:
    changes = []
    keys = sorted(set(before) | set(after))
    for key in keys:
        path = f"{prefix}{key}"
        a, b = before.get(key), after.get(key)
        if isinstance(a, dict) and isinstance(b, dict):
            changes.extend(diff(a, b, path + "."))
        elif a != b:
            changes.append({"path": path, "before": a, "after": b})
    return changes


# --------------------------------------------------------------------------- evaluation
def rule_matches(rule: dict, ctx: dict) -> bool:
    if not rule.get("enabled", True):
        return False
    cond = rule.get("conditions") or {}
    if cond.get("tx_types") and ctx["tx_type"] not in cond["tx_types"]:
        return False
    if cond.get("channels") and ctx.get("channel") not in cond["channels"]:
        return False
    if cond.get("modes") and ctx.get("mode") not in cond["modes"]:
        return False
    if cond.get("segments") and ctx.get("segment_key") not in [int(s) for s in cond["segments"]]:
        return False
    if cond.get("customer_types") and ctx.get("customer_type") not in cond["customer_types"]:
        return False
    if cond.get("customer_risk") and ctx.get("customer_risk") not in cond["customer_risk"]:
        return False
    if cond.get("countries"):
        countries = {c.upper() for c in cond["countries"]}
        if not ({ctx.get("beneficiary_country"), ctx.get("ip_country")} & countries):
            return False
    if cond.get("min_amount") is not None and ctx["amount"] < float(cond["min_amount"]):
        return False
    if cond.get("max_amount") is not None and ctx["amount"] > float(cond["max_amount"]):
        return False
    if cond.get("new_beneficiary") is not None and bool(cond["new_beneficiary"]) != bool(ctx.get("new_beneficiary")):
        return False
    if cond.get("hours") and ctx.get("hour") not in [int(h) for h in cond["hours"]]:
        return False
    if cond.get("velocity_count"):
        hours = float(cond.get("velocity_hours") or 24)
        count = ctx["velocity"](hours, cond.get("tx_types"))
        if count < int(cond["velocity_count"]):
            return False
    return True


def combine(doc: dict, risk_level: str, rule_hits: list[dict], aml_level: str, undertrained: bool,
            hard_rule: bool) -> tuple[str, list[str]]:
    """Return the outcome and the drivers that set it."""
    actions = doc.get("actions", {})
    outcome = actions.get(risk_level, "PASS")
    drivers = [f"risk level {risk_level} -> {outcome}"] if outcome != "PASS" else []
    if undertrained and not hard_rule and not rule_hits:
        cap = (doc.get("undertrained") or {}).get("cap_action", "REVIEW")
        if SEVERITY[outcome] > SEVERITY[cap]:
            drivers.append(f"limited customer history: capped {outcome} -> {cap}")
            outcome = cap
    aml = doc.get("aml", {})
    aml_action = {"ELEVATED": aml.get("elevated_action", "REVIEW"), "HIGH": aml.get("high_action", "HOLD")} \
        .get(aml_level)
    if aml_action and SEVERITY[aml_action] > SEVERITY[outcome]:
        outcome = aml_action
        drivers.append(f"AML signal {aml_level} -> {aml_action}")
    for hit in rule_hits:
        if SEVERITY[hit["action"]] > SEVERITY[outcome]:
            outcome = hit["action"]
            drivers.append(f"rule {hit['id']} -> {hit['action']}")
    return outcome, drivers


# --------------------------------------------------------------------------- simulation
def simulate(db: Session, tenant_id: str, space: str, doc: dict, days: int = 30, limit: int = 20000) -> dict:
    """Re-apply ``doc`` to stored profile scores of recent decisions (no rescoring, no side effects).

    The original operational decisions are untouched; this is an analytical preview.
    """
    from .engine_registry import make_decider

    since = utcnow() - timedelta(days=days)
    rows = db.execute(select(Decision, Transaction).join(Transaction, (Transaction.tenant_id == Decision.tenant_id)
                                                         & (Transaction.space == Decision.space)
                                                         & (Transaction.tx_ref == Decision.tx_ref))
                      .where(Decision.tenant_id == tenant_id, Decision.space == space, Decision.issued_at >= since,
                             Decision.decision != "ERROR_POLICY")
                      .order_by(Decision.issued_at.desc()).limit(limit)).all()
    current, proposed = Count(), Count()
    by_type: dict[str, Count] = {}
    changed = []
    decider = make_decider(doc["engine_thresholds"], tenant_id)
    for dec, tx in rows:
        level, hard = decider.level(dec, tx)
        hits = [h for h in (dec.rule_hits or []) if h.get("kind") == "policy_rule"]
        rule_ids = {r["id"]: r for r in doc.get("rules", [])}
        hits = [{"id": h["id"], "action": rule_ids[h["id"]]["action"]} for h in hits
                if h["id"] in rule_ids and rule_ids[h["id"]].get("enabled", True)]
        outcome, _ = combine(doc, level, hits, dec.aml_signal_level, dec.undertrained, hard)
        current[dec.decision] += 1
        proposed[outcome] += 1
        by_type.setdefault(tx.tx_type, Count())[outcome] += 1
        if outcome != dec.decision and len(changed) < 50:
            changed.append({"tx_ref": dec.tx_ref, "customer_ref": dec.customer_ref, "amount": tx.amount,
                            "fraud_score": dec.fraud_score, "current": dec.decision, "proposed": outcome})
    total = len(rows)
    return {
        "evaluated": total, "days": days,
        "current": {o: current.get(o, 0) for o in OUTCOMES},
        "proposed": {o: proposed.get(o, 0) for o in OUTCOMES},
        "delta": {o: proposed.get(o, 0) - current.get(o, 0) for o in OUTCOMES},
        "by_tx_type": {k: {o: v.get(o, 0) for o in OUTCOMES} for k, v in by_type.items()},
        "changed_examples": changed,
        "note": "Simulation re-applies thresholds/actions to stored profile scores. New rule conditions are "
                "evaluated on the next live decisions; original decisions are never rewritten.",
    }
