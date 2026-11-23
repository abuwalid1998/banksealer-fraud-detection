"""VeriShield relational data model (BRD section 16).

Every business record is scoped by ``tenant_id``; records that hold operational
or synthetic data are additionally scoped by ``space`` (``LIVE`` or ``DEMO``) so
Demo Mode has its own namespace that a demo reset can clear without touching
production data.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12].upper()}" if prefix else str(uuid.uuid4())


ROLES = ("IT_ADMIN", "AML_MANAGER", "AML_EMPLOYEE", "CHIEF_AML")
ROLE_LABELS = {"IT_ADMIN": "IT Admin", "AML_MANAGER": "AML Manager", "AML_EMPLOYEE": "AML Employee",
               "CHIEF_AML": "Chief AML / Compliance"}
SPACES = ("LIVE", "DEMO")


# --------------------------------------------------------------------------- tenant & identity
class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)          # institution code, e.g. BOP
    legal_name: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    base_currency: Mapped[str] = mapped_column(String(3), default="USD")
    locale: Mapped[str] = mapped_column(String(16), default="en-GB")
    languages: Mapped[list] = mapped_column(JSON, default=lambda: ["en", "ar"])
    region: Mapped[str] = mapped_column(String(64), default="")
    demo_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)               # security, retention, governance
    branding: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "username", name="uq_user_tenant_username"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    username: Mapped[str] = mapped_column(String(5))
    first_name: Mapped[str] = mapped_column(String(80), default="")
    last_name: Mapped[str] = mapped_column(String(80), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    email_code_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    role: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(12), default="INVITED")      # INVITED ACTIVE LOCKED DISABLED
    password_hash: Mapped[str] = mapped_column(String(256))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    temp_password_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    mfa_secret: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    extra_permissions: Mapped[list] = mapped_column(JSON, default=list)
    is_bootstrap: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    reset_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    reset_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    @property
    def full_name(self) -> str:
        name = f"{self.first_name} {self.last_name}".strip()
        return name or f"User {self.username}"


class UserSession(Base):
    __tablename__ = "user_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(16))
    mfa_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(200), default="")
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class ApiClient(Base):
    """System-to-system credential (bank payment switch, MCP server, ...)."""
    __tablename__ = "api_clients"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: new_id("API-"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    space: Mapped[str] = mapped_column(String(8), default="LIVE")      # fixed at creation; never follows the UI toggle
    name: Mapped[str] = mapped_column(String(120))
    key_prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=600)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SecretRecord(Base):
    """Encrypted local vault entry; connectors only ever hold a reference to it."""
    __tablename__ = "secrets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: new_id("SEC-"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    ciphertext: Mapped[str] = mapped_column(Text)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    rotated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# --------------------------------------------------------------------------- integrations
class Connector(Base):
    __tablename__ = "connectors"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: new_id("CON-"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    space: Mapped[str] = mapped_column(String(8), default="LIVE")
    name: Mapped[str] = mapped_column(String(120))
    type: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(12), default="INBOUND")
    environment: Mapped[str] = mapped_column(String(8), default="DEV")
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    ingest_mode: Mapped[str] = mapped_column(String(16), default="POST_EVENT")   # POST_EVENT | PRE_AUTH | HISTORY
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    auth_method: Mapped[str] = mapped_column(String(32), default="NONE")
    secret_ref: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    tls_verify: Mapped[bool] = mapped_column(Boolean, default=True)
    field_mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    schedule: Mapped[dict] = mapped_column(JSON, default=dict)
    allowed_ips: Mapped[list] = mapped_column(JSON, default=list)
    data_scope: Mapped[str] = mapped_column(String(200), default="")
    permission: Mapped[str] = mapped_column(String(12), default="READ_ONLY")
    owner: Mapped[str] = mapped_column(String(120), default="")
    credential_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_test_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_test_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str] = mapped_column(Text, default="")
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_event_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    counters: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ConnectorLog(Base):
    __tablename__ = "connector_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(16), index=True)
    connector_id: Mapped[str] = mapped_column(String(36), index=True)
    level: Mapped[str] = mapped_column(String(8), default="INFO")
    message: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(80), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DeadLetterEvent(Base):
    __tablename__ = "dead_letters"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: new_id("DLQ-"))
    tenant_id: Mapped[str] = mapped_column(String(16), index=True)
    space: Mapped[str] = mapped_column(String(8), default="LIVE")
    source: Mapped[str] = mapped_column(String(120))
    connector_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(24), default="VALIDATION")   # VALIDATION DUPLICATE PROCESSING
    status: Mapped[str] = mapped_column(String(12), default="PENDING")       # PENDING REPLAYED DISCARDED
    retries: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    replayed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# --------------------------------------------------------------------------- customers & models
class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("tenant_id", "space", "customer_ref", name="uq_customer"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(16), index=True)
    space: Mapped[str] = mapped_column(String(8), default="LIVE")
    customer_ref: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(160), default="")
    customer_type: Mapped[str] = mapped_column(String(16), default="INDIVIDUAL")   # INDIVIDUAL | BUSINESS
    business_activity: Mapped[str] = mapped_column(String(120), default="")
    risk_category: Mapped[str] = mapped_column(String(12), default="LOW")        # institution CDD classification
    kyc_review_status: Mapped[str] = mapped_column(String(20), default="CURRENT")
    country: Mapped[str] = mapped_column(String(2), default="")
    tenure_months: Mapped[int] = mapped_column(Integer, default=0)
    products: Mapped[list] = mapped_column(JSON, default=list)
    expected_monthly_turnover: Mapped[float] = mapped_column(Float, default=0.0)
    expected_monthly_tx: Mapped[float] = mapped_column(Float, default=0.0)
    account_purpose: Mapped[str] = mapped_column(String(160), default="")
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    segment_key: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    segment_label: Mapped[str] = mapped_column(String(160), default="")
    segment_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    segment_basis: Mapped[str] = mapped_column(String(200), default="")
    segment_model: Mapped[str] = mapped_column(String(40), default="")
    segment_assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ModelVersion(Base):
    __tablename__ = "model_versions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)              # e.g. BOP-FRAUD-03
    tenant_id: Mapped[str] = mapped_column(String(16), index=True)
    space: Mapped[str] = mapped_column(String(8), default="LIVE")
    kind: Mapped[str] = mapped_column(String(16))                            # FRAUD | SEGMENTATION
    seq: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(12), default="TRAINED")       # TRAINED ACTIVE SHADOW RETIRED FAILED
    artifact_path: Mapped[str] = mapped_column(String(400), default="")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    features: Mapped[list] = mapped_column(JSON, default=list)
    training_size: Mapped[int] = mapped_column(Integer, default=0)
    training_from: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    training_to: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    trained_by: Mapped[str] = mapped_column(String(80), default="system")
    trained_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    approved_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class CustomerSegment(Base):
    __tablename__ = "customer_segments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(16), index=True)
    space: Mapped[str] = mapped_column(String(8), default="LIVE")
    model_version: Mapped[str] = mapped_column(String(40), index=True)
    segment_key: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(160))
    size: Mapped[int] = mapped_column(Integer, default=0)
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    description: Mapped[str] = mapped_column(Text, default="")


# --------------------------------------------------------------------------- transactions & decisions
class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("tenant_id", "space", "tx_ref", name="uq_tx"),
                      Index("ix_tx_tenant_customer_ts", "tenant_id", "space", "customer_ref", "ts"),
                      Index("ix_tx_tenant_ts", "tenant_id", "space", "ts"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(16))
    space: Mapped[str] = mapped_column(String(8), default="LIVE")
    tx_ref: Mapped[str] = mapped_column(String(80))
    customer_ref: Mapped[str] = mapped_column(String(64))
    account_ref: Mapped[str] = mapped_column(String(64), default="")
    ts: Mapped[datetime] = mapped_column(DateTime)
    tx_type: Mapped[str] = mapped_column(String(24))
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="")
    channel: Mapped[str] = mapped_column(String(40), default="")
    source: Mapped[str] = mapped_column(String(120), default="")
    mode: Mapped[str] = mapped_column(String(12), default="POST_EVENT")      # PRE_AUTH | POST_EVENT | HISTORY
    beneficiary: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    beneficiary_country: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    cc_asn: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    session_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    device_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)               # iban / phone / card / operator
    label: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    scenario: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    execution_state: Mapped[str] = mapped_column(String(20), default="RECEIVED")
    decision_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Decision(Base):
    """Immutable record of one operational risk decision."""
    __tablename__ = "decisions"
    __table_args__ = (UniqueConstraint("tenant_id", "space", "idempotency_key", name="uq_decision_idem"),
                      Index("ix_decision_tenant_issued", "tenant_id", "space", "issued_at"))
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(16))
    space: Mapped[str] = mapped_column(String(8), default="LIVE")
    tx_ref: Mapped[str] = mapped_column(String(80), index=True)
    customer_ref: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    mode: Mapped[str] = mapped_column(String(12))
    decision: Mapped[str] = mapped_column(String(14))
    execution_required: Mapped[bool] = mapped_column(Boolean, default=False)
    fraud_score: Mapped[float] = mapped_column(Float, default=0.0)
    local_score: Mapped[float] = mapped_column(Float, default=0.0)
    temporal_score: Mapped[float] = mapped_column(Float, default=0.0)
    global_score: Mapped[float] = mapped_column(Float, default=0.0)
    risk_level: Mapped[str] = mapped_column(String(10), default="LOW")
    aml_signal_level: Mapped[str] = mapped_column(String(10), default="NONE")
    aml_score: Mapped[float] = mapped_column(Float, default=0.0)
    customer_risk_context: Mapped[str] = mapped_column(String(10), default="LOW")
    reason_codes: Mapped[list] = mapped_column(JSON, default=list)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    rule_hits: Mapped[list] = mapped_column(JSON, default=list)
    aml_indicators: Mapped[list] = mapped_column(JSON, default=list)
    explanation: Mapped[str] = mapped_column(Text, default="")
    undertrained: Mapped[bool] = mapped_column(Boolean, default=False)
    peer_group: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    policy_version: Mapped[str] = mapped_column(String(40), default="")
    fraud_model_version: Mapped[str] = mapped_column(String(40), default="")
    shadow: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    correlation_id: Mapped[str] = mapped_column(String(40))
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    caller: Mapped[str] = mapped_column(String(120), default="")


class ExecutionStatusEvent(Base):
    __tablename__ = "execution_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(16), index=True)
    space: Mapped[str] = mapped_column(String(8), default="LIVE")
    tx_ref: Mapped[str] = mapped_column(String(80), index=True)
    state: Mapped[str] = mapped_column(String(24))
    source: Mapped[str] = mapped_column(String(40), default="BANK")
    reference: Mapped[str] = mapped_column(String(120), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------- alerts & cases
class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alert_tenant_status", "tenant_id", "space", "status"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(16))
    space: Mapped[str] = mapped_column(String(8), default="LIVE")
    kind: Mapped[str] = mapped_column(String(16), default="TRANSACTION")     # TRANSACTION CUSTOMER MANUAL SCREENING
    tx_ref: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    customer_ref: Mapped[str] = mapped_column(String(64), index=True)
    decision_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    decision: Mapped[str] = mapped_column(String(14), default="REVIEW")
    timing: Mapped[str] = mapped_column(String(12), default="PRE_EVENT")    # PRE_EVENT | POST_EVENT
    severity: Mapped[str] = mapped_column(String(10), default="MEDIUM")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    aml_signal_level: Mapped[str] = mapped_column(String(10), default="NONE")
    reason_codes: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="OPEN")         # OPEN TRIAGED IN_CASE CLOSED
    related_alert_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    case_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    policy_version: Mapped[str] = mapped_column(String(40), default="")
    model_version: Mapped[str] = mapped_column(String(40), default="")
    closed_reason: Mapped[str] = mapped_column(Text, default="")
    closed_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (Index("ix_case_tenant_status", "tenant_id", "space", "status"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(16))
    space: Mapped[str] = mapped_column(String(8), default="LIVE")
    title: Mapped[str] = mapped_column(String(200))
    case_type: Mapped[str] = mapped_column(String(16), default="TRANSACTION")   # TRANSACTION CUSTOMER REFERRAL
    status: Mapped[str] = mapped_column(String(28), default="NEW")
    priority: Mapped[str] = mapped_column(String(10), default="MEDIUM")         # LOW MEDIUM HIGH URGENT
    classification: Mapped[str] = mapped_column(String(24), default="AML_SUSPICION")  # AML_SUSPICION FRAUD_SUSPICION
    owner_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    assignee_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    purpose: Mapped[str] = mapped_column(Text, default="")
    questions: Mapped[list] = mapped_column(JSON, default=list)
    window_from: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    window_to: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    checklist: Mapped[list] = mapped_column(JSON, default=list)
    recommendation: Mapped[dict] = mapped_column(JSON, default=dict)
    disposition: Mapped[dict] = mapped_column(JSON, default=dict)
    escalation: Mapped[str] = mapped_column(String(40), default="")
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    recommended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class CaseEntityLink(Base):
    __tablename__ = "case_links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(16))     # TRANSACTION CUSTOMER ALERT CASE
    entity_ref: Mapped[str] = mapped_column(String(80), index=True)
    note: Mapped[str] = mapped_column(String(200), default="")


class CaseEvent(Base):
    __tablename__ = "case_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    actor_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    actor_name: Mapped[str] = mapped_column(String(120), default="system")
    action: Mapped[str] = mapped_column(String(40))
    from_status: Mapped[str] = mapped_column(String(28), default="")
    to_status: Mapped[str] = mapped_column(String(28), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CaseNote(Base):
    __tablename__ = "case_notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    author_id: Mapped[str] = mapped_column(String(36))
    author_name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20), default="NOTE")   # NOTE FINDING SOURCE_OF_FUNDS RELATED_PARTY
    body: Mapped[str] = mapped_column(Text)
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EvidenceReference(Base):
    __tablename__ = "evidence"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("EVD-"))
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    filename: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(80))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    external_reference: Mapped[str] = mapped_column(String(300), default="")
    uploaded_by: Mapped[str] = mapped_column(String(36))
    uploaded_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------- policy
class PolicyVersion(Base):
    __tablename__ = "policy_versions"
    __table_args__ = (UniqueConstraint("tenant_id", "version", name="uq_policy_version"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)         # BOP-POLICY-12
    tenant_id: Mapped[str] = mapped_column(String(16), index=True)
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(18), default="DRAFT")     # DRAFT PENDING_APPROVAL ACTIVE SUPERSEDED REJECTED
    document: Mapped[dict] = mapped_column(JSON, default=dict)
    justification: Mapped[str] = mapped_column(Text, default="")
    based_on: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(120), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approved_by_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    effective_from: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    effective_to: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    simulation: Mapped[dict] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------- reports, audit, ops
class ReportJob(Base):
    __tablename__ = "report_jobs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(16), index=True)
    space: Mapped[str] = mapped_column(String(8), default="LIVE")
    number: Mapped[str] = mapped_column(String(40), default="")
    report_type: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200), default="")
    fmt: Mapped[str] = mapped_column(String(6), default="pdf")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="PENDING")
    storage_key: Mapped[str] = mapped_column(String(300), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditEvent(Base):
    """Tamper-evident audit trail: each event stores the hash of the previous one."""
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(16), index=True)
    space: Mapped[str] = mapped_column(String(8), default="LIVE")
    actor_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    actor_name: Mapped[str] = mapped_column(String(120), default="system")
    actor_role: Mapped[str] = mapped_column(String(20), default="SYSTEM")
    action: Mapped[str] = mapped_column(String(60), index=True)
    resource_type: Mapped[str] = mapped_column(String(40), default="")
    resource_id: Mapped[str] = mapped_column(String(80), default="")
    before: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    after: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    outcome: Mapped[str] = mapped_column(String(12), default="SUCCESS")
    reason: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    correlation_id: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    hash: Mapped[str] = mapped_column(String(64), default="")


class SystemIncident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("INC-"))
    tenant_id: Mapped[str] = mapped_column(String(16), index=True)
    severity: Mapped[str] = mapped_column(String(8), default="SEV3")
    category: Mapped[str] = mapped_column(String(40), default="")
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="DETECTED")   # DETECTED ACKNOWLEDGED INVESTIGATING MITIGATED RESOLVED POSTMORTEM
    affected: Mapped[str] = mapped_column(String(200), default="")
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    owner: Mapped[str] = mapped_column(String(120), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    timeline: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Counter(Base):
    """Per-tenant sequence counters for human-readable identifiers."""
    __tablename__ = "counters"
    tenant_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)
