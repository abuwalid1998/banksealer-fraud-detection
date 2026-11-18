from datetime import datetime

from banksealer.preparation import TransactionPreparer, parse_amount, parse_timestamp


def test_aliases_types_and_enrichment():
    p = TransactionPreparer()
    tx = p.prepare({"Transaction_ID": "T1", "customer_id": " C1 ", "date_time": "16/11/2026 10:30",
                    "type": "Wire", "value": "1.234,50 EUR", "beneficiary_iban": "de89 3704 0044 0532 0130 00",
                    "ip_address": "1.2.3.4", "ip_country": "it", "asn": "3269", "bank": "B1"})
    assert tx.tx_id == "T1" and tx.user_id == "C1" and tx.bank_id == "B1"
    assert tx.tx_type == "bank_transfer"
    assert tx.amount == 1234.50
    assert tx.iban == "DE89370400440532013000" and tx.iban_cc == "DE"
    assert tx.cc_asn == "IT-AS3269" and tx.ip_country == "IT"
    assert tx.timestamp == datetime(2026, 11, 16, 10, 30)


def test_validation_and_dedup():
    p = TransactionPreparer()
    assert p.prepare({"user_id": "C1", "timestamp": "2026-01-01 10:00", "tx_type": "transfer", "amount": 10}) is None
    assert p.prepare({"user_id": "C1", "timestamp": "nope", "tx_type": "recharge", "amount": 10}) is None
    assert p.prepare({"user_id": "C1", "timestamp": "2026-01-01", "tx_type": "recharge", "amount": -5}) is None
    good = {"tx_id": "X", "user_id": "C1", "timestamp": 1767261600, "tx_type": "topup", "amount": "10",
            "phone": "+39 333"}
    assert p.prepare(good) is not None
    assert p.prepare(good) is None
    assert p.stats == {"received": 5, "accepted": 1, "rejected": 3, "duplicates": 1}
    assert len(p.errors) == 3


def test_field_mapping_has_priority():
    p = TransactionPreparer({"cust": "user_id", "amt_cents": "amount"})
    tx = p.prepare({"cust": "Z9", "user_id": "ignored", "amt_cents": "42", "ts": "2026-02-02T08:00:00Z",
                    "kind": "prepaid", "card": "5355"})
    assert tx.user_id == "Z9" and tx.amount == 42 and tx.tx_type == "prepaid_card"


def test_parsers():
    assert parse_amount("1,234.50") == 1234.5
    assert parse_amount("12,5") == 12.5
    assert parse_timestamp(1767261600000).year == 2026
