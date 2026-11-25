"""AML unusual-activity indicators (BRD 8.1), kept separate from the fraud behavioural score.

Indicators are institution-configurable typology checks; they are alerting inputs for human
review, never a finding of money laundering.
"""
from __future__ import annotations

import math
from datetime import timedelta
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from banksealer.models import Transaction as ETx

from ..models import Customer, Transaction

LEVELS = ("NONE", "LOW", "ELEVATED", "HIGH")


class TxContext:
    """Recent-activity lookups for one transaction (lazy, cached)."""

    def __init__(self, db: Session, tenant_id: str, space: str, tx: ETx):
        self.db, self.tenant_id, self.space, self.tx = db, tenant_id, space, tx
        self._recent: Optional[list] = None

    def _base(self):
        return and_(Transaction.tenant_id == self.tenant_id, Transaction.space == self.space,
                    Transaction.customer_ref == self.tx.user_id, Transaction.tx_ref != self.tx.tx_id)

    def recent(self, hours: float = 24 * 31) -> list:
        if self._recent is None:
            since = self.tx.timestamp - timedelta(hours=24 * 31)
            self._recent = self.db.execute(
                select(Transaction.ts, Transaction.amount, Transaction.tx_type, Transaction.beneficiary)
                .where(self._base(), Transaction.ts >= since, Transaction.ts <= self.tx.timestamp)).all()
        since = self.tx.timestamp - timedelta(hours=hours)
        return [r for r in self._recent if r.ts >= since]

    def velocity(self, hours: float, tx_types: Optional[list] = None) -> int:
        rows = self.recent(hours)
        if tx_types:
            rows = [r for r in rows if r.tx_type in tx_types]
        own = 1 if (not tx_types or self.tx.tx_type in tx_types) else 0
        return len(rows) + own

    def new_beneficiary(self) -> bool:
        ben = self.tx.beneficiary
        if not ben:
            return False
        seen = self.db.execute(select(func.count(Transaction.id)).where(
            self._base(), Transaction.beneficiary == ben, Transaction.ts < self.tx.timestamp)).scalar()
        return not seen

    def shared_counterparty(self, days: int) -> int:
        ben = self.tx.beneficiary
        if not ben:
            return 0
        since = self.tx.timestamp - timedelta(days=days)
        others = self.db.execute(select(func.count(func.distinct(Transaction.customer_ref))).where(
            Transaction.tenant_id == self.tenant_id, Transaction.space == self.space,
            Transaction.beneficiary == ben, Transaction.customer_ref != self.tx.user_id,
            Transaction.ts >= since, Transaction.ts <= self.tx.timestamp)).scalar() or 0
        return int(others) + 1


def evaluate(ctx: TxContext, customer: Optional[Customer], cfg: dict, high_risk_countries: list) -> dict:
    """Return ``{"level", "score", "indicators": [...]}``."""
    tx = ctx.tx
    indicators: list[dict] = []
    if not cfg.get("enabled", True):
        return {"level": "NONE", "score": 0.0, "indicators": []}
    window = float(cfg.get("window_hours", 24))

    # Structuring: several amounts just below the reporting threshold within the window.
    threshold = float(cfg.get("reporting_threshold", 10000))
    band = float(cfg.get("structuring_band", 0.15))
    lo = threshold * (1 - band)
    if lo <= tx.amount < threshold:
        near = [r for r in ctx.recent(window) if lo <= r.amount < threshold]
        if len(near) + 1 >= int(cfg.get("structuring_count", 3)):
            indicators.append({"code": "STRUCTURING_PATTERN", "weight": 0.55,
                               "message": f"{len(near) + 1} transactions between {lo:,.0f} and {threshold:,.0f} "
                                          f"within {window:.0f}h (just below the reporting threshold)"})

    # Rapid movement of funds / velocity - relative to the customer's declared activity when known.
    count = ctx.velocity(window)
    velocity_limit = int(cfg.get("velocity_count", 8))
    if customer is not None and customer.expected_monthly_tx:
        expected_in_window = customer.expected_monthly_tx / 30.0 * (window / 24.0)
        velocity_limit = max(velocity_limit,
                             math.ceil(float(cfg.get("velocity_expected_multiplier", 4.0)) * expected_in_window))
    if count >= velocity_limit:
        total = sum(r.amount for r in ctx.recent(window)) + tx.amount
        indicators.append({"code": "RAPID_MOVEMENT_OF_FUNDS", "weight": 0.35,
                           "message": f"{count} transactions totalling {total:,.2f} within {window:.0f}h"})

    # Activity inconsistent with the customer's declared profile.
    if customer is not None and customer.expected_monthly_turnover:
        month = sum(r.amount for r in ctx.recent(24 * 30)) + tx.amount
        mult = float(cfg.get("turnover_multiplier", 2.5))
        limit = max(customer.expected_monthly_turnover * mult,
                    customer.expected_monthly_turnover + float(cfg.get("turnover_min_excess", 3000)))
        # customer-level breach: raised by the transaction that crosses the limit, not by every later one
        if month - tx.amount < limit <= month:
            indicators.append({"code": "TURNOVER_ABOVE_EXPECTED", "weight": 0.4,
                               "message": f"30-day turnover {month:,.2f} exceeds {mult:g}x the declared expected "
                                          f"monthly turnover {customer.expected_monthly_turnover:,.2f}"})

    # Shared / concentrated counterparty across customers.
    min_customers = int(cfg.get("shared_counterparty_customers", 3))
    # (only person-to-person transfers: many customers paying the same shop, ATM or employer is normal)
    shared = ctx.shared_counterparty(int(cfg.get("shared_counterparty_days", 7))) \
        if tx.tx_type == "bank_transfer" else 0
    if shared >= min_customers:
        indicators.append({"code": "SHARED_COUNTERPARTY", "weight": 0.45,
                           "message": f"Beneficiary received funds from {shared} different customers within "
                                      f"{cfg.get('shared_counterparty_days', 7)} days"})

    # Mule / pass-through: money received from several NEW senders leaves again quickly (individuals - businesses
    # receive from many clients and pay suppliers as a matter of course).
    individual = customer is None or customer.customer_type != "BUSINESS"
    if tx.tx_type in ("bank_transfer", "cash_withdrawal") and individual:
        hours = float(cfg.get("passthrough_hours", 48))
        rec = ctx.recent(hours)
        window_start = tx.timestamp - timedelta(hours=hours)
        known = {r.beneficiary for r in ctx.recent(24 * 31) if r.ts < window_start}
        inflow = sum(r.amount for r in rec if r.tx_type in ("incoming_transfer", "cash_deposit")
                     and r.beneficiary not in known)
        senders = {r.beneficiary for r in rec if r.tx_type == "incoming_transfer" and r.beneficiary
                   and r.beneficiary not in known}
        outflow = sum(r.amount for r in rec if r.tx_type in ("bank_transfer", "cash_withdrawal")) + tx.amount
        if inflow >= float(cfg.get("passthrough_min_inflow", 3000)) and \
                outflow >= float(cfg.get("passthrough_ratio", 0.8)) * inflow and \
                len(senders) >= int(cfg.get("passthrough_min_senders", 2)):
            indicators.append({"code": "RAPID_PASS_THROUGH", "weight": 0.5,
                               "message": f"{inflow:,.2f} received from {len(senders)} senders within {hours:.0f}h "
                                          f"and {outflow:,.2f} sent out again (mule / pass-through pattern)"})
    if tx.tx_type == "incoming_transfer" and individual:
        days = int(cfg.get("originators_days", 7))
        start = tx.timestamp - timedelta(days=days)
        all_recent = ctx.recent(24 * 31)
        known = {r.beneficiary for r in all_recent if r.ts < start}
        senders = {r.beneficiary for r in all_recent if r.ts >= start and r.tx_type == "incoming_transfer"
                   and r.beneficiary and r.beneficiary not in known}
        if tx.beneficiary not in known:
            senders.add(tx.beneficiary)
        if len(senders) >= int(cfg.get("many_originators", 3)):
            indicators.append({"code": "MANY_ORIGINATORS", "weight": 0.4,
                               "message": f"Funds received from {len(senders)} new senders within {days} days"})

    # High-risk jurisdiction.
    hrc = {c.upper() for c in high_risk_countries or []}
    hit = sorted({c for c in (tx.counterparty_country, tx.ip_country) if c and c.upper() in hrc})
    if hit:
        indicators.append({"code": "HIGH_RISK_JURISDICTION", "weight": 0.3,
                           "message": f"Involves high-risk jurisdiction(s) {', '.join(hit)}"})

    # Institution-classified high-risk customer with a material amount.
    if customer is not None and customer.risk_category == "HIGH" and \
            tx.amount >= float(cfg.get("high_risk_customer_amount", 5000)):
        indicators.append({"code": "HIGH_RISK_CUSTOMER_MATERIAL_AMOUNT", "weight": 0.25,
                           "message": f"Customer classified HIGH risk by the institution; amount {tx.amount:,.2f}"})

    score = 1.0
    for ind in indicators:
        score *= (1 - ind["weight"])
    score = round(1 - score, 3)
    level = "NONE" if not indicators else "HIGH" if score >= 0.6 else "ELEVATED" if score >= 0.35 else "LOW"
    return {"level": level, "score": score, "indicators": indicators}
