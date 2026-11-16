"""PDF / CSV reports for suspicious transactions (ReportLab)."""
from __future__ import annotations

import csv
import os
from collections import Counter
from datetime import datetime
from typing import Iterable
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .config import Config
from .engine.banksealer import PROFILE_NAMES
from .models import TX_TYPE_LABELS, ScoreResult, Transaction

RISK_COLORS = {"LOW": "#2e7d32", "MEDIUM": "#f9a825", "HIGH": "#ef6c00", "CRITICAL": "#c62828"}
ACTIONS = {
    "CRITICAL": ["Block the transaction / recall the funds immediately if already executed.",
                 "Suspend online-banking credentials and active sessions of the customer.",
                 "Contact the customer through a verified out-of-band channel.",
                 "Notify the beneficiary bank and evaluate a suspicious activity report (SAR)."],
    "HIGH": ["Hold the transaction pending verification.",
             "Call the customer on the registered phone number to confirm the operation.",
             "Review the customer's other recent operations and active sessions."],
    "MEDIUM": ["Review the transaction within the analyst SLA.",
               "Verify the beneficiary and connection details against the customer's history.",
               "Release or escalate after review; record the outcome as feedback."],
    "LOW": ["No action required."],
}


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle("Small", parent=ss["Normal"], fontSize=8, leading=10, textColor=colors.HexColor("#555555")))
    ss.add(ParagraphStyle("Cell", parent=ss["Normal"], fontSize=8.5, leading=11))
    ss.add(ParagraphStyle("H2b", parent=ss["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4,
                          textColor=colors.HexColor("#1a237e")))
    return ss


def _table(rows, widths, header=True):
    t = Table(rows, colWidths=widths, hAlign="LEFT")
    style = [("FONTSIZE", (0, 0), (-1, -1), 8.5), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c5cae9")),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 3)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eaf6")), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
    t.setStyle(TableStyle(style))
    return t


def _score_bars(result: ScoreResult, threshold: float) -> Drawing:
    items = [("Local (HBOS)", result.local_score), ("Temporal", result.temporal_score),
             ("Global (CBLOF)", result.global_score), ("Suspicion score", result.combined_score)]
    width, bar_w, h = 170 * mm, 110 * mm, 14
    d = Drawing(width, len(items) * (h + 6) + 6)
    for i, (label, value) in enumerate(items):
        y = d.height - (i + 1) * (h + 6)
        d.add(String(0, y + 4, label, fontSize=8.5, fontName="Helvetica-Bold" if i == 3 else "Helvetica"))
        d.add(Rect(35 * mm, y, bar_w, h, fillColor=colors.HexColor("#eeeeee"), strokeColor=None))
        level = "CRITICAL" if value >= 0.8 else "HIGH" if value >= 0.65 else "MEDIUM" if value >= threshold else "LOW"
        d.add(Rect(35 * mm, y, bar_w * max(0.0, min(1.0, value)), h, fillColor=colors.HexColor(RISK_COLORS[level]),
                   strokeColor=None))
        d.add(String(35 * mm + bar_w + 4, y + 4, f"{value:.2f}", fontSize=8.5))
    x = 35 * mm + bar_w * threshold
    d.add(Rect(x - 0.5, 0, 1, d.height, fillColor=colors.black, strokeColor=None))
    d.add(String(x + 2, d.height - 8, f"threshold {threshold:.2f}", fontSize=7))
    return d


class ReportGenerator:
    def __init__(self, config: Config, out_dir: str | None = None):
        self.config = config
        self.out_dir = out_dir or config.section("app").get("reports_dir", "reports")
        os.makedirs(self.out_dir, exist_ok=True)

    # ------------------------------------------------------- single report
    def transaction_report(self, tx: Transaction, result: ScoreResult, customer: dict | None = None,
                           ranked_reasons=None) -> str:
        ss = _styles()
        th = self.config.thresholds_for(tx.bank_id)
        path = os.path.join(self.out_dir, f"report_{tx.tx_id}.pdf")
        doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                                topMargin=16 * mm, bottomMargin=16 * mm,
                                title=f"BankSealer report {tx.tx_id}", author="BankSealer")
        story = []
        color = RISK_COLORS.get(result.risk_level, "#333333")
        story.append(Paragraph("Suspicious Transaction Report", ss["Title"]))
        story.append(Paragraph(
            f"{escape(self.config.bank_name(tx.bank_id))} &nbsp;|&nbsp; Report ID SR-{escape(tx.tx_id)} &nbsp;|&nbsp; "
            f"generated {datetime.now():%Y-%m-%d %H:%M}", ss["Small"]))
        story.append(Spacer(1, 6))
        story.append(Paragraph(
            f'<font color="{color}" size="14"><b>{result.risk_level} RISK</b></font> &nbsp; suspicion score '
            f'<b>{result.combined_score:.2f}</b> (bank threshold {float(th.get("suspicious", 0.5)):.2f}) '
            f'&nbsp; analyst status: <b>{escape(result.status)}</b>', ss["Normal"]))

        story.append(Paragraph("Transaction", ss["H2b"]))
        rows = [["Field", "Value", "Field", "Value"],
                ["Transaction ID", tx.tx_id, "Type", TX_TYPE_LABELS.get(tx.tx_type, tx.tx_type)],
                ["Customer", tx.user_id, "Date / time", f"{tx.timestamp:%Y-%m-%d %H:%M:%S}"],
                ["Amount", f"{tx.amount:,.2f} {tx.currency}", "Channel", tx.channel or "-"],
                ["Beneficiary", tx.beneficiary or "-", "Beneficiary country", tx.iban_cc or "-"],
                ["IP address", tx.ip or "-", "Country / ASN", tx.cc_asn or "-"],
                ["Session", tx.session_id or "-", "Bank / company", tx.bank_id]]
        story.append(_table(rows, [32 * mm, 55 * mm, 32 * mm, 55 * mm]))

        story.append(Paragraph("BankSealer profile scores", ss["H2b"]))
        story.append(_score_bars(result, float(th.get("suspicious", 0.5))))
        story.append(Paragraph(
            f"Raw values: HBOS {result.raw.get('local_hbos', 0):.2f} (history {result.raw.get('local_history', 0)} tx), "
            f"temporal excess {result.raw.get('temporal_excess', 0):.2f}, CBLOF {result.raw.get('global_cblof', 0):.2f}, "
            f"peer group #{result.cluster}{' - customer undertrained, peer-group statistics used' if result.undertrained else ''}.",
            ss["Small"]))

        story.append(Paragraph("Why this transaction is suspicious", ss["H2b"]))
        reasons = ranked_reasons if ranked_reasons is not None else (result.rule_reasons + sorted(
            result.reasons, key=lambda r: r.contribution, reverse=True))
        rows = [["#", "Source", "Finding", "Impact"]]
        for i, r in enumerate(reasons, 1):
            rows.append([str(i), PROFILE_NAMES.get(r.profile, r.profile), Paragraph(escape(r.message), ss["Cell"]),
                         f"+{r.contribution:.2f}" if r.contribution else "alert"])
        if len(rows) == 1:
            rows.append(["-", "-", "No individual finding above the reporting level.", "-"])
        story.append(_table(rows, [7 * mm, 27 * mm, 122 * mm, 18 * mm]))

        if customer:
            story.append(Paragraph("Customer behaviour (learned profile)", ss["H2b"]))
            story.extend(self._customer_section(customer, ss))

        story.append(Paragraph("Recommended actions", ss["H2b"]))
        for action in ACTIONS.get(result.risk_level, []):
            story.append(Paragraph(f"• {escape(action)}", ss["Normal"]))
        story.append(Spacer(1, 10))
        story.append(Paragraph(
            "Method: BankSealer (Carminati et al., 2015) - local profile: per-customer histogram-based outlier score "
            "(HBOS); temporal profile: per-customer thresholds (mean + k·std) on activity aggregated over a sliding "
            "window; global profile: iterative DBSCAN on Mahalanobis-whitened spending patterns scored with CBLOF. "
            "Thresholds are configured per bank/company.", ss["Small"]))
        doc.build(story)
        return path

    def _customer_section(self, customer: dict, ss) -> list:
        out = []
        local = customer.get("local") or {}
        rows = [["Operation type", "Past ops", "Median", "P95", "Usual beneficiaries", "Usual connections"]]
        for tx_type, info in local.items():
            ben_key = {"bank_transfer": "top_iban", "phone_recharge": "top_phone_number", "prepaid_card": "top_card_id"}[tx_type]
            rows.append([TX_TYPE_LABELS.get(tx_type, tx_type), str(info.get("count", 0)),
                         f"{info.get('amount_median', 0):,.2f}", f"{info.get('amount_p95', 0):,.2f}",
                         Paragraph(escape(", ".join(f"{v} ({c})" for v, c in info.get(ben_key, []))), ss["Cell"]),
                         Paragraph(escape(", ".join(f"{v} ({c})" for v, c in info.get("top_cc_asn", []))), ss["Cell"])])
        if len(rows) > 1:
            out.append(_table(rows, [27 * mm, 15 * mm, 22 * mm, 18 * mm, 52 * mm, 40 * mm]))
        temporal = customer.get("temporal") or {}
        if temporal:
            mean = temporal.get("mean", {})
            out.append(Spacer(1, 4))
            out.append(Paragraph(
                f"Temporal profile ({temporal.get('samples', 0)} samples, home country {temporal.get('home_country') or '-'}): "
                f"on average {mean.get('tx_count', 0):.1f} operations and {mean.get('total_amount', 0):,.2f} per window, "
                f"{mean.get('distinct_beneficiaries', 0):.1f} distinct beneficiaries.", ss["Cell"]))
        glob = customer.get("global") or {}
        if glob:
            out.append(Paragraph(
                f"Global profile: peer group #{glob.get('cluster')} (size {glob.get('cluster_size') or '-'}), "
                f"customer outlier score {glob.get('score') or 0:.2f}.", ss["Cell"]))
        return out

    # ------------------------------------------------------- summary report
    def summary_report(self, pairs: Iterable[tuple[Transaction, ScoreResult]], title: str = "Fraud Analysis Summary") -> str:
        pairs = list(pairs)
        ss = _styles()
        path = os.path.join(self.out_dir, f"summary_{datetime.now():%Y%m%d_%H%M%S}.pdf")
        doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                                topMargin=15 * mm, bottomMargin=15 * mm, title=title)
        flagged = [(t, r) for t, r in pairs if r.suspicious]
        story = [Paragraph(title, ss["Title"]),
                 Paragraph(f"generated {datetime.now():%Y-%m-%d %H:%M}", ss["Small"]), Spacer(1, 6)]
        customers = {t.user_id for t, _ in pairs}
        rows = [["Transactions", "Customers", "Suspicious", "Rate", "Critical", "High", "Medium"]]
        levels = Counter(r.risk_level for _, r in flagged)
        rows.append([f"{len(pairs):,}", f"{len(customers):,}", f"{len(flagged):,}",
                     f"{(len(flagged) / len(pairs) if pairs else 0):.2%}", str(levels.get("CRITICAL", 0)),
                     str(levels.get("HIGH", 0)), str(levels.get("MEDIUM", 0))])
        story.append(_table(rows, [26 * mm] * 7))

        story.append(Paragraph("By bank / company", ss["H2b"]))
        rows = [["Bank", "Name", "Transactions", "Suspicious", "Threshold"]]
        for bank in sorted({t.bank_id for t, _ in pairs}):
            n = sum(1 for t, _ in pairs if t.bank_id == bank)
            s = sum(1 for t, _ in flagged if t.bank_id == bank)
            rows.append([bank, self.config.bank_name(bank), f"{n:,}", f"{s:,}",
                         f"{float(self.config.thresholds_for(bank).get('suspicious', 0.5)):.2f}"])
        story.append(_table(rows, [25 * mm, 65 * mm, 28 * mm, 28 * mm, 25 * mm]))

        story.append(Paragraph("Suspicious transactions (highest score first)", ss["H2b"]))
        rows = [["Score", "Risk", "Time", "Customer", "Type", "Amount", "Main reason"]]
        for t, r in sorted(flagged, key=lambda p: p[1].combined_score, reverse=True)[:200]:
            ranked = r.rule_reasons + sorted(r.reasons, key=lambda x: x.contribution, reverse=True)
            main = ranked[0].message if ranked else ""
            rows.append([f"{r.combined_score:.2f}", r.risk_level, f"{t.timestamp:%m-%d %H:%M}", t.user_id,
                         TX_TYPE_LABELS.get(t.tx_type, t.tx_type), f"{t.amount:,.2f}",
                         Paragraph(escape(main[:180]), ss["Cell"])])
        story.append(_table(rows, [12 * mm, 18 * mm, 20 * mm, 16 * mm, 22 * mm, 20 * mm, 72 * mm]))
        doc.build(story)
        return path

    # ----------------------------------------------------------------- csv
    def export_csv(self, pairs: Iterable[tuple[Transaction, ScoreResult]], path: str | None = None) -> str:
        path = path or os.path.join(self.out_dir, f"scores_{datetime.now():%Y%m%d_%H%M%S}.csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["tx_id", "timestamp", "bank_id", "user_id", "tx_type", "amount", "currency", "beneficiary",
                        "cc_asn", "local_score", "temporal_score", "global_score", "suspicion_score", "risk_level",
                        "suspicious", "status", "explanation"])
            for t, r in pairs:
                w.writerow([t.tx_id, t.timestamp, t.bank_id, t.user_id, t.tx_type, t.amount, t.currency, t.beneficiary,
                            t.cc_asn, r.local_score, r.temporal_score, r.global_score, r.combined_score, r.risk_level,
                            int(r.suspicious), r.status, r.explanation])
        return path
