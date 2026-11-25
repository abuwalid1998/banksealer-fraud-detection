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


# --------------------------------------------------------------------------- business / platform gauges
_gauge_cache: dict = {"at": 0.0, "text": ""}


def business_gauges(max_age: float = 15.0) -> str:
    """Prometheus gauges read from the database (cached for ``max_age`` seconds)."""
    if time.time() - _gauge_cache["at"] < max_age and _gauge_cache["text"]:
        return _gauge_cache["text"]
    from datetime import timedelta

    from sqlalchemy import func, select

    from .. import __version__
    from ..db import SessionLocal
    from ..models import (Alert, Case, CardControl, Connector, CtrReport, CustomerConfirmation, Decision,
                          DeadLetterEvent, SystemIncident, utcnow)
    from . import cluster

    lines = [f'verishield_build_info{{version="{__version__}",node="{cluster.node_id()}"}} 1',
             f'verishield_cluster_leader{{node="{cluster.node_id()}"}} {1 if cluster.is_leader() else 0}']
    try:
        with SessionLocal() as db:
            now = utcnow()
            alive = sum(1 for n in cluster.nodes(db) if n["alive"])
            lines.append(f"verishield_cluster_nodes_alive {alive}")
            for space, sev, n in db.execute(select(Alert.space, Alert.severity, func.count()).where(
                    Alert.status.in_(("OPEN", "TRIAGED"))).group_by(Alert.space, Alert.severity)).all():
                lines.append(f'verishield_alerts_open{{space="{space}",severity="{sev}"}} {n}')
            for space, n in db.execute(select(Case.space, func.count()).where(Case.closed_at.is_(None))
                                       .group_by(Case.space)).all():
                lines.append(f'verishield_cases_open{{space="{space}"}} {n}')
            for space, n in db.execute(select(Case.space, func.count()).where(
                    Case.closed_at.is_(None), Case.due_at.is_not(None), Case.due_at < now).group_by(Case.space)).all():
                lines.append(f'verishield_cases_overdue{{space="{space}"}} {n}')
            for status, n in db.execute(select(Connector.status, func.count()).group_by(Connector.status)).all():
                lines.append(f'verishield_connectors{{status="{status}"}} {n}')
            lines.append("verishield_dead_letters_pending " + str(db.execute(select(func.count()).select_from(
                DeadLetterEvent).where(DeadLetterEvent.status == "PENDING")).scalar() or 0))
            lines.append("verishield_incidents_open " + str(db.execute(select(func.count()).select_from(
                SystemIncident).where(SystemIncident.status.notin_(("RESOLVED", "POSTMORTEM")))).scalar() or 0))
            lines.append("verishield_confirmations_pending " + str(db.execute(select(func.count()).select_from(
                CustomerConfirmation).where(CustomerConfirmation.status == "SENT")).scalar() or 0))
            lines.append("verishield_cards_frozen " + str(db.execute(select(func.count()).select_from(
                CardControl).where(CardControl.status == "FROZEN")).scalar() or 0))
            lines.append("verishield_ctr_draft " + str(db.execute(select(func.count()).select_from(
                CtrReport).where(CtrReport.status == "DRAFT")).scalar() or 0))
            for space, dec, n in db.execute(select(Decision.space, Decision.decision, func.count()).where(
                    Decision.issued_at >= now - timedelta(minutes=5)).group_by(Decision.space, Decision.decision)).all():
                lines.append(f'verishield_decisions_5m{{space="{space}",decision="{dec}"}} {n}')
            from ..models import Tenant
            from . import siem
            for t in db.execute(select(Tenant)).scalars():
                st = siem.public(db, t)
                if st["config"]["enabled"]:
                    lines.append(f'verishield_siem_lag_events{{tenant="{t.id}"}} {st["state"].get("lag") or 0}')
    except Exception as exc:  # noqa: BLE001 - metrics must never fail the scrape
        lines.append(f'verishield_metrics_error{{error="{type(exc).__name__}"}} 1')
    _gauge_cache.update(at=time.time(), text="\n".join(lines) + "\n")
    return _gauge_cache["text"]
