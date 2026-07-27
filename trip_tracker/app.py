import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from trip_tracker.api.deps import WEB_API_AUTH_EXEMPT_PATHS, verify_web_api_auth
from trip_tracker.api.routes import router as api_router
from trip_tracker.config import get_settings
from trip_tracker.database import engine, is_database_unavailable_error
from trip_tracker.logging_config import configure_logging
from trip_tracker.models import Base
from trip_tracker.services.app_health import AppHealthMonitor
from trip_tracker.services.backups import automatic_backup_scheduler
from trip_tracker.services.gas_prices import gas_snapshot_scheduler
from trip_tracker.services.runtime_status import build_runtime_status
from trip_tracker.services.trip_processor import AutomaticTripProcessor
from trip_tracker.web.auth import enforce_web_login
from trip_tracker.web.routes import router as web_router
from trip_tracker.web.routes import templates

settings = get_settings()
trip_processor = AutomaticTripProcessor()
app_health_monitor = AppHealthMonitor()
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging("app")
    logger.info("Starting Trip Tracker app with console logging")
    if settings.create_tables_on_startup:
        try:
            Base.metadata.create_all(bind=engine)
        except Exception as exc:
            if not is_database_unavailable_error(exc):
                raise
            logger.warning(
                "Database unavailable during create_tables_on_startup; starting in limp mode"
            )
    if settings.automatic_backups_enabled:
        _app.state.automatic_backup_task = asyncio.create_task(
            automatic_backup_scheduler(settings)
        )
    if settings.gas_snapshot_enabled:
        _app.state.gas_snapshot_task = asyncio.create_task(gas_snapshot_scheduler(settings))
    trip_processor.start()
    app_health_monitor.start()
    try:
        yield
    finally:
        logger.info("Stopping Trip Tracker app")
        gas_snapshot_task = getattr(_app.state, "gas_snapshot_task", None)
        if gas_snapshot_task is not None:
            gas_snapshot_task.cancel()
            with suppress(asyncio.CancelledError):
                await gas_snapshot_task
        backup_task = getattr(_app.state, "automatic_backup_task", None)
        if backup_task is not None:
            backup_task.cancel()
            with suppress(asyncio.CancelledError):
                await backup_task
        trip_processor.stop()
        app_health_monitor.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)


def _is_limp_mode_fragment_request(request: Request) -> bool:
    """Return true when JavaScript content loaders expect an HTML fragment."""

    return request.headers.get("x-requested-with", "").casefold() == "fetch"


def _limp_mode_template_response(request: Request, *, fragment: bool):
    """Build a database-outage response without querying PostgreSQL."""

    runtime_status = build_runtime_status(settings, database_available=False)
    template_name = "_limp_mode_panel.html" if fragment else "limp_mode.html"
    return templates.TemplateResponse(
        request,
        template_name,
        {
            "settings": settings,
            "runtime_status": runtime_status,
            "limp_mode_active": True,
        },
        headers={"X-Trip-Tracker-Limp-Mode": "true"},
    )


@app.middleware("http")
async def database_limp_mode(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        if not is_database_unavailable_error(exc):
            raise
        logger.warning(
            "Database unavailable; returning limp-mode response path=%s",
            request.url.path,
        )
        if request.url.path == "/api" or request.url.path.startswith("/api/"):
            return JSONResponse(
                content={
                    "detail": "Database is unavailable. Try the request again later."
                },
                status_code=503,
                headers={
                    "Cache-Control": "no-store",
                    "Retry-After": "30",
                    "X-Trip-Tracker-Limp-Mode": "true",
                },
            )
        return _limp_mode_template_response(
            request,
            fragment=_is_limp_mode_fragment_request(request),
        )


@app.middleware("http")
async def require_web_login(request: Request, call_next):
    return await enforce_web_login(request, call_next)


@app.middleware("http")
async def require_web_api_bearer_auth(request: Request, call_next):
    path = request.url.path
    if (path == "/api" or path.startswith("/api/")) and path not in WEB_API_AUTH_EXEMPT_PATHS:
        try:
            verify_web_api_auth(request)
        except HTTPException as exc:
            return JSONResponse(
                content={"detail": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )
    return await call_next(request)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "request method=%s path=%s status=%s duration_ms=%.1f",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="trip_tracker_session",
    same_site="lax",
    https_only=settings.web_session_cookie_secure,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(api_router, prefix="/api", tags=["api"])
app.include_router(web_router, tags=["web"])
