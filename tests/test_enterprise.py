"""Integration test of the enterprise backend: API + worker on a temporary SQLite database."""
import io
import os
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient  # noqa: E402

from server import db  # noqa: E402
from server.settings import settings  # noqa: E402


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("enterprise")
    settings.database_url = f"sqlite:///{tmp / 'bs.db'}"
    settings.data_dir = str(tmp / "data")
    settings.seed_config = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "enterprise.yaml")
    db.configure(settings.database_url)
    from server.api.main import app
    from server.worker.main import Worker
    client = TestClient(app)
    client.__enter__()
    worker = Worker()
    worker.startup()
    yield client, worker
    worker.manager.stop_all()
    client.__exit__(None, None, None)


def auth(client, user="admin", password="admin123"):
    r = client.post("/api/auth/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def run_until(worker, cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        worker.step()
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_enterprise_flow(stack):
    client, worker = stack
    assert client.get("/api/health").json()["database"] == "ok"
    assert client.get("/api/dashboard").status_code == 401
    h = auth(client)

    # --- channels: seeded from config, switch network channels off and enable a fast simulator
    channels = client.get("/api/channels", headers=h).json()
    assert {c["name"] for c in channels} >= {"Core banking webhook", "Built-in simulator", "Legacy card system"}
    for c in channels:
        if c["enabled"]:
            assert client.post(f"/api/channels/{c['id']}/stop", headers=h).status_code == 200
    sim = next(c for c in channels if c["type"] == "mock")
    body = {**sim, "enabled": True, "options": {**sim["options"], "customers": 50, "history_days": 25, "rate": 0,
                                               "fraud_ratio": 0.08, "seed": 4}}
    r = client.put(f"/api/channels/{sim['id']}", json=body, headers=h)
    assert r.status_code == 200 and r.json()["version"] == sim["version"] + 1

    assert run_until(worker, lambda: worker.scored_total > 1200)
    dash = client.get("/api/dashboard", headers=h).json()
    assert dash["engine"]["trained"] and dash["engine"]["online"]
    assert dash["kpis"]["transactions"] > 1200 and dash["kpis"]["suspicious"] > 0
    assert sum(dash["histogram"]) == dash["kpis"]["transactions"]
    assert dash["latest"] and dash["latest"][0]["main_reason"]
    sim_status = next(c for c in client.get("/api/channels", headers=h).json() if c["id"] == sim["id"])["runtime"]
    assert sim_status["status"] == "running" and sim_status["history"] > 500

    # --- transactions, detail with reasons, filters, export
    page = client.get("/api/transactions", params={"suspicious": True, "sort": "score"}, headers=h).json()
    assert page["total"] == dash["kpis"]["suspicious"]
    tx_id = page["items"][0]["tx_id"]
    detail = client.get(f"/api/transactions/{tx_id}", headers=h).json()
    assert detail["score"]["suspicious"] and detail["reasons"] and detail["score"]["explanation"].count("Why")
    assert detail["customer_profile"]
    assert client.get("/api/transactions", params={"risk": "CRITICAL"}, headers=h).json()["total"] <= page["total"]
    csv_text = client.get("/api/export/transactions.csv", params={"suspicious": True}, headers=h).text
    assert csv_text.startswith("tx_id,") and tx_id in csv_text

    # --- reports (auto + on demand + summary) and download
    rep = client.post(f"/api/transactions/{tx_id}/report", headers=h).json()
    pdf = client.get(f"/api/reports/{rep['id']}/download", headers=h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert client.post("/api/reports/summary", params={"suspicious": True}, headers=h).status_code == 200
    reports = client.get("/api/reports", headers=h).json()
    assert reports["total"] >= 2 and any(r["created_by"] == "auto" for r in reports["items"])

    # --- analyst feedback -> worker learns
    fb = client.post(f"/api/transactions/{tx_id}/feedback", json={"status": "legit", "note": "customer confirmed"},
                     headers=h)
    assert fb.json()["status"] == "legit"
    assert run_until(worker, lambda: client.get("/api/engine/commands", headers=h).json()[0]["status"] == "done")

    # --- customers
    customers = client.get("/api/customers", params={"flagged": True}, headers=h).json()
    assert customers["total"] > 0
    cust = client.get(f"/api/customers/{customers['items'][0]['user_id']}", headers=h).json()
    assert cust["profile"] and cust["transactions"]

    # --- bank thresholds: stricter bank -> more alerts after re-evaluation
    banks = client.get("/api/banks", headers=h).json()
    bank = banks[0]
    before = client.get("/api/transactions", params={"suspicious": True, "bank": bank["code"]}, headers=h).json()["total"]
    r = client.put(f"/api/banks/{bank['code']}", json={"code": bank["code"], "name": bank["name"], "active": True,
                                                       "thresholds": {"suspicious": 0.2, "high": 0.6}}, headers=h)
    assert r.status_code == 200
    cmd_id = r.json()["command_id"]
    assert run_until(worker, lambda: client.get(f"/api/engine/commands/{cmd_id}", headers=h).json()["status"] == "done")
    after = client.get("/api/transactions", params={"suspicious": True, "bank": bank["code"]}, headers=h).json()["total"]
    assert after > before
    bad = client.put("/api/config/thresholds", json={"suspicious": 0.9, "high": 0.5, "critical": 0.8}, headers=h)
    assert bad.status_code == 400
    assert client.post("/api/banks", json={"code": "BANK009", "name": "New Bank"}, headers=h).status_code == 200
    assert "BANK009" in client.get("/api/config/export", headers=h).json()["yaml"]

    # --- data preparation preview with the legacy channel rules
    legacy = next(c for c in channels if c["name"] == "Legacy card system")
    sample = "client,op_date,op,value_cents,card\nZ1,16/11/2026 10:30,CARD_LOAD,125050,4024-1\nZ2,bad,CARD_LOAD,10,4024-2\n"
    prev = client.post("/api/preparation/preview", json={"channel_id": legacy["id"], "text": sample}, headers=h).json()
    ok, err = prev["results"]
    assert ok["ok"] and ok["transaction"]["amount"] == 1250.5 and ok["transaction"]["tx_type"] == "prepaid_card"
    assert ok["transaction"]["bank_id"] == "BANK001"
    assert not err["ok"] and "format" in err["error"]
    assert client.get("/api/preparation/schema", headers=h).json()["rules"]

    # --- file upload (score mode)
    data = "user_id,timestamp,tx_type,amount,iban\nC00001,2030-01-01 10:00:00,transfer,99,IT60X0542811101000000123456\n" \
           "C00001,2030-01-01 10:05:00,unknown,5,\n"
    up = client.post("/api/engine/upload", data={"mode": "score"}, headers=h,
                     files={"file": ("batch.csv", io.BytesIO(data.encode()), "text/csv")}).json()
    assert run_until(worker, lambda: client.get(f"/api/engine/commands/{up['id']}", headers=h).json()["status"] == "done")
    res = client.get(f"/api/engine/commands/{up['id']}", headers=h).json()["result"]
    assert res == {"accepted": 1, "rejected": 1, "scored": 1}
    errs = client.get("/api/preparation/errors", headers=h).json()
    assert any(e["channel"] == "file:" + os.path.basename(up["payload"]["path"]) for e in errs["items"])

    # --- users and roles
    assert client.post("/api/users", json={"username": "ana", "role": "viewer", "password": "viewer-pass-1"},
                       headers=h).status_code == 200
    hv = auth(client, "ana", "viewer-pass-1")
    assert client.get("/api/dashboard", headers=hv).status_code == 200
    assert client.put("/api/config/app", json={}, headers=hv).status_code == 403
    assert client.post(f"/api/transactions/{tx_id}/feedback", json={"status": "fraud"}, headers=hv).status_code == 403
    audit = client.get("/api/audit", headers=h).json()
    actions = {a["action"] for a in audit["items"]}
    assert {"bank.update", "transaction.feedback", "user.create", "channel.update"} <= actions
