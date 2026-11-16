"""Configuration database: load/seed/export the BankSealer configuration."""
from __future__ import annotations

import hashlib
import logging
import os

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from banksealer.config import DEFAULT_CONFIG, Config

from .models import AuditLog, Bank, ChannelConfig, ChannelStatus, Setting, User
from .security import hash_password

SECTIONS = ("thresholds", "engine", "local", "temporal", "global", "app")
log = logging.getLogger("banksealer.config")


def load_config(session: Session) -> Config:
    data = {row.key: row.value for row in session.scalars(select(Setting))}
    data["banks"] = {b.code: {"name": b.name, "thresholds": b.thresholds or {}}
                     for b in session.scalars(select(Bank).where(Bank.active.is_(True)))}
    data["channels"] = [channel_spec(c) for c in session.scalars(select(ChannelConfig).order_by(ChannelConfig.id))]
    return Config(data)


def config_version(session: Session) -> str:
    """Changes whenever settings or banks change (used by the worker to hot-reload)."""
    parts = [
        session.scalar(select(func.max(Setting.updated_at))),
        session.scalar(select(func.max(Bank.updated_at))),
        session.scalar(select(func.count(Bank.code))),
        session.scalar(select(func.count(Bank.code)).where(Bank.active.is_(True))),
    ]
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def channel_spec(c: ChannelConfig) -> dict:
    return {"id": c.id, "name": c.name, "type": c.type, "enabled": c.enabled, "options": c.options or {},
            "field_mapping": c.field_mapping or {}, "preparation": c.preparation or {}, "version": c.version}


def save_section(session: Session, key: str, value: dict, user: str | None = None) -> None:
    if key not in SECTIONS:
        raise ValueError(f"unknown section {key}")
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value, updated_by=user))
    else:
        row.value = value
        row.updated_by = user
        from .models import now
        row.updated_at = now()


def apply_config_data(session: Session, data: dict, user: str | None = None, channels: bool = True) -> None:
    """Write a whole configuration dictionary (YAML import / seed)."""
    cfg = Config(data)
    for key in SECTIONS:
        save_section(session, key, cfg.data[key], user)
    existing = {b.code: b for b in session.scalars(select(Bank))}
    for code, entry in (cfg.banks or {}).items():
        entry = entry or {}
        bank = existing.get(code) or Bank(code=code)
        bank.name = entry.get("name") or code
        bank.thresholds = entry.get("thresholds") or {}
        bank.active = entry.get("active", True)
        session.add(bank)
    if channels:
        names = {c.name: c for c in session.scalars(select(ChannelConfig))}
        for spec in cfg.channels:
            ch = names.get(spec["name"]) or ChannelConfig(name=spec["name"])
            ch.type = spec["type"]
            ch.description = spec.get("description")
            ch.enabled = bool(spec.get("enabled", False))
            ch.options = spec.get("options") or {}
            ch.field_mapping = spec.get("field_mapping") or {}
            ch.preparation = spec.get("preparation") or {}
            ch.version = (ch.version or 0) + 1
            session.add(ch)


def export_yaml(session: Session) -> str:
    cfg = load_config(session)
    data = {k: cfg.data[k] for k in SECTIONS}
    data["banks"] = cfg.data["banks"]
    data["channels"] = [{k: v for k, v in spec.items() if k not in ("id", "version")} for spec in cfg.data["channels"]]
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def seed(session: Session, path: str, admin_user: str, admin_password: str) -> None:
    """First start: load the seed YAML into the configuration database and create the admin user."""
    if session.scalar(select(func.count(Setting.key))) == 0:
        data = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            log.info("seeding configuration database from %s", path)
        else:
            data = DEFAULT_CONFIG
        apply_config_data(session, data, "system")
        session.add(AuditLog(username="system", action="config.seed", target=path))
    if session.scalar(select(func.count(User.id))) == 0:
        session.add(User(username=admin_user, full_name="Administrator", role="admin",
                         password_hash=hash_password(admin_password)))
        log.info("created admin user %s", admin_user)
    session.flush()
    for ch in session.scalars(select(ChannelConfig)):
        if session.get(ChannelStatus, ch.id) is None:
            session.add(ChannelStatus(channel_id=ch.id))
