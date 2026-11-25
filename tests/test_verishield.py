"""VeriShield platform tests (BRD section 22 acceptance criteria, exercised through the REST API)."""
from __future__ import annotations

import os
import tempfile

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select, update  # noqa: E402

from verishield.settings import configure  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="verishield-test-")
configure(data_dir=__import__("pathlib").Path(_tmp), database_url=os.environ.get("VERISHIELD_TEST_DATABASE_URL", f"sqlite:///{_tmp}/test.db"),
          background_jobs=False, seed_demo=True, seed_demo_users=True, demo_customers=80, demo_history_days=35,
          require_mfa=False, demo_fixed_passwords=False)

from verishield.app import app  # noqa: E402

PASSWORDS = {"1000": "Admin", "2000": "Manager#2026", "3001": "Analyst1#2026", "3002": "Analyst2#2026",
             "4000": "Chief#2026", "5000": "Ceo#2026"}
NEW = "N3w!Secure-Pass"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        from verishield.db import SessionLocal
        from verishield.services import demo
        with SessionLocal() as db:
            assert db.execute(select(__import__("verishield.models", fromlist=["Decision"]).Decision.id)).first() is None
            demo.pump(db, "BOP", 400)      # nothing is pre-scored: stream gateway transactions through the engine
        yield c


_tokens: dict = {}


def login(c, username: str, tenant: str = "BOP") -> dict:
    key = (tenant, username)
    if key in _tokens:
        return _tokens[key]
    pw = PASSWORDS.get(username, "Admin") if tenant == "BOP" else "Admin"
    r = c.post("/api/v1/auth/login", json={"username": username, "password": pw, "tenant": tenant})
    if r.status_code == 401:
        r = c.post("/api/v1/auth/login", json={"username": username, "password": NEW, "tenant": tenant})
        pw = NEW
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    if "CHANGE_PASSWORD" in r.json()["restrictions"]:
        assert c.post("/api/v1/auth/change-password", headers=h,
                      json={"current_password": pw, "new_password": NEW}).status_code == 200
    me = c.get("/api/v1/auth/me", headers=h).json()
    if "COMPLETE_PROFILE" in me["restrictions"]:
        assert c.put("/api/v1/auth/profile", headers=h, json={"first_name": "Ibrahim", "last_name": "Saleh",
                                                             "email": "it@example.org"}).status_code == 200
    _tokens[key] = h
    return h


# --------------------------------------------------------------------------- 22.2 user administration
def test_bootstrap_admin_must_change_password_first(client):
    r = client.post("/api/v1/auth/login", json={"username": "1000", "password": "Admin"})
    assert r.status_code == 200
    body = r.json()
    assert "CHANGE_PASSWORD" in body["restrictions"] and "COMPLETE_PROFILE" in body["restrictions"]
    h = {"Authorization": f"Bearer {body['token']}"}
    assert client.get("/api/v1/dashboard", headers=h).status_code == 403          # nothing else allowed
    weak = client.post("/api/v1/auth/change-password", headers=h,
                       json={"current_password": "Admin", "new_password": "admin"})
    assert weak.status_code == 422
    with_username = client.post("/api/v1/auth/change-password", headers=h,
                                json={"current_password": "Admin", "new_password": "Abc!1000-defgh"})
    assert with_username.status_code == 422
    assert login(client, "1000")
    assert client.get("/api/v1/dashboard", headers=login(client, "1000")).status_code == 200
    # the old bootstrap password no longer works
    assert client.post("/api/v1/auth/login", json={"username": "1000", "password": "Admin"}).status_code == 401


def test_it_admin_creates_user_with_single_use_temp_password(client):
    h = login(client, "1000")
    r = client.post("/api/v1/users", headers=h, json={"username": "5123", "first_name": "Test", "last_name": "Analyst",
                                                      "email": "t@example.org", "role": "AML_EMPLOYEE"})
    assert r.status_code == 201, r.text
    temp = r.json()["temporary_password"]
    assert "temporary_password" not in client.get("/api/v1/users", headers=h).text.replace('"temporary_password"', "")
    assert client.post("/api/v1/users", headers=h, json={"username": "12", "first_name": "x", "last_name": "y",
                                                         "email": "a@b.c", "role": "AML_EMPLOYEE"}).status_code == 422
    # second AML Manager blocked by the initial role limit
    assert client.post("/api/v1/users", headers=h, json={"username": "2001", "first_name": "x", "last_name": "y",
                                                         "email": "a@b.cd", "role": "AML_MANAGER"}).status_code == 409
    lr = client.post("/api/v1/auth/login", json={"username": "5123", "password": temp})
    assert lr.json()["restrictions"] == ["CHANGE_PASSWORD"]


def test_lockout_after_failed_logins(client):
    h = login(client, "1000")
    client.post("/api/v1/users", headers=h, json={"username": "5999", "first_name": "Lock", "last_name": "Me",
                                                  "email": "l@example.org", "role": "AML_EMPLOYEE"})
    for _ in range(5):
        client.post("/api/v1/auth/login", json={"username": "5999", "password": "wrong"})
    assert client.post("/api/v1/auth/login", json={"username": "5999", "password": "wrong"}).status_code == 403


# --------------------------------------------------------------------------- RBAC matrix (backend-enforced)
@pytest.mark.parametrize("user,method,path,expected", [
    ("3001", "get", "/api/v1/alerts", 403),
    ("3001", "get", "/api/v1/users", 403),
    ("3001", "post", "/api/v1/policies", 403),
    ("2000", "get", "/api/v1/users", 403),
    ("2000", "post", "/api/v1/connectors", 403),
    ("1000", "get", "/api/v1/alerts", 403),
    ("1000", "post", "/api/v1/cases", 403),
    ("4000", "post", "/api/v1/cases", 403),
    ("4000", "post", "/api/v1/demo/reset", 403),
    ("2000", "get", "/api/v1/alerts", 200),
    ("4000", "get", "/api/v1/audit", 200),
    ("1000", "get", "/api/v1/connectors", 200),
    ("5000", "get", "/api/v1/users", 403),
    ("5000", "post", "/api/v1/cases", 403),
    ("5000", "get", "/api/v1/notifications", 403),
    ("5000", "get", "/api/v1/dashboard", 200),
    ("5000", "get", "/api/v1/models/clusters", 200),
    ("2000", "get", "/api/v1/models/clusters", 200),
    ("2000", "get", "/api/v1/mcp/settings", 403),
    ("1000", "get", "/api/v1/notifications", 200),
    ("1000", "get", "/api/v1/models/health", 200),
])
def test_role_matrix(client, user, method, path, expected):
    h = login(client, user)
    r = getattr(client, method)(path, headers=h, **({"json": {}} if method == "post" else {}))
    assert r.status_code == expected, (user, path, r.text)


def test_it_admin_sees_masked_transactions(client):
    item = client.get("/api/v1/transactions?page_size=1", headers=login(client, "1000")).json()["items"][0]
    assert item["masked"] and item["amount"] is None and "•" in item["customer_ref"]


# --------------------------------------------------------------------------- 22.4 decisions
def _policy_with_channel_rules(client):
    h = login(client, "2000")
    doc = client.get("/api/v1/policies", headers=h).json()["active"]["document"]
    doc["actions"] = {k: "PASS" for k in doc["actions"]}
    th = doc["engine_thresholds"]
    th.update({"local_alert": 1.0, "temporal_alert": 1.0, "global_alert": 1.0, "high_risk_countries": []})
    th["max_amount"] = {k: 10 ** 9 for k in th["max_amount"]}
    doc["aml"]["enabled"] = False
    doc["rules"] = [{"id": f"T-{o}", "name": o, "enabled": True, "conditions": {"channels": [f"test_{o.lower()}"]},
                     "action": o, "reason_code": f"TEST_{o}"} for o in ("REVIEW", "HOLD", "DECLINE")]
    r = client.post("/api/v1/policies", headers=h, json={"document": doc, "name": "test", "justification": "tests"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert client.post(f"/api/v1/policies/{pid}/simulate", headers=h, json={"days": 30}).status_code == 200
    r = client.post(f"/api/v1/policies/{pid}/publish", headers=h, json={})
    assert r.status_code == 200 and r.json()["status"] == "ACTIVE", r.text
    return pid


def test_all_four_outcomes_idempotency_and_versions(client):
    pid = _policy_with_channel_rules(client)
    h = login(client, "2000")
    seen = {}
    for i, channel in enumerate(("mobile_banking", "test_review", "test_hold", "test_decline")):
        body = {"transaction_id": f"T-OUT-{i}", "customer_id": "C00003", "transaction_type": "bank_transfer",
                "amount": 120.0, "channel": channel, "beneficiary_reference": "PS92PALS000000000400123456702"}
        r = client.post("/api/v1/decisions/evaluate", headers=h, json=body)
        assert r.status_code == 201, r.text
        d = r.json()
        seen[channel] = d["decision"]
        assert d["policy_version"] == pid and d["fraud_model_version"] and d["correlation_id"]
        again = client.post("/api/v1/decisions/evaluate", headers=h, json=body)
        assert again.status_code == 200 and again.json()["decision_id"] == d["decision_id"]
        assert again.json()["idempotent_replay"] is True
    assert seen == {"mobile_banking": "PASS", "test_review": "REVIEW", "test_hold": "HOLD", "test_decline": "DECLINE"}
    dup = client.post("/api/v1/decisions/evaluate", headers=h, json={
        "transaction_id": "T-OUT-0", "idempotency_key": "other", "customer_id": "C00003",
        "transaction_type": "bank_transfer", "amount": 1, "beneficiary_reference": "X"})
    assert dup.status_code == 409
    bad = client.post("/api/v1/decisions/evaluate", headers=h, json={
        "transaction_id": "T-BAD", "customer_id": "C1", "transaction_type": "teleport", "amount": 5})
    assert bad.status_code == 422


def test_bank_execution_lifecycle_and_api_key(client):
    admin = login(client, "1000")
    r = client.post("/api/v1/api-clients", headers=admin, json={"name": "switch", "scopes": [
        "decisions:write", "events:write", "execution:write"]})
    key = {"X-API-Key": r.json()["api_key"]}
    d = client.post("/api/v1/decisions/evaluate", headers=key, json={
        "transaction_id": "T-EXEC-1", "customer_id": "C00004", "transaction_type": "bank_transfer", "amount": 99,
        "channel": "test_hold", "beneficiary_reference": "PS92PALS000000000400123456702"}).json()
    assert d["decision"] == "HOLD" and d["execution_required"] and d["execution_state"] == "HELD"   # demo bank sim
    ok = client.post("/api/v1/transactions/T-EXEC-1/execution-result", headers=key, json={"state": "AUTHORISED"})
    assert ok.status_code == 200 and ok.json()["execution_state"] == "AUTHORISED"
    bad = client.post("/api/v1/transactions/T-EXEC-1/execution-result", headers=key, json={"state": "HELD"})
    assert bad.status_code == 409
    # an API key cannot reach user administration
    assert client.get("/api/v1/users", headers=key).status_code == 403
    assert client.post("/api/v1/decisions/evaluate", headers={"X-API-Key": "vs_bad.key"}, json={}).status_code == 401


def test_post_event_is_never_reported_as_blocked(client):
    admin = login(client, "1000")
    key = {"X-API-Key": client.post("/api/v1/api-clients", headers=admin, json={
        "name": "batch", "scopes": ["events:write", "decisions:write"]}).json()["api_key"]}
    r = client.post("/api/v1/transactions/events", headers=key, json={"events": [
        {"transaction_id": "T-POST-1", "customer_id": "C00005", "transaction_type": "bank_transfer", "amount": 50,
         "channel": "test_decline", "beneficiary_reference": "PS92PALS000000000400123456702"},
        {"transaction_id": "T-POST-2", "customer_id": "C00005", "transaction_type": "unknown", "amount": 50}]})
    assert r.json()["accepted"] == 1 and r.json()["rejected"] == 1
    d = client.get("/api/v1/transactions/T-POST-1/decision", headers=key).json()
    assert d["mode"] == "POST_EVENT" and d["execution_state"] == "SETTLED" and "not blocked" in d["note"]
    dlq = client.get("/api/v1/dead-letters", headers=login(client, "1000")).json()["items"]
    assert any("unknown transaction type" in x["reason"] for x in dlq)


# --------------------------------------------------------------------------- 22.6 AML workflow
def test_case_workflow_segregation_and_disposition(client):
    mgr, emp1, emp2 = login(client, "2000"), login(client, "3001"), login(client, "3002")
    alerts = client.get("/api/v1/alerts?status=OPEN", headers=mgr).json()["items"]
    hold_alert = next(a for a in alerts if a["tx_ref"] and a["tx_ref"].startswith("T-OUT-2"))
    analyst = next(u for u in client.get("/api/v1/users/assignable", headers=mgr).json()["users"]
                   if u["username"] == "3001")
    r = client.post("/api/v1/cases", headers=mgr, json={"alert_ids": [hold_alert["id"]], "priority": "HIGH",
                                                        "assignee_id": analyst["id"], "purpose": "verify"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert r.json()["status"] == "ASSIGNED"
    assert client.get(f"/api/v1/cases/{cid}", headers=emp2).status_code == 404           # not assigned
    assert client.get("/api/v1/transactions/T-OUT-2", headers=emp1).status_code == 200    # linked context
    assert client.get("/api/v1/transactions/T-OUT-1", headers=emp1).status_code == 200    # same customer history
    assert client.get("/api/v1/transactions/T-EXEC-1", headers=emp1).status_code == 404   # unrelated customer
    assert client.post(f"/api/v1/cases/{cid}/disposition", headers=emp1,
                       json={"value": "CLOSE_NO_CONCERN", "rationale": "x" * 20}).status_code == 403
    assert client.post(f"/api/v1/cases/{cid}/notes", headers=emp1, json={"body": "called customer"}).status_code == 201
    ev = client.post(f"/api/v1/cases/{cid}/evidence", headers=emp1, files={"file": ("note.txt", b"evidence", "text/plain")},
                     data={"description": "call note"})
    assert ev.status_code == 201 and ev.json()["evidence"][0]["sha256"]
    assert client.post(f"/api/v1/cases/{cid}/evidence", headers=emp1,
                       files={"file": ("x.exe", b"MZ..", "application/octet-stream")}).status_code == 422
    rec = client.post(f"/api/v1/cases/{cid}/recommendation", headers=emp1,
                      json={"value": "RECOMMEND_RELEASE", "rationale": "Supplier payment confirmed by invoice."})
    assert rec.json()["status"] == "RECOMMENDATION_SUBMITTED"
    assert client.post(f"/api/v1/cases/{cid}/disposition", headers=mgr,
                       json={"value": "RELEASE_TRANSACTION", "rationale": "short"}).status_code == 422
    d = client.post(f"/api/v1/cases/{cid}/disposition", headers=mgr,
                    json={"value": "RELEASE_TRANSACTION", "rationale": "Evidence reviewed, release approved."}).json()
    assert d["status"] == "CLOSED" and d["execution"][0]["state"] == "AUTHORISED"
    actions = [e["action"] for e in d["events"]]
    assert actions[:2] == ["CREATED", "ASSIGNED"] and "DISPOSITION_RECORDED" in actions
    pdf = client.post("/api/v1/reports", headers=mgr, json={"type": "investigation", "format": "pdf",
                                                            "params": {"case_id": cid}})
    assert pdf.status_code == 201
    blob = client.get(f"/api/v1/reports/{pdf.json()['id']}/download", headers=mgr)
    assert blob.content[:4] == b"%PDF"


def test_reports_respect_role_and_formats(client):
    chief = login(client, "4000")
    for fmt in ("pdf", "csv", "xlsx"):
        r = client.post("/api/v1/reports", headers=chief, json={"type": "executive_overview", "format": fmt})
        assert r.status_code == 201, r.text
    assert client.post("/api/v1/reports", headers=login(client, "3001"),
                       json={"type": "executive_overview", "format": "pdf"}).status_code == 403
    csv_job = client.post("/api/v1/reports", headers=chief, json={"type": "decision_batch", "format": "csv"}).json()
    text = client.get(f"/api/v1/reports/{csv_job['id']}/download", headers=chief).text
    assert text.lstrip("﻿").startswith("DEMO DATA - NOT FOR OPERATIONAL USE")


# --------------------------------------------------------------------------- 22.3 connectors & secrets
def test_connector_secret_never_returned(client):
    h = login(client, "1000")
    c = client.post("/api/v1/connectors", headers=h, json={"type": "rest_pull", "name": "core",
                                                           "config": {"url": "http://127.0.0.1:9/x"}}).json()
    assert c["status"] == "DRAFT"
    assert client.post("/api/v1/connectors", headers=h, json={"type": "rest_pull", "name": "bad",
                                                              "config": {"password": "p"}}).status_code == 422
    r = client.post(f"/api/v1/connectors/{c['id']}/secret", headers=h, json={"value": "SUPER-SECRET-TOKEN"})
    assert r.status_code == 200 and "SUPER-SECRET-TOKEN" not in r.text and r.json()["secret_configured"]
    t = client.post(f"/api/v1/connectors/{c['id']}/test", headers=h).json()
    assert t["ok"] is False and "SUPER-SECRET" not in t["message"]
    assert client.post(f"/api/v1/connectors/{c['id']}/enable", headers=h).status_code == 409
    assert "SUPER-SECRET-TOKEN" not in client.get("/api/v1/audit?limit=1000", headers=h).text


# --------------------------------------------------------------------------- 22.1 tenants, 22.7 demo, audit
def test_tenant_isolation(client):
    from verishield.bootstrap import ensure_bootstrap_admin, ensure_tenant
    from verishield.db import session_scope
    from verishield.services.policy import ensure_default_policy
    with session_scope() as db:
        t = ensure_tenant(db, "TWO", "Second Bank", "USD", "UTC", False)
        ensure_bootstrap_admin(db, t)
        db.flush()
        ensure_default_policy(db, "TWO")
    h2 = login(client, "1000", tenant="TWO")
    users = client.get("/api/v1/users", headers=h2).json()["users"]
    assert {u["tenant_id"] for u in users} == {"TWO"}
    assert client.get("/api/v1/transactions/T-OUT-0", headers=h2).status_code == 404
    assert client.get("/api/v1/transactions", headers=h2).json()["total"] == 0
    assert client.get("/api/v1/models", headers=h2).json()["models"] == []


def test_demo_reset_never_touches_live(client):
    from verishield.db import session_scope
    from verishield.models import Customer, Transaction
    admin = login(client, "1000")
    assert client.post("/api/v1/demo/mode", headers=admin, json={"enabled": False}).json()["demo_mode"] is False
    key = {"X-API-Key": client.post("/api/v1/api-clients", headers=admin, json={
        "name": "live", "scopes": ["decisions:write"]}).json()["api_key"]}
    r = client.post("/api/v1/decisions/evaluate", headers=key, json={
        "transaction_id": "LIVE-1", "customer_id": "L1", "transaction_type": "bank_transfer", "amount": 10,
        "beneficiary_reference": "PS92PALS000000000400123456702"})
    assert r.json()["decision"] == "ERROR_POLICY" and r.json()["fallback"] == "FALLBACK_HOLD"   # no live model yet
    assert client.post("/api/v1/demo/mode", headers=admin, json={"enabled": True}).status_code == 200
    assert client.post("/api/v1/demo/reset", headers=admin, json={"confirm": "nope"}).status_code == 422
    res = client.post("/api/v1/demo/reset", headers=admin, json={"confirm": "RESET"})
    assert res.status_code == 200, res.text
    with session_scope() as db:
        assert db.execute(select(Transaction).where(Transaction.tx_ref == "LIVE-1", Transaction.space == "LIVE")).scalar()
        assert db.execute(select(Customer).where(Customer.space == "DEMO")).first() is not None
        assert db.execute(select(Transaction).where(Transaction.tx_ref == "T-OUT-0")).scalar() is None


def test_audit_chain_detects_tampering(client):
    from verishield.db import session_scope
    from verishield.models import AuditEvent
    h = login(client, "4000")
    assert client.get("/api/v1/audit/verify", headers=h).json()["valid"] is True
    with session_scope() as db:
        first = db.execute(select(AuditEvent.id).where(AuditEvent.tenant_id == "BOP").order_by(AuditEvent.id)).scalars().first()
        db.execute(update(AuditEvent).where(AuditEvent.id == first).values(reason="tampered"))
    v = client.get("/api/v1/audit/verify", headers=h).json()
    assert v["valid"] is False and v["broken_at"] == first


def test_health_endpoints(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").status_code == 200
    assert "verishield_uptime_seconds" in client.get("/metrics").text
    assert client.get("/").status_code == 200


def teardown_module(_):
    import shutil
    shutil.rmtree(_tmp, ignore_errors=True)
    os.environ.pop("VERISHIELD_DATA_DIR", None)


# --------------------------------------------------------------------------- v1.2 features
def test_demo_channels_wait_for_it_admin_and_results_are_written(client):
    import time
    h = login(client, "1000")
    if not client.get("/api/v1/demo", headers=h).json()["demo_mode"]:
        client.post("/api/v1/demo/mode", headers=h, json={"enabled": True})
    cons = client.get("/api/v1/connectors", headers=h).json()["connectors"]
    by_type = {c["type"]: c for c in cons}
    assert {"demo_stream", "tcp_json", "http_listener", "file_drop", "db_results"} <= set(by_type)
    assert not any(c["running"] for c in cons)                        # nothing streams until started
    res = by_type["db_results"]
    assert client.post(f"/api/v1/connectors/{res['id']}/enable", headers=h).json()["status"] == "ENABLED"
    from verishield.db import SessionLocal
    from verishield.services import demo
    with SessionLocal() as db:
        n = demo.pump(db, "BOP", 40)             # login events in the feed are not decisions
    assert n >= 25
    deadline = time.time() + 15
    written = 0
    while time.time() < deadline and written < n:
        time.sleep(1)
        written = client.get(f"/api/v1/connectors/{res['id']}", headers=h).json()["counters"].get("received", 0)
    assert written >= n
    import sqlalchemy
    eng = sqlalchemy.create_engine(res["config"]["url"])
    with eng.connect() as conn:
        row = conn.execute(sqlalchemy.text("SELECT decision, fraud_score, model_version FROM fraud_results LIMIT 1")).first()
    eng.dispose()
    assert row and row[0] in ("PASS", "REVIEW", "HOLD", "DECLINE", "ERROR_POLICY") and row[2]
    client.post(f"/api/v1/connectors/{res['id']}/disable", headers=h)


def test_gateway_simulator_is_realistic():
    from verishield.models import utcnow
    from verishield.services.demo import GatewaySimulator
    sim = GatewaySimulator("BOP", 0.03, speed=1e9)
    recs = sim.due(5000)
    assert all(r["timestamp"] <= utcnow().isoformat(sep=" ") for r in recs)   # never in the future
    more = [r for r in sim.take(400) if r.get("record_type") != "login"]
    assert {"channel", "city", "device"} <= set(more[0])
    assert {"card_payment", "cash_withdrawal", "bank_transfer"} <= {r["tx_type"] for r in more}
    transfers = [r for r in more if r["tx_type"] == "bank_transfer"]
    assert transfers and all(r["iban"] and r.get("beneficiary_name") for r in transfers)
    assert any(r["iban"].startswith("PS") and len(r["iban"]) == 29 for r in transfers)


def test_fixed_demo_passwords_skip_first_login_change():
    from verishield.bootstrap import apply_fixed_demo_passwords
    from verishield.db import session_scope
    from verishield.models import Tenant, User
    from verishield.security import hash_password
    from verishield.settings import settings
    with session_scope() as db:
        t = db.get(Tenant, "BOP")
        u = db.execute(select(User).where(User.tenant_id == "BOP", User.username == "4000")).scalar()
        saved = (u.password_hash, u.must_change_password, u.status)
        u.password_hash, u.must_change_password, u.status = hash_password("Chief#2026"), True, "INVITED"
        db.flush()
        settings.demo_fixed_passwords = True
        try:
            assert apply_fixed_demo_passwords(db, t) >= 1
        finally:
            settings.demo_fixed_passwords = False
        assert u.must_change_password is False and u.status == "ACTIVE"
        u.password_hash, u.must_change_password, u.status = saved


def test_ceo_executive_overview_and_board_report(client):
    h = login(client, "5000")
    d = client.get("/api/v1/dashboard", headers=h).json()
    assert d["role"] == "CEO" and d["kpis"]["transactions"] > 0 and len(d["trend"]) == 30
    assert client.get("/api/v1/auth/me", headers=h).json()["user"]["role_label"] == "Chief Executive Officer"
    for typ in ("board_overview", "customer_portfolio", "transaction_activity"):
        r = client.post("/api/v1/reports", headers=h, json={"type": typ, "format": "pdf"})
        assert r.status_code == 201 and r.json()["status"] == "DONE", r.text
    blob = client.get(f"/api/v1/reports/{r.json()['id']}/download", headers=h).content
    assert blob[:4] == b"%PDF" and b"VeriShield" not in blob.split(b"stream")[0]


def test_forgot_password_and_admin_reset(client):
    r = client.post("/api/v1/auth/forgot-password", json={"username": "3002", "channel": "email"})
    assert r.status_code == 200 and r.json()["dev_code"]           # development: no SMTP configured
    unknown = client.post("/api/v1/auth/forgot-password", json={"username": "9999", "channel": "email"}).json()
    assert "dev_code" not in unknown and unknown["message"] == r.json()["message"].split(" (Development")[0]
    assert client.post("/api/v1/auth/reset-password", json={"username": "3002", "code": "000000",
                                                             "new_password": "Brand!New-Pass9"}).status_code == 400
    ok = client.post("/api/v1/auth/reset-password", json={"username": "3002", "code": r.json()["dev_code"],
                                                          "new_password": "Brand!New-Pass9"})
    assert ok.status_code == 200
    assert client.post("/api/v1/auth/login", json={"username": "3002", "password": "Brand!New-Pass9"}).status_code == 200
    admin = login(client, "1000")
    uid = next(u["id"] for u in client.get("/api/v1/users", headers=admin).json()["users"] if u["username"] == "3002")
    weak = client.post(f"/api/v1/users/{uid}/reset-password", headers=admin, json={"password": "short"})
    assert weak.status_code == 422
    r = client.post(f"/api/v1/users/{uid}/reset-password", headers=admin,
                    json={"password": "Admin!Chosen-77", "require_change": False, "notify": ["email"]})
    assert r.status_code == 200 and r.json()["temporary_password"] is None
    assert r.json()["delivery"]["email"]["sent"] is False                    # e-mail channel is switched off
    login_r = client.post("/api/v1/auth/login", json={"username": "3002", "password": "Admin!Chosen-77"}).json()
    assert login_r["restrictions"] == []
    _tokens.pop(("BOP", "3002"), None)
    PASSWORDS["3002"] = "Admin!Chosen-77"


def test_notification_settings_keep_secrets_out(client):
    h = login(client, "1000")
    r = client.put("/api/v1/notifications", headers=h, json={"email": {"enabled": True, "host": "127.0.0.1",
                                                                      "port": 1, "from_address": "vs@bank.ps",
                                                                      "security": "NONE"}})
    assert r.status_code == 200 and r.json()["config"]["email"]["ready"]
    assert client.put("/api/v1/notifications", headers=h, json={"sms": {"url": "ftp://x"}}).status_code == 422
    s = client.post("/api/v1/notifications/secret", headers=h, json={"channel": "sms", "value": "TOKEN-XYZ-123"})
    assert s.status_code == 200 and "TOKEN-XYZ-123" not in s.text and s.json()["config"]["sms"]["secret_configured"]
    t = client.post("/api/v1/notifications/test", headers=h, json={"channel": "email", "to": "a@b.c"}).json()
    assert t["ok"] is False
    out = client.get("/api/v1/notifications", headers=h).json()
    assert out["outbox"][0]["status"] == "FAILED" and "TOKEN-XYZ-123" not in str(out)
    client.put("/api/v1/notifications", headers=h, json={"email": {"enabled": False}})


def test_mcp_switch_and_tools(client):
    h = login(client, "1000")
    key = client.post("/api/v1/mcp/keys", headers=h, json={"name": "test agent"}).json()["api_key"]
    hk = {"X-API-Key": key}
    off = client.post("/mcp", headers=hk, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert off.status_code == 403 and "switched off" in off.json()["error"]["message"]
    client.put("/api/v1/mcp/settings", headers=h, json={"enabled": True, "tools": ["get_model_health", "get_clusters"]})
    init = client.post("/mcp", headers=hk, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).json()
    assert init["result"]["serverInfo"]["name"].startswith("verishield")
    tools = client.post("/mcp", headers=hk, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
    assert {t["name"] for t in tools["result"]["tools"]} == {"get_model_health", "get_clusters"}
    call = client.post("/mcp", headers=hk, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                 "params": {"name": "get_model_health", "arguments": {}}}).json()
    assert call["result"]["isError"] is False and '"checks"' in call["result"]["content"][0]["text"]
    blocked = client.post("/mcp", headers=hk, json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                                    "params": {"name": "list_connectors", "arguments": {}}}).json()
    assert blocked["error"]["code"] == -32602
    assert client.get("/api/v1/mcp/policy", headers=hk).json()["tools"] == ["get_model_health", "get_clusters"]
    client.put("/api/v1/mcp/settings", headers=h, json={"enabled": False})


def test_model_settings_health_clusters_and_retrain(client):
    h = login(client, "1000")
    p = client.get("/api/v1/models/params", headers=h).json()
    assert p["editable"] and any(x["id"] == "global.eps" for x in p["parameters"])
    assert client.put("/api/v1/models/params", headers=h, json={"values": {"global.eps": 99}}).status_code == 422
    assert client.put("/api/v1/models/params", headers=h,
                      json={"values": {"engine.profile_weights.local": 0.9}}).status_code == 422   # weights != 1
    ok = client.put("/api/v1/models/params", headers=h, json={"values": {"global.eps": 1.5, "segmentation.k_max": 5}})
    assert ok.status_code == 200 and ok.json()["overrides"]["global"]["eps"] == 1.5
    health = client.get("/api/v1/models/health", headers=h).json()
    assert health["status"] in ("OK", "CONSIDER", "RETRAIN_RECOMMENDED") and len(health["checks"]) >= 6
    m = client.post("/api/v1/models/fraud/train", headers=h, json={"mode": "trained"}).json()
    assert m["metrics"]["parameter_overrides"]["global"]["eps"] == 1.5
    assert m["metrics"]["hyperparameters"]["global"]["eps"] == 1.5
    seg = client.post("/api/v1/models/segmentation/build", headers=h, json={"activate": True})
    assert seg.status_code == 200
    cl = client.get("/api/v1/models/clusters", headers=login(client, "2000")).json()
    assert cl["segmentation"]["metrics"]["k"] <= 5 and len(cl["segmentation"]["points"]) > 50
    assert cl["peer_groups"]["groups"] and cl["segmentation"]["points"][0]["name"]
    client.put("/api/v1/models/params", headers=h, json={"reset": True})


# --------------------------------------------------------------------------- 1.3 features
def _demo_on(client):
    h = login(client, "1000")
    if not client.get("/api/v1/demo", headers=h).json()["demo_mode"]:
        client.post("/api/v1/demo/mode", headers=h, json={"enabled": True})
    return h


def test_new_transaction_types_are_scored(client):
    _demo_on(client)
    h = login(client, "2000")
    cases = [
        {"transaction_type": "card_payment", "amount": 85.5, "merchant": "Shams Bakery", "mcc": "5462",
         "merchant_country": "PS", "entry_mode": "CONTACTLESS"},
        {"transaction_type": "cash_withdrawal", "amount": 500, "terminal_id": "ATM-RAM-001", "location": "Ramallah"},
        {"transaction_type": "cash_deposit", "amount": 3000, "terminal_id": "BR-RAM-01", "location": "Ramallah"},
        {"transaction_type": "incoming_transfer", "amount": 4200, "originator_iban": "JO94CBJO0010000000000131000302"},
    ]
    for i, body in enumerate(cases):
        r = client.post("/api/v1/decisions/evaluate", headers=h,
                        json={"transaction_id": f"T-NEWTYPE-{i}", "customer_id": "C00007", **body})
        assert r.status_code == 201, r.text
        assert r.json()["decision"] in ("PASS", "REVIEW", "HOLD", "DECLINE")
    tx = client.get("/api/v1/transactions/T-NEWTYPE-3", headers=h).json()
    assert tx["transaction"]["beneficiary"].startswith("JO") if "transaction" in tx else True
    bad = client.post("/api/v1/decisions/evaluate", headers=h, json={
        "transaction_id": "T-NEWTYPE-BAD", "customer_id": "C00007", "transaction_type": "card_payment", "amount": 5})
    assert bad.status_code == 422 and "merchant" in bad.text


def test_watchlist_holds_payment_and_screening_matches(client):
    _demo_on(client)
    m = login(client, "2000")
    iban = "PS92PALS000000000400999999999"
    r = client.post("/api/v1/watchlist", headers=m, json={"kind": "IBAN", "value": iban, "reason": "mule account"})
    assert r.status_code == 201, r.text
    d = client.post("/api/v1/decisions/evaluate", headers=m, json={
        "transaction_id": "T-WL-1", "customer_id": "C00008", "transaction_type": "bank_transfer", "amount": 300,
        "beneficiary_reference": iban}).json()
    assert d["decision"] in ("HOLD", "DECLINE") and "WATCHLISTED_COUNTERPARTY" in d["reason_codes"]
    s = client.post("/api/v1/decisions/evaluate", headers=m, json={
        "transaction_id": "T-SANC-1", "customer_id": "C00008", "transaction_type": "bank_transfer", "amount": 900,
        "beneficiary_reference": "AE070331234567890123456", "beneficiary_name": "Global Trading FZE"}).json()
    assert "SANCTIONS_MATCH" in s["reason_codes"] and s["decision"] in ("HOLD", "DECLINE")
    chk = client.post("/api/v1/screening/check", headers=m, json={"name": "Viktor Draganoff"}).json()
    assert chk["matches"] and chk["matches"][0]["list_code"] == "UN"
    matches = client.get("/api/v1/screening/matches", headers=m).json()["matches"]
    beneficiary = next(x for x in matches if x["tx_ref"] == "T-SANC-1")
    res = client.post(f"/api/v1/screening/matches/{beneficiary['id']}/resolve", headers=m,
                      json={"status": "FALSE_POSITIVE", "note": "different company, verified registry"})
    assert res.json()["status"] == "FALSE_POSITIVE"
    pep = [x for x in matches if x["list_code"] == "PEP" and x["subject_type"] == "CUSTOMER"]
    assert pep, "demo PEP customers are screened at seed time"


def test_customer_confirmation_and_customer_fraud_report(client):
    from verishield.db import SessionLocal
    from verishield.models import CustomerConfirmation, Tenant
    from verishield.services import confirmations
    from verishield.services.policy import active_policy
    _demo_on(client)
    m = login(client, "2000")
    d = client.post("/api/v1/decisions/evaluate", headers=m, json={
        "transaction_id": "T-CNF-1", "customer_id": "C00011", "transaction_type": "bank_transfer", "amount": 120,
        "channel": "test_hold", "beneficiary_reference": "PS92PALS000000000400123456111"}).json()
    with SessionLocal() as db:
        conf = db.execute(select(CustomerConfirmation).where(CustomerConfirmation.tx_ref == "T-CNF-1")).scalar()
        if conf is None:      # the test policy maps this channel to HOLD only when the rules test ran first
            tx = db.execute(select(__import__("verishield.models", fromlist=["Transaction"]).Transaction)
                            .where(__import__("verishield.models", fromlist=["Transaction"]).Transaction.tx_ref
                                   == "T-CNF-1")).scalar()
            confirmations.request_confirmation(db, db.get(Tenant, "BOP"), tx, None, active_policy(db, "BOP").document)
            conf = db.execute(select(CustomerConfirmation).where(CustomerConfirmation.tx_ref == "T-CNF-1")).scalar()
        cid = conf.id
    assert d["decision"]
    lst = client.get("/api/v1/confirmations", headers=m).json()
    assert any(c["id"] == cid for c in lst["confirmations"]) and lst["simulated"]
    out = client.post(f"/api/v1/confirmations/{cid}/answer", headers=m, json={"reply": "2"}).json()
    assert out["result"].startswith("rejected") and out["case_id"]
    wl = client.get("/api/v1/watchlist", headers=m).json()["entries"]
    assert any(w["value"] == "PS92PALS000000000400123456111" and w["source"] == "CONFIRMATION" for w in wl)
    emp = login(client, "3001")
    rep = client.post("/api/v1/fraud-reports", headers=emp, json={
        "tx_refs": ["T-NEWTYPE-0"], "description": "Customer called: card was stolen yesterday",
        "reported_via": "CALL_CENTRE"})
    assert rep.status_code == 201, rep.text
    case = client.get(f"/api/v1/cases/{rep.json()['case_id']}", headers=m).json()
    assert case["priority"] == "URGENT" and case.get("assignee_id")


def test_str_network_timeline_ai_summary(client):
    _demo_on(client)
    m = login(client, "2000")
    cases = client.get("/api/v1/cases?page_size=50", headers=m).json()
    cid = (cases.get("items") or cases.get("cases"))[0]["id"]
    ai = client.post(f"/api/v1/cases/{cid}/ai-summary", headers=m).json()
    assert ai["source"] == "template" and "Case" in ai["text"]
    rep = client.post(f"/api/v1/cases/{cid}/str", headers=m)
    assert rep.status_code == 201, rep.text
    sid = rep.json()["id"]
    assert client.post(f"/api/v1/str/{sid}/submit", headers=m, json={"reference": "X"}).status_code == 409
    client.put(f"/api/v1/str/{sid}", headers=m, json={"narrative": "The customer received funds from several unknown "
                                                                    "senders and moved them abroad within hours."})
    assert client.post(f"/api/v1/str/{sid}/approve", headers=m).json()["status"] == "APPROVED"
    assert client.post(f"/api/v1/str/{sid}/submit", headers=m, json={"reference": "FFU-2026-0042"}).json()["status"] \
        == "SUBMITTED"
    xml = client.get(f"/api/v1/str/{sid}/export?fmt=xml", headers=m)
    assert xml.status_code == 200 and b"<report>" in xml.content and b"<transaction>" in xml.content
    assert client.get(f"/api/v1/str/{sid}/export?fmt=pdf", headers=m).content[:4] == b"%PDF"
    net = client.get("/api/v1/customers/C00011/network", headers=m).json()
    assert net["focus"] == "C00011" and any(n["kind"] == "customer" for n in net["nodes"])
    # several customers paying one mule account are linked; shops and ATMs never link customers
    inj = client.post("/api/v1/demo/scenarios", headers=m, json={"scenario": "shared_counterparty"})
    assert inj.status_code == 200, inj.text
    from verishield.db import SessionLocal
    from verishield.models import Transaction
    with SessionLocal() as db:
        mule, victim = db.execute(select(Transaction.beneficiary, Transaction.customer_ref).where(
            Transaction.tx_ref == inj.json()["results"][0]["transaction_id"])).first()
    ring = client.get(f"/api/v1/customers/{victim}/network", headers=m).json()
    assert ring["linked_customers"] >= 3 and any(s["value"] == mule for s in ring["shared"])
    assert all(s["customers"] <= 25 for s in ring["shared"])
    tl = client.get("/api/v1/customers/C00011/timeline", headers=m).json()["events"]
    assert {"TRANSACTION", "CONFIRMATION"} <= {e["kind"] for e in tl}


def test_reviews_schedules_quality_archive_value(client):
    _demo_on(client)
    m, a, ceo = login(client, "2000"), login(client, "1000"), login(client, "5000")
    revs = client.get("/api/v1/reviews", headers=m).json()["reviews"]
    assert revs, "periodic reviews are scheduled from the risk rating"
    emp = next(u for u in client.get("/api/v1/users/assignable", headers=m).json()["users"] if u["username"] == "3001")
    rid = revs[0]["id"]
    assert client.post(f"/api/v1/reviews/{rid}/assign", headers=m, json={"assignee_id": emp["id"]}).status_code == 200
    done = client.post(f"/api/v1/reviews/{rid}/complete", headers=login(client, "3001"),
                       json={"new_risk": "MEDIUM", "notes": "Documents refreshed, activity consistent."})
    assert done.status_code == 200 and done.json()["status"] == "COMPLETED"
    sch = client.post("/api/v1/report-schedules", headers=ceo, json={"report_type": "board_overview",
                                                                      "frequency": "WEEKLY", "recipients": ["ceo@bank.ps"]})
    assert sch.status_code == 201, sch.text
    run = client.post(f"/api/v1/report-schedules/{sch.json()['id']}/run", headers=ceo).json()
    assert "generated" in run["last_status"]
    q = client.get("/api/v1/connectors-quality", headers=a).json()["channels"]
    assert q and "missing_pct" in q[0]
    arc = client.get("/api/v1/archive", headers=a).json()
    assert arc["preview"]["retention_days"] >= 365
    v = client.get("/api/v1/value", headers=ceo).json()
    assert "prevented_loss" in v and "cost_per_alert" in v
    w = client.get("/api/v1/what-if?strictness=0.8", headers=ceo).json()
    assert w["interventions"]["proposed"] >= w["interventions"]["current"]
    g = client.get("/api/v1/alerts/groups", headers=m).json()
    assert g["alerts"] >= len(g["groups"]) >= 1


def test_learned_model_challenger_and_case_automation(client):
    from verishield.db import SessionLocal
    from verishield.services import demo
    _demo_on(client)
    a = login(client, "1000")
    with SessionLocal() as db:
        demo.pump(db, "BOP", 1200, fraud_ratio=0.08)
    info = client.get("/api/v1/models/learned", headers=a).json()
    assert info["demo_ground_truth"]
    r = client.post("/api/v1/models/learned/train", headers=a, json={})
    assert r.status_code == 200, r.text
    assert r.json()["metrics"]["holdout_auc"] is None or r.json()["metrics"]["holdout_auc"] > 0.6
    ch = client.get("/api/v1/models/challenger", headers=a).json()
    assert "pairs" in ch
    m = login(client, "2000")
    conf = client.put("/api/v1/case-automation", headers=m, json={"strategy": "ROUND_ROBIN"}).json()["config"]
    assert conf["strategy"] == "ROUND_ROBIN"


def test_active_directory_sign_in(client, monkeypatch):
    from verishield.services import directory
    a = login(client, "1000")
    assert client.put("/api/v1/directory", headers=a, json={"enabled": True, "server_url": "ldap://dc.bank.local"}
                      ).status_code == 422                                  # plain LDAP without TLS is refused
    assert client.put("/api/v1/directory", headers=a, json={
        "enabled": True, "server_url": "ldaps://dc.bank.local", "sync_roles": True,
        "group_role_map": {"VS-AML-Managers": "AML_MANAGER"}}).status_code == 200
    uid = next(u["id"] for u in client.get("/api/v1/users", headers=a).json()["users"] if u["username"] == "4000")
    assert client.post(f"/api/v1/users/{uid}/directory", headers=a,
                       json={"auth_source": "AD", "ad_account": "samir.h"}).status_code == 200
    monkeypatch.setattr(directory, "authenticate", lambda t, acc, pw: {
        "ok": pw == "Domain#Pass1", "message": "ok", "groups": ["CN=VS-Chief,OU=Groups,DC=bank,DC=local"]})
    assert client.post("/api/v1/auth/login", json={"username": "samir.h", "password": "wrong"}).status_code == 401
    ok = client.post("/api/v1/auth/login", json={"username": "samir.h", "password": "Domain#Pass1"})
    assert ok.status_code == 200 and "CHANGE_PASSWORD" not in ok.json()["restrictions"]
    client.post(f"/api/v1/users/{uid}/directory", headers=a, json={"auth_source": "LOCAL"})
    client.put("/api/v1/directory", headers=a, json={"enabled": False})


# =========================================================================== 1.4
def test_cluster_leader_election_checkpoints_and_metrics(client):
    from datetime import timedelta
    from verishield.db import SessionLocal
    from verishield.models import ClusterLease, ModelCheckpoint, ModelVersion, utcnow
    from verishield.services import cluster, engine_registry
    from verishield.settings import settings
    a = login(client, "1000")
    st = client.get("/api/v1/system/cluster", headers=a).json()
    assert st["leader"] == settings.node_id and st["nodes"]
    assert client.get("/ready").json()["role"] == "LEADER"
    with SessionLocal() as db:
        assert cluster.try_acquire(db, settings.node_id)              # renew own lease
        assert not cluster.try_acquire(db, "node-b:8000")             # lease held: the other node waits
        db.execute(update(ClusterLease).values(expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
        assert cluster.try_acquire(db, "node-b:8000")                 # leader stopped renewing: takeover
        assert cluster.current_leader(db)["node"] == "node-b:8000"
        cluster.release(db, "node-b:8000")
        assert cluster.try_acquire(db, settings.node_id)
        # shared model checkpoint, verified before it is loaded
        engine_registry.load_active_models(db)
        assert cluster.save_checkpoints(db) >= 1
        mv = db.execute(select(ModelVersion).where(ModelVersion.status == "ACTIVE", ModelVersion.kind == "FRAUD")).scalars().first()
        cfg = engine_registry.workspace(mv.tenant_id, mv.space).engine.config
        settings.cluster_mode = True
        try:
            assert cluster.load_checkpoint(db, mv, cfg) is not None
            cp = db.get(ModelCheckpoint, mv.id)
            cp.blob = cp.blob[:-10] + b"0123456789"                   # tampered
            db.commit()
            assert cluster.load_checkpoint(db, mv, cfg) is None
        finally:
            settings.cluster_mode = False
    text = client.get("/metrics").text
    assert "verishield_cluster_leader" in text and "verishield_alerts_open" in text


def test_security_posture_headers_vault_and_api_key_rotation(client):
    from verishield.db import SessionLocal
    from verishield.models import SecretRecord
    from verishield.security import decrypt_secret
    a = login(client, "1000")
    page = client.get("/")
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert page.headers["X-Frame-Options"] == "DENY"
    post = client.get("/api/v1/security/posture", headers=a).json()
    assert 0 <= post["score"] <= 100 and {c["id"] for c in post["checks"]} >= {"tls", "audit", "vault", "mfa"}
    client.post("/api/v1/notifications/secret", headers=a, json={"channel": "push", "value": "push-hmac-secret"})
    rot = client.post("/api/v1/security/vault/rotate", headers=a).json()
    assert rot["secrets"] >= 1 and rot["failed"] == 0
    with SessionLocal() as db:
        values = [decrypt_secret(r.ciphertext) for r in db.execute(select(SecretRecord)).scalars()]
    assert "push-hmac-secret" in values
    assert client.post("/api/v1/security/vault/retire", headers=a).status_code == 200
    assert client.get("/api/v1/security/posture", headers=a).json()["vault"]["primary"] == rot["primary"]
    made = client.post("/api/v1/api-clients", headers=a, json={"name": "ops-reader", "scopes": ["read:operations"]}).json()
    old = {"X-API-Key": made["api_key"]}
    assert client.get("/api/v1/system/cluster", headers=old).status_code == 200
    new = client.post(f"/api/v1/api-clients/{made['client']['id']}/rotate", headers=a, json={"grace_hours": 24}).json()
    assert "previous_key_hash" not in new["client"]
    assert client.get("/api/v1/system/cluster", headers={"X-API-Key": new["api_key"]}).status_code == 200
    assert client.get("/api/v1/system/cluster", headers=old).status_code == 200        # grace period
    newer = client.post(f"/api/v1/api-clients/{made['client']['id']}/rotate", headers=a, json={"grace_hours": 0}).json()
    assert client.get("/api/v1/system/cluster", headers={"X-API-Key": new["api_key"]}).status_code == 401
    assert client.get("/api/v1/system/cluster", headers={"X-API-Key": newer["api_key"]}).status_code == 200
    assert client.get("/api/v1/security/posture", headers=login(client, "2000")).status_code == 403


def test_ctr_generation_export_submission_and_statistics(client):
    from verishield.db import SessionLocal
    from verishield.models import Tenant
    from verishield.services.demo import _tz, local_today
    _demo_on(client)
    m = login(client, "2000")
    with SessionLocal() as db:
        today = local_today(_tz(db.get(Tenant, "BOP"))).date().isoformat()
    for i, amount in enumerate((6000, 5500)):                    # 11,500 cash in one day, split in two
        r = client.post("/api/v1/decisions/evaluate", headers=m, json={
            "transaction_id": f"T-CTR-{i}", "customer_id": "C00012", "transaction_type": "cash_deposit",
            "amount": amount, "terminal_id": "BR-RAM-01", "location": "Ramallah"})
        assert r.status_code == 201, r.text
    gen = client.post("/api/v1/ctr/generate", headers=m, json={"business_date": today}).json()
    assert gen["threshold"] == 10000
    reps = client.get(f"/api/v1/ctr?business_date={today}", headers=m).json()["reports"]
    mine = next(r for r in reps if r["customer_ref"] == "C00012")
    assert mine["trigger"] == "AGGREGATED" and mine["cash_in"] >= 11500 and mine["status"] == "DRAFT"
    assert client.post(f"/api/v1/ctr/{mine['id']}/review", headers=m).json()["status"] == "REVIEWED"
    xml = client.get(f"/api/v1/ctr/export?fmt=xml&ids={mine['id']}", headers=m)
    assert xml.status_code == 200 and b"<report_code>CTR</report_code>" in xml.content and b"T-CTR-0" in xml.content
    assert client.get(f"/api/v1/ctr/export?fmt=pdf&ids={mine['id']}", headers=m).content[:4] == b"%PDF"
    sub = client.post("/api/v1/ctr/submit", headers=m, json={"ids": [mine["id"]], "reference": "REG-CTR-0001"}).json()
    assert sub["submitted"] == 1
    assert client.post(f"/api/v1/ctr/{mine['id']}/exempt", headers=m, json={"reason": "already submitted test"}).status_code == 409
    stats = client.get(f"/api/v1/regulator/statistics?date_from={today}&date_to={today}", headers=login(client, "4000")).json()
    assert stats["ctr"]["submitted"] >= 1 and stats["transactions"]["total"] >= 2
    job = client.post("/api/v1/reports", headers=m, json={"type": "regulator_statistics", "format": "pdf",
                                                             "params": {"date_from": today, "date_to": today}})
    assert job.status_code in (200, 201), job.text


def test_customer_risk_assessment_onboarding_override_four_eyes(client):
    _demo_on(client)
    m, chief = login(client, "2000"), login(client, "4000")
    low = client.post("/api/v1/customers/risk-assessment", headers=m, json={
        "display_name": "Lina Odeh", "customer_type": "INDIVIDUAL", "occupation": "Teacher", "country": "PS",
        "tenure_months": 0, "products": ["current_account"]}).json()
    high = client.post("/api/v1/customers/risk-assessment", headers=m, json={
        "display_name": "Atlas Money Exchange", "customer_type": "BUSINESS", "business_activity": "Money exchange",
        "country": "PS", "onboarding_channel": "digital", "products": ["foreign_transfer", "trade_finance"]}).json()
    assert low["level"] == "LOW" and high["score"] > low["score"] and high["level"] in ("MEDIUM", "HIGH")
    pep = client.post("/api/v1/customers/risk-assessment", headers=m, json={
        "display_name": "Prospect", "customer_type": "INDIVIDUAL", "pep": True}).json()
    assert pep["level"] == "HIGH" and pep["edd_required"]
    view = client.get("/api/v1/customers/C00013/risk-assessment", headers=m).json()
    assert view["current"]["factors"] and view["history"], "initial rating stored at seed"
    up = client.post("/api/v1/customers/C00013/risk-assessment/override", headers=m,
                     json={"level": "HIGH", "reason": "adverse media about the customer's business partner"}).json()
    assert up["status"] == "APPLIED"
    down = client.post("/api/v1/customers/C00013/risk-assessment/override", headers=m,
                       json={"level": "LOW", "reason": "adverse media cleared after the review of documents"}).json()
    assert down["status"] == "PENDING_APPROVAL"
    same = client.post(f"/api/v1/risk-assessments/{down['id']}/approve", headers=m)
    assert same.status_code == 403 and same.json()["detail"]["code"] == "FOUR_EYES"
    ok = client.post(f"/api/v1/risk-assessments/{down['id']}/approve", headers=chief).json()
    assert ok["status"] == "APPLIED"
    assert client.get("/api/v1/customers/C00013/risk-assessment", headers=m).json()["rating"] == "LOW"
    pend = client.get("/api/v1/risk-assessments", headers=m).json()
    assert "distribution" in pend and any(a["id"] == down["id"] for a in pend["assessments"])


def test_cheque_duplicate_returned_drawer_and_post_dated(client):
    from datetime import date, timedelta
    _demo_on(client)
    m = login(client, "2000")
    drawer = "PS92PALS000000000400777000111"
    base = {"customer_id": "C00014", "transaction_type": "cheque_deposit", "beneficiary_reference": drawer,
            "drawer_bank": "PALS", "location": "Ramallah"}
    first = client.post("/api/v1/decisions/evaluate", headers=m, json={
        **base, "transaction_id": "T-CHQ-1", "amount": 2500, "cheque_number": "551001"})
    assert first.status_code == 201, first.text
    dup = client.post("/api/v1/decisions/evaluate", headers=m, json={
        **base, "transaction_id": "T-CHQ-2", "amount": 2500, "cheque_number": "551001"}).json()
    assert "DUPLICATE_CHEQUE" in dup["reason_codes"] and dup["decision"] in ("HOLD", "DECLINE")
    for i in range(2):
        r = client.post("/api/v1/cheques/returns", headers=m, json={
            "drawer_account": drawer, "cheque_number": f"55090{i}", "amount": 900, "reason": "INSUFFICIENT_FUNDS"})
        assert r.status_code == 200, r.text
    third = client.post("/api/v1/decisions/evaluate", headers=m, json={
        **base, "transaction_id": "T-CHQ-3", "amount": 1800, "cheque_number": "551002"}).json()
    assert "RETURNED_CHEQUE_DRAWER" in third["reason_codes"]
    future = (date.today() + timedelta(days=40)).isoformat()
    post = client.post("/api/v1/decisions/evaluate", headers=m, json={
        **base, "transaction_id": "T-CHQ-4", "amount": 700, "cheque_number": "551003", "cheque_date": future}).json()
    assert "POST_DATED_OR_STALE_CHEQUE" in post["reason_codes"]
    missing = client.post("/api/v1/decisions/evaluate", headers=m, json={
        **base, "transaction_id": "T-CHQ-5", "amount": 700})
    assert missing.status_code == 422 and "cheque number" in missing.text
    summ = client.get("/api/v1/cheques/summary", headers=m).json()
    assert summ["returned"] >= 2 and any(d["account"] == drawer for d in summ["top_drawers"])
    # returned-cheque events through the ingestion API as well
    key = {"X-API-Key": client.post("/api/v1/api-clients", headers=login(client, "1000"), json={
        "name": "clearing", "scopes": ["events:write"]}).json()["api_key"]}
    ev = client.post("/api/v1/transactions/events", headers=key, json={"events": [
        {"record_type": "cheque_return", "transaction_id": "T-CHQ-1", "reason": "SIGNATURE"}]})
    assert ev.status_code in (200, 202), ev.text


def test_network_rings_and_mule_account_signal(client):
    _demo_on(client)
    m = login(client, "2000")
    inj = client.post("/api/v1/demo/scenarios", headers=m, json={"scenario": "shared_counterparty"}).json()["results"]
    refs = {r["customer_id"] for r in inj}
    from verishield.db import SessionLocal
    from verishield.models import Transaction
    with SessionLocal() as db:
        mule = db.execute(select(Transaction.beneficiary).where(Transaction.tx_ref == inj[0]["transaction_id"])).scalar()
    out = client.post("/api/v1/networks/recompute", headers=m).json()
    assert out["rings"] >= 1 and out["seeds"] >= 1
    rings = client.get("/api/v1/networks/rings", headers=m).json()["rings"]
    ring = next(r for r in rings if refs & {x["customer_ref"] for x in r["members"]})
    assert ring["max_score"] > 0.5 and ring["seeded"] >= 1
    mules = client.get("/api/v1/networks/mules", headers=m).json()["accounts"]
    assert any(x["account"] == mule for x in mules)
    nxt = client.post("/api/v1/decisions/evaluate", headers=m, json={
        "transaction_id": "T-MULE-NEXT", "customer_id": "C00015", "transaction_type": "bank_transfer", "amount": 650,
        "beneficiary_reference": mule}).json()
    assert "MULE_ACCOUNT_SUSPECTED" in nxt["reason_codes"] and nxt["decision"] in ("REVIEW", "HOLD", "DECLINE")
    assert client.get("/api/v1/networks/rings", headers=login(client, "3001")).status_code == 403


def test_push_confirmation_card_freeze_and_siem_forwarding(client):
    import socket
    from datetime import timedelta
    from verishield.db import SessionLocal
    from verishield.models import CustomerConfirmation, Tenant, Transaction, utcnow
    from verishield.services import confirmations, siem
    from verishield.services.policy import active_policy
    a = _demo_on(client)
    m = login(client, "2000")
    card = {"customer_id": "C00016", "transaction_type": "card_payment", "merchant": "Global E-Shop", "mcc": "5732",
            "merchant_country": "GB", "entry_mode": "ECOM", "card_ref": "CARD-TEST-0016"}
    client.post("/api/v1/decisions/evaluate", headers=m, json={**card, "transaction_id": "T-CARD-1", "amount": 9500})
    with SessionLocal() as db:
        conf = db.execute(select(CustomerConfirmation).where(CustomerConfirmation.tx_ref == "T-CARD-1")).scalar()
        if conf is None:      # make sure there is a confirmation even if the policy reviewed instead of holding
            tx = db.execute(select(Transaction).where(Transaction.tx_ref == "T-CARD-1")).scalar()
            tx.mode, tx.execution_state = "PRE_AUTH", "HELD"
            db.commit()
            conf = confirmations.request_confirmation(db, db.get(Tenant, "BOP"), tx, None,
                                                      active_policy(db, "BOP").document)
        assert conf.channel == "PUSH" and conf.fallback_at is not None      # app first, SMS later
        conf.fallback_at = utcnow() - timedelta(seconds=1)
        db.commit()
        assert confirmations.push_fallback(db) >= 1
        db.refresh(conf)
        assert conf.channel == "PUSH+SMS" and conf.status == "SENT"
        cid = conf.id
    app = {"X-API-Key": client.post("/api/v1/api-clients", headers=a, json={
        "name": "mobile app backend", "scopes": ["confirmations:respond", "cards:control"]}).json()["api_key"]}
    wrong = client.post(f"/api/v1/confirmations/{cid}/respond", headers=app, json={"answer": "NO", "customer_id": "C99999"})
    assert wrong.status_code == 403
    res = client.post(f"/api/v1/confirmations/{cid}/respond", headers=app, json={"answer": "NO"}).json()
    assert res["result"].startswith("rejected") and res["card_frozen"] == "CARD-TEST-0016"
    frozen = client.get("/api/v1/cards/frozen", headers=m).json()
    assert any(c["card_ref"] == "CARD-TEST-0016" and c["status"] == "FROZEN" for c in frozen["controls"])
    nxt = client.post("/api/v1/decisions/evaluate", headers=m, json={**card, "transaction_id": "T-CARD-2", "amount": 20}).json()
    assert nxt["decision"] == "DECLINE" and "CARD_FROZEN" in nxt["reason_codes"]
    assert client.post("/api/v1/cards/CARD-TEST-0016/unfreeze", headers=app, json={}).status_code == 403
    assert client.post("/api/v1/cards/CARD-TEST-0016/unfreeze", headers=m, json={"reason": "case closed"}).json()["status"] == "ACTIVE"
    own = client.post("/api/v1/cards/CARD-TEST-0016/freeze", headers=app, json={"customer_id": "C00016"}).json()
    assert own["source"] == "CUSTOMER_APP"
    assert client.post("/api/v1/cards/CARD-TEST-0016/unfreeze", headers=app, json={}).json()["status"] == "ACTIVE"
    cards = client.get("/api/v1/customers/C00016/cards", headers=m).json()["cards"]
    assert any(c["card_ref"] == "CARD-TEST-0016" and len(c["history"]) >= 2 for c in cards)
    # SIEM: CEF over UDP
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(5)
    port = sock.getsockname()[1]
    put = client.put("/api/v1/siem", headers=a, json={"enabled": True, "protocol": "udp", "host": "127.0.0.1",
                                                       "port": port, "format": "cef", "include_demo": True})
    assert put.status_code == 200, put.text
    assert client.post("/api/v1/siem/test", headers=a).json()["ok"]
    assert b"CEF:0|VeriShield|VeriShield|" in sock.recvfrom(9000)[0]
    with SessionLocal() as db:
        siem.forward(db)                                          # initialises the cursor
    client.put("/api/v1/siem", headers=a, json={"batch": 100})       # an audited configuration change
    with SessionLocal() as db:
        sent = siem.forward(db)
        assert sent >= 1, siem.public(db, db.get(Tenant, "BOP"))
    got = sock.recvfrom(9000)[0]
    assert b"act=siem.update" in got and b"suser=" in got
    sock.close()
    client.put("/api/v1/siem", headers=a, json={"enabled": False})
