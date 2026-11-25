"""Cheque fraud: duplicate presentment, stolen cheque books, bounced-cheque drawers, post-dated / stale cheques
and cheque kiting.

Cheques remain widely used in Palestine and the region. The behavioural engine profiles cheque deposits (drawer
account, drawer bank, branch, amount) and the customer's own cheques (payee, branch, amount); this module adds the
cheque-specific checks that a profile cannot see:

* **Duplicate presentment** - the same drawer account + cheque number already deposited (here or by another
  customer): a copied or re-presented cheque.
* **Sequence anomaly** - the customer's own cheque number lies far outside the range of their cheque book(s):
  typical of a stolen cheque book.
* **Bounced-cheque drawer** - the drawer account has returned cheques in the last 12 months.
* **Post-dated / stale** - presented before the date written on it, or after the validity period.
* **Kiting** - cash withdrawn or money transferred out within a few days of depositing cheques that have not cleared
  yet, for most of the deposited amount.

Returned cheques are reported by the clearing system as ``record_type: "cheque_return"`` events
(``POST /api/v1/transactions/events``) and feed the drawer checks and the customer risk assessment.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import ChequeReturn, Transaction, utcnow

RETURN_REASONS = {"INSUFFICIENT_FUNDS": "Insufficient funds", "SIGNATURE": "Signature mismatch / missing",
                  "STOPPED": "Payment stopped by the drawer", "ACCOUNT_CLOSED": "Account closed",
                  "ALTERED": "Cheque altered", "POST_DATED": "Post-dated", "STALE": "Out of date", "OTHER": "Other"}


def _num(value) -> Optional[int]:
    digits = re.sub(r"\D", "", str(value or ""))
    return int(digits) if digits else None


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def returned_count(db: Session, tenant_id: str, space: str, *, drawer_account: str = "", customer_ref: str = "",
                   days: int = 365) -> int:
    q = select(func.count()).select_from(ChequeReturn).where(
        ChequeReturn.tenant_id == tenant_id, ChequeReturn.space == space,
        ChequeReturn.returned_at >= utcnow() - timedelta(days=days))
    if drawer_account:
        q = q.where(ChequeReturn.drawer_account == drawer_account)
    if customer_ref:
        q = q.where(ChequeReturn.customer_ref == customer_ref, ChequeReturn.role == "DRAWER")
    return int(db.execute(q).scalar() or 0)


def signals(db: Session, tenant_id: str, space: str, etx, tx_row, c: dict) -> list[dict]:
    """Signal hits for one transaction (cheques, and outflows that may be kiting)."""
    hits: list[dict] = []
    now = etx.timestamp

    def hit(hid: str, name: str, action: str, code: str, message: str) -> None:
        hits.append({"id": hid, "name": name, "kind": "signal", "action": action, "reason_code": code,
                     "message": message})

    if etx.tx_type in ("cheque_deposit", "cheque_payment"):
        number = etx.cheque_number or ""
        # ---- duplicate presentment
        if etx.tx_type == "cheque_deposit":
            prior = db.execute(select(Transaction.customer_ref, Transaction.ts, Transaction.details).where(
                Transaction.tenant_id == tenant_id, Transaction.space == space,
                Transaction.tx_type == "cheque_deposit", Transaction.beneficiary == etx.beneficiary,
                Transaction.tx_ref != etx.tx_id).order_by(Transaction.ts.desc()).limit(200)).all()
            for cust, ts, other in prior:
                if str((other or {}).get("cheque_number") or "") == str(number):
                    who = "the same customer" if cust == etx.user_id else f"customer {cust}"
                    hit("CHEQUE-DUPLICATE", "Duplicate cheque presentment", c["cheque_duplicate_action"],
                        "DUPLICATE_CHEQUE", f"Cheque {number} on account {etx.beneficiary} was already deposited by "
                                            f"{who} on {ts:%d %b %Y}")
                    break
        # ---- own cheque book sequence
        if etx.tx_type == "cheque_payment":
            mine = [n for n in (_num((d or {}).get("cheque_number")) for (d,) in db.execute(
                select(Transaction.details).where(Transaction.tenant_id == tenant_id, Transaction.space == space,
                                                  Transaction.customer_ref == etx.user_id,
                                                  Transaction.tx_type == "cheque_payment",
                                                  Transaction.tx_ref != etx.tx_id)
                .order_by(Transaction.ts.desc()).limit(60)).all()) if n is not None]
            n = _num(number)
            gap = int(c["cheque_sequence_gap"])
            if n is not None and len(mine) >= 3 and min(abs(n - m) for m in mine) > gap:
                hit("CHEQUE-SEQUENCE", "Cheque book sequence anomaly", c["cheque_sequence_action"],
                    "CHEQUE_SEQUENCE_ANOMALY", f"Cheque number {number} is far from the customer's recent cheques "
                                               f"({min(mine)}-{max(mine)}): possible stolen cheque book")
            if n is not None and n in mine:
                hit("CHEQUE-REUSED", "Cheque number already paid", c["cheque_duplicate_action"], "DUPLICATE_CHEQUE",
                    f"Cheque number {number} of this customer was already paid")
        # ---- bounced-cheque drawer
        drawer = etx.beneficiary if etx.tx_type == "cheque_deposit" else ""
        returned = returned_count(db, tenant_id, space, drawer_account=drawer) if drawer else \
            returned_count(db, tenant_id, space, customer_ref=etx.user_id)
        if returned >= int(c["returned_cheques_review"]):
            action = "HOLD" if returned >= int(c["returned_cheques_hold"]) else "REVIEW"
            whose = f"drawer account {drawer}" if drawer else "this customer's account"
            hit("CHEQUE-RETURNED", "Returned cheques history", action, "RETURNED_CHEQUE_DRAWER",
                f"{returned} cheque(s) drawn on {whose} were returned unpaid in the last 12 months")
        # ---- post-dated / stale
        written = _parse_date(etx.cheque_date)
        if written:
            if written > now.date():
                hit("CHEQUE-POSTDATED", "Post-dated cheque", "REVIEW", "POST_DATED_OR_STALE_CHEQUE",
                    f"Cheque dated {written:%d %b %Y} presented on {now:%d %b %Y} (post-dated)")
            elif written < (now - timedelta(days=30 * int(c["cheque_stale_months"]))).date():
                hit("CHEQUE-STALE", "Stale cheque", "REVIEW", "POST_DATED_OR_STALE_CHEQUE",
                    f"Cheque dated {written:%d %b %Y} is older than {c['cheque_stale_months']} months")

    # ---- kiting: money out soon after depositing uncleared cheques from new drawers
    if etx.is_outflow and etx.tx_type in ("cash_withdrawal", "bank_transfer", "cheque_payment"):
        days = int(c["cheque_kiting_days"])
        start = now - timedelta(days=days)
        base = (Transaction.tenant_id == tenant_id) & (Transaction.space == space) & \
            (Transaction.customer_ref == etx.user_id)
        recent = db.execute(select(Transaction.beneficiary, Transaction.amount).where(
            base, Transaction.tx_type == "cheque_deposit", Transaction.ts >= start, Transaction.ts <= now)).all()
        if recent:
            known = {b for (b,) in db.execute(select(Transaction.beneficiary).where(
                base, Transaction.tx_type == "cheque_deposit", Transaction.ts < start,
                Transaction.ts >= now - timedelta(days=180)).distinct()).all()}
            new_dep = sum(float(a) for b, a in recent if b not in known)
            if new_dep >= 3000:
                out_types = ("cash_withdrawal", "bank_transfer", "cheque_payment")
                out = float(db.execute(select(func.coalesce(func.sum(Transaction.amount), 0.0)).where(
                    base, Transaction.tx_type.in_(out_types), Transaction.ts >= start,
                    Transaction.tx_ref != etx.tx_id)).scalar() or 0.0) + float(etx.amount)
                hist, first = db.execute(select(func.coalesce(func.sum(Transaction.amount), 0.0),
                                                func.min(Transaction.ts)).where(
                    base, Transaction.tx_type.in_(out_types), Transaction.ts < start,
                    Transaction.ts >= start - timedelta(days=60))).one()
                span = max(7.0, min(60.0, (start - first).total_seconds() / 86400)) if first else 60.0
                usual = float(hist or 0.0) / span * days
                # well above the customer's normal outflow, by about the amount of the new uncleared cheques
                excess = out - 1.5 * usual
                if out >= 2 * usual and excess >= float(c["cheque_kiting_share"]) * new_dep:
                    hit("CHEQUE-KITING", "Cheque kiting", c["cheque_kiting_action"], "CHEQUE_KITING",
                        f"{out:,.2f} {etx.currency} taken out within {days} days (usual {usual:,.0f}) after depositing "
                        f"{new_dep:,.2f} in uncleared cheques from new drawers")
    return hits


def record_return(db: Session, tenant_id: str, space: str, payload: dict) -> ChequeReturn:
    """A returned (bounced) cheque reported by the clearing system."""
    reason = str(payload.get("reason") or payload.get("return_reason") or "OTHER").upper().replace(" ", "_")
    if reason not in RETURN_REASONS:
        reason = "OTHER"
    tx_ref = str(payload.get("transaction_id") or payload.get("tx_ref") or "")
    tx = db.execute(select(Transaction).where(Transaction.tenant_id == tenant_id, Transaction.space == space,
                                              Transaction.tx_ref == tx_ref)).scalar() if tx_ref else None
    drawer = str(payload.get("drawer_account") or (tx.beneficiary if tx is not None and tx.tx_type == "cheque_deposit"
                                                   else "") or "").replace(" ", "").upper()
    role = str(payload.get("role") or ("DEPOSITOR" if tx is not None and tx.tx_type == "cheque_deposit" else "DRAWER"))
    ret = ChequeReturn(tenant_id=tenant_id, space=space, tx_ref=tx_ref,
                       customer_ref=str(payload.get("customer_id") or (tx.customer_ref if tx is not None else "")),
                       role=role.upper()[:10], drawer_account=drawer,
                       cheque_number=str(payload.get("cheque_number") or ((tx.details or {}).get("cheque_number")
                                                                          if tx is not None else "") or ""),
                       amount=float(payload.get("amount") or (tx.amount if tx is not None else 0) or 0),
                       reason=reason)
    if payload.get("timestamp"):
        try:
            ret.returned_at = datetime.fromisoformat(str(payload["timestamp"]).replace("Z", "")[:19])
        except ValueError:
            pass
    db.add(ret)
    if tx is not None:
        tx.details = {**(tx.details or {}), "returned": reason}
    db.commit()
    return ret


def summary(db: Session, tenant_id: str, space: str, days: int = 90) -> dict:
    since = utcnow() - timedelta(days=days)
    by_type = dict(db.execute(select(Transaction.tx_type, func.count()).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space,
        Transaction.tx_type.in_(("cheque_deposit", "cheque_payment")), Transaction.ts >= since)
        .group_by(Transaction.tx_type)).all())
    returns = db.execute(select(ChequeReturn).where(ChequeReturn.tenant_id == tenant_id, ChequeReturn.space == space,
                                                    ChequeReturn.returned_at >= since)
                         .order_by(ChequeReturn.returned_at.desc())).scalars().all()
    reasons: dict[str, int] = {}
    for r in returns:
        reasons[r.reason] = reasons.get(r.reason, 0) + 1
    drawers: dict[str, int] = {}
    for r in returns:
        if r.drawer_account:
            drawers[r.drawer_account] = drawers.get(r.drawer_account, 0) + 1
    return {"deposits": by_type.get("cheque_deposit", 0), "payments": by_type.get("cheque_payment", 0),
            "returned": len(returns), "reasons": reasons,
            "top_drawers": sorted(({"account": k, "returned": v} for k, v in drawers.items()),
                                  key=lambda x: -x["returned"])[:15],
            "recent_returns": [{"id": r.id, "tx_ref": r.tx_ref, "customer_ref": r.customer_ref, "role": r.role,
                                "drawer_account": r.drawer_account, "cheque_number": r.cheque_number,
                                "amount": r.amount, "reason": RETURN_REASONS.get(r.reason, r.reason),
                                "returned_at": r.returned_at} for r in returns[:50]]}


__all__ = ["signals", "record_return", "returned_count", "summary", "RETURN_REASONS"]
