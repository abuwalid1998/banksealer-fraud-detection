"""Reusable widgets, table models and HTML helpers for the desktop UI."""
from __future__ import annotations

from html import escape

import matplotlib

matplotlib.use("QtAgg")
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt  # noqa: E402
from PySide6.QtGui import QBrush, QColor, QFont  # noqa: E402
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout  # noqa: E402

from ..engine.banksealer import PROFILE_NAMES  # noqa: E402
from ..models import RISK_LEVELS, TX_TYPE_LABELS  # noqa: E402

RISK_COLORS = {"LOW": "#2e7d32", "MEDIUM": "#f9a825", "HIGH": "#ef6c00", "CRITICAL": "#c62828"}
RISK_BG = {"LOW": None, "MEDIUM": "#fff8e1", "HIGH": "#fff3e0", "CRITICAL": "#ffebee"}
STATUS_LABELS = {"open": "Open", "fraud": "Confirmed fraud", "legit": "Legitimate"}


def score_color(value: float, threshold: float = 0.5) -> str:
    if value >= 0.8:
        return RISK_COLORS["CRITICAL"]
    if value >= 0.65:
        return RISK_COLORS["HIGH"]
    if value >= threshold:
        return RISK_COLORS["MEDIUM"]
    return RISK_COLORS["LOW"]


class KpiCard(QFrame):
    def __init__(self, title: str, accent: str = "#1a237e"):
        super().__init__()
        self.setObjectName("kpi")
        self.setStyleSheet(f"#kpi {{ background: white; border: 1px solid #dfe3f0; border-radius: 8px; "
                           f"border-left: 5px solid {accent}; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.title = QLabel(title)
        self.title.setStyleSheet("color: #5c6370; font-size: 11px;")
        self.value = QLabel("-")
        self.value.setStyleSheet(f"color: {accent}; font-size: 22px; font-weight: 600;")
        layout.addWidget(self.title)
        layout.addWidget(self.value)

    def set(self, text: str) -> None:
        self.value.setText(text)


class MplCanvas(FigureCanvasQTAgg):
    def __init__(self, width=8, height=5, dpi=96):
        self.figure = Figure(figsize=(width, height), dpi=dpi, tight_layout=True)
        super().__init__(self.figure)


def refilter(proxy: QSortFilterProxyModel, values: dict) -> None:
    """Change filter attributes and re-run the filter (Qt >= 6.9 API when available)."""
    if hasattr(proxy, "beginFilterChange"):
        proxy.beginFilterChange()
        for k, v in values.items():
            setattr(proxy, k, v)
        proxy.endFilterChange()
    else:
        for k, v in values.items():
            setattr(proxy, k, v)
        proxy.invalidateFilter()


# ---------------------------------------------------------------- tables
class TransactionModel(QAbstractTableModel):
    COLUMNS = ["Time", "Transaction", "Customer", "Bank", "Type", "Amount", "Beneficiary", "Country/ASN",
               "Local", "Temporal", "Global", "Score", "Risk", "Status"]

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.rows = []

    def refresh(self) -> None:
        self.beginResetModel()
        self.rows = list(self.controller.pairs)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COLUMNS[section]
        return None

    def pair(self, row: int):
        return self.rows[row]

    def data(self, index, role=Qt.DisplayRole):
        tx, r = self.rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            return [
                f"{tx.timestamp:%Y-%m-%d %H:%M}", tx.tx_id, tx.user_id, tx.bank_id,
                TX_TYPE_LABELS.get(tx.tx_type, tx.tx_type), f"{tx.amount:,.2f}", tx.beneficiary or "",
                tx.cc_asn or "", f"{r.local_score:.2f}", f"{r.temporal_score:.2f}", f"{r.global_score:.2f}",
                f"{r.combined_score:.2f}", r.risk_level, STATUS_LABELS.get(r.status, r.status),
            ][col]
        if role == Qt.UserRole:  # sort keys
            return [tx.timestamp, tx.tx_id, tx.user_id, tx.bank_id, tx.tx_type, tx.amount, tx.beneficiary or "",
                    tx.cc_asn or "", r.local_score, r.temporal_score, r.global_score, r.combined_score,
                    RISK_LEVELS.index(r.risk_level), r.status][col]
        if role == Qt.TextAlignmentRole and col in (5, 8, 9, 10, 11):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.BackgroundRole:
            if col in (8, 9, 10, 11):
                value = [r.local_score, r.temporal_score, r.global_score, r.combined_score][col - 8]
                c = QColor(score_color(value))
                c.setAlpha(int(40 + 150 * value))
                return QBrush(c)
            if col == 12 and RISK_BG.get(r.risk_level):
                return QBrush(QColor(RISK_BG[r.risk_level]))
        if role == Qt.ForegroundRole and col == 12:
            return QBrush(QColor(RISK_COLORS[r.risk_level]))
        if role == Qt.FontRole and (col == 12 or (col == 11 and r.suspicious)):
            f = QFont()
            f.setBold(True)
            return f
        return None


class TransactionFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.bank = ""
        self.tx_type = ""
        self.min_level = 0
        self.only_suspicious = False
        self.user = ""
        self.status = ""
        self.setSortRole(Qt.UserRole)

    def set(self, **kwargs) -> None:
        refilter(self, kwargs)

    def filterAcceptsRow(self, row, parent):
        tx, r = self.sourceModel().rows[row]
        if self.user and tx.user_id != self.user:
            return False
        if self.only_suspicious and not r.suspicious:
            return False
        if self.bank and tx.bank_id != self.bank:
            return False
        if self.tx_type and tx.tx_type != self.tx_type:
            return False
        if self.status and r.status != self.status:
            return False
        if self.min_level and RISK_LEVELS.index(r.risk_level) < self.min_level:
            return False
        if self.text:
            t = self.text.lower()
            hay = f"{tx.tx_id} {tx.user_id} {tx.beneficiary or ''} {tx.cc_asn or ''} {tx.ip or ''}".lower()
            if t not in hay:
                return False
        return True

    def lessThan(self, left, right):
        a, b = left.data(Qt.UserRole), right.data(Qt.UserRole)
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


class CustomerModel(QAbstractTableModel):
    COLUMNS = ["Customer", "Bank", "Transactions", "Suspicious", "Confirmed fraud", "Max score", "Avg score",
               "Total amount", "Last activity", "Peer group", "Global score"]

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.rows: list[dict] = []

    def refresh(self) -> None:
        self.beginResetModel()
        self.rows = sorted(self.controller.customers(), key=lambda c: c["max_score"], reverse=True)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COLUMNS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        c = self.rows[index.row()]
        col = index.column()
        keys = ["user_id", "bank_id", "transactions", "suspicious", "confirmed_fraud", "max_score", "avg_score",
                "amount", "last", "cluster", "global_score"]
        value = c[keys[col]]
        if role == Qt.UserRole:
            return value if value is not None else -1
        if role == Qt.DisplayRole:
            if value is None:
                return "-"
            if keys[col] in ("max_score", "avg_score", "global_score"):
                return f"{value:.2f}"
            if keys[col] == "amount":
                return f"{value:,.2f}"
            if keys[col] == "last":
                return f"{value:%Y-%m-%d %H:%M}"
            if keys[col] == "cluster":
                return "noise" if value < 0 else f"#{value}"
            return str(value)
        if role == Qt.BackgroundRole and keys[col] in ("max_score", "global_score") and value is not None:
            color = QColor(score_color(value))
            color.setAlpha(int(40 + 150 * value))
            return QBrush(color)
        if role == Qt.ForegroundRole and keys[col] == "suspicious" and value:
            return QBrush(QColor(RISK_COLORS["HIGH"]))
        return None


class CustomerFilter(QSortFilterProxyModel):
    def set(self, **kwargs) -> None:
        refilter(self, kwargs)

    def __init__(self):
        super().__init__()
        self.text = ""
        self.only_flagged = False
        self.setSortRole(Qt.UserRole)

    def filterAcceptsRow(self, row, parent):
        c = self.sourceModel().rows[row]
        if self.only_flagged and not c["suspicious"]:
            return False
        return not self.text or self.text.lower() in f"{c['user_id']} {c['bank_id']}".lower()

    def lessThan(self, left, right):
        a, b = left.data(Qt.UserRole), right.data(Qt.UserRole)
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


# ------------------------------------------------------------------ html
def bar(value: float, threshold: float, width: int = 40) -> str:
    """Horizontal bar drawn with background-coloured spaces (renders reliably in QTextBrowser)."""
    filled = int(round(width * min(max(value, 0.0), 1.0)))
    color = score_color(value, threshold)
    return (f'<span style="font-family:monospace; background-color:{color}">{"&nbsp;" * filled}</span>'
            f'<span style="font-family:monospace; background-color:#e3e6ec">{"&nbsp;" * (width - filled)}</span>'
            f'&nbsp;<b>{value:.2f}</b>')


def transaction_html(controller, tx, r) -> str:
    th = controller.config.thresholds_for(tx.bank_id)
    t = float(th.get("suspicious", 0.5))
    color = RISK_COLORS[r.risk_level]
    verdict = "SUSPICIOUS" if r.suspicious else "Normal"
    html = [f'<h3 style="margin:0">{escape(tx.tx_id)} &nbsp;<span style="color:{color}">{r.risk_level} · {verdict}</span></h3>',
            f'<p style="color:#5c6370;margin-top:2px">{escape(controller.config.bank_name(tx.bank_id))} · '
            f'customer <b>{escape(tx.user_id)}</b> · {tx.timestamp:%Y-%m-%d %H:%M:%S} · status '
            f'<b>{STATUS_LABELS.get(r.status, r.status)}</b></p>',
            '<table cellspacing="2">']
    for label, value in (("Local (HBOS)", r.local_score), ("Temporal", r.temporal_score),
                         ("Global (CBLOF)", r.global_score), ("<b>Suspicion score</b>", r.combined_score)):
        html.append(f"<tr><td>{label}&nbsp;</td><td style='white-space:nowrap'>{bar(value, t)}</td></tr>")
    html.append(f'</table><p style="color:#5c6370">Bank threshold {t:.2f} (high {float(th.get("high", 0.65)):.2f}, '
                f'critical {float(th.get("critical", 0.8)):.2f})'
                f'{" · customer undertrained: compared with peer group" if r.undertrained else ""}</p>')
    fields = [("Type", TX_TYPE_LABELS.get(tx.tx_type, tx.tx_type)), ("Amount", f"{tx.amount:,.2f} {tx.currency}"),
              ("Beneficiary", tx.beneficiary or "-"), ("Beneficiary country", tx.iban_cc or "-"),
              ("IP", tx.ip or "-"), ("Country/ASN", tx.cc_asn or "-"), ("Channel", tx.channel or "-"),
              ("Peer group", "-" if r.cluster is None else f"#{r.cluster}")]
    html.append('<table cellspacing="0" cellpadding="3" style="border:1px solid #e0e0e0">')
    for i in range(0, len(fields), 2):
        a, b = fields[i], fields[i + 1]
        html.append(f'<tr><td style="color:#5c6370">{a[0]}</td><td><b>{escape(a[1])}</b></td>'
                    f'<td style="color:#5c6370">&nbsp;&nbsp;{b[0]}</td><td><b>{escape(b[1])}</b></td></tr>')
    html.append("</table>")
    ranked = controller.engine.ranked_reasons(r)
    title = "Why it is suspicious" if r.suspicious else "Main deviations from the customer's profile"
    html.append(f"<h4 style='margin-bottom:2px'>{title}</h4>")
    if ranked:
        html.append("<ol style='margin-top:2px'>")
        for reason in ranked[: int(controller.config.section("engine").get("max_reasons", 6)) + 2]:
            impact = f" <span style='color:#5c6370'>(+{reason.contribution:.2f})</span>" if reason.contribution else ""
            html.append(f"<li><b>{PROFILE_NAMES.get(reason.profile, reason.profile)}</b>: {escape(reason.message)}{impact}</li>")
        html.append("</ol>")
    else:
        html.append("<p>No individual feature deviates from the customer's usual behaviour.</p>")
    path = controller.report_paths.get(tx.tx_id)
    if path:
        html.append(f"<p style='color:#5c6370'>Report: {escape(path)}</p>")
    return "".join(html)


def customer_html(controller, user_id: str) -> str:
    summary = controller.engine.customer_summary(user_id)
    html = [f"<h3 style='margin:0'>Customer {escape(user_id)}</h3>"]
    glob = summary.get("global") or {}
    if glob:
        cluster = glob.get("cluster")
        html.append(f"<p>Peer group <b>{'noise' if cluster is not None and cluster < 0 else '#' + str(cluster)}</b> "
                    f"(size {glob.get('cluster_size') or '-'}) · global outlier score "
                    f"<b>{(glob.get('score') or 0):.2f}</b></p>")
    temporal = summary.get("temporal") or {}
    if temporal:
        m = temporal.get("mean", {})
        html.append(f"<p>Home country <b>{escape(str(temporal.get('home_country') or '-'))}</b> · per 24h window on "
                    f"average <b>{m.get('tx_count', 0):.1f}</b> operations, <b>{m.get('total_amount', 0):,.2f}</b> "
                    f"total, {m.get('distinct_beneficiaries', 0):.1f} beneficiaries "
                    f"({temporal.get('samples', 0)} samples{', undertrained' if temporal.get('undertrained') else ''})</p>")
    local = summary.get("local") or {}
    if local:
        html.append("<table cellspacing='0' cellpadding='3' style='border:1px solid #e0e0e0'><tr style='background:#e8eaf6'>"
                    "<th>Type</th><th>Ops</th><th>Median</th><th>P95</th><th>Usual beneficiaries</th><th>Connections</th></tr>")
        for tx_type, info in local.items():
            ben = {"bank_transfer": "top_iban", "phone_recharge": "top_phone_number", "prepaid_card": "top_card_id"}[tx_type]
            html.append(f"<tr><td>{TX_TYPE_LABELS[tx_type]}</td><td>{info.get('count', 0)}</td>"
                        f"<td>{info.get('amount_median', 0):,.2f}</td><td>{info.get('amount_p95', 0):,.2f}</td>"
                        f"<td>{escape(', '.join(f'{v} ({c})' for v, c in info.get(ben, [])))}</td>"
                        f"<td>{escape(', '.join(f'{v} ({c})' for v, c in info.get('top_cc_asn', [])))}</td></tr>")
        html.append("</table>")
    if not (glob or temporal or local):
        html.append("<p>No learned profile yet (profiles are built from history).</p>")
    return "".join(html)
