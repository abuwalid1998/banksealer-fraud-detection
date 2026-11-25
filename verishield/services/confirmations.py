"""Customer confirmation of held payments - in the bank's mobile app (push) and/or by SMS.

Channel (policy ``signals.confirmation_channel``): ``PUSH_THEN_SMS`` (default: the app first, SMS if there is no
answer after ``push_fallback_seconds``), ``PUSH`` or ``SMS``. The app backend answers through
``POST /api/v1/confirmations/{id}/respond`` (API key with the ``confirmations:respond`` scope); a NO on a card
payment also freezes the card (``freeze_card_on_denial``).

When a pre-authorisation payment is HELD, the customer receives an SMS ("Did you send 5,000 ILS to ...? Reply
1 = yes, 2 = no"). The answer comes back through the SMS gateway's inbound webhook
(``POST /api/v1/confirmations/reply``) or is recorded by the call centre:

* **1 / YES** - the payment is released through the bank integration, the alert is closed as confirmed by the
  customer and the transaction is labelled legitimate.
* **2 / NO**  - the payment is rejected, the transaction is labelled fraud, the beneficiary goes on the watch list
  and an URGENT case is opened for the AML team.

Unanswered requests expire after the configured minutes and the hold stays with the analysts.
"""
from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..models import Alert, Customer, CustomerConfirmation, Decision, Tenant, Transaction, utcnow
from . import notify
from .signals import cfg, label_transactions, watch_transaction

YES = {"1", "YES", "Y", "OK", "نعم"}
NO = {"2", "NO", "N", "لا"}


def customer_phone(db: Session, tenant_id: str, space: str, customer_ref: str) -> str:
    c = db.execute(select(Customer).where(Customer.tenant_id == tenant_id, Customer.space == space,
                                          Customer.customer_ref == customer_ref)).scalar()
    return str((c.attributes or {}).get("phone") or "") if c else ""


INBOUND_TYPES = ("incoming_transfer", "cash_deposit", "cheque_deposit")


def _push_payload(tenant: Tenant, conf: CustomerConfirmation, tx: Transaction, to: str) -> dict:
    return {"type": "PAYMENT_CONFIRMATION", "confirmation_id": conf.id, "customer_id": tx.customer_ref,
            "transaction_id": tx.tx_ref, "amount": round(float(tx.amount), 2), "currency": tx.currency,
            "transaction_type": tx.tx_type, "counterparty": to, "channel": tx.channel,
            "card_reference": (tx.details or {}).get("card_ref") or "",
            "message": f"Did you make a {tx.tx_type.replace('_', ' ')} of {tx.amount:,.2f} {tx.currency} to {to}?",
            "expires_at": conf.expires_at.isoformat(timespec="seconds") + "Z",
            "respond_path": f"/api/v1/confirmations/{conf.id}/respond",
            "answers": ["YES", "NO"], "institution": tenant.display_name}


def _send_sms(db: Session, tenant: Tenant, conf: CustomerConfirmation) -> bool:
    if not conf.phone:
        return False
    if conf.space == "DEMO":
        notify._record(db, tenant.id, "SMS", conf.phone, "", conf.message, "SIMULATED", "Demo Mode: SMS simulated",
                       "customer_confirmation")
        return True
    ok, detail = notify.send_sms(db, tenant, conf.phone, conf.message, "customer_confirmation")
    if not ok:
        conf.reply = detail[:200]
    return ok


def request_confirmation(db: Session, tenant: Tenant, tx: Transaction, dec: Decision, doc: dict
                         ) -> Optional[CustomerConfirmation]:
    c = cfg(doc)
    if not c.get("confirmation_sms") or tx.tx_type in INBOUND_TYPES:
        return None
    phone = customer_phone(db, tenant.id, tx.space, tx.customer_ref)
    channel = str(c.get("confirmation_channel") or "SMS").upper()
    push_on = channel in ("PUSH", "PUSH_THEN_SMS") and (tx.space == "DEMO" or notify.push_ready(tenant))
    if not phone and not push_on:
        return None
    code = f"{secrets.randbelow(9000) + 1000}"
    to = (tx.details or {}).get("beneficiary_name") or tx.beneficiary or "the beneficiary"
    text = (f"{tenant.display_name}: did you make a {tx.tx_type.replace('_', ' ')} of {tx.amount:,.2f} "
            f"{tx.currency} to {to}? Reply 1 = YES it was me, 2 = NO it was not me. Ref {code}")
    conf = CustomerConfirmation(tenant_id=tenant.id, space=tx.space, tx_ref=tx.tx_ref, customer_ref=tx.customer_ref,
                                phone=phone, code=code, message=text,
                                expires_at=utcnow() + timedelta(minutes=int(c.get("confirmation_minutes", 30))))
    if push_on:
        db.add(conf)
        db.flush()
        conf.channel = "PUSH"
        payload = _push_payload(tenant, conf, tx, to)
        if tx.space == "DEMO":
            ok = True
            notify._record(db, tenant.id, "PUSH", tx.customer_ref, "", payload["message"], "SIMULATED",
                           "Demo Mode: app notification simulated", "customer_confirmation")
        else:
            ok, _ = notify.send_push(db, tenant, payload)
        if ok:
            conf.status = "SENT"
            if channel == "PUSH_THEN_SMS" and phone:
                conf.fallback_at = utcnow() + timedelta(seconds=int(c.get("push_fallback_seconds", 120)))
            db.commit()
            return conf
        if channel == "PUSH" or not phone:          # push failed and no SMS fallback
            conf.status = "NOT_SENT"
            db.commit()
            return conf
        conf.channel = "PUSH+SMS"                     # push failed: SMS straight away
        conf.status = "SENT" if _send_sms(db, tenant, conf) else "NOT_SENT"
        db.commit()
        return conf
    if tx.space == "DEMO":
        conf.status = "SENT"            # simulated delivery; answer it from the transaction or Confirmations page
        notify._record(db, tenant.id, "SMS", phone, "", text, "SIMULATED", "Demo Mode: SMS simulated",
                       "customer_confirmation")
    else:
        ok, detail = notify.send_sms(db, tenant, phone, text, "customer_confirmation")
        conf.status = "SENT" if ok else "NOT_SENT"
        if not ok:
            conf.reply = detail[:200]
    db.add(conf)
    db.commit()
    return conf


def _find(db: Session, tenant_id: str, phone: str, text: str) -> Optional[CustomerConfirmation]:
    m = re.search(r"\b(\d{4})\b", text or "")
    q = select(CustomerConfirmation).where(CustomerConfirmation.tenant_id == tenant_id,
                                           CustomerConfirmation.status == "SENT")
    if m:
        hit = db.execute(q.where(CustomerConfirmation.code == m.group(1)).order_by(
            CustomerConfirmation.sent_at.desc())).scalars().first()
        if hit:
            return hit
    digits = re.sub(r"\D", "", phone or "")[-9:]
    for conf in db.execute(q.order_by(CustomerConfirmation.sent_at.desc()).limit(200)).scalars():
        if digits and re.sub(r"\D", "", conf.phone)[-9:] == digits:
            return conf
    return None


def answer(db: Session, conf: CustomerConfirmation, reply: str, actor: str = "customer SMS", via: str = "SMS",
           freeze_card: Optional[bool] = None) -> dict:
    from .cases import create_case
    from .decision import request_bank_action
    from ..deps import Principal

    if conf.status != "SENT":
        raise ValueError(f"confirmation is already {conf.status}")
    word = (reply or "").strip().split()[0].upper() if (reply or "").strip() else ""
    if word not in YES | NO:
        raise ValueError("reply must be 1 (yes) or 2 (no)")
    conf.reply, conf.answered_at, conf.answered_via = reply[:200], utcnow(), via[:16]
    tx = db.execute(select(Transaction).where(Transaction.tenant_id == conf.tenant_id, Transaction.space == conf.space,
                                              Transaction.tx_ref == conf.tx_ref)).scalar()
    system = Principal(tenant_id=conf.tenant_id, kind="user", role="AML_MANAGER",
                       perms={"cases.view_all", "cases.create", "cases.dispose", "cases.assign"}, space=conf.space,
                       display=f"Customer confirmation ({actor})", user_id=None)
    out: dict = {"confirmation": conf.id, "tx_ref": conf.tx_ref}
    alerts = db.execute(select(Alert).where(Alert.tenant_id == conf.tenant_id, Alert.space == conf.space,
                                            Alert.tx_ref == conf.tx_ref)).scalars().all()
    if word in YES:
        conf.status = "CONFIRMED"
        out["execution"] = request_bank_action(db, conf.tenant_id, conf.space, conf.tx_ref, "RELEASE", system,
                                               "Customer confirmed the payment by SMS")
        for a in alerts:
            if a.status in ("OPEN", "TRIAGED"):
                a.status, a.closed_reason, a.closed_at = "CLOSED", "Confirmed by the customer (SMS)", utcnow()
        label_transactions(db, conf.tenant_id, conf.space, [conf.tx_ref], 0, "customer_confirmation")
        out["result"] = "released"
    else:
        conf.status = "DENIED"
        out["execution"] = request_bank_action(db, conf.tenant_id, conf.space, conf.tx_ref, "REJECT", system,
                                               "Customer denied the payment by SMS")
        label_transactions(db, conf.tenant_id, conf.space, [conf.tx_ref], 1, "customer_confirmation")
        if tx is not None:
            watch_transaction(db, tx, f"Customer denied payment {tx.tx_ref} by SMS", actor, "CONFIRMATION", tx.tx_ref)
        open_alerts = [a.id for a in alerts if a.status in ("OPEN", "TRIAGED")]
        data = {"priority": "URGENT", "title": f"Customer denied payment {conf.tx_ref}",
                "purpose": "The customer replied NO to the SMS confirmation: probable fraud.",
                "classification": "FRAUD_SUSPICION"}
        data.update({"alert_ids": open_alerts} if open_alerts else {"tx_refs": [conf.tx_ref]})
        case = create_case(db, system, data)
        from .cases import auto_assign
        auto_assign(db, case, force=True)
        out["case_id"] = case.id
        out["result"] = "rejected - case opened"
        if tx is not None:
            from . import cards
            from .policy import active_policy
            wanted = freeze_card if freeze_card is not None else \
                cfg(active_policy(db, conf.tenant_id).document).get("freeze_card_on_denial", True)
            if wanted:
                ctl = cards.freeze_for_transaction(db, tx, f"Customer denied {tx.tx_ref} ({via})", "AUTO", actor,
                                                   case.id)
                if ctl is not None:
                    out["card_frozen"] = ctl.card_ref
    db.commit()
    audit.record(db, conf.tenant_id, "confirmation.answer", actor=actor, resource_type="transaction",
                 resource_id=conf.tx_ref, space=conf.space, after=out)
    return out


def inbound(db: Session, tenant_id: str, phone: str, text: str) -> dict:
    conf = _find(db, tenant_id, phone, text)
    if conf is None:
        raise LookupError("no pending confirmation for this number / reference")
    return answer(db, conf, text)


def push_fallback(db: Session) -> int:
    """App notifications without an answer after ``push_fallback_seconds`` are repeated by SMS."""
    n = 0
    for conf in db.execute(select(CustomerConfirmation).where(CustomerConfirmation.status == "SENT",
                                                              CustomerConfirmation.channel == "PUSH",
                                                              CustomerConfirmation.fallback_at.is_not(None),
                                                              CustomerConfirmation.fallback_at <= utcnow())).scalars().all():
        tenant = db.get(Tenant, conf.tenant_id)
        conf.fallback_at = None
        conf.channel = "PUSH+SMS"
        _send_sms(db, tenant, conf)
        n += 1
    db.commit()
    return n


def expire(db: Session) -> int:
    push_fallback(db)
    now = utcnow()
    n = 0
    for conf in db.execute(select(CustomerConfirmation).where(CustomerConfirmation.status == "SENT",
                                                              CustomerConfirmation.expires_at < now)).scalars():
        conf.status = "EXPIRED"
        n += 1
    db.commit()
    return n
