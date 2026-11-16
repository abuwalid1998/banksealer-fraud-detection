"""Transaction preparation: map, clean, validate and enrich raw channel records.

Every channel delivers raw dictionaries in its own schema. The
:class:`TransactionPreparer` turns them into :class:`~banksealer.models.Transaction`
objects so the engine always sees the same, clean representation:

1. **Field mapping** - explicit per-channel mapping first, then known aliases
   (``customer_id`` -> ``user_id``, ``beneficiary_iban`` -> ``iban``, ...).
2. **Type coercion** - timestamps (ISO, epoch s/ms, common formats), amounts
   (``"1.234,50 EUR"``), transaction type synonyms.
3. **Validation** - required fields, positive amounts, known types.
4. **Enrichment** - IBAN normalisation and country, ``cc_asn`` from separate
   country/ASN fields, beneficiary masking, generated id when missing.
5. **De-duplication** by transaction id.
"""
from __future__ import annotations

import hashlib
import re
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .models import TX_TYPES, Transaction

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "tx_id": ("tx_id", "id", "transaction_id", "txid", "trx_id", "reference"),
    "user_id": ("user_id", "customer_id", "client_id", "customer", "user", "account_id", "account"),
    "timestamp": ("timestamp", "ts", "datetime", "date_time", "time", "date", "created_at"),
    "tx_type": ("tx_type", "type", "transaction_type", "operation", "kind"),
    "amount": ("amount", "value", "amt", "sum"),
    "bank_id": ("bank_id", "bank", "institution", "company", "company_id"),
    "currency": ("currency", "ccy"),
    "iban": ("iban", "beneficiary_iban", "dest_iban", "to_iban", "payee_iban"),
    "iban_cc": ("iban_cc", "beneficiary_country", "iban_country"),
    "ip": ("ip", "ip_address", "client_ip", "remote_ip"),
    "cc_asn": ("cc_asn", "country_asn"),
    "session_id": ("session_id", "session", "sid"),
    "card_id": ("card_id", "card", "card_number", "prepaid_card"),
    "phone_number": ("phone_number", "phone", "msisdn", "mobile"),
    "operator": ("operator", "carrier", "phone_operator"),
    "label": ("label", "is_fraud", "fraud"),
    "scenario": ("scenario", "fraud_scenario"),
}
TYPE_ALIASES = {
    "bank_transfer": "bank_transfer", "transfer": "bank_transfer", "wire": "bank_transfer",
    "wire_transfer": "bank_transfer", "sepa": "bank_transfer", "bonifico": "bank_transfer",
    "payment": "bank_transfer", "bt": "bank_transfer",
    "phone_recharge": "phone_recharge", "recharge": "phone_recharge", "topup": "phone_recharge",
    "top_up": "phone_recharge", "mobile_topup": "phone_recharge", "ricarica": "phone_recharge",
    "pr": "phone_recharge",
    "prepaid_card": "prepaid_card", "prepaid": "prepaid_card", "card_topup": "prepaid_card",
    "card_recharge": "prepaid_card", "prepaid_topup": "prepaid_card", "pc": "prepaid_card",
}
TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M", "%Y/%m/%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y%m%d%H%M%S", "%Y-%m-%d",
)
REQUIRED = ("user_id", "timestamp", "amount", "tx_type")


class PreparationError(ValueError):
    pass


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d{10}(\d{3})?(\.\d+)?", value.strip())):
        number = float(value)
        if number > 1e11:  # milliseconds
            number /= 1000.0
        return datetime.fromtimestamp(number, tz=timezone.utc).replace(tzinfo=None)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise PreparationError(f"unparseable timestamp {value!r}")


def parse_amount(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^\d,.\-]", "", str(value))
    if not text:
        raise PreparationError(f"unparseable amount {value!r}")
    if "," in text and "." in text:
        # the last separator is the decimal one: 1.234,50 or 1,234.50
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        text = f"{head.replace(',', '')}.{tail}" if len(tail) != 3 else text.replace(",", "")
    try:
        return float(text)
    except ValueError as exc:
        raise PreparationError(f"unparseable amount {value!r}") from exc


def normalise_type(value: Any) -> str:
    key = re.sub(r"[\s\-]+", "_", str(value).strip().lower())
    if key in TYPE_ALIASES:
        return TYPE_ALIASES[key]
    raise PreparationError(f"unknown transaction type {value!r} (expected one of {', '.join(TX_TYPES)})")


def normalise_iban(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    iban = re.sub(r"\s+", "", str(value)).upper()
    return iban or None


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text.lower() not in ("nan", "none", "null") else None


class TransactionPreparer:
    """Converts raw channel records into validated :class:`Transaction` objects."""

    def __init__(self, field_mapping: dict | None = None, default_bank: str = "DEFAULT",
                 dedup_capacity: int = 200_000):
        self.field_mapping = dict(field_mapping or {})
        self.default_bank = default_bank
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._dedup_capacity = dedup_capacity
        self.stats = {"received": 0, "accepted": 0, "rejected": 0, "duplicates": 0}
        self.errors: deque[str] = deque(maxlen=200)

    # ------------------------------------------------------------ mapping
    def _map_fields(self, raw: dict) -> dict:
        lowered = {str(k).strip().lower(): v for k, v in raw.items()}
        mapped: dict[str, Any] = {}
        for source, target in self.field_mapping.items():
            if source.lower() in lowered:
                mapped[target] = lowered[source.lower()]
        for target, aliases in FIELD_ALIASES.items():
            if target in mapped:
                continue
            for alias in aliases:
                if alias in lowered and lowered[alias] not in (None, ""):
                    mapped[target] = lowered[alias]
                    break
        # country + asn delivered separately
        if "cc_asn" not in mapped:
            country = lowered.get("ip_country") or lowered.get("country")
            asn = lowered.get("asn")
            if country or asn:
                asn_text = str(asn or "0").upper()
                asn_text = asn_text if asn_text.startswith("AS") else f"AS{asn_text}"
                mapped["cc_asn"] = f"{str(country or 'XX').upper()}-{asn_text}"
        return mapped

    # ------------------------------------------------------------ prepare
    def prepare(self, raw: dict, channel: str = "") -> Optional[Transaction]:
        """Prepare one record; returns ``None`` when rejected or duplicated."""
        self.stats["received"] += 1
        try:
            tx = self._build(raw, channel)
        except PreparationError as exc:
            self.stats["rejected"] += 1
            self.errors.append(f"[{channel or 'input'}] {exc}")
            return None
        if tx.tx_id in self._seen:
            self.stats["duplicates"] += 1
            return None
        self._seen[tx.tx_id] = None
        if len(self._seen) > self._dedup_capacity:
            self._seen.popitem(last=False)
        self.stats["accepted"] += 1
        return tx

    def prepare_many(self, records: Iterable[dict], channel: str = "") -> list[Transaction]:
        out = [tx for tx in (self.prepare(r, channel) for r in records) if tx is not None]
        out.sort(key=lambda t: t.timestamp)
        return out

    def _build(self, raw: dict, channel: str) -> Transaction:
        if not isinstance(raw, dict):
            raise PreparationError(f"record is not an object: {raw!r}")
        data = self._map_fields(raw)
        missing = [f for f in REQUIRED if _clean(data.get(f)) is None]
        if missing:
            raise PreparationError(f"missing required field(s): {', '.join(missing)}")

        timestamp = parse_timestamp(data["timestamp"])
        amount = parse_amount(data["amount"])
        if amount <= 0:
            raise PreparationError(f"amount must be positive, got {amount}")
        tx_type = normalise_type(data["tx_type"])

        iban = normalise_iban(data.get("iban"))
        iban_cc = _clean(data.get("iban_cc"))
        if iban and not iban_cc and re.match(r"^[A-Z]{2}\d{2}", iban):
            iban_cc = iban[:2]
        if tx_type == "bank_transfer" and not iban:
            raise PreparationError("bank transfer without beneficiary IBAN")

        cc_asn = _clean(data.get("cc_asn"))
        cc_asn = cc_asn.upper() if cc_asn else ("UNKNOWN" if _clean(data.get("ip")) is None else "XX-AS0")

        label = data.get("label")
        if label not in (None, ""):
            label = 1 if str(label).strip().lower() in ("1", "true", "yes", "fraud", "1.0") else 0
        else:
            label = None

        user_id = str(data["user_id"]).strip()
        tx_id = _clean(data.get("tx_id"))
        if not tx_id:
            digest = hashlib.sha1(f"{user_id}|{timestamp.isoformat()}|{amount}|{tx_type}|{iban}".encode()).hexdigest()
            tx_id = f"TX-{digest[:16]}"

        return Transaction(
            tx_id=tx_id,
            user_id=user_id,
            timestamp=timestamp,
            tx_type=tx_type,
            amount=round(amount, 2),
            bank_id=_clean(data.get("bank_id")) or self.default_bank,
            channel=channel,
            currency=(_clean(data.get("currency")) or "EUR").upper(),
            iban=iban,
            iban_cc=iban_cc.upper() if iban_cc else None,
            ip=_clean(data.get("ip")),
            cc_asn=cc_asn,
            session_id=_clean(data.get("session_id")),
            card_id=_clean(data.get("card_id")),
            phone_number=_clean(data.get("phone_number")),
            operator=_clean(data.get("operator")),
            label=label,
            scenario=_clean(data.get("scenario")),
        )
