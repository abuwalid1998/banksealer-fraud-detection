import json
import socket
import time
import urllib.request

from banksealer.channels import ChannelManager
from banksealer.controller import AppController
from banksealer.reports import ReportGenerator
from mock_channel.generate_transactions import main as generate


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


RECORD = {"user_id": "C1", "timestamp": "2026-01-01 10:00:00", "tx_type": "bank_transfer", "amount": 10,
          "iban": "IT60X0542811101000000123456"}


def test_tcp_and_http_channels():
    m = ChannelManager()
    tcp_port, http_port = _free_port(), _free_port()
    tcp = m.add({"name": "tcp", "type": "tcp", "options": {"host": "127.0.0.1", "port": tcp_port}})
    http = m.add({"name": "http", "type": "http", "options": {"host": "127.0.0.1", "port": http_port}})
    tcp.start()
    http.start()
    assert _wait(lambda: tcp.status == "running" and http.status == "running")
    time.sleep(0.3)
    with socket.create_connection(("127.0.0.1", tcp_port)) as s:
        s.sendall((json.dumps({**RECORD, "tx_id": "A"}) + "\nnot json\n").encode())
    req = urllib.request.Request(f"http://127.0.0.1:{http_port}/transactions", method="POST",
                                 data=json.dumps([{**RECORD, "tx_id": "B"}, {**RECORD, "tx_id": "C"}]).encode())
    assert json.loads(urllib.request.urlopen(req, timeout=5).read())["accepted"] == 2
    assert _wait(lambda: tcp.received == 1 and http.received == 2)
    _, live = m.drain()
    assert sorted(t.tx_id for t in live) == ["A", "B", "C"]
    assert tcp.preparer.stats["rejected"] == 1
    m.stop_all()


def test_csv_and_sqlite_channels(tmp_path):
    csv_path, db_path = tmp_path / "in.csv", tmp_path / "core.db"
    generate(["--customers", "5", "--days", "3", "--seed", "1", "--output", "csv", "--file", str(csv_path)])
    generate(["--customers", "5", "--days", "3", "--seed", "2", "--output", "sqlite", "--file", str(db_path)])
    m = ChannelManager()
    a = m.add({"name": "csv", "type": "csv", "options": {"path": str(csv_path), "follow": False}})
    b = m.add({"name": "db", "type": "sqlite", "options": {"path": str(db_path), "poll_seconds": 0.1}})
    a.start()
    b.start()
    expected = sum(1 for _ in open(csv_path)) - 1
    assert _wait(lambda: a.received == expected and b.received > 0)
    _, live = m.drain()
    assert len(live) == a.received + b.received
    m.stop_all()


def test_controller_end_to_end_with_mock_channel(tmp_path, config):
    cfg_path = tmp_path / "cfg.yaml"
    c = config.copy()
    c.data["app"]["reports_dir"] = str(tmp_path / "reports")
    c.data["channels"] = [{"name": "Mock", "type": "mock", "enabled": True,
                           "options": {"customers": 60, "history_days": 30, "rate": 0, "fraud_ratio": 0.08, "seed": 5}}]
    c.save(str(cfg_path))
    ctl = AppController(str(cfg_path), str(tmp_path / "db.sqlite"), start_channels=True)
    assert _wait(lambda: (ctl.pump() or True) and len(ctl.pairs) > 1500, timeout=30)
    ctl.manager.stop_all()
    flagged = [(t, r) for t, r in ctl.pairs if r.suspicious]
    assert ctl.engine.trained and flagged
    assert len(list((tmp_path / "reports").glob("report_*.pdf"))) > 0      # auto reports
    tx, r = flagged[0]
    ctl.set_status(tx.tx_id, "legit")
    assert ctl.storage.load()[-1][1].status in ("open", "legit", "fraud")
    # stricter thresholds for one bank -> more alerts after re-evaluation
    before = sum(1 for _, x in ctl.pairs if x.suspicious)
    new_cfg = ctl.config.copy()
    new_cfg.set_bank_thresholds("BANK001", {"suspicious": 0.2})
    ctl.apply_config(new_cfg)
    assert sum(1 for _, x in ctl.pairs if x.suspicious) > before
    assert ctl.summary_report().endswith(".pdf")
    assert ctl.export_csv().endswith(".csv")
    ctl.shutdown()


def test_transaction_report_pdf(engine, dataset, tmp_path, config):
    _, live, _ = dataset
    pairs = [(t, engine.score(t)) for t in live[:400]]
    tx, r = max(pairs, key=lambda p: p[1].combined_score)
    gen = ReportGenerator(config, str(tmp_path))
    path = gen.transaction_report(tx, r, engine.customer_summary(tx.user_id), engine.ranked_reasons(r))
    data = open(path, "rb").read()
    assert data.startswith(b"%PDF") and len(data) > 3000
