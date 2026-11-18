"""Command line interface (headless use, batch scoring and evaluation).

    python -m banksealer.cli score --history data/history.csv --input data/new.csv --out reports/
    python -m banksealer.cli evaluate --customers 150 --days 45 --live-days 10
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict

from .config import Config
from .engine import BankSealerEngine
from .preparation import TransactionPreparer


def read_records(path: str) -> list[dict]:
    if path.endswith(".jsonl") or path.endswith(".json"):
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
        if text.startswith("["):
            return json.loads(text)
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def auc(scores: list[float], labels: list[int]) -> float:
    pos = [s for s, l in zip(scores, labels) if l]
    neg = [s for s, l in zip(scores, labels) if not l]
    if not pos or not neg:
        return float("nan")
    import numpy as np
    neg_sorted = np.sort(neg)
    wins = sum(np.searchsorted(neg_sorted, p, side="left") + 0.5 * (
        np.searchsorted(neg_sorted, p, side="right") - np.searchsorted(neg_sorted, p, side="left")) for p in pos)
    return float(wins / (len(pos) * len(neg)))


def metrics(pairs) -> dict:
    """pairs: list of (Transaction, ScoreResult)."""
    labelled = [(t, r) for t, r in pairs if t.label is not None]
    tp = sum(1 for t, r in labelled if t.label and r.suspicious)
    fp = sum(1 for t, r in labelled if not t.label and r.suspicious)
    fn = sum(1 for t, r in labelled if t.label and not r.suspicious)
    out = {
        "transactions": len(pairs), "frauds": sum(1 for t, _ in labelled if t.label),
        "flagged": sum(1 for _, r in pairs if r.suspicious), "tp": tp, "fp": fp, "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else 0.0, "recall": tp / (tp + fn) if tp + fn else 0.0,
        "auc_combined": auc([r.combined_score for _, r in labelled], [t.label for t, _ in labelled]),
        "auc_local": auc([r.local_score for _, r in labelled], [t.label for t, _ in labelled]),
        "auc_temporal": auc([r.temporal_score for _, r in labelled], [t.label for t, _ in labelled]),
        "auc_global": auc([r.global_score for _, r in labelled], [t.label for t, _ in labelled]),
    }
    per = defaultdict(lambda: [0, 0])
    for t, r in labelled:
        if t.label:
            per[t.scenario or "unknown"][0] += 1
            per[t.scenario or "unknown"][1] += int(r.suspicious)
    out["recall_by_scenario"] = {k: f"{v[1]}/{v[0]}" for k, v in sorted(per.items())}
    return out


def cmd_score(args) -> int:
    config = Config.load(args.config)
    engine = BankSealerEngine(config)
    prep = TransactionPreparer()
    history = prep.prepare_many(read_records(args.history), "history")
    engine.fit(history)
    txs = prep.prepare_many(read_records(args.input), "input")
    pairs = [(t, engine.score(t)) for t in txs]
    os.makedirs(args.out, exist_ok=True)
    out_csv = os.path.join(args.out, "scores.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["tx_id", "user_id", "bank_id", "timestamp", "tx_type", "amount", "local", "temporal", "global",
                    "combined", "risk_level", "suspicious", "explanation"])
        for t, r in pairs:
            w.writerow([t.tx_id, t.user_id, t.bank_id, t.timestamp, t.tx_type, t.amount, r.local_score,
                        r.temporal_score, r.global_score, r.combined_score, r.risk_level, int(r.suspicious),
                        r.explanation])
    print(f"scored {len(pairs)} transactions -> {out_csv}")
    if args.reports:
        from .reports import ReportGenerator
        gen = ReportGenerator(config, args.out)
        flagged = [(t, r) for t, r in pairs if r.suspicious]
        for t, r in flagged:
            gen.transaction_report(t, r, engine.customer_summary(t.user_id))
        path = gen.summary_report(pairs)
        print(f"{len(flagged)} transaction reports + summary {path}")
    if any(t.label is not None for t in txs):
        print(json.dumps(metrics(pairs), indent=2))
    if prep.errors:
        print(f"{prep.stats['rejected']} records rejected, e.g.: {list(prep.errors)[:3]}", file=sys.stderr)
    return 0


def cmd_evaluate(args) -> int:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from mock_channel.generator import TransactionGenerator

    config = Config.load(args.config)
    gen = TransactionGenerator(args.customers, seed=args.seed, fraud_ratio=args.fraud_ratio)
    prep = TransactionPreparer()
    history = prep.prepare_many(gen.history(args.days, 0.0), "mock")
    engine = BankSealerEngine(config)
    engine.fit(history)
    live_records = []
    stream = gen.live()
    start_day = gen.cursor
    for record in stream:
        if gen.cursor - start_day >= __import__("datetime").timedelta(days=args.live_days):
            break
        live_records.append(record)
    txs = prep.prepare_many(live_records, "mock")
    pairs = [(t, engine.score(t)) for t in txs]
    print(f"engine: {engine.state}")
    print(json.dumps(metrics(pairs), indent=2))
    if args.show:
        for t, r in [p for p in pairs if p[1].suspicious][: args.show]:
            print("-" * 80)
            print(t.tx_id, t.user_id, t.tx_type, t.amount, "fraud" if t.label else "legit", t.scenario or "")
            print(r.explanation)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="banksealer")
    p.add_argument("--config", default="config/banksealer.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score", help="train on a history file and score another file")
    s.add_argument("--history", required=True)
    s.add_argument("--input", required=True)
    s.add_argument("--out", default="reports")
    s.add_argument("--reports", action="store_true", help="also write PDF reports")
    s.set_defaults(func=cmd_score)
    e = sub.add_parser("evaluate", help="evaluate detection quality on mock data")
    e.add_argument("--customers", type=int, default=150)
    e.add_argument("--days", type=int, default=45)
    e.add_argument("--live-days", type=int, default=10)
    e.add_argument("--fraud-ratio", type=float, default=0.03)
    e.add_argument("--seed", type=int, default=7)
    e.add_argument("--show", type=int, default=0, help="print N explanations")
    e.set_defaults(func=cmd_evaluate)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
