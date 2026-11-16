"""Histogram primitives used by the HBOS (Histogram-Based Outlier Score) local profile.

HBOS models every feature independently with a histogram whose highest bin is
normalised to 1. The outlier score of a sample is

    HBOS(t) = sum_i  w_i * log( 1 / hist_i(t_i) )

so values falling in a frequently used bin contribute ~0 and values never (or
rarely) observed contribute a lot. Values never observed receive a smoothed
density ``alpha / (max_count + alpha)``: the longer the history of a customer,
the more surprising a never-seen value becomes.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Hashable, Optional

import numpy as np


class CategoricalHistogram:
    """Frequency histogram for categorical values (IBAN, ASN, IP, ...)."""

    __slots__ = ("counts", "n")

    def __init__(self) -> None:
        self.counts: Counter = Counter()
        self.n = 0

    def add(self, value: Hashable, weight: int = 1) -> None:
        if value is None:
            value = "<none>"
        self.counts[value] += weight
        self.n += weight

    def merge(self, other: "CategoricalHistogram") -> None:
        self.counts.update(other.counts)
        self.n += other.n

    def count(self, value: Hashable) -> int:
        return self.counts.get("<none>" if value is None else value, 0)

    def density(self, value: Hashable, alpha: float) -> float:
        if self.n == 0:
            return 1.0  # nothing known: neutral
        peak = max(self.counts.values())
        return (self.count(value) + alpha) / (peak + alpha)

    def most_common(self, k: int = 3) -> list[tuple[Hashable, int]]:
        return [(v, c) for v, c in self.counts.most_common(k) if v != "<none>"]


class NumericHistogram:
    """Static-bin histogram for numeric features (amount, hour of day)."""

    __slots__ = ("edges", "counts", "n", "circular", "neighbor_weight")

    def __init__(self, edges: np.ndarray, circular: bool = False, neighbor_weight: float = 0.5):
        self.edges = edges
        self.counts = np.zeros(len(edges) - 1, dtype=float)
        self.n = 0
        self.circular = circular
        self.neighbor_weight = neighbor_weight

    def bin_of(self, value: float) -> int:
        idx = int(np.searchsorted(self.edges, value, side="right")) - 1
        return min(max(idx, 0), len(self.counts) - 1)

    def add(self, value: float, weight: float = 1.0) -> None:
        self.counts[self.bin_of(value)] += weight
        self.n += weight

    def merge(self, other: "NumericHistogram") -> None:
        self.counts += other.counts
        self.n += other.n

    def _smoothed(self) -> np.ndarray:
        """Counts smoothed towards adjacent bins (avoids 9:59 vs 10:01 artefacts)."""
        c = self.counts
        if self.neighbor_weight <= 0:
            return c
        if self.circular:
            left, right = np.roll(c, 1), np.roll(c, -1)
        else:
            left = np.concatenate(([0.0], c[:-1]))
            right = np.concatenate((c[1:], [0.0]))
        return c + self.neighbor_weight * (left + right)

    def density(self, value: float, alpha: float) -> float:
        if self.n == 0:
            return 1.0
        smoothed = self._smoothed()
        peak = float(smoothed.max())
        return (float(smoothed[self.bin_of(value)]) + alpha) / (peak + alpha)

    def bin_range(self, idx: int) -> tuple[float, float]:
        return float(self.edges[idx]), float(self.edges[idx + 1])


def blended_density(personal: float, reference: Optional[float], lam: float) -> float:
    """Shrink an undertrained personal density towards a reference (peer) density."""
    if reference is None:
        return personal
    return lam * personal + (1.0 - lam) * reference


def hbos_term(density: float, weight: float, floor: float = 1e-6) -> float:
    return weight * math.log(1.0 / max(density, floor))


def amount_edges(amounts: np.ndarray, bins: int) -> np.ndarray:
    """Log-spaced bin edges covering the observed amounts with head-room."""
    amounts = np.asarray(amounts, dtype=float)
    amounts = amounts[amounts > 0]
    if amounts.size == 0:
        low, high = 1.0, 10000.0
    else:
        low, high = float(amounts.min()), float(amounts.max())
    low = max(low / 4.0, 0.01)
    high = max(high * 4.0, low * 10.0)
    edges = np.geomspace(low, high, bins + 1)
    edges[0], edges[-1] = 0.0, np.inf
    return edges


HOUR_EDGES = np.arange(0, 25, 1.0)
