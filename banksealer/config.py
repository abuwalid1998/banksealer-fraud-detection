"""Configuration handling.

The configuration is a YAML file (see ``config/banksealer.yaml``). Missing keys
fall back to :data:`DEFAULT_CONFIG`, and per-bank threshold overrides are merged
on top of the default thresholds by :meth:`Config.thresholds_for`.
"""
from __future__ import annotations

import copy
import os
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "app": {
        "database": "data/banksealer.db",
        "reports_dir": "reports",
        "refresh_interval_ms": 1000,
        "max_rows_in_memory": 50000,
        "auto_report": True,
        "auto_report_min_level": "MEDIUM",
    },
    "engine": {
        "profile_weights": {"local": 0.5, "temporal": 0.3, "global": 0.2},
        "min_training_transactions": 300,
        "retrain_global_every": 1000,
        "update_profiles_online": True,
        "exclude_suspicious_from_learning": True,
        "max_reasons": 6,
    },
    "local": {
        "min_history": 10,
        "shrinkage_k": 10,
        "smoothing_alpha": 0.5,
        "amount_bins": 24,
        "neighbor_weight": 0.5,
        "score_scale": 8.0,
        "min_reason_contribution": 1.0,
        "feature_weights": {
            "bank_transfer": {"amount": 1.0, "iban": 1.0, "iban_cc": 1.2, "cc_asn": 1.0, "ip": 0.4, "hour": 0.6},
            "phone_recharge": {"amount": 1.0, "phone_number": 1.0, "operator": 0.5, "cc_asn": 1.0, "ip": 0.4, "hour": 0.6},
            "prepaid_card": {"amount": 1.0, "card_id": 1.0, "cc_asn": 1.0, "ip": 0.4, "hour": 0.6},
            "card_payment": {"amount": 1.0, "merchant": 0.6, "mcc": 0.8, "merchant_cc": 1.2, "entry_mode": 0.6,
                             "hour": 0.5},
            "cash_withdrawal": {"amount": 1.0, "terminal": 0.6, "location": 1.0, "hour": 0.6},
            "cash_deposit": {"amount": 1.0, "terminal": 0.5, "location": 0.8, "hour": 0.4},
            "incoming_transfer": {"amount": 1.0, "counterparty": 0.8, "counterparty_cc": 1.2, "hour": 0.3},
        },
    },
    "temporal": {
        "window_hours": 24,
        "sigma_multiplier": 3.0,
        "min_windows": 5,
        "shrinkage_k": 10,
        "score_scale": 4.0,
        "feature_weights": {
            "tx_count": 1.0,
            "total_amount": 1.0,
            "max_amount": 0.6,
            "distinct_beneficiaries": 1.0,
            "distinct_cc_asn": 0.8,
            "foreign_count": 1.0,
        },
    },
    "global": {
        "eps": 1.2,
        "min_samples": 5,
        "eps_growth": 1.5,
        "max_iterations": 4,
        "cblof_alpha": 0.9,
        "cblof_beta": 5.0,
        "weight_by_cluster_size": False,
        "score_quantile": 0.95,
        "min_reason_score": 0.5,
    },
    "thresholds": {
        "suspicious": 0.50,
        "high": 0.65,
        "critical": 0.80,
        "local_alert": 0.92,
        "temporal_alert": 0.92,
        "global_alert": 0.97,
        "max_amount": {"bank_transfer": 15000, "phone_recharge": 150, "prepaid_card": 2500},
        "hard_rule_score": 0.85,
        "high_risk_countries": ["KY", "PA", "VG", "BZ", "SC", "RU", "NG"],
        "high_risk_country_boost": 0.10,
        "night_hours": [0, 1, 2, 3, 4],
        "night_boost": 0.03,
    },
    "banks": {},
    "channels": [],
}


def deep_merge(base: dict, override: dict | None) -> dict:
    """Return a new dict: ``base`` recursively updated with ``override``."""
    result = copy.deepcopy(base)
    if not override:
        return result
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class Config:
    """Wrapper around the configuration dictionary."""

    def __init__(self, data: dict | None = None, path: str | None = None):
        self.data: dict = deep_merge(DEFAULT_CONFIG, data or {})
        self.path = path

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls, path: str | None) -> "Config":
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            return cls(data, path)
        return cls({}, path)

    def save(self, path: str | None = None) -> str:
        path = path or self.path or "config/banksealer.yaml"
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# BankSealer configuration (saved from the desktop application)\n")
            yaml.safe_dump(self.data, fh, sort_keys=False, allow_unicode=True)
        self.path = path
        return path

    def copy(self) -> "Config":
        return Config(copy.deepcopy(self.data), self.path)

    # ------------------------------------------------------------- access
    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def section(self, key: str) -> dict:
        return self.data.get(key, {})

    @property
    def banks(self) -> dict:
        return self.data.setdefault("banks", {}) or {}

    def bank_name(self, bank_id: str) -> str:
        bank = self.banks.get(bank_id) or {}
        return bank.get("name") or bank_id

    def thresholds_for(self, bank_id: str | None) -> dict:
        """Default thresholds merged with the overrides of ``bank_id``."""
        base = self.data.get("thresholds", {})
        bank = (self.banks.get(bank_id) or {}) if bank_id else {}
        return deep_merge(base, bank.get("thresholds"))

    def set_bank_thresholds(self, bank_id: str | None, thresholds: dict, name: str | None = None) -> None:
        """Store thresholds as defaults (``bank_id`` None) or as a bank override."""
        if not bank_id:
            self.data["thresholds"] = copy.deepcopy(thresholds)
            return
        banks = self.data.setdefault("banks", {})
        entry = banks.setdefault(bank_id, {})
        if name:
            entry["name"] = name
        entry["thresholds"] = copy.deepcopy(thresholds)

    def remove_bank(self, bank_id: str) -> None:
        self.data.setdefault("banks", {}).pop(bank_id, None)

    @property
    def channels(self) -> list:
        return self.data.setdefault("channels", []) or []
