"""High availability: several VeriShield application servers sharing one PostgreSQL database.

* **Every node scores.** Decisions are stateless HTTP calls; a load balancer spreads them over the nodes.
  Sessions, idempotency keys, sequence counters and the audit chain live in the database.
* **One leader.** A database lease (``cluster_leases.leader``) elects one node that runs the channel connectors
  and the background jobs (hold expiry, SMS fallback, schedules, model health, network scoring, CTRs, SIEM
  forwarding...). If the leader stops renewing the lease, another node takes over within ``LEASE_SECONDS``.
* **Shared learning.** Every node absorbs the transactions scored by the other nodes into its in-memory
  profiles, and the leader stores a checkpoint of each active model in the database so that a node that
  starts (or restarts) catches up from the checkpoint instead of from its local file.

A single server is simply a one-node cluster: it always holds the lease and skips checkpoints and absorption.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import __version__
from ..models import ClusterLease, ClusterNode, Decision, ModelCheckpoint, ModelVersion, Transaction, utcnow
from ..security import sign_blob
from ..settings import settings

log = logging.getLogger("verishield.cluster")
LEASE = "leader"
LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 10
CHECKPOINT_SECONDS = 300


class _State:
    def __init__(self):
        self.leader = False
        self.leader_since: Optional[datetime] = None
        self.leader_node = ""
        self.absorb_cursor: dict[tuple, datetime] = {}
        self.checkpoint_seq: dict[str, int] = {}
        self.last_checkpoint = 0.0
        self.peers: dict = {}
        self.lock = threading.Lock()


state = _State()


def node_id() -> str:
    return settings.node_id


def is_leader() -> bool:
    return state.leader


# --------------------------------------------------------------------------- leader election
def try_acquire(db: Session, node: Optional[str] = None, seconds: int = LEASE_SECONDS) -> bool:
    """Take or renew the leader lease (atomic in SQLite and PostgreSQL). Returns True when ``node`` leads."""
    node = node or node_id()
    now = utcnow()
    before = db.execute(select(ClusterLease.holder).where(ClusterLease.name == LEASE)).scalar()
    res = db.execute(update(ClusterLease)
                     .where(ClusterLease.name == LEASE, or_(ClusterLease.holder == node, ClusterLease.expires_at < now))
                     .values(holder=node, expires_at=now + timedelta(seconds=seconds)))
    if res.rowcount == 1:
        if before != node:
            db.execute(update(ClusterLease).where(ClusterLease.name == LEASE).values(acquired_at=now))
        db.commit()
        return True
    db.rollback()
    if db.get(ClusterLease, LEASE) is None:
        try:
            db.add(ClusterLease(name=LEASE, holder=node, acquired_at=now, expires_at=now + timedelta(seconds=seconds)))
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
    return False


def release(db: Session, node: Optional[str] = None) -> None:
    node = node or node_id()
    db.execute(update(ClusterLease).where(ClusterLease.name == LEASE, ClusterLease.holder == node)
               .values(expires_at=utcnow() - timedelta(seconds=1)))
    db.commit()


def current_leader(db: Session) -> dict:
    lease = db.get(ClusterLease, LEASE)
    if lease is None:
        return {"node": "", "expires_at": None, "valid": False}
    return {"node": lease.holder, "since": lease.acquired_at, "expires_at": lease.expires_at,
            "valid": lease.expires_at > utcnow()}


def heartbeat(db: Session, duties: bool = True) -> bool:
    """Register this node, renew / contest the lease and switch duties on a role change (``duties=False``:
    register only - used when background jobs are switched off)."""
    from .connectors import runtime, start_enabled

    was = state.leader
    leader = try_acquire(db)
    node = db.get(ClusterNode, node_id())
    info = {"pid": os.getpid(), "cluster_mode": settings.cluster_mode,
            "connectors": {cid: runtime.status(cid) for cid in list(runtime.runners)},
            "checkpoints": dict(state.checkpoint_seq)}
    if node is None:
        node = ClusterNode(node_id=node_id(), host=settings.host, started_at=utcnow())
        db.add(node)
    node.version, node.last_seen, node.info = __version__, utcnow(), info
    node.role = "LEADER" if leader else "FOLLOWER"
    db.commit()
    with state.lock:
        state.leader = leader
        state.leader_node = node_id() if leader else current_leader(db)["node"]
        if not leader and state.leader_node:
            ln = db.get(ClusterNode, state.leader_node)
            state.peers = dict(((ln.info or {}).get("connectors") or {}) if ln else {})
        if leader and not was:
            state.leader_since = utcnow()
    if not duties:
        return leader
    if leader and not was:
        log.warning("node %s is now the LEADER (channels and background jobs run here)", node_id())
        start_enabled(db)
    elif leader and settings.cluster_mode:
        from .connectors import reconcile
        reconcile(db)          # connectors enabled / disabled through another node
    elif was and not leader:
        log.warning("node %s lost the leader lease; stopping channels", node_id())
        runtime.stop_all()
    return leader


def nodes(db: Session) -> list[dict]:
    now = utcnow()
    lead = current_leader(db)
    out = []
    for n in db.execute(select(ClusterNode).order_by(ClusterNode.node_id)).scalars():
        age = (now - n.last_seen).total_seconds()
        out.append({"node_id": n.node_id, "host": n.host, "version": n.version,
                    "role": "LEADER" if (lead["valid"] and lead["node"] == n.node_id) else "FOLLOWER",
                    "alive": age < HEARTBEAT_SECONDS * 3, "last_seen": n.last_seen.isoformat(timespec="seconds") + "Z",
                    "seconds_ago": round(age), "started_at": n.started_at.isoformat(timespec="seconds") + "Z",
                    "connectors_running": sum(1 for s in (n.info or {}).get("connectors", {}).values()
                                              if s.get("running")),
                    "this_node": n.node_id == node_id()})
    return out


def connector_status(db: Session, cid: str) -> Optional[dict]:
    """Runtime status of a connector as reported by the leader (when it does not run on this node)."""
    lead = current_leader(db)
    if not lead["valid"] or lead["node"] == node_id():
        return None
    n = db.get(ClusterNode, lead["node"])
    st = ((n.info or {}).get("connectors") or {}).get(cid) if n else None
    if st:
        st = dict(st, node=lead["node"])
    return st


def summary(db: Session) -> dict:
    lead = current_leader(db)
    ns = nodes(db)
    cps = [{"model_id": c.model_id, "seq": c.seq, "size_mb": round(c.size / 1e6, 2), "node": c.node_id,
            "saved_at": c.saved_at.isoformat(timespec="seconds") + "Z"}
           for c in db.execute(select(ModelCheckpoint)).scalars()]
    return {"cluster_mode": settings.cluster_mode, "this_node": node_id(), "leader": lead["node"] if lead["valid"] else "",
            "lease_expires_at": lead["expires_at"].isoformat(timespec="seconds") + "Z" if lead["expires_at"] else None,
            "nodes": ns, "alive": sum(1 for n in ns if n["alive"]), "checkpoints": cps,
            "database": db.get_bind().dialect.name,
            "warnings": (["Cluster mode needs PostgreSQL: SQLite cannot be shared safely between servers."]
                         if settings.cluster_mode and db.get_bind().dialect.name == "sqlite" else [])}


# --------------------------------------------------------------------------- shared learning
def save_checkpoints(db: Session) -> int:
    """Leader: store the learned state of each active model in the database."""
    from . import engine_registry
    n = 0
    for ws in engine_registry.all_workspaces():
        with ws.lock:
            if ws.engine is None or not ws.engine.trained or not ws.model_id:
                continue
            fd, tmp = tempfile.mkstemp(suffix=".pkl")
            os.close(fd)
            try:
                ws.engine.save_model(tmp)
                blob = open(tmp, "rb").read()
            finally:
                os.unlink(tmp)
            model_id = ws.model_id
        cp = db.get(ModelCheckpoint, model_id)
        if cp is None:
            cp = ModelCheckpoint(model_id=model_id, seq=0, blob=b"")
            db.add(cp)
        cp.seq += 1
        cp.blob, cp.size, cp.node_id, cp.saved_at = blob, len(blob), node_id(), utcnow()
        cp.signature = sign_blob(blob)
        db.commit()
        state.checkpoint_seq[model_id] = cp.seq
        n += 1
    return n


def load_checkpoint(db: Session, mv: ModelVersion, cfg) -> Optional[object]:
    """Engine restored from the shared checkpoint of ``mv`` (cluster mode), or None."""
    if not settings.cluster_mode:
        return None
    cp = db.get(ModelCheckpoint, mv.id)
    if cp is None or not cp.blob:
        return None
    import hmac
    if not hmac.compare_digest(cp.signature or "", sign_blob(cp.blob)):
        log.error("checkpoint of model %s failed its integrity check - ignored", mv.id)
        return None
    from .engine_registry import _load_engine
    fd, tmp = tempfile.mkstemp(suffix=".pkl")
    os.close(fd)
    try:
        with open(tmp, "wb") as fh:
            fh.write(cp.blob)
        eng = _load_engine(tmp, cfg)
    finally:
        os.unlink(tmp)
    state.checkpoint_seq[mv.id] = cp.seq
    state.absorb_cursor[(mv.tenant_id, mv.space)] = cp.saved_at
    log.info("model %s restored from cluster checkpoint #%s (%s)", mv.id, cp.seq, cp.node_id)
    return eng


def absorb_peers(db: Session, limit: int = 2000) -> int:
    """Learn the transactions that the other nodes scored since the last call."""
    from .engine_registry import all_workspaces, to_engine
    total = 0
    for ws in all_workspaces():
        if ws.engine is None or not ws.engine.trained:
            continue
        key = (ws.tenant_id, ws.space)
        since = state.absorb_cursor.get(key) or (utcnow() - timedelta(seconds=HEARTBEAT_SECONDS * 3))
        rows = db.execute(select(Decision, Transaction).join(Transaction, Transaction.tx_ref == Decision.tx_ref)
                          .where(Decision.tenant_id == ws.tenant_id, Decision.space == ws.space,
                                 Transaction.tenant_id == ws.tenant_id, Transaction.space == ws.space,
                                 Decision.issued_at > since, Decision.node_id.is_not(None),
                                 Decision.node_id != node_id())
                          .order_by(Decision.issued_at).limit(limit)).all()
        with ws.lock:
            for dec, tx in rows:
                etx = to_engine(tx)
                try:
                    ws.engine.temporal.observe(etx)
                    if dec.decision == "PASS":
                        ws.engine.learn(etx)
                except Exception as exc:  # noqa: BLE001 - one bad row must not stop the sync
                    log.debug("absorb %s: %s", tx.tx_ref, exc)
                total += 1
        if rows:
            state.absorb_cursor[key] = rows[-1][0].issued_at
        elif key not in state.absorb_cursor:
            state.absorb_cursor[key] = since
    return total


# --------------------------------------------------------------------------- heartbeat thread
class _Heartbeat:
    def __init__(self):
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, name="verishield-cluster", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        import time
        from ..db import SessionLocal
        while not self.stop.wait(HEARTBEAT_SECONDS):
            try:
                with SessionLocal() as db:
                    leader = heartbeat(db)
                    if settings.cluster_mode:
                        absorb_peers(db)
                        if leader and time.time() - state.last_checkpoint > CHECKPOINT_SECONDS:
                            save_checkpoints(db)
                            state.last_checkpoint = time.time()
            except Exception as exc:  # noqa: BLE001
                log.warning("cluster heartbeat error: %s", exc)

    def shutdown(self) -> None:
        self.stop.set()
        if state.leader:
            try:
                from ..db import SessionLocal
                with SessionLocal() as db:
                    release(db)
            except Exception:  # noqa: BLE001
                pass
            state.leader = False


heartbeat_thread = _Heartbeat()

__all__ = ["is_leader", "node_id", "heartbeat", "try_acquire", "release", "nodes", "summary", "save_checkpoints",
           "load_checkpoint", "absorb_peers", "heartbeat_thread", "connector_status", "state"]
