"""Demo Mode (BRD section 18): synthetic data in an isolated namespace.

Everything created here lives in ``space = 'DEMO'``: customers, transactions, decisions, alerts, cases,
models, connectors and reports. A reset deletes only DEMO rows/artifacts and never touches LIVE data.
Payment execution in Demo Mode is simulated; no external system is ever called.
"""
from __future__ import annotations

import logging
import random
import shutil
import threading
import time
from datetime import timedelta
from typing import Iterator, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from banksealer.preparation import TransactionPreparer
from mock_channel.generator import TransactionGenerator, make_iban

from .. import audit
from ..deps import Principal
from ..models import (Alert, ApiClient, Case, CaseEntityLink, CaseEvent, CaseNote, Connector, Customer,
                      CustomerSegment, DeadLetterEvent, Decision, EvidenceReference, ExecutionStatusEvent,
                      ModelVersion, ReportJob, Tenant, Transaction, User, utcnow)
from ..settings import settings
from . import cases as case_service
from . import engine_registry, segmentation
from .decision import engine_tx_fields, evaluate
from .engine_registry import model_dir

log = logging.getLogger("verishield.demo")

SCENARIOS = {
    "normal": "Normal activity for a customer",
    "new_beneficiary": "Unusual new beneficiary (session hijacking-like)",
    "velocity_burst": "Velocity burst to several new beneficiaries",
    "account_takeover": "Account takeover-like behaviour (foreign connection, large transfer)",
    "recharge_fraud": "Burst of phone recharges to unknown numbers",
    "prepaid_drain": "Prepaid card drain",
    "structuring": "Amounts just below the reporting threshold (structuring indicator)",
    "turnover": "Unusual customer turnover vs declared profile",
    "shared_counterparty": "Multi-account / shared counterparty pattern",
}
FIRST = ("Rami", "Lina", "Omar", "Dana", "Sami", "Hala", "Yousef", "Maya", "Karim", "Nour", "Tariq", "Reem", "Fadi",
         "Salma", "Ziad", "Aya", "Hani", "Rana", "Majd", "Leen")
LAST = ("Haddad", "Khoury", "Nasser", "Saleh", "Odeh", "Barakat", "Hamdan", "Darwish", "Awad", "Mansour", "Qasem",
        "Jaber", "Shami", "Halabi", "Yasin", "Rabah")
BUSINESS = ("Trading Co.", "Construction Ltd.", "Pharma Supplies", "Tech Services", "Import & Export", "Foods Ltd.",
            "Logistics", "Stone & Marble")
ACTIVITIES_IND = ("Salaried employee", "Self-employed", "Public sector employee", "Retired", "Student")
ACTIVITIES_BUS = ("Wholesale trade", "Construction", "Retail", "IT services", "Import/export", "Manufacturing")
CHANNELS = ("mobile_banking", "mobile_banking", "internet_banking", "internet_banking", "branch", "atm_kiosk")

_gens: dict[str, TransactionGenerator] = {}
_gen_lock = threading.Lock()


def _params(tenant: Tenant) -> dict:
    d = (tenant.settings or {}).get("demo", {})
    return {"seed": int(d.get("seed", 2026)), "customers": int(d.get("customers", settings.demo_customers)),
            "history_days": int(d.get("history_days", settings.demo_history_days))}


def generator(tenant: Tenant, fresh: bool = False) -> TransactionGenerator:
    with _gen_lock:
        if fresh or tenant.id not in _gens:
            p = _params(tenant)
            _gens[tenant.id] = TransactionGenerator(p["customers"], banks={tenant.id: "PS"}, seed=p["seed"],
                                                    fraud_ratio=0.04, currency=tenant.base_currency,
                                                    start=utcnow() - timedelta(days=2))
        return _gens[tenant.id]


def _persona(gen: TransactionGenerator, ref: str):
    return next((c for c in gen.customers if c.user_id == ref), None)


# --------------------------------------------------------------------------- seed
def _customer_row(tenant: Tenant, c, rng: random.Random) -> Customer:
    business = c.segment == "business"
    if business:
        name = f"{rng.choice(LAST)} {rng.choice(BUSINESS)}"
    else:
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
    mean_amount = 2.718281828 ** (c.amount_mu + c.amount_sigma ** 2 / 2)
    turnover = c.transfer_rate * 30 * mean_amount * rng.uniform(0.8, 1.5) + c.recharge_rate * 30 * 20
    products = ["current_account", "mobile_banking"]
    if c.prepaid_rate:
        products.append("prepaid_card")
    if business:
        products += ["business_account", "pos_acquiring"][: rng.randint(1, 2)]
    if c.segment == "affluent":
        products.append("savings_account")
    risk = rng.choices(["LOW", "MEDIUM", "HIGH"], weights=[78, 17, 5])[0]
    return Customer(tenant_id=tenant.id, space="DEMO", customer_ref=c.user_id, display_name=name,
                    customer_type="BUSINESS" if business else "INDIVIDUAL",
                    business_activity=rng.choice(ACTIVITIES_BUS if business else ACTIVITIES_IND),
                    risk_category=risk, kyc_review_status=rng.choice(["CURRENT"] * 6 + ["DUE_FOR_REVIEW"]),
                    country="PS", tenure_months=rng.randint(3, 180), products=products,
                    expected_monthly_turnover=round(turnover, -1),
                    expected_monthly_tx=round(c.daily_rate * 30, 1),
                    account_purpose="Business operations" if business else rng.choice(
                        ["Salary and personal expenses", "Family support", "Savings and bill payments"]),
                    attributes={"synthetic": True, "persona_segment": c.segment})


def seed(db: Session, tenant_id: str, actor: str = "system", with_cases: bool = True) -> dict:
    """Create the synthetic dataset, train demo models and generate recent decisions/alerts/cases."""
    started = time.time()
    tenant = db.get(Tenant, tenant_id)
    rng = random.Random(_params(tenant)["seed"])
    gen = generator(tenant, fresh=True)
    days = _params(tenant)["history_days"]

    for c in gen.customers:
        db.add(_customer_row(tenant, c, rng))
    # onboarding: customers with KYC but no history yet (cold start)
    for i in range(1, 9):
        business = i % 4 == 0
        db.add(Customer(tenant_id=tenant.id, space="DEMO", customer_ref=f"N{i:05d}",
                        display_name=f"{rng.choice(LAST)} {rng.choice(BUSINESS)}" if business else
                        f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                        customer_type="BUSINESS" if business else "INDIVIDUAL",
                        business_activity=rng.choice(ACTIVITIES_BUS if business else ACTIVITIES_IND),
                        risk_category="LOW" if i % 3 else "MEDIUM", kyc_review_status="CURRENT", country="PS",
                        tenure_months=0, products=["current_account", "mobile_banking"],
                        expected_monthly_turnover=float(rng.choice([1500, 3000, 6000, 25000 if business else 4000])),
                        expected_monthly_tx=float(rng.choice([8, 15, 25])), account_purpose="New relationship",
                        attributes={"synthetic": True, "onboarding": "new customer - no history"}))
    db.commit()

    prep = TransactionPreparer(default_bank=tenant.id)
    history = prep.prepare_many(gen.history(days, 0.0), "demo-history")
    for etx in history:
        db.add(Transaction(tenant_id=tenant.id, space="DEMO", tx_ref=etx.tx_id, mode="HISTORY",
                           source="Demo history import", channel=rng.choice(CHANNELS), execution_state="SETTLED",
                           **engine_tx_fields(etx)))
    db.commit()

    seg = segmentation.build_segmentation(db, tenant.id, "DEMO", actor)
    segmentation.activate_segmentation(db, seg.id, actor)
    model = engine_registry.train_fraud_model(db, tenant.id, "DEMO", actor, activate=True,
                                              notes="Synthetic demo history (Demo Mode namespace)")

    # two recent days of scored activity
    stop = utcnow()
    n_live = 0
    end_day = gen.cursor + timedelta(days=3)
    for record in gen.live(0.05):
        if gen.cursor >= end_day or str(record["timestamp"]) > stop.isoformat(sep=" ", timespec="seconds"):
            break
        post = rng.random() < 0.2
        record["channel"] = "core_batch" if post else rng.choice(CHANNELS)
        try:
            evaluate(db, tenant.id, "DEMO", record, mode="POST_EVENT" if post else "PRE_AUTH",
                     source="Core banking batch (demo)" if post else "Payment switch (demo)", caller="demo-seed")
            n_live += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("seed record skipped: %s", exc)
    for scenario in ("structuring", "shared_counterparty", "turnover", "account_takeover"):
        try:
            inject(db, tenant.id, scenario, actor="demo-seed", at=stop - timedelta(minutes=rng.randint(15, 150)))
        except Exception as exc:  # noqa: BLE001
            log.warning("scenario %s failed: %s", scenario, exc)
    _backdate(db, tenant.id)
    if with_cases:
        _sample_cases(db, tenant)
    channels = create_live_channels(db, tenant.id, actor) if settings.demo_live_channels else []
    audit.record(db, tenant.id, "demo.seed", actor=actor, resource_type="demo", space="DEMO",
                 after={"customers": len(gen.customers) + 8, "history": len(history), "scored": n_live,
                        "fraud_model": model.id, "segmentation": seg.id, "live_channels": channels,
                        "seconds": round(time.time() - started, 1)})
    if settings.background_jobs:
        from .connectors import start_enabled
        start_enabled(db)
    return {"customers": len(gen.customers) + 8, "history": len(history), "scored": n_live, "model": model.id,
            "segmentation": seg.id, "live_channels": channels}


INCOMING_COLUMNS = ["transaction_id", "customer_id", "timestamp", "transaction_type", "amount", "currency", "channel",
                    "beneficiary_reference", "beneficiary_country", "ip_address", "ip_country", "asn"]


def incoming_file() -> str:
    folder = settings.data_dir / "incoming"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "transactions.csv"
    if not path.exists():
        path.write_text(",".join(INCOMING_COLUMNS) + "\n", encoding="utf-8")
    return str(path)


def create_live_channels(db: Session, tenant_id: str, actor: str = "system") -> list[dict]:
    """Ready-to-use live channels for the Demo workspace; every record they receive is scored in real time."""
    from .connectors import add_log, test_connection

    bind = "0.0.0.0" if settings.host == "0.0.0.0" else "127.0.0.1"
    specs = [
        ("Live payment stream (synthetic payment switch)", "demo_stream", "PRE_AUTH",
         {"rate": settings.demo_stream_rate, "fraud_ratio": 0.04, "poll_seconds": 2}),
        ("TCP JSON-lines feed", "tcp_json", "POST_EVENT", {"host": bind, "port": settings.demo_tcp_port}),
        ("HTTP webhook listener", "http_listener", "POST_EVENT",
         {"host": bind, "port": settings.demo_http_port, "path": "/transactions"}),
        ("CSV drop file", "file_drop", "POST_EVENT", {"path": incoming_file(), "follow": True, "poll_seconds": 1}),
    ]
    out = []
    for name, ctype, mode, cfg in specs:
        c = Connector(tenant_id=tenant_id, space="DEMO", name=name, type=ctype, direction="INBOUND", environment="DEV",
                      ingest_mode=mode, config=cfg, auth_method="NONE" if ctype in ("demo_stream", "file_drop")
                      else "NETWORK_ALLOWLIST", owner=actor, created_by=None,
                      data_scope="Live channel of the Demo workspace (scored in real time)")
        db.add(c)
        db.flush()
        add_log(db, c, "Created automatically for Demo Mode", actor=actor)
        res = test_connection(db, c, actor)
        if res["ok"]:
            c.status = "ENABLED"
            add_log(db, c, "Enabled automatically - data received here is scored live", actor=actor)
        out.append({"name": name, "type": ctype, "status": c.status, "detail": res["message"]})
    db.commit()
    return out


def _backdate(db: Session, tenant_id: str) -> None:
    """Seeded decisions happened at the (synthetic) transaction time, so trends and SLAs look realistic."""
    rows = db.execute(select(Transaction).where(Transaction.tenant_id == tenant_id, Transaction.space == "DEMO",
                                                Transaction.mode != "HISTORY")).scalars().all()
    for tx in rows:
        ts = min(tx.ts, utcnow())
        tx.created_at = ts
        dec = db.get(Decision, tx.decision_id) if tx.decision_id else None
        if dec is not None:
            shift = ts - dec.issued_at
            dec.issued_at = ts
            if dec.expires_at:
                dec.expires_at = dec.expires_at + shift
        for a in db.execute(select(Alert).where(Alert.tenant_id == tenant_id, Alert.space == "DEMO",
                                                Alert.tx_ref == tx.tx_ref)).scalars():
            a.created_at = ts
        for ev in db.execute(select(ExecutionStatusEvent).where(ExecutionStatusEvent.tenant_id == tenant_id,
                                                                ExecutionStatusEvent.space == "DEMO",
                                                                ExecutionStatusEvent.tx_ref == tx.tx_ref)).scalars():
            ev.created_at = ts
    db.commit()


def _sample_cases(db: Session, tenant: Tenant) -> None:
    users = db.execute(select(User).where(User.tenant_id == tenant.id)).scalars().all()
    manager = next((u for u in users if u.role == "AML_MANAGER"), None)
    analysts = [u for u in users if u.role == "AML_EMPLOYEE"]
    if manager is None or not analysts:
        return
    mp = Principal(tenant_id=tenant.id, kind="user", role="AML_MANAGER", perms={"cases.view_all", "cases.dispose"},
                   space="DEMO", display=manager.full_name, user_id=manager.id)
    alerts = db.execute(select(Alert).where(Alert.tenant_id == tenant.id, Alert.space == "DEMO",
                                            Alert.status == "OPEN").order_by(Alert.score.desc())).scalars().all()
    by_customer: dict[str, list] = {}
    for a in alerts:
        by_customer.setdefault(a.customer_ref, []).append(a)
    groups = list(by_customer.values())
    if len(groups) < 4:
        return
    # 1) in progress with analyst 1
    c1 = case_service.create_case(db, mp, {"alert_ids": [a.id for a in groups[0][:3]], "priority": "HIGH",
                                           "assignee_id": analysts[0].id,
                                           "purpose": "Review unusual transfers and confirm customer intent."})
    ap1 = Principal(tenant_id=tenant.id, kind="user", role="AML_EMPLOYEE", perms=set(), space="DEMO",
                    display=analysts[0].full_name, user_id=analysts[0].id)
    case_service.add_note(db, ap1, c1, "Customer profile reviewed. Beneficiary not seen in 45 days of history; "
                                       "requesting call-back verification.", "FINDING")
    # 2) recommendation submitted by analyst 2
    a2 = analysts[1 % len(analysts)]
    c2 = case_service.create_case(db, mp, {"alert_ids": [a.id for a in groups[1][:2]], "priority": "MEDIUM",
                                           "assignee_id": a2.id})
    ap2 = Principal(tenant_id=tenant.id, kind="user", role="AML_EMPLOYEE", perms=set(), space="DEMO",
                    display=a2.full_name, user_id=a2.id)
    case_service.add_note(db, ap2, c2, "Activity consistent with declared business seasonality; invoices "
                                       "referenced by the relationship manager.", "SOURCE_OF_FUNDS")
    case_service.recommend(db, ap2, c2, "NO_CONCERN", "Transactions explained by documented supplier payments; "
                                                      "no indicators of layering.")
    # 3) closed with disposition
    c3 = case_service.create_case(db, mp, {"alert_ids": [a.id for a in groups[2][:1]], "priority": "LOW",
                                           "assignee_id": analysts[0].id})
    case_service.recommend(db, ap1, c3, "NO_CONCERN", "Known beneficiary confirmed by the customer by phone.")
    case_service.dispose(db, mp, c3, "CLOSE_FALSE_POSITIVE", "Analyst verification sufficient; model alert "
                                                             "driven by an unusual hour only.")
    # 4) a new customer-profile referral awaiting assignment
    case_service.create_case(db, mp, {"customer_refs": [groups[3][0].customer_ref], "referral": True,
                                      "purpose": "Customer-level review of activity versus declared turnover.",
                                      "questions": ["Is the 30-day turnover consistent with the declared profile?",
                                                    "Are counterparties shared with other customers?"]})


# --------------------------------------------------------------------------- scenarios & stream
def _restamp(records: list[dict], at) -> list[dict]:
    n = len(records)
    for i, r in enumerate(records):
        r["timestamp"] = (at - timedelta(minutes=4 * (n - 1 - i))).isoformat(sep=" ", timespec="seconds")
        r["tx_id"] = f"SCN-{random.getrandbits(32):08X}{i}"
    return records


def _scenario_records(gen: TransactionGenerator, scenario: str, persona, rng: random.Random, at) -> list[dict]:
    if scenario == "normal":
        recs = []
        for _ in range(10):
            recs = gen._legit_day(persona, at.replace(hour=0, minute=0, second=0))
            if recs:
                break
        return recs[:2] or gen._legit_day(gen.customers[0], at.replace(hour=0, minute=0, second=0))[:1]
    mapping = {"new_beneficiary": "transaction_hijacking", "velocity_burst": "salami_burst",
               "account_takeover": "information_stealing", "recharge_fraud": "recharge_fraud",
               "prepaid_drain": "prepaid_drain"}
    if scenario in mapping:
        recs = gen.make_fraud(persona, at, mapping[scenario])
        if scenario == "account_takeover":
            for r in recs:
                r["amount"] = max(r["amount"], 6000.0)
        return recs
    if scenario == "structuring":
        recs = []
        for _ in range(4):
            r = gen._base(persona, at, "bank_transfer", round(rng.uniform(8700, 9950), 2), gen._home_ip(persona),
                          "structuring")
            r["iban"] = rng.choice(persona.payees) if rng.random() < 0.5 else make_iban(rng, "PS")
            recs.append(r)
        return recs
    if scenario == "turnover":
        recs = []
        for _ in range(3):
            r = gen._base(persona, at, "bank_transfer", round(gen._median_amount(persona) * rng.uniform(12, 18), 2),
                          gen._home_ip(persona), "turnover")
            r["iban"] = rng.choice(persona.payees)
            recs.append(r)
        return recs
    if scenario == "shared_counterparty":
        mule = make_iban(rng, rng.choice(["PS", "JO", "AE"]))
        recs = []
        for other in rng.sample(gen.customers, 4):
            r = gen._base(other, at, "bank_transfer", round(rng.uniform(1500, 4800), 2), gen._home_ip(other),
                          "shared_counterparty")
            r["iban"] = mule
            recs.append(r)
        return recs
    raise ValueError(f"unknown scenario {scenario}")


def inject(db: Session, tenant_id: str, scenario: str, customer_ref: Optional[str] = None, actor: str = "user",
           at=None, mode: str = "PRE_AUTH") -> list[dict]:
    """Generate a scenario now and send it through the real decision pipeline (Demo space only)."""
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario must be one of {', '.join(SCENARIOS)}")
    tenant = db.get(Tenant, tenant_id)
    gen = generator(tenant)
    rng = random.Random()
    persona = _persona(gen, customer_ref) if customer_ref else None
    if persona is None:
        candidates = gen.customers
        if scenario == "turnover":
            candidates = [c for c in gen.customers if c.segment == "retail"] or gen.customers
        persona = rng.choice(candidates)
    at = at or utcnow()
    records = _restamp(_scenario_records(gen, scenario, persona, rng, at), at)
    out = []
    for r in records:
        r["channel"] = rng.choice(CHANNELS[:4])
        dec, tx, _ = evaluate(db, tenant_id, "DEMO", r, mode=mode, source=f"Demo scenario: {scenario}",
                              caller=actor)
        out.append({"transaction_id": dec.tx_ref, "customer_id": dec.customer_ref, "amount": tx.amount,
                    "decision": dec.decision, "fraud_score": dec.fraud_score, "aml": dec.aml_signal_level,
                    "reason_codes": dec.reason_codes})
    return out


def live_records(tenant_id: str, fraud_ratio: float = 0.04) -> Iterator[dict]:
    """Endless synthetic stream for the demo_stream connector (timestamps = now)."""
    from ..db import SessionLocal
    with SessionLocal() as db:
        tenant = db.get(Tenant, tenant_id)
        p = _params(tenant)
        currency = tenant.base_currency
    gen = TransactionGenerator(p["customers"], banks={tenant_id: "PS"}, seed=p["seed"], fraud_ratio=fraud_ratio,
                               currency=currency, start=utcnow())
    stream = gen.live(fraud_ratio)
    rng = random.Random()
    while True:
        record = next(stream)
        record["timestamp"] = utcnow().isoformat(sep=" ", timespec="seconds")
        record["tx_id"] = f"STR-{rng.getrandbits(36):09X}"
        record["channel"] = rng.choice(CHANNELS)
        yield record


# --------------------------------------------------------------------------- reset
def reset(db: Session, tenant_id: str, actor, reseed: bool = True) -> dict:
    """Delete every DEMO-space record and artifact of the tenant (LIVE data is never touched)."""
    from .connectors import runtime

    counts = {}
    for c in db.execute(select(Connector).where(Connector.tenant_id == tenant_id, Connector.space == "DEMO")).scalars():
        runtime.stop(c.id)
    case_ids = [c for (c,) in db.execute(select(Case.id).where(Case.tenant_id == tenant_id, Case.space == "DEMO"))]
    if case_ids:
        for model in (CaseEntityLink, CaseEvent, CaseNote, EvidenceReference):
            db.execute(delete(model).where(model.case_id.in_(case_ids)))
    for model in (Case, Alert, ExecutionStatusEvent, Decision, Transaction, Customer, CustomerSegment, ModelVersion,
                  DeadLetterEvent, ReportJob, Connector, ApiClient):
        res = db.execute(delete(model).where(model.tenant_id == tenant_id, model.space == "DEMO"))
        counts[model.__tablename__] = res.rowcount
    db.commit()
    engine_registry.reset_workspace(tenant_id, "DEMO")
    segmentation.clear(db, tenant_id, "DEMO")
    db.commit()
    shutil.rmtree(model_dir(tenant_id, "DEMO"), ignore_errors=True)
    for case_id in case_ids:
        shutil.rmtree(settings.evidence_dir / tenant_id / case_id, ignore_errors=True)
    shutil.rmtree(settings.reports_dir / tenant_id / "demo", ignore_errors=True)
    audit.record(db, tenant_id, "demo.reset", actor=actor, resource_type="demo", space="DEMO", after=counts)
    result = {"deleted": counts}
    if reseed:
        result["seed"] = seed(db, tenant_id, getattr(actor, "display", str(actor)))
    return result


def has_demo_data(db: Session, tenant_id: str) -> bool:
    return db.execute(select(Customer.id).where(Customer.tenant_id == tenant_id, Customer.space == "DEMO")
                      .limit(1)).scalar() is not None
