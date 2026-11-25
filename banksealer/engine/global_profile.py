"""BankSealer global profile.

The global profile compares a customer with *other customers*. Every customer
is summarised by a spending-pattern vector (operations and amounts per
transaction type normalised to 30 days, average amount, number of beneficiaries,
share of foreign transfers, share of night-time operations). Vectors are
log-scaled, standardised and whitened, so that the Euclidean distance in the
whitened space is the Mahalanobis distance.

Customers are grouped with an *iterative DBSCAN*: DBSCAN runs with radius
``eps``; points left as noise are clustered again with a larger radius
(``eps * eps_growth``) for ``max_iterations`` rounds. The anomaly score of a
customer is the Cluster-Based Local Outlier Factor (CBLOF): clusters are split
into *large* and *small* (alpha/beta rule of He et al.), and

    CBLOF(p) = |C| * d(p, centroid(C))                if C is large
    CBLOF(p) = |C| * min_{L large} d(p, centroid(L))  if C is small / noise

(the ``|C|`` factor is optional, ``weight_by_cluster_size``).
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

import numpy as np

from ..models import Reason, Transaction

FEATURES = ("bt_per_30d", "bt_amount_30d", "pr_per_30d", "pr_amount_30d", "pc_per_30d", "pc_amount_30d",
            "mean_amount", "distinct_beneficiaries", "foreign_ratio", "night_ratio",
            # added in 1.3 - appended so that models trained on the first ten features keep working
            "cp_per_30d", "cp_amount_30d", "cash_out_30d", "cash_in_30d", "incoming_30d")
LABELS = {
    "bt_per_30d": ("bank transfers per 30 days", "{:,.1f}"),
    "bt_amount_30d": ("transferred amount per 30 days", "{:,.2f}"),
    "pr_per_30d": ("phone recharges per 30 days", "{:,.1f}"),
    "pr_amount_30d": ("recharged amount per 30 days", "{:,.2f}"),
    "pc_per_30d": ("prepaid-card top-ups per 30 days", "{:,.1f}"),
    "pc_amount_30d": ("prepaid-card amount per 30 days", "{:,.2f}"),
    "mean_amount": ("average amount per operation", "{:,.2f}"),
    "distinct_beneficiaries": ("distinct beneficiaries", "{:,.0f}"),
    "foreign_ratio": ("share of transfers to foreign IBANs", "{:.0%}"),
    "night_ratio": ("share of night-time operations", "{:.0%}"),
    "cp_per_30d": ("card payments per 30 days", "{:,.1f}"),
    "cp_amount_30d": ("card spending per 30 days", "{:,.2f}"),
    "cash_out_30d": ("cash withdrawn per 30 days", "{:,.2f}"),
    "cash_in_30d": ("cash deposited per 30 days", "{:,.2f}"),
    "incoming_30d": ("money received per 30 days", "{:,.2f}"),
}
LOG_FEATURES = {0, 1, 2, 3, 4, 5, 6, 7, 10, 11, 12, 13, 14}
TYPE_INDEX = {"bank_transfer": (0, 1), "phone_recharge": (2, 3), "prepaid_card": (4, 5), "card_payment": (10, 11)}
AMOUNT_INDEX = {"cash_withdrawal": 12, "cash_deposit": 13, "incoming_transfer": 14}
NIGHT = {0, 1, 2, 3, 4, 5}


@dataclass
class UserStats:
    counts: dict = field(default_factory=lambda: defaultdict(int))
    amounts: dict = field(default_factory=lambda: defaultdict(float))
    beneficiaries: set = field(default_factory=set)
    countries: dict = field(default_factory=lambda: defaultdict(int))
    transfers: int = 0
    foreign: int = 0
    night: int = 0
    total: int = 0
    first: Optional[datetime] = None

    def add(self, tx: Transaction, home: Optional[str]) -> None:
        self.counts[tx.tx_type] += 1
        self.amounts[tx.tx_type] += tx.amount
        self.beneficiaries.add((tx.tx_type, tx.beneficiary))
        self.total += 1
        if tx.ip_country:
            self.countries[tx.ip_country] += 1
        if tx.tx_type == "bank_transfer":
            self.transfers += 1
            if tx.iban_cc and home and tx.iban_cc != home:
                self.foreign += 1
        if tx.timestamp.hour in NIGHT:
            self.night += 1
        if self.first is None or tx.timestamp < self.first:
            self.first = tx.timestamp

    @property
    def home(self) -> Optional[str]:
        return max(self.countries, key=self.countries.get) if self.countries else None

    def vector(self, period_days: float) -> np.ndarray:
        factor = 30.0 / max(period_days, 1.0)
        v = np.zeros(len(FEATURES))
        for tx_type, (ci, ai) in TYPE_INDEX.items():
            v[ci] = self.counts.get(tx_type, 0) * factor
            v[ai] = self.amounts.get(tx_type, 0.0) * factor
        for tx_type, ai in AMOUNT_INDEX.items():
            v[ai] = self.amounts.get(tx_type, 0.0) * factor
        v[6] = sum(self.amounts.values()) / self.total if self.total else 0.0
        v[7] = len(self.beneficiaries)
        v[8] = self.foreign / self.transfers if self.transfers else 0.0
        v[9] = self.night / self.total if self.total else 0.0
        return v


def dbscan(X: np.ndarray, eps: float, min_samples: int) -> np.ndarray:
    """Plain DBSCAN (Euclidean) returning labels, -1 = noise. O(n^2) time, O(n) memory per row."""
    n = len(X)
    labels = np.full(n, -1, dtype=int)
    if n == 0:
        return labels
    neighbors: list[np.ndarray] = []
    for start in range(0, n, 64):
        block = X[start:start + 64]
        d2 = ((block[:, None, :] - X[None, :, :]) ** 2).sum(axis=2)
        for row in d2:
            neighbors.append(np.flatnonzero(row <= eps * eps))
    core = np.array([len(nb) >= min_samples for nb in neighbors])
    visited = np.zeros(n, dtype=bool)
    cluster = 0
    for i in range(n):
        if visited[i] or not core[i]:
            continue
        stack = [i]
        visited[i] = True
        while stack:
            p = stack.pop()
            labels[p] = cluster
            if not core[p]:
                continue
            for q in neighbors[p]:
                if labels[q] == -1:
                    labels[q] = cluster
                if not visited[q]:
                    visited[q] = True
                    stack.append(q)
        cluster += 1
    return labels


def iterative_dbscan(X: np.ndarray, eps: float, min_samples: int, growth: float, iterations: int) -> np.ndarray:
    labels = np.full(len(X), -1, dtype=int)
    next_label = 0
    current_eps = eps
    for _ in range(max(1, iterations)):
        idx = np.flatnonzero(labels == -1)
        if len(idx) < min_samples:
            break
        sub = dbscan(X[idx], current_eps, min_samples)
        for local_label in set(sub.tolist()) - {-1}:
            labels[idx[sub == local_label]] = next_label
            next_label += 1
        current_eps *= growth
    return labels


@dataclass
class GlobalScore:
    score: float
    cblof: float
    cluster: int
    large: bool
    reasons: list


class GlobalProfiler:
    def __init__(self, config: dict):
        self.cfg = config
        self.user_stats: dict[str, UserStats] = defaultdict(UserStats)
        self.start: Optional[datetime] = None
        self.end: Optional[datetime] = None
        self.fitted = False
        self.labels: dict[str, int] = {}
        self.user_scores: dict[str, float] = {}

    # ------------------------------------------------------------- data
    def _transform(self, V: np.ndarray) -> np.ndarray:
        V = np.array(V, dtype=float, copy=True)
        for j in LOG_FEATURES:
            if j < V.shape[-1]:
                V[..., j] = np.log1p(V[..., j])
        return V

    @property
    def dim(self) -> int:
        """Number of features this fitted model uses (older models: the first ten)."""
        return len(self.mu) if getattr(self, "mu", None) is not None else len(FEATURES)

    def _whiten(self, V: np.ndarray) -> np.ndarray:
        V = np.asarray(V)[..., :self.dim]
        Z = (self._transform(V) - self.mu) / self.sd
        return Z @ self.W

    def _period(self, at: Optional[datetime]) -> float:
        if self.start is None:
            return 30.0
        end = at or self.end or self.start
        return max((end - self.start).total_seconds() / 86400.0, 1.0)

    def add(self, tx: Transaction, home: Optional[str] = None) -> None:
        stats = self.user_stats[tx.user_id]
        stats.add(tx, home or stats.home or tx.ip_country)
        if self.start is None or tx.timestamp < self.start:
            self.start = tx.timestamp
        if self.end is None or tx.timestamp > self.end:
            self.end = tx.timestamp

    # -------------------------------------------------------------- fit
    def fit(self, transactions: Iterable[Transaction] | None = None, homes: dict | None = None) -> None:
        if transactions is not None:
            self.user_stats = defaultdict(UserStats)
            self.start = self.end = None
            for tx in sorted(transactions, key=lambda t: t.timestamp):
                self.add(tx, (homes or {}).get(tx.user_id))
        users = list(self.user_stats)
        if len(users) < 3:
            self.fitted = False
            return
        period = self._period(self.end)
        V = np.vstack([self.user_stats[u].vector(period) for u in users])
        T = self._transform(V)
        self.mu = T.mean(axis=0)
        self.sd = T.std(axis=0)
        self.sd[self.sd < 1e-9] = 1.0
        Z = (T - self.mu) / self.sd
        cov = np.cov(Z, rowvar=False) + np.eye(Z.shape[1]) * 1e-3
        evals, evecs = np.linalg.eigh(cov)
        evals = np.clip(evals, 1e-3, None)
        self.W = evecs @ np.diag(1.0 / np.sqrt(evals)) @ evecs.T   # ZCA whitening -> Mahalanobis
        X = Z @ self.W

        min_samples = int(self.cfg.get("min_samples", 5))
        labels = iterative_dbscan(X, float(self.cfg.get("eps", 1.2)), min_samples,
                                  float(self.cfg.get("eps_growth", 1.5)), int(self.cfg.get("max_iterations", 4)))
        self._build_clusters(X, V, labels)
        self.fitted = True
        cblof = np.array([self._cblof(x, lab)[0] for x, lab in zip(X, labels)])
        self.med = float(np.median(cblof))
        q = float(np.quantile(cblof, float(self.cfg.get("score_quantile", 0.95))))
        self.spread = max(q - self.med, 1e-6)
        self.labels = {u: int(l) for u, l in zip(users, labels)}
        self.user_scores = {u: self._normalise(c) for u, c in zip(users, cblof)}

    def _build_clusters(self, X: np.ndarray, V: np.ndarray, labels: np.ndarray) -> None:
        ids = sorted(set(labels.tolist()) - {-1}, key=lambda c: -(labels == c).sum())
        self.centroids = {c: X[labels == c].mean(axis=0) for c in ids}
        self.raw_centroids = {c: V[labels == c].mean(axis=0) for c in ids}
        self.sizes = {c: int((labels == c).sum()) for c in ids}
        alpha = float(self.cfg.get("cblof_alpha", 0.9))
        beta = float(self.cfg.get("cblof_beta", 5.0))
        n = len(labels)
        large = set()
        cumulative = 0
        for i, c in enumerate(ids):
            large.add(c)
            cumulative += self.sizes[c]
            nxt = self.sizes[ids[i + 1]] if i + 1 < len(ids) else None
            if cumulative >= alpha * n or (nxt and self.sizes[c] / nxt >= beta):
                break
        if not large:  # every customer is noise: use the global mean as a single cluster
            self.centroids = {0: X.mean(axis=0)}
            self.raw_centroids = {0: V.mean(axis=0)}
            self.sizes = {0: n}
            large = {0}
        self.large = large

    def _cblof(self, x: np.ndarray, label: int) -> tuple[float, int]:
        weighted = bool(self.cfg.get("weight_by_cluster_size", False))
        if label in self.large:
            size = self.sizes[label] if weighted else 1
            return size * float(np.linalg.norm(x - self.centroids[label])), label
        size = (self.sizes.get(label, 1) if label >= 0 else 1) if weighted else 1
        best, best_c = math.inf, -1
        for c in self.large:
            d = float(np.linalg.norm(x - self.centroids[c]))
            if d < best:
                best, best_c = d, c
        return size * best, best_c

    def _normalise(self, cblof: float) -> float:
        return 1.0 - math.exp(-max(0.0, cblof - self.med) / self.spread)

    def _assign(self, x: np.ndarray) -> int:
        """Nearest cluster centroid (used for customers unseen at fit time)."""
        return min(self.centroids, key=lambda c: float(np.linalg.norm(x - self.centroids[c])))

    # ------------------------------------------------------------- score
    def score(self, tx: Transaction, home: Optional[str] = None) -> GlobalScore:
        """Score the customer's spending pattern *including* this transaction."""
        if not self.fitted:
            return GlobalScore(0.0, 0.0, -1, False, [])
        stats = self.user_stats.get(tx.user_id)
        tmp = UserStats()
        if stats is not None:
            tmp.counts, tmp.amounts = defaultdict(int, stats.counts), defaultdict(float, stats.amounts)
            tmp.beneficiaries, tmp.countries = set(stats.beneficiaries), defaultdict(int, stats.countries)
            tmp.transfers, tmp.foreign, tmp.night, tmp.total, tmp.first = (
                stats.transfers, stats.foreign, stats.night, stats.total, stats.first)
        tmp.add(tx, home or tmp.home or tx.ip_country)
        v = tmp.vector(self._period(max(tx.timestamp, self.end) if self.end else tx.timestamp))[:self.dim]
        x = self._whiten(v[None, :])[0]
        label = self.labels.get(tx.user_id)
        if label is None:
            label = self._assign(x)
        cblof, ref = self._cblof(x, label)
        score = self._normalise(cblof)
        reasons = self._reasons(v, ref if ref >= 0 else label, score, label)
        return GlobalScore(score=score, cblof=cblof, cluster=int(label), large=label in self.large, reasons=reasons)

    def _reasons(self, v: np.ndarray, cluster: int, score: float, own_label: int) -> list:
        if score < float(self.cfg.get("min_reason_score", 0.5)) or cluster not in self.raw_centroids:
            return []
        ref_raw = self.raw_centroids[cluster]
        z_v = (self._transform(v[None, :])[0] - self.mu) / self.sd
        z_c = (self._transform(ref_raw[None, :])[0] - self.mu) / self.sd
        diff = z_v - z_c
        order = np.argsort(-np.abs(diff))[:3]
        where = "its peer group" if own_label in self.large else "the nearest peer group (customer is an outlier among peers)"
        reasons = []
        for j in order:
            if abs(diff[j]) < 1.0:
                continue
            label, fmt = LABELS[FEATURES[j]]
            direction = "higher" if diff[j] > 0 else "lower"
            msg = (f"Spending pattern differs from {where} #{cluster}: {label} {fmt.format(v[j])} is much "
                   f"{direction} than the group average {fmt.format(ref_raw[j])}")
            reasons.append(Reason(profile="global", feature=FEATURES[j], message=msg,
                                  contribution=round(float(abs(diff[j])), 3)))
        return reasons

    def summary(self, user_id: str) -> dict:
        stats = self.user_stats.get(user_id)
        if stats is None:
            return {}
        v = stats.vector(self._period(self.end))
        return {"cluster": self.labels.get(user_id), "score": self.user_scores.get(user_id),
                "vector": {FEATURES[i]: round(float(v[i]), 3) for i in range(len(FEATURES))},
                "cluster_size": self.sizes.get(self.labels.get(user_id, -2)) if self.fitted else None}
