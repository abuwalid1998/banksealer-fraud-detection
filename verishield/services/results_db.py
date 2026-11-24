"""Fraud-engine results database: writes every scored transaction to a database the bank chooses.

The IT Admin configures it in *Integrations & Channels* as the connector type ``db_results`` (any SQLAlchemy
URL: PostgreSQL, SQLite, SQL Server / Oracle when their drivers are installed). A background writer copies
each new decision - scores, risk level, AML signal, reasons, model and policy versions - into the configured
table, so the bank's core systems, data warehouse or BI tools can read the results directly.

Writing is decoupled from the decision path: a slow or unavailable results database never delays a
decision; the writer resumes from its cursor when the database is back.
"""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import (Column, DateTime, Float, Integer, MetaData, String, Table, Text, create_engine, func, select)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..models import Decision, Transaction, utcnow

COLUMNS = ["decision_id", "institution", "workspace", "transaction_id", "customer_id", "transaction_time",
           "transaction_type", "amount", "currency", "channel", "beneficiary", "beneficiary_country", "mode",
           "decision", "fraud_score", "local_score", "temporal_score", "global_score", "risk_level",
           "aml_signal_level", "aml_score", "reason_codes", "explanation", "policy_version", "model_version",
           "latency_ms", "issued_at", "written_at"]


def normalise_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def make_engine(url: str) -> Engine:
    return create_engine(normalise_url(url), pool_pre_ping=True, pool_size=2, max_overflow=2) \
        if not url.startswith("sqlite") else create_engine(url)


def table_for(name: str, metadata: Optional[MetaData] = None) -> Table:
    schema, _, tname = name.rpartition(".")
    md = metadata or MetaData()
    return Table(tname, md,
                 Column("decision_id", String(40), primary_key=True),
                 Column("institution", String(16)), Column("workspace", String(8)),
                 Column("transaction_id", String(80), index=True), Column("customer_id", String(64), index=True),
                 Column("transaction_time", DateTime), Column("transaction_type", String(24)),
                 Column("amount", Float), Column("currency", String(3)), Column("channel", String(40)),
                 Column("beneficiary", String(80)), Column("beneficiary_country", String(2)),
                 Column("mode", String(12)), Column("decision", String(14), index=True),
                 Column("fraud_score", Float), Column("local_score", Float), Column("temporal_score", Float),
                 Column("global_score", Float), Column("risk_level", String(10)),
                 Column("aml_signal_level", String(10)), Column("aml_score", Float),
                 Column("reason_codes", Text), Column("explanation", Text),
                 Column("policy_version", String(40)), Column("model_version", String(40)),
                 Column("latency_ms", Float), Column("issued_at", DateTime, index=True),
                 Column("written_at", DateTime), schema=schema or None)


def test(url: str, table: str, create: bool = True) -> tuple[bool, str]:
    eng = make_engine(url)
    try:
        t = table_for(table)
        if create:
            t.metadata.create_all(eng)
        with eng.connect() as conn:
            n = conn.execute(select(func.count()).select_from(t)).scalar()
        return True, f"connected to {eng.dialect.name}; table {table} is ready ({n:,} result rows)"
    finally:
        eng.dispose()


def _row(dec: Decision, tx: Transaction) -> dict:
    return {"decision_id": dec.id, "institution": dec.tenant_id, "workspace": dec.space,
            "transaction_id": dec.tx_ref, "customer_id": dec.customer_ref, "transaction_time": tx.ts,
            "transaction_type": tx.tx_type, "amount": tx.amount, "currency": tx.currency, "channel": tx.channel,
            "beneficiary": tx.beneficiary, "beneficiary_country": tx.beneficiary_country, "mode": dec.mode,
            "decision": dec.decision, "fraud_score": dec.fraud_score, "local_score": dec.local_score,
            "temporal_score": dec.temporal_score, "global_score": dec.global_score, "risk_level": dec.risk_level,
            "aml_signal_level": dec.aml_signal_level, "aml_score": dec.aml_score,
            "reason_codes": json.dumps(dec.reason_codes or []), "explanation": (dec.explanation or "")[:2000],
            "policy_version": dec.policy_version, "model_version": dec.fraud_model_version,
            "latency_ms": dec.latency_ms, "issued_at": dec.issued_at, "written_at": utcnow()}


def current_cursor(db: Session, tenant_id: str, space: str) -> int:
    return int(db.execute(select(func.max(Transaction.id)).where(Transaction.tenant_id == tenant_id,
                                                                 Transaction.space == space)).scalar() or 0)


def write_new(db: Session, eng: Engine, table: str, tenant_id: str, space: str, cursor: int,
              batch: int = 500) -> tuple[int, int]:
    """Copy decisions of transactions with id > cursor; returns ``(written, new_cursor)``."""
    rows = db.execute(select(Transaction, Decision).join(
        Decision, (Decision.tenant_id == Transaction.tenant_id) & (Decision.space == Transaction.space)
        & (Decision.id == Transaction.decision_id)).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.id > cursor)
        .order_by(Transaction.id).limit(batch)).all()
    if not rows:
        return 0, cursor
    t = table_for(table)
    payload = [_row(dec, tx) for tx, dec in rows]
    ids = [r["decision_id"] for r in payload]
    with eng.begin() as conn:
        existing = {r[0] for r in conn.execute(select(t.c.decision_id).where(t.c.decision_id.in_(ids)))}
        fresh = [r for r in payload if r["decision_id"] not in existing]
        if fresh:
            conn.execute(t.insert(), fresh)
    return len(fresh), rows[-1][0].id


def preview(url: str, table: str, limit: int = 10) -> list[dict]:
    eng = make_engine(url)
    try:
        t = table_for(table)
        with eng.connect() as conn:
            rows = conn.execute(select(t).order_by(t.c.issued_at.desc()).limit(limit)).mappings().all()
        return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(r).items()} for r in rows]
    finally:
        eng.dispose()


__all__ = ["COLUMNS", "test", "write_new", "preview", "make_engine", "current_cursor", "Integer"]
