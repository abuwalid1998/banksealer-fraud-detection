"""BankSealer desktop application - main window."""
from __future__ import annotations

import argparse
import os
import sys
import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QFont
from PySide6.QtWidgets import (QApplication, QDockWidget, QFileDialog, QLabel, QMainWindow, QMessageBox,
                               QPlainTextEdit, QStyleFactory, QTabWidget, QToolBar)

from ..controller import AppController
from .tabs import ChannelsTab, ConfigTab, CustomersTab, DashboardTab, TransactionsTab

STYLE = """
QMainWindow, QWidget { font-size: 12px; }
QTabWidget::pane { border: 1px solid #dfe3f0; background: #f7f8fc; }
QTabBar::tab { padding: 7px 16px; }
QTabBar::tab:selected { background: #f7f8fc; color: #1a237e; font-weight: 600; }
QGroupBox { font-weight: 600; border: 1px solid #dfe3f0; border-radius: 6px; margin-top: 10px; padding-top: 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 3px; }
QPushButton { padding: 5px 12px; }
QToolBar { spacing: 4px; padding: 3px; }
QTableView { gridline-color: #eceff1; selection-background-color: #c5cae9; selection-color: black; }
"""


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController):
        super().__init__()
        self.c = controller
        self.setWindowTitle("BankSealer - Online Banking Fraud Analysis")
        self.resize(1480, 920)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        dock = QDockWidget("Activity log", self)
        dock.setWidget(self.log_view)
        dock.setObjectName("log")
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.resizeDocks([dock], [110], Qt.Vertical)
        self.c.listeners.append(self.log)

        self.tabs = QTabWidget()
        self.dashboard = DashboardTab(controller)
        self.transactions = TransactionsTab(controller, log=self.log)
        self.alerts = TransactionsTab(controller, alerts=True, log=self.log)
        self.customers = CustomersTab(controller)
        self.channels = ChannelsTab(controller, log=self.log)
        self.config_tab = ConfigTab(controller, log=self.log)
        for widget, title in ((self.dashboard, "Dashboard"), (self.transactions, "Transactions"),
                              (self.alerts, "Alerts && Reports"), (self.customers, "Customers"),
                              (self.channels, "Channels"), (self.config_tab, "Configuration")):
            self.tabs.addTab(widget, title)
        self.setCentralWidget(self.tabs)

        self.dashboard.open_transaction.connect(self.open_transaction)
        self.customers.open_transaction.connect(self.open_transaction)
        self.transactions.show_customer.connect(self.open_customer)
        self.alerts.show_customer.connect(self.open_customer)
        self.config_tab.applied.connect(self._force_refresh)
        self.tabs.currentChanged.connect(lambda _: self._refresh_visible(force=True))

        self._toolbar()
        self.status = QLabel()
        self.statusBar().addPermanentWidget(self.status)

        self._seen_version = -1
        self._last_refresh = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(int(controller.config.section("app").get("refresh_interval_ms", 1000)))
        self.log(f"BankSealer started - config {controller.config_path}")
        self._refresh_visible(force=True)

    # ------------------------------------------------------------- ui
    def _toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setObjectName("main")
        self.addToolBar(tb)
        actions = [
            ("▶ Start channels", self._start, "Start all enabled channels"),
            ("■ Stop channels", self._stop, "Stop all channels"),
            (None, None, None),
            ("Load history file…", self._load_history, "Train the profiles on a CSV/JSONL history file"),
            ("Score file…", self._score_file, "Prepare and score a CSV/JSONL file of transactions"),
            ("Retrain profiles", self._retrain, "Refit all profiles on history + legitimate transactions"),
            (None, None, None),
            ("Summary report", self._summary, "PDF report of all suspicious transactions"),
            ("Export CSV", self._csv, "Export all scores to CSV"),
            ("Open reports folder", self._open_reports, "Open the reports directory"),
        ]
        for label, fn, tip in actions:
            if label is None:
                tb.addSeparator()
                continue
            action = QAction(label, self)
            action.setToolTip(tip)
            action.triggered.connect(fn)
            tb.addAction(action)

    def log(self, message: str) -> None:
        if not message[:2].isdigit():
            message = f"{time.strftime('%H:%M:%S')}  {message}"
        self.log_view.appendPlainText(message)

    def _tick(self) -> None:
        try:
            self.c.pump()
        except Exception as exc:  # noqa: BLE001 - keep the UI alive, show the problem
            self.log(f"ERROR while processing: {type(exc).__name__}: {exc}")
        self._refresh_visible()

    def _force_refresh(self) -> None:
        self._refresh_visible(force=True)

    def _refresh_visible(self, force: bool = False) -> None:
        now = time.time()
        running = sum(1 for ch in self.c.manager.channels if ch.running)
        self.status.setText(f"{self.c.engine.state}  |  {len(self.c.pairs):,} scored  |  "
                            f"{running}/{len(self.c.manager.channels)} channels running")
        widget = self.tabs.currentWidget()
        if widget is self.channels:
            self.channels.refresh()
        changed = self.c.version != self._seen_version
        if not force and (not changed or now - self._last_refresh < 2.0):
            return
        self._seen_version = self.c.version
        self._last_refresh = now
        if widget in (self.dashboard, self.transactions, self.alerts, self.customers):
            widget.refresh()

    # --------------------------------------------------------- navigation
    def open_transaction(self, tx_id: str) -> None:
        self.tabs.setCurrentWidget(self.transactions)
        self.transactions.refresh()
        if not self.transactions.select(tx_id):
            self.transactions.search.clear()
            self.transactions.only.setChecked(False)
            self.transactions.select(tx_id)

    def open_customer(self, user_id: str) -> None:
        self.tabs.setCurrentWidget(self.customers)
        self.customers.refresh()
        self.customers.select(user_id)

    # ------------------------------------------------------------ actions
    def _start(self) -> None:
        self.c.manager.start_enabled()
        self.log("enabled channels started")

    def _stop(self) -> None:
        self.c.manager.stop_all()
        self.log("all channels stopped")

    def _load_history(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "History file", "data", "Transactions (*.csv *.jsonl *.json)")
        if path:
            self.c.load_history_file(path)
            self._force_refresh()

    def _score_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Transactions to score", "data", "Transactions (*.csv *.jsonl *.json)")
        if path:
            if not self.c.engine.trained:
                QMessageBox.warning(self, "Not trained", "Load a history file (or start a channel) first: the "
                                                         "profiles must be trained before scoring.")
                return
            self.c.score_file(path)
            self._force_refresh()

    def _retrain(self) -> None:
        self.c.retrain()
        self._force_refresh()

    def _summary(self) -> None:
        path = self.c.summary_report()
        self.log(f"summary report written: {path}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(path)))

    def _csv(self) -> None:
        path = self.c.export_csv()
        self.log(f"CSV export written: {path}")

    def _open_reports(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(self.c.reports.out_dir)))

    def closeEvent(self, event) -> None:
        self.timer.stop()
        self.c.shutdown()
        super().closeEvent(event)


def screenshot_tour(window: MainWindow, out_dir: str) -> None:
    """Save a PNG of every tab (used for documentation / headless verification)."""
    os.makedirs(out_dir, exist_ok=True)
    names = ["dashboard", "transactions", "alerts", "customers", "channels", "configuration"]
    for i, name in enumerate(names):
        window.tabs.setCurrentIndex(i)
        window._refresh_visible(force=True)
        if name == "alerts" and window.alerts.proxy.rowCount():
            window.alerts.view.selectRow(0)
        if name == "transactions" and window.transactions.proxy.rowCount():
            window.transactions.only.setChecked(True)
            window.transactions.view.selectRow(0)
        if name == "customers" and window.customers.proxy.rowCount():
            window.customers.view.selectRow(0)
        QApplication.processEvents()
        window.grab().save(os.path.join(out_dir, f"{name}.png"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="BankSealer desktop application")
    parser.add_argument("--config", default="config/banksealer.yaml")
    parser.add_argument("--database", default=None, help="SQLite file (default from config)")
    parser.add_argument("--resume", action="store_true", help="reload the previous session (scores + model)")
    parser.add_argument("--no-autostart", action="store_true", help="do not start enabled channels at launch")
    parser.add_argument("--screenshots", default=None, help="run, save a PNG of each tab into DIR and exit")
    parser.add_argument("--run-seconds", type=float, default=25.0, help="with --screenshots: seconds to run first")
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setStyle(QStyleFactory.create("Fusion"))
    app.setStyleSheet(STYLE)
    app.setFont(QFont(app.font().family(), 10))
    controller = AppController(args.config, args.database, resume=args.resume,
                               start_channels=not args.no_autostart)
    window = MainWindow(controller)
    window.show()
    if args.screenshots:
        def finish():
            screenshot_tour(window, args.screenshots)
            window.close()
            app.quit()
        QTimer.singleShot(int(args.run_seconds * 1000), finish)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
