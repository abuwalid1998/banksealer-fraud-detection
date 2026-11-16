"""ORM models: configuration database + operational data."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def now() -> datetime:
    """Naive UTC timestamp (stored without time zone)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------ configuration
class Setting(Base):
    """Global engine/threshold settings, one row per section (thresholds, engine, local, temporal, global, app)."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    updated_by: Mapped[str | None] = mapped_column(String(64))


class Bank(Base):
    """A bank/company with its threshold overrides."""
    __tablename__ = "banks"
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    thresholds: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ChannelConfig(Base):
    """A transaction channel: connector type/options, field mapping and preparation rules."""
    __tablename__ = "channels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    type: Mapped[str] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    field_mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    preparation: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ChannelStatus(Base):
    """Runtime status reported by the worker."""
    __tablename__ = "channel_status"
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="stopped")
    received: Mapped[int] = mapped_column(Integer, default=0)
    accepted: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    history: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    heartbeat: Mapped[datetime | None] = mapped_column(DateTime)


# ---------------------------------------------------------- operational
class TransactionRow(Base):
    __tablename__ = "transactions"
    tx_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    bank_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str | None] = mapped_column(String(120))
    user_id: Mapped[str] = mapped_column(String(80), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    tx_type: Mapped[str] = mapped_column(String(32))
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="EUR")
    iban: Mapped[str | None] = mapped_column(String(64))
    iban_cc: Mapped[str | None] = mapped_column(String(4))
    ip: Mapped[str | None] = mapped_column(String(64))
    cc_asn: Mapped[str | None] = mapped_column(String(64))
    session_id: Mapped[str | None] = mapped_column(String(80))
    card_id: Mapped[str | None] = mapped_column(String(64))
    phone_number: Mapped[str | None] = mapped_column(String(64))
    operator: Mapped[str | None] = mapped_column(String(64))
    label: Mapped[int | None] = mapped_column(Integer)
    scenario: Mapped[str | None] = mapped_column(String(64))
    is_training: Mapped[bool] = mapped_column(Boolean, default=False)


class ScoreRow(Base):
    __tablename__ = "scores"
    tx_id: Mapped[str] = mapped_column(ForeignKey("transactions.tx_id", ondelete="CASCADE"), primary_key=True)
    local: Mapped[float] = mapped_column(Float)
    temporal: Mapped[float] = mapped_column(Float)
    global_: Mapped[float] = mapped_column("global", Float)
    combined: Mapped[float] = mapped_column(Float, index=True)
    risk_level: Mapped[str] = mapped_column(String(16), index=True)
    suspicious: Mapped[bool] = mapped_column(Boolean, index=True)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    rule_reasons: Mapped[list] = mapped_column(JSON, default=list)
    explanation: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    cluster: Mapped[int | None] = mapped_column(Integer)
    undertrained: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    analyst_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    scored_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class CustomerProfile(Base):
    """Learned profile summary of a customer, published by the worker."""
    __tablename__ = "customer_profiles"
    user_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    bank_id: Mapped[str | None] = mapped_column(String(64))
    cluster: Mapped[int | None] = mapped_column(Integer)
    global_score: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class PreparationError(Base):
    __tablename__ = "preparation_errors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(120), index=True)
    message: Mapped[str] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tx_id: Mapped[str | None] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="transaction")
    path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    created_by: Mapped[str | None] = mapped_column(String(64))


class EngineStatus(Base):
    __tablename__ = "engine_status"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(Text, default="starting")
    trained: Mapped[bool] = mapped_column(Boolean, default=False)
    training_size: Mapped[int] = mapped_column(Integer, default=0)
    buffered: Mapped[int] = mapped_column(Integer, default=0)
    customers: Mapped[int] = mapped_column(Integer, default=0)
    peer_groups: Mapped[int] = mapped_column(Integer, default=0)
    scored_total: Mapped[int] = mapped_column(Integer, default=0)
    last_fit: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat: Mapped[datetime | None] = mapped_column(DateTime)
    config_version: Mapped[str | None] = mapped_column(String(64))


class Command(Base):
    """Work requested by the API and executed by the worker (retrain, re-evaluate, ...)."""
    __tablename__ = "commands"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result: Mapped[dict | None] = mapped_column(JSON)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class ModelBlob(Base):
    """Serialized engine (so a restarted worker or a replica resumes with trained profiles)."""
    __tablename__ = "model_blobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ------------------------------------------------------------- security
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="analyst")   # admin | analyst | viewer
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_login: Mapped[datetime | None] = mapped_column(DateTime)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[dict | None] = mapped_column(JSON)


Index("ix_tx_bank_ts", TransactionRow.bank_id, TransactionRow.ts)
