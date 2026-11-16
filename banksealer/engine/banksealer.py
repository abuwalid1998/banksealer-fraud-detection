"""BankSealer engine: trains the three profiles, scores transactions, explains decisions.

Pipeline for one prepared transaction ``t`` of customer ``u``:

1. local score    s_L = 1 - exp(-HBOS_u(t) / scale_L)
2. temporal score s_T = 1 - exp(-sum_f w_f * excess_f / scale_T)
3. global score   s_G = 1 - exp(-max(0, CBLOF(u) - median) / spread)
4. suspicion score S  = w_L*s_L + w_T*s_T + w_G*s_G  (+ configurable rule boosts)
5. decision with the thresholds of the transaction's bank/company: risk level,
   suspicious flag, ranked reasons and a plain-language explanation.

Profile scores are kept on the result, so new thresholds can be applied to
already-scored transactions with :meth:`BankSealerEngine.decide` (no rescoring).
"""
from __future__ import annotations

from typing import Iterable, Optional

import numpy as np

from ..config import Config
from ..models import Reason, ScoreResult, Transaction
from .global_profile import GlobalProfiler
from .local_profile import LocalProfiler
from .temporal_profile import TemporalProfiler

PROFILE_NAMES = {"local": "Local profile", "temporal": "Temporal profile", "global": "Global profile", "rule": "Bank rule"}


def risk_level(score: float, th: dict) -> str:
    if score >= float(th.get("critical", 0.8)):
        return "CRITICAL"
    if score >= float(th.get("high", 0.65)):
        return "HIGH"
    if score >= float(th.get("suspicious", 0.5)):
        return "MEDIUM"
    return "LOW"


class BankSealerEngine:
    def __init__(self, config: Config):
        self.config = config
        self._build()

    def _build(self) -> None:
        self.local = LocalProfiler(self.config.section("local"))
        self.temporal = TemporalProfiler(self.config.section("temporal"))
        self.global_ = GlobalProfiler(self.config.section("global"))
        self.training_buffer: list[Transaction] = []
        self.trained = False
        self.scored_since_retrain = 0
        self.training_size = 0

    def apply_config(self, config: Config) -> None:
        """Use a new configuration. Model hyper-parameters apply to the next fit/score."""
        self.config = config
        self.local.cfg = config.section("local")
        self.temporal.cfg = config.section("temporal")
        self.global_.cfg = config.section("global")

    # ------------------------------------------------------------ training
    @property
    def min_training(self) -> int:
        return int(self.config.section("engine").get("min_training_transactions", 300))

    def add_training(self, transactions: Iterable[Transaction]) -> None:
        self.training_buffer.extend(transactions)

    def fit(self, transactions: Optional[Iterable[Transaction]] = None) -> int:
        """Fit all profiles on ``transactions`` (or on the training buffer)."""
        txs = sorted(list(transactions) if transactions is not None else self.training_buffer,
                     key=lambda t: t.timestamp)
        if not txs:
            return 0
        self.local = LocalProfiler(self.config.section("local"))
        self.temporal = TemporalProfiler(self.config.section("temporal"))
        self.global_ = GlobalProfiler(self.config.section("global"))
        self.local.fit(txs)
        self.temporal.fit(txs)
        self.global_.fit(txs, homes=self.temporal.home)
        self.local.set_clusters(self.global_.labels)
        self.training_buffer = []
        self.trained = True
        self.training_size = len(txs)
        self.scored_since_retrain = 0
        return len(txs)

    def retrain_global(self) -> None:
        self.global_.fit(homes=self.temporal.home)
        self.local.set_clusters(self.global_.labels)
        self.scored_since_retrain = 0

    def ingest(self, tx: Transaction) -> Optional[ScoreResult]:
        """Score ``tx`` or, before the first fit, buffer it for training."""
        if not self.trained:
            self.training_buffer.append(tx)
            if len(self.training_buffer) >= self.min_training:
                self.fit()
            return None
        return self.score(tx)

    # ------------------------------------------------------------- scoring
    def score(self, tx: Transaction, learn: bool = True) -> ScoreResult:
        if not self.trained:
            raise RuntimeError("engine is not trained yet")
        engine_cfg = self.config.section("engine")
        local_w = self.config.section("local").get("feature_weights", {}).get(tx.tx_type, {})
        temporal_w = self.config.section("temporal").get("feature_weights", {})

        home = self.temporal.home.get(tx.user_id)
        ls = self.local.score(tx, local_w)
        ts = self.temporal.score(tx, temporal_w)
        gs = self.global_.score(tx, home)

        min_contrib = float(self.config.section("local").get("min_reason_contribution", 1.0))
        reasons: list[Reason] = []
        for _, term, _, reason in ls.contributions:
            if term >= min_contrib:
                reason.share = term / ls.raw if ls.raw > 0 else 0.0
                reasons.append(reason)
        for reason in ts.reasons:
            reason.share = reason.contribution / ts.raw if ts.raw > 0 else 0.0
            reasons.append(reason)
        total_g = sum(r.contribution for r in gs.reasons)
        for reason in gs.reasons:
            reason.share = reason.contribution / total_g if total_g > 0 else 0.0
            reasons.append(reason)

        result = ScoreResult(
            tx_id=tx.tx_id, user_id=tx.user_id, bank_id=tx.bank_id,
            local_score=round(ls.score, 4), temporal_score=round(ts.score, 4), global_score=round(gs.score, 4),
            reasons=reasons, cluster=gs.cluster, undertrained=ls.undertrained or ts.undertrained,
            raw={"local_hbos": round(ls.raw, 4), "local_history": ls.history,
                 "temporal_excess": round(ts.raw, 4), "temporal_window": ts.values,
                 "global_cblof": round(gs.cblof, 4), "global_large_cluster": gs.large},
        )
        self.decide(result, tx)

        # ---- online learning
        self.temporal.observe(tx)
        learn_ok = learn and engine_cfg.get("update_profiles_online", True)
        if learn_ok and result.suspicious and engine_cfg.get("exclude_suspicious_from_learning", True):
            learn_ok = False
        if learn_ok:
            self.local.update(tx)
            self.temporal.learn(tx, np.array([ts.values[f] for f in ts.values], dtype=float))
            self.global_.add(tx, home)
        self.scored_since_retrain += 1
        every = int(engine_cfg.get("retrain_global_every", 1000) or 0)
        if every and self.scored_since_retrain >= every:
            self.retrain_global()
        return result

    def learn(self, tx: Transaction) -> None:
        """Add a transaction confirmed legitimate by an analyst to the profiles."""
        if not self.trained:
            return
        self.local.update(tx)
        self.temporal.learn(tx)
        self.global_.add(tx, self.temporal.home.get(tx.user_id))

    def save_model(self, path: str) -> None:
        import pickle
        with open(path, "wb") as fh:
            pickle.dump({"local": self.local, "temporal": self.temporal, "global": self.global_,
                         "trained": self.trained, "training_size": self.training_size}, fh)

    def load_model(self, path: str) -> None:
        """Load a model saved by :meth:`save_model` (only load files you created: pickle)."""
        import pickle
        with open(path, "rb") as fh:
            state = pickle.load(fh)
        self.local, self.temporal, self.global_ = state["local"], state["temporal"], state["global"]
        self.trained, self.training_size = state["trained"], state["training_size"]
        self.apply_config(self.config)

    # ------------------------------------------------------------ decision
    def decide(self, result: ScoreResult, tx: Transaction) -> ScoreResult:
        """Apply the bank/company thresholds to the profile scores of ``result``."""
        th = self.config.thresholds_for(tx.bank_id)
        weights = dict(self.config.section("engine").get("profile_weights", {}))
        total_w = sum(float(weights.get(p, 0)) for p in ("local", "temporal", "global")) or 1.0
        w = {p: float(weights.get(p, 0)) / total_w for p in ("local", "temporal", "global")}
        scores = {"local": result.local_score, "temporal": result.temporal_score, "global": result.global_score}
        combined = sum(w[p] * scores[p] for p in w)

        for reason in result.reasons:
            reason.contribution = round(w[reason.profile] * scores[reason.profile] * reason.share, 3)

        rules: list[Reason] = []
        forced = False
        high_risk = {c.upper() for c in th.get("high_risk_countries", []) or []}
        hit = sorted({c for c in (tx.iban_cc, tx.ip_country) if c and c.upper() in high_risk})
        boost = float(th.get("high_risk_country_boost", 0.0))
        if hit and boost > 0:
            combined += boost
            rules.append(Reason("rule", "high_risk_country",
                                f"Involves high-risk country {', '.join(hit)} (bank list; +{boost:.2f} to the score)", boost))
        night_boost = float(th.get("night_boost", 0.0))
        if night_boost > 0 and tx.timestamp.hour in set(th.get("night_hours", []) or []):
            combined += night_boost
            rules.append(Reason("rule", "night", f"Executed during night hours ({tx.timestamp:%H:%M}; +{night_boost:.2f})",
                                night_boost))
        limit = (th.get("max_amount") or {}).get(tx.tx_type)
        if limit and tx.amount > float(limit):
            floor = float(th.get("hard_rule_score", 0.85))
            rules.append(Reason("rule", "max_amount",
                                f"Amount {tx.amount:,.2f} exceeds the bank limit {float(limit):,.2f} for this operation type",
                                round(max(0.0, floor - combined), 3)))
            combined = max(combined, floor)
            forced = True
        combined = min(1.0, max(0.0, combined))

        for profile, key in (("local", "local_alert"), ("temporal", "temporal_alert"), ("global", "global_alert")):
            limit = th.get(key)
            if limit is not None and scores[profile] >= float(limit):
                forced = True
                rules.append(Reason("rule", f"{profile}_alert",
                                    f"{PROFILE_NAMES[profile]} score {scores[profile]:.2f} is above the bank's "
                                    f"alert level {float(limit):.2f}", 0.0))

        level = risk_level(combined, th)
        suspicious = combined >= float(th.get("suspicious", 0.5)) or forced
        if suspicious and level == "LOW":
            level = "MEDIUM"

        result.combined_score = round(combined, 4)
        result.risk_level = level
        result.suspicious = suspicious
        result.rule_reasons = rules
        result.explanation = self.explain(result, tx, th) if suspicious else ""
        return result

    def ranked_reasons(self, result: ScoreResult) -> list[Reason]:
        rules = [r for r in result.rule_reasons if r.feature not in ("local_alert", "temporal_alert", "global_alert")]
        profile = sorted(result.reasons, key=lambda r: r.contribution, reverse=True)
        alerts = [r for r in result.rule_reasons if r not in rules]
        return sorted(rules, key=lambda r: r.contribution, reverse=True) + profile + alerts

    def explain(self, result: ScoreResult, tx: Transaction, th: dict | None = None) -> str:
        th = th or self.config.thresholds_for(tx.bank_id)
        max_reasons = int(self.config.section("engine").get("max_reasons", 6))
        ranked = self.ranked_reasons(result)[:max_reasons]
        head = (f"{result.risk_level} risk: suspicion score {result.combined_score:.2f} "
                f"(threshold {float(th.get('suspicious', 0.5)):.2f} for {self.config.bank_name(tx.bank_id)}). "
                f"Profile scores - local {result.local_score:.2f}, temporal {result.temporal_score:.2f}, "
                f"global {result.global_score:.2f}.")
        if not ranked:
            return head
        lines = [f"{i}. [{PROFILE_NAMES[r.profile]}] {r.message}" for i, r in enumerate(ranked, 1)]
        return head + " Why:\n" + "\n".join(lines)

    # ------------------------------------------------------------- summary
    def customer_summary(self, user_id: str) -> dict:
        return {
            "user_id": user_id,
            "local": self.local.summary(user_id) if self.local.fitted else {},
            "temporal": self.temporal.summary(user_id) if self.temporal.fitted else {},
            "global": self.global_.summary(user_id) if self.global_.fitted else {},
        }

    @property
    def state(self) -> str:
        if self.trained:
            return f"trained on {self.training_size:,} tx, {len(self.global_.labels)} customers, " \
                   f"{len(getattr(self.global_, 'centroids', {}))} peer groups"
        return f"collecting training data {len(self.training_buffer):,}/{self.min_training:,}"


__all__ = ["BankSealerEngine", "risk_level", "PROFILE_NAMES"]
