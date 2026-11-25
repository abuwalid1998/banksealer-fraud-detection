from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Integer, case, cast, func, select
from sqlalchemy.orm import Session

from ...db import get_session
from ...mapping import score_json, tx_json
from ...models import CustomerProfile, ScoreRow, TransactionRow, User
from ..deps import page, viewer

router = APIRouter(prefix="/api", tags=["customers"])


def _aggregate():
    return select(
        TransactionRow.user_id.label("user_id"),
        func.max(TransactionRow.bank_id).label("bank_id"),
        func.count().label("transactions"),
        func.coalesce(func.sum(cast(ScoreRow.suspicious, Integer)), 0).label("suspicious"),
        func.coalesce(func.sum(case((ScoreRow.status == "fraud", 1), else_=0)), 0).label("confirmed_fraud"),
        func.max(ScoreRow.combined).label("max_score"),
        func.avg(ScoreRow.combined).label("avg_score"),
        func.sum(TransactionRow.amount).label("amount"),
        func.max(TransactionRow.ts).label("last_activity"),
    ).join(ScoreRow, ScoreRow.tx_id == TransactionRow.tx_id).group_by(TransactionRow.user_id)


SORTS = {"max_score", "avg_score", "transactions", "suspicious", "amount", "last_activity", "user_id"}


@router.get("/customers")
def list_customers(q: Optional[str] = None, bank: Optional[str] = None, flagged: bool = False, page_no: int = 1,
                   size: int = 50, sort: str = "max_score", order: str = "desc", _: User = Depends(viewer),
                   s: Session = Depends(get_session)):
    agg = _aggregate()
    if q:
        agg = agg.where(TransactionRow.user_id.ilike(f"%{q}%"))
    if bank:
        agg = agg.where(TransactionRow.bank_id == bank)
    sub = agg.subquery()
    query = select(sub, CustomerProfile.cluster, CustomerProfile.global_score).outerjoin(
        CustomerProfile, CustomerProfile.user_id == sub.c.user_id)
    if flagged:
        query = query.where(sub.c.suspicious > 0)
    total = s.scalar(select(func.count()).select_from(query.subquery()))
    col = sub.c[sort if sort in SORTS else "max_score"]
    query = query.order_by(col.desc() if order == "desc" else col.asc(), sub.c.user_id)
    rows = s.execute(query.offset((page_no - 1) * size).limit(size)).mappings().all()
    items = [{**{k: v for k, v in r.items()}, "last_activity": r["last_activity"].isoformat(sep=" ", timespec="minutes")
              if r["last_activity"] else None} for r in rows]
    return page(items, total, page_no, size)


@router.get("/customers/{user_id}")
def get_customer(user_id: str, _: User = Depends(viewer), s: Session = Depends(get_session)):
    stats = s.execute(_aggregate().where(TransactionRow.user_id == user_id)).mappings().first()
    profile = s.get(CustomerProfile, user_id)
    if stats is None and profile is None:
        raise HTTPException(404, "customer not found")
    rows = s.execute(select(TransactionRow, ScoreRow).join(ScoreRow, ScoreRow.tx_id == TransactionRow.tx_id)
                     .where(TransactionRow.user_id == user_id).order_by(TransactionRow.ts.desc()).limit(300)).all()
    txs = []
    for tx, sc in rows:
        item = tx_json(tx)
        item["score"] = score_json(sc)
        txs.append(item)
    training = s.scalar(select(func.count()).select_from(TransactionRow).where(
        TransactionRow.user_id == user_id, TransactionRow.is_training.is_(True)))
    return {
        "user_id": user_id,
        "stats": {**dict(stats), "last_activity": stats["last_activity"].isoformat(sep=" ", timespec="minutes")
                  if stats and stats["last_activity"] else None} if stats else None,
        "training_transactions": training,
        "profile": profile.summary if profile else None,
        "cluster": profile.cluster if profile else None,
        "global_score": profile.global_score if profile else None,
        "transactions": txs,
    }
