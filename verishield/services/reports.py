"""Report catalogue and tenant-branded rendering (BRD sections 11.5 and 12).

Each report builder returns a neutral document structure (KPIs + titled tables + text); renderers
produce PDF (ReportLab, with the tenant's logo, colours, header/footer, confidentiality notice,
numbering, watermark and sign-off blocks), CSV (main table) and XLSX (every table as a sheet).
"""
from __future__ import annotations

import csv
import io
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from xml.sax.saxutils import escape

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import Principal
from ..models import (Alert, AuditEvent, Case, Connector, Customer, CustomerSegment, DeadLetterEvent, Decision,
                      ModelVersion, PolicyVersion, ReportJob, SystemIncident, Tenant, Transaction, User, utcnow)
from ..settings import settings
from .common import next_seq
from .metrics import metrics

DEFAULT_BRANDING = {
    "display_name": "", "report_title_prefix": "", "primary_color": "#10233F", "accent_color": "#0BAA9B",
    "secondary_color": "#3569C8", "header_text": "Financial Crime & AML Compliance",
    "department": "Financial Crime & AML Compliance", "classification": "CONFIDENTIAL",
    "footer_text": "", "confidentiality_notice": "This document is confidential and intended for authorised internal "
                                                   "use only. Do not distribute outside the institution.",
    "disclaimer": "Model signals and alerts are inputs for human review, not findings of fraud or money laundering.",
    "numbering_prefix": "VS", "watermark": "", "prepared_by_label": "Prepared by", "reviewed_by_label": "Reviewed by",
    "approved_by_label": "Approved by", "signature_blocks": True, "logo_key": "", "locale": "en-GB",
    "date_format": "%d %b %Y %H:%M", "page_number_format": "Page {page} of {pages}", "appendix": True,
    "aml_header_text": "", "aml_footer_text": "",
}
TENANT_BRANDING_FIELDS = set(DEFAULT_BRANDING) - {"aml_header_text", "aml_footer_text"}
AML_BRANDING_FIELDS = {"aml_header_text", "aml_footer_text", "disclaimer_addendum"}


def branding(tenant: Tenant) -> dict:
    b = dict(DEFAULT_BRANDING)
    b.update({k: v for k, v in (tenant.branding or {}).items() if v is not None})
    b["display_name"] = b.get("display_name") or tenant.display_name
    b["legal_name"] = tenant.legal_name or b["display_name"]
    return b


# --------------------------------------------------------------------------- document model
class Doc:
    def __init__(self, title: str, subtitle: str = ""):
        self.title, self.subtitle = title, subtitle
        self.kpis: list[tuple[str, Any]] = []
        self.sections: list[dict] = []
        self.meta: dict[str, str] = {}
        self.summary: str = ""

    def kpi(self, label: str, value: Any) -> "Doc":
        self.kpis.append((label, value))
        return self

    def text(self, heading: str, body: str) -> "Doc":
        self.sections.append({"heading": heading, "text": body})
        return self

    def table(self, heading: str, columns: list[str], rows: list[list], main: bool = False,
              note: str = "") -> "Doc":
        self.sections.append({"heading": heading, "columns": columns, "rows": rows, "main": main, "note": note})
        return self

    @property
    def main_table(self) -> Optional[dict]:
        tables = [s for s in self.sections if "columns" in s]
        return next((s for s in tables if s.get("main")), tables[0] if tables else None)


# --------------------------------------------------------------------------- filters & scope
def _range(params: dict) -> tuple[datetime, datetime]:
    to = params.get("date_to")
    frm = params.get("date_from")
    end = datetime.fromisoformat(str(to).replace("Z", "")) if to else utcnow()
    if to and len(str(to)) <= 10:
        end = end + timedelta(days=1)
    start = datetime.fromisoformat(str(frm).replace("Z", "")) if frm else end - timedelta(days=30)
    return start, end


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    return str(v)


def _decisions(db: Session, p: Principal, params: dict):
    start, end = _range(params)
    q = select(Decision, Transaction).join(Transaction, (Transaction.tenant_id == Decision.tenant_id)
                                           & (Transaction.space == Decision.space)
                                           & (Transaction.tx_ref == Decision.tx_ref)).where(
        Decision.tenant_id == p.tenant_id, Decision.space == p.space, Decision.issued_at >= start,
        Decision.issued_at < end)
    if params.get("tx_type"):
        q = q.where(Transaction.tx_type == params["tx_type"])
    if params.get("channel"):
        q = q.where(Transaction.channel == params["channel"])
    if params.get("risk_level"):
        q = q.where(Decision.risk_level == params["risk_level"])
    if params.get("decision"):
        q = q.where(Decision.decision == params["decision"])
    return db.execute(q.order_by(Decision.issued_at.desc()).limit(20000)).all()


# --------------------------------------------------------------------------- builders
def r_executive(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    start, end = _range(params)
    rows = _decisions(db, p, params)
    outcomes = Counter(d.decision for d, _ in rows)
    alerts = db.execute(select(Alert).where(Alert.tenant_id == p.tenant_id, Alert.space == p.space,
                                            Alert.created_at >= start, Alert.created_at < end)).scalars().all()
    cases = db.execute(select(Case).where(Case.tenant_id == p.tenant_id, Case.space == p.space)).scalars().all()
    open_cases = [c for c in cases if c.status != "CLOSED"]
    breached = [c for c in open_cases if c.due_at and c.due_at < utcnow()]
    held_amount = sum(t.amount for d, t in rows if t.execution_state == "HELD")
    doc = Doc("Executive Fraud & AML Overview", f"{start:%d %b %Y} - {end:%d %b %Y}")
    doc.kpi("Transactions decided", len(rows)).kpi("Alerts raised", len(alerts)) \
        .kpi("Open cases", len(open_cases)).kpi("SLA breaches", len(breached)) \
        .kpi("Held exposure", f"{held_amount:,.2f} {tenant.base_currency}") \
        .kpi("Pre-auth / post-event", f"{sum(1 for d, _ in rows if d.mode == 'PRE_AUTH')} / "
                                      f"{sum(1 for d, _ in rows if d.mode == 'POST_EVENT')}")
    doc.table("Decision outcomes", ["Outcome", "Count", "Share"],
              [[o, outcomes.get(o, 0), f"{100 * outcomes.get(o, 0) / max(1, len(rows)):.1f}%"]
               for o in ("PASS", "REVIEW", "HOLD", "DECLINE", "ERROR_POLICY")], main=True,
              note="Post-event decisions are monitoring outcomes; those transactions were not stopped.")
    by_day: dict[str, Counter] = defaultdict(Counter)
    for d, _ in rows:
        by_day[d.issued_at.strftime("%Y-%m-%d")][d.decision] += 1
    doc.table("Daily trend", ["Day", "PASS", "REVIEW", "HOLD", "DECLINE"],
              [[day, c["PASS"], c["REVIEW"], c["HOLD"], c["DECLINE"]] for day, c in sorted(by_day.items())])
    sev = Counter(a.severity for a in alerts)
    doc.table("Alerts by severity", ["Severity", "Count"], [[s, sev.get(s, 0)] for s in ("CRITICAL", "HIGH",
                                                                                         "MEDIUM", "LOW")])
    disp = Counter((c.disposition or {}).get("label", "-") for c in cases if c.status == "CLOSED")
    doc.table("Case dispositions", ["Disposition", "Count"], [[k, v] for k, v in disp.most_common()])
    codes = Counter(code for d, _ in rows if d.decision != "PASS" for code in d.reason_codes or [])
    doc.table("Top reason codes", ["Reason code", "Count"], [[k, v] for k, v in codes.most_common(10)])
    return doc


def _days(params: dict) -> int:
    start, end = _range(params)
    return max(1, min(366, (end - start).days or 1))


def r_board(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    from .executive import overview
    from .model_ops import health
    days = _days(params)
    o = overview(db, p.tenant_id, p.space, days)
    k, cur = o["kpis"], tenant.base_currency
    doc = Doc("Board Report - Fraud, AML & Platform Overview", f"Last {days} days")
    doc.summary = (f"During the last {days} days the fraud engine scored {k['transactions']:,} transactions worth "
                   f"{k['value']:,.2f} {cur}. {k['intervention_rate']:.2f}% required an intervention (review, hold or "
                   f"decline); {k['protected_value']:,.2f} {cur} was held or declined before execution. "
                   f"{k['alerts_period']:,} alerts were raised and {k['cases_open']:,} AML cases are open "
                   f"({k['cases_breached']:,} past their due date). The customer base holds {k['customers']:,} customers, "
                   f"{k['customers_high_risk']:,} of them classified high risk by the institution.")
    doc.kpi("Transactions scored", f"{k['transactions']:,}").kpi("Value scored", f"{k['value']:,.0f} {cur}")         .kpi("Intervention rate", f"{k['intervention_rate']:.2f}%").kpi("Value protected", f"{k['protected_value']:,.0f} {cur}")         .kpi("Alerts raised", f"{k['alerts_period']:,}").kpi("Open AML cases", f"{k['cases_open']:,}")         .kpi("Cases past due", f"{k['cases_breached']:,}").kpi("Escalations (STR/SAR)", f"{k['escalations']:,}")
    total = max(1, k["transactions"])
    doc.table("Decision outcomes", ["Outcome", "Transactions", "Share"],
              [[d, o["decisions"].get(d, 0), f"{100 * o['decisions'].get(d, 0) / total:.2f}%"]
               for d in ("PASS", "REVIEW", "HOLD", "DECLINE", "ERROR_POLICY")], main=True)
    doc.table("Activity by channel", ["Channel", "Transactions", f"Value ({cur})"],
              [[c["channel"].replace("_", " ").title(), c["count"], c["value"]] for c in o["by_channel"]])
    doc.table("Activity by product", ["Product", "Transactions", f"Value ({cur})"],
              [[c["type"].replace("_", " ").title(), c["count"], c["value"]] for c in o["by_type"]])
    doc.table("Weekly trend", ["Week starting", "Transactions", f"Value ({cur})", "Interventions"],
              _weekly(o["trend"]))
    doc.table("Customer portfolio by institution risk rating", ["Risk rating", "Customers"],
              [[r, o["customer_risk"].get(r, 0)] for r in ("LOW", "MEDIUM", "HIGH") if r in o["customer_risk"]])
    doc.table("Customers with the most interventions", ["Customer", "Name", "Interventions", "Highest score"],
              [[c["customer_ref"], c["name"], c["interventions"], c["max_score"]] for c in o["top_risk_customers"]
               if c["interventions"]])
    sysm = o["system"]
    h = health(db, p.tenant_id, p.space)
    doc.table("Platform & model status", ["Item", "Status"], [
        ["Decision service", "Ready" if sysm["decision_service_ready"] else "NOT READY"],
        ["Active fraud model", sysm["active_model"] or "-"],
        ["Model health", h["status"].replace("_", " ").title() + (f" - {'; '.join(h['reasons'])}" if h["reasons"] else "")],
        ["Channels running", f"{sysm['channels_running']} of {sysm['channels_total']}"],
        ["Degraded channels", sysm["channels_degraded"]], ["Open incidents", sysm["open_incidents"]],
        ["Decision latency (p50 / p95)", f"{sysm['decision_p50_ms'] or '-'} / {sysm['decision_p95_ms'] or '-'} ms"]])
    return doc


def _weekly(trend: list[dict]) -> list[list]:
    out, bucket = [], None
    for i, d in enumerate(trend):
        if i % 7 == 0:
            bucket = [d["day"], 0, 0.0, 0]
            out.append(bucket)
        bucket[1] += d["count"]
        bucket[2] = round(bucket[2] + d["value"], 2)
        bucket[3] += d["interventions"]
    return out


def r_portfolio(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    from .executive import overview
    days = _days(params)
    o = overview(db, p.tenant_id, p.space, days)
    customers = db.execute(select(Customer).where(Customer.tenant_id == p.tenant_id, Customer.space == p.space)
                           ).scalars().all()
    doc = Doc("Customer Portfolio Report", f"{len(customers):,} customers · activity of the last {days} days")
    k = o["kpis"]
    doc.summary = (f"{len(customers):,} customers are on record; {k['customers_active']:,} transacted in the period. "
                   f"{k['customers_high_risk']:,} are rated high risk by the institution. Customers are grouped into "
                   f"peer segments by the segmentation model; segments are neutral comparison groups, not risk ratings.")
    by_type = Counter(c.customer_type for c in customers)
    doc.kpi("Customers", f"{len(customers):,}").kpi("Active in period", f"{k['customers_active']:,}")         .kpi("Individuals", f"{by_type.get('INDIVIDUAL', 0):,}").kpi("Businesses", f"{by_type.get('BUSINESS', 0):,}")         .kpi("High risk (institution)", f"{k['customers_high_risk']:,}")         .kpi("KYC review due", f"{sum(1 for c in customers if c.kyc_review_status != 'CURRENT'):,}")
    seg = Counter(c.segment_label or "Unassigned" for c in customers)
    doc.table("Customers by peer segment", ["Segment", "Customers", "Share"],
              [[label, n, f"{100 * n / max(1, len(customers)):.1f}%"] for label, n in seg.most_common()], main=True)
    risk = Counter((c.customer_type, c.risk_category) for c in customers)
    doc.table("Risk rating by customer type", ["Customer type", "LOW", "MEDIUM", "HIGH"],
              [[t.title(), risk.get((t, "LOW"), 0), risk.get((t, "MEDIUM"), 0), risk.get((t, "HIGH"), 0)]
               for t in ("INDIVIDUAL", "BUSINESS")])
    city = Counter((c.attributes or {}).get("city") or "Not recorded" for c in customers)
    doc.table("Customers by city", ["City", "Customers"], [[k_, v] for k_, v in city.most_common(15)])
    doc.table("Customers with the most interventions", ["Customer", "Name", "Interventions", "Highest score"],
              [[c["customer_ref"], c["name"], c["interventions"], c["max_score"]] for c in o["top_risk_customers"]])
    return doc


def r_activity(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    from .executive import overview
    days = _days(params)
    o = overview(db, p.tenant_id, p.space, days)
    k, cur = o["kpis"], tenant.base_currency
    doc = Doc("Transaction Activity Report", f"Last {days} days")
    doc.summary = (f"{k['transactions']:,} transactions worth {k['value']:,.2f} {cur} were received through the live "
                   f"channels and scored in real time; {k['transactions_24h']:,} in the last 24 hours.")
    doc.kpi("Transactions", f"{k['transactions']:,}").kpi(f"Value ({cur})", f"{k['value']:,.0f}")         .kpi("Last 24 hours", f"{k['transactions_24h']:,}").kpi("Intervention rate", f"{k['intervention_rate']:.2f}%")
    doc.table("Daily activity", ["Day", "Transactions", f"Value ({cur})", "Interventions"],
              [[d["day"], d["count"], d["value"], d["interventions"]] for d in o["trend"]], main=True)
    doc.table("By channel", ["Channel", "Transactions", f"Value ({cur})"],
              [[c["channel"].replace("_", " ").title(), c["count"], c["value"]] for c in o["by_channel"]])
    doc.table("By product", ["Product", "Transactions", f"Value ({cur})"],
              [[c["type"].replace("_", " ").title(), c["count"], c["value"]] for c in o["by_type"]])
    doc.table("By city", ["City", "Transactions"], [[c["city"], c["count"]] for c in o["by_city"]])
    doc.table("Risk level of scored transactions", ["Risk level", "Transactions"],
              [[r, o["risk_levels"].get(r, 0)] for r in ("LOW", "MEDIUM", "HIGH", "CRITICAL")])
    return doc


def r_decision_batch(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    rows = _decisions(db, p, params)
    doc = Doc("Transaction Decision Report (batch)", f"{len(rows)} decisions")
    doc.kpi("Decisions", len(rows)).kpi("Non-PASS", sum(1 for d, _ in rows if d.decision != "PASS"))
    doc.table("Decisions", ["Decision ID", "Issued", "Transaction", "Customer", "Type", "Amount", "Mode", "Decision",
                            "Fraud score", "Risk", "AML", "Execution", "Reason codes", "Policy", "Model"],
              [[d.id, d.issued_at, d.tx_ref, d.customer_ref, t.tx_type, t.amount, d.mode, d.decision,
                round(d.fraud_score, 3), d.risk_level, d.aml_signal_level, t.execution_state,
                " ".join(d.reason_codes or []), d.policy_version, d.fraud_model_version] for d, t in rows], main=True)
    return doc


def r_transaction(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    from .decision import explain_codes
    ref = params.get("tx_ref")
    tx = db.execute(select(Transaction).where(Transaction.tenant_id == p.tenant_id, Transaction.space == p.space,
                                              Transaction.tx_ref == ref)).scalar()
    if tx is None:
        raise LookupError(f"transaction {ref} not found")
    dec = db.get(Decision, tx.decision_id) if tx.decision_id else None
    doc = Doc("Transaction Decision Report", f"Transaction {tx.tx_ref}")
    if dec:
        doc.kpi("Decision", dec.decision).kpi("Fraud score", f"{dec.fraud_score:.2f}").kpi("Risk level", dec.risk_level)\
            .kpi("AML signal", dec.aml_signal_level).kpi("Mode", dec.mode).kpi("Execution", tx.execution_state)
    doc.table("Transaction", ["Field", "Value"], [
        ["Transaction ID", tx.tx_ref], ["Customer", tx.customer_ref], ["Timestamp", tx.ts], ["Type", tx.tx_type],
        ["Amount", f"{tx.amount:,.2f} {tx.currency}"], ["Channel", tx.channel], ["Beneficiary", tx.beneficiary],
        ["Beneficiary country", tx.beneficiary_country], ["Connection", tx.cc_asn], ["Source", tx.source]], main=True)
    if dec:
        doc.table("Profile scores", ["Profile", "Score"], [["Local (HBOS)", dec.local_score],
                                                           ["Temporal", dec.temporal_score],
                                                           ["Global (peer group)", dec.global_score],
                                                           ["Combined fraud score", dec.fraud_score]])
        doc.table("Reason codes", ["Code", "Meaning"], [[c["code"], c["text"]] for c in explain_codes(dec.reason_codes)])
        if dec.explanation:
            doc.text("Explanation", dec.explanation)
        doc.table("Governance", ["Item", "Value"], [["Decision ID", dec.id], ["Policy version", dec.policy_version],
                                                    ["Fraud model version", dec.fraud_model_version],
                                                    ["Correlation ID", dec.correlation_id], ["Issued", dec.issued_at],
                                                    ["Latency (ms)", dec.latency_ms]])
    if tx.mode == "POST_EVENT":
        doc.text("Timing", "This transaction was received after execution (post-event monitoring). It was not "
                           "blocked by VeriShield.")
    return doc


def r_customer(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    ref = params.get("customer_ref")
    c = db.execute(select(Customer).where(Customer.tenant_id == p.tenant_id, Customer.space == p.space,
                                          Customer.customer_ref == ref)).scalar()
    if c is None:
        raise LookupError(f"customer {ref} not found")
    txs = db.execute(select(Transaction).where(Transaction.tenant_id == p.tenant_id, Transaction.space == p.space,
                                               Transaction.customer_ref == ref).order_by(Transaction.ts.desc())
                     .limit(500)).scalars().all()
    alerts = db.execute(select(Alert).where(Alert.tenant_id == p.tenant_id, Alert.space == p.space,
                                            Alert.customer_ref == ref)).scalars().all()
    doc = Doc("Customer Risk & Behaviour Profile", f"Customer {ref}")
    doc.kpi("Transactions", len(txs)).kpi("Alerts", len(alerts)).kpi("Institution risk", c.risk_category) \
        .kpi("Peer segment", c.segment_label.split(" · ")[0] if c.segment_label else "-")
    doc.table("Profile", ["Field", "Value"], [
        ["Customer", ref], ["Type", c.customer_type], ["Business activity", c.business_activity],
        ["KYC review status", c.kyc_review_status], ["Expected monthly turnover", c.expected_monthly_turnover],
        ["Expected monthly transactions", c.expected_monthly_tx], ["Tenure (months)", c.tenure_months],
        ["Products", ", ".join(c.products or [])], ["Peer segment", c.segment_label],
        ["Segment confidence", c.segment_confidence], ["Segment basis", c.segment_basis],
        ["Segmentation model", c.segment_model]], main=True)
    doc.table("Recent transactions", ["Time", "Transaction", "Type", "Amount", "Beneficiary", "Channel", "Execution"],
              [[t.ts, t.tx_ref, t.tx_type, t.amount, t.beneficiary, t.channel, t.execution_state] for t in txs[:100]])
    doc.table("Alerts", ["Alert", "Created", "Severity", "Decision", "Status", "Summary"],
              [[a.id, a.created_at, a.severity, a.decision, a.status, a.summary] for a in alerts])
    doc.text("Segment safeguard", "A peer segment is a comparison aid - not a regulatory risk grade and never a "
                                  "standalone reason for an adverse decision.")
    return doc


def r_segments(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    mv = db.execute(select(ModelVersion).where(ModelVersion.tenant_id == p.tenant_id, ModelVersion.space == p.space,
                                               ModelVersion.kind == "SEGMENTATION", ModelVersion.status == "ACTIVE")
                    ).scalar()
    doc = Doc("Customer Segment Distribution", mv.id if mv else "no active segmentation model")
    if mv:
        doc.kpi("Segments", mv.metrics.get("k")).kpi("Silhouette", mv.metrics.get("silhouette")) \
            .kpi("Stability (Rand)", mv.metrics.get("stability_rand_index")).kpi("Customers", mv.training_size)
        segs = db.execute(select(CustomerSegment).where(CustomerSegment.model_version == mv.id)
                          .order_by(CustomerSegment.segment_key)).scalars().all()
        counts = Counter(c for (c,) in db.execute(select(Customer.segment_key).where(
            Customer.tenant_id == p.tenant_id, Customer.space == p.space)))
        doc.table("Segments", ["Segment", "Label", "Size at training", "Customers now"],
                  [[s.segment_key + 1, s.label, s.size, counts.get(s.segment_key, 0)] for s in segs], main=True)
    return doc


def r_register(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    start, end = _range(params)
    alerts = db.execute(select(Alert).where(Alert.tenant_id == p.tenant_id, Alert.space == p.space,
                                            Alert.created_at >= start, Alert.created_at < end)
                        .order_by(Alert.created_at.desc())).scalars().all()
    cases = db.execute(select(Case).where(Case.tenant_id == p.tenant_id, Case.space == p.space)
                       .order_by(Case.created_at.desc())).scalars().all()
    names = {u.id: u.full_name for u in db.execute(select(User).where(User.tenant_id == p.tenant_id)).scalars()}
    doc = Doc("AML Alert & Case Register", f"{start:%d %b %Y} - {end:%d %b %Y}")
    doc.kpi("Alerts", len(alerts)).kpi("Open alerts", sum(1 for a in alerts if a.status in ("OPEN", "TRIAGED"))) \
        .kpi("Cases", len(cases)).kpi("Open cases", sum(1 for c in cases if c.status != "CLOSED"))
    doc.table("Cases", ["Case", "Created", "Type", "Priority", "Status", "Assignee", "Due", "Recommendation",
                        "Disposition"],
              [[c.id, c.created_at, c.case_type, c.priority, c.status, names.get(c.assignee_id, ""), c.due_at,
                (c.recommendation or {}).get("label", ""), (c.disposition or {}).get("label", "")] for c in cases],
              main=True)
    doc.table("Alerts", ["Alert", "Created", "Customer", "Transaction", "Severity", "Decision", "Timing", "Status",
                         "Case"],
              [[a.id, a.created_at, a.customer_ref, a.tx_ref, a.severity, a.decision, a.timing, a.status, a.case_id]
               for a in alerts])
    return doc


def r_investigation(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    from . import cases as case_service
    case = case_service.get_case(db, p, params.get("case_id", ""))
    data = case_service.serialize(db, case, detail=True, p=p)
    doc = Doc("AML Case Investigation Report", f"{case.id} - {case.title}")
    doc.kpi("Status", case.status).kpi("Priority", case.priority).kpi("Assignee", data["assignee_name"] or "-") \
        .kpi("Due", case.due_at.strftime("%d %b %Y") if case.due_at else "-")
    doc.table("Case", ["Field", "Value"], [
        ["Case ID", case.id], ["Type", case.case_type], ["Classification", case.classification],
        ["Purpose", case.purpose], ["Questions", "; ".join(case.questions or [])], ["Created", case.created_at],
        ["Owner", data["owner_name"]], ["Linked entities", ", ".join(f"{l['type']}:{l['ref']}" for l in data["links"])]],
        main=True)
    doc.table("Investigation checklist", ["Item", "Done"], [[i["item"], "Yes" if i.get("done") else "No"]
                                                            for i in case.checklist or []])
    doc.table("Notes & findings", ["Time", "Author", "Kind", "Note"],
              [[n["created_at"], n["author_name"], n["kind"], n["body"]] for n in data["notes"]])
    doc.table("Evidence index", ["File / reference", "SHA-256", "Size", "Added by", "Time"],
              [[e["filename"], e["sha256"][:20] + "…", e["size"], e["uploaded_by_name"], e["created_at"]]
               for e in data["evidence"]])
    rec = case.recommendation or {}
    doc.table("Employee recommendation", ["Field", "Value"], [["Recommendation", rec.get("label", "-")],
                                                               ["Rationale", rec.get("rationale", "")],
                                                               ["Findings", rec.get("findings", "")],
                                                               ["Submitted by", rec.get("submitted_by", "")],
                                                               ["Submitted at", rec.get("submitted_at", "")]])
    disp = case.disposition or {}
    doc.table("Manager disposition", ["Field", "Value"], [["Disposition", disp.get("label", "-")],
                                                          ["Rationale", disp.get("rationale", "")],
                                                          ["Decided by", disp.get("decided_by", "")],
                                                          ["Decided at", disp.get("decided_at", "")]])
    doc.table("Chronology", ["Time", "Actor", "Action", "Status", "Detail"],
              [[e["created_at"], e["actor_name"], e["action"],
                f"{e['from_status']} -> {e['to_status']}" if e["to_status"] else "", e["detail"]]
               for e in data["events"]])
    doc.text("Regulatory note", "A suspicious alert is not a confirmed crime. External STR/SAR filing follows the "
                                "institution's authorised compliance process and is never automatic.")
    return doc


def r_held(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    rows = [(d, t) for d, t in _decisions(db, p, params) if d.decision in ("HOLD", "DECLINE")
            or t.execution_state in ("HELD", "DECLINED", "EXPIRED", "RELEASE_REQUESTED", "DECLINE_REQUESTED")]
    doc = Doc("Held / Declined Transactions", "Pre-authorisation outcomes and release/decline results")
    states = Counter(t.execution_state for _, t in rows)
    doc.kpi("Transactions", len(rows)).kpi("Currently held", states.get("HELD", 0)) \
        .kpi("Declined", states.get("DECLINED", 0)).kpi("Released (authorised)", states.get("AUTHORISED", 0))
    doc.table("Transactions", ["Issued", "Transaction", "Customer", "Amount", "Decision", "Mode", "Execution state",
                               "Reason codes"],
              [[d.issued_at, d.tx_ref, d.customer_ref, t.amount, d.decision, d.mode, t.execution_state,
                " ".join(d.reason_codes or [])] for d, t in rows], main=True,
              note="Post-event rows are monitoring outcomes; only PRE_AUTH holds/declines were enforced by the bank.")
    return doc


def r_policy(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    from .policy import diff
    rows = db.execute(select(PolicyVersion).where(PolicyVersion.tenant_id == p.tenant_id)
                      .order_by(PolicyVersion.version)).scalars().all()
    doc = Doc("Threshold / Policy Configuration History", f"{len(rows)} versions")
    doc.table("Versions", ["Version", "Name", "Status", "Created by", "Created", "Activated", "Justification"],
              [[v.id, v.name, v.status, v.created_by_name, v.created_at, v.activated_at, v.justification] for v in rows],
              main=True)
    changes = []
    for prev, cur in zip(rows, rows[1:]):
        for ch in diff(prev.document, cur.document)[:40]:
            changes.append([cur.id, ch["path"], str(ch["before"])[:60], str(ch["after"])[:60]])
    doc.table("Changes between versions", ["Version", "Setting", "Before", "After"], changes)
    return doc


def r_sla(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    cases = db.execute(select(Case).where(Case.tenant_id == p.tenant_id, Case.space == p.space)).scalars().all()
    users = db.execute(select(User).where(User.tenant_id == p.tenant_id, User.role == "AML_EMPLOYEE")).scalars().all()
    now = utcnow()
    rows = []
    for u in users:
        mine = [c for c in cases if c.assignee_id == u.id]
        open_ = [c for c in mine if c.status != "CLOSED"]
        turn = [(c.recommended_at - c.assigned_at).total_seconds() / 3600 for c in mine
                if c.recommended_at and c.assigned_at]
        rows.append([u.full_name, len(mine), len(open_), sum(1 for c in open_ if c.due_at and c.due_at < now),
                     sum(1 for c in mine if c.recommendation), f"{sum(turn) / len(turn):.1f}" if turn else "-",
                     sum(1 for c in mine if any(e for e in [c.status == "RETURNED_FOR_MORE_INFO"]))])
    doc = Doc("Analyst Workload & SLA Performance")
    open_all = [c for c in cases if c.status != "CLOSED"]
    doc.kpi("Open cases", len(open_all)).kpi("SLA breached", sum(1 for c in open_all if c.due_at and c.due_at < now)) \
        .kpi("Awaiting Manager", sum(1 for c in open_all if c.status in ("RECOMMENDATION_SUBMITTED", "MANAGER_REVIEW")))
    doc.table("Analysts", ["Analyst", "Assigned", "Open", "SLA breached", "Recommendations",
                           "Avg hours to recommend", "Returned"], rows, main=True)
    ageing = Counter()
    for c in open_all:
        age = (now - c.created_at).days
        ageing["0-2 days" if age <= 2 else "3-7 days" if age <= 7 else "8-30 days" if age <= 30 else ">30 days"] += 1
    doc.table("Open case ageing", ["Bucket", "Cases"], [[k, ageing.get(k, 0)] for k in
                                                        ("0-2 days", "3-7 days", "8-30 days", ">30 days")])
    return doc


def r_models(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    from . import engine_registry
    rows = db.execute(select(ModelVersion).where(ModelVersion.tenant_id == p.tenant_id, ModelVersion.space == p.space)
                      .order_by(ModelVersion.trained_at.desc())).scalars().all()
    st = engine_registry.status(db, p.tenant_id, p.space)
    doc = Doc("Model Health, Coverage & Performance", f"Active fraud model: {st['active_model'] or '-'}")
    doc.kpi("Engine", "ready" if st["ready"] else "not ready").kpi("Shadow model", st["shadow_model"] or "-") \
        .kpi("Undertrained profiles", st["undertrained_profiles"]).kpi("Scoring p95 (ms)",
                                                                       metrics.latency_summary("decision")["p95"])
    doc.table("Model versions", ["Model", "Kind", "Status", "Trained", "Training size", "Trained by", "Approved by"],
              [[m.id, m.kind, m.status, m.trained_at, m.training_size, m.trained_by, m.approved_by or ""] for m in rows],
              main=True)
    perf = performance(db, p.tenant_id, p.space)
    doc.table("Alert outcomes (labelled where available)", ["Metric", "Value"], [[k, v] for k, v in perf.items()],
              note="Labels come from analyst dispositions (and synthetic ground truth in Demo Mode).")
    return doc


def performance(db: Session, tenant_id: str, space: str) -> dict:
    rows = db.execute(select(Decision.decision, Transaction.label).join(
        Transaction, (Transaction.tenant_id == Decision.tenant_id) & (Transaction.space == Decision.space)
        & (Transaction.tx_ref == Decision.tx_ref)).where(Decision.tenant_id == tenant_id, Decision.space == space,
                                                         Decision.decision != "ERROR_POLICY")).all()
    labelled = [(d, l) for d, l in rows if l is not None]
    out = {"decisions": len(rows), "alert_rate": f"{100 * sum(1 for d, _ in rows if d != 'PASS') / max(1, len(rows)):.1f}%"}
    if labelled:
        tp = sum(1 for d, l in labelled if d != "PASS" and l == 1)
        fp = sum(1 for d, l in labelled if d != "PASS" and l == 0)
        fn = sum(1 for d, l in labelled if d == "PASS" and l == 1)
        out.update({"labelled": len(labelled), "precision": f"{tp / max(1, tp + fp):.2f}",
                    "recall": f"{tp / max(1, tp + fn):.2f}", "true_positives": tp, "false_positives": fp,
                    "missed": fn})
    dispositions = Counter((c.disposition or {}).get("value") for c in db.execute(
        select(Case).where(Case.tenant_id == tenant_id, Case.space == space, Case.status == "CLOSED")).scalars())
    out["cases_closed_false_positive"] = dispositions.get("CLOSE_FALSE_POSITIVE", 0)
    out["cases_confirmed_fraud"] = dispositions.get("CONFIRM_FRAUD", 0)
    return out


def r_channels(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    from .connectors import public
    conns = db.execute(select(Connector).where(Connector.tenant_id == p.tenant_id, Connector.space == p.space))\
        .scalars().all()
    start, end = _range(params)
    by_source = db.execute(select(Transaction.source, Transaction.mode, func.count()).where(
        Transaction.tenant_id == p.tenant_id, Transaction.space == p.space, Transaction.created_at >= start,
        Transaction.created_at < end).group_by(Transaction.source, Transaction.mode)).all()
    dlq = db.execute(select(DeadLetterEvent.source, DeadLetterEvent.category, func.count()).where(
        DeadLetterEvent.tenant_id == p.tenant_id, DeadLetterEvent.space == p.space,
        DeadLetterEvent.created_at >= start, DeadLetterEvent.created_at < end)
        .group_by(DeadLetterEvent.source, DeadLetterEvent.category)).all()
    doc = Doc("Channel / Data Ingestion Health & Reconciliation", f"{start:%d %b %Y} - {end:%d %b %Y}")
    doc.kpi("Connectors", len(conns)).kpi("Enabled", sum(1 for c in conns if c.status == "ENABLED")) \
        .kpi("Degraded", sum(1 for c in conns if c.status == "DEGRADED")) \
        .kpi("Dead letters", sum(n for *_, n in dlq))
    doc.table("Connectors", ["Name", "Type", "Status", "Received", "Accepted", "Rejected", "Duplicates",
                             "Last event", "Last error"],
              [[c.name, c.type, c.status, (c.counters or {}).get("received", 0), (c.counters or {}).get("accepted", 0),
                (c.counters or {}).get("rejected", 0), (c.counters or {}).get("duplicates", 0), c.last_event_at,
                public(c)["last_error"][:80]] for c in conns], main=True)
    doc.table("Reconciliation by source", ["Source", "Mode", "Stored transactions"],
              [[s or "-", m, n] for s, m, n in by_source])
    doc.table("Rejected / duplicate records", ["Source", "Category", "Count"], [[s, c, n] for s, c, n in dlq])
    return doc


def r_failures(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    start, end = _range(params)
    incidents = db.execute(select(SystemIncident).where(SystemIncident.tenant_id == p.tenant_id,
                                                        SystemIncident.started_at >= start - timedelta(days=60))
                           .order_by(SystemIncident.started_at.desc())).scalars().all()
    errors = db.execute(select(Decision.error, func.count()).where(
        Decision.tenant_id == p.tenant_id, Decision.space == p.space, Decision.decision == "ERROR_POLICY",
        Decision.issued_at >= start).group_by(Decision.error)).all()
    auth = db.execute(select(AuditEvent.action, func.count()).where(
        AuditEvent.tenant_id == p.tenant_id, AuditEvent.outcome == "FAILURE", AuditEvent.created_at >= start)
        .group_by(AuditEvent.action)).all()
    snap = metrics.snapshot()
    doc = Doc("API, Connector & System Failure Report", f"{start:%d %b %Y} - {end:%d %b %Y}")
    lat = metrics.latency_summary("api")
    doc.kpi("Open incidents", sum(1 for i in incidents if i.status not in ("RESOLVED", "POSTMORTEM"))) \
        .kpi("API p95 (ms)", lat["p95"]).kpi("Decision p95 (ms)", metrics.latency_summary("decision")["p95"]) \
        .kpi("Uptime (h)", round(snap["uptime_seconds"] / 3600, 1))
    doc.table("Incidents", ["Incident", "Severity", "Category", "Title", "Status", "Occurrences", "Started",
                            "Resolved"],
              [[i.id, i.severity, i.category, i.title, i.status, i.occurrences, i.started_at, i.resolved_at]
               for i in incidents], main=True)
    doc.table("Decision fallbacks (ERROR_POLICY)", ["Error", "Count"], [[e, n] for e, n in errors])
    doc.table("Security / authentication failures", ["Action", "Count"], [[a, n] for a, n in auth])
    doc.table("Latency (in-process, since start)", ["Route", "Count", "p50", "p95", "p99"],
              [[k, v["count"], v["p50"], v["p95"], v["p99"]] for k, v in snap["latency"].items()])
    return doc


def r_demo(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    rows = db.execute(select(AuditEvent).where(AuditEvent.tenant_id == p.tenant_id, AuditEvent.action.like("demo.%"))
                      .order_by(AuditEvent.created_at.desc()).limit(200)).scalars().all()
    n = db.execute(select(func.count(Decision.id)).where(Decision.tenant_id == p.tenant_id,
                                                         Decision.space == "DEMO")).scalar()
    doc = Doc("Demo Mode Usage Report", "SYNTHETIC DATA - NOT FOR OPERATIONAL USE")
    doc.kpi("Demo decisions", n).kpi("Demo events", len(rows))
    doc.table("Demo activity", ["Time", "Actor", "Action", "Detail"],
              [[e.created_at, e.actor_name, e.action, str(e.after or "")[:120]] for e in rows], main=True,
              note="All figures are synthetic and must not be presented as production detection performance.")
    return doc


def r_regulator(db: Session, p: Principal, params: dict, tenant: Tenant) -> Doc:
    """Periodic AML statistics return for the regulator (default: the previous calendar month)."""
    from .ctr import statistics
    if params.get("date_from") or params.get("days"):
        start, end = _range(params)
    else:
        first = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = first
        start = (first - timedelta(days=1)).replace(day=1)
    s = statistics(db, p.tenant_id, p.space, start, end)
    cur = tenant.base_currency
    doc = Doc("Periodic AML Statistics Return", f"{s['period']['from']} to {s['period']['to']}")
    doc.meta = {"eyebrow": "REGULATORY REPORTING"}
    doc.summary = (f"In the period the institution monitored {s['transactions']['total']:,} transactions in real time. "
                   f"{s['alerts']['total']:,} alerts were raised, {s['cases']['opened']:,} AML cases opened and "
                   f"{s['cases']['closed']:,} closed. {s['str']['total']:,} suspicious transaction report(s) and "
                   f"{s['ctr']['total']:,} cash transaction report(s) were prepared.")
    doc.kpi("Transactions monitored", f"{s['transactions']['total']:,}").kpi("Alerts", f"{s['alerts']['total']:,}") \
        .kpi("Cases opened / closed", f"{s['cases']['opened']:,} / {s['cases']['closed']:,}") \
        .kpi("STR prepared", f"{s['str']['total']:,}").kpi("CTR prepared", f"{s['ctr']['total']:,}") \
        .kpi("Average days to close", s["cases"]["avg_days_to_close"] if s["cases"]["avg_days_to_close"] is not None else "-")
    doc.table("Transactions monitored by type", ["Type", "Transactions"],
              [[k.replace("_", " ").title(), v] for k, v in sorted(s["transactions"]["by_type"].items())], main=True)
    doc.table("Real-time decisions", ["Decision", "Count"], [[k, v] for k, v in sorted(s["decisions"].items())])
    doc.table("Alerts by severity", ["Severity", "Alerts"],
              [[k, s["alerts"]["by_severity"].get(k, 0)] for k in ("CRITICAL", "HIGH", "MEDIUM", "LOW")])
    doc.table("Investigations", ["Measure", "Value"], [
        ["Cases opened", s["cases"]["opened"]], ["Cases closed", s["cases"]["closed"]],
        ["Average days to close", s["cases"]["avg_days_to_close"] if s["cases"]["avg_days_to_close"] is not None else "-"],
        ["Cases open at the end of the period", s["cases"]["open_at_end"]]])
    doc.table("Suspicious transaction reports", ["Status", "Reports"],
              [[k.title(), v] for k, v in sorted(s["str"]["by_status"].items())] or [["-", 0]])
    doc.table("Cash transaction reports", ["Measure", "Value"], [
        ["Reports", s["ctr"]["total"]], ["Submitted", s["ctr"]["submitted"]], ["Exempt", s["ctr"]["exempt"]],
        [f"Cash deposits reported ({cur})", f"{s['ctr']['cash_in']:,.2f}"],
        [f"Cash withdrawals reported ({cur})", f"{s['ctr']['cash_out']:,.2f}"]])
    doc.table("Sanctions / PEP screening", ["Status", "Matches"],
              [[k.replace("_", " ").title(), v] for k, v in sorted(s["screening"]["by_status"].items())] or [["-", 0]])
    doc.table("Customer confirmations of held payments", ["Outcome", "Count"],
              [[k.title(), v] for k, v in sorted(s["confirmations"].items())] or [["-", 0]])
    doc.table("Customers by risk rating", ["Risk rating", "Customers"],
              [[k, s["customers_by_risk"].get(k, 0)] for k in ("LOW", "MEDIUM", "HIGH")])
    doc.table("Periodic KYC reviews", ["Measure", "Value"], [["Completed in the period", s["kyc_reviews"]["completed"]],
                                                            ["Overdue at the end of the period", s["kyc_reviews"]["overdue"]]])
    return doc


CATALOGUE: dict[str, dict] = {
    "regulator_statistics": {"title": "Periodic AML statistics return (regulator)", "builder": r_regulator,
                             "perms": ("reports.aml", "reports.executive", "reports.board")},
    "board_overview": {"title": "Board report - fraud, AML & platform overview", "builder": r_board,
                       "perms": ("reports.board", "reports.executive")},
    "customer_portfolio": {"title": "Customer portfolio report", "builder": r_portfolio,
                           "perms": ("reports.board", "reports.executive", "reports.aml")},
    "transaction_activity": {"title": "Transaction activity report", "builder": r_activity,
                             "perms": ("reports.board", "reports.executive", "reports.aml", "reports.system")},
    "executive_overview": {"title": "Executive fraud & AML overview", "builder": r_executive,
                           "perms": ("reports.executive", "reports.aml")},
    "decision_batch": {"title": "Transaction decision report (batch)", "builder": r_decision_batch,
                       "perms": ("reports.aml", "reports.executive")},
    "transaction_decision": {"title": "Transaction decision report (single)", "builder": r_transaction,
                             "perms": ("reports.aml", "reports.executive", "reports.assigned"), "params": ["tx_ref"]},
    "customer_profile": {"title": "Customer risk / behaviour profile", "builder": r_customer,
                         "perms": ("reports.aml", "reports.executive", "reports.assigned"),
                         "params": ["customer_ref"]},
    "segment_distribution": {"title": "Customer segment / cluster distribution", "builder": r_segments,
                             "perms": ("reports.aml", "reports.executive")},
    "alert_case_register": {"title": "AML alert & case register", "builder": r_register,
                            "perms": ("reports.aml", "reports.executive")},
    "investigation": {"title": "Investigation report (chronology, evidence, recommendation, disposition)",
                      "builder": r_investigation, "perms": ("reports.aml", "reports.executive", "reports.assigned"),
                      "params": ["case_id"]},
    "held_declined": {"title": "Held / declined transactions & outcomes", "builder": r_held,
                      "perms": ("reports.aml", "reports.executive")},
    "policy_history": {"title": "Threshold / policy history & changes", "builder": r_policy,
                       "perms": ("reports.aml", "reports.executive", "reports.system")},
    "analyst_sla": {"title": "Analyst workload & SLA performance", "builder": r_sla,
                    "perms": ("reports.aml", "reports.executive")},
    "model_health": {"title": "Model health, coverage & performance", "builder": r_models,
                     "perms": ("reports.aml", "reports.executive", "reports.system")},
    "channel_health": {"title": "Channel ingestion health & reconciliation", "builder": r_channels,
                       "perms": ("reports.system", "reports.executive")},
    "system_failures": {"title": "API, connector & system failure report", "builder": r_failures,
                        "perms": ("reports.system", "reports.executive")},
    "demo_usage": {"title": "Demo Mode usage report", "builder": r_demo, "perms": ("reports.system",)},
}


def catalogue_for(p: Principal) -> list[dict]:
    return [{"id": k, "title": v["title"], "params": v.get("params", [])} for k, v in CATALOGUE.items()
            if p.can(*v["perms"])]


def _check_assigned_scope(db: Session, p: Principal, report_type: str, params: dict) -> None:
    if p.can("reports.aml", "reports.executive"):
        return
    from .cases import assigned_entity_refs, get_case
    scope = assigned_entity_refs(db, p)
    if report_type == "investigation":
        get_case(db, p, params.get("case_id", ""))
    elif report_type == "transaction_decision" and params.get("tx_ref") not in scope["tx"]:
        raise PermissionError("transaction is not linked to one of your assigned cases")
    elif report_type == "customer_profile" and params.get("customer_ref") not in scope["customers"]:
        raise PermissionError("customer is not linked to one of your assigned cases")


# --------------------------------------------------------------------------- rendering
def _hex(color: str, fallback: str = "#10233F"):
    from reportlab.lib import colors
    try:
        return colors.HexColor(color or fallback)
    except (ValueError, TypeError):
        return colors.HexColor(fallback)


def _logo_file(b: dict):
    key = b.get("logo_key")
    if key:
        path = settings.assets_dir / key
        if path.exists():
            return path
    return None


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def render_pdf(doc: Doc, b: dict, number: str, generated_by: str, demo: bool, aml_report: bool,
               meta: Optional[dict] = None) -> bytes:
    """Formal, client-branded PDF: the institution's logo, name and colours - no vendor header.

    Page 1 carries a title block and a document-control table; every page has a running header (logo,
    institution, report title and number), a classification footer and "Page x of y". Sections are numbered;
    the document ends with the disclaimer and a prepared / reviewed / approved sign-off table.
    """
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame, KeepTogether, NextPageTemplate,
                                    PageTemplate, Paragraph, Spacer, Table, TableStyle)

    primary, accent = _hex(b["primary_color"]), _hex(b["accent_color"], "#0BAA9B")
    ink, muted, rule, soft = (colors.HexColor("#1B2738"), colors.HexColor("#5B6B7F"), colors.HexColor("#C9D3DE"),
                              colors.HexColor("#F3F6F9"))
    institution = b.get("display_name") or ""
    department = (b.get("aml_header_text") if aml_report and b.get("aml_header_text") else
                  b.get("department") or b.get("header_text") or "")
    footer_text = (b.get("aml_footer_text") if aml_report and b.get("aml_footer_text") else b.get("footer_text")) or ""
    classification = (b.get("classification") or "CONFIDENTIAL").upper()
    watermark = "DEMO DATA - NOT FOR OPERATIONAL USE" if demo else (b.get("watermark") or "")
    generated = utcnow().strftime(b.get("date_format") or "%d %b %Y %H:%M") + " UTC"
    page_fmt = b.get("page_number_format") or "Page {page} of {pages}"
    title_text = (b.get("report_title_prefix") + " " if b.get("report_title_prefix") else "") + doc.title
    logo = _logo_file(b)
    logo_reader = None
    if logo:
        try:
            logo_reader = ImageReader(str(logo))
        except Exception:  # noqa: BLE001
            logo_reader = None
    margin = 18 * mm
    char_w = 1.62 * mm                                   # ~width of one character at 7.8 pt Helvetica

    def natural_widths(sec: dict) -> list[float]:
        cols, rows_ = sec["columns"], sec["rows"][:300]
        out = []
        for i, c in enumerate(cols):
            longest_word = max((len(w_) for w_ in str(c).split()), default=4)
            vals = sorted(len(_fmt(r[i])) for r in rows_) or [4]
            typical = vals[int(len(vals) * 0.9) - 1 if len(vals) > 1 else 0]
            out.append(max(longest_word + 1, min(typical, 46), 4) * char_w + 5 * mm)
        return out

    tables = [s_ for s_ in doc.sections if "columns" in s_ and s_["rows"]]
    widest = max((sum(natural_widths(t_)) for t_ in tables), default=0)
    landscape_mode = widest > (A4[0] - 2 * margin)
    if landscape_mode:
        from reportlab.lib.pagesizes import landscape
        W, H = landscape(A4)
    else:
        W, H = A4

    def draw_logo(c, x, y_top, max_w, max_h):
        """Draw the logo with its top-left corner at (x, y_top); returns the width used."""
        if logo_reader is None:
            return 0
        iw, ih = logo_reader.getSize()
        scale = min(max_w / iw, max_h / ih)
        w, h = iw * scale, ih * scale
        c.drawImage(logo_reader, x, y_top - h, width=w, height=h, mask="auto")
        return w

    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved = []

        def showPage(self):
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved)
            for state in self._saved:
                self.__dict__.update(state)
                self._decorate(total)
                super().showPage()
            super().save()

        def _decorate(self, total: int):
            page = self._pageNumber
            self.saveState()
            # top colour rule
            self.setFillColor(primary)
            self.rect(0, H - 4 * mm, W, 4 * mm, stroke=0, fill=1)
            self.setFillColor(accent)
            self.rect(0, H - 4.8 * mm, W, 0.8 * mm, stroke=0, fill=1)
            if page == 1:
                used = draw_logo(self, margin, H - 10 * mm, 62 * mm, 20 * mm)
                if not used:
                    self.setFillColor(primary)
                    self.setFont("Helvetica-Bold", 15)
                    self.drawString(margin, H - 17 * mm, institution[:60])
                self.setFillColor(muted)
                self.setFont("Helvetica", 8.5)
                self.drawRightString(W - margin, H - 13 * mm, (b.get("legal_name") or institution)[:70])
                self.drawRightString(W - margin, H - 17.5 * mm, department[:80])
                self.setStrokeColor(rule)
                self.setLineWidth(0.6)
                self.line(margin, H - 33 * mm, W - margin, H - 33 * mm)
            else:
                used = draw_logo(self, margin, H - 8 * mm, 30 * mm, 9 * mm)
                self.setFillColor(primary if not used else muted)
                self.setFont("Helvetica-Bold" if not used else "Helvetica", 8.5)
                if not used:
                    self.drawString(margin, H - 13 * mm, institution[:60])
                self.setFillColor(muted)
                self.setFont("Helvetica", 8)
                self.drawRightString(W - margin, H - 11 * mm, title_text[:90])
                self.drawRightString(W - margin, H - 15 * mm, f"Ref. {number}")
                self.setStrokeColor(rule)
                self.setLineWidth(0.6)
                self.line(margin, H - 19 * mm, W - margin, H - 19 * mm)
            # footer
            self.setStrokeColor(rule)
            self.line(margin, 17 * mm, W - margin, 17 * mm)
            self.setFillColor(primary)
            self.setFont("Helvetica-Bold", 7.5)
            self.drawString(margin, 12.5 * mm, classification)
            self.setFillColor(muted)
            self.setFont("Helvetica", 7)
            self.drawString(margin + self.stringWidth(classification, "Helvetica-Bold", 7.5) + 3 * mm, 12.5 * mm,
                            f"{institution} · Ref. {number}"[:90])
            self.drawRightString(W - margin, 12.5 * mm, page_fmt.format(page=page, pages=total))
            notice = b.get("confidentiality_notice") or ""
            if footer_text:
                notice = f"{footer_text} · {notice}" if notice else footer_text
            self.setFont("Helvetica", 6.5)
            self.drawString(margin, 8.5 * mm, notice[:150])
            if watermark:
                self.setFillColor(colors.Color(0.78, 0.29, 0.33, alpha=0.10) if demo else
                                  colors.Color(0.4, 0.45, 0.5, alpha=0.08))
                self.setFont("Helvetica-Bold", 34 if len(watermark) < 24 else 26)
                self.translate(W / 2, H / 2)
                self.rotate(35)
                self.drawCentredString(0, 0, watermark)
            self.restoreState()

    ss = getSampleStyleSheet()
    base = ParagraphStyle("base", parent=ss["Normal"], fontName="Helvetica", fontSize=9, leading=12.5, textColor=ink)
    eyebrow = ParagraphStyle("eb", parent=base, fontName="Helvetica-Bold", fontSize=8, textColor=accent, leading=10,
                             spaceAfter=3)
    title = ParagraphStyle("t", parent=base, fontName="Helvetica-Bold", fontSize=19, leading=23, textColor=primary,
                           spaceAfter=3)
    sub = ParagraphStyle("s", parent=base, fontSize=10, textColor=muted, spaceAfter=10)
    h2 = ParagraphStyle("h2", parent=base, fontName="Helvetica-Bold", fontSize=11.5, leading=14, textColor=primary,
                        spaceBefore=12, spaceAfter=6)
    cell = ParagraphStyle("c", parent=base, fontSize=7.8, leading=9.8)
    cell_r = ParagraphStyle("cr", parent=cell, alignment=TA_RIGHT)
    head = ParagraphStyle("hd", parent=cell, textColor=colors.white, fontName="Helvetica-Bold")
    head_r = ParagraphStyle("hdr", parent=head, alignment=TA_RIGHT)
    lab = ParagraphStyle("lab", parent=cell, textColor=muted)
    small = ParagraphStyle("sm", parent=base, fontSize=7.5, leading=10, textColor=muted)
    avail = W - 2 * margin

    story: list = [Spacer(1, 2 * mm), Paragraph(escape(doc.meta.get("eyebrow") or "REPORT"), eyebrow),
                   Paragraph(escape(title_text), title)]
    if doc.subtitle:
        story.append(Paragraph(escape(doc.subtitle), sub))
    control = [("Reference", number), ("Classification", classification),
               ("Institution", b.get("legal_name") or institution), ("Workspace", "Demo (synthetic data)" if demo
                                                                      else "Live"),
               ("Generated", generated), ("Prepared by", generated_by)]
    for k_, v_ in (meta or {}).items():
        control.append((k_, v_))
    for k_, v_ in doc.meta.items():
        if k_ != "eyebrow":
            control.append((k_, v_))
    rows = []
    for i in range(0, len(control), 2):
        pair = control[i:i + 2]
        row = []
        for k_, v_ in pair:
            row += [Paragraph(escape(str(k_)), lab), Paragraph(f"<b>{escape(str(v_))}</b>", cell)]
        while len(row) < 4:
            row.append("")
        rows.append(row)
    ct = Table(rows, colWidths=[26 * mm, avail / 2 - 26 * mm, 26 * mm, avail / 2 - 26 * mm])
    ct.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, rule), ("INNERGRID", (0, 0), (-1, -1), 0.3, rule),
                            ("BACKGROUND", (0, 0), (0, -1), soft), ("BACKGROUND", (2, 0), (2, -1), soft),
                            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5)]))
    story += [ct, Spacer(1, 4 * mm)]

    n = 0

    def heading(text: str, need: float = 40):
        nonlocal n
        n += 1
        story.append(CondPageBreak(need * mm))
        story.append(Paragraph(f"{n}.&nbsp;&nbsp;{escape(text)}", h2))

    if doc.kpis or doc.summary:
        heading("Summary", 50)
        if doc.summary:
            story += [Paragraph(escape(doc.summary), base), Spacer(1, 3 * mm)]
        if doc.kpis:
            kstyle = ParagraphStyle("k", parent=cell, leading=14)
            per = 4
            kp = [[Paragraph(f"<font size=6.8 color='#5B6B7F'>{escape(k.upper())}</font><br/>"
                             f"<font size=12.5 color='{b['primary_color']}'><b>{escape(_fmt(v))}</b></font>", kstyle)
                   for k, v in doc.kpis[i:i + per]] for i in range(0, len(doc.kpis), per)]
            for row in kp:
                row += [""] * (per - len(row))
            t = Table(kp, colWidths=[avail / per] * per)
            t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, rule), ("INNERGRID", (0, 0), (-1, -1), 0.3, rule),
                                   ("LINEABOVE", (0, 0), (-1, 0), 1.6, accent),
                                   ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 7)]))
            story.append(t)
    for sec in doc.sections:
        heading(sec["heading"])
        if "text" in sec:
            for para in str(sec["text"]).split("\n"):
                story.append(Paragraph(escape(para), base))
            continue
        cols, rows_ = sec["columns"], sec["rows"][:1500]
        if not rows_:
            story.append(Paragraph("No records for the selected period and filters.", small))
            continue
        numeric = [all(_is_num(r[i]) or r[i] in (None, "") for r in rows_) and any(_is_num(r[i]) for r in rows_)
                   for i in range(len(cols))]
        data = [[Paragraph(escape(c), head_r if numeric[i] else head) for i, c in enumerate(cols)]] + \
               [[Paragraph(escape(_fmt(v))[:400], cell_r if numeric[i] else cell) for i, v in enumerate(r)]
                for r in rows_]
        # content-aware widths, scaled to the printable width
        nat = natural_widths(sec)
        widths = [avail * w_ / sum(nat) for w_ in nat]
        t = Table(data, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), primary),
                               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, soft]),
                               ("LINEBELOW", (0, 0), (-1, -1), 0.3, rule), ("BOX", (0, 0), (-1, -1), 0.5, rule),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(t)
        if len(sec["rows"]) > 1500:
            story.append(Paragraph(f"Showing 1,500 of {len(sec['rows']):,} rows - the CSV / Excel export contains "
                                   "every row.", small))
        if sec.get("note"):
            story += [Spacer(1, 1.5 * mm), Paragraph(f"<i>Note:</i> {escape(sec['note'])}", small)]

    heading("Disclaimer and approval", 70)
    disc = [Paragraph(escape(b.get("disclaimer") or ""), small)]
    if b.get("disclaimer_addendum") and aml_report:
        disc.append(Paragraph(escape(b["disclaimer_addendum"]), small))
    if demo:
        disc.append(Paragraph("This report was produced from the Demo workspace with synthetic customers and "
                              "transactions; its figures must not be presented as production results.", small))
    disc.append(Paragraph(escape(f"Generated by {generated_by} on {generated}. Reference {number}."), small))
    box = Table([[disc]], colWidths=[avail])
    box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, rule), ("BACKGROUND", (0, 0), (-1, -1), soft),
                             ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 6),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story += [box, Spacer(1, 5 * mm)]
    if b.get("signature_blocks", True):
        roles = [b.get(k) or d for k, d in (("prepared_by_label", "Prepared by"), ("reviewed_by_label", "Reviewed by"),
                                             ("approved_by_label", "Approved by"))]
        sig_rows = [[Paragraph("", head)] + [Paragraph(escape(r), head) for r in roles]]
        for label_ in ("Name", "Title", "Signature", "Date"):
            sig_rows.append([Paragraph(label_, lab)] + [Paragraph(escape(generated_by) if label_ == "Name" and i == 0
                                                                  else "", cell) for i in range(3)])
        sig = Table(sig_rows, colWidths=[24 * mm] + [(avail - 24 * mm) / 3] * 3,
                    rowHeights=[None, 9 * mm, 9 * mm, 13 * mm, 9 * mm])
        sig.setStyle(TableStyle([("BACKGROUND", (1, 0), (-1, 0), primary), ("BOX", (0, 0), (-1, -1), 0.5, rule),
                                 ("INNERGRID", (0, 0), (-1, -1), 0.3, rule), ("VALIGN", (0, 1), (-1, -1), "MIDDLE"),
                                 ("BACKGROUND", (0, 1), (0, -1), soft)]))
        story.append(KeepTogether([sig]))
    buf = io.BytesIO()
    tpl = BaseDocTemplate(buf, pagesize=(W, H), leftMargin=margin, rightMargin=margin, title=title_text,
                          author=institution, subject=department, creator=institution)
    first = Frame(margin, 24 * mm, avail, H - 24 * mm - 38 * mm, id="first", leftPadding=0, rightPadding=0)
    later = Frame(margin, 24 * mm, avail, H - 24 * mm - 25 * mm, id="later", leftPadding=0, rightPadding=0)
    tpl.addPageTemplates([PageTemplate(id="First", frames=[first]), PageTemplate(id="Later", frames=[later])])
    tpl.build([NextPageTemplate("Later")] + story, canvasmaker=NumberedCanvas)
    return buf.getvalue()


def render_csv(doc: Doc, demo: bool) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    if demo:
        w.writerow(["DEMO DATA - NOT FOR OPERATIONAL USE"])
    main = doc.main_table
    if main:
        w.writerow(main["columns"])
        for r in main["rows"]:
            w.writerow([_fmt(v) if not isinstance(v, (int, float)) else v for v in r])
    return out.getvalue().encode("utf-8-sig")


def render_xlsx(doc: Doc, b: dict, demo: bool, number: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append([b.get("legal_name") or b["display_name"]])
    ws.append([doc.title])
    ws.append([doc.subtitle])
    ws.append([f"Report {number}", utcnow().strftime("%Y-%m-%d %H:%M UTC")])
    if demo:
        ws.append(["DEMO DATA - NOT FOR OPERATIONAL USE"])
    ws.append([])
    for k, v in doc.kpis:
        ws.append([k, _fmt(v)])
    ws["A1"].font = Font(bold=True, size=14)
    fill = PatternFill("solid", fgColor=(b.get("primary_color") or "#10233F").lstrip("#"))
    used = set()
    for sec in doc.sections:
        if "columns" not in sec:
            continue
        name = "".join(ch for ch in sec["heading"] if ch.isalnum() or ch == " ")[:28] or "Data"
        while name in used:
            name = name[:25] + str(len(used))
        used.add(name)
        sh = wb.create_sheet(name)
        sh.append(sec["columns"])
        for cell in sh[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = fill
        for r in sec["rows"]:
            sh.append([v if isinstance(v, (int, float)) else _fmt(v) for v in r])
        for col in sh.columns:
            sh.column_dimensions[col[0].column_letter].width = min(48, max(10, max(len(str(c.value or ""))
                                                                                  for c in col[:200]) + 2))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def generate(db: Session, p: Principal, report_type: str, fmt: str, params: dict) -> ReportJob:
    spec = CATALOGUE.get(report_type)
    if spec is None:
        raise LookupError(f"unknown report {report_type}")
    if not p.can(*spec["perms"]):
        raise PermissionError("you are not permitted to generate this report")
    if fmt not in ("pdf", "csv", "xlsx"):
        raise ValueError("format must be pdf, csv or xlsx")
    for need in spec.get("params", []):
        if not params.get(need):
            raise ValueError(f"parameter '{need}' is required")
    _check_assigned_scope(db, p, report_type, params)
    tenant = db.get(Tenant, p.tenant_id)
    b = branding(tenant)
    year = utcnow().year
    number = f"{b.get('numbering_prefix') or 'VS'}-{year}-{next_seq(db, p.tenant_id, f'report:{year}'):05d}"
    job = ReportJob(id=f"{p.tenant_id}-RPT-{number}", tenant_id=p.tenant_id, space=p.space, number=number,
                    report_type=report_type, title=spec["title"], fmt=fmt, params=params, status="RUNNING",
                    created_by=p.user_id, created_by_name=p.display)
    db.add(job)
    db.commit()
    try:
        doc = spec["builder"](db, p, params, tenant)
        demo = p.space == "DEMO"
        if fmt == "pdf":
            from ..models import ROLE_LABELS
            start, end = _range(params)
            meta = {"Period": f"{start:%d %b %Y} - {(end - timedelta(seconds=1)):%d %b %Y}"}
            for key, label_ in (("tx_ref", "Transaction"), ("customer_ref", "Customer"), ("case_id", "Case")):
                if params.get(key):
                    meta[label_] = params[key]
            by = f"{p.display} ({ROLE_LABELS.get(p.role, p.role)})" if p.kind == "user" else p.display
            data = render_pdf(doc, b, number, by, demo, report_type in ("investigation", "alert_case_register",
                                                                        "analyst_sla", "held_declined"), meta)
        elif fmt == "csv":
            data = render_csv(doc, demo)
        else:
            data = render_xlsx(doc, b, demo, number)
        folder = settings.reports_dir / p.tenant_id / ("demo" if demo else "live")
        folder.mkdir(parents=True, exist_ok=True)
        key = f"{p.tenant_id}/{'demo' if demo else 'live'}/{number}_{report_type}.{fmt}"
        (settings.reports_dir / key).write_bytes(data)
        job.storage_key, job.size, job.status = key, len(data), "DONE"
    except Exception as exc:  # noqa: BLE001
        job.status, job.error = "FAILED", f"{type(exc).__name__}: {exc}"[:500]
        metrics.error("report", job.error)
    db.commit()
    return job


def report_path(job: ReportJob) -> Optional[str]:
    if not job.storage_key:
        return None
    path = settings.reports_dir / job.storage_key
    return str(path) if os.path.exists(path) else None


__all__ = ["CATALOGUE", "catalogue_for", "generate", "branding", "report_path", "performance", "DEFAULT_BRANDING",
           "TENANT_BRANDING_FIELDS", "AML_BRANDING_FIELDS", "Callable"]
