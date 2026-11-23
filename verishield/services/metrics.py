"""In-process operational metrics (request latency, decisions, errors) + Prometheus export."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

import numpy as np

STARTED = time.time()


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.counters: dict[str, float] = defaultdict(float)
        self.latency: dict[str, deque] = defaultdict(lambda: deque(maxlen=2000))
        self.errors: deque = deque(maxlen=200)

    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self.counters[name] += value

    def observe(self, name: str, ms: float) -> None:
        with self._lock:
            self.latency[name].append(ms)

    def error(self, category: str, message: str) -> None:
        with self._lock:
            self.errors.append({"at": time.time(), "category": category, "message": message[:300]})
            self.counters[f"errors_total:{category}"] += 1

    def latency_summary(self, name: str) -> dict:
        with self._lock:
            data = list(self.latency.get(name, []))
        if not data:
            return {"count": 0, "p50": None, "p95": None, "p99": None, "avg": None}
        arr = np.asarray(data)
        return {"count": len(arr), "p50": round(float(np.percentile(arr, 50)), 2),
                "p95": round(float(np.percentile(arr, 95)), 2), "p99": round(float(np.percentile(arr, 99)), 2),
                "avg": round(float(arr.mean()), 2)}

    def snapshot(self) -> dict:
        with self._lock:
            names = list(self.latency)
            counters = dict(self.counters)
            errors = list(self.errors)[-50:]
        return {"uptime_seconds": round(time.time() - STARTED), "counters": counters,
                "latency": {n: self.latency_summary(n) for n in names}, "recent_errors": errors}

    def prometheus(self) -> str:
        lines = ["# HELP verishield_uptime_seconds Process uptime", "# TYPE verishield_uptime_seconds gauge",
                 f"verishield_uptime_seconds {time.time() - STARTED:.0f}"]
        with self._lock:
            counters = dict(self.counters)
            names = list(self.latency)
        for key, value in sorted(counters.items()):
            name, _, label = key.partition(":")
            metric = "verishield_" + name.replace(".", "_").replace("-", "_")
            lines.append(f'{metric}{{label="{label}"}} {value}' if label else f"{metric} {value}")
        for name in names:
            s = self.latency_summary(name)
            if s["count"]:
                for q in ("p50", "p95", "p99"):
                    lines.append(f'verishield_latency_ms{{route="{name}",quantile="{q}"}} {s[q]}')
        return "\n".join(lines) + "\n"


metrics = Metrics()
