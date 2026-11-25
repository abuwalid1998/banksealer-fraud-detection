import csv
import io
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from banksealer.engine.banksealer import ranked_reasons
from banksealer.reports import ReportGenerator

from ...config_store import load_config
from ...db import get_session
from ...mapping import reasons_json, row_to_result, row_to_tx, score_json, tx_json
from ...models import CustomerProfile, Report, ScoreRow, TransactionRow, User, now
from ...settings import settings
from ..deps import analyst, audit, enqueue, page, viewer
from ..filters import TxFilters

router = APIRouter(prefix="/api", tags=["transactions"])

SORTS = {"timestamp": TransactionRow.ts, "amount": TransactionRow.amount, "score": ScoreRow.combined,
         "local": ScoreRow.local, "temporal": ScoreRow.temporal, "global": ScoreRow.global_,
         "customer": TransactionRow.user_id}


def _base():
    return select(TransactionRow, ScoreRow).join(ScoreRow, ScoreRow.tx_id == TransactionRow.tx_id)


@router.get("/transactions")
def list_transactions(f: TxFilters = Depends(), page_no: int = 1, size: int = 50, sort: str = "timestamp",
                      order: str = "desc", _: User = Depends(viewer), s: Session = Depends(get_session)):
    size = max(1, min(size, 500))
    q = f.apply(_base())
    total = s.scalar(select(func.count()).select_from(q.subquery()))
    col = SORTS.get(sort, TransactionRow.ts)
    q = q.order_by(col.desc() if order == "desc" else col.asc(), TransactionRow.tx_id)
    rows = s.execute(q.offset((page_no - 1) * size).limit(size)).all()
    items = []
    for tx, sc in rows:
        item = tx_json(tx)
        item["score"] = score_json(sc)
        top = reasons_json(tx, sc)[:1] if sc.suspicious else []
        item["main_reason"] = top[0]["message"] if top else None
        items.append(item)
    return page(items, total, page_no, size)


@router.get("/transactions/{tx_id}")
def get_transaction(tx_id: str, _: User = Depends(viewer), s: Session = Depends(get_session)):
    row = s.execute(_base().where(TransactionRow.tx_id == tx_id)).first()
    if row is None:
        raise HTTPException(404, "transaction not found")
    tx, sc = row
    cfg = load_config(s)
    th = cfg.thresholds_for(tx.bank_id)
    reports = s.scalars(select(Report).where(Report.tx_id == tx_id).order_by(Report.id.desc()))
    profile = s.get(CustomerProfile, tx.user_id)
    return {
        "transaction": tx_json(tx), "score": score_json(sc, full=True), "reasons": reasons_json(tx, sc),
        "bank": {"code": tx.bank_id, "name": cfg.bank_name(tx.bank_id), "thresholds": th},
        "reports": [{"id": r.id, "created_at": r.created_at.isoformat(timespec="seconds"), "created_by": r.created_by}
                    for r in reports],
        "customer_profile": profile.summary if profile else None,
    }


class Feedback(BaseModel):
    status: str             # open | fraud | legit
    note: Optional[str] = None


@router.post("/transactions/{tx_id}/feedback")
def feedback(tx_id: str, body: Feedback, user: User = Depends(analyst), s: Session = Depends(get_session)):
    if body.status not in ("open", "fraud", "legit"):
        raise HTTPException(400, "status must be open, fraud or legit")
    sc = s.get(ScoreRow, tx_id)
    if sc is None:
        raise HTTPException(404, "transaction not found")
    previous = sc.status
    sc.status, sc.analyst_note, sc.reviewed_by, sc.reviewed_at = body.status, body.note, user.username, now()
    if body.status == "legit" and previous != "legit" and sc.suspicious:
        enqueue(s, user, "learn", {"tx_id": tx_id})   # the worker adds the behaviour to the customer profile
    audit(s, user, "transaction.feedback", tx_id, {"status": body.status, "note": body.note})
    s.commit()
    return score_json(sc, full=True)


def generate_report(s: Session, tx_id: str, user: User) -> Report:
    row = s.execute(_base().where(TransactionRow.tx_id == tx_id)).first()
    if row is None:
        raise HTTPException(404, "transaction not found")
    tx_row, sc = row
    result = row_to_result(tx_row, sc)
    profile = s.get(CustomerProfile, tx_row.user_id)
    gen = ReportGenerator(load_config(s), settings.reports_dir)
    path = gen.transaction_report(row_to_tx(tx_row), result, profile.summary if profile else None,
                                  ranked_reasons(result))
    report = Report(tx_id=tx_id, kind="transaction", path=path, created_by=user.username)
    s.add(report)
    audit(s, user, "report.create", tx_id)
    s.commit()
    return report


@router.post("/transactions/{tx_id}/report")
def create_report(tx_id: str, user: User = Depends(analyst), s: Session = Depends(get_session)):
    r = generate_report(s, tx_id, user)
    return {"id": r.id, "tx_id": tx_id, "created_at": r.created_at.isoformat(timespec="seconds")}


@router.get("/export/transactions.csv")
def export_csv(f: TxFilters = Depends(), limit: int = 100_000, user: User = Depends(viewer),
               s: Session = Depends(get_session)):
    rows = s.execute(f.apply(_base()).order_by(TransactionRow.ts.desc()).limit(limit)).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["tx_id", "timestamp", "bank_id", "channel", "user_id", "tx_type", "amount", "currency", "beneficiary",
                "beneficiary_country", "cc_asn", "ip", "local_score", "temporal_score", "global_score",
                "suspicion_score", "risk_level", "suspicious", "status", "explanation"])
    for tx, sc in rows:
        t = tx_json(tx)
        w.writerow([tx.tx_id, t["timestamp"], tx.bank_id, tx.channel, tx.user_id, tx.tx_type, tx.amount, tx.currency,
                    t["beneficiary"], tx.iban_cc, tx.cc_asn, tx.ip, sc.local, sc.temporal, sc.global_, sc.combined,
                    sc.risk_level, int(sc.suspicious), sc.status, sc.explanation])
    audit(s, user, "export.csv", None, {"rows": len(rows)})
    s.commit()
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=banksealer_transactions.csv"})


# ------------------------------------------------------------------ reports
@router.get("/reports")
def list_reports(page_no: int = 1, size: int = 50, kind: Optional[str] = None, tx_id: Optional[str] = None,
                 _: User = Depends(viewer), s: Session = Depends(get_session)):
    q = select(Report, TransactionRow, ScoreRow).outerjoin(TransactionRow, TransactionRow.tx_id == Report.tx_id) \
        .outerjoin(ScoreRow, ScoreRow.tx_id == Report.tx_id)
    if kind:
        q = q.where(Report.kind == kind)
    if tx_id:
        q = q.where(Report.tx_id.ilike(f"%{tx_id}%"))
    total = s.scalar(select(func.count()).select_from(q.subquery()))
    rows = s.execute(q.order_by(Report.id.desc()).offset((page_no - 1) * size).limit(size)).all()
    items = []
    for r, tx, sc in rows:
        items.append({"id": r.id, "kind": r.kind, "tx_id": r.tx_id, "created_at": r.created_at.isoformat(timespec="seconds"),
                      "created_by": r.created_by, "file": os.path.basename(r.path),
                      "user_id": tx.user_id if tx else None, "bank_id": tx.bank_id if tx else None,
                      "amount": tx.amount if tx else None, "risk_level": sc.risk_level if sc else None,
                      "score": sc.combined if sc else None})
    return page(items, total, page_no, size)


@router.get("/reports/{report_id}/download")
def download_report(report_id: int, user: User = Depends(viewer), s: Session = Depends(get_session)):
    r = s.get(Report, report_id)
    if r is None or not os.path.exists(r.path):
        raise HTTPException(404, "report not found")
    root = os.path.abspath(settings.reports_dir)
    if not os.path.abspath(r.path).startswith(root + os.sep):
        raise HTTPException(403, "report outside the reports directory")
    audit(s, user, "report.download", str(report_id))
    s.commit()
    return FileResponse(r.path, media_type="application/pdf", filename=os.path.basename(r.path))


@router.post("/reports/summary")
def summary_report(f: TxFilters = Depends(), limit: int = 5000, user: User = Depends(analyst),
                   s: Session = Depends(get_session)):
    rows = s.execute(f.apply(_base()).order_by(ScoreRow.combined.desc()).limit(limit)).all()
    pairs = [(row_to_tx(tx), row_to_result(tx, sc)) for tx, sc in rows]
    gen = ReportGenerator(load_config(s), settings.reports_dir)
    path = gen.summary_report(pairs, "Fraud Analysis Summary")
    r = Report(kind="summary", path=path, created_by=user.username)
    s.add(r)
    audit(s, user, "report.summary", None, {"transactions": len(pairs)})
    s.commit()
    return {"id": r.id, "transactions": len(pairs)}
