"""VeriShield platform tests (BRD section 22 acceptance criteria, exercised through the REST API)."""
from __future__ import annotations

import os
import tempfile

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, update  # noqa: E402

from verishield.settings import configure  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="verishield-test-")
configure(data_dir=__import__("pathlib").Path(_tmp), database_url=os.environ.get("VERISHIELD_TEST_DATABASE_URL", f"sqlite:///{_tmp}/test.db"),
          background_jobs=False, seed_demo=True, seed_demo_users=True, demo_customers=80, demo_history_days=35,
          require_mfa=False)

from verishield.app import app  # noqa: E402

PASSWORDS = {"1000": "Admin", "2000": "Manager#2026", "3001": "Analyst1#2026", "3002": "Analyst2#2026",
             "4000": "Chief#2026"}
NEW = "N3w!Secure-Pass"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
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
