"""BankSealer processing worker.

Loop (every WORKER_POLL_SECONDS):
  1. hot-reload the configuration from the database (thresholds, engine settings, banks)
  2. reconcile channels with their configuration rows (start/stop/restart on change)
  3. drain the channels -> data preparation -> training data or scoring
  4. persist transactions, scores, rejected records, auto-generated reports
  5. publish channel status, engine status and customer profile summaries
  6. execute commands requested through the API (retrain, re-evaluate, learn, files ...)
"""
from __future__ import annotations

import logging
import os
import signal
import time
from collections import Counter
from datetime import datetime

from sqlalchemy import delete, func, or_, select, update

from banksealer.channels import ChannelManager
from banksealer.cli import read_records
from banksealer.engine import BankSealerEngine
from banksealer.models import RISK_LEVELS, Transaction
from banksealer.preparation import TransactionPreparer
from banksealer.reports import ReportGenerator

from .. import db
from ..bootstrap import init_database
from ..config_store import channel_spec, config_version, load_config
from ..mapping import row_to_result, row_to_tx, score_values, tx_values
from ..models import (ChannelConfig, ChannelStatus, Command, CustomerProfile, EngineStatus, ModelBlob,
                      PreparationError, Report, ScoreRow, TransactionRow, now)
from ..settings import settings

log = logging.getLogger("banksealer.worker")


class Worker:
    def __init__(self):
        self.manager = ChannelManager()
        self.versions: dict[str, tuple[int, int]] = {}   # channel name -> (id, version)
        self.config = None
        self.config_ver = None
        self.engine: BankSealerEngine | None = None
        self.history_pending = False
        self.last_history_at = 0.0
        self.dirty_users: set[str] = set()
        self.rejections_seen: dict[str, int] = {}
        self.history_counts: Counter = Counter()
        self.model_dirty = False
        self.last_model_save = time.time()
        self.scored_total = 0
        self.running = True

    # ------------------------------------------------------------ startup
    def startup(self) -> None:
        init_database()
        with db.session_scope() as s:
            self.config = load_config(s)
            self.config_ver = config_version(s)
            self.engine = BankSealerEngine(self.config)
            blob = s.scalar(select(ModelBlob).order_by(ModelBlob.id.desc()).limit(1))
            if blob is not None:
                try:
                    self.engine.loads(blob.data)
                    log.info("model restored: %s", self.engine.state)
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not restore model: %s", exc)
            self.scored_total = s.scalar(select(func.count(ScoreRow.tx_id))) or 0
            s.execute(update(Command).where(Command.status == "running").values(status="pending"))
        self.reports = ReportGenerator(self.config, settings.reports_dir)

    # --------------------------------------------------------------- loop
    def run(self) -> None:
        self.startup()
        log.info("worker started")
        while self.running:
            started = time.time()
            try:
                self.step()
            except Exception:  # noqa: BLE001
                log.exception("worker step failed")
                time.sleep(2)
            time.sleep(max(0.05, settings.worker_poll_seconds - (time.time() - started)))
        self.manager.stop_all()
        self.save_model(force=True)

    def step(self) -> None:
        with db.session_scope() as s:
            self.reload_config(s)
            self.reconcile_channels(s)
        history, live = self.manager.drain(settings.worker_batch)
        if history:
            self.store_history(history)
        if self.history_pending and (live or time.time() - self.last_history_at > 3.0):
            self.fit_from_database("history received")
        if live:
            self.process(live)
        with db.session_scope() as s:
            self.publish_channels(s)
            self.run_commands(s)
        self.flush_profiles()
        self.publish_engine()
        self.save_model()

    # -------------------------------------------------------- configuration
    def reload_config(self, s) -> None:
        version = config_version(s)
        if version == self.config_ver:
            return
        self.config = load_config(s)
        self.config_ver = version
        self.engine.apply_config(self.config)
        self.reports.config = self.config
        log.info("configuration reloaded (%s)", version)

    def reconcile_channels(self, s) -> None:
        rows = {c.name: c for c in s.scalars(select(ChannelConfig))}
        for name in list(self.versions):
            row = rows.get(name)
            if row is None or not row.enabled or self.versions[name] != (row.id, row.version):
                self.manager.remove(name)
                self.versions.pop(name, None)
                self.rejections_seen.pop(name, None)
                if row is not None:
                    st = s.get(ChannelStatus, row.id)
                    if st:
                        st.status = "stopped"
                log.info("channel stopped: %s", name)
        for name, row in rows.items():
            if row.enabled and name not in self.versions:
                try:
                    channel = self.manager.add(channel_spec(row))
                    channel.start()
                    self.versions[name] = (row.id, row.version)
                    log.info("channel started: %s (%s)", name, row.type)
                except Exception as exc:  # noqa: BLE001
                    st = s.get(ChannelStatus, row.id) or ChannelStatus(channel_id=row.id)
                    st.status, st.last_error = "error", str(exc)
                    s.add(st)
                    self.versions[name] = (row.id, row.version)

    # ------------------------------------------------------------ training
    def _existing(self, s, ids: list[str]) -> set[str]:
        found = set()
        for i in range(0, len(ids), 900):
            found.update(s.scalars(select(TransactionRow.tx_id).where(TransactionRow.tx_id.in_(ids[i:i + 900]))))
        return found

    def store_history(self, history: list[Transaction]) -> None:
        with db.session_scope() as s:
            existing = self._existing(s, [t.tx_id for t in history])
            rows = [TransactionRow(**tx_values(t, training=True)) for t in history if t.tx_id not in existing]
            s.add_all(rows)
        for t in history:
            self.history_counts[t.channel] += 1
        if rows:   # replayed history (already stored) does not trigger a refit
            self.history_pending = True
            self.last_history_at = time.time()
        log.info("historical transactions: %d received, %d new stored for training", len(history), len(rows))

    def training_data(self, s, include_scored: bool = False) -> list[Transaction]:
        q = select(TransactionRow).outerjoin(ScoreRow, ScoreRow.tx_id == TransactionRow.tx_id)
        if include_scored:
            q = q.where(or_(TransactionRow.is_training.is_(True),
                            (ScoreRow.suspicious.is_(False) & (ScoreRow.status != "fraud")),
                            ScoreRow.status == "legit"))
        else:
            q = q.where(TransactionRow.is_training.is_(True))
        rows = s.scalars(q.order_by(TransactionRow.ts.desc()).limit(300_000)).all()
        return [row_to_tx(r) for r in reversed(rows)]

    def fit_from_database(self, reason: str, include_scored: bool = False) -> int:
        with db.session_scope() as s:
            data = self.training_data(s, include_scored)
        if not data:
            return 0
        started = time.time()
        n = self.engine.fit(data)
        self.history_pending = False
        self.model_dirty = True
        self.dirty_users.update(self.engine.global_.labels)
        with db.session_scope() as s:
            st = self._engine_row(s)
            st.last_fit = now()
        log.info("profiles fitted on %d transactions in %.1fs (%s): %s", n, time.time() - started, reason,
                 self.engine.state)
        self.save_model(force=True)
        return n

    # ------------------------------------------------------------- scoring
    def process(self, transactions: list[Transaction]) -> int:
        with db.session_scope() as s:
            existing = self._existing(s, [t.tx_id for t in transactions])
        transactions = [t for t in transactions if t.tx_id not in existing]
        scored, training = [], []
        was_trained = self.engine.trained
        for tx in transactions:
            result = self.engine.ingest(tx)
            if result is None:
                training.append(tx)
                continue
            scored.append((tx, result))
        if not was_trained and self.engine.trained:
            self.model_dirty = True
            self.dirty_users.update(self.engine.global_.labels)
            log.info("profiles fitted on buffered live data: %s", self.engine.state)
        with db.session_scope() as s:
            s.add_all([TransactionRow(**tx_values(t, training=True)) for t in training])
            s.add_all([TransactionRow(**tx_values(t)) for t, _ in scored])
            s.flush()
            s.add_all([ScoreRow(**score_values(r)) for _, r in scored])
        if scored:
            self.model_dirty = True
            self.scored_total += len(scored)
            self.dirty_users.update(t.user_id for t, _ in scored)
            flagged = [(t, r) for t, r in scored if r.suspicious]
            self.auto_reports(flagged)
            log.info("scored %d transactions, %d suspicious", len(scored), len(flagged))
        return len(scored)

    def auto_reports(self, flagged) -> None:
        app = self.config.section("app")
        if not flagged or not app.get("auto_report", False):
            return
        minimum = str(app.get("auto_report_min_level", "MEDIUM")).upper()
        min_idx = RISK_LEVELS.index(minimum) if minimum in RISK_LEVELS else 1
        rows = []
        for tx, r in flagged:
            if RISK_LEVELS.index(r.risk_level) < min_idx:
                continue
            try:
                path = self.reports.transaction_report(tx, r, self.engine.customer_summary(tx.user_id),
                                                       self.engine.ranked_reasons(r))
                rows.append(Report(tx_id=tx.tx_id, kind="transaction", path=path, created_by="auto"))
            except Exception as exc:  # noqa: BLE001
                log.warning("report failed for %s: %s", tx.tx_id, exc)
        if rows:
            with db.session_scope() as s:
                s.add_all(rows)

    # ------------------------------------------------------------ publishing
    def publish_channels(self, s) -> None:
        for channel in self.manager.channels:
            row_id = self.versions.get(channel.name, (None,))[0]
            if row_id is None:
                continue
            st = s.get(ChannelStatus, row_id) or ChannelStatus(channel_id=row_id)
            stats = channel.preparer.stats
            st.status = channel.status
            st.received, st.accepted = channel.received, stats["accepted"]
            st.rejected, st.duplicates = stats["rejected"], stats["duplicates"]
            st.history = self.history_counts.get(channel.name, 0)
            st.last_error = channel.last_error or None
            st.heartbeat = now()
            s.add(st)
            seen = self.rejections_seen.get(channel.name, 0)
            new = stats["rejected"] - seen
            if new > 0:
                for message, raw in list(channel.preparer.rejections)[-min(new, 200):]:
                    s.add(PreparationError(channel=channel.name, message=message, raw=_safe_json(raw)))
                self.rejections_seen[channel.name] = stats["rejected"]

    def _engine_row(self, s) -> EngineStatus:
        st = s.get(EngineStatus, 1)
        if st is None:
            st = EngineStatus(id=1)
            s.add(st)
        return st

    def publish_engine(self) -> None:
        with db.session_scope() as s:
            st = self._engine_row(s)
            e = self.engine
            st.state = e.state
            st.trained = e.trained
            st.training_size = e.training_size
            st.buffered = len(e.training_buffer)
            st.customers = len(e.global_.labels) if e.trained else 0
            st.peer_groups = len(getattr(e.global_, "centroids", {})) if e.trained else 0
            st.scored_total = self.scored_total
            st.heartbeat = now()
            st.config_version = self.config_ver

    def flush_profiles(self, limit: int = 500) -> None:
        if not self.dirty_users or not self.engine.trained:
            return
        users = [self.dirty_users.pop() for _ in range(min(limit, len(self.dirty_users)))]
        gl = self.engine.global_
        with db.session_scope() as s:
            existing = {p.user_id: p for p in s.scalars(select(CustomerProfile).where(CustomerProfile.user_id.in_(users)))}
            for user in users:
                summary = _safe_json(self.engine.customer_summary(user))
                p = existing.get(user) or CustomerProfile(user_id=user)
                p.summary = summary
                p.cluster = gl.labels.get(user)
                score = gl.user_scores.get(user)
                p.global_score = float(score) if score is not None else None
                p.updated_at = now()
                s.add(p)

    def save_model(self, force: bool = False) -> None:
        if not self.engine or not self.engine.trained:
            return
        if not force and (not self.model_dirty or time.time() - self.last_model_save < 120):
            return
        data = self.engine.dumps()
        with db.session_scope() as s:
            s.execute(delete(ModelBlob))
            s.add(ModelBlob(id=1, data=data))
        self.model_dirty = False
        self.last_model_save = time.time()

    # ------------------------------------------------------------- commands
    def run_commands(self, s) -> None:
        commands = s.scalars(select(Command).where(Command.status == "pending").order_by(Command.id).limit(5)).all()
        for cmd in commands:
            cmd.status = "running"
            s.commit()
            try:
                result = self.execute(cmd.kind, cmd.payload or {})
                cmd.status, cmd.result = "done", result
            except Exception as exc:  # noqa: BLE001
                log.exception("command %s failed", cmd.kind)
                cmd.status, cmd.result = "failed", {"error": f"{type(exc).__name__}: {exc}"}
            cmd.finished_at = now()
            s.commit()

    def execute(self, kind: str, payload: dict) -> dict:
        if kind == "retrain":
            n = self.fit_from_database("retrain requested", include_scored=True)
            return {"trained_on": n, "state": self.engine.state}
        if kind == "reevaluate":
            return {"updated": self.reevaluate()}
        if kind == "learn":
            with db.session_scope() as s:
                row = s.get(TransactionRow, payload["tx_id"])
                if row is None:
                    return {"learned": False}
                self.engine.learn(row_to_tx(row))
            self.model_dirty = True
            self.dirty_users.add(row.user_id)
            return {"learned": True}
        if kind in ("train_file", "score_file"):
            prep = TransactionPreparer(payload.get("field_mapping"), rules=payload.get("preparation"))
            txs = prep.prepare_many(read_records(payload["path"]), os.path.basename(payload["path"]))
            with db.session_scope() as s:
                for message, raw in list(prep.rejections):
                    s.add(PreparationError(channel=f"file:{os.path.basename(payload['path'])}", message=message,
                                           raw=_safe_json(raw)))
            if kind == "train_file":
                self.store_history(txs)
                n = self.fit_from_database("history file uploaded")
                return {"accepted": len(txs), "rejected": prep.stats["rejected"], "trained_on": n}
            n = 0
            for i in range(0, len(txs), 2000):
                n += self.process(txs[i:i + 2000])
            return {"accepted": len(txs), "rejected": prep.stats["rejected"], "scored": n}
        if kind == "reset_model":
            self.engine = BankSealerEngine(self.config)
            with db.session_scope() as s:
                s.execute(delete(ModelBlob))
            return {"state": self.engine.state}
        if kind == "purge_data":
            with db.session_scope() as s:
                for model in (Report, ScoreRow, PreparationError, CustomerProfile, TransactionRow):
                    s.execute(delete(model))
            self.engine = BankSealerEngine(self.config)
            self.scored_total = 0
            return {"purged": True}
        raise ValueError(f"unknown command {kind}")

    def reevaluate(self, chunk: int = 2000) -> int:
        updated, last = 0, ""
        while True:
            with db.session_scope() as s:
                rows = s.execute(select(TransactionRow, ScoreRow).join(ScoreRow, ScoreRow.tx_id == TransactionRow.tx_id)
                                 .where(TransactionRow.tx_id > last).order_by(TransactionRow.tx_id).limit(chunk)).all()
                if not rows:
                    return updated
                for tx_row, score_row in rows:
                    result = row_to_result(tx_row, score_row)
                    self.engine.decide(result, row_to_tx(tx_row))
                    values = score_values(result)
                    for key in ("combined", "risk_level", "suspicious", "reasons", "rule_reasons", "explanation"):
                        setattr(score_row, key, values[key])
                last = rows[-1][0].tx_id
                updated += len(rows)


def _safe_json(value):
    if isinstance(value, dict):
        return {str(k): _safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker = Worker()

    def stop(*_):
        worker.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.run()


if __name__ == "__main__":
    main()
