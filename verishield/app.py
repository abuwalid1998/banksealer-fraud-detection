"""FastAPI application: API, web UI, middleware, background jobs."""
from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import admin, auth, cases, core, governance, intel, system
from .models import Tenant
from .services.metrics import metrics
from .settings import settings

log = logging.getLogger("verishield")
WEB = Path(__file__).resolve().parent / "web"
MAX_BODY = 110 * 1024 * 1024


class _Jobs:
    def __init__(self):
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if not settings.background_jobs:
            return
        self.thread = threading.Thread(target=self._run, name="verishield-jobs", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        from .db import SessionLocal
        from .services import decision, engine_registry
        last_save = last_health = time.time()
        while not self.stop.wait(30):
            try:
                with SessionLocal() as db:
                    decision.expire_holds(db)
                with SessionLocal() as db:
                    from .services import cases as case_service
                    from .services import confirmations, operations
                    confirmations.expire(db)
                    operations.run_due_schedules(db)
                    if time.time() - last_health > 1800:
                        from .services import model_ops
                        model_ops.check_all(db)
                        case_service.escalate_due(db)
                        operations.check_quality(db)
                        for t in db.execute(__import__("sqlalchemy").select(Tenant)).scalars():
                            operations.schedule_reviews(db, t.id, "LIVE")
                            operations.schedule_reviews(db, t.id, "DEMO")
                        last_health = time.time()
                if time.time() - last_save > 300:
                    engine_registry.save_all()
                    last_save = time.time()
            except Exception as exc:  # noqa: BLE001
                log.warning("background job error: %s", exc)

    def shutdown(self) -> None:
        self.stop.set()


jobs = _Jobs()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .bootstrap import initialise
    from .services import engine_registry
    from .services.connectors import runtime

    summary = initialise()
    app.state.bootstrap = summary
    jobs.start()
    log.info("VeriShield %s ready (%s, tenant %s)", __version__, settings.env, settings.tenant_code)
    yield
    jobs.shutdown()
    runtime.stop_all()
    engine_registry.save_all()


def create_app() -> FastAPI:
    app = FastAPI(title="VeriShield API", version=__version__, lifespan=lifespan,
                  description="Transaction Intelligence. Trusted Decisions. - fraud risk decisioning, customer "
                              "intelligence and AML case management. Authenticate users with `POST /api/v1/auth/login` "
                              "(Bearer token) and systems with an `X-API-Key` header.",
                  docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")

    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                           allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def guard(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY:
            return JSONResponse({"detail": {"code": "PAYLOAD_TOO_LARGE", "message": "request body too large"}}, 413)
        started = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - started) * 1000
        path = request.url.path
        if path.startswith("/api/"):
            metrics.observe("api", ms)
            metrics.inc(f"http_requests:{response.status_code // 100}xx")
            if response.status_code in (401, 403):
                metrics.inc("auth_failures")
            if response.status_code >= 500:
                metrics.error("api", f"{request.method} {path} -> {response.status_code}")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Response-Time-ms"] = f"{ms:.1f}"
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if settings.ssl_certfile:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        errors = [{"field": ".".join(str(x) for x in e.get("loc", [])[1:]), "message": e.get("msg")}
                  for e in exc.errors()]
        return JSONResponse({"detail": {"code": "VALIDATION_ERROR", "message": "; ".join(
            f"{e['field']}: {e['message']}" for e in errors), "errors": errors}}, 422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        if isinstance(exc, HTTPException):
            raise exc
        log.exception("unhandled error on %s", request.url.path)
        metrics.error("unhandled", f"{type(exc).__name__}: {exc}")
        return JSONResponse({"detail": {"code": "INTERNAL_ERROR", "message": "Unexpected error - see server log",
                                        "type": type(exc).__name__}}, 500)

    for module in (intel, auth, admin, core, cases, governance, system):
        app.include_router(module.router)
    app.include_router(admin.health_router)
    app.include_router(system.mcp_router)

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/api/docs", include_in_schema=False)
    def api_docs():
        from fastapi.openapi.docs import get_swagger_ui_html
        # assets are served locally so the documentation also works on servers without internet access
        return get_swagger_ui_html(openapi_url="/api/openapi.json", title="VeriShield API",
                                   swagger_js_url="/static/vendor/swagger-ui/swagger-ui-bundle.js",
                                   swagger_css_url="/static/vendor/swagger-ui/swagger-ui.css",
                                   swagger_favicon_url="/static/logo.svg")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(WEB / "logo.svg", media_type="image/svg+xml")

    return app


app = create_app()
