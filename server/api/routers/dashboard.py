from collections import Counter
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import Integer, case, cast, func, select
from sqlalchemy.orm import Session

from banksealer.engine.banksealer import PROFILE_NAMES

from ...config_store import load_config
from ...db import get_session
from ...mapping import reasons_json, score_json, tx_json
from ...models import ChannelConfig, ChannelStatus, Command, EngineStatus, ScoreRow, TransactionRow, User, now
from ..deps import viewer

router = APIRouter(prefix="/api", tags=["dashboard"])


def engine_json(st: Optional[EngineStatus]) -> dict:
    if st is None:
        return {"state": "worker not started", "trained": False, "online": False}
    online = bool(st.heartbeat and now() - st.heartbeat < timedelta(seconds=30))
    return {"state": st.state, "trained": st.trained, "training_size": st.training_size, "buffered": st.buffered,
            "customers": st.customers, "peer_groups": st.peer_groups, "scored_total": st.scored_total,
            "last_fit": st.last_fit.isoformat(timespec="seconds") if st.last_fit else None,
            "heartbeat": st.heartbeat.isoformat(timespec="seconds") if st.heartbeat else None, "online": online}


@router.get("/dashboard")
def dashboard(bank: Optional[str] = None, _: User = Depends(viewer), s: Session = Depends(get_session)):
    base = select(TransactionRow, ScoreRow).join(ScoreRow, ScoreRow.tx_id == TransactionRow.tx_id)

    def scoped(q):
        q = q.select_from(TransactionRow).join(ScoreRow, ScoreRow.tx_id == TransactionRow.tx_id)
        return q.where(TransactionRow.bank_id == bank) if bank else q

    susp = cast(ScoreRow.suspicious, Integer)
    k = s.execute(scoped(select(
        func.count(), func.coalesce(func.sum(susp), 0), func.avg(ScoreRow.combined),
        func.count(func.distinct(TransactionRow.user_id)),
        func.coalesce(func.sum(case((ScoreRow.risk_level == "CRITICAL", 1), else_=0)), 0),
        func.coalesce(func.sum(case((ScoreRow.risk_level == "HIGH", 1), else_=0)), 0),
        func.coalesce(func.sum(case(((ScoreRow.suspicious.is_(True)) & (ScoreRow.status == "open"), 1), else_=0)), 0),
        func.coalesce(func.sum(case((ScoreRow.status == "fraud", 1), else_=0)), 0),
        func.coalesce(func.sum(TransactionRow.amount), 0),
        func.coalesce(func.sum(case((ScoreRow.suspicious.is_(True), TransactionRow.amount), else_=0)), 0),
    ))).one()
    total, suspicious = int(k[0] or 0), int(k[1] or 0)
    kpis = {"transactions": total, "suspicious": suspicious, "rate": suspicious / total if total else 0.0,
            "avg_score": float(k[2] or 0), "customers": int(k[3] or 0), "critical": int(k[4]), "high": int(k[5]),
            "open_alerts": int(k[6]), "confirmed_fraud": int(k[7]), "amount": float(k[8]),
            "amount_at_risk": float(k[9])}

    bucket = cast(ScoreRow.combined * 20, Integer)
    hist = dict(s.execute(scoped(select(bucket, func.count())).group_by(bucket)).all())
    histogram = [int(hist.get(i, 0)) + (int(hist.get(20, 0)) if i == 19 else 0) for i in range(20)]

    day = func.date(TransactionRow.ts)
    per_day = s.execute(scoped(select(day, ScoreRow.risk_level, func.count()))
                        .where(ScoreRow.suspicious.is_(True)).group_by(day, ScoreRow.risk_level)).all()
    totals_day = dict(s.execute(scoped(select(day, func.count())).group_by(day)).all())
    days = sorted({str(d) for d in totals_day})[-30:]
    by_day = {d: {"date": d, "total": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0} for d in days}
    for d, n in totals_day.items():
        if str(d) in by_day:
            by_day[str(d)]["total"] = int(n)
    for d, level, n in per_day:
        if str(d) in by_day and level in by_day[str(d)]:
            by_day[str(d)][level] = int(n)

    bank_type = [{"bank_id": b, "tx_type": t, "count": int(n)} for b, t, n in s.execute(
        scoped(select(TransactionRow.bank_id, TransactionRow.tx_type, func.count()))
        .where(ScoreRow.suspicious.is_(True)).group_by(TransactionRow.bank_id, TransactionRow.tx_type)).all()]

    profiles = {("suspicious" if flag else "normal"): {"local": float(a or 0), "temporal": float(b or 0),
                                                         "global": float(c or 0), "count": int(n)}
                for flag, a, b, c, n in s.execute(scoped(select(
                    ScoreRow.suspicious, func.avg(ScoreRow.local), func.avg(ScoreRow.temporal),
                    func.avg(ScoreRow.global_), func.count())).group_by(ScoreRow.suspicious)).all()}

    recent = s.execute((base.where(TransactionRow.bank_id == bank) if bank else base)
                       .where(ScoreRow.suspicious.is_(True)).order_by(TransactionRow.ts.desc()).limit(400)).all()
    reason_counts = Counter()
    latest = []
    for i, (tx, sc) in enumerate(recent):
        reasons = reasons_json(tx, sc)
        for r in reasons[:3]:
            reason_counts[f"{PROFILE_NAMES.get(r['profile'], r['profile'])}: {r['feature']}"] += 1
        if i < 12:
            item = tx_json(tx)
            item["score"] = score_json(sc)
            item["main_reason"] = reasons[0]["message"] if reasons else None
            latest.append(item)

    channels = [{"id": c.id, "name": c.name, "type": c.type, "enabled": c.enabled,
                 "status": st.status if st else "unknown", "received": st.received if st else 0,
                 "rejected": st.rejected if st else 0}
                for c, st in s.execute(select(ChannelConfig, ChannelStatus).outerjoin(
                    ChannelStatus, ChannelStatus.channel_id == ChannelConfig.id).order_by(ChannelConfig.id)).all()]
    cfg = load_config(s)
    th = cfg.thresholds_for(bank)
    return {
        "kpis": kpis, "histogram": histogram, "by_day": list(by_day.values()), "by_bank_type": bank_type,
        "profiles": profiles, "top_reasons": [{"reason": r, "count": n} for r, n in reason_counts.most_common(8)],
        "latest": latest, "channels": channels, "engine": engine_json(s.get(EngineStatus, 1)),
        "thresholds": {k: th.get(k) for k in ("suspicious", "high", "critical")},
        "banks": [{"code": c, "name": cfg.bank_name(c)} for c in cfg.banks],
        "pending_commands": s.scalar(select(func.count(Command.id)).where(Command.status.in_(("pending", "running")))),
    }
