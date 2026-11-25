"""Conversions between core dataclasses, ORM rows and API JSON."""
from __future__ import annotations

from banksealer.engine.banksealer import PROFILE_NAMES, ranked_reasons
from banksealer.models import Reason, ScoreResult, Transaction

from .models import ScoreRow, TransactionRow

TX_FIELDS = ("tx_id", "bank_id", "channel", "user_id", "tx_type", "amount", "currency", "iban", "iban_cc", "ip",
             "cc_asn", "session_id", "card_id", "phone_number", "operator", "label", "scenario")


def tx_values(tx: Transaction, training: bool = False) -> dict:
    data = {f: getattr(tx, f) for f in TX_FIELDS}
    data["ts"] = tx.timestamp
    data["is_training"] = training
    return data


def row_to_tx(row: TransactionRow) -> Transaction:
    data = {f: getattr(row, f) for f in TX_FIELDS}
    return Transaction(timestamp=row.ts, **data)


def score_values(r: ScoreResult) -> dict:
    return {
        "tx_id": r.tx_id, "local": r.local_score, "temporal": r.temporal_score, "global_": r.global_score,
        "combined": r.combined_score, "risk_level": r.risk_level, "suspicious": r.suspicious,
        "reasons": [x.to_dict() for x in r.reasons], "rule_reasons": [x.to_dict() for x in r.rule_reasons],
        "explanation": r.explanation, "raw": _jsonable(r.raw), "cluster": r.cluster, "undertrained": r.undertrained,
        "status": r.status,
    }


def row_to_result(tx: TransactionRow, s: ScoreRow) -> ScoreResult:
    return ScoreResult(
        tx_id=tx.tx_id, user_id=tx.user_id, bank_id=tx.bank_id, local_score=s.local, temporal_score=s.temporal,
        global_score=s.global_, combined_score=s.combined, risk_level=s.risk_level, suspicious=s.suspicious,
        reasons=[Reason.from_dict(x) for x in (s.reasons or [])],
        rule_reasons=[Reason.from_dict(x) for x in (s.rule_reasons or [])],
        explanation=s.explanation or "", raw=s.raw or {}, cluster=s.cluster, undertrained=s.undertrained,
        status=s.status or "open")


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "item"):          # numpy scalars
        return value.item()
    return value


def tx_json(row: TransactionRow) -> dict:
    data = {f: getattr(row, f) for f in TX_FIELDS}
    data["timestamp"] = row.ts.isoformat(sep=" ", timespec="seconds")
    data["beneficiary"] = row.iban if row.tx_type == "bank_transfer" else (
        row.phone_number if row.tx_type == "phone_recharge" else row.card_id)
    return data


def score_json(s: ScoreRow | None, full: bool = False) -> dict | None:
    if s is None:
        return None
    data = {"local": s.local, "temporal": s.temporal, "global": s.global_, "combined": s.combined,
            "risk_level": s.risk_level, "suspicious": s.suspicious, "status": s.status, "cluster": s.cluster,
            "undertrained": s.undertrained}
    if full:
        data.update(explanation=s.explanation, raw=s.raw, analyst_note=s.analyst_note, reviewed_by=s.reviewed_by,
                    reviewed_at=s.reviewed_at.isoformat(timespec="seconds") if s.reviewed_at else None)
    return data


def reasons_json(tx: TransactionRow, s: ScoreRow) -> list[dict]:
    out = []
    for r in ranked_reasons(row_to_result(tx, s)):
        d = r.to_dict()
        d["source"] = PROFILE_NAMES.get(r.profile, r.profile)
        out.append(d)
    return out
