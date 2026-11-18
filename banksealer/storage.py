"""SQLite persistence for prepared transactions, scores and analyst feedback."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Iterable

from .models import Reason, ScoreResult, Transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    tx_id TEXT PRIMARY KEY, bank_id TEXT, channel TEXT, user_id TEXT, ts TEXT, tx_type TEXT,
    amount REAL, currency TEXT, iban TEXT, iban_cc TEXT, ip TEXT, cc_asn TEXT, session_id TEXT,
    card_id TEXT, phone_number TEXT, operator TEXT, label INTEGER, scenario TEXT
);
CREATE INDEX IF NOT EXISTS ix_tx_user ON transactions(user_id);
CREATE TABLE IF NOT EXISTS scores (
    tx_id TEXT PRIMARY KEY REFERENCES transactions(tx_id), local REAL, temporal REAL, global REAL,
    combined REAL, risk_level TEXT, suspicious INTEGER, reasons TEXT, rule_reasons TEXT,
    explanation TEXT, raw TEXT, cluster INTEGER, undertrained INTEGER, status TEXT DEFAULT 'open',
    scored_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_scores_susp ON scores(suspicious);
CREATE TABLE IF NOT EXISTS reports (
    tx_id TEXT, path TEXT, created_at TEXT
);
"""
TX_COLUMNS = ("tx_id", "bank_id", "channel", "user_id", "ts", "tx_type", "amount", "currency", "iban", "iban_cc",
              "ip", "cc_asn", "session_id", "card_id", "phone_number", "operator", "label", "scenario")


class Storage:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def save(self, pairs: Iterable[tuple[Transaction, ScoreResult]]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        tx_rows, score_rows = [], []
        for tx, r in pairs:
            d = tx.to_dict()
            d["ts"] = d.pop("timestamp")
            tx_rows.append(tuple(d.get(c) for c in TX_COLUMNS))
            score_rows.append((r.tx_id, r.local_score, r.temporal_score, r.global_score, r.combined_score,
                               r.risk_level, int(r.suspicious), json.dumps([x.to_dict() for x in r.reasons]),
                               json.dumps([x.to_dict() for x in r.rule_reasons]), r.explanation,
                               json.dumps(r.raw, default=str), r.cluster, int(r.undertrained), r.status, now))
        self.conn.executemany(f"INSERT OR REPLACE INTO transactions ({', '.join(TX_COLUMNS)}) "
                              f"VALUES ({', '.join('?' for _ in TX_COLUMNS)})", tx_rows)
        self.conn.executemany("INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", score_rows)
        self.conn.commit()

    def set_status(self, tx_id: str, status: str) -> None:
        self.conn.execute("UPDATE scores SET status=? WHERE tx_id=?", (status, tx_id))
        self.conn.commit()

    def add_report(self, tx_id: str, path: str) -> None:
        self.conn.execute("INSERT INTO reports VALUES (?,?,?)", (tx_id, path, datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()

    def load(self, limit: int = 50000) -> list[tuple[Transaction, ScoreResult]]:
        cur = self.conn.execute(
            f"SELECT {', '.join('t.' + c for c in TX_COLUMNS)}, s.local, s.temporal, s.global, s.combined, s.risk_level, "
            "s.suspicious, s.reasons, s.rule_reasons, s.explanation, s.raw, s.cluster, s.undertrained, s.status "
            "FROM transactions t JOIN scores s ON s.tx_id = t.tx_id ORDER BY t.ts DESC LIMIT ?", (limit,))
        out = []
        for row in cur.fetchall():
            d = dict(zip(TX_COLUMNS, row[:len(TX_COLUMNS)]))
            d["timestamp"] = datetime.fromisoformat(d.pop("ts"))
            tx = Transaction(**d)
            (local, temporal, glob, combined, level, susp, reasons, rules, expl, raw, cluster, under, status) = \
                row[len(TX_COLUMNS):]
            r = ScoreResult(tx_id=tx.tx_id, user_id=tx.user_id, bank_id=tx.bank_id, local_score=local,
                            temporal_score=temporal, global_score=glob, combined_score=combined, risk_level=level,
                            suspicious=bool(susp), reasons=[Reason.from_dict(x) for x in json.loads(reasons or "[]")],
                            rule_reasons=[Reason.from_dict(x) for x in json.loads(rules or "[]")],
                            explanation=expl or "", raw=json.loads(raw or "{}"), cluster=cluster,
                            undertrained=bool(under), status=status or "open")
            out.append((tx, r))
        out.reverse()
        return out

    def clear(self) -> None:
        self.conn.executescript("DELETE FROM scores; DELETE FROM transactions; DELETE FROM reports;")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
