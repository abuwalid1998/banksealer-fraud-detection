"""BankSealer Enterprise REST API."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .. import __version__, db
from ..bootstrap import init_database
from ..settings import settings
from .routers import auth, channels, config, customers, dashboard, engine, preparation, transactions, users

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")



@asynccontextmanager
async def lifespan(_app):
    if os.environ.get("SKIP_DB_INIT") != "1":
        init_database()
    yield


app = FastAPI(title="BankSealer Enterprise API", version=__version__, lifespan=lifespan,
              description="Online-banking fraud analysis (BankSealer: local HBOS, temporal and global "
                          "DBSCAN/CBLOF profiles). Authenticate with POST /api/auth/login.",
              docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])
for module in (auth, users, dashboard, transactions, customers, channels, preparation, config, engine):
    app.include_router(module.router)


@app.get("/api/health", tags=["system"])
def health():
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        database = "ok"
    except Exception as exc:  # noqa: BLE001
        database = f"error: {exc}"
    return {"status": "ok" if database == "ok" else "degraded", "database": database, "version": __version__}
