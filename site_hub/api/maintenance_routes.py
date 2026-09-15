from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from site_hub.services.maintenance import (
    MaintenanceBusyError, MaintenanceService, MaintenanceSettings, MaintenanceStatus,
)

router = APIRouter()


def _maintenance(request: Request) -> MaintenanceService:
    service = getattr(request.app.state, "maintenance", None)
    if service is None:
        raise HTTPException(503, detail={
            "code": "MAINTENANCE_UNAVAILABLE", "message": "Maintenance requires the SQLite data store",
        })
    return service


def _busy() -> HTTPException:
    return HTTPException(409, detail={
        "code": "MAINTENANCE_BUSY", "message": "Maintenance is already running",
    })


@router.get("/settings/maintenance", response_model=MaintenanceSettings)
async def get_maintenance_settings(request: Request):
    return await asyncio.to_thread(_maintenance(request).get_settings)


@router.put("/settings/maintenance", response_model=MaintenanceSettings)
async def update_maintenance_settings(request: Request, payload: MaintenanceSettings):
    try:
        return await asyncio.to_thread(_maintenance(request).update_settings, payload.model_dump())
    except MaintenanceBusyError:
        raise _busy() from None


@router.get("/maintenance/status", response_model=MaintenanceStatus)
async def maintenance_status(request: Request):
    return await asyncio.to_thread(_maintenance(request).status)


@router.post("/maintenance/run", response_model=MaintenanceStatus)
async def run_maintenance(request: Request):
    try:
        return await asyncio.to_thread(_maintenance(request).run_once, manual=True)
    except MaintenanceBusyError:
        raise _busy() from None
    except RuntimeError:
        raise HTTPException(503, detail={
            "code": "MAINTENANCE_FAILED",
            "message": "Maintenance failed; inspect its status before retrying",
        }) from None
