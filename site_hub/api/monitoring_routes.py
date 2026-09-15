from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from site_hub.monitoring_models import MonitorPolicy, ProductWatchRule

router = APIRouter()


def store(request: Request):
    value = request.app.state.store
    if not hasattr(value, "get_monitor_policy"):
        raise HTTPException(503, detail={"code": "UNAVAILABLE", "message": "Monitoring storage is unavailable"})
    return value


@router.get("/sites/{site_id}/monitor-policy", response_model=MonitorPolicy)
async def get_policy(request: Request, site_id: UUID):
    value = await store(request).get_monitor_policy(site_id)
    if value is None:
        raise HTTPException(404, detail="Site not found")
    return value


@router.put("/sites/{site_id}/monitor-policy", response_model=MonitorPolicy)
async def update_policy(request: Request, site_id: UUID, payload: MonitorPolicy):
    value = await store(request).update_monitor_policy(site_id, payload.model_dump())
    if value is None:
        raise HTTPException(404, detail="Site not found")
    return value


@router.get("/products/{product_id}/watch-rule", response_model=ProductWatchRule)
async def get_rule(request: Request, product_id: UUID):
    value = await store(request).get_product_watch_rule(product_id)
    if value is None:
        raise HTTPException(404, detail="Product not found")
    return value


@router.put("/products/{product_id}/watch-rule", response_model=ProductWatchRule)
async def update_rule(request: Request, product_id: UUID, payload: ProductWatchRule):
    value = await store(request).update_product_watch_rule(product_id, payload.model_dump())
    if value is None:
        raise HTTPException(404, detail="Product not found")
    return value
