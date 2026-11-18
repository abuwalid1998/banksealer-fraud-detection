from datetime import datetime, timedelta

import numpy as np

from banksealer.cli import metrics
from banksealer.engine.global_profile import dbscan, iterative_dbscan
from banksealer.engine.hbos import CategoricalHistogram, NumericHistogram
from banksealer.models import Transaction


def _tx(user="U1", amount=100.0, iban="IT60X0542811101000000123456", hour=10, asn="IT-AS3269", day=0, i=0):
    return Transaction(tx_id=f"T{user}{day}{i}{amount}{iban[-3:]}{hour}", user_id=user,
                       timestamp=datetime(2026, 1, 1, hour) + timedelta(days=day), tx_type="bank_transfer",
                       amount=amount, iban=iban, iban_cc=iban[:2], ip="1.1.1.1", cc_asn=asn, bank_id="BANK001")


def test_hbos_histograms():
    h = CategoricalHistogram()
    for _ in range(9):
        h.add("A")
    h.add("B")
    assert h.density("A", 0.5) == 1.0
    assert h.density("B", 0.5) < h.density("A", 0.5)
    assert h.density("NEW", 0.5) < h.density("B", 0.5)
    n = NumericHistogram(np.arange(0, 25, 1.0), circular=True, neighbor_weight=0.5)
    for _ in range(10):
        n.add(23.5)
    assert n.density(0.2, 0.5) > n.density(12, 0.5)   # circular neighbour of 23h


def test_dbscan_finds_two_groups():
    rng = np.random.default_rng(0)
    X = np.vstack([rng.normal(0, 0.1, (30, 2)), rng.normal(5, 0.1, (30, 2)), [[20, 20]]])
    labels = dbscan(X, 0.5, 5)
    assert len(set(labels[:30])) == 1 and len(set(labels[30:60])) == 1 and labels[0] != labels[30]
    assert labels[-1] == -1
    assert iterative_dbscan(X, 0.5, 5, 1.5, 3)[-1] == -1


def test_local_profile_flags_new_beneficiary_and_amount(engine):
    from banksealer.config import Config
    e = engine.__class__(Config(engine.config.data))
    history = [_tx(amount=100 + i % 7, day=i, i=i) for i in range(40)] + \
              [_tx(user=f"P{k}", amount=50, day=i, i=i) for k in range(5) for i in range(10)]
    e.fit(history)
    normal = e.score(_tx(amount=103, day=41, i=99), learn=False)
    odd = e.score(_tx(amount=9000, iban="LT121000011101001000", asn="NL-AS60781", hour=3, day=41, i=98), learn=False)
    assert odd.local_score > normal.local_score + 0.5
    assert odd.suspicious and not normal.suspicious
    features = {r.feature for r in odd.reasons if r.profile == "local"}
    assert {"iban", "iban_cc", "cc_asn", "amount"} <= features
    assert "never used before" in odd.explanation and odd.risk_level in ("HIGH", "CRITICAL")


def test_temporal_profile_flags_bursts(engine):
    from banksealer.config import Config
    e = engine.__class__(Config(engine.config.data))
    e.fit([_tx(amount=100, day=i, i=i) for i in range(30)] +
          [_tx(user=f"P{k}", amount=50, day=i, i=i) for k in range(4) for i in range(10)])
    scores = []
    for j in range(6):
        tx = _tx(amount=120, day=31, i=j, iban=f"IT60X05428111010000001234{j:02d}")
        tx.timestamp = tx.timestamp + timedelta(minutes=10 * j)
        scores.append(e.score(tx).temporal_score)
    assert scores[0] < 0.2 and scores[-1] > 0.5


def test_detection_quality_on_mock_data(engine, dataset):
    _, live, _ = dataset
    pairs = [(t, engine.score(t)) for t in live]
    m = metrics(pairs)
    assert m["frauds"] > 5
    assert m["auc_combined"] > 0.9
    assert m["recall"] > 0.6 and m["precision"] > 0.6
    flagged = [r for _, r in pairs if r.suspicious]
    assert all(r.explanation.startswith(r.risk_level) and "Why:" in r.explanation for r in flagged)


def test_bank_thresholds_are_applied(engine, dataset):
    _, live, _ = dataset
    tx = next(t for t in live if t.tx_type == "bank_transfer" and t.bank_id == "BANK001")
    result = engine.score(tx, learn=False)
    cfg = engine.config
    cfg.set_bank_thresholds("BANK001", {"suspicious": 0.0})
    engine.decide(result, tx)
    assert result.suspicious
    cfg.set_bank_thresholds("BANK001", {"suspicious": 1.0, "local_alert": 2, "temporal_alert": 2,
                                        "global_alert": 2, "max_amount": {"bank_transfer": tx.amount / 2}})
    engine.decide(result, tx)
    assert result.suspicious and any(r.feature == "max_amount" for r in result.rule_reasons)
    assert result.combined_score >= float(cfg.thresholds_for("BANK001")["hard_rule_score"])
    assert cfg.thresholds_for("BANK002")["suspicious"] == 0.45   # other banks untouched


def test_model_save_and_load(engine, dataset, tmp_path):
    _, live, _ = dataset
    path = tmp_path / "engine.pkl"
    engine.save_model(str(path))
    other = engine.__class__(engine.config)
    other.load_model(str(path))
    a = engine.score(live[0], learn=False)
    b = other.score(live[0], learn=False)
    assert a.combined_score == b.combined_score
