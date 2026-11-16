from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from banksealer.channels import CHANNEL_TYPES

from ...db import get_session
from ...models import ChannelConfig, ChannelStatus, PreparationError, User
from ..deps import admin, audit, viewer

router = APIRouter(prefix="/api/channels", tags=["channels"])


class ChannelIn(BaseModel):
    name: str
    type: str
    description: Optional[str] = None
    enabled: bool = False
    options: dict = Field(default_factory=dict)
    field_mapping: dict = Field(default_factory=dict)
    preparation: dict = Field(default_factory=dict)


def channel_json(c: ChannelConfig, st: Optional[ChannelStatus]) -> dict:
    return {
        "id": c.id, "name": c.name, "type": c.type, "description": c.description, "enabled": c.enabled,
        "options": c.options or {}, "field_mapping": c.field_mapping or {}, "preparation": c.preparation or {},
        "version": c.version, "updated_at": c.updated_at.isoformat(timespec="seconds") if c.updated_at else None,
        "runtime": {
            "status": (st.status if st else "stopped") if c.enabled else "disabled",
            "received": st.received if st else 0, "accepted": st.accepted if st else 0,
            "rejected": st.rejected if st else 0, "duplicates": st.duplicates if st else 0,
            "history": st.history if st else 0, "last_error": st.last_error if st else None,
            "heartbeat": st.heartbeat.isoformat(timespec="seconds") if st and st.heartbeat else None,
        },
    }


def _validate(body: ChannelIn) -> dict:
    if body.type not in CHANNEL_TYPES:
        raise HTTPException(400, f"unknown channel type; choose one of {', '.join(CHANNEL_TYPES)}")
    if not body.name.strip():
        raise HTTPException(400, "name is required")
    options = {**CHANNEL_TYPES[body.type].default_options, **(body.options or {})}
    if body.type == "sqlite" and not str(options.get("table", "")).replace("_", "").isalnum():
        raise HTTPException(400, "invalid table name")
    for key in ("port",):
        if key in options:
            try:
                options[key] = int(options[key])
            except (TypeError, ValueError):
                raise HTTPException(400, f"{key} must be a number")
    return options


@router.get("/types")
def channel_types(_: User = Depends(viewer)):
    return [{"type": name, "description": cls.description, "default_options": cls.default_options}
            for name, cls in CHANNEL_TYPES.items()]


@router.get("")
def list_channels(_: User = Depends(viewer), s: Session = Depends(get_session)):
    rows = s.execute(select(ChannelConfig, ChannelStatus).outerjoin(
        ChannelStatus, ChannelStatus.channel_id == ChannelConfig.id).order_by(ChannelConfig.id)).all()
    return [channel_json(c, st) for c, st in rows]


@router.get("/{channel_id}")
def get_channel(channel_id: int, _: User = Depends(viewer), s: Session = Depends(get_session)):
    c = s.get(ChannelConfig, channel_id)
    if c is None:
        raise HTTPException(404, "channel not found")
    return channel_json(c, s.get(ChannelStatus, channel_id))


@router.post("")
def create_channel(body: ChannelIn, user: User = Depends(admin), s: Session = Depends(get_session)):
    options = _validate(body)
    if s.scalar(select(ChannelConfig).where(ChannelConfig.name == body.name.strip())):
        raise HTTPException(409, "a channel with this name already exists")
    c = ChannelConfig(name=body.name.strip(), type=body.type, description=body.description, enabled=body.enabled,
                      options=options, field_mapping=body.field_mapping, preparation=body.preparation, version=1)
    s.add(c)
    s.flush()
    s.add(ChannelStatus(channel_id=c.id))
    audit(s, user, "channel.create", c.name, body.model_dump())
    s.commit()
    return channel_json(c, s.get(ChannelStatus, c.id))


@router.put("/{channel_id}")
def update_channel(channel_id: int, body: ChannelIn, user: User = Depends(admin), s: Session = Depends(get_session)):
    c = s.get(ChannelConfig, channel_id)
    if c is None:
        raise HTTPException(404, "channel not found")
    options = _validate(body)
    clash = s.scalar(select(ChannelConfig).where(ChannelConfig.name == body.name.strip(), ChannelConfig.id != channel_id))
    if clash:
        raise HTTPException(409, "a channel with this name already exists")
    before = {"options": c.options, "field_mapping": c.field_mapping, "preparation": c.preparation, "enabled": c.enabled}
    c.name, c.type, c.description, c.enabled = body.name.strip(), body.type, body.description, body.enabled
    c.options, c.field_mapping, c.preparation = options, body.field_mapping, body.preparation
    c.version = (c.version or 0) + 1      # the worker restarts the channel with the new configuration
    audit(s, user, "channel.update", c.name, {"before": before, "after": body.model_dump()})
    s.commit()
    return channel_json(c, s.get(ChannelStatus, c.id))


@router.post("/{channel_id}/{action}")
def channel_action(channel_id: int, action: str, user: User = Depends(admin), s: Session = Depends(get_session)):
    c = s.get(ChannelConfig, channel_id)
    if c is None:
        raise HTTPException(404, "channel not found")
    if action == "start":
        c.enabled = True
    elif action == "stop":
        c.enabled = False
    elif action == "restart":
        c.enabled = True
        c.version = (c.version or 0) + 1
    else:
        raise HTTPException(400, "action must be start, stop or restart")
    audit(s, user, f"channel.{action}", c.name)
    s.commit()
    return channel_json(c, s.get(ChannelStatus, c.id))


@router.delete("/{channel_id}")
def delete_channel(channel_id: int, user: User = Depends(admin), s: Session = Depends(get_session)):
    c = s.get(ChannelConfig, channel_id)
    if c is None:
        raise HTTPException(404, "channel not found")
    s.execute(delete(ChannelStatus).where(ChannelStatus.channel_id == channel_id))
    s.delete(c)
    audit(s, user, "channel.delete", c.name)
    s.commit()
    return {"ok": True}


@router.get("/{channel_id}/errors")
def channel_errors(channel_id: int, limit: int = 100, _: User = Depends(viewer), s: Session = Depends(get_session)):
    c = s.get(ChannelConfig, channel_id)
    if c is None:
        raise HTTPException(404, "channel not found")
    rows = s.scalars(select(PreparationError).where(PreparationError.channel == c.name)
                     .order_by(PreparationError.id.desc()).limit(limit))
    total = s.scalar(select(func.count(PreparationError.id)).where(PreparationError.channel == c.name))
    return {"total": total, "items": [{"id": r.id, "message": r.message, "raw": r.raw,
                                       "created_at": r.created_at.isoformat(timespec="seconds")} for r in rows]}
