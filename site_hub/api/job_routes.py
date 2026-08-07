from __future__ import annotations

from contextlib import suppress
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from site_hub.jobs.service import JobService
from site_hub.models import JobKind, JobStatus


router = APIRouter()


def _store(request: Request):
    return request.app.state.store


def _jobs(request: Request) -> JobService:
    return request.app.state.jobs


def _page(
    items: list[dict[str, Any]], page: int, page_size: int, total: int,
) -> dict[str, Any]:
    return {"items": items, "page": page, "page_size": page_size, "total": total}


def _not_found(message: str) -> None:
    raise HTTPException(404, detail={"code": "NOT_FOUND", "message": message})


@router.get("/jobs")
async def list_jobs(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    kind: JobKind | None = None,
    status: list[JobStatus] | None = Query(None),
    site_id: UUID | None = None,
    account_id: UUID | None = None,
):
    statuses = tuple(value.value for value in status) if status else None
    rows, total = await _store(request).list_jobs(
        page=page,
        page_size=page_size,
        kind=kind.value if kind else None,
        status=statuses,
        site_id=site_id,
        account_id=account_id,
    )
    return _page(
        [_jobs(request).public_job(row) for row in rows],
        page,
        page_size,
        total,
    )


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: UUID):
    row = await _store(request).get_job(job_id)
    if not row:
        _not_found("Job not found")
    return _jobs(request).public_job(row)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: UUID):
    row = await _jobs(request).cancel(job_id)
    await request.app.state.dispatcher.cancel_execution(job_id)
    with suppress(Exception):
        await request.app.state.supervisor.cancel_job(job_id)
    await request.app.state.system_dispatcher.cancel_execution(job_id)
    return _jobs(request).public_job(row)


@router.post("/jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: UUID):
    row = await _jobs(request).retry(job_id)
    return _jobs(request).task_ref(row).model_dump(mode="json", by_alias=True)


@router.get("/stream")
@router.get("/tasks/stream", include_in_schema=False)
async def task_stream(request: Request):
    return StreamingResponse(
        request.app.state.events.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = ["router"]
