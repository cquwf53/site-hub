from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from site_hub.api import router
from site_hub.api.security import LocalRequestSecurityMiddleware, error_response
from site_hub.config import Settings
from site_hub.jobs.dispatcher import JobDispatcher
from site_hub.jobs.service import JobConflict, JobNotFound, JobService, StaleLease
from site_hub.jobs.system_dispatcher import BookmarkStaging, SystemJobDispatcher
from site_hub.notifications import (
    NotificationChannelService,
    NotificationOutboxExecutor,
    NotificationOutboxStore,
)
from site_hub.scrapers.network import sanitize_log_value
from site_hub.scheduling.coordinator import ScheduleCoordinator
from site_hub.services.backups import BackupService
from site_hub.services.maintenance import MaintenanceService
from site_hub.services.data_store import DataLayerStore
from site_hub.services.ports import AccountNotReady
from site_hub.services.events import EventBroker
from site_hub.services.ports import Store
from site_hub.services.single_instance import SingleInstanceLock
from site_hub.workers.supervisor import NodeWorkerSupervisor


logger = logging.getLogger(__name__)


def create_app(*, settings: Settings | None = None, store: Store | None = None,
               start_background: bool = True, product_handler=None) -> FastAPI:
    runtime = settings or Settings()
    durable_store = store or DataLayerStore(runtime.db_path)
    events = EventBroker()
    jobs = JobService(durable_store, events, lease_seconds=runtime.worker_lease_seconds)
    if product_handler is None and hasattr(durable_store, "get_product_runtime"):
        from site_hub.scrapers import ProductScraperService
        product_handler = ProductScraperService(durable_store)
    scheduler = ScheduleCoordinator(
        durable_store, jobs, tick_seconds=runtime.scheduler_tick_seconds,
        timezone=runtime.checkin_timezone,
    )
    supervisor = NodeWorkerSupervisor(
        jobs, executable=runtime.node_executable, entry=runtime.node_entry,
        profiles_dir=runtime.profiles_dir, artifacts_dir=runtime.artifacts_dir,
        handshake_seconds=runtime.worker_handshake_seconds, ping_seconds=runtime.worker_ping_seconds,
        shutdown_seconds=runtime.worker_shutdown_seconds,
    )
    dispatcher = JobDispatcher(
        jobs, product_handler=product_handler, product_concurrency=runtime.scrape_concurrency,
    )
    bookmark_staging = BookmarkStaging(runtime.data_dir / "staging")
    backups = BackupService(runtime.data_dir)
    system_dispatcher = SystemJobDispatcher(
        jobs, supervisor=supervisor, staging=bookmark_staging,
    )
    instance_lock = SingleInstanceLock(runtime.data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        instance_lock.acquire()
        store_initialized = False
        restored = None
        scheduler_started = dispatcher_started = system_dispatcher_started = supervisor_started = False
        notification_task: asyncio.Task[None] | None = None
        notification_stop = asyncio.Event()
        try:
            runtime.prepare_directories()
            # Startup is not accepting requests yet, so keep this synchronous.
            # A detached restore thread could outlive lifespan cancellation and
            # the process mutex, allowing a second instance to touch the DB.
            restored = backups.apply_pending_restore_with_instance_lock()
            if restored is not None:
                logger.info(
                    "restored database backup %s; rollback backup %s",
                    restored.restored_backup.backup_id,
                    restored.rollback_backup.backup_id,
                )
            try:
                if restored is None and runtime.db_path.is_file():
                    # Preserve the on-disk schema before applying an irreversible migration.
                    from site_hub.db.schema import SCHEMA_VERSION
                    with sqlite3.connect(runtime.db_path.resolve().as_uri() + "?mode=ro", uri=True) as existing:
                        previous_version = existing.execute("PRAGMA user_version").fetchone()[0]
                    if 0 < previous_version < SCHEMA_VERSION:
                        backups.create_backup(label=f"Before schema upgrade {previous_version} -> {SCHEMA_VERSION}")
                await durable_store.initialize()
                store_initialized = True
                await jobs.recover()
                database = getattr(durable_store, "database", None)
                secret_service = getattr(durable_store, "secret_service", None)
                if database is not None and secret_service is not None:
                    channel_service = NotificationChannelService(database, secret_service)
                    outbox_store = NotificationOutboxStore(database)
                    notification_executor = NotificationOutboxExecutor(
                        outbox_store, secret_service
                    )
                    app.state.notification_channels = channel_service
                    app.state.notification_executor = notification_executor
                    await asyncio.to_thread(outbox_store.recover_expired)
                if database is not None:
                    maintenance = MaintenanceService(database, backups, runtime.data_dir)
                    await asyncio.to_thread(maintenance.initialize)
                    app.state.maintenance = maintenance
                if restored is not None:
                    backups.confirm_pending_restore(restored.restored_backup.backup_id)
            except BaseException:
                if restored is not None:
                    # Database connections are short-lived, but close the store
                    # contract before replacing the file and keep the process
                    # lock held throughout rollback.
                    try:
                        await durable_store.close()
                    finally:
                        store_initialized = False
                    backups.rollback_applied_restore_with_instance_lock(restored)
                    logger.exception(
                        "restored database failed startup validation; original database rolled back"
                    )
                raise
            if start_background:
                if app.state.maintenance is not None:
                    await app.state.maintenance.start()
                supervisor.start(); supervisor_started = True
                scheduler.start(); scheduler_started = True
                dispatcher.start(); dispatcher_started = True
                system_dispatcher.start(); system_dispatcher_started = True
                if app.state.notification_executor is not None:
                    notification_task = asyncio.create_task(
                        app.state.notification_executor.run_forever(notification_stop),
                        name="notification-outbox",
                    )
                    app.state.notification_task = notification_task
            yield
        finally:
            try:
                notification_stop.set()
                # Stop producers before consumers, and finish maintenance threads
                # while the database and instance lock are still owned.
                if scheduler_started:
                    await asyncio.gather(scheduler.stop(), return_exceptions=True)
                if app.state.maintenance is not None:
                    await asyncio.gather(app.state.maintenance.stop(), return_exceptions=True)
                if system_dispatcher_started:
                    await asyncio.gather(system_dispatcher.stop(), return_exceptions=True)
                if dispatcher_started:
                    await asyncio.gather(dispatcher.stop(), return_exceptions=True)
                if supervisor_started:
                    await asyncio.gather(supervisor.stop(), return_exceptions=True)
                if notification_task is not None:
                    await asyncio.gather(notification_task, return_exceptions=True)
                pending = tuple(app.state.background_tasks)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            finally:
                try:
                    if store_initialized:
                        await durable_store.close()
                finally:
                    instance_lock.release()

    app = FastAPI(
        title="SiteHub", version="1.0.0", docs_url=None, redoc_url=None,
        openapi_url="/api/v1/openapi.json", lifespan=lifespan,
    )
    app.state.settings = runtime
    app.state.store = durable_store
    app.state.events = events
    app.state.jobs = jobs
    app.state.scheduler = scheduler
    app.state.dispatcher = dispatcher
    app.state.system_dispatcher = system_dispatcher
    app.state.bookmark_staging = bookmark_staging
    app.state.backups = backups
    app.state.maintenance = None
    app.state.notification_channels = None
    app.state.notification_executor = None
    app.state.notification_task = None
    app.state.supervisor = supervisor
    app.state.instance_lock = instance_lock
    app.state.started_at = datetime.now(UTC)
    app.state.start_background = start_background
    app.state.background_tasks: set = set()

    app.add_middleware(LocalRequestSecurityMiddleware, settings=runtime)
    app.include_router(router)
    _install_error_handlers(app)
    _install_spa(app, runtime.frontend_dir)
    return app


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(item) for item in first.get("loc", [])[1:]) or None
        return error_response(422, "VALIDATION_ERROR", first.get("msg", "Request validation failed"),
                              request_id=getattr(request.state, "request_id", None), field=field)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return error_response(
            exc.status_code, str(detail.get("code", "HTTP_ERROR")), str(detail.get("message", "Request failed")),
            request_id=getattr(request.state, "request_id", None), field=detail.get("field"),
            context=detail.get("context"),
        )

    @app.exception_handler(JobNotFound)
    async def job_not_found(request: Request, exc: JobNotFound):
        return error_response(404, "NOT_FOUND", str(exc), request_id=getattr(request.state, "request_id", None))

    async def job_conflict(request: Request, exc: Exception):
        return error_response(409, "JOB_CONFLICT", str(exc), request_id=getattr(request.state, "request_id", None))
    app.add_exception_handler(JobConflict, job_conflict)
    app.add_exception_handler(StaleLease, job_conflict)

    @app.exception_handler(AccountNotReady)
    async def account_not_ready(request: Request, exc: AccountNotReady):
        return error_response(
            409, "ACCOUNT_NOT_READY", str(exc),
            request_id=getattr(request.state, "request_id", None), field="enabled",
        )

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(request: Request, exc: sqlite3.IntegrityError):
        return error_response(409, "CONFLICT", "The requested value conflicts with existing data",
                              request_id=getattr(request.state, "request_id", None))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        logger.error("request failed: %s", sanitize_log_value(exc))
        return error_response(500, "INTERNAL_ERROR", "An internal error occurred",
                              request_id=getattr(request.state, "request_id", None))


def _install_spa(app: FastAPI, frontend_dir: Path) -> None:
    root = Path(frontend_dir).resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/"):
            return JSONResponse({"error": {"code": "NOT_FOUND", "message": "API endpoint not found"}}, status_code=404)
        target = (root / path).resolve()
        if path and root in target.parents and target.is_file():
            return FileResponse(target)
        index = root / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            {"error": {"code": "FRONTEND_NOT_BUILT", "message": "Frontend assets are not built"}},
            status_code=503,
        )


app = create_app()


def run() -> None:
    import uvicorn
    runtime = app.state.settings
    # Uvicorn's stock access formatter includes the raw query string. SiteHub
    # treats every query/fragment value as sensitive, so request access logging
    # stays disabled instead of risking plaintext tokens in the console/logs.
    uvicorn.run(app, host=runtime.host, port=runtime.port, log_level="info", access_log=False)


if __name__ == "__main__":
    run()
