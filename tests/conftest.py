import os
import sys
from datetime import timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from banksealer.config import Config  # noqa: E402
from banksealer.engine import BankSealerEngine  # noqa: E402
from banksealer.preparation import TransactionPreparer  # noqa: E402
from mock_channel.generator import TransactionGenerator  # noqa: E402


@pytest.fixture(scope="session")
def config():
    return Config.load(os.path.join(ROOT, "config", "banksealer.yaml"))


@pytest.fixture(scope="session")
def dataset():
    gen = TransactionGenerator(80, seed=11, fraud_ratio=0.05)
    prep = TransactionPreparer()
    history = prep.prepare_many(gen.history(40, 0.0), "mock")
    start, live = gen.cursor, []
    for record in gen.live():
        if gen.cursor - start >= timedelta(days=8):
            break
        live.append(record)
    return history, prep.prepare_many(live, "mock"), gen


@pytest.fixture()
def engine(config, dataset):
    e = BankSealerEngine(config.copy())
    e.fit(dataset[0])
    return e
