"""Transaction filters shared by listing, export and summary report endpoints."""

from datetime import datetime
from typing import Optional

from fastapi import Query
from sqlalchemy import or_

from banksealer.models import RISK_LEVELS

from ..models import ScoreRow, TransactionRow


class TxFilters:
    def __init__(self, bank: Optional[str] = None, tx_type: Optional[str] = None,
                 risk: Optional[str] = Query(None, description="minimum risk level"),
                 status: Optional[str] = None, suspicious: Optional[bool] = None,
                 q: Optional[str] = Query(None, description="search tx id, customer, beneficiary, ASN, IP"),
                 user_id: Optional[str] = None, channel: Optional[str] = None,
                 date_from: Optional[datetime] = None, date_to: Optional[datetime] = None):
        self.bank, self.tx_type, self.risk, self.status = bank, tx_type, risk, status
        self.suspicious, self.q, self.user_id, self.channel = suspicious, q, user_id, channel
        self.date_from, self.date_to = date_from, date_to

    def apply(self, query):
        if self.bank:
            query = query.where(TransactionRow.bank_id == self.bank)
        if self.tx_type:
            query = query.where(TransactionRow.tx_type == self.tx_type)
        if self.risk and self.risk.upper() in RISK_LEVELS:
            query = query.where(ScoreRow.risk_level.in_(RISK_LEVELS[RISK_LEVELS.index(self.risk.upper()):]))
        if self.status:
            query = query.where(ScoreRow.status == self.status)
        if self.suspicious is not None:
            query = query.where(ScoreRow.suspicious.is_(self.suspicious))
        if self.user_id:
            query = query.where(TransactionRow.user_id == self.user_id)
        if self.channel:
            query = query.where(TransactionRow.channel == self.channel)
        if self.date_from:
            query = query.where(TransactionRow.ts >= self.date_from)
        if self.date_to:
            query = query.where(TransactionRow.ts <= self.date_to)
        if self.q:
            like = f"%{self.q.strip()}%"
            query = query.where(or_(TransactionRow.tx_id.ilike(like), TransactionRow.user_id.ilike(like),
                                    TransactionRow.iban.ilike(like), TransactionRow.cc_asn.ilike(like),
                                    TransactionRow.ip.ilike(like), TransactionRow.phone_number.ilike(like),
                                    TransactionRow.card_id.ilike(like)))
        return query
