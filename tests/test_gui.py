import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_main_window_renders_all_tabs(tmp_path, config):
    from banksealer.controller import AppController
    from banksealer.gui.main_window import MainWindow, screenshot_tour

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    c = config.copy()
    c.data["app"]["reports_dir"] = str(tmp_path / "reports")
    c.data["app"]["auto_report"] = False
    c.data["channels"] = [{"name": "Mock", "type": "mock", "enabled": True,
                           "options": {"customers": 40, "history_days": 20, "rate": 0, "fraud_ratio": 0.1, "seed": 3}}]
    cfg = tmp_path / "cfg.yaml"
    c.save(str(cfg))
    ctl = AppController(str(cfg), str(tmp_path / "db.sqlite"), start_channels=True)
    import time
    end = time.time() + 20
    while time.time() < end and len(ctl.pairs) < 500:
        ctl.pump()
        time.sleep(0.05)
    ctl.manager.stop_all()
    window = MainWindow(ctl)
    screenshot_tour(window, str(tmp_path / "shots"))
    assert len(list((tmp_path / "shots").glob("*.png"))) == 6
    flagged = next(t for t, r in ctl.pairs if r.suspicious)
    window.open_transaction(flagged.tx_id)
    assert "SUSPICIOUS" in window.transactions.detail.toPlainText()
    window.close()
