"""BankSealer local profile.

For every customer and every transaction type the local profile keeps one
histogram per feature, built from that customer's own past transactions, and
scores a new transaction with HBOS. Customers with too few transactions of a
type are *undertrained*: their personal histograms are shrunk towards the
histograms of their peer group (global-profile cluster), or of the whole
population when no cluster is known:

    hist(t) = lambda * hist_user(t) + (1 - lambda) * hist_peer(t),
    lambda  = n / (n + k)
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Iterable, Optional

import numpy as np

from ..models import Reason, Transaction
from .hbos import HOUR_EDGES, CategoricalHistogram, NumericHistogram, amount_edges, blended_density, hbos_term

FEATURES: dict[str, list[tuple[str, str]]] = {
    "bank_transfer": [("amount", "numeric"), ("iban", "categorical"), ("iban_cc", "categorical"),
                      ("cc_asn", "categorical"), ("ip", "categorical"), ("hour", "hour")],
    "phone_recharge": [("amount", "numeric"), ("phone_number", "categorical"), ("operator", "categorical"),
                       ("cc_asn", "categorical"), ("ip", "categorical"), ("hour", "hour")],
    "prepaid_card": [("amount", "numeric"), ("card_id", "categorical"), ("cc_asn", "categorical"),
                     ("ip", "categorical"), ("hour", "hour")],
    "card_payment": [("amount", "numeric"), ("merchant", "categorical"), ("mcc", "categorical"),
                     ("merchant_cc", "categorical"), ("entry_mode", "categorical"), ("hour", "hour")],
    "cash_withdrawal": [("amount", "numeric"), ("terminal", "categorical"), ("location", "categorical"),
                        ("hour", "hour")],
    "cash_deposit": [("amount", "numeric"), ("terminal", "categorical"), ("location", "categorical"),
                     ("hour", "hour")],
    "incoming_transfer": [("amount", "numeric"), ("counterparty", "categorical"), ("counterparty_cc", "categorical"),
                          ("hour", "hour")],
}
FEATURE_LABELS = {
    "amount": "Amount", "iban": "Beneficiary IBAN", "iban_cc": "Beneficiary country",
    "cc_asn": "Connection (country/ASN)", "ip": "IP address", "hour": "Time of day",
    "phone_number": "Recharged phone number", "operator": "Phone operator", "card_id": "Prepaid card",
    "merchant": "Merchant", "mcc": "Merchant category", "merchant_cc": "Merchant country", "entry_mode": "Card entry mode",
    "terminal": "Terminal (ATM / branch)", "location": "Terminal location", "counterparty": "Originator account",
    "counterparty_cc": "Originator country",
}
TYPE_NOUN = {"bank_transfer": "transfers", "phone_recharge": "phone recharges", "prepaid_card": "prepaid-card top-ups",
             "card_payment": "card payments", "cash_withdrawal": "cash withdrawals", "cash_deposit": "cash deposits",
             "incoming_transfer": "incoming transfers"}


def mask(value: Optional[str]) -> str:
    if not value:
        return "-"
    value = str(value)
    if len(value) <= 8:
        return value
    return f"{value[:6]}…{value[-4:]}"


@dataclass
class TypeProfile:
    """Histograms of one customer (or peer group) for one transaction type."""

    hists: dict = field(default_factory=dict)
    n: int = 0
    amounts: deque = field(default_factory=lambda: deque(maxlen=500))
    hours: deque = field(default_factory=lambda: deque(maxlen=500))

    @classmethod
    def empty(cls, tx_type: str, edges: np.ndarray, neighbor_weight: float) -> "TypeProfile":
        prof = cls()
        for name, kind in FEATURES[tx_type]:
            if kind == "numeric":
                prof.hists[name] = NumericHistogram(edges, neighbor_weight=neighbor_weight)
            elif kind == "hour":
                prof.hists[name] = NumericHistogram(HOUR_EDGES, circular=True, neighbor_weight=neighbor_weight)
            else:
                prof.hists[name] = CategoricalHistogram()
        return prof

    def add(self, tx: Transaction) -> None:
        for name, hist in self.hists.items():
            hist.add(tx.feature(name))
        self.n += 1
        self.amounts.append(tx.amount)
        self.hours.append(tx.hour)

    def merge(self, other: "TypeProfile") -> None:
        for name, hist in self.hists.items():
            hist.merge(other.hists[name])
        self.n += other.n


@dataclass
class LocalScore:
    raw: float
    score: float
    contributions: list  # (feature, contribution, density, Reason)
    undertrained: bool
    history: int


class LocalProfiler:
    def __init__(self, config: dict):
        self.cfg = config
        self.users: dict[str, dict[str, TypeProfile]] = defaultdict(dict)
        self.population: dict[str, TypeProfile] = {}
        self.clusters: dict[int, dict[str, TypeProfile]] = {}
        self.user_cluster: dict[str, int] = {}
        self.edges: dict[str, np.ndarray] = {}
        self.fitted = False

    # --------------------------------------------------------------- fit
    def fit(self, transactions: Iterable[Transaction]) -> None:
        transactions = list(transactions)
        nw = float(self.cfg.get("neighbor_weight", 0.5))
        bins = int(self.cfg.get("amount_bins", 24))
        for tx_type in FEATURES:
            amounts = np.array([t.amount for t in transactions if t.tx_type == tx_type])
            self.edges[tx_type] = amount_edges(amounts, bins)
            self.population[tx_type] = TypeProfile.empty(tx_type, self.edges[tx_type], nw)
        self.users = defaultdict(dict)
        for tx in transactions:
            self._add(tx)
        self.fitted = True

    def _ensure_type(self, tx_type: str) -> None:
        """Models trained before a transaction type existed learn it from scratch."""
        if tx_type not in self.edges:
            self.edges[tx_type] = amount_edges(np.array([]), int(self.cfg.get("amount_bins", 24)))
        if tx_type not in self.population:
            self.population[tx_type] = TypeProfile.empty(tx_type, self.edges[tx_type],
                                                         float(self.cfg.get("neighbor_weight", 0.5)))

    def _profile(self, user_id: str, tx_type: str) -> TypeProfile:
        self._ensure_type(tx_type)
        per_user = self.users[user_id]
        if tx_type not in per_user:
            per_user[tx_type] = TypeProfile.empty(tx_type, self.edges[tx_type], float(self.cfg.get("neighbor_weight", 0.5)))
        return per_user[tx_type]

    def _add(self, tx: Transaction) -> None:
        self._profile(tx.user_id, tx.tx_type).add(tx)
        self.population[tx.tx_type].add(tx)

    def update(self, tx: Transaction) -> None:
        if self.fitted:
            self._add(tx)

    def set_clusters(self, user_cluster: dict[str, int]) -> None:
        """Build peer-group histograms from the global-profile clusters."""
        self.user_cluster = dict(user_cluster)
        nw = float(self.cfg.get("neighbor_weight", 0.5))
        self.clusters = {}
        for user_id, cluster in user_cluster.items():
            if cluster < 0:
                continue
            group = self.clusters.setdefault(cluster, {})
            for tx_type, prof in self.users.get(user_id, {}).items():
                if tx_type not in group:
                    group[tx_type] = TypeProfile.empty(tx_type, self.edges[tx_type], nw)
                group[tx_type].merge(prof)

    # ------------------------------------------------------------- score
    def _reference(self, user_id: str, tx_type: str) -> Optional[TypeProfile]:
        cluster = self.user_cluster.get(user_id)
        if cluster is not None and cluster in self.clusters and tx_type in self.clusters[cluster]:
            ref = self.clusters[cluster][tx_type]
            if ref.n > 0:
                return ref
        return self.population.get(tx_type)

    def score(self, tx: Transaction, weights: dict) -> LocalScore:
        alpha = float(self.cfg.get("smoothing_alpha", 0.5))
        min_history = int(self.cfg.get("min_history", 10))
        k = float(self.cfg.get("shrinkage_k", 10))
        scale = float(self.cfg.get("score_scale", 8.0))

        prof = self.users.get(tx.user_id, {}).get(tx.tx_type)
        n = prof.n if prof else 0
        undertrained = n < min_history
        lam = n / (n + k) if undertrained else 1.0
        reference = self._reference(tx.user_id, tx.tx_type) if undertrained else None

        raw = 0.0
        contributions = []
        for name, kind in FEATURES[tx.tx_type]:
            w = float(weights.get(name, 1.0))
            if w <= 0:
                continue
            value = tx.feature(name)
            personal = prof.hists[name].density(value, alpha) if prof and prof.n else (0.0 if reference else 1.0)
            ref_density = reference.hists[name].density(value, alpha) if reference and reference.n else None
            density = blended_density(personal, ref_density, lam) if undertrained else personal
            term = hbos_term(density, w)
            raw += term
            contributions.append((name, term, density, self._reason(tx, name, kind, term, prof, undertrained)))
        contributions.sort(key=lambda c: c[1], reverse=True)
        score = 1.0 - math.exp(-raw / scale) if scale > 0 else 0.0
        return LocalScore(raw=raw, score=score, contributions=contributions, undertrained=undertrained, history=n)

    # ------------------------------------------------------- explanations
    def _reason(self, tx: Transaction, name: str, kind: str, term: float,
                prof: Optional[TypeProfile], undertrained: bool) -> Reason:
        label = FEATURE_LABELS.get(name, name)
        noun = TYPE_NOUN[tx.tx_type]
        n = prof.n if prof else 0
        suffix = " (few past operations: compared with similar customers)" if undertrained else ""
        if kind == "categorical":
            value = tx.feature(name)
            seen = prof.hists[name].count(value) if prof else 0
            sensitive = name in ("iban", "card_id", "phone_number", "ip", "counterparty")
            usual = ", ".join(mask(v) if sensitive else str(v) for v, _ in prof.hists[name].most_common(2)) \
                if prof and prof.n else "none"
            shown = mask(value) if sensitive else (value or "-")
            if seen == 0:
                msg = f"{label} {shown} never used before by this customer (0 of {n} past {noun}; usual: {usual}){suffix}"
            else:
                msg = f"{label} {shown} rarely used ({seen} of {n} past {noun}; usual: {usual}){suffix}"
        elif kind == "numeric":
            if prof and prof.amounts:
                arr = np.asarray(prof.amounts)
                lo, med, hi = np.percentile(arr, [5, 50, 95])
                msg = (f"{label} {tx.amount:,.2f} {tx.currency} is unusual for this customer "
                       f"(typical {lo:,.2f}–{hi:,.2f}, median {med:,.2f}){suffix}")
            else:
                msg = f"{label} {tx.amount:,.2f} {tx.currency}: first {noun[:-1]} of this customer{suffix}"
        else:  # hour
            if prof and prof.hours:
                hours = np.asarray(prof.hours)
                counts = np.bincount(hours.astype(int) % 24, minlength=24)
                top = sorted(int(h) for h in np.argsort(counts)[::-1][:3] if counts[h] > 0)
                usual = ", ".join(f"{h:02d}:00" for h in top)
                msg = f"Executed at {tx.timestamp:%H:%M}, unusual time for this customer (usually around {usual}){suffix}"
            else:
                msg = f"Executed at {tx.timestamp:%H:%M}{suffix}"
        return Reason(profile="local", feature=name, message=msg, contribution=round(term, 3))

    # ------------------------------------------------------------ summary
    def summary(self, user_id: str) -> dict:
        out = {}
        for tx_type, prof in self.users.get(user_id, {}).items():
            entry = {"count": prof.n}
            if prof.amounts:
                arr = np.asarray(prof.amounts)
                entry.update(amount_median=float(np.median(arr)), amount_p95=float(np.percentile(arr, 95)),
                             amount_max=float(arr.max()))
            for name, hist in prof.hists.items():
                if isinstance(hist, CategoricalHistogram):
                    entry[f"top_{name}"] = [(mask(v) if name in ("iban", "card_id", "phone_number") else v, c)
                                            for v, c in hist.most_common(3)]
            if prof.hours:
                entry["hour_hist"] = np.bincount(np.asarray(prof.hours).astype(int) % 24, minlength=24).tolist()
            out[tx_type] = entry
        return out
