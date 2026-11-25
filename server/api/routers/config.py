import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from banksealer.config import DEFAULT_CONFIG

from ...config_store import SECTIONS, apply_config_data, export_yaml, load_config, save_section
from ...db import get_session
from ...models import Bank, Setting, User, now
from ..deps import admin, audit, enqueue, viewer

router = APIRouter(prefix="/api", tags=["configuration"])


def bank_json(b: Bank, cfg) -> dict:
    return {"code": b.code, "name": b.name, "active": b.active, "thresholds": b.thresholds or {},
            "effective": cfg.thresholds_for(b.code),
            "updated_at": b.updated_at.isoformat(timespec="seconds") if b.updated_at else None}


@router.get("/config")
def get_config(_: User = Depends(viewer), s: Session = Depends(get_session)):
    cfg = load_config(s)
    meta = {r.key: {"updated_at": r.updated_at.isoformat(timespec="seconds") if r.updated_at else None,
                    "updated_by": r.updated_by} for r in s.scalars(select(Setting))}
    return {"sections": {k: cfg.data[k] for k in SECTIONS}, "defaults": {k: DEFAULT_CONFIG[k] for k in SECTIONS},
            "meta": meta, "banks": [bank_json(b, cfg) for b in s.scalars(select(Bank).order_by(Bank.code))]}


@router.put("/config/{section}")
def put_section(section: str, value: dict, reevaluate: bool = True, user: User = Depends(admin),
                s: Session = Depends(get_session)):
    if section not in SECTIONS:
        raise HTTPException(404, f"unknown section; use one of {', '.join(SECTIONS)}")
    _check(section, value)
    old = load_config(s).data[section]
    save_section(s, section, value, user.username)
    audit(s, user, "config.update", section, {"before": old, "after": value})
    cmd = enqueue(s, user, "reevaluate") if reevaluate and section in ("thresholds", "engine") else None
    s.commit()
    return {"ok": True, "command_id": cmd.id if cmd else None}


def _check(section: str, value: dict) -> None:
    if section == "thresholds":
        for key in ("suspicious", "high", "critical"):
            v = value.get(key)
            if v is None or not 0 <= float(v) <= 1:
                raise HTTPException(400, f"{key} must be between 0 and 1")
        if not float(value["suspicious"]) <= float(value["high"]) <= float(value["critical"]):
            raise HTTPException(400, "thresholds must satisfy suspicious <= high <= critical")
    if section == "engine":
        weights = value.get("profile_weights") or {}
        if sum(float(weights.get(k, 0)) for k in ("local", "temporal", "global")) <= 0:
            raise HTTPException(400, "at least one profile weight must be positive")


class BankIn(BaseModel):
    code: str
    name: str
    active: bool = True
    thresholds: dict = Field(default_factory=dict)


@router.get("/banks")
def list_banks(_: User = Depends(viewer), s: Session = Depends(get_session)):
    cfg = load_config(s)
    return [bank_json(b, cfg) for b in s.scalars(select(Bank).order_by(Bank.code))]


@router.post("/banks")
def create_bank(body: BankIn, user: User = Depends(admin), s: Session = Depends(get_session)):
    code = body.code.strip()
    if not code:
        raise HTTPException(400, "code is required")
    if s.get(Bank, code):
        raise HTTPException(409, "bank already exists")
    b = Bank(code=code, name=body.name.strip() or code, active=body.active, thresholds=body.thresholds)
    s.add(b)
    audit(s, user, "bank.create", code, body.model_dump())
    enqueue(s, user, "reevaluate")
    s.commit()
    return bank_json(b, load_config(s))


@router.put("/banks/{code}")
def update_bank(code: str, body: BankIn, user: User = Depends(admin), s: Session = Depends(get_session)):
    b = s.get(Bank, code)
    if b is None:
        raise HTTPException(404, "bank not found")
    merged = {**load_config(s).thresholds_for(None), **body.thresholds}
    _check("thresholds", merged)
    before = {"name": b.name, "active": b.active, "thresholds": b.thresholds}
    b.name, b.active, b.thresholds, b.updated_at = body.name, body.active, body.thresholds, now()
    audit(s, user, "bank.update", code, {"before": before, "after": body.model_dump()})
    cmd = enqueue(s, user, "reevaluate")
    s.commit()
    return {**bank_json(b, load_config(s)), "command_id": cmd.id}


@router.delete("/banks/{code}")
def delete_bank(code: str, user: User = Depends(admin), s: Session = Depends(get_session)):
    b = s.get(Bank, code)
    if b is None:
        raise HTTPException(404, "bank not found")
    s.delete(b)
    audit(s, user, "bank.delete", code)
    enqueue(s, user, "reevaluate")
    s.commit()
    return {"ok": True}


@router.get("/config/export")
def export_config(user: User = Depends(admin), s: Session = Depends(get_session)):
    audit(s, user, "config.export")
    s.commit()
    return {"yaml": export_yaml(s)}


class ImportIn(BaseModel):
    yaml: str
    include_channels: bool = False


@router.post("/config/import")
def import_config(body: ImportIn, user: User = Depends(admin), s: Session = Depends(get_session)):
    try:
        data = yaml.safe_load(body.yaml) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(400, f"invalid YAML: {exc}")
    if not isinstance(data, dict):
        raise HTTPException(400, "the YAML document must be a mapping")
    current = load_config(s).data
    merged = {k: data.get(k, current[k]) for k in SECTIONS}
    merged["banks"] = data.get("banks", current["banks"])
    merged["channels"] = data.get("channels", []) if body.include_channels else []
    _check("thresholds", {**DEFAULT_CONFIG["thresholds"], **merged["thresholds"]})
    apply_config_data(s, merged, user.username, channels=body.include_channels)
    audit(s, user, "config.import", None, {"include_channels": body.include_channels})
    enqueue(s, user, "reevaluate")
    s.commit()
    return {"ok": True}


@router.post("/config/reevaluate")
def reevaluate(user: User = Depends(admin), s: Session = Depends(get_session)):
    cmd = enqueue(s, user, "reevaluate")
    audit(s, user, "engine.reevaluate")
    s.commit()
    return {"command_id": cmd.id}

