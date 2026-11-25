"""Network risk scoring: rings of linked customers, risk propagated from confirmed fraud, mule accounts.

Customers are linked when they transfer money to the same account, use the same device or connect from the same
IP address (the links of the investigation network graph). Popular payees, shops, ATMs and billers - values
shared by more than ``HUB_LIMIT`` customers - are left out, so a ring is a small group that shares something
unusual.

* **Rings** - connected groups of linked customers (union-find over the shared values).
* **Seeds** - confirmed fraud (labelled transactions), watch-listed customers / accounts / devices / IPs, STR
  subjects and confirmed sanctions matches.
* **Propagation** - risk spreads from the seeds through shared values with a decay per hop
  (1 hop 0.8, 2 hops 0.55, 3 hops 0.35) and is combined with the share of seeded members in the ring.
* **Mule score of a payee account** - computed live in the decision: how many different customers paid it
  recently, how many of them were confirmed fraud victims or new to the payee.

The leader node recomputes the scores every 30 minutes; the decision uses them through the ``network_review``,
``mule_review`` and ``mule_hold`` signal settings of the policy.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from datetime import timedelta
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import (Customer, NetworkScore, ScreeningMatch, StrReport, Transaction, WatchlistEntry, utcnow)
from .intel import GENERIC_DEVICES, HUB_LIMIT, LINK_TYPES

DECAY = {0: 1.0, 1: 0.8, 2: 0.55, 3: 0.35}


def _links(db: Session, tenant_id: str, space: str, days: int) -> dict[str, set]:
    """value-key -> customers sharing it (hubs removed)."""
    since = utcnow() - timedelta(days=days)
    base = (Transaction.tenant_id == tenant_id) & (Transaction.space == space) & (Transaction.ts >= since)
    groups: dict[str, set] = defaultdict(set)
    for ref, val in db.execute(select(Transaction.customer_ref, Transaction.beneficiary).where(
            base, Transaction.tx_type.in_(LINK_TYPES), Transaction.beneficiary.is_not(None)).distinct()).all():
        groups[f"A:{val}"].add(ref)
    for ref, val in db.execute(select(Transaction.customer_ref, Transaction.device_ref).where(
            base, Transaction.device_ref.is_not(None)).distinct()).all():
        if val and val not in GENERIC_DEVICES:
            groups[f"D:{val}"].add(ref)
    for ref, val in db.execute(select(Transaction.customer_ref, Transaction.ip).where(
            base, Transaction.ip.is_not(None)).distinct()).all():
        if val:
            groups[f"I:{val}"].add(ref)
    return {k: v for k, v in groups.items() if 2 <= len(v) <= HUB_LIMIT}


def _seeds(db: Session, tenant_id: str, space: str, days: int) -> dict[str, str]:
    since = utcnow() - timedelta(days=days)
    seeds: dict[str, str] = {}
    for (ref,) in db.execute(select(Transaction.customer_ref).where(
            Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.label == 1,
            Transaction.mode != "HISTORY", Transaction.ts >= since).distinct()).all():
        seeds[ref] = "confirmed fraud on the account"
    for w in db.execute(select(WatchlistEntry).where(WatchlistEntry.tenant_id == tenant_id, WatchlistEntry.space == space,
                                                     WatchlistEntry.active.is_(True), WatchlistEntry.kind == "CUSTOMER")
                        ).scalars():
        seeds.setdefault(w.value, "customer on the watch list")
    for r in db.execute(select(StrReport).where(StrReport.tenant_id == tenant_id, StrReport.space == space,
                                                StrReport.status.in_(("APPROVED", "SUBMITTED")))).scalars():
        for s in r.subjects or []:
            if s.get("role") == "SUBJECT" and s.get("ref"):
                seeds.setdefault(s["ref"], "subject of a suspicious transaction report")
    for (ref,) in db.execute(select(ScreeningMatch.customer_ref).where(
            ScreeningMatch.tenant_id == tenant_id, ScreeningMatch.space == space,
            ScreeningMatch.status == "TRUE_MATCH", ScreeningMatch.list_code != "PEP")).all():
        if ref:
            seeds.setdefault(ref, "confirmed sanctions match")
    return seeds


def compute(db: Session, tenant_id: str, space: str, days: int = 90) -> dict:
    groups = _links(db, tenant_id, space, days)
    watched = {w.value.upper() for w in db.execute(select(WatchlistEntry).where(
        WatchlistEntry.tenant_id == tenant_id, WatchlistEntry.space == space, WatchlistEntry.active.is_(True))).scalars()}
    seeds = _seeds(db, tenant_id, space, days)
    # customers linked to a watch-listed value are seeds too
    for key, members in groups.items():
        if key[2:].upper() in watched:
            for m in members:
                seeds.setdefault(m, f"shares a watch-listed {'account' if key[0] == 'A' else 'device' if key[0] == 'D' else 'IP'}")
    by_customer: dict[str, set] = defaultdict(set)
    for key, members in groups.items():
        for m in members:
            by_customer[m].add(key)
    # ---- rings (union-find)
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for members in groups.values():
        ms = list(members)
        for m in ms[1:]:
            ra, rb = find(ms[0]), find(m)
            if ra != rb:
                parent[ra] = rb
    rings: dict[str, list] = defaultdict(list)
    for c in by_customer:
        rings[find(c)].append(c)
    # ---- propagation (BFS from seeds over customer -> value -> customer)
    hops: dict[str, int] = {}
    via: dict[str, str] = {}
    q = deque()
    for s in seeds:
        if s in by_customer or s in seeds:
            hops[s] = 0
            q.append(s)
    while q:
        cur = q.popleft()
        if hops[cur] >= 3:
            continue
        for key in by_customer.get(cur, ()):
            for nb in groups[key]:
                if nb not in hops:
                    hops[nb] = hops[cur] + 1
                    via[nb] = f"{'account' if key[0] == 'A' else 'device' if key[0] == 'D' else 'IP'} {key[2:]} " \
                              f"shared with {cur}"
                    q.append(nb)
    now = utcnow()
    db.execute(delete(NetworkScore).where(NetworkScore.tenant_id == tenant_id, NetworkScore.space == space))
    n_rows = 0
    ring_out = []
    for root, members in rings.items():
        if len(members) < 2:
            continue
        rid = "RING-" + hashlib.sha256("|".join(sorted(members)).encode()).hexdigest()[:8].upper()
        seeded = [m for m in members if m in seeds]
        share = len(seeded) / len(members)
        ring_out.append({"ring_id": rid, "size": len(members), "seeded": len(seeded)})
        for m in members:
            h = hops.get(m)
            prop = DECAY.get(h, 0.0) if h is not None else 0.0
            score = round(min(1.0, max(prop, 0.6 * share + (0.1 if len(members) >= 5 else 0.0))), 3)
            reasons = []
            if m in seeds:
                reasons.append(seeds[m])
            elif h is not None:
                reasons.append(f"{h} hop(s) from confirmed risk: {via.get(m, '')}")
            if seeded:
                reasons.append(f"{len(seeded)} of {len(members)} ring members carry confirmed risk")
            db.add(NetworkScore(tenant_id=tenant_id, space=space, customer_ref=m, score=score, ring_id=rid,
                                ring_size=len(members), hops_to_fraud=h, reasons=reasons, computed_at=now))
            n_rows += 1
    db.commit()
    return {"customers_scored": n_rows, "rings": len(ring_out), "seeds": len(seeds),
            "risky_rings": sum(1 for r in ring_out if r["seeded"]), "computed_at": now}


def run_all(db: Session) -> int:
    from ..models import Tenant
    n = 0
    for t in db.execute(select(Tenant)).scalars():
        for space in ("LIVE", "DEMO"):
            n += compute(db, t.id, space)["customers_scored"]
    return n


def customer_score(db: Session, tenant_id: str, space: str, ref: str) -> Optional[NetworkScore]:
    return db.execute(select(NetworkScore).where(NetworkScore.tenant_id == tenant_id, NetworkScore.space == space,
                                                 NetworkScore.customer_ref == ref)).scalar()


def mule_score(db: Session, tenant_id: str, space: str, account: str, exclude_customer: str = "",
               days: int = 30) -> dict:
    """Live mule-account score of a payee account from the payments made to it."""
    if not account:
        return {"score": 0.0}
    since = utcnow() - timedelta(days=days)
    rows = db.execute(select(Transaction.customer_ref, Transaction.label, Transaction.amount).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.beneficiary == account,
        Transaction.tx_type == "bank_transfer", Transaction.ts >= since)).all()
    senders = {r for r, _, _ in rows if r != exclude_customer}
    if len(senders) > HUB_LIMIT:
        return {"score": 0.0, "senders": len(senders), "hub": True}       # a popular payee, not a mule
    fraud_senders = {r for r, lbl, _ in rows if lbl == 1 and r != exclude_customer}
    # senders who already paid this account before the window are established relationships (rent, suppliers);
    # mules receive from customers who never paid them before
    established = {r for (r,) in db.execute(select(Transaction.customer_ref).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.beneficiary == account,
        Transaction.tx_type == "bank_transfer", Transaction.ts < since,
        Transaction.ts >= since - timedelta(days=180)).distinct()).all()}
    new_senders = senders - established
    watched = db.execute(select(WatchlistEntry.id).where(
        WatchlistEntry.tenant_id == tenant_id, WatchlistEntry.space == space, WatchlistEntry.active.is_(True),
        func.upper(WatchlistEntry.value) == account.upper()).limit(1)).scalar() is not None
    k = len(new_senders)
    score = min(1.0, max(0.0, 0.18 * (k - 2)) + 0.5 * (len(fraud_senders) / len(senders) if senders else 0.0)
                + (0.5 if watched else 0.0))
    return {"score": round(score, 3), "senders": len(senders), "new_senders": k, "fraud_senders": len(fraud_senders),
            "watched": watched,
            "amount": round(sum(float(a) for r, _, a in rows if r != exclude_customer), 2)}


def signals(db: Session, tenant_id: str, space: str, etx, c: dict) -> list[dict]:
    hits = []
    if etx.tx_type == "bank_transfer" and etx.iban:
        m = mule_score(db, tenant_id, space, etx.iban, exclude_customer=etx.user_id)
        if m["score"] >= float(c["mule_review"]):
            hits.append({"id": "MULE-ACCOUNT", "name": "Suspected mule account", "kind": "signal",
                         "action": "HOLD" if m["score"] >= float(c["mule_hold"]) else "REVIEW",
                         "reason_code": "MULE_ACCOUNT_SUSPECTED",
                         "message": f"Beneficiary {etx.iban} received money from {m['new_senders']} new customer(s) in 30 days"
                                    + (f", {m['fraud_senders']} of them confirmed fraud victims" if m.get("fraud_senders")
                                       else "") + (" and is watch-listed" if m.get("watched") else "")
                                    + f" (mule score {m['score']:.2f})"})
    if etx.is_outflow:
        ns = customer_score(db, tenant_id, space, etx.user_id)
        if ns is not None and ns.score >= float(c["network_review"]):
            hits.append({"id": "NETWORK-RISK", "name": "Linked to a risky network", "kind": "signal",
                         "action": "REVIEW", "reason_code": "LINKED_TO_FRAUD_NETWORK",
                         "message": f"Network risk {ns.score:.2f} ({ns.ring_id}, {ns.ring_size} linked customers): "
                                    + "; ".join(ns.reasons[:2])})
    return hits


def rings(db: Session, tenant_id: str, space: str, limit: int = 100) -> list[dict]:
    rows = db.execute(select(NetworkScore).where(NetworkScore.tenant_id == tenant_id, NetworkScore.space == space)
                      ).scalars().all()
    by: dict[str, list] = defaultdict(list)
    for r in rows:
        by[r.ring_id].append(r)
    names = {c.customer_ref: c.display_name for c in db.execute(select(Customer).where(
        Customer.tenant_id == tenant_id, Customer.space == space,
        Customer.customer_ref.in_([r.customer_ref for r in rows]))).scalars()} if rows else {}
    out = []
    for rid, members in by.items():
        members.sort(key=lambda r: -r.score)
        seeded = [m for m in members if m.hops_to_fraud == 0]
        out.append({"ring_id": rid, "size": len(members), "max_score": members[0].score,
                    "avg_score": round(sum(m.score for m in members) / len(members), 3), "seeded": len(seeded),
                    "computed_at": members[0].computed_at,
                    "members": [{"customer_ref": m.customer_ref, "name": names.get(m.customer_ref, ""),
                                 "score": m.score, "hops": m.hops_to_fraud, "reasons": m.reasons} for m in members]})
    out.sort(key=lambda r: (-r["max_score"], -r["size"]))
    return out[:limit]


def mule_accounts(db: Session, tenant_id: str, space: str, days: int = 30, limit: int = 50) -> list[dict]:
    since = utcnow() - timedelta(days=days)
    cands = db.execute(select(Transaction.beneficiary, func.count(func.distinct(Transaction.customer_ref))).where(
        Transaction.tenant_id == tenant_id, Transaction.space == space, Transaction.tx_type == "bank_transfer",
        Transaction.ts >= since, Transaction.beneficiary.is_not(None)).group_by(Transaction.beneficiary)
        .having(func.count(func.distinct(Transaction.customer_ref)) >= 3)).all()
    out = []
    for acc, _ in cands:
        m = mule_score(db, tenant_id, space, acc, days=days)
        if m["score"] > 0 and not m.get("hub"):
            out.append({"account": acc, **m})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


__all__ = ["compute", "run_all", "customer_score", "mule_score", "signals", "rings", "mule_accounts"]
