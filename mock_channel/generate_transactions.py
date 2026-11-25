#!/usr/bin/env python3
"""Mock data channel: generate online-banking transactions (with injected fraud).

Examples
--------
Write 60 days of history to a CSV file (for training / batch analysis)::

    python mock_channel/generate_transactions.py --days 60 --output csv --file data/history.csv

Stream live transactions to the desktop application's TCP channel::

    python mock_channel/generate_transactions.py --days 0 --output tcp --port 9009 --live --rate 5

Stream to the HTTP webhook channel, after first sending 30 days of history::

    python mock_channel/generate_transactions.py --days 30 --output http \
        --url http://127.0.0.1:8088/transactions --live --rate 10

Fill (and keep appending to) an SQLite "core banking" table::

    python mock_channel/generate_transactions.py --days 30 --output sqlite --file data/core_bank.db --live
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import socket
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
import time
import urllib.request
from typing import Iterable, Iterator

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mock_channel.generator import TransactionGenerator  # noqa: E402

COLUMNS = ["tx_id", "bank_id", "user_id", "timestamp", "tx_type", "amount", "currency", "iban", "ip", "cc_asn",
           "session_id", "card_id", "phone_number", "operator", "is_fraud", "scenario"]


class Sink:
    def write(self, record: dict) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class CSVSink(Sink):
    def __init__(self, path: str, append: bool = False):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        exists = append and os.path.exists(path) and os.path.getsize(path) > 0
        self.fh = open(path, "a" if append else "w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.fh, fieldnames=COLUMNS)
        if not exists:
            self.writer.writeheader()

    def write(self, record: dict) -> None:
        self.writer.writerow({k: ("" if record.get(k) is None else record.get(k)) for k in COLUMNS})
        self.fh.flush()

    def close(self) -> None:
        self.fh.close()


class JSONLSink(Sink):
    def __init__(self, path: str | None):
        self.fh = open(path, "w", encoding="utf-8") if path else sys.stdout

    def write(self, record: dict) -> None:
        self.fh.write(json.dumps(record) + "\n")
        self.fh.flush()

    def close(self) -> None:
        if self.fh is not sys.stdout:
            self.fh.close()


class TCPSink(Sink):
    def __init__(self, host: str, port: int):
        self.sock = socket.create_connection((host, port), timeout=10)

    def write(self, record: dict) -> None:
        self.sock.sendall((json.dumps(record) + "\n").encode("utf-8"))

    def close(self) -> None:
        self.sock.close()


class HTTPSink(Sink):
    def __init__(self, url: str, batch: int = 1, token: str = "", retries: int = 60):
        self.url, self.batch, self.buffer = url, max(1, batch), []
        self.token, self.retries = token, retries
        self.history = False

    def write(self, record: dict) -> None:
        self.buffer.append(record)
        if len(self.buffer) >= (500 if self.history else self.batch):
            self.flush()

    def flush(self) -> None:
        if not self.buffer:
            return
        payload = {"history": True, "transactions": self.buffer} if self.history else \
            (self.buffer if len(self.buffer) > 1 else self.buffer[0])
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(self.retries):   # the receiving channel may still be starting
            try:
                req = urllib.request.Request(self.url, data=json.dumps(payload).encode("utf-8"), headers=headers,
                                             method="POST")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    resp.read()
                break
            except OSError as exc:
                if attempt == self.retries - 1:
                    raise
                print(f"waiting for {self.url}: {exc}", file=sys.stderr)
                time.sleep(2)
        self.buffer = []

    def close(self) -> None:
        self.flush()


class SQLiteSink(Sink):
    def __init__(self, path: str, table: str = "transactions"):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.table = table
        cols = ", ".join(f"{c} TEXT" if c not in ("amount", "is_fraud") else f"{c} REAL" for c in COLUMNS)
        self.conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (rowid INTEGER PRIMARY KEY AUTOINCREMENT, {cols})")
        self.conn.commit()
        self.pending = 0

    def write(self, record: dict) -> None:
        placeholders = ", ".join("?" for _ in COLUMNS)
        self.conn.execute(f"INSERT INTO {self.table} ({', '.join(COLUMNS)}) VALUES ({placeholders})",
                          [record.get(c) for c in COLUMNS])
        self.pending += 1
        if self.pending >= 200:
            self.conn.commit()
            self.pending = 0

    def flush(self) -> None:
        self.conn.commit()
        self.pending = 0

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


def make_sink(args) -> Sink:
    if args.output == "csv":
        return CSVSink(args.file or "data/transactions.csv", append=args.append)
    if args.output == "jsonl":
        return JSONLSink(args.file or "data/transactions.jsonl")
    if args.output == "stdout":
        return JSONLSink(None)
    if args.output == "tcp":
        return TCPSink(args.host, args.port)
    if args.output == "http":
        return HTTPSink(args.url, args.batch, args.token)
    if args.output == "sqlite":
        return SQLiteSink(args.file or "data/core_bank.db", args.table)
    raise SystemExit(f"unknown output {args.output}")


def paced(records: Iterable[dict], rate: float) -> Iterator[dict]:
    interval = 1.0 / rate if rate > 0 else 0.0
    for record in records:
        yield record
        if interval:
            time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--customers", type=int, default=150)
    p.add_argument("--days", type=int, default=60, help="days of history to emit first (0 = none)")
    p.add_argument("--fraud-ratio", type=float, default=0.03, help="fraud share in the live stream")
    p.add_argument("--history-fraud-ratio", type=float, default=0.005, help="fraud share in the history")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--banks", default="BANK001:IT,BANK002:DE", help="bank_id:country list")
    p.add_argument("--output", choices=["csv", "jsonl", "stdout", "tcp", "http", "sqlite"], default="csv")
    p.add_argument("--file", help="output file for csv/jsonl/sqlite")
    p.add_argument("--append", action="store_true", help="append to an existing CSV")
    p.add_argument("--table", default="transactions")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9009)
    p.add_argument("--url", default="http://127.0.0.1:8088/transactions")
    p.add_argument("--batch", type=int, default=1, help="records per HTTP POST")
    p.add_argument("--token", default=os.environ.get("CHANNEL_TOKEN", ""), help="bearer token of the HTTP channel")
    p.add_argument("--live", action="store_true", help="keep streaming after the history")
    p.add_argument("--rate", type=float, default=5.0, help="live transactions per second (0 = no pause)")
    p.add_argument("--count", type=int, default=0, help="stop the live stream after N records (0 = never)")
    p.add_argument("--currency", default="EUR")
    p.add_argument("--now", action="store_true", help="stamp live records with the current time")
    p.add_argument("--verishield", action="store_true",
                   help="feed a VeriShield Demo workspace: same customers as the demo (BOP, PS, ILS, seed 2026, "
                        "180 customers), live stream only, current timestamps, unique ids")
    args = p.parse_args(argv)

    if args.verishield:
        args.banks, args.currency, args.seed, args.days, args.live, args.now = "BOP:PS", "ILS", 2026, 0, True, True
        if args.customers == 150:
            args.customers = 180
        if args.rate == 5.0:
            args.rate = 1.0
    banks = dict(item.split(":") for item in args.banks.split(",") if item)
    gen = TransactionGenerator(args.customers, banks=banks, seed=args.seed, fraud_ratio=args.fraud_ratio,
                               currency=args.currency)
    run_id = uuid.uuid4().hex[:6].upper()
    sink = make_sink(args)
    written = frauds = 0
    try:
        if args.days > 0:
            if isinstance(sink, HTTPSink):
                sink.history = True     # sent as {"history": true, ...}: used to train the profiles
            for record in gen.history(args.days, args.history_fraud_ratio):
                sink.write(record)
                written += 1
                frauds += record["is_fraud"]
            if hasattr(sink, "flush"):
                sink.flush()
            if isinstance(sink, HTTPSink):
                sink.history = False
            print(f"history: {written} transactions ({frauds} fraudulent) over {args.days} days", file=sys.stderr)
        if args.live:
            live = 0
            print(f"live stream: {args.rate:g} tx/s to {args.output} (Ctrl+C to stop)", file=sys.stderr)
            for record in paced(gen.live(), args.rate):
                if args.now:
                    record["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    record["tx_id"] = f"FEED-{run_id}-{live + 1:07d}"
                sink.write(record)
                live += 1
                frauds += record["is_fraud"]
                if hasattr(sink, "flush") and args.output == "sqlite" and live % 20 == 0:
                    sink.flush()
                if args.count and live >= args.count:
                    break
            written += live
    except KeyboardInterrupt:
        pass
    finally:
        sink.close()
    print(f"done: {written} transactions written, {frauds} fraudulent", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
