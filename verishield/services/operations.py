"""Operational services: periodic customer reviews, scheduled reports, channel data quality, archiving,
value (money saved) metrics, champion / challenger comparison and the board "what if" simulation."""
from __future__ import annotations

import gzip
import json
import logging
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import (Alert, AuditEvent, Case, CaseEntityLink, Connector, Customer, CustomerReview, DeadLetterEvent,
                      Decision, ExecutionStatusEvent, ReportSchedule, Tenant, Transaction, User, utcnow)
from ..settings import settings

log = logging.getLogger("verishield.operations")

# --------------------------------------------------------------------------- periodic customer reviews
REVIEW_MONTHS = {"HIGH": 6, "MEDIUM": 12, "LOW": 36}
REVIEW_CHECKLIST = ["Identity and KYC documents still valid", "Occupation / business activity confirmed",
                    "Source of funds and expected turnover confirmed", "Activity of the period consistent with profile",
                    "Sanctions / PEP screening checked", "Risk rating confirmed or changed"]


def review_settings(tenant: Tenant) -> dict:
    return {"months": {**REVIEW_MONTHS, **((tenant.settings or {}).get("reviews", {}).get("months") or {})}}


def schedule_reviews(db: Session, tenant_id: str, space: str) -> int:
    """Create the next periodic review for every customer that has none open (by institution risk rating)."""
    tenant = db.get(Tenant, tenant_id)
    months = review_settings(tenant)["months"]
    open_refs = {r for (r,) in db.execute(select(CustomerReview.customer_ref).where(
        CustomerReview.tenant_id == tenant_id, CustomerReview.space == space,
        CustomerReview.status != "COMPLETED")).all()}
    last_done = dict(db.execute(select(CustomerReview.customer_ref, func.max(CustomerReview.completed_at)).where(
        CustomerReview.tenant_id == tenant_id, CustomerReview.space == space,
        CustomerReview.status == "COMPLETED").group_by(CustomerReview.customer_ref)).all())
    n = 0
    for c in db.execute(select(Customer).where(Customer.tenant_id == tenant_id, Customer.space == space)).scalars():
        if c.customer_ref in open_refs:
            continue
        m = int(months.get(c.risk_category, 12))
        anchor = last_done.get(c.customer_ref) or (c.created_at - timedelta(days=30 * max(c.tenure_months % m, 0)))
        due = anchor + timedelta(days=30 * m)
        if c.kyc_review_status == "DUE_FOR_REVIEW":
            due = min(due, utcnow() + timedelta(days=14))
        if due <= utcnow() + timedelta(days=60):
            db.add(CustomerReview(tenant_id=tenant_id, space=space, customer_ref=c.customer_ref, reason="PERIODIC",
                                  risk_at_open=c.risk_category, due_at=due,
                                  checklist=[{"item": i, "done": False} for i in REVIEW_CHECKLIST]))
            n += 1
    db.commit()
    return n


def trigger_review(db: Session, tenant_id: str, space: str, customer_ref: str, reason: str = "TRIGGER",
                   days: int = 14) -> CustomerReview:
    existing = db.execute(select(CustomerReview).where(
        CustomerReview.tenant_id == tenant_id, CustomerReview.space == space,
        CustomerReview.customer_ref == customer_ref, CustomerReview.status != "COMPLETED")).scalar()
    if existing:
        existing.due_at = min(existing.due_at, utcnow() + timedelta(days=days))
        return existing
    c = db.execute(select(Customer).where(Customer.tenant_id == tenant_id, Customer.space == space,
                                          Customer.customer_ref == customer_ref)).scalar()
    if c is None:
        raise LookupError("customer not found")
    rev = CustomerReview(tenant_id=tenant_id, space=space, customer_ref=customer_ref, reason=reason,
                         risk_at_open=c.risk_category, due_at=utcnow() + timedelta(days=days),
                         checklist=[{"item": i, "done": False} for i in REVIEW_CHECKLIST])
    db.add(rev)
    db.flush()
    return rev


def complete_review(db: Session, rev: CustomerReview, outcome: str, new_risk: str, notes: str, actor: str) -> dict:
    if rev.status == "COMPLETED":
        raise ValueError("review already completed")
    if new_risk and new_risk not in ("LOW", "MEDIUM", "HIGH"):
        raise ValueError("risk rating must be LOW, MEDIUM or HIGH")
    if len((notes or "").strip()) < 10:
        raise ValueError("record the review conclusion (at least 10 characters)")
    c = db.execute(select(Customer).where(Customer.tenant_id == rev.tenant_id, Customer.space == rev.space,
                                          Customer.customer_ref == rev.customer_ref)).scalar()
    before = c.risk_category if c else ""
    if c is not None:
        if new_risk:
            c.risk_category = new_risk
        c.kyc_review_status = "CURRENT"
        c.updated_at = utcnow()
    rev.status, rev.outcome, rev.new_risk, rev.notes = "COMPLETED", outcome[:40] or "REVIEWED", new_risk or before, notes
    rev.completed_by, rev.completed_at = actor, utcnow()
    db.commit()
    schedule_reviews(db, rev.tenant_id, rev.space)
    return {"risk_before": before, "risk_after": rev.new_risk}


# --------------------------------------------------------------------------- scheduled reports
def next_run(frequency: str, after: Optional[datetime] = None) -> datetime:
    base = (after or utcnow()).replace(hour=5, minute=0, second=0, microsecond=0)     # 07:00-08:00 Palestine
    if frequency == "DAILY":
        return base + timedelta(days=1)
    if frequency == "WEEKLY":                     # Saturdays (first working day of the week in Palestine)
        days = (5 - base.weekday()) % 7 or 7
        return base + timedelta(days=days)
    month = base.replace(day=1) + timedelta(days=32)
    return month.replace(day=1)


def run_schedule(db: Session, sched: ReportSchedule) -> str:
    from ..deps import Principal
    from ..rbac import permissions_for
    from . import notify, reports
    owner = db.get(User, sched.owner_id) if sched.owner_id else None
    if owner is None or owner.status not in ("ACTIVE", "INVITED"):
        return "owner account is not active - schedule skipped"
    p = Principal(tenant_id=sched.tenant_id, kind="user", role=owner.role,
                  perms=permissions_for(owner.role, owner.extra_permissions), space=sched.space,
                  display=f"{owner.full_name} (scheduled)", user_id=owner.id)
    days = {"DAILY": 1, "WEEKLY": 7, "MONTHLY": 31}.get(sched.frequency, 7)
    params = {**(sched.params or {}), "date_from": (utcnow() - timedelta(days=days)).date().isoformat(),
              "date_to": utcnow().date().isoformat()}
    job = reports.generate(db, p, sched.report_type, sched.fmt, params)
    if job.status != "DONE":
        return f"report failed: {job.error}"
    path = reports.report_path(job)
    tenant = db.get(Tenant, sched.tenant_id)
    sent = 0
    for to in sched.recipients or []:
        ok, _ = notify.send_email(db, tenant, to, f"{job.title} ({sched.frequency.lower()})",
                                  f"Attached: {job.title}, report {job.number}, period {params['date_from']} to "
                                  f"{params['date_to']}.", "scheduled_report",
                                  attachments=[(f"{job.number}.{sched.fmt}", open(path, "rb").read())] if path else None)
        sent += ok
    return f"report {job.number} generated; e-mailed to {sent} of {len(sched.recipients or [])} recipient(s)"


def run_due_schedules(db: Session) -> int:
    n = 0
    for sched in db.execute(select(ReportSchedule).where(ReportSchedule.active.is_(True),
                                                         ReportSchedule.next_run_at <= utcnow())).scalars().all():
        try:
            sched.last_status = run_schedule(db, sched)[:200]
        except Exception as exc:  # noqa: BLE001
            sched.last_status = f"error: {exc}"[:200]
            log.warning("scheduled report %s failed: %s", sched.id, exc)
        sched.last_run_at = utcnow()
        sched.next_run_at = next_run(sched.frequency)
        db.commit()
        n += 1
    return n


# --------------------------------------------------------------------------- channel data quality
QUALITY_FIELDS = ("channel", "ip", "beneficiary", "beneficiary_country", "currency")


def data_quality(db: Session, tenant_id: str, space: str, hours: int = 24) -> list[dict]:
    now = utcnow()
    out = []
    for c in db.execute(select(Connector).where(Connector.tenant_id == tenant_id, Connector.space == space,
                                                Connector.direction == "INBOUND",
                                                Connector.status != "RETIRED")).scalars():
        recent = db.execute(select(Transaction.created_at, Transaction.channel, Transaction.ip, Transaction.beneficiary,
                                   Transaction.beneficiary_country, Transaction.currency, Transaction.tx_type,
                                   Transaction.ts).where(
            Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.source == c.name,
            Transaction.created_at >= now - timedelta(days=8))).all()
        last = [r for r in recent if r.created_at >= now - timedelta(hours=hours)]
        per_hour = Counter(r.created_at.strftime("%Y-%m-%d %H") for r in recent
                           if r.created_at < now - timedelta(hours=hours))
        baseline = statistics.median(per_hour.values()) if per_hour else None
        rate = len(last) / hours
        dlq = dict(db.execute(select(DeadLetterEvent.category, func.count()).where(
            DeadLetterEvent.tenant_id == tenant_id, DeadLetterEvent.space == space,
            DeadLetterEvent.connector_id == c.id, DeadLetterEvent.created_at >= now - timedelta(hours=hours))
            .group_by(DeadLetterEvent.category)).all())
        reasons = Counter(r for (r,) in db.execute(select(DeadLetterEvent.reason).where(
            DeadLetterEvent.tenant_id == tenant_id, DeadLetterEvent.connector_id == c.id,
            DeadLetterEvent.created_at >= now - timedelta(hours=hours))).all())
        unknown_types = sum(n for r, n in reasons.items() if "unknown transaction type" in r)
        missing = {f: round(100 * sum(1 for r in last if not getattr(r, f)) / len(last), 1) if last else None
                   for f in QUALITY_FIELDS}
        lag = [max(0.0, (r.created_at - r.ts).total_seconds() / 60) for r in last if r.ts <= r.created_at]
        received = len(last) + sum(dlq.values())
        issues = []
        if c.status in ("ENABLED", "DEGRADED") and baseline and rate < 0.3 * baseline:
            issues.append(f"volume drop: {rate:.1f}/h vs usual {baseline:.1f}/h")
        if received and dlq.get("VALIDATION", 0) / received > 0.05:
            issues.append(f"{100 * dlq.get('VALIDATION', 0) / received:.1f}% of records rejected")
        if unknown_types:
            issues.append(f"{unknown_types} records with an unknown transaction type")
        for f, pct in missing.items():
            if pct is not None and pct > 20 and f in ("ip", "channel"):
                issues.append(f"{f} missing on {pct:.0f}% of records")
        if lag and statistics.median(lag) > 30:
            issues.append(f"median delivery delay {statistics.median(lag):.0f} min")
        out.append({"connector_id": c.id, "name": c.name, "type": c.type, "status": c.status,
                    "received": received, "accepted": len(last), "rejected": dlq.get("VALIDATION", 0),
                    "duplicates": dlq.get("DUPLICATE", 0), "unknown_types": unknown_types,
                    "rate_per_hour": round(rate, 2), "baseline_per_hour": round(baseline, 2) if baseline else None,
                    "missing_pct": missing, "median_delay_min": round(statistics.median(lag), 1) if lag else None,
                    "types": dict(Counter(r.tx_type for r in last)), "top_rejections": reasons.most_common(3),
                    "issues": issues, "health": "ATTENTION" if issues else "OK"})
    return out


def check_quality(db: Session) -> None:
    from .decision import open_incident
    for tenant in db.execute(select(Tenant)).scalars():
        for space in ("LIVE", "DEMO"):
            for q in data_quality(db, tenant.id, space):
                if q["issues"] and q["status"] in ("ENABLED", "DEGRADED"):
                    open_incident(db, tenant.id, f"quality:{q['connector_id']}",
                                  f"Data quality: {q['name']}", "SEV4", affected="; ".join(q["issues"])[:200])


# --------------------------------------------------------------------------- archiving
def archive(db: Session, tenant_id: str, space: str, actor: str, dry_run: bool = False) -> dict:
    """Move transactions / decisions / closed alerts older than the retention period to compressed files."""
    from .. import audit
    tenant = db.get(Tenant, tenant_id)
    days = int(((tenant.settings or {}).get("retention") or {}).get("transactions_days", 1825))
    cutoff = utcnow() - timedelta(days=days)
    protected = {r for (r,) in db.execute(select(CaseEntityLink.entity_ref).join(
        Case, Case.id == CaseEntityLink.case_id).where(Case.tenant_id == tenant_id, Case.status != "CLOSED",
                                                       CaseEntityLink.entity_type == "TRANSACTION")).all()}
    rows = [t for t in db.execute(select(Transaction).where(Transaction.tenant_id == tenant_id,
                                                            Transaction.space == space,
                                                            Transaction.ts < cutoff)).scalars()
            if t.tx_ref not in protected]
    refs = [t.tx_ref for t in rows]
    decisions = db.execute(select(Decision).where(Decision.tenant_id == tenant_id, Decision.space == space,
                                                  Decision.tx_ref.in_(refs))).scalars().all() if refs else []
    alerts = db.execute(select(Alert).where(Alert.tenant_id == tenant_id, Alert.space == space,
                                            Alert.created_at < cutoff, Alert.status == "CLOSED")).scalars().all()
    result = {"cutoff": cutoff.isoformat(timespec="seconds"), "retention_days": days, "transactions": len(rows),
              "decisions": len(decisions), "alerts": len(alerts), "protected_by_open_cases": len(protected)}
    if dry_run or not (rows or alerts):
        result["file"] = None
        return result
    from .common import row_dict
    folder = settings.data_dir / "archive" / tenant_id / space.lower()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"archive_{utcnow():%Y%m%d_%H%M%S}.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for kind, items in (("transaction", rows), ("decision", decisions), ("alert", alerts)):
            for it in items:
                fh.write(json.dumps({"kind": kind, **row_dict(it)}, default=str) + "\n")
    if refs:
        db.execute(delete(ExecutionStatusEvent).where(ExecutionStatusEvent.tenant_id == tenant_id,
                                                      ExecutionStatusEvent.space == space,
                                                      ExecutionStatusEvent.tx_ref.in_(refs)))
        db.execute(delete(Decision).where(Decision.tenant_id == tenant_id, Decision.space == space,
                                          Decision.tx_ref.in_(refs)))
        db.execute(delete(Transaction).where(Transaction.tenant_id == tenant_id, Transaction.space == space,
                                             Transaction.tx_ref.in_(refs)))
    if alerts:
        db.execute(delete(Alert).where(Alert.id.in_([a.id for a in alerts])))
    db.commit()
    result["file"] = str(path)
    audit.record(db, tenant_id, "data.archive", actor=actor, resource_type="archive", space=space, after=result)
    return result


def archives(tenant_id: str) -> list[dict]:
    root = settings.data_dir / "archive" / tenant_id
    if not root.exists():
        return []
    return [{"file": p.name, "space": p.parent.name.upper(), "bytes": p.stat().st_size,
             "created": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")}
            for p in sorted(root.glob("*/*.jsonl.gz"), reverse=True)]


# --------------------------------------------------------------------------- value (money saved)
def value_metrics(db: Session, tenant_id: str, space: str, days: int = 30) -> dict:
    tenant = db.get(Tenant, tenant_id)
    cost = (tenant.settings or {}).get("value", {})
    minutes_alert = float(cost.get("minutes_per_alert", 12))
    hourly = float(cost.get("analyst_hourly_cost", 60))
    since = utcnow() - timedelta(days=days)
    rows = db.execute(select(Transaction.amount, Transaction.label, Transaction.mode, Transaction.execution_state,
                             Decision.decision, Decision.issued_at).join(
        Decision, (Decision.tenant_id == Transaction.tenant_id) & (Decision.space == Transaction.space)
        & (Decision.tx_ref == Transaction.tx_ref)).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space, Decision.issued_at >= since)).all()
    prevented = sum(r.amount for r in rows if r.label == 1 and r.mode == "PRE_AUTH"
                    and r.execution_state in ("DECLINED", "HELD", "DECLINE_REQUESTED"))
    confirmed = sum(r.amount for r in rows if r.label == 1)
    missed = sum(r.amount for r in rows if r.label == 1 and r.decision == "PASS")
    held_legit = sum(1 for r in rows if r.label == 0 and r.decision in ("HOLD", "DECLINE"))
    alerts = db.execute(select(Alert.created_at, Alert.status, Alert.closed_at, Alert.closed_reason).where(
        Alert.tenant_id == tenant_id, Alert.space == space, Alert.created_at >= since)).all()
    closed = [a for a in alerts if a.status == "CLOSED"]
    fp = sum(1 for a in closed if any(k in (a.closed_reason or "").lower()
                                      for k in ("false positive", "no concern", "confirmed by the customer")))
    handling = len(alerts) * minutes_alert / 60 * hourly
    weekly: dict[str, dict] = defaultdict(lambda: {"prevented": 0.0, "alerts": 0, "closed": 0})
    for r in rows:
        if r.label == 1 and r.mode == "PRE_AUTH" and r.execution_state in ("DECLINED", "HELD", "DECLINE_REQUESTED"):
            weekly[(r.issued_at - timedelta(days=r.issued_at.weekday())).strftime("%Y-%m-%d")]["prevented"] += r.amount
    for a in alerts:
        wk = (a.created_at - timedelta(days=a.created_at.weekday())).strftime("%Y-%m-%d")
        weekly[wk]["alerts"] += 1
        weekly[wk]["closed"] += a.status == "CLOSED"
    analysts = db.execute(select(User.first_name, User.last_name, func.count(Case.id))
                          .join(Case, Case.assignee_id == User.id).where(
        User.tenant_id == tenant_id, Case.space == space, Case.recommended_at >= since)
        .group_by(User.id, User.first_name, User.last_name)).all()
    return {"days": days, "currency": tenant.base_currency, "assumptions": {"minutes_per_alert": minutes_alert,
                                                                           "analyst_hourly_cost": hourly},
            "prevented_loss": round(prevented, 2), "confirmed_fraud_value": round(confirmed, 2),
            "missed_fraud_value": round(missed, 2),
            "detection_rate_by_value": round(100 * (confirmed - missed) / confirmed, 1) if confirmed else None,
            "alerts": len(alerts), "alerts_closed": len(closed), "false_positive_share":
                round(100 * fp / len(closed), 1) if closed else None,
            "legitimate_payments_held": held_legit, "alert_handling_cost": round(handling, 2),
            "cost_per_alert": round(minutes_alert / 60 * hourly, 2),
            "net_benefit": round(prevented - handling, 2),
            "return_per_unit_cost": round(prevented / handling, 1) if handling else None,
            "weekly": [{"week": k, **v} for k, v in sorted(weekly.items())],
            "analysts": [{"name": f"{a} {b}", "recommendations": n} for a, b, n in analysts]}


# --------------------------------------------------------------------------- champion vs challenger
def challenger(db: Session, tenant_id: str, space: str, limit: int = 5000) -> dict:
    rows = db.execute(select(Decision, Transaction.label, Transaction.scenario).join(
        Transaction, (Transaction.tenant_id == Decision.tenant_id) & (Transaction.space == Decision.space)
        & (Transaction.tx_ref == Decision.tx_ref)).where(
        Decision.tenant_id == tenant_id, Decision.space == space, Decision.decision != "ERROR_POLICY")
        .order_by(Decision.issued_at.desc()).limit(limit)).all()
    pairs = []
    for d, label, scenario in rows:
        sh = (d.shadow or {}).get("model_shadow") or {}
        if "fraud_score" not in sh:
            continue
        truth = label if label is not None else ((1 if scenario else 0) if space == "DEMO" else None)
        pairs.append((d.fraud_score, sh["fraud_score"], d.risk_level, sh.get("risk_level"), truth, sh.get("model"),
                      d.fraud_model_version))
    if not pairs:
        return {"pairs": 0, "note": "No decisions with a shadow (challenger) score yet - train a model as shadow "
                                    "under Models › Fraud models and let traffic flow."}
    champion, challenger_ = pairs[0][6], pairs[0][5]
    agree = sum(1 for p in pairs if p[2] == p[3])
    a = [p[0] for p in pairs]
    b = [p[1] for p in pairs]
    try:
        corr = round(statistics.correlation(a, b), 3)
    except statistics.StatisticsError:
        corr = None

    def perf(idx: int, thr: float = 0.5):
        lab = [p for p in pairs if p[4] is not None]
        if not lab:
            return None
        tp = sum(1 for p in lab if p[idx] >= thr and p[4] == 1)
        fp = sum(1 for p in lab if p[idx] >= thr and p[4] == 0)
        fn = sum(1 for p in lab if p[idx] < thr and p[4] == 1)
        return {"precision": round(tp / (tp + fp), 3) if tp + fp else None,
                "recall": round(tp / (tp + fn), 3) if tp + fn else None, "alerts": tp + fp, "labelled": len(lab)}
    bins = [0] * 10
    bins2 = [0] * 10
    for x, y in zip(a, b):
        bins[min(int(x * 10), 9)] += 1
        bins2[min(int(y * 10), 9)] += 1
    return {"pairs": len(pairs), "champion": champion, "challenger": challenger_,
            "risk_level_agreement": round(100 * agree / len(pairs), 1), "score_correlation": corr,
            "would_alert": {"champion": sum(1 for x in a if x >= 0.5), "challenger": sum(1 for y in b if y >= 0.5)},
            "performance": {"champion": perf(0), "challenger": perf(1)},
            "distribution": [{"bin": f"{i / 10:.1f}", "champion": bins[i], "challenger": bins2[i]} for i in range(10)],
            "disagreements": [{"champion_score": round(p[0], 3), "challenger_score": round(p[1], 3),
                               "champion_level": p[2], "challenger_level": p[3], "truth": p[4]}
                              for p in pairs if p[2] != p[3]][:15]}


# --------------------------------------------------------------------------- board "what if"
def what_if(db: Session, tenant_id: str, space: str, strictness: float, days: int = 14) -> dict:
    """Scale the active policy's engine thresholds (strictness 0.5 = much stricter ... 1.5 = much more lenient)
    and replay recent decisions - no side effects."""
    import copy
    from .policy import active_policy, simulate
    strictness = max(0.5, min(1.5, float(strictness)))
    doc = copy.deepcopy(active_policy(db, tenant_id).document)
    th = doc["engine_thresholds"]
    for k in ("suspicious", "high", "critical"):
        th[k] = round(min(0.99, max(0.05, float(th[k]) * strictness)), 3)
    sim = simulate(db, tenant_id, space, doc, days=days)
    analyst_minutes = 12
    cur, prop = sim.get("current", {}), sim.get("proposed", {})
    interventions = lambda c: sum(c.get(k, 0) for k in ("REVIEW", "HOLD", "DECLINE"))  # noqa: E731
    return {"strictness": strictness, "thresholds": {k: th[k] for k in ("suspicious", "high", "critical")},
            "days": days, "current": cur, "proposed": prop,
            "interventions": {"current": interventions(cur), "proposed": interventions(prop)},
            "analyst_hours": {"current": round(interventions(cur) * analyst_minutes / 60, 1),
                              "proposed": round(interventions(prop) * analyst_minutes / 60, 1)},
            "delta": sim.get("delta"), "examples": sim.get("changed_examples", [])[:10],
            "evaluated": sim.get("evaluated", 0)}


__all__ = ["schedule_reviews", "trigger_review", "complete_review", "run_due_schedules", "next_run", "data_quality",
           "check_quality", "archive", "archives", "value_metrics", "challenger", "what_if", "AuditEvent"]
