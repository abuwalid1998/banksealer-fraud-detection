"""Application controller (Qt-free): links channels -> preparation -> engine -> storage/reports.

The desktop UI calls :meth:`AppController.pump` periodically; everything else is
plain Python so it can be tested and used headless.
"""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime
from typing import Callable, Optional

from .channels import ChannelManager
from .cli import read_records
from .config import Config
from .engine import BankSealerEngine
from .models import RISK_LEVELS, ScoreResult, Transaction
from .preparation import TransactionPreparer
from .reports import ReportGenerator
from .storage import Storage


class AppController:
    def __init__(self, config_path: str = "config/banksealer.yaml", database: Optional[str] = None,
                 resume: bool = False, start_channels: bool = False):
        self.config_path = config_path
        self.config = Config.load(config_path)
        app = self.config.section("app")
        self.engine = BankSealerEngine(self.config)
        self.storage = Storage(database or app.get("database", "data/banksealer.db"))
        self.reports = ReportGenerator(self.config)
        self.manager = ChannelManager()
        for spec in self.config.channels:
            try:
                self.manager.add(spec)
            except ValueError as exc:
                self._log(f"channel skipped: {exc}")
        self.pairs: list[tuple[Transaction, ScoreResult]] = []
        self.index: dict[str, int] = {}
        self.report_paths: dict[str, str] = {}
        self.history: list[Transaction] = []
        self._history_pending = False
        self.listeners: list[Callable[[str], None]] = []
        self.version = 0          # increments whenever data changes (UI refresh hint)
        self.started_at = datetime.now()
        self.model_path = os.path.join(os.path.dirname(os.path.abspath(self.storage.path)), "engine.pkl") \
            if self.storage.path != ":memory:" else None
        if resume:
            self._resume()
        else:
            self.storage.clear()
        if start_channels:
            self.manager.start_enabled()

    # ---------------------------------------------------------------- log
    def _log(self, message: str) -> None:
        line = f"{datetime.now():%H:%M:%S}  {message}"
        for cb in getattr(self, "listeners", []):
            cb(line)

    def _resume(self) -> None:
        if self.model_path and os.path.exists(self.model_path):
            try:
                self.engine.load_model(self.model_path)
                self._log(f"model restored: {self.engine.state}")
            except Exception as exc:  # noqa: BLE001
                self._log(f"could not restore model: {exc}")
        for tx, r in self.storage.load(int(self.config.section("app").get("max_rows_in_memory", 50000))):
            self._append(tx, r)

    # -------------------------------------------------------------- pump
    def pump(self, limit: int = 5000) -> int:
        """Drain the channel queue; train/score; returns number of new results."""
        history, live = self.manager.drain(limit)
        if history:
            self.history.extend(history)
            self._history_pending = True
        if self._history_pending and (live or self.manager.queue.empty()):
            n = self.engine.fit(self.history)
            self._history_pending = False
            self._log(f"profiles trained on {n:,} historical transactions - {self.engine.state}")
        return self.process(live)

    def process(self, transactions: list[Transaction]) -> int:
        new = []
        was_trained = self.engine.trained
        for tx in transactions:
            result = self.engine.ingest(tx)
            if not was_trained and self.engine.trained:
                was_trained = True
                self._log(f"profiles trained on buffered transactions - {self.engine.state}")
            if result is None:
                continue
            self._append(tx, result)
            new.append((tx, result))
            if result.suspicious:
                self._maybe_report(tx, result)
        if new:
            self.storage.save(new)
            flagged = sum(1 for _, r in new if r.suspicious)
            if flagged:
                self._log(f"{len(new)} transactions scored, {flagged} suspicious")
            self.version += 1
        return len(new)

    def _append(self, tx: Transaction, result: ScoreResult) -> None:
        if tx.tx_id in self.index:
            self.pairs[self.index[tx.tx_id]] = (tx, result)
            return
        self.index[tx.tx_id] = len(self.pairs)
        self.pairs.append((tx, result))
        cap = int(self.config.section("app").get("max_rows_in_memory", 50000))
        if len(self.pairs) > cap:
            drop = len(self.pairs) - cap
            self.pairs = self.pairs[drop:]
            self.index = {t.tx_id: i for i, (t, _) in enumerate(self.pairs)}

    def _maybe_report(self, tx: Transaction, result: ScoreResult) -> None:
        app = self.config.section("app")
        if not app.get("auto_report", False):
            return
        minimum = str(app.get("auto_report_min_level", "MEDIUM")).upper()
        if minimum in RISK_LEVELS and RISK_LEVELS.index(result.risk_level) < RISK_LEVELS.index(minimum):
            return
        try:
            self.generate_report(tx.tx_id)
        except Exception as exc:  # noqa: BLE001
            self._log(f"report failed for {tx.tx_id}: {exc}")

    # ----------------------------------------------------------- actions
    def get(self, tx_id: str) -> Optional[tuple[Transaction, ScoreResult]]:
        i = self.index.get(tx_id)
        return self.pairs[i] if i is not None else None

    def generate_report(self, tx_id: str) -> str:
        pair = self.get(tx_id)
        if pair is None:
            raise KeyError(tx_id)
        tx, result = pair
        path = self.reports.transaction_report(tx, result, self.engine.customer_summary(tx.user_id),
                                               self.engine.ranked_reasons(result))
        self.report_paths[tx_id] = path
        self.storage.add_report(tx_id, path)
        return path

    def summary_report(self, pairs=None) -> str:
        return self.reports.summary_report(pairs if pairs is not None else self.pairs)

    def export_csv(self, pairs=None, path: Optional[str] = None) -> str:
        return self.reports.export_csv(pairs if pairs is not None else self.pairs, path)

    def set_status(self, tx_id: str, status: str) -> None:
        pair = self.get(tx_id)
        if pair is None:
            return
        tx, result = pair
        previous = result.status
        result.status = status
        self.storage.set_status(tx_id, status)
        if status == "legit" and previous != "legit" and result.suspicious:
            self.engine.learn(tx)   # feedback: the behaviour becomes part of the customer's profile
            self._log(f"{tx_id} marked legitimate - learned into {tx.user_id}'s profile")
        elif status == "fraud":
            self._log(f"{tx_id} confirmed as fraud")
        self.version += 1

    def load_history_file(self, path: str) -> int:
        prep = TransactionPreparer()
        txs = prep.prepare_many(read_records(path), os.path.basename(path))
        self.history.extend(txs)
        n = self.engine.fit(self.history)
        self._log(f"history file {os.path.basename(path)}: {len(txs):,} accepted, {prep.stats['rejected']} rejected; "
                  f"retrained on {n:,} transactions")
        self.version += 1
        return len(txs)

    def score_file(self, path: str) -> int:
        prep = TransactionPreparer()
        txs = prep.prepare_many(read_records(path), os.path.basename(path))
        n = self.process(txs)
        self._log(f"file {os.path.basename(path)}: {n:,} transactions scored ({prep.stats['rejected']} rejected)")
        return n

    def retrain(self) -> None:
        """Refit all profiles on history + scored transactions not considered fraud."""
        data = list(self.history) + [t for t, r in self.pairs if (not r.suspicious and r.status != "fraud")
                                     or r.status == "legit"]
        if data:
            n = self.engine.fit(data)
            self._log(f"retrained on {n:,} transactions - {self.engine.state}")
            self.version += 1

    def apply_config(self, config: Config, reevaluate: bool = True) -> None:
        self.config = config
        self.engine.apply_config(config)
        self.reports.config = config
        if reevaluate and self.pairs:
            for tx, result in self.pairs:
                self.engine.decide(result, tx)
            self.storage.save(self.pairs)
            self._log(f"thresholds applied to {len(self.pairs):,} transactions "
                      f"({sum(1 for _, r in self.pairs if r.suspicious):,} suspicious)")
        self.version += 1

    def save_config(self) -> str:
        self.config.data["channels"] = self.manager.specs_list()
        return self.config.save(self.config_path)

    # ---------------------------------------------------------- queries
    def customers(self) -> list[dict]:
        agg: dict[str, dict] = defaultdict(lambda: {"n": 0, "suspicious": 0, "max": 0.0, "sum": 0.0, "last": None,
                                                    "bank": "", "amount": 0.0, "fraud": 0})
        for tx, r in self.pairs:
            a = agg[tx.user_id]
            a["n"] += 1
            a["suspicious"] += int(r.suspicious)
            a["max"] = max(a["max"], r.combined_score)
            a["sum"] += r.combined_score
            a["amount"] += tx.amount
            a["bank"] = tx.bank_id
            a["fraud"] += int(r.status == "fraud")
            if a["last"] is None or tx.timestamp > a["last"]:
                a["last"] = tx.timestamp
        out = []
        gl = self.engine.global_
        for user, a in agg.items():
            out.append({"user_id": user, "bank_id": a["bank"], "transactions": a["n"], "suspicious": a["suspicious"],
                        "max_score": a["max"], "avg_score": a["sum"] / a["n"] if a["n"] else 0.0,
                        "amount": a["amount"], "last": a["last"], "cluster": gl.labels.get(user),
                        "global_score": gl.user_scores.get(user), "confirmed_fraud": a["fraud"]})
        return out

    def shutdown(self) -> None:
        self.manager.stop_all()
        if self.model_path and self.engine.trained:
            try:
                self.engine.save_model(self.model_path)
            except Exception:  # noqa: BLE001
                pass
        self.storage.close()
