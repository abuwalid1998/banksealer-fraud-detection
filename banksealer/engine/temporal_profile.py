"""BankSealer temporal profile.

The temporal profile captures the *rhythm* of a customer: for each transaction
it aggregates the customer's activity over a sliding time window (default 24h)
into features such as number of operations, total amount, number of distinct
beneficiaries/connections and transfers abroad. During training the same
aggregates are computed for every past transaction, and their per-customer mean
and standard deviation define a personal threshold

    threshold_f = mean_f + k * std_f

A new transaction whose window exceeds a threshold is anomalous; the score is
the weighted sum of the standardised excesses the transaction is responsible for

    raw = sum_f w_f * max(0, (x_f - max(threshold_f, x_f_before)) / std_f)

where ``x_f_before`` is the window value without the transaction (so activity
already above threshold, e.g. an earlier fraud, is not charged again).

Undertrained customers (few samples) have mean/std shrunk towards population
statistics, exactly like the local profile.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable

import numpy as np

from ..models import Reason, Transaction

FEATURES = ("tx_count", "total_amount", "max_amount", "distinct_beneficiaries", "distinct_cc_asn", "foreign_count")
LABELS = {
    "tx_count": "operations", "total_amount": "total amount", "max_amount": "largest single amount",
    "distinct_beneficiaries": "distinct beneficiaries", "distinct_cc_asn": "distinct connections (country/ASN)",
    "foreign_count": "transfers to foreign IBANs",
}
# absolute lower bounds for the standard deviation (avoid zero-variance blow-ups)
MIN_STD = {"tx_count": 0.5, "total_amount": 25.0, "max_amount": 25.0, "distinct_beneficiaries": 0.5,
           "distinct_cc_asn": 0.5, "foreign_count": 0.5}


class Welford:
    __slots__ = ("n", "mean", "m2")

    def __init__(self, size: int):
        self.n = 0
        self.mean = np.zeros(size)
        self.m2 = np.zeros(size)

    def add(self, x: np.ndarray) -> None:
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (x - self.mean)

    @property
    def std(self) -> np.ndarray:
        return np.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else np.zeros_like(self.mean)


@dataclass
class TemporalScore:
    raw: float
    score: float
    values: dict
    thresholds: dict
    reasons: list
    undertrained: bool


class TemporalProfiler:
    def __init__(self, config: dict):
        self.cfg = config
        self.window = timedelta(hours=float(config.get("window_hours", 24)))
        self.recent: dict[str, deque] = defaultdict(deque)   # sliding window per user
        self.stats: dict[str, Welford] = {}
        self.population = Welford(len(FEATURES))
        self.home: dict[str, str] = {}
        self._home_votes: dict[str, Counter] = defaultdict(Counter)
        self.fitted = False

    # ---------------------------------------------------------- features
    def _home_country(self, user_id: str) -> str | None:
        return self.home.get(user_id)

    def _vote_home(self, tx: Transaction) -> None:
        if tx.ip_country:
            votes = self._home_votes[tx.user_id]
            votes[tx.ip_country] += 1
            self.home[tx.user_id] = votes.most_common(1)[0][0]

    def _window_values(self, tx: Transaction, include: bool = True) -> np.ndarray:
        dq = self.recent[tx.user_id]
        start = tx.timestamp - self.window
        while dq and dq[0].timestamp < start:
            dq.popleft()
        items = [t for t in dq if t.tx_id != tx.tx_id] + ([tx] if include else [])
        home = self._home_country(tx.user_id)
        amounts = [t.amount for t in items]
        foreign = sum(1 for t in items if t.is_outflow and t.counterparty_country and home
                      and t.counterparty_country != home)
        return np.array([
            len(items),
            float(sum(amounts)),
            float(max(amounts)) if amounts else 0.0,
            len({(t.tx_type, t.beneficiary) for t in items}),
            len({t.cc_asn for t in items}),
            foreign,
        ], dtype=float)

    def _push(self, tx: Transaction) -> None:
        dq = self.recent[tx.user_id]
        dq.append(tx)

    # --------------------------------------------------------------- fit
    def fit(self, transactions: Iterable[Transaction]) -> None:
        txs = sorted(transactions, key=lambda t: t.timestamp)
        self.recent = defaultdict(deque)
        self.stats = {}
        self.population = Welford(len(FEATURES))
        self.home, self._home_votes = {}, defaultdict(Counter)
        for tx in txs:
            self._vote_home(tx)
        for tx in txs:
            values = self._window_values(tx)
            self._push(tx)
            self.stats.setdefault(tx.user_id, Welford(len(FEATURES))).add(values)
            self.population.add(values)
        self.fitted = True

    def observe(self, tx: Transaction) -> None:
        """Always record the transaction in the sliding window (velocity is real activity)."""
        self._push(tx)

    def learn(self, tx: Transaction, values: np.ndarray | None = None) -> None:
        """Update the customer's statistics with a transaction considered legitimate."""
        self._vote_home(tx)
        if values is None:
            values = self._window_values(tx)
        self.stats.setdefault(tx.user_id, Welford(len(FEATURES))).add(values)
        self.population.add(values)

    # ------------------------------------------------------------- score
    def _mean_std(self, user_id: str) -> tuple[np.ndarray, np.ndarray, bool, int]:
        min_windows = int(self.cfg.get("min_windows", 5))
        k = float(self.cfg.get("shrinkage_k", 10))
        pop_mean, pop_std = self.population.mean, self.population.std
        st = self.stats.get(user_id)
        n = st.n if st else 0
        if n == 0:
            return pop_mean.copy(), pop_std.copy(), True, 0
        mean, std = st.mean.copy(), st.std
        if n < min_windows:
            lam = n / (n + k)
            mean = lam * mean + (1 - lam) * pop_mean
            std = lam * std + (1 - lam) * pop_std
            return mean, std, True, n
        return mean, std, False, n

    def score(self, tx: Transaction, weights: dict) -> TemporalScore:
        sigma_k = float(self.cfg.get("sigma_multiplier", 3.0))
        scale = float(self.cfg.get("score_scale", 4.0))
        values = self._window_values(tx)
        before = self._window_values(tx, include=False)
        mean, std, undertrained, n = self._mean_std(tx.user_id)
        raw = 0.0
        reasons = []
        thresholds = {}
        window_h = self.window.total_seconds() / 3600.0
        for i, name in enumerate(FEATURES):
            s = max(float(std[i]), MIN_STD[name], 0.1 * float(mean[i]))
            thr = float(mean[i]) + sigma_k * s
            thresholds[name] = thr
            w = float(weights.get(name, 1.0))
            # only the excess this transaction adds: activity that was already above the
            # threshold before it (e.g. an earlier fraud) is not charged to it again
            excess = (values[i] - max(thr, float(before[i]))) / s
            if excess > 0 and w > 0:
                term = w * excess
                raw += term
                fmt = "{:,.2f}" if "amount" in name else "{:,.0f}"
                msg = (f"{fmt.format(values[i])} {LABELS[name]} in the last {window_h:g}h, above the personal "
                       f"threshold {fmt.format(thr)} (mean {fmt.format(mean[i])} ± {fmt.format(s)} × {sigma_k:g})")
                if undertrained:
                    msg += " (few past windows: blended with population behaviour)"
                reasons.append(Reason(profile="temporal", feature=name, message=msg, contribution=round(term, 3)))
        reasons.sort(key=lambda r: r.contribution, reverse=True)
        score = 1.0 - math.exp(-raw / scale) if scale > 0 else 0.0
        return TemporalScore(raw=raw, score=score, values=dict(zip(FEATURES, values.tolist())),
                             thresholds=thresholds, reasons=reasons, undertrained=undertrained)

    def summary(self, user_id: str) -> dict:
        mean, std, undertrained, n = self._mean_std(user_id)
        return {"samples": n, "undertrained": undertrained, "home_country": self.home.get(user_id),
                "mean": dict(zip(FEATURES, np.round(mean, 2).tolist())),
                "std": dict(zip(FEATURES, np.round(std, 2).tolist()))}
