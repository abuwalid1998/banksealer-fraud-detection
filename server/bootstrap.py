"""Schema creation + first-start seeding, safe when api and worker start concurrently."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from . import db
from .config_store import seed
from .db import Base
from .settings import settings

log = logging.getLogger("banksealer.bootstrap")


def init_database() -> None:
    settings.ensure_dirs()
    db.wait_for_db()
    with db.engine.connect() as conn:
        postgres = conn.dialect.name == "postgresql"
        if postgres:
            conn.exec_driver_sql("SELECT pg_advisory_lock(727001)")
        try:
            Base.metadata.create_all(conn)
            conn.commit()
            with Session(bind=conn) as session:
                seed(session, settings.seed_config, settings.admin_username, settings.admin_password)
                session.commit()
        finally:
            if postgres:
                conn.exec_driver_sql("SELECT pg_advisory_unlock(727001)")
                conn.commit()
    log.info("database ready")
