"""Tabs of the main window: dashboard, transactions, alerts, customers, channels, configuration."""
from __future__ import annotations

from collections import Counter, defaultdict

import numpy as np
import yaml
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
                               QTableView, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout,
                               QWidget)

from ..channels import CHANNEL_TYPES
from ..config import Config
from ..models import TX_TYPE_LABELS, TX_TYPES
from .widgets import (RISK_COLORS, CustomerFilter, CustomerModel, KpiCard, MplCanvas, TransactionFilter,
                      TransactionModel, customer_html, transaction_html)


def _table(view: QTableView) -> QTableView:
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setSelectionMode(QAbstractItemView.SingleSelection)
    view.setSortingEnabled(True)
    view.setAlternatingRowColors(True)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(22)
    view.horizontalHeader().setStretchLastSection(True)
    view.setEditTriggers(QAbstractItemView.NoEditTriggers)
    return view


# ================================================================ dashboard
class DashboardTab(QWidget):
    open_transaction = Signal(str)

    def __init__(self, controller):
        super().__init__()
        self.c = controller
        layout = QVBoxLayout(self)
        grid = QGridLayout()
        self.cards = {
            "tx": KpiCard("Transactions scored", "#1a237e"),
            "customers": KpiCard("Customers", "#3949ab"),
            "suspicious": KpiCard("Suspicious", RISK_COLORS["HIGH"]),
            "rate": KpiCard("Suspicious rate", RISK_COLORS["MEDIUM"]),
            "critical": KpiCard("Critical", RISK_COLORS["CRITICAL"]),
            "open": KpiCard("Open alerts", "#6a1b9a"),
            "avg": KpiCard("Avg suspicion score", "#00838f"),
        }
        for i, card in enumerate(self.cards.values()):
            grid.addWidget(card, 0, i)
        layout.addLayout(grid)
        self.engine_label = QLabel()
        self.engine_label.setStyleSheet("color:#5c6370")
        layout.addWidget(self.engine_label)

        split = QSplitter(Qt.Vertical)
        self.canvas = MplCanvas(10, 5.2)
        split.addWidget(self.canvas)
        box = QGroupBox("Latest suspicious transactions (double-click to open)")
        bl = QVBoxLayout(box)
        self.latest = QTableWidget(0, 8)
        self.latest.setHorizontalHeaderLabels(["Time", "Transaction", "Customer", "Bank", "Type", "Amount", "Score",
                                               "Main reason"])
        self.latest.verticalHeader().setVisible(False)
        self.latest.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.latest.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.latest.horizontalHeader().setStretchLastSection(True)
        self.latest.cellDoubleClicked.connect(lambda row, _: self.open_transaction.emit(self.latest.item(row, 1).text()))
        bl.addWidget(self.latest)
        split.addWidget(box)
        split.setSizes([520, 260])
        layout.addWidget(split, 1)

    def refresh(self) -> None:
        pairs = self.c.pairs
        n = len(pairs)
        flagged = [(t, r) for t, r in pairs if r.suspicious]
        self.cards["tx"].set(f"{n:,}")
        self.cards["customers"].set(f"{len({t.user_id for t, _ in pairs}):,}")
        self.cards["suspicious"].set(f"{len(flagged):,}")
        self.cards["rate"].set(f"{(len(flagged) / n if n else 0):.2%}")
        self.cards["critical"].set(f"{sum(1 for _, r in flagged if r.risk_level == 'CRITICAL'):,}")
        self.cards["open"].set(f"{sum(1 for _, r in flagged if r.status == 'open'):,}")
        self.cards["avg"].set(f"{(np.mean([r.combined_score for _, r in pairs]) if n else 0):.3f}")
        self.engine_label.setText(f"Engine: {self.c.engine.state}")
        self._charts(pairs, flagged)
        self._latest(flagged)

    def _charts(self, pairs, flagged) -> None:
        fig = self.canvas.figure
        fig.clear()
        axes = fig.subplots(2, 2)
        th = self.c.config.thresholds_for(None)
        ax = axes[0][0]
        scores = [r.combined_score for _, r in pairs]
        if scores:
            ax.hist(scores, bins=40, range=(0, 1), color="#5c6bc0", alpha=0.85)
            ax.set_yscale("log")
        for key, color in (("suspicious", RISK_COLORS["MEDIUM"]), ("high", RISK_COLORS["HIGH"]),
                           ("critical", RISK_COLORS["CRITICAL"])):
            ax.axvline(float(th.get(key, 0.5)), color=color, ls="--", lw=1.2, label=f"{key} {float(th.get(key)):.2f}")
        ax.set_title("Suspicion score distribution", fontsize=10)
        ax.legend(fontsize=7)

        ax = axes[0][1]
        by_day = defaultdict(Counter)
        for t, r in flagged:
            by_day[t.timestamp.date()][r.risk_level] += 1
        days = sorted(by_day)[-30:]
        bottom = np.zeros(len(days))
        for level in ("MEDIUM", "HIGH", "CRITICAL"):
            vals = np.array([by_day[d][level] for d in days], dtype=float)
            ax.bar(range(len(days)), vals, bottom=bottom, color=RISK_COLORS[level], label=level)
            bottom += vals
        ax.set_xticks(range(len(days)))
        ax.set_xticklabels([d.strftime("%m-%d") for d in days], rotation=60, fontsize=7)
        ax.set_title("Suspicious transactions per day", fontsize=10)
        if days:
            ax.legend(fontsize=7)

        ax = axes[1][0]
        banks = sorted({t.bank_id for t, _ in pairs})
        width = 0.8 / max(len(banks), 1)
        for i, bank in enumerate(banks):
            counts = [sum(1 for t, _ in flagged if t.bank_id == bank and t.tx_type == tt) for tt in TX_TYPES]
            ax.bar(np.arange(len(TX_TYPES)) + i * width, counts, width, label=self.c.config.bank_name(bank))
        ax.set_xticks(np.arange(len(TX_TYPES)) + width * (len(banks) - 1) / 2)
        ax.set_xticklabels([TX_TYPE_LABELS[t] for t in TX_TYPES], fontsize=8)
        ax.set_title("Suspicious by bank and operation type", fontsize=10)
        if banks:
            ax.legend(fontsize=7)

        ax = axes[1][1]
        normal = [r for _, r in pairs if not r.suspicious]
        sus = [r for _, r in flagged]
        names = ["local", "temporal", "global"]
        for j, (group, color, label) in enumerate(((normal, "#90a4ae", "normal"), (sus, RISK_COLORS["HIGH"], "suspicious"))):
            vals = [np.mean([getattr(r, f"{p}_score") for r in group]) if group else 0 for p in names]
            ax.bar(np.arange(3) + j * 0.38, vals, 0.38, color=color, label=label)
        ax.set_xticks(np.arange(3) + 0.19)
        ax.set_xticklabels(["Local (HBOS)", "Temporal", "Global (CBLOF)"], fontsize=8)
        ax.set_ylim(0, 1)
        ax.set_title("Average profile scores", fontsize=10)
        ax.legend(fontsize=7)
        for row in axes:
            for a in row:
                a.tick_params(labelsize=7)
                a.spines[["top", "right"]].set_visible(False)
        self.canvas.draw_idle()

    def _latest(self, flagged) -> None:
        rows = sorted(flagged, key=lambda p: p[0].timestamp, reverse=True)[:25]
        self.latest.setRowCount(len(rows))
        for i, (t, r) in enumerate(rows):
            ranked = self.c.engine.ranked_reasons(r)
            values = [f"{t.timestamp:%Y-%m-%d %H:%M}", t.tx_id, t.user_id, t.bank_id, TX_TYPE_LABELS[t.tx_type],
                      f"{t.amount:,.2f}", f"{r.combined_score:.2f} {r.risk_level}", ranked[0].message if ranked else ""]
            for j, v in enumerate(values):
                item = QTableWidgetItem(v)
                if j == 6:
                    item.setForeground(Qt.white)
                    item.setBackground(QColor(RISK_COLORS[r.risk_level]))
                self.latest.setItem(i, j, item)
        self.latest.resizeColumnsToContents()


# ============================================================= transactions
class TransactionsTab(QWidget):
    show_customer = Signal(str)

    def __init__(self, controller, alerts: bool = False, log=None):
        super().__init__()
        self.c = controller
        self.alerts = alerts
        self.log = log or (lambda m: None)
        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search transaction, customer, IBAN, ASN, IP…")
        self.bank = QComboBox()
        self.type = QComboBox()
        self.type.addItem("All types", "")
        for t in TX_TYPES:
            self.type.addItem(TX_TYPE_LABELS[t], t)
        self.level = QComboBox()
        for i, label in enumerate(["All risk levels", "MEDIUM and above", "HIGH and above", "CRITICAL only"]):
            self.level.addItem(label, i)
        self.status = QComboBox()
        for label, value in (("Any status", ""), ("Open", "open"), ("Confirmed fraud", "fraud"), ("Legitimate", "legit")):
            self.status.addItem(label, value)
        self.only = QCheckBox("Only suspicious")
        self.only.setChecked(alerts)
        self.only.setVisible(not alerts)
        for w in (self.search, self.bank, self.type, self.level, self.status, self.only):
            bar.addWidget(w)
        self.count = QLabel()
        bar.addWidget(self.count)
        layout.addLayout(bar)

        split = QSplitter(Qt.Horizontal)
        self.model = TransactionModel(controller)
        self.proxy = TransactionFilter()
        self.proxy.setSourceModel(self.model)
        self.proxy.only_suspicious = alerts
        self.view = _table(QTableView())
        self.view.setModel(self.proxy)
        self.view.sortByColumn(11 if alerts else 0, Qt.DescendingOrder)
        self.view.selectionModel().currentRowChanged.connect(self._selected)
        split.addWidget(self.view)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(False)
        rl.addWidget(self.detail, 1)
        buttons = QGridLayout()
        self.btn_report = QPushButton("Generate PDF report")
        self.btn_fraud = QPushButton("Confirm fraud")
        self.btn_legit = QPushButton("Mark legitimate")
        self.btn_customer = QPushButton("Open customer")
        buttons.addWidget(self.btn_report, 0, 0)
        buttons.addWidget(self.btn_customer, 0, 1)
        buttons.addWidget(self.btn_fraud, 1, 0)
        buttons.addWidget(self.btn_legit, 1, 1)
        if alerts:
            self.btn_all = QPushButton("Reports for all open alerts")
            self.btn_summary = QPushButton("Summary report (PDF)")
            self.btn_csv = QPushButton("Export CSV")
            buttons.addWidget(self.btn_all, 2, 0)
            buttons.addWidget(self.btn_summary, 2, 1)
            buttons.addWidget(self.btn_csv, 3, 0, 1, 2)
            self.btn_all.clicked.connect(self._reports_all)
            self.btn_summary.clicked.connect(self._summary)
            self.btn_csv.clicked.connect(self._csv)
        rl.addLayout(buttons)
        split.addWidget(right)
        split.setSizes([900, 520])
        layout.addWidget(split, 1)

        self.btn_report.clicked.connect(self._report)
        self.btn_fraud.clicked.connect(lambda: self._status("fraud"))
        self.btn_legit.clicked.connect(lambda: self._status("legit"))
        self.btn_customer.clicked.connect(lambda: self.current() and self.show_customer.emit(self.current()[0].user_id))
        self.search.textChanged.connect(lambda t: self._filter(text=t.strip()))
        self.bank.currentIndexChanged.connect(lambda _: self._filter(bank=self.bank.currentData() or ""))
        self.type.currentIndexChanged.connect(lambda _: self._filter(tx_type=self.type.currentData() or ""))
        self.level.currentIndexChanged.connect(lambda _: self._filter(min_level=self.level.currentData() or 0))
        self.status.currentIndexChanged.connect(lambda _: self._filter(status=self.status.currentData() or ""))
        self.only.toggled.connect(lambda v: self._filter(only_suspicious=v))
        self._banks = []
        self._sized = False

    def _filter(self, **kwargs) -> None:
        self.proxy.set(**kwargs)
        self._update_count()

    def _update_count(self) -> None:
        self.count.setText(f"{self.proxy.rowCount():,} shown")

    def refresh(self) -> None:
        selected = self.current()
        banks = sorted({t.bank_id for t, _ in self.c.pairs})
        if banks != self._banks:
            self._banks = banks
            current = self.bank.currentData()
            self.bank.blockSignals(True)
            self.bank.clear()
            self.bank.addItem("All banks", "")
            for b in banks:
                self.bank.addItem(f"{b} - {self.c.config.bank_name(b)}", b)
            idx = self.bank.findData(current)
            self.bank.setCurrentIndex(max(idx, 0))
            self.bank.blockSignals(False)
        self.model.refresh()
        self._update_count()
        if not self._sized and self.proxy.rowCount():
            self.view.resizeColumnsToContents()
            self._sized = True
        if selected:
            self.select(selected[0].tx_id, scroll=False)

    def current(self):
        idx = self.view.selectionModel().currentIndex()
        if not idx.isValid():
            return None
        return self.model.pair(self.proxy.mapToSource(idx).row())

    def select(self, tx_id: str, scroll: bool = True) -> bool:
        for row, (t, _) in enumerate(self.model.rows):
            if t.tx_id == tx_id:
                idx = self.proxy.mapFromSource(self.model.index(row, 0))
                if idx.isValid():
                    self.view.setCurrentIndex(idx)
                    if scroll:
                        self.view.scrollTo(idx)
                    self._selected(idx, None)
                    return True
        return False

    def _selected(self, current, _previous) -> None:
        pair = self.current() if current is None or not current.isValid() else \
            self.model.pair(self.proxy.mapToSource(current).row())
        if pair:
            self.detail.setHtml(transaction_html(self.c, *pair))

    def _report(self) -> None:
        pair = self.current()
        if not pair:
            return
        path = self.c.generate_report(pair[0].tx_id)
        self.log(f"report written: {path}")
        self._selected(None, None)
        QMessageBox.information(self, "Report generated", f"PDF report written to:\n{path}")

    def _status(self, status: str) -> None:
        pair = self.current()
        if pair:
            self.c.set_status(pair[0].tx_id, status)
            self.model.refresh()
            self.select(pair[0].tx_id, scroll=False)

    def _visible_pairs(self):
        return [self.model.pair(self.proxy.mapToSource(self.proxy.index(i, 0)).row()) for i in range(self.proxy.rowCount())]

    def _reports_all(self) -> None:
        pairs = [(t, r) for t, r in self._visible_pairs() if r.status == "open"]
        for t, _ in pairs:
            self.c.generate_report(t.tx_id)
        self.log(f"{len(pairs)} alert reports written to {self.c.reports.out_dir}")
        QMessageBox.information(self, "Reports", f"{len(pairs)} reports written to {self.c.reports.out_dir}")

    def _summary(self) -> None:
        path = self.c.summary_report()
        self.log(f"summary report written: {path}")
        QMessageBox.information(self, "Summary report", f"Summary report written to:\n{path}")

    def _csv(self) -> None:
        path = self.c.export_csv(self._visible_pairs())
        self.log(f"CSV export written: {path}")
        QMessageBox.information(self, "Export", f"CSV written to:\n{path}")


# ================================================================ customers
class CustomersTab(QWidget):
    open_transaction = Signal(str)

    def __init__(self, controller):
        super().__init__()
        self.c = controller
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search customer or bank…")
        self.flagged = QCheckBox("Only customers with suspicious transactions")
        bar.addWidget(self.search)
        bar.addWidget(self.flagged)
        layout.addLayout(bar)

        split = QSplitter(Qt.Horizontal)
        self.model = CustomerModel(controller)
        self.proxy = CustomerFilter()
        self.proxy.setSourceModel(self.model)
        self.view = _table(QTableView())
        self.view.setModel(self.proxy)
        self.view.sortByColumn(5, Qt.DescendingOrder)
        self.view.selectionModel().currentRowChanged.connect(lambda *_: self._selected())
        split.addWidget(self.view)

        right = QSplitter(Qt.Vertical)
        self.detail = QTextBrowser()
        right.addWidget(self.detail)
        self.canvas = MplCanvas(6, 3.2)
        right.addWidget(self.canvas)
        self.tx_model = TransactionModel(controller)
        self.tx_proxy = TransactionFilter()
        self.tx_proxy.setSourceModel(self.tx_model)
        self.tx_view = _table(QTableView())
        self.tx_view.setModel(self.tx_proxy)
        self.tx_view.sortByColumn(0, Qt.DescendingOrder)
        for col in (1, 2, 3, 7, 8, 9, 10):
            self.tx_view.hideColumn(col)
        self.tx_view.doubleClicked.connect(self._open_tx)
        right.addWidget(self.tx_view)
        right.setSizes([220, 260, 260])
        split.addWidget(right)
        split.setSizes([700, 700])
        layout.addWidget(split, 1)

        self.search.textChanged.connect(self._filter)
        self.flagged.toggled.connect(self._filter)
        self.user = None

    def _filter(self, *_):
        self.proxy.set(text=self.search.text().strip(), only_flagged=self.flagged.isChecked())

    def refresh(self) -> None:
        user = self.user
        self.model.refresh()
        if user:
            self.select(user)

    def select(self, user_id: str) -> None:
        for row, c in enumerate(self.model.rows):
            if c["user_id"] == user_id:
                idx = self.proxy.mapFromSource(self.model.index(row, 0))
                if idx.isValid():
                    self.view.selectRow(idx.row())
                    self.view.scrollTo(idx)
                self.user = user_id
                self._show(user_id)
                return

    def _selected(self) -> None:
        idx = self.view.selectionModel().currentIndex()
        if idx.isValid():
            self.user = self.model.rows[self.proxy.mapToSource(idx).row()]["user_id"]
            self._show(self.user)

    def _show(self, user_id: str) -> None:
        self.detail.setHtml(customer_html(self.c, user_id))
        self.tx_model.refresh()
        self.tx_proxy.set(user=user_id)
        pairs = [(t, r) for t, r in self.c.pairs if t.user_id == user_id]
        fig = self.canvas.figure
        fig.clear()
        ax1, ax2 = fig.subplots(1, 2, gridspec_kw={"width_ratios": [2, 1]})
        if pairs:
            xs = [t.timestamp for t, _ in pairs]
            ys = [r.combined_score for _, r in pairs]
            cs = [RISK_COLORS[r.risk_level] for _, r in pairs]
            ax1.scatter(xs, ys, c=cs, s=18, edgecolors="none")
            th = float(self.c.config.thresholds_for(pairs[0][0].bank_id).get("suspicious", 0.5))
            ax1.axhline(th, color=RISK_COLORS["MEDIUM"], ls="--", lw=1)
            ax1.set_ylim(0, 1)
            ax1.tick_params(axis="x", labelrotation=45)
        ax1.set_title("Suspicion score of each transaction", fontsize=9)
        hours = np.zeros(24)
        for info in (self.c.engine.customer_summary(user_id).get("local") or {}).values():
            if info.get("hour_hist"):
                hours += np.array(info["hour_hist"])
        ax2.bar(range(24), hours, color="#5c6bc0")
        for t, r in pairs:
            if r.suspicious:
                ax2.axvline(t.timestamp.hour + 0.5, color=RISK_COLORS[r.risk_level], lw=1, alpha=0.7)
        ax2.set_title("Usual hours (bars) vs alerts", fontsize=9)
        for a in (ax1, ax2):
            a.tick_params(labelsize=7)
            a.spines[["top", "right"]].set_visible(False)
        self.canvas.draw_idle()

    def _open_tx(self, idx) -> None:
        t, _ = self.tx_model.pair(self.tx_proxy.mapToSource(idx).row())
        self.open_transaction.emit(t.tx_id)


# ================================================================= channels
class ChannelDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Link a transaction channel")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit()
        self.kind = QComboBox()
        for name, cls in CHANNEL_TYPES.items():
            self.kind.addItem(f"{name} - {cls.description}", name)
        self.enabled = QCheckBox("Start automatically")
        self.enabled.setChecked(True)
        form.addRow("Name", self.name)
        form.addRow("Type", self.kind)
        form.addRow("", self.enabled)
        layout.addLayout(form)
        self.options_box = QGroupBox("Options")
        self.options_form = QFormLayout(self.options_box)
        layout.addWidget(self.options_box)
        layout.addWidget(QLabel("Field mapping (one per line: source_field = banksealer_field)\n"
                                "BankSealer fields: user_id, timestamp, tx_type, amount, iban, ip, cc_asn, bank_id, ..."))
        self.mapping = QPlainTextEdit()
        self.mapping.setPlaceholderText("customer = user_id\nvalue = amount")
        self.mapping.setMaximumHeight(90)
        layout.addWidget(self.mapping)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.edits: dict[str, QLineEdit] = {}
        self.kind.currentIndexChanged.connect(self._options)
        self._options()

    def _options(self) -> None:
        while self.options_form.rowCount():
            self.options_form.removeRow(0)
        self.edits = {}
        cls = CHANNEL_TYPES[self.kind.currentData()]
        for key, value in cls.default_options.items():
            edit = QLineEdit(str(value))
            self.edits[key] = edit
            self.options_form.addRow(key, edit)
        if not self.name.text() or self.name.text() in [c.type_name for c in CHANNEL_TYPES.values()]:
            self.name.setText(cls.type_name)

    def spec(self) -> dict:
        options = {k: yaml.safe_load(e.text()) if e.text().strip() else None for k, e in self.edits.items()}
        mapping = {}
        for line in self.mapping.toPlainText().splitlines():
            if "=" in line:
                src, dst = (s.strip() for s in line.split("=", 1))
                if src and dst:
                    mapping[src] = dst
        spec = {"name": self.name.text().strip() or self.kind.currentData(), "type": self.kind.currentData(),
                "enabled": self.enabled.isChecked(), "options": options}
        if mapping:
            spec["field_mapping"] = mapping
        return spec


class ChannelsTab(QWidget):
    def __init__(self, controller, log=None):
        super().__init__()
        self.c = controller
        self.log = log or (lambda m: None)
        layout = QVBoxLayout(self)
        intro = QLabel("Transaction channels feed raw records into the preparation pipeline (field mapping, "
                       "type coercion, validation, enrichment, de-duplication) before BankSealer scores them.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(["Name", "Type", "Status", "Received", "Accepted", "Rejected",
                                              "Duplicates", "Options", "Last error"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 2)
        bar = QHBoxLayout()
        buttons = [("Link channel…", self._add), ("Remove", self._remove), ("Start", self._start), ("Stop", self._stop),
                   ("Start all", self._start_all), ("Stop all", self._stop_all), ("Save channels to config", self._save)]
        for label, fn in buttons:
            b = QPushButton(label)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch(1)
        layout.addLayout(bar)
        layout.addWidget(QLabel("Preparation log (rejected records)"))
        self.errors = QPlainTextEdit()
        self.errors.setReadOnly(True)
        layout.addWidget(self.errors, 1)

    def _name(self):
        row = self.table.currentRow()
        return self.table.item(row, 0).text() if row >= 0 and self.table.item(row, 0) else None

    def refresh(self) -> None:
        channels = self.c.manager.channels
        current = self._name()
        self.table.setRowCount(len(channels))
        errors = []
        for i, ch in enumerate(channels):
            s = ch.preparer.stats
            values = [ch.name, ch.type_name, ch.status, f"{ch.received:,}", f"{s['accepted']:,}", f"{s['rejected']:,}",
                      f"{s['duplicates']:,}", ch.describe(), ch.last_error]
            for j, v in enumerate(values):
                item = QTableWidgetItem(str(v))
                if j == 2:
                    item.setForeground({"running": Qt.darkGreen, "error": Qt.red}.get(ch.status, Qt.darkGray))
                self.table.setItem(i, j, item)
            errors.extend(ch.preparer.errors)
            if current == ch.name:
                self.table.selectRow(i)
        text = "\n".join(list(errors)[-200:])
        if text != self.errors.toPlainText():
            self.errors.setPlainText(text)

    def _add(self) -> None:
        dlg = ChannelDialog(self)
        if dlg.exec() == QDialog.Accepted:
            try:
                ch = self.c.manager.add(dlg.spec())
            except (ValueError, TypeError) as exc:
                QMessageBox.warning(self, "Channel", str(exc))
                return
            if dlg.enabled.isChecked():
                ch.start()
            self.log(f"channel linked: {ch.name} ({ch.type_name})")
            self.refresh()

    def _remove(self) -> None:
        name = self._name()
        if name and QMessageBox.question(self, "Remove channel", f"Remove channel '{name}'?") == QMessageBox.Yes:
            self.c.manager.remove(name)
            self.log(f"channel removed: {name}")
            self.refresh()

    def _start(self) -> None:
        if self._name():
            self.c.manager.start(self._name())
            self.log(f"channel started: {self._name()}")

    def _stop(self) -> None:
        if self._name():
            self.c.manager.stop(self._name())
            self.log(f"channel stopped: {self._name()}")

    def _start_all(self) -> None:
        for ch in self.c.manager.channels:
            ch.start()
        self.log("all channels started")

    def _stop_all(self) -> None:
        self.c.manager.stop_all()
        self.log("all channels stopped")

    def _save(self) -> None:
        path = self.c.save_config()
        self.log(f"channels saved to {path}")


# ============================================================ configuration
class ConfigTab(QWidget):
    applied = Signal()

    THRESHOLD_FIELDS = [
        ("suspicious", "Suspicious from score", 0, 1), ("high", "HIGH risk from score", 0, 1),
        ("critical", "CRITICAL risk from score", 0, 1), ("local_alert", "Local profile alert level", 0, 1.01),
        ("temporal_alert", "Temporal profile alert level", 0, 1.01), ("global_alert", "Global profile alert level", 0, 1.01),
        ("hard_rule_score", "Score when a limit is broken", 0, 1),
        ("high_risk_country_boost", "High-risk country boost", 0, 1), ("night_boost", "Night-time boost", 0, 1),
    ]

    def __init__(self, controller, log=None):
        super().__init__()
        self.c = controller
        self.log = log or (lambda m: None)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        # ---- thresholds per bank
        page = QWidget()
        pl = QHBoxLayout(page)
        left = QVBoxLayout()
        left.addWidget(QLabel("Bank / company"))
        self.banks = QListWidget()
        left.addWidget(self.banks, 1)
        b_add, b_del = QPushButton("Add bank…"), QPushButton("Remove bank")
        b_add.clicked.connect(self._add_bank)
        b_del.clicked.connect(self._remove_bank)
        left.addWidget(b_add)
        left.addWidget(b_del)
        pl.addLayout(left, 1)

        right = QVBoxLayout()
        self.scope = QLabel()
        self.scope.setWordWrap(True)
        right.addWidget(self.scope)
        form_box = QGroupBox("Decision thresholds")
        form = QFormLayout(form_box)
        self.bank_name = QLineEdit()
        form.addRow("Display name", self.bank_name)
        self.spins: dict[str, QDoubleSpinBox] = {}
        for key, label, lo, hi in self.THRESHOLD_FIELDS:
            spin = QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setSingleStep(0.01)
            spin.setDecimals(2)
            self.spins[key] = spin
            form.addRow(label, spin)
        self.limits: dict[str, QDoubleSpinBox] = {}
        for t in TX_TYPES:
            spin = QDoubleSpinBox()
            spin.setRange(0, 10_000_000)
            spin.setDecimals(2)
            spin.setSingleStep(100)
            spin.setSpecialValueText("no limit")
            self.limits[t] = spin
            form.addRow(f"Max amount - {TX_TYPE_LABELS[t]}", spin)
        self.countries = QLineEdit()
        self.countries.setPlaceholderText("e.g. KY, PA, VG")
        form.addRow("High-risk countries", self.countries)
        self.night = QLineEdit()
        self.night.setPlaceholderText("e.g. 0, 1, 2, 3, 4")
        form.addRow("Night hours", self.night)
        right.addWidget(form_box)
        right.addStretch(1)
        pl.addLayout(right, 3)
        tabs.addTab(page, "Bank / company thresholds")

        # ---- engine
        page2 = QWidget()
        f2 = QFormLayout(page2)
        self.w_local, self.w_temporal, self.w_global = (self._dspin(0, 10, 0.05) for _ in range(3))
        f2.addRow("Profile weight - local (HBOS)", self.w_local)
        f2.addRow("Profile weight - temporal", self.w_temporal)
        f2.addRow("Profile weight - global (CBLOF)", self.w_global)
        self.sigma = self._dspin(0.5, 10, 0.1)
        f2.addRow("Temporal threshold k (mean + k·std)", self.sigma)
        self.window = self._dspin(1, 24 * 30, 1)
        f2.addRow("Temporal window (hours)", self.window)
        self.min_history = QSpinBox()
        self.min_history.setRange(1, 1000)
        f2.addRow("Undertrained below (past ops)", self.min_history)
        self.local_scale = self._dspin(0.5, 100, 0.5)
        f2.addRow("Local score scale", self.local_scale)
        self.eps = self._dspin(0.1, 20, 0.1)
        f2.addRow("Global DBSCAN eps (Mahalanobis)", self.eps)
        self.min_samples = QSpinBox()
        self.min_samples.setRange(2, 500)
        f2.addRow("Global DBSCAN min samples", self.min_samples)
        self.auto_report = QCheckBox("Write a PDF report for every suspicious transaction")
        f2.addRow("", self.auto_report)
        self.auto_level = QComboBox()
        self.auto_level.addItems(["MEDIUM", "HIGH", "CRITICAL"])
        f2.addRow("Auto-report from level", self.auto_level)
        self.exclude = QCheckBox("Do not learn from suspicious transactions")
        f2.addRow("", self.exclude)
        tabs.addTab(page2, "Engine")

        # ---- yaml
        page3 = QWidget()
        l3 = QVBoxLayout(page3)
        self.yaml = QPlainTextEdit()
        self.yaml.setStyleSheet("font-family: monospace;")
        l3.addWidget(self.yaml)
        b_yaml = QPushButton("Apply YAML")
        b_yaml.clicked.connect(self._apply_yaml)
        l3.addWidget(b_yaml)
        tabs.addTab(page3, "Advanced (YAML)")

        bar = QHBoxLayout()
        for label, fn in (("Apply (re-evaluate transactions)", self._apply), ("Save to file", self._save),
                          ("Reload from file", self._reload)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch(1)
        self.path_label = QLabel()
        bar.addWidget(self.path_label)
        layout.addLayout(bar)

        self.working = controller.config.copy()
        self.current_bank = None
        self.banks.currentRowChanged.connect(self._bank_changed)
        self._load_all()

    @staticmethod
    def _dspin(lo, hi, step):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setDecimals(2)
        return s

    # ---------------------------------------------------------- loading
    def _load_all(self) -> None:
        self.path_label.setText(f"file: {self.c.config_path}")
        self.banks.blockSignals(True)
        self.banks.clear()
        self.banks.addItem("Default (all banks/companies)")
        for bank_id in self.working.banks:
            self.banks.addItem(f"{bank_id} - {self.working.bank_name(bank_id)}")
        self.current_bank = None
        self.banks.setCurrentRow(0)   # signals blocked: the form is not stored while reloading
        self.banks.blockSignals(False)
        self._load_thresholds(None)
        e, loc, tmp, glo, app = (self.working.section(k) for k in ("engine", "local", "temporal", "global", "app"))
        w = e.get("profile_weights", {})
        self.w_local.setValue(float(w.get("local", 0.5)))
        self.w_temporal.setValue(float(w.get("temporal", 0.3)))
        self.w_global.setValue(float(w.get("global", 0.2)))
        self.sigma.setValue(float(tmp.get("sigma_multiplier", 3)))
        self.window.setValue(float(tmp.get("window_hours", 24)))
        self.min_history.setValue(int(loc.get("min_history", 10)))
        self.local_scale.setValue(float(loc.get("score_scale", 8)))
        self.eps.setValue(float(glo.get("eps", 1.2)))
        self.min_samples.setValue(int(glo.get("min_samples", 5)))
        self.auto_report.setChecked(bool(app.get("auto_report", True)))
        self.auto_level.setCurrentText(str(app.get("auto_report_min_level", "MEDIUM")))
        self.exclude.setChecked(bool(e.get("exclude_suspicious_from_learning", True)))
        self.yaml.setPlainText(yaml.safe_dump(self.working.data, sort_keys=False, allow_unicode=True))

    def _bank_id(self, row: int):
        if row <= 0:
            return None
        return list(self.working.banks)[row - 1]

    def _bank_changed(self, row: int) -> None:
        self._store_thresholds()
        self.current_bank = self._bank_id(row)
        self._load_thresholds(self.current_bank)

    def _load_thresholds(self, bank_id) -> None:
        th = self.working.thresholds_for(bank_id)
        self.bank_name.setEnabled(bank_id is not None)
        self.bank_name.setText(self.working.bank_name(bank_id) if bank_id else "Default")
        self.scope.setText("Default thresholds apply to every bank/company without an override." if bank_id is None
                           else f"Overrides for {bank_id}. Values equal to the defaults are inherited.")
        for key, spin in self.spins.items():
            spin.setValue(float(th.get(key, 0)))
        for t, spin in self.limits.items():
            spin.setValue(float((th.get("max_amount") or {}).get(t) or 0))
        self.countries.setText(", ".join(th.get("high_risk_countries", []) or []))
        self.night.setText(", ".join(str(h) for h in th.get("night_hours", []) or []))

    def _form_thresholds(self) -> dict:
        th = {key: round(spin.value(), 4) for key, spin in self.spins.items()}
        th["max_amount"] = {t: spin.value() for t, spin in self.limits.items() if spin.value() > 0}
        th["high_risk_countries"] = [c.strip().upper() for c in self.countries.text().split(",") if c.strip()]
        th["night_hours"] = [int(h) for h in self.night.text().replace(";", ",").split(",") if h.strip().isdigit()]
        return th

    def _store_thresholds(self) -> None:
        th = self._form_thresholds()
        if self.current_bank is None:
            self.working.set_bank_thresholds(None, th)
            return
        defaults = self.working.thresholds_for(None)
        override = {}
        for key, value in th.items():
            if key == "max_amount":
                diff = {t: v for t, v in value.items() if (defaults.get("max_amount") or {}).get(t) != v}
                if diff:
                    override[key] = diff
            elif defaults.get(key) != value:
                override[key] = value
        self.working.set_bank_thresholds(self.current_bank, override, self.bank_name.text().strip() or None)

    def _store_engine(self) -> None:
        d = self.working.data
        d["engine"]["profile_weights"] = {"local": self.w_local.value(), "temporal": self.w_temporal.value(),
                                          "global": self.w_global.value()}
        d["engine"]["exclude_suspicious_from_learning"] = self.exclude.isChecked()
        d["temporal"]["sigma_multiplier"] = self.sigma.value()
        d["temporal"]["window_hours"] = self.window.value()
        d["local"]["min_history"] = self.min_history.value()
        d["local"]["score_scale"] = self.local_scale.value()
        d["global"]["eps"] = self.eps.value()
        d["global"]["min_samples"] = self.min_samples.value()
        d["app"]["auto_report"] = self.auto_report.isChecked()
        d["app"]["auto_report_min_level"] = self.auto_level.currentText()

    # ---------------------------------------------------------- actions
    def _add_bank(self) -> None:
        bank_id, ok = QInputDialog.getText(self, "Add bank / company", "Bank or company ID (as in transactions):")
        if ok and bank_id.strip():
            self._store_thresholds()
            self.working.set_bank_thresholds(bank_id.strip(), {}, bank_id.strip())
            self._load_all()
            self.banks.setCurrentRow(self.banks.count() - 1)

    def _remove_bank(self) -> None:
        if self.current_bank:
            self.working.remove_bank(self.current_bank)
            self.current_bank = None
            self._load_all()

    def _apply(self) -> None:
        self._store_thresholds()
        self._store_engine()
        self.working.data["channels"] = self.c.manager.specs_list()
        self.c.apply_config(self.working.copy())
        self.yaml.setPlainText(yaml.safe_dump(self.working.data, sort_keys=False, allow_unicode=True))
        self.log("configuration applied")
        self.applied.emit()

    def _apply_yaml(self) -> None:
        try:
            data = yaml.safe_load(self.yaml.toPlainText()) or {}
        except yaml.YAMLError as exc:
            QMessageBox.warning(self, "YAML", f"Invalid YAML:\n{exc}")
            return
        self.working = Config(data, self.c.config_path)
        self.c.apply_config(self.working.copy())
        self._load_all()
        self.log("configuration applied from YAML")
        self.applied.emit()

    def _save(self) -> None:
        self._apply()
        path = self.c.save_config()
        self.log(f"configuration saved to {path}")
        QMessageBox.information(self, "Configuration", f"Saved to {path}")

    def _reload(self) -> None:
        self.working = Config.load(self.c.config_path)
        self.c.apply_config(self.working.copy())
        self._load_all()
        self.log(f"configuration reloaded from {self.c.config_path}")
        self.applied.emit()


__all__ = ["DashboardTab", "TransactionsTab", "CustomersTab", "ChannelsTab", "ConfigTab"]
