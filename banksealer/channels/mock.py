"""In-process mock channel backed by :mod:`mock_channel.generator`."""
from __future__ import annotations

import os
import sys
import time

from .base import Channel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from mock_channel.generator import TransactionGenerator  # noqa: E402


class MockChannel(Channel):
    type_name = "mock"
    description = "Synthetic transactions with injected fraud (accelerated simulated clock)"
    default_options = {"customers": 150, "history_days": 45, "rate": 4.0, "fraud_ratio": 0.03,
                       "history_fraud_ratio": 0.0, "seed": 42}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.generator = None
        self._history_sent = False

    def _ensure_generator(self) -> None:
        if self.generator is None:
            o = self.options
            seed = o.get("seed")
            self.generator = TransactionGenerator(int(o.get("customers", 150)),
                                                  seed=int(seed) if seed not in (None, "") else None,
                                                  fraud_ratio=float(o.get("fraud_ratio", 0.03)))
            self._stream = self.generator.live(float(o.get("fraud_ratio", 0.03)))

    def run(self) -> None:
        self._ensure_generator()
        days = int(self.options.get("history_days", 0) or 0)
        if days > 0 and not self._history_sent:
            for record in self.generator.history(days, float(self.options.get("history_fraud_ratio", 0.0))):
                self.emit(record, history=True)
            self._stream = self.generator.live(float(self.options.get("fraud_ratio", 0.03)))
            self._history_sent = True
        rate = float(self.options.get("rate", 4.0))
        interval = 1.0 / rate if rate > 0 else 0.0
        while not self._stop.is_set():
            self.emit(next(self._stream))
            if interval:
                self._stop.wait(interval)
            else:
                time.sleep(0)
