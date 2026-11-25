import os
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_session
from ...models import ChannelConfig, Command, EngineStatus, User
from ...settings import settings
from ..deps import admin, analyst, audit, enqueue, viewer
from .dashboard import engine_json

router = APIRouter(prefix="/api/engine", tags=["engine"])


def command_json(c: Command) -> dict:
    return {"id": c.id, "kind": c.kind, "status": c.status, "payload": c.payload, "result": c.result,
            "created_by": c.created_by, "created_at": c.created_at.isoformat(timespec="seconds"),
            "finished_at": c.finished_at.isoformat(timespec="seconds") if c.finished_at else None}


@router.get("/status")
def status(_: User = Depends(viewer), s: Session = Depends(get_session)):
    return engine_json(s.get(EngineStatus, 1))


@router.get("/commands")
def commands(limit: int = 30, _: User = Depends(viewer), s: Session = Depends(get_session)):
    return [command_json(c) for c in s.scalars(select(Command).order_by(Command.id.desc()).limit(limit))]


@router.get("/commands/{command_id}")
def command(command_id: int, _: User = Depends(viewer), s: Session = Depends(get_session)):
    c = s.get(Command, command_id)
    if c is None:
        raise HTTPException(404, "command not found")
    return command_json(c)


@router.post("/retrain")
def retrain(user: User = Depends(analyst), s: Session = Depends(get_session)):
    cmd = enqueue(s, user, "retrain")
    audit(s, user, "engine.retrain")
    s.commit()
    return command_json(cmd)


@router.post("/upload")
def upload(file: UploadFile = File(...), mode: str = Form("train"), channel_id: Optional[int] = Form(None),
           user: User = Depends(analyst), s: Session = Depends(get_session)):
    """Upload a CSV/JSON(L) file: ``mode=train`` loads history and refits, ``mode=score`` scores it."""
    if mode not in ("train", "score"):
        raise HTTPException(400, "mode must be train or score")
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(file.filename or "upload.csv"))
    if not name.lower().endswith((".csv", ".jsonl", ".json")):
        raise HTTPException(400, "only .csv, .json and .jsonl files are accepted")
    path = os.path.join(settings.uploads_dir, f"{int(time.time())}_{name}")
    os.makedirs(settings.uploads_dir, exist_ok=True)
    with open(path, "wb") as fh:
        while chunk := file.file.read(1 << 20):
            fh.write(chunk)
    payload = {"path": path}
    if channel_id is not None:
        c = s.get(ChannelConfig, channel_id)
        if c is None:
            raise HTTPException(404, "channel not found")
        payload.update(field_mapping=c.field_mapping or {}, preparation=c.preparation or {})
    cmd = enqueue(s, user, "train_file" if mode == "train" else "score_file", payload)
    audit(s, user, f"engine.upload_{mode}", name, {"channel_id": channel_id})
    s.commit()
    return command_json(cmd)


@router.post("/reset")
def reset(user: User = Depends(admin), s: Session = Depends(get_session)):
    cmd = enqueue(s, user, "reset_model")
    audit(s, user, "engine.reset")
    s.commit()
    return command_json(cmd)


@router.post("/purge")
def purge(user: User = Depends(admin), s: Session = Depends(get_session)):
    cmd = enqueue(s, user, "purge_data")
    audit(s, user, "engine.purge_data")
    s.commit()
    return command_json(cmd)
