import csv
import io
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from banksealer.preparation import FIELD_ALIASES, PREPARATION_RULES, TYPE_ALIASES, TransactionPreparer

from ...db import get_session
from ...models import ChannelConfig, PreparationError, User
from ..deps import admin, audit, page, viewer

router = APIRouter(prefix="/api/preparation", tags=["data preparation"])

FIELD_DOCS = {
    "tx_id": "unique transaction id (generated when missing)", "user_id": "customer id (required)",
    "timestamp": "date/time of the operation (required)",
    "tx_type": "bank_transfer | phone_recharge | prepaid_card (required)", "amount": "positive amount (required)",
    "bank_id": "bank/company - selects the thresholds", "currency": "ISO currency", "iban": "beneficiary IBAN",
    "iban_cc": "beneficiary country (derived from the IBAN)", "ip": "client IP address",
    "cc_asn": "country-ASN of the IP, e.g. IT-AS3269 (or ip_country + asn)", "session_id": "online session",
    "card_id": "prepaid card", "phone_number": "recharged phone number", "operator": "phone operator",
    "label": "ground truth fraud label (optional)", "scenario": "fraud scenario (mock data)",
}
REQUIRED = {"user_id", "timestamp", "tx_type", "amount"}


@router.get("/schema")
def schema(_: User = Depends(viewer)):
    return {
        "fields": [{"name": f, "description": FIELD_DOCS.get(f, ""), "required": f in REQUIRED,
                    "aliases": list(aliases)} for f, aliases in FIELD_ALIASES.items()],
        "type_aliases": TYPE_ALIASES,
        "rules": [{"name": k, "description": v} for k, v in PREPARATION_RULES.items()],
        "steps": ["Field mapping (channel mapping first, then known aliases)",
                  "Constant defaults for missing fields", "Timestamp and amount parsing (+ amount scale)",
                  "Operation type normalisation (+ channel type mapping)",
                  "Validation (required fields, amount limits, allowed types)",
                  "Enrichment (IBAN normalisation and country, country/ASN)", "De-duplication by transaction id"],
    }


class PreviewIn(BaseModel):
    channel_id: Optional[int] = None
    field_mapping: dict = Field(default_factory=dict)
    preparation: dict = Field(default_factory=dict)
    records: list = Field(default_factory=list)
    text: Optional[str] = None      # CSV (with header), JSON array or JSON lines


def parse_text(text: str) -> list:
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        return json.loads(text)
    if text.startswith("{"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return list(csv.DictReader(io.StringIO(text)))


@router.post("/preview")
def preview(body: PreviewIn, _: User = Depends(viewer), s: Session = Depends(get_session)):
    mapping, rules = body.field_mapping, body.preparation
    if body.channel_id is not None:
        c = s.get(ChannelConfig, body.channel_id)
        if c is None:
            raise HTTPException(404, "channel not found")
        mapping = mapping or c.field_mapping or {}
        rules = rules or c.preparation or {}
    try:
        records = body.records or parse_text(body.text or "")
    except (json.JSONDecodeError, csv.Error) as exc:
        raise HTTPException(400, f"could not parse the sample: {exc}")
    prep = TransactionPreparer(mapping, rules=rules)
    results = []
    for raw in records[:200]:
        before = prep.stats["rejected"]
        tx = prep.prepare(raw, "preview")
        if tx is not None:
            results.append({"ok": True, "raw": raw, "transaction": tx.to_dict()})
        else:
            error = prep.errors[-1] if prep.stats["rejected"] > before else "duplicate transaction id"
            results.append({"ok": False, "raw": raw, "error": error.replace("[preview] ", "")})
    return {"results": results, "stats": prep.stats}


@router.get("/errors")
def errors(channel: Optional[str] = None, page_no: int = 1, size: int = 50, _: User = Depends(viewer),
           s: Session = Depends(get_session)):
    q = select(PreparationError)
    if channel:
        q = q.where(PreparationError.channel == channel)
    total = s.scalar(select(func.count()).select_from(q.subquery()))
    rows = s.scalars(q.order_by(PreparationError.id.desc()).offset((page_no - 1) * size).limit(size))
    by_channel = dict(s.execute(select(PreparationError.channel, func.count()).group_by(PreparationError.channel)).all())
    result = page([{"id": r.id, "channel": r.channel, "message": r.message, "raw": r.raw,
                    "created_at": r.created_at.isoformat(timespec="seconds")} for r in rows], total, page_no, size)
    result["by_channel"] = by_channel
    return result


@router.delete("/errors")
def clear_errors(channel: Optional[str] = None, user: User = Depends(admin), s: Session = Depends(get_session)):
    q = delete(PreparationError)
    if channel:
        q = q.where(PreparationError.channel == channel)
    n = s.execute(q).rowcount
    audit(s, user, "preparation.errors_cleared", channel, {"rows": n})
    s.commit()
    return {"deleted": n}
