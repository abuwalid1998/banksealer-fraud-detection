#!/usr/bin/env python3
"""Smoke test of a running BankSealer Enterprise stack (docker compose --profile demo up).

    python scripts/smoke_test.py --url http://localhost:8080 --user admin --password admin123
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid


class Client:
    def __init__(self, base):
        self.base, self.token = base.rstrip("/"), None

    def call(self, method, path, body=None, raw=None, content_type="application/json"):
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                return resp.status, (json.loads(payload) if "json" in ctype else payload)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(errors="replace")


def wait_command(c, cmd_id, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        _, cmd = c.call("GET", f"/api/engine/commands/{cmd_id}")
        if cmd["status"] in ("done", "failed"):
            return cmd
        time.sleep(1)
    raise AssertionError(f"command {cmd_id} did not finish")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8080")
    p.add_argument("--user", default="admin")
    p.add_argument("--password", default="admin123")
    a = p.parse_args()
    c = Client(a.url)
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond)))
        print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")

    status, health = c.call("GET", "/api/health")
    check("health", status == 200 and health["database"] == "ok", health)
    status, login = c.call("POST", "/api/auth/login", {"username": a.user, "password": a.password})
    check("login", status == 200)
    c.token = login["access_token"]

    _, dash = c.call("GET", "/api/dashboard")
    check("engine trained and online", dash["engine"]["trained"] and dash["engine"]["online"], dash["engine"]["state"])
    check("transactions scored", dash["kpis"]["transactions"] > 0, dash["kpis"]["transactions"])

    _, alerts = c.call("GET", "/api/transactions?suspicious=true&sort=score&size=5")
    check("alerts listed", alerts["total"] > 0, alerts["total"])
    tx_id = alerts["items"][0]["tx_id"]
    _, detail = c.call("GET", f"/api/transactions/{urllib.parse.quote(tx_id)}")
    check("reasons explained", len(detail["reasons"]) > 0 and "Why" in detail["score"]["explanation"])

    status, rep = c.call("POST", f"/api/transactions/{urllib.parse.quote(tx_id)}/report")
    status, pdf = c.call("GET", f"/api/reports/{rep['id']}/download")
    check("PDF report", status == 200 and pdf[:4] == b"%PDF")

    status, fb = c.call("POST", f"/api/transactions/{urllib.parse.quote(tx_id)}/feedback", {"status": "fraud", "note": "smoke test"})
    check("analyst feedback", status == 200 and fb["status"] == "fraud")

    _, banks = c.call("GET", "/api/banks")
    bank = banks[0]
    status, upd = c.call("PUT", f"/api/banks/{bank['code']}", {"code": bank["code"], "name": bank["name"], "active": True,
                                                              "thresholds": bank["thresholds"]})
    cmd = wait_command(c, upd["command_id"])
    check("bank thresholds + re-evaluation", cmd["status"] == "done", cmd["result"])

    _, channels = c.call("GET", "/api/channels")
    legacy = next(ch for ch in channels if ch["name"] == "Legacy card system")
    sample = "client,op_date,op,value_cents,card\nZ1,16/11/2026 10:30,CARD_LOAD,125050,4024-1\n"
    _, prev = c.call("POST", "/api/preparation/preview", {"channel_id": legacy["id"], "text": sample})
    check("preparation preview", prev["results"][0]["ok"] and prev["results"][0]["transaction"]["amount"] == 1250.5)

    name = f"smoke-{uuid.uuid4().hex[:6]}"
    status, ch = c.call("POST", "/api/channels", {"name": name, "type": "csv", "enabled": False,
                                                  "options": {"path": "/data/incoming/smoke.csv"}})
    check("create channel", status == 200, name)
    status, _ = c.call("DELETE", f"/api/channels/{ch['id']}")
    check("delete channel", status == 200)

    boundary = uuid.uuid4().hex
    csv_body = "user_id,timestamp,tx_type,amount,iban\nC00001,2031-01-01 10:00:00,transfer,99,IT60X0542811101000000123456\n"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"mode\"\r\n\r\nscore\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"smoke.csv\"\r\n"
            f"Content-Type: text/csv\r\n\r\n{csv_body}\r\n--{boundary}--\r\n").encode()
    status, up = c.call("POST", "/api/engine/upload", raw=body, content_type=f"multipart/form-data; boundary={boundary}")
    cmd = wait_command(c, up["id"])
    check("file upload scored", cmd["status"] == "done" and cmd["result"]["accepted"] == 1, cmd["result"])

    status, csv_export = c.call("GET", "/api/export/transactions.csv?suspicious=true")
    check("CSV export", status == 200)
    status, summary = c.call("POST", "/api/reports/summary?suspicious=true")
    check("summary report", status == 200, summary)
    _, audit = c.call("GET", "/api/audit?size=20")
    check("audit log", audit["total"] > 0)

    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    import urllib.parse  # noqa: F401
    sys.exit(main())
