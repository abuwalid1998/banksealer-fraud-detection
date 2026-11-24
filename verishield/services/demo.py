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
from datetime import datetime, timedelta
from typing import Iterator, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from banksealer.preparation import TransactionPreparer
from mock_channel.generator import PS_CITIES, PS_COMPANIES, PS_FIRST, PS_LAST, TransactionGenerator, make_iban

from .. import audit
from ..deps import Principal
from ..models import (Alert, ApiClient, Case, CaseEntityLink, CaseEvent, CaseNote, Connector, Customer,
                      CustomerConfirmation, CustomerReview, CustomerSegment, DeadLetterEvent, Decision,
                      EvidenceReference, ExecutionStatusEvent, LoginEvent, ModelVersion, ReportJob, ReportSchedule,
                      ScreeningMatch, StrReport, Tenant, Transaction, User, WatchlistEntry, utcnow)
from ..settings import settings
from . import cases as case_service
from . import engine_registry, segmentation
from .decision import _enrich, engine_tx_fields, evaluate
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
    "card_testing": "Card testing: tiny online payments to unknown foreign merchants, then a large one",
    "atm_cashout": "Cloned card emptied at ATMs in other cities at night",
    "mule_passthrough": "Mule account: several unknown senders, money sent abroad within hours",
    "cash_structuring": "Several cash deposits just below 10,000 the same day",
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
            # the historical import ends yesterday 00:00 (local); the gateway simulator streams from there
            _gens[tenant.id] = TransactionGenerator(p["customers"], banks={tenant.id: "PS"}, seed=p["seed"],
                                                    fraud_ratio=0.04, currency=tenant.base_currency,
                                                    start=local_today(_tz(tenant)) - timedelta(days=1))
        return _gens[tenant.id]


def _persona(gen: TransactionGenerator, ref: str):
    return next((c for c in gen.customers if c.user_id == ref), None)


# --------------------------------------------------------------------------- seed
def _customer_row(tenant: Tenant, c, rng: random.Random) -> Customer:
    business = c.segment == "business"
    if c.name:
        name = c.name
    elif business:
        name = f"{rng.choice(LAST)} {rng.choice(BUSINESS)}"
    else:
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
    mean_amount = 2.718281828 ** (c.amount_mu + c.amount_sigma ** 2 / 2)
    turnover = c.transfer_rate * 30 * mean_amount * rng.uniform(0.8, 1.5) + c.recharge_rate * 30 * 20
    # card spending, cash and incoming money (salary, remittances, business receipts) are part of the turnover
    turnover += c.card_rate * 30 * 180 + c.cash_out_rate * 30 * 650 + c.salary + (c.remitter[2] if c.remitter else 0)
    if c.cash_in_rate:
        turnover += c.cash_in_rate * 30 * 2.718281828 ** (c.cash_in_mu + 0.125)
    if c.receipts_rate:
        turnover += c.receipts_rate * 30 * mean_amount
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
                    attributes={"synthetic": True, "persona_segment": c.segment, "city": c.city or "",
                                "home_branch": f"{c.city} branch" if c.city else "",
                                "phone": c.phones[0] if c.phones else "",
                                "national_id": f"4{int(c.user_id[1:]) * 7919 % 10 ** 8:08d}"})


def seed(db: Session, tenant_id: str, actor: str = "system", with_cases: bool = False) -> dict:
    """Prepare the Demo workspace: customer master data (KYC), a historical transaction import and real models.

    Nothing is pre-scored: no decisions, alerts or cases are created here. Scores appear only when the IT Admin
    starts a live channel (or a system posts to the decision API) and transactions flow through the engine.
    """
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
                        display_name=rng.choice(PS_COMPANIES) if business else
                        f"{rng.choice(PS_FIRST)} {rng.choice(PS_LAST)}",
                        customer_type="BUSINESS" if business else "INDIVIDUAL",
                        business_activity=rng.choice(ACTIVITIES_BUS if business else ACTIVITIES_IND),
                        risk_category="LOW" if i % 3 else "MEDIUM", kyc_review_status="CURRENT", country="PS",
                        tenure_months=0, products=["current_account", "mobile_banking"],
                        expected_monthly_turnover=float(rng.choice([1500, 3000, 6000, 25000 if business else 4000])),
                        expected_monthly_tx=float(rng.choice([8, 15, 25])), account_purpose="New relationship",
                        attributes={"synthetic": True, "onboarding": "new customer - no history",
                                    "city": rng.choice(PS_CITIES)}))
    db.commit()

    # historical import (settled transactions) used to train the models - exactly what a bank loads at go-live
    prep = TransactionPreparer(default_bank=tenant.id)
    n_hist = 0
    tz = _tz(tenant)
    for raw in gen.history(days, 0.0):
        raw["timestamp"] = local_to_utc(datetime.fromisoformat(str(raw["timestamp"])), tz).isoformat(
            sep=" ", timespec="seconds")
        etx = prep.prepare(raw, "demo-history")
        if etx is None:
            continue
        row = Transaction(tenant_id=tenant.id, space="DEMO", tx_ref=etx.tx_id, mode="HISTORY",
                          source="Historical import (core banking, last %d days)" % days,
                          channel=str(raw.get("channel") or rng.choice(CHANNELS)), execution_state="SETTLED",
                          **engine_tx_fields(etx))
        _enrich(row, raw)
        row.created_at = min(etx.timestamp, utcnow())
        db.add(row)
        n_hist += 1
        if n_hist % 2000 == 0:
            db.commit()
    db.commit()

    from . import operations, screening
    screening.seed_demo(db, tenant.id)
    screening.screen_customers(db, tenant.id, "DEMO")
    operations.schedule_reviews(db, tenant.id, "DEMO")

    seg = segmentation.build_segmentation(db, tenant.id, "DEMO", actor)
    segmentation.activate_segmentation(db, seg.id, actor)
    model = engine_registry.train_fraud_model(db, tenant.id, "DEMO", actor, activate=True,
                                              notes=f"Trained on the {days}-day historical import of the Demo "
                                                    "workspace")
    channels = create_live_channels(db, tenant.id, actor) if settings.demo_live_channels else []
    audit.record(db, tenant.id, "demo.seed", actor=actor, resource_type="demo", space="DEMO",
                 after={"customers": len(gen.customers) + 8, "history": n_hist, "fraud_model": model.id,
                        "segmentation": seg.id, "live_channels": channels,
                        "seconds": round(time.time() - started, 1)})
    if with_cases:
        _sample_cases(db, tenant)
    return {"customers": len(gen.customers) + 8, "history": n_hist, "scored": 0, "model": model.id,
            "segmentation": seg.id, "live_channels": channels,
            "next_step": "Sign in as IT Admin and start a channel in Integrations & Channels to stream transactions"}


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
    """Ready-to-use live channels for the Demo workspace (tested, not started).

    The IT Admin starts them from Integrations & Channels; every record they then receive is scored in real time.
    """
    from .connectors import add_log, test_connection

    bind = "0.0.0.0" if settings.host == "0.0.0.0" else "127.0.0.1"
    specs = [
        ("Payment gateway simulator (mobile, internet banking, branch, ATM)", "demo_stream", "PRE_AUTH",
         {"speed": settings.demo_stream_speed, "fraud_ratio": 0.03, "poll_seconds": 2}),
        ("TCP JSON-lines feed", "tcp_json", "POST_EVENT", {"host": bind, "port": settings.demo_tcp_port}),
        ("HTTP webhook listener", "http_listener", "POST_EVENT",
         {"host": bind, "port": settings.demo_http_port, "path": "/transactions"}),
        ("CSV drop file", "file_drop", "POST_EVENT", {"path": incoming_file(), "follow": True, "poll_seconds": 1}),
        ("Fraud engine results database", "db_results", "POST_EVENT",
         {"url": "sqlite:///" + (settings.data_dir / "fraud_results_demo.db").as_posix(), "table": "fraud_results",
          "batch": 500, "poll_seconds": 2}),
    ]
    out = []
    existing = {t for (t,) in db.execute(select(Connector.type).where(Connector.tenant_id == tenant_id,
                                                                      Connector.space == "DEMO"))}
    for name, ctype, mode, cfg in specs:
        if ctype in existing:
            continue
        c = Connector(tenant_id=tenant_id, space="DEMO", name=name, type=ctype, environment="DEV",
                      ingest_mode=mode, config=cfg,
                      auth_method="NONE" if ctype in ("demo_stream", "file_drop", "db_results")
                      else "NETWORK_ALLOWLIST", direction="OUTBOUND" if ctype == "db_results" else "INBOUND", owner=actor, created_by=None,
                      data_scope="Decision results of the Demo workspace" if ctype == "db_results" else
                      "Live channel of the Demo workspace (scored in real time)")
        db.add(c)
        db.flush()
        add_log(db, c, "Created automatically for Demo Mode", actor=actor)
        res = test_connection(db, c, actor)
        if res["ok"]:
            add_log(db, c, "Ready - an IT Admin starts the channel to stream data into the engine", actor=actor)
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
    if scenario in ("card_testing", "atm_cashout", "mule_passthrough", "cash_structuring"):
        return gen._ps_fraud(persona, at, scenario)
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
        mule = rng.choice(gen.mule_accounts) if rng.random() < 0.5 else make_iban(rng, rng.choice(["PS", "JO", "AE"]))
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
        r.setdefault("channel", rng.choice(CHANNELS[:4]))
        dec, tx, _ = evaluate(db, tenant_id, "DEMO", r, mode=mode, source=f"Demo scenario: {scenario}",
                              caller=actor)
        out.append({"transaction_id": dec.tx_ref, "customer_id": dec.customer_ref, "amount": tx.amount,
                    "decision": dec.decision, "fraud_score": dec.fraud_score, "aml": dec.aml_signal_level,
                    "reason_codes": dec.reason_codes})
    return out


def _tz(tenant: Tenant):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tenant.timezone or "UTC")
    except Exception:  # noqa: BLE001 - unknown zone / no tz database
        return None


def local_to_utc(ts: datetime, tz) -> datetime:
    """The simulation runs on the bank's local clock; the database stores UTC."""
    if tz is None:
        return ts
    from datetime import timezone as _tzmod
    return ts.replace(tzinfo=tz).astimezone(_tzmod.utc).replace(tzinfo=None)


def local_today(tz) -> datetime:
    from datetime import timezone as _tzmod
    now = datetime.now(_tzmod.utc)
    local = now.astimezone(tz) if tz is not None else now
    return local.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)


class GatewaySimulator:
    """Realistic payment-gateway feed for the Demo workspace.

    Each customer transacts on his own simulated schedule (working-hours peaks, quiet Fridays, salary week) and
    every record carries its *simulated* timestamp, so per-customer behaviour stays realistic. Records are
    released on the wall clock: from ``start`` (default: yesterday 00:00 local, where the historical import ends)
    the simulator first catches up at ``speed`` x real time, then continues in real time - timestamps are never in
    the future.
    """

    def __init__(self, tenant_id: str, fraud_ratio: float = 0.03, speed: float = 60.0,
                 start: Optional[datetime] = None, allow_future: bool = False):
        from ..db import SessionLocal
        with SessionLocal() as db:
            tenant = db.get(Tenant, tenant_id)
            p = _params(tenant)
            currency = tenant.base_currency
            self.tz = _tz(tenant)
        day0 = local_today(self.tz) - timedelta(days=1)
        start_utc = max(start or datetime.min, local_to_utc(day0, self.tz))
        self.gen = TransactionGenerator(p["customers"], banks={tenant_id: "PS"}, seed=p["seed"],
                                        fraud_ratio=fraud_ratio, currency=currency, start=day0)
        self.stream = self.gen.live(fraud_ratio)
        self.speed = max(1.0, float(speed))
        self.allow_future = allow_future
        self.sim_start = start_utc
        self.wall_start = utcnow()
        self.rng = random.Random()
        self.pending: Optional[dict] = None
        self.cursor = start_utc
        self._last_ts: Optional[datetime] = None
        self._last_release = self.wall_start
        self.queue: list[dict] = []

    def _logins_for(self, record: dict) -> list[dict]:
        """Digital-banking session events that precede a payment (account-takeover signals for fraud)."""
        if record.get("channel") not in ("mobile_banking", "internet_banking") or self.rng.random() > \
                (1.0 if record.get("scenario") in ("information_stealing", "salami_burst", "recharge_fraud") else 0.3):
            return []
        ts = datetime.fromisoformat(record["timestamp"])
        country = (record.get("cc_asn") or "PS-").split("-")[0]
        base = {"record_type": "login", "customer_id": record["user_id"], "channel": record["channel"],
                "device_id": record.get("device") or "", "ip_address": record.get("ip"), "ip_country": country}
        out = []
        if record.get("scenario") in ("information_stealing", "salami_burst"):
            for k in range(self.rng.randint(1, 3)):
                out.append({**base, "event": "LOGIN_FAILED", "success": False,
                            "timestamp": (ts - timedelta(minutes=12 - k)).isoformat(sep=" ", timespec="seconds")})
        out.append({**base, "event": "LOGIN", "success": True,
                    "timestamp": (ts - timedelta(minutes=self.rng.randint(1, 6))).isoformat(sep=" ", timespec="seconds")})
        return out

    def _next(self) -> dict:
        if self.queue:
            return self.queue.pop(0)
        while True:
            record = next(self.stream)
            ts = local_to_utc(datetime.fromisoformat(str(record["timestamp"])), self.tz)
            if ts < self.sim_start:
                continue                          # resume after the last streamed transaction
            record["timestamp"] = ts.isoformat(sep=" ", timespec="seconds")
            record["tx_id"] = f"GW-{self.rng.getrandbits(40):010X}"
            record.setdefault("channel", self.rng.choice(CHANNELS))
            record["gateway"] = "Payment gateway simulator"
            logins = self._logins_for(record)
            if logins:
                self.queue = logins[1:] + [record]
                return logins[0]
            return record

    MAX_GAP = timedelta(minutes=5)          # quiet periods (nights) are compressed during catch-up

    def release_at(self, ts: datetime) -> datetime:
        gap = min(ts - (self._last_ts or self.sim_start), self.MAX_GAP)
        accelerated = self._last_release + max(gap, timedelta(0)) / self.speed
        return max(accelerated, ts)

    def due(self, limit: int = 500) -> list[dict]:
        """Records whose release time has come."""
        out, now = [], utcnow()
        while len(out) < limit:
            rec = self.pending or self._next()
            ts = datetime.fromisoformat(rec["timestamp"])
            release = self.release_at(ts)
            if not self.allow_future and release > now:
                self.pending = rec
                break
            self.pending = None
            self.cursor = ts
            self._last_ts, self._last_release = ts, release
            out.append(rec)
        return out

    def take(self, n: int) -> list[dict]:
        """Next ``n`` records regardless of the clock (tests and bulk loading)."""
        out = []
        for _ in range(n):
            rec = self.pending or self._next()
            self.pending = None
            self.cursor = datetime.fromisoformat(rec["timestamp"])
            out.append(rec)
        return out


def live_records(tenant_id: str, fraud_ratio: float = 0.03) -> Iterator[dict]:
    """Endless realistic stream in simulated time from yesterday 00:00 (local)."""
    sim = GatewaySimulator(tenant_id, fraud_ratio, allow_future=True)
    while True:
        yield from sim.take(50)


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
                  DeadLetterEvent, ReportJob, Connector, ApiClient, ScreeningMatch, WatchlistEntry,
                  CustomerConfirmation, StrReport, LoginEvent, CustomerReview, ReportSchedule):
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


def pump(db: Session, tenant_id: str, n: int, fraud_ratio: float = 0.03, mode: str = "PRE_AUTH",
         source: str = "Payment gateway simulator", until_now: bool = False) -> int:
    """Push gateway-simulator transactions through the live decision pipeline (tests / CLI).

    ``until_now`` stops at the current time (realistic); otherwise ``n`` records are taken in simulated time
    from yesterday 00:00, which may run past the current time for large ``n``.
    """
    sim = GatewaySimulator(tenant_id, fraud_ratio, speed=1e9, allow_future=not until_now)
    records = sim.due(n) if until_now else sim.take(n)
    done = 0
    from .signals import record_login
    for record in records:
        if record.get("record_type") == "login":
            record_login(db, tenant_id, "DEMO", record)
            db.commit()
            continue
        try:
            evaluate(db, tenant_id, "DEMO", record, mode=mode, source=source, caller="gateway-simulator")
            done += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("pump record skipped: %s", exc)
    return done
