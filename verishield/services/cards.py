"""Card controls: freeze / unfreeze a payment card.

A card is frozen when the customer denies a held card payment (push or SMS confirmation), when the customer taps
"freeze my card" in the bank's app, or by the AML team. VeriShield records the control, sends ``FREEZE_CARD`` /
``UNFREEZE_CARD`` to the bank's card-management system through the outbound execution adapter, and - as a second
line of defence - declines card payments and ATM withdrawals with that card while it is frozen.

Who may unfreeze: the customer (through the app) for a freeze they set themselves; the AML Manager for any freeze
(an AML / automatic freeze linked to fraud must be released by the AML team, never by the app).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..models import CardControl, Transaction, utcnow

CARD_TYPES = ("card_payment", "cash_withdrawal")
CARD_KEYS = ("card_ref", "card_token", "masked_pan")


def card_of(details: Optional[dict]) -> str:
    d = details or {}
    return next((str(d[k]) for k in CARD_KEYS if d.get(k)), "")


def active_freeze(db: Session, tenant_id: str, space: str, card_ref: str) -> Optional[CardControl]:
    if not card_ref:
        return None
    return db.execute(select(CardControl).where(CardControl.tenant_id == tenant_id, CardControl.space == space,
                                                CardControl.card_ref == card_ref, CardControl.status == "FROZEN")
                      .order_by(CardControl.frozen_at.desc())).scalars().first()


def _command(db: Session, tenant_id: str, space: str, ctl: CardControl, command: str) -> None:
    from . import core_api
    if core_api.available(db, tenant_id, space, command):        # configured core-banking API for this purpose
        call = core_api.execute(db, tenant_id, space, command, {"card_ref": ctl.card_ref, "customer_id": ctl.customer_ref,
                                                                  "transaction_id": ctl.tx_ref or None},
                                ctl.unfrozen_by or ctl.frozen_by or "VeriShield", "AUTO", ctl.reason, commit=False)
        ctl.command_status = "ACKED" if call.status == "SUCCESS" else "FAILED"
        ref = (call.result or {}).get("reference")
        ctl.command_detail = (f"{command} accepted by the core (ref {ref})" if call.status == "SUCCESS"
                              else f"{command} failed: {call.error}")[:300]
        return
    if space == "DEMO":
        ctl.command_status, ctl.command_detail = "SIMULATED", f"Demo Mode: {command} simulated"
        return
    from .connectors import send_bank_command
    res = send_bank_command(db, tenant_id, {"command": command, "transaction_id": ctl.tx_ref or ctl.id,
                                            "card_reference": ctl.card_ref, "customer_id": ctl.customer_ref,
                                            "reason": ctl.reason, "control_id": ctl.id, "requested_at":
                                            utcnow().isoformat(timespec="seconds") + "Z"})
    ctl.command_status = "SENT" if res.get("sent") else "FAILED"
    ctl.command_detail = res.get("message", "")[:300]


def freeze(db: Session, tenant_id: str, space: str, card_ref: str, customer_ref: str, reason: str, source: str,
           actor: str, tx_ref: str = "", case_id: str = "") -> CardControl:
    if not card_ref:
        raise ValueError("card reference is required")
    current = active_freeze(db, tenant_id, space, card_ref)
    if current is not None:
        return current
    ctl = CardControl(tenant_id=tenant_id, space=space, card_ref=card_ref, customer_ref=customer_ref,
                      reason=(reason or "")[:300], source=source, tx_ref=tx_ref, case_id=case_id, frozen_by=actor)
    db.add(ctl)
    db.flush()
    _command(db, tenant_id, space, ctl, "FREEZE_CARD")
    db.commit()
    audit.record(db, tenant_id, "card.freeze", actor=actor, resource_type="card", resource_id=card_ref, space=space,
                 after={"customer": customer_ref, "source": source, "command": ctl.command_status}, reason=reason)
    return ctl


def unfreeze(db: Session, tenant_id: str, space: str, card_ref: str, actor: str, by_customer: bool = False,
             reason: str = "") -> CardControl:
    ctl = active_freeze(db, tenant_id, space, card_ref)
    if ctl is None:
        raise LookupError("the card is not frozen")
    if by_customer and ctl.source != "CUSTOMER_APP":
        raise PermissionError("this card was frozen by the bank; the AML team must release it")
    ctl.status, ctl.unfrozen_by, ctl.unfrozen_at = "ACTIVE", actor, utcnow()
    _command(db, tenant_id, space, ctl, "UNFREEZE_CARD")
    db.commit()
    audit.record(db, tenant_id, "card.unfreeze", actor=actor, resource_type="card", resource_id=card_ref, space=space,
                 after={"by_customer": by_customer, "command": ctl.command_status}, reason=reason)
    return ctl


def freeze_for_transaction(db: Session, tx: Transaction, reason: str, source: str, actor: str,
                           case_id: str = "") -> Optional[CardControl]:
    card = card_of(tx.details)
    if tx.tx_type not in CARD_TYPES or not card:
        return None
    return freeze(db, tx.tenant_id, tx.space, card, tx.customer_ref, reason, source, actor, tx.tx_ref, case_id)


def signals(db: Session, tenant_id: str, space: str, etx, tx_row) -> list[dict]:
    if etx.tx_type not in CARD_TYPES or tx_row is None:
        return []
    ctl = active_freeze(db, tenant_id, space, card_of(tx_row.details))
    if ctl is None:
        return []
    return [{"id": "CARD-FROZEN", "name": "Frozen card", "kind": "signal", "action": "DECLINE",
             "reason_code": "CARD_FROZEN",
             "message": f"Card {ctl.card_ref} is frozen since {ctl.frozen_at:%d %b %Y %H:%M} ({ctl.source.replace('_', ' ').lower()}: "
                        f"{ctl.reason[:80]})"}]


def as_dict(c: CardControl) -> dict:
    return {"id": c.id, "card_ref": c.card_ref, "customer_ref": c.customer_ref, "status": c.status, "reason": c.reason,
            "source": c.source, "tx_ref": c.tx_ref, "case_id": c.case_id, "command_status": c.command_status,
            "command_detail": c.command_detail, "frozen_by": c.frozen_by, "frozen_at": c.frozen_at,
            "unfrozen_by": c.unfrozen_by, "unfrozen_at": c.unfrozen_at}


def customer_cards(db: Session, tenant_id: str, space: str, customer_ref: str) -> list[dict]:
    """Cards seen on the customer's transactions with their current control status."""
    seen: dict[str, dict] = {}
    for d, tt, ts in db.execute(select(Transaction.details, Transaction.tx_type, Transaction.ts).where(
            Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.customer_ref == customer_ref,
            Transaction.tx_type.in_(CARD_TYPES)).order_by(Transaction.ts.desc()).limit(500)).all():
        card = card_of(d)
        if card and card not in seen:
            seen[card] = {"card_ref": card, "last_used": ts, "last_type": tt}
    controls = db.execute(select(CardControl).where(CardControl.tenant_id == tenant_id, CardControl.space == space,
                                                    CardControl.customer_ref == customer_ref)
                          .order_by(CardControl.frozen_at.desc())).scalars().all()
    for c in controls:
        seen.setdefault(c.card_ref, {"card_ref": c.card_ref, "last_used": None, "last_type": ""})
    out = []
    for card, info in seen.items():
        latest = next((c for c in controls if c.card_ref == card), None)
        out.append({**info, "status": latest.status if latest else "ACTIVE",
                    "control": as_dict(latest) if latest else None,
                    "history": [as_dict(c) for c in controls if c.card_ref == card][:10]})
    return out


__all__ = ["freeze", "unfreeze", "freeze_for_transaction", "signals", "active_freeze", "card_of", "customer_cards",
           "as_dict"]
