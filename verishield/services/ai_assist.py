"""AI-assisted case narratives (optional, switched ON/OFF by the IT Admin under *AI Agents*).

A case fact sheet (alerts, transactions, reason codes, AML indicators, notes, recommendation, disposition) is
always built locally. Without AI - or when the bank's server has no route to the model - VeriShield writes a
deterministic narrative from that fact sheet. With AI switched on, the fact sheet is sent to Claude through the
official Anthropic SDK (``pip install anthropic``; the Windows offline bundle includes it with ``--with-ai``),
customer identifiers are masked by default, and the analyst edits and approves the draft before it is saved.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Alert, Case, CaseEntityLink, CaseNote, Customer, Decision, Tenant, Transaction
from ..security import mask

log = logging.getLogger("verishield.ai")

DEFAULTS = {"enabled": False, "model": "claude-opus-5-5", "effort": "medium", "base_url": "",
            "api_key_ref": None, "mask_identifiers": True, "fallbacks": True, "timeout_seconds": 120}
SYSTEM = ("You are an assistant to anti-money-laundering analysts at a bank. Write factual, neutral case "
          "narratives from the fact sheet you are given. Use only facts from the fact sheet; never invent amounts, "
          "dates, names or conclusions. Model scores and alerts are indicators for human review, not findings of "
          "fraud or money laundering. Write in plain English for a compliance reader.")


def config(tenant: Tenant) -> dict:
    return {**DEFAULTS, **((tenant.settings or {}).get("ai") or {})}


def public_config(tenant: Tenant) -> dict:
    c = config(tenant)
    c["api_key_configured"] = bool(c.pop("api_key_ref", None))
    try:
        import anthropic  # noqa: F401
        c["sdk_installed"] = True
    except ImportError:
        c["sdk_installed"] = False
    return c


def fact_sheet(db: Session, case: Case, masked: bool) -> dict:
    links = db.execute(select(CaseEntityLink).where(CaseEntityLink.case_id == case.id)).scalars().all()
    refs = {t: [l.entity_ref for l in links if l.entity_type == t] for t in ("TRANSACTION", "CUSTOMER", "ALERT")}
    m = (lambda v: mask(v, 3)) if masked else (lambda v: v)
    customers = db.execute(select(Customer).where(Customer.tenant_id == case.tenant_id, Customer.space == case.space,
                                                  Customer.customer_ref.in_(refs["CUSTOMER"]))).scalars().all()
    txs = db.execute(select(Transaction, Decision).outerjoin(Decision, Decision.id == Transaction.decision_id).where(
        Transaction.tenant_id == case.tenant_id, Transaction.space == case.space,
        Transaction.tx_ref.in_(refs["TRANSACTION"])).order_by(Transaction.ts)).all()
    alerts = db.execute(select(Alert).where(Alert.id.in_(refs["ALERT"]))).scalars().all()
    notes = db.execute(select(CaseNote).where(CaseNote.case_id == case.id).order_by(CaseNote.created_at)).scalars().all()
    return {
        "case": {"id": case.id, "title": case.title, "priority": case.priority, "status": case.status,
                 "classification": case.classification, "purpose": case.purpose, "opened": str(case.created_at)[:16]},
        "customers": [{"ref": m(c.customer_ref), "name": m(c.display_name) if masked else c.display_name,
                       "type": c.customer_type, "risk_rating": c.risk_category, "activity": c.business_activity,
                       "expected_monthly_turnover": c.expected_monthly_turnover, "tenure_months": c.tenure_months,
                       "segment": c.segment_label} for c in customers],
        "transactions": [{"ref": t.tx_ref, "time": str(t.ts)[:16], "type": t.tx_type, "amount": t.amount,
                          "currency": t.currency, "channel": t.channel,
                          "counterparty": m(t.beneficiary) if t.beneficiary else None,
                          "counterparty_country": t.beneficiary_country,
                          "decision": d.decision if d else None, "fraud_score": round(d.fraud_score, 3) if d else None,
                          "reason_codes": d.reason_codes if d else [],
                          "aml_indicators": [i.get("message") for i in (d.aml_indicators or [])] if d else []}
                         for t, d in txs],
        "alerts": [{"id": a.id, "severity": a.severity, "summary": a.summary, "status": a.status} for a in alerts],
        "notes": [{"kind": n.kind, "text": n.body[:800], "at": str(n.created_at)[:16]} for n in notes
                  if not getattr(n, "restricted", False)],
        "recommendation": case.recommendation or None,
        "disposition": case.disposition or None,
    }


def template_narrative(facts: dict) -> str:
    """Deterministic narrative used when AI is off or unavailable."""
    c = facts["case"]
    txs = facts["transactions"]
    total = sum(t["amount"] for t in txs)
    cur = txs[0]["currency"] if txs else ""
    lines = [f"Case {c['id']} ({c['priority']} priority, {c['classification'].replace('_', ' ').lower()}) was opened on "
             f"{c['opened']} UTC. {c['purpose'] or ''}".strip()]
    for cu in facts["customers"]:
        lines.append(f"The subject is customer {cu['ref']} ({cu['type'].lower()}, institution risk rating "
                     f"{cu['risk_rating']}, {cu['tenure_months']} months with the bank, declared expected monthly "
                     f"turnover {cu['expected_monthly_turnover']:,.2f}).")
    if txs:
        lines.append(f"{len(txs)} transaction(s) totalling {total:,.2f} {cur} between {txs[0]['time']} and "
                     f"{txs[-1]['time']} are linked to the case:")
        for t in txs:
            codes = ", ".join(t["reason_codes"][:4]) or "no reason codes"
            lines.append(f"- {t['time']}: {t['type'].replace('_', ' ')} of {t['amount']:,.2f} {t['currency']} via "
                         f"{t['channel'] or 'unknown channel'} to {t['counterparty'] or '-'} "
                         f"({t['counterparty_country'] or '-'}); decision {t['decision']}, score {t['fraud_score']}; "
                         f"{codes}.")
        indicators = sorted({i for t in txs for i in t["aml_indicators"]})
        if indicators:
            lines.append("AML indicators: " + "; ".join(indicators) + ".")
    for n in facts["notes"]:
        lines.append(f"Analyst {n['kind'].lower()} ({n['at']}): {n['text']}")
    if facts["recommendation"]:
        r = facts["recommendation"]
        lines.append(f"Analyst recommendation: {r.get('label')} - {r.get('rationale')}")
    if facts["disposition"]:
        d = facts["disposition"]
        lines.append(f"Manager disposition: {d.get('label')} - {d.get('rationale')}")
    lines.append("Model signals and alerts are indicators for human review, not findings of fraud or money laundering.")
    return "\n".join(lines)


def _claude(db: Session, tenant: Tenant, prompt: str) -> str:
    import anthropic

    from .connectors import resolve_secret
    c = config(tenant)
    key = resolve_secret(db, tenant.id, c.get("api_key_ref"))
    if not key:
        raise RuntimeError("no API key stored for the AI assistant")
    client = anthropic.Anthropic(api_key=key, base_url=c.get("base_url") or None,
                                 timeout=float(c.get("timeout_seconds") or 120), max_retries=2)
    kwargs = {"model": c.get("model") or DEFAULTS["model"], "max_tokens": 16000, "system": SYSTEM,
              "output_config": {"effort": c.get("effort") or "medium"},
              "messages": [{"role": "user", "content": prompt}]}
    if c.get("fallbacks", True):
        # a policy decline is retried server-side on another model inside the same call
        response = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                               **kwargs)
    else:
        response = client.messages.create(**kwargs)
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined this request")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise RuntimeError(f"empty response (stop reason {response.stop_reason})")
    return text


def narrative(db: Session, case: Case, purpose: str = "summary") -> dict:
    """Return ``{"text", "source": "ai"|"template", "model", "note"}`` - a draft for the analyst to edit."""
    import json
    tenant = db.get(Tenant, case.tenant_id)
    c = config(tenant)
    facts = fact_sheet(db, case, masked=bool(c.get("mask_identifiers", True)))
    if not c.get("enabled"):
        return {"text": template_narrative(facts), "source": "template", "model": None,
                "note": "AI assistant is switched off - narrative generated from the case facts."}
    task = ("Write the narrative section of a suspicious transaction report (STR) for the Financial Follow-Up Unit: "
            "who, what, when, how much, why it is unusual for this customer, and what the bank did."
            if purpose == "str" else
            "Summarise this case for the AML Manager in 5-10 sentences: what happened, why it was flagged, what the "
            "analyst found, and what is still open.")
    prompt = f"{task}\n\nFact sheet (JSON):\n{json.dumps(facts, default=str, indent=1)}"
    try:
        text = _claude(db, tenant, prompt)
        return {"text": text, "source": "ai", "model": c.get("model"),
                "note": "AI draft - verify every statement against the case before using it."
                        + (" Customer identifiers were masked before sending." if c.get("mask_identifiers") else "")}
    except ImportError:
        reason = "the anthropic package is not installed on the server"
    except Exception as exc:  # noqa: BLE001 - always fall back to the local narrative
        log.warning("AI narrative failed: %s", exc)
        reason = f"{type(exc).__name__}: {str(exc)[:200]}"
    return {"text": template_narrative(facts), "source": "template", "model": None,
            "note": f"AI assistant unavailable ({reason}) - narrative generated from the case facts."}


__all__ = ["narrative", "fact_sheet", "template_narrative", "config", "public_config", "Optional"]
