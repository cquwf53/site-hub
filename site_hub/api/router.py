from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from site_hub.jobs.service import JobConflict, JobNotFound, JobService
from site_hub.models import (
    BackupCreateRequest, BackupRestoreRequest,
    CheckinAccountCreate, CheckinAccountUpdate, JobKind, JobStatus,
    ProductCategoryAssignment, ProductCategoryCreate, ProductCategoryUpdate,
    ProductSettingsUpdate, ScheduleSettingsUpdate, SiteCreate, SiteUpdate,
)
from site_hub.services.backups import (
    BackupError, BackupNotFoundError, BackupPathError, BackupValidationError,
)


router = APIRouter(prefix="/api/v1")


def _store(request: Request):
    return request.app.state.store


def _jobs(request: Request) -> JobService:
    return request.app.state.jobs


def _backups(request: Request):
    return request.app.state.backups


def _raise_backup_error(exc: Exception) -> None:
    if isinstance(exc, BackupNotFoundError):
        status, code = 404, "BACKUP_NOT_FOUND"
    elif isinstance(exc, BackupPathError):
        status, code = 400, "INVALID_BACKUP_PATH"
    elif isinstance(exc, BackupValidationError):
        status, code = 409, "BACKUP_INVALID"
    else:
        status, code = 503, "BACKUP_UNAVAILABLE"
    raise HTTPException(
        status,
        detail={"code": code, "message": str(exc)},
    ) from None


def _notification_channels(request: Request):
    service = request.app.state.notification_channels
    if service is None:
        raise HTTPException(
            503,
            detail={
                "code": "SECRET_STORAGE_UNAVAILABLE",
                "message": "Notification channels require Windows DPAPI secret storage",
            },
        )
    return service


def _not_found(message: str = "Resource not found") -> None:
    raise HTTPException(404, detail={"code": "NOT_FOUND", "message": message})


def _page(items: list[dict[str, Any]], page: int, page_size: int, total: int) -> dict[str, Any]:
    return {"items": items, "page": page, "page_size": page_size, "total": total}


def _task_json(task) -> dict[str, Any]:
    return task.model_dump(mode="json", by_alias=True)


def _account_public(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["label"] = result.pop("name", result.get("label", ""))
    result["auth_state"] = result.pop("login_state", result.get("auth_state", "not_configured"))
    result["schedule_override"] = result.pop("schedule", result.get("schedule_override"))
    profile_key = result.pop("profile_key", None)
    if not result.get("profile"):
        result["profile"] = {"relative_path": profile_key} if profile_key else None
    result.pop("runtime_config", None)
    return result


async def _require_adapter_capability(
    request: Request, adapter_id: str | None, capability: str, *,
    field: str = "adapter", allow_none: bool = False,
) -> None:
    """Reject adapters that are unavailable or belong to another capability."""
    normalized = str(adapter_id or "").strip()
    if normalized == "none" and allow_none:
        return
    manifests = await adapters(request)
    manifest = next((item for item in manifests if item["id"] == normalized), None)
    if manifest is None:
        raise HTTPException(422, detail={
            "code": "ADAPTER_NOT_AVAILABLE",
            "message": f"Adapter '{normalized}' is not available",
            "field": field,
        })
    if capability not in manifest.get("capabilities", []):
        raise HTTPException(422, detail={
            "code": "ADAPTER_CAPABILITY_MISMATCH",
            "message": f"Adapter '{normalized}' does not support {capability}",
            "field": field,
        })


@router.get("/bootstrap")
async def bootstrap(request: Request):
    worker = request.app.state.supervisor.status().model_dump(mode="json")
    worker["connected"] = worker["state"] == "ready"
    worker["status"] = "online" if worker["connected"] else "offline"
    return {
        "csrf_token": request.app.state.settings.csrf_token,
        "api_version": "v1",
        "features": {
            "sites": True, "checkin": True, "products": True,
            "encrypted_secrets": getattr(_store(request), "secret_service", None) is not None,
            "node_worker": worker["state"] != "disabled",
            "notifications": request.app.state.notification_channels is not None,
        },
        "worker": worker,
    }


# -- Sites and accounts -------------------------------------------------
@router.get("/sites")
async def list_sites(request: Request, page: int = Query(1, ge=1),
                     page_size: int = Query(20, ge=1, le=200), q: str | None = None,
                     folder_id: str | None = None, tag_id: UUID | None = None):
    items, total = await _store(request).list_sites(page=page, page_size=page_size, q=q,
                                                    folder_id=folder_id, tag_id=tag_id)
    return _page(items, page, page_size, total)


@router.post("/sites", status_code=201)
async def create_site(request: Request, payload: SiteCreate):
    return await _store(request).create_site(payload.model_dump(exclude_unset=True))


@router.get("/sites/{site_id}")
async def get_site(request: Request, site_id: UUID):
    row = await _store(request).get_site(site_id)
    if not row: _not_found("Site not found")
    return row


@router.patch("/sites/{site_id}")
async def update_site(request: Request, site_id: UUID, payload: SiteUpdate):
    if payload.adapter is not None:
        await _require_adapter_capability(
            request, payload.adapter, "product_monitor", allow_none=True,
        )
    row = await _store(request).update_site(site_id, payload.model_dump(exclude_unset=True))
    if not row: _not_found("Site not found")
    return row


@router.delete("/sites/{site_id}", status_code=204)
async def delete_site(request: Request, site_id: UUID):
    if not await _store(request).delete_site(site_id): _not_found("Site not found")
    return Response(status_code=204)


@router.get("/sites/{site_id}/accounts")
@router.get("/sites/{site_id}/checkin-accounts", include_in_schema=False)
async def list_accounts(request: Request, site_id: UUID):
    if not await _store(request).get_site(site_id): _not_found("Site not found")
    return [_account_public(row) for row in await _store(request).list_accounts(site_id)]


@router.post("/sites/{site_id}/accounts", status_code=201)
@router.post("/sites/{site_id}/checkin-accounts", status_code=201, include_in_schema=False)
async def create_account(request: Request, site_id: UUID, payload: CheckinAccountCreate):
    await _require_adapter_capability(request, payload.adapter, "checkin")
    return _account_public(await _store(request).create_account(site_id, payload.model_dump(exclude_unset=True)))


@router.patch("/accounts/{account_id}")
@router.patch("/checkin-accounts/{account_id}", include_in_schema=False)
async def update_account(request: Request, account_id: UUID, payload: CheckinAccountUpdate):
    if payload.adapter is not None:
        await _require_adapter_capability(request, payload.adapter, "checkin")
    row = await _store(request).update_account(account_id, payload.model_dump(exclude_unset=True))
    if not row: _not_found("Check-in account not found")
    return _account_public(row)


@router.delete("/accounts/{account_id}", status_code=204)
@router.delete("/checkin-accounts/{account_id}", status_code=204, include_in_schema=False)
async def delete_account(request: Request, account_id: UUID):
    if not await _store(request).delete_account(account_id): _not_found("Check-in account not found")
    return Response(status_code=204)


@router.post("/accounts/{account_id}/run")
@router.post("/checkin-accounts/{account_id}/run", include_in_schema=False)
async def run_checkin(request: Request, account_id: UUID):
    task, _ = await _jobs(request).enqueue_checkin(account_id)
    return _task_json(task)


@router.post("/accounts/{account_id}/login-session")
@router.post("/checkin-accounts/{account_id}/login-session", include_in_schema=False)
async def open_login_session(request: Request, account_id: UUID):
    task, _ = await _jobs(request).enqueue_login_session(account_id)
    return _task_json(task)


@router.post("/accounts/{account_id}/login-session/verify")
async def verify_login_session(request: Request, account_id: UUID):
    """Queue a read-only browser-profile authentication check."""
    task, _ = await _jobs(request).enqueue_login_session(account_id, action="verify")
    return _task_json(task)


@router.get("/accounts/{account_id}/login-session")
async def login_session_status(request: Request, account_id: UUID):
    return await request.app.state.supervisor.session_status(account_id)


@router.delete("/accounts/{account_id}/login-session")
async def close_login_session(request: Request, account_id: UUID):
    return await request.app.state.supervisor.close_session(account_id)


# -- Products -----------------------------------------------------------
@router.get("/sites/{site_id}/product-settings")
async def get_product_settings(request: Request, site_id: UUID):
    row = await _store(request).get_product_settings(site_id)
    if not row: _not_found("Site not found")
    return row


@router.put("/sites/{site_id}/product-settings")
async def update_product_settings(request: Request, site_id: UUID, payload: ProductSettingsUpdate):
    if not await _store(request).get_site(site_id):
        _not_found("Site not found")
    if payload.scraper_type is not None:
        await _require_adapter_capability(
            request, payload.scraper_type, "product_monitor",
            field="scraper_type", allow_none=True,
        )
    return await _store(request).update_product_settings(site_id, payload.model_dump(exclude_unset=True))


@router.post("/sites/{site_id}/sync")
async def sync_products(request: Request, site_id: UUID):
    task, _ = await _jobs(request).enqueue_product_sync(site_id)
    return _task_json(task)


@router.post("/endpoints/{endpoint_id}/open", status_code=204)
async def open_endpoint(request: Request, endpoint_id: UUID):
    method = getattr(_store(request), "get_endpoint_runtime", None)
    endpoint = await method(endpoint_id) if method else None
    if not endpoint: _not_found("Endpoint not found")
    url = str(endpoint["url"])
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
        raise HTTPException(400, detail={"code": "INVALID_ENDPOINT", "message": "Endpoint URL is not safe to open"})

    def launch() -> None:
        if sys.platform == "win32": os.startfile(url)  # type: ignore[attr-defined]
        else:
            import webbrowser
            if not webbrowser.open(url, new=2): raise RuntimeError("no browser accepted the endpoint")
    await asyncio.to_thread(launch)
    return Response(status_code=204)


@router.get("/products")
async def list_products(request: Request, page: int = Query(1, ge=1),
                        page_size: int = Query(20, ge=1, le=200), site_id: UUID | None = None,
                        q: str | None = Query(None, max_length=200),
                        availability_status: Literal[
                            "available", "sold_out", "missing", "delisted", "unknown"
                        ] | None = None,
                        category_id: UUID | None = None):
    method = getattr(_store(request), "list_products", None)
    if not method: return _page([], page, page_size, 0)
    items, total = await method(page=page, page_size=page_size, site_id=site_id, q=q,
                                availability_status=availability_status, category_id=category_id)
    return _page(items, page, page_size, total)


@router.get("/products/{product_id}")
async def get_product(request: Request, product_id: UUID):
    row = await _store(request).get_product(product_id)
    if not row:
        _not_found("Product not found")
    return row


@router.get("/products/{product_id}/price-history")
async def product_price_history(
    request: Request, product_id: UUID, page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=200),
    date_from: datetime | None = None, date_to: datetime | None = None,
):
    normalized_from = (
        date_from.replace(tzinfo=UTC)
        if date_from is not None and date_from.utcoffset() is None
        else date_from.astimezone(UTC) if date_from is not None else None
    )
    normalized_to = (
        date_to.replace(tzinfo=UTC)
        if date_to is not None and date_to.utcoffset() is None
        else date_to.astimezone(UTC) if date_to is not None else None
    )
    if normalized_from and normalized_to and normalized_from > normalized_to:
        raise HTTPException(422, detail={
            "code": "VALIDATION_ERROR",
            "message": "date_from must not be later than date_to",
            "field": "date_from",
        })
    result = await _store(request).list_product_price_history(
        product_id, page=page, page_size=page_size,
        date_from=normalized_from, date_to=normalized_to,
    )
    if result is None:
        _not_found("Product not found")
    items, total = result
    return _page(items, page, page_size, total)


@router.patch("/products/{product_id}")
async def assign_product_category(
    request: Request, product_id: UUID, payload: ProductCategoryAssignment,
):
    try:
        row = await _store(request).assign_product_category(
            product_id,
            payload.category_id,
            automatic=payload.category_mode == "automatic",
        )
    except LookupError as exc:
        raise HTTPException(404, detail={
            "code": "NOT_FOUND", "message": str(exc), "field": "category_id",
        }) from None
    except ValueError as exc:
        raise HTTPException(409, detail={
            "code": "CATEGORY_SITE_MISMATCH", "message": str(exc), "field": "category_id",
        }) from None
    if not row:
        _not_found("Product not found")
    return row


@router.get("/product-categories")
async def list_product_categories(
    request: Request, page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200), site_id: UUID | None = None,
    q: str | None = Query(None, max_length=200),
):
    items, total = await _store(request).list_product_categories(
        page=page, page_size=page_size, site_id=site_id, q=q,
    )
    return _page(items, page, page_size, total)


@router.post("/sites/{site_id}/product-categories", status_code=201)
async def create_product_category(
    request: Request, site_id: UUID, payload: ProductCategoryCreate,
):
    try:
        row = await _store(request).create_product_category(
            site_id, payload.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(409, detail={
            "code": "CATEGORY_CONFLICT", "message": str(exc), "field": "name",
        }) from None
    if not row:
        _not_found("Site not found")
    return row


@router.patch("/product-categories/{category_id}")
async def update_product_category(
    request: Request, category_id: UUID, payload: ProductCategoryUpdate,
):
    try:
        row = await _store(request).update_product_category(
            category_id, payload.model_dump(exclude_unset=True),
        )
    except ValueError as exc:
        raise HTTPException(409, detail={
            "code": "CATEGORY_CONFLICT", "message": str(exc), "field": "name",
        }) from None
    if not row:
        _not_found("Product category not found")
    return row


@router.delete("/product-categories/{category_id}", status_code=204)
async def delete_product_category(request: Request, category_id: UUID):
    if not await _store(request).delete_product_category(category_id):
        _not_found("Product category not found")
    return Response(status_code=204)


# -- Durable tasks ------------------------------------------------------
@router.get("/jobs")
async def list_jobs(request: Request, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200),
                    kind: JobKind | None = None, status: list[JobStatus] | None = Query(None),
                    site_id: UUID | None = None, account_id: UUID | None = None):
    one_status = status[0].value if status and len(status) == 1 else None
    if status and len(status) > 1:
        rows, _ = await _store(request).list_jobs(
            page=1, page_size=200, kind=kind.value if kind else None, status=None,
            site_id=site_id, account_id=account_id,
        )
        rows = [row for row in rows if row.get("status") in {value.value for value in status}]
        total = len(rows); start = (page - 1) * page_size; rows = rows[start:start + page_size]
    else:
        rows, total = await _store(request).list_jobs(
            page=page, page_size=page_size, kind=kind.value if kind else None,
            status=one_status, site_id=site_id, account_id=account_id,
        )
    return _page([_jobs(request).public_job(row) for row in rows], page, page_size, total)


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: UUID):
    row = await _store(request).get_job(job_id)
    if not row: _not_found("Job not found")
    return _jobs(request).public_job(row)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: UUID):
    row = await _jobs(request).cancel(job_id)
    await request.app.state.dispatcher.cancel_execution(job_id)
    with __import__("contextlib").suppress(Exception):
        await request.app.state.supervisor.cancel_job(job_id)
    await request.app.state.system_dispatcher.cancel_execution(job_id)
    return _jobs(request).public_job(row)


@router.post("/jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: UUID):
    row = await _jobs(request).retry(job_id)
    return _task_json(_jobs(request).task_ref(row))


@router.get("/stream")
@router.get("/tasks/stream", include_in_schema=False)
async def task_stream(request: Request):
    return StreamingResponse(request.app.state.events.stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# -- Scheduling and worker ---------------------------------------------
@router.get("/settings/schedules")
async def get_schedule_settings(request: Request):
    values = await _store(request).get_settings()
    return {
        "timezone": values.get("scheduler.timezone", "Asia/Shanghai"),
        "checkin_window_start": values.get("scheduler.checkin_window_start", "08:00:00"),
        "checkin_window_end": values.get("scheduler.checkin_window_end", "22:00:00"),
        "product_interval_minutes": int(values.get("scheduler.product_interval_minutes", "60")),
    }


@router.put("/settings/schedules")
async def update_schedule_settings(request: Request, payload: ScheduleSettingsUpdate):
    await _store(request).update_settings({
        "scheduler.timezone": payload.timezone,
        "scheduler.checkin_window_start": payload.checkin_window_start,
        "scheduler.checkin_window_end": payload.checkin_window_end,
        "scheduler.product_interval_minutes": str(payload.product_interval_minutes),
    })
    return await get_schedule_settings(request)


@router.get("/schedules")
async def list_schedules(request: Request, page: int = Query(1, ge=1),
                         page_size: int = Query(50, ge=1, le=200),
                         capability: str | None = None, enabled: bool | None = None):
    items, total = await _store(request).list_schedules(
        page=page, page_size=page_size, capability=capability, enabled=enabled,
    )
    return _page(items, page, page_size, total)


@router.get("/batches")
async def list_batches(request: Request, page: int = Query(1, ge=1),
                       page_size: int = Query(20, ge=1, le=200), status: str | None = None,
                       date_from: str | None = None, date_to: str | None = None):
    items, total = await _store(request).list_batches(
        page=page, page_size=page_size, status=status, date_from=date_from, date_to=date_to,
    )
    return _page(items, page, page_size, total)


@router.get("/batches/{batch_id}")
async def get_batch(request: Request, batch_id: UUID):
    batch = await _store(request).get_batch(batch_id)
    if not batch: _not_found("Batch not found")
    batch["items"] = await _store(request).list_batch_items(batch_id)
    return batch


@router.get("/schedules/checkin-batch/today")
async def today_batch(request: Request):
    from zoneinfo import ZoneInfo
    key = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    batch = await _store(request).get_batch_for_date(key)
    if not batch: _not_found("Today's batch has not been planned")
    batch["items"] = await _store(request).list_batch_items(UUID(str(batch["id"])))
    return batch


@router.post("/schedules/checkin-batch/today/regenerate")
async def regenerate_today_batch(request: Request):
    from zoneinfo import ZoneInfo
    local = datetime.now(ZoneInfo("Asia/Shanghai"))
    planned = await request.app.state.scheduler.random_planned_at(local.date())
    row = await _store(request).regenerate_batch(local.date().isoformat(), planned.astimezone(UTC))
    if not row: raise JobConflict("batch cannot be regenerated after it starts")
    return row


@router.get("/worker/status")
async def worker_status(request: Request):
    result = request.app.state.supervisor.status().model_dump(mode="json")
    result["status"] = "online" if result["state"] == "ready" else "offline"
    result["connected"] = result["state"] == "ready"
    return result


@router.post("/worker/restart", status_code=202)
async def restart_worker(request: Request):
    await request.app.state.supervisor.restart()
    return await worker_status(request)


def _manifest(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id") or item.get("adapterId"),
        "name": item.get("name") or item.get("title") or item.get("displayName") or item.get("id") or item.get("adapterId"),
        "version": str(item.get("version") or "1.0.0"),
        "description": item.get("description", ""),
        "capabilities": item.get("capabilities") or ["checkin"],
        "site_config_schema": item.get("site_config_schema") or item.get("siteConfigSchema") or {"type": "object"},
        "account_config_schema": item.get("account_config_schema") or item.get("accountConfigSchema") or {"type": "object"},
        "ui_schema": item.get("ui_schema") or item.get("uiSchema") or {},
        "secret_fields": item.get("secret_fields") or item.get("secretFields") or [],
        "defaults": item.get("defaults") or {},
    }


@router.get("/adapters")
async def adapters(request: Request):
    worker = [_manifest(item) for item in request.app.state.supervisor.adapters]
    builtins = [
        {
            "id": "classic", "name": "VShop Classic", "version": "1.0.0",
            "description": "Browser-rendered Dujiao/Myuki catalog detection.",
            "capabilities": ["product_monitor"], "secret_fields": [],
            "site_config_schema": {"type": "object", "properties": {}, "additionalProperties": True},
        },
        {
            "id": "next_api", "name": "VShop Next API", "version": "1.0.0",
            "description": "Probe Dujiao-Next JSON APIs, with a browser fallback.",
            "capabilities": ["product_monitor"],
            "site_config_schema": {
                "type": "object", "additionalProperties": True,
                "properties": {
                    "api_path": {
                        "type": "string", "title": "Preferred API path",
                        "description": "Optional path such as /api/v1/public/products.",
                    },
                    "headers": {
                        "type": "object", "title": "API headers", "additionalProperties": True,
                        "properties": {
                            "Authorization": {"type": "string", "title": "Authorization"},
                            "X-Api-Key": {"type": "string", "title": "X-Api-Key"},
                            "Accept-Language": {"type": "string", "title": "Accept-Language"},
                        },
                    },
                },
            },
            "ui_schema": {
                "api_path": {"label": "优先 API 路径", "placeholder": "/api/v1/public/products", "order": 1},
                "headers": {"label": "请求头", "order": 2},
                "Authorization": {"label": "Authorization"},
                "X-Api-Key": {"label": "X-Api-Key"},
                "Accept-Language": {"label": "Accept-Language"},
            },
            "secret_fields": ["headers.Authorization", "headers.X-Api-Key"],
        },
        {
            "id": "custom_spa", "name": "VShop Custom SPA", "version": "1.0.0",
            "description": "Browser-based heuristic extraction for custom storefronts.",
            "capabilities": ["product_monitor"], "secret_fields": [],
            "site_config_schema": {"type": "object", "properties": {}, "additionalProperties": True},
        },
        {
            "id": "manual", "name": "Manual Catalog", "version": "1.0.0",
            "description": "Keep a manually managed catalog without automatic scraping.",
            "capabilities": ["product_monitor"], "secret_fields": [],
            "site_config_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "id": "manager", "name": "VShop Manager", "version": "1.0.0",
            "description": "Try product adapters in a configurable order until one returns products.",
            "capabilities": ["product_monitor"], "secret_fields": [],
            "site_config_schema": {
                "type": "object", "additionalProperties": True,
                "properties": {
                    "probe_order": {
                        "type": "array", "title": "Probe order",
                        "description": "Comma-separated adapter IDs.",
                        "items": {"type": "string"},
                        "default": ["next_api", "classic", "custom_spa"],
                    },
                },
            },
            "ui_schema": {"probe_order": {"label": "探测顺序", "order": 1}},
            "defaults": {"probe_order": ["next_api", "classic", "custom_spa"]},
        },
    ]
    known = {item["id"] for item in worker}
    return worker + [_manifest(item) for item in builtins if item["id"] not in known]


@router.post("/adapters/{adapter_id}/test")
async def test_adapter(request: Request, adapter_id: str):
    manifests = await adapters(request)
    if adapter_id not in {item["id"] for item in manifests}:
        _not_found("Adapter not found")
    task, _ = await _jobs(request).enqueue_adapter_test(adapter_id)
    return _task_json(task)


@router.post("/imports/bookmarks", status_code=202)
async def import_bookmarks(request: Request):
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(422, detail={
            "code": "VALIDATION_ERROR", "message": "A bookmark HTML file is required", "field": "file",
        })
    content = await upload.read()
    if not content:
        raise HTTPException(422, detail={
            "code": "VALIDATION_ERROR", "message": "The bookmark file is empty", "field": "file",
        })
    job_id = uuid4()
    try:
        staged = await request.app.state.bookmark_staging.stage(job_id, content)
    except ValueError as exc:
        raise HTTPException(413, detail={"code": "PAYLOAD_TOO_LARGE", "message": str(exc), "field": "file"}) from exc
    try:
        task, _ = await _jobs(request).enqueue_bookmark_import(
            job_id=job_id, relative_path=staged["relative_path"],
            sha256=staged["sha256"], size=staged["size"],
        )
    except Exception:
        await request.app.state.bookmark_staging.cleanup({"staging_path": staged["relative_path"]})
        raise
    return _task_json(task)


# -- Local database backups --------------------------------------------
@router.get("/backups")
async def list_backups(request: Request):
    try:
        items = await asyncio.to_thread(_backups(request).list_backups)
        pending = await asyncio.to_thread(_backups(request).pending_restore)
    except BackupError as exc:
        _raise_backup_error(exc)
    return {
        "items": [item.to_dict() for item in items],
        "total": len(items),
        "pending": pending.to_dict() if pending else None,
    }


@router.post("/backups", status_code=201)
async def create_backup(request: Request, payload: BackupCreateRequest):
    try:
        record = await asyncio.to_thread(
            _backups(request).create_backup,
            label=payload.label,
        )
    except (BackupError, TypeError, ValueError) as exc:
        if isinstance(exc, BackupError):
            _raise_backup_error(exc)
        raise HTTPException(422, detail={
            "code": "INVALID_BACKUP_LABEL", "message": str(exc), "field": "label",
        }) from None
    return record.to_dict()


@router.post("/backups/{backup_id}/verify")
async def verify_backup(request: Request, backup_id: str):
    try:
        result = await asyncio.to_thread(_backups(request).verify_backup, backup_id)
    except BackupError as exc:
        _raise_backup_error(exc)
    return result.to_dict()


@router.post("/backups/{backup_id}/restore", status_code=202)
async def stage_backup_restore(
    request: Request, backup_id: str, payload: BackupRestoreRequest,
):
    if payload.confirmation != backup_id:
        raise HTTPException(400, detail={
            "code": "CONFIRMATION_REQUIRED",
            "message": "Enter the full backup identifier to arrange a restore",
            "field": "confirmation",
        })
    try:
        pending = await asyncio.to_thread(_backups(request).stage_restore, backup_id)
    except BackupError as exc:
        _raise_backup_error(exc)
    return {
        "pending": pending.to_dict(),
        "restart_required": True,
        "message": "Restore verified and staged for the next SiteHub startup",
    }


@router.delete("/backups/restore-pending", status_code=204)
async def cancel_backup_restore(request: Request):
    try:
        await asyncio.to_thread(_backups(request).cancel_pending_restore)
    except BackupError as exc:
        _raise_backup_error(exc)
    return Response(status_code=204)


# -- Folders, tags, history and inbox ----------------------------------
@router.get("/folders")
async def folders(request: Request):
    method = getattr(_store(request), "list_folders", None)
    return await method() if method else []


@router.post("/folders", status_code=201)
async def create_folder(request: Request, payload: dict[str, Any] = Body(...)):
    if not str(payload.get("name", "")).strip(): raise HTTPException(422, detail={"code": "VALIDATION_ERROR", "message": "Folder name is required", "field": "name"})
    return await _store(request).create_folder(payload)


@router.patch("/folders/{folder_id}")
async def update_folder(request: Request, folder_id: UUID, payload: dict[str, Any] = Body(...)):
    if "name" in payload and not str(payload["name"]).strip():
        raise HTTPException(422, detail={"code": "VALIDATION_ERROR", "message": "Folder name is required", "field": "name"})
    try:
        row = await _store(request).update_folder(folder_id, payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(409, detail={"code": "FOLDER_CONFLICT", "message": str(exc)}) from exc
    if not row: _not_found("Folder not found")
    return row


@router.delete("/folders/{folder_id}", status_code=204)
async def delete_folder(request: Request, folder_id: UUID):
    if not await _store(request).delete_folder(folder_id):
        if str(folder_id) == str(getattr(_store(request), "ROOT_FOLDER_ID", "")):
            raise HTTPException(409, detail={"code": "FOLDER_PROTECTED", "message": "The built-in folder cannot be deleted"})
        _not_found("Folder not found")
    return Response(status_code=204)


@router.get("/tags")
async def tags(request: Request):
    method = getattr(_store(request), "list_tags", None)
    return await method() if method else []


@router.post("/tags", status_code=201)
async def create_tag(request: Request, payload: dict[str, Any] = Body(...)):
    return await _store(request).create_tag(payload)


@router.patch("/tags/{tag_id}")
async def update_tag(request: Request, tag_id: UUID, payload: dict[str, Any] = Body(...)):
    row = await _store(request).update_tag(tag_id, payload)
    if not row: _not_found("Tag not found")
    return row


@router.delete("/tags/{tag_id}", status_code=204)
async def delete_tag(request: Request, tag_id: UUID):
    if not await _store(request).delete_tag(tag_id): _not_found("Tag not found")
    return Response(status_code=204)


@router.get("/runs")
async def runs(request: Request, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200), status: str | None = None):
    method = getattr(_store(request), "list_runs", None)
    if not method: return _page([], page, page_size, 0)
    items, total = await method(page=page, page_size=page_size, status=status)
    return _page(items, page, page_size, total)


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: UUID):
    method = getattr(_store(request), "get_run", None)
    row = await method(run_id) if method else None
    if not row: _not_found("Run not found")
    return row


@router.post("/runs/{run_id}/legacy-detail")
async def get_run_legacy_detail(
    request: Request,
    response: Response,
    run_id: UUID,
    confirmation: dict[str, Any] = Body(...),
):
    if confirmation.get("confirm") is not True:
        raise HTTPException(
            400,
            detail={"code": "CONFIRMATION_REQUIRED", "message": "Explicit confirmation is required"},
        )
    method = getattr(_store(request), "get_run_legacy_detail", None)
    row = await method(run_id) if method else None
    if row is None: _not_found("Run not found")
    response.headers["Cache-Control"] = "no-store"
    return row


@router.get("/events")
async def events(request: Request, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200),
                 site_id: UUID | None = None, unread: bool | None = None,
                 dismissed: bool | None = None, severity: str | None = None,
                 inbox: bool = False):
    method = getattr(_store(request), "list_events", None)
    if not method: return _page([], page, page_size, 0)
    items, total = await method(page=page, page_size=page_size, site_id=site_id, unread=unread,
                                dismissed=dismissed, severity=severity, inbox=inbox)
    return _page(items, page, page_size, total)


@router.patch("/events/{event_id}")
async def update_event(request: Request, event_id: UUID, payload: dict[str, Any] = Body(...)):
    row = await _store(request).update_event(event_id, read=payload.get("read"), dismissed=payload.get("dismissed"))
    if not row: _not_found("Event not found")
    return row


@router.post("/events/read-all", status_code=204)
async def read_all_events(request: Request, payload: dict[str, Any] | None = Body(default=None)):
    filters = (payload or {}).get("filters") or {}
    await _store(request).mark_all_events_read(
        severity=filters.get("severity"), inbox=bool(filters.get("inbox", False)),
    )
    return Response(status_code=204)


# -- Settings and controlled artifacts --------------------------------
def _decode_setting(value: str | None, default: Any) -> Any:
    if value is None: return default
    try: return json.loads(value)
    except (TypeError, ValueError): return value


@router.get("/settings/general")
async def general_settings(request: Request):
    values = await _store(request).get_settings()
    defaults = {"language": "zh-CN", "timezone": "Asia/Shanghai", "start_on_login": True,
                "open_browser_on_start": True, "retain_history_days": 90}
    return {key: _decode_setting(values.get(f"general.{key}"), default) for key, default in defaults.items()}


@router.put("/settings/general")
async def update_general_settings(request: Request, payload: dict[str, Any] = Body(...)):
    allowed = {"language", "timezone", "start_on_login", "open_browser_on_start", "retain_history_days"}
    await _store(request).update_settings({f"general.{key}": json.dumps(value) for key, value in payload.items() if key in allowed})
    return await general_settings(request)


_NOTIFICATION_DEFAULTS = {
    "enabled": True,
    "price_drop": True,
    "back_in_stock": True,
    "checkin_failed": True,
    "checkin_success": False,
    "product_sync_failed": True,
    "quiet_hours_enabled": False,
    "quiet_hours_start": "23:00",
    "quiet_hours_end": "08:00",
}


def _notification_preferences(payload: dict[str, Any]) -> dict[str, Any]:
    unknown = set(payload) - set(_NOTIFICATION_DEFAULTS)
    if unknown:
        raise HTTPException(
            422,
            detail={"code": "VALIDATION_ERROR", "message": "Notification settings contain unsupported fields"},
        )
    result = dict(_NOTIFICATION_DEFAULTS)
    for key, value in payload.items():
        if key in {"quiet_hours_start", "quiet_hours_end"}:
            try:
                result[key] = datetime.strptime(str(value), "%H:%M").strftime("%H:%M")
            except (TypeError, ValueError):
                raise HTTPException(
                    422,
                    detail={"code": "VALIDATION_ERROR", "message": "Quiet-hour times must use HH:MM", "field": key},
                ) from None
        else:
            if type(value) is not bool:
                raise HTTPException(
                    422,
                    detail={"code": "VALIDATION_ERROR", "message": "Notification switches must be boolean", "field": key},
                )
            result[key] = value
    return result


@router.get("/settings/notifications")
async def notification_settings(request: Request):
    values = await _store(request).get_settings()
    stored = _decode_setting(values.get("notifications.config"), {})
    if not isinstance(stored, dict):
        stored = {}
    return {**_NOTIFICATION_DEFAULTS, **{key: value for key, value in stored.items() if key in _NOTIFICATION_DEFAULTS}}


@router.put("/settings/notifications")
async def update_notification_settings(request: Request, payload: dict[str, Any] = Body(...)):
    safe = _notification_preferences(payload)
    await _store(request).update_settings({"notifications.config": json.dumps(safe)})
    return safe


@router.get("/notification-channels")
async def list_notification_channels(request: Request):
    items = await _notification_channels(request).list()
    return {"items": items, "total": len(items)}


@router.post("/notification-channels", status_code=201)
async def create_notification_channel(request: Request, payload: dict[str, Any] = Body(...)):
    try:
        return await _notification_channels(request).create(payload)
    except ValueError as exc:
        raise HTTPException(
            422, detail={"code": "VALIDATION_ERROR", "message": str(exc)}
        ) from None


@router.patch("/notification-channels/{channel_id}")
async def update_notification_channel(
    request: Request, channel_id: UUID, payload: dict[str, Any] = Body(...)
):
    try:
        row = await _notification_channels(request).update(channel_id, payload)
    except ValueError as exc:
        raise HTTPException(
            422, detail={"code": "VALIDATION_ERROR", "message": str(exc)}
        ) from None
    if row is None:
        _not_found("Notification channel not found")
    return row


@router.delete("/notification-channels/{channel_id}", status_code=204)
async def delete_notification_channel(request: Request, channel_id: UUID):
    if not await _notification_channels(request).delete(channel_id):
        _not_found("Notification channel not found")
    return Response(status_code=204)


@router.post("/notification-channels/{channel_id}/test", status_code=202)
async def test_notification_channel(request: Request, channel_id: UUID):
    service = _notification_channels(request)
    channel = await service.get(channel_id)
    if channel is None:
        _not_found("Notification channel not found")
    if not channel["enabled"]:
        raise HTTPException(
            409,
            detail={"code": "CHANNEL_DISABLED", "message": "Enable the notification channel before testing it"},
        )
    result = await service.enqueue_test(channel_id)
    if result is None:
        raise HTTPException(
            409,
            detail={"code": "CHANNEL_UNAVAILABLE", "message": "Notification test could not be queued"},
        )
    return result


@router.get("/artifacts/{artifact_id}")
async def artifact_metadata(request: Request, artifact_id: UUID):
    row = await _store(request).get_artifact(artifact_id)
    if not row: _not_found("Artifact not found")
    return row


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(request: Request, artifact_id: UUID):
    row = await _store(request).get_artifact(artifact_id)
    if not row: _not_found("Artifact not found")
    root: Path = request.app.state.settings.artifacts_dir.resolve()
    target = (root / row["relative_path"]).resolve()
    if target == root or root not in target.parents or not target.is_file(): _not_found("Artifact file not found")
    return FileResponse(target, media_type=row.get("mime_type") or "application/octet-stream",
                        filename=Path(row["relative_path"]).name)


# -- Health -------------------------------------------------------------
@router.get("/health")
async def health(request: Request):
    database = "ok" if await _store(request).ping() else "error"
    worker = request.app.state.supervisor.state
    scheduler = "error" if request.app.state.scheduler.last_error else "ok"
    degraded = database != "ok" or worker not in {"ready", "disabled"} or scheduler != "ok"
    return {"status": "degraded" if degraded else "healthy", "database": database,
            "worker": worker, "scheduler": scheduler,
            "uptime_seconds": int((datetime.now(UTC) - request.app.state.started_at).total_seconds())}


@router.get("/health/live")
async def live(): return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request):
    settings = request.app.state.settings
    store = _store(request)
    secret_service = getattr(store, "secret_service", None)
    components: dict[str, Any] = {
        "database": "ok" if await store.ping() else "error",
        "frontend": "ok" if (settings.frontend_dir / "index.html").is_file() else "error",
        "secrets": "ok" if secret_service is not None else (
            "error" if hasattr(store, "database") else "skipped"
        ),
        "directories": {},
    }
    for name, directory in {
        "data": settings.data_dir,
        "profiles": settings.profiles_dir,
        "artifacts": settings.artifacts_dir,
        "logs": settings.logs_dir,
    }.items():
        components["directories"][name] = (
            "ok" if directory.is_dir() and os.access(directory, os.W_OK) else "error"
        )

    if request.app.state.start_background:
        scheduler_task = request.app.state.scheduler._task
        dispatcher_task = request.app.state.dispatcher._task
        system_dispatcher_task = request.app.state.system_dispatcher._task
        notification_task = request.app.state.notification_task
        components["scheduler"] = (
            "ok" if scheduler_task is not None and not scheduler_task.done()
            and request.app.state.scheduler.last_error is None else "error"
        )
        components["dispatcher"] = (
            "ok" if dispatcher_task is not None and not dispatcher_task.done() else "error"
        )
        components["system_dispatcher"] = (
            "ok" if system_dispatcher_task is not None and not system_dispatcher_task.done() else "error"
        )
        components["notification_outbox"] = (
            "ok" if notification_task is not None and not notification_task.done() else "error"
        )
        components["worker"] = request.app.state.supervisor.state
    else:
        components.update({
            "scheduler": "skipped", "dispatcher": "skipped",
            "system_dispatcher": "skipped", "notification_outbox": "skipped",
            "worker": "skipped",
        })

    directory_ready = all(value == "ok" for value in components["directories"].values())
    background_ready = not request.app.state.start_background or (
        components["scheduler"] == "ok"
        and components["dispatcher"] == "ok"
        and components["system_dispatcher"] == "ok"
        and components["notification_outbox"] == "ok"
        and components["worker"] == "ready"
    )
    is_ready = (
        components["database"] == "ok"
        and components["frontend"] == "ok"
        and components["secrets"] in {"ok", "skipped"}
        and directory_ready
        and background_ready
    )
    if not is_ready:
        raise HTTPException(503, detail={
            "code": "NOT_READY", "message": "One or more required components are unavailable",
            "context": {"components": components},
        })
    return {"status": "ready", "components": components}
