"""Core data structures shared by channels, the engine, storage and the UI."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional

TX_TYPES = ("bank_transfer", "phone_recharge", "prepaid_card", "card_payment", "cash_withdrawal", "cash_deposit",
            "incoming_transfer", "cheque_deposit", "cheque_payment")
TX_TYPE_LABELS = {
    "bank_transfer": "Bank transfer",
    "phone_recharge": "Phone recharge",
    "prepaid_card": "Prepaid card",
    "card_payment": "Card payment (POS / e-commerce)",
    "cash_withdrawal": "Cash withdrawal (ATM / branch)",
    "cash_deposit": "Cash deposit",
    "incoming_transfer": "Incoming transfer",
    "cheque_deposit": "Cheque deposit (drawn on another account)",
    "cheque_payment": "Cheque payment (customer's own cheque presented)",
}
OUTFLOW_TYPES = ("bank_transfer", "phone_recharge", "prepaid_card", "card_payment", "cash_withdrawal", "cheque_payment")
CHEQUE_TYPES = ("cheque_deposit", "cheque_payment")
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")


@dataclass
class Transaction:
    """A prepared (normalised and validated) transaction."""

    tx_id: str
    user_id: str
    timestamp: datetime
    tx_type: str
    amount: float
    bank_id: str = "DEFAULT"
    channel: str = ""
    currency: str = "EUR"
    iban: Optional[str] = None          # beneficiary IBAN (bank transfers)
    iban_cc: Optional[str] = None       # beneficiary IBAN country code
    ip: Optional[str] = None            # IP address of the session
    cc_asn: Optional[str] = None        # "<country>-AS<number>" of the IP
    session_id: Optional[str] = None
    card_id: Optional[str] = None       # prepaid card number (masked)
    phone_number: Optional[str] = None  # recharged phone number
    operator: Optional[str] = None      # phone operator
    label: Optional[int] = None         # ground truth, if the source provides it
    scenario: Optional[str] = None      # fraud scenario name (mock data only)
    merchant: Optional[str] = None      # card payments: merchant id / name
    mcc: Optional[str] = None           # card payments: merchant category code
    merchant_cc: Optional[str] = None   # card payments: merchant country
    entry_mode: Optional[str] = None    # card payments: CHIP / CONTACTLESS / ECOM / MAGSTRIPE
    terminal: Optional[str] = None      # cash: ATM or branch terminal id
    location: Optional[str] = None      # cash: city / location of the terminal
    counterparty: Optional[str] = None  # incoming transfers: originator IBAN
    counterparty_cc: Optional[str] = None
    cheque_number: Optional[str] = None   # cheques: serial number printed on the cheque
    drawer_bank: Optional[str] = None     # cheque deposits: bank code of the drawer's bank
    cheque_date: Optional[str] = None     # date written on the cheque (YYYY-MM-DD; post-dated / stale checks)

    @property
    def hour(self) -> float:
        return self.timestamp.hour + self.timestamp.minute / 60.0

    @property
    def beneficiary(self) -> Optional[str]:
        """The counterparty that identifies the operation (who receives / where it happens)."""
        if self.tx_type == "bank_transfer":
            return self.iban
        if self.tx_type == "phone_recharge":
            return self.phone_number
        if self.tx_type == "card_payment":
            return self.merchant
        if self.tx_type in ("cash_withdrawal", "cash_deposit"):
            return self.terminal
        if self.tx_type in ("incoming_transfer", "cheque_deposit", "cheque_payment"):
            return self.counterparty
        return self.card_id

    @property
    def counterparty_country(self) -> Optional[str]:
        if self.tx_type == "card_payment":
            return self.merchant_cc
        if self.tx_type in ("incoming_transfer", "cheque_deposit", "cheque_payment"):
            return self.counterparty_cc
        return self.iban_cc

    @property
    def is_outflow(self) -> bool:
        return self.tx_type in OUTFLOW_TYPES

    @property
    def ip_country(self) -> Optional[str]:
        if self.cc_asn and "-" in self.cc_asn:
            return self.cc_asn.split("-", 1)[0]
        return None

    def feature(self, name: str) -> Any:
        if name == "hour":
            return self.hour
        return getattr(self, name)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat(sep=" ", timespec="seconds")
        return data


@dataclass
class Reason:
    """One explanation item: why a profile considered the transaction unusual."""

    profile: str        # local | temporal | global | rule
    feature: str
    message: str
    contribution: float = 0.0   # impact on the combined suspicion score (0-1 points)
    share: float = 0.0          # share of the profile's raw anomaly score explained by this item

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Reason":
        return cls(**{k: data.get(k, 0.0) for k in ("profile", "feature", "message", "contribution", "share")})


@dataclass
class ScoreResult:
    """Output of the BankSealer engine for one transaction."""

    tx_id: str
    user_id: str
    bank_id: str
    local_score: float
    temporal_score: float
    global_score: float
    combined_score: float = 0.0
    risk_level: str = "LOW"
    suspicious: bool = False
    reasons: list[Reason] = field(default_factory=list)       # profile reasons (always kept)
    rule_reasons: list[Reason] = field(default_factory=list)  # threshold/rule reasons
    explanation: str = ""
    raw: dict = field(default_factory=dict)
    cluster: Optional[int] = None
    undertrained: bool = False
    status: str = "open"   # analyst feedback: open | fraud | legit

    @property
    def all_reasons(self) -> list[Reason]:
        return list(self.rule_reasons) + list(self.reasons)

    def to_dict(self) -> dict:
        data = asdict(self)
        return data
