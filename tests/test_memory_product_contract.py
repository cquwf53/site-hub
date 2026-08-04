from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from site_hub.config import Settings
from site_hub.jobs.dispatcher import JobDispatcher
from site_hub.jobs.service import JobService
from site_hub.main import create_app
from site_hub.scrapers.manager import ProductScraperService
from site_hub.services.data_store import DataLayerStore
from site_hub.services.events import EventBroker
from site_hub.services.memory_store import MemoryStore


HOST = "127.0.0.1:8686"
ORIGIN = f"http://{HOST}"


def _settings(tmp_path) -> Settings:
    frontend = tmp_path / "frontend" / "dist"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text(
        "<!doctype html><title>SiteHub</title>", encoding="utf-8",
    )
    return Settings(
        project_root=tmp_path, data_dir=tmp_path / "data", frontend_dir=frontend,
    )


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    return {"X-CSRF-Token": token, "Origin": ORIGIN}


def test_memory_store_product_api_and_schedule_contract(tmp_path) -> None:
    store = MemoryStore()
    app = create_app(
        settings=_settings(tmp_path), store=store, start_background=False,
    )
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        headers = _csrf(client)
        site = asyncio.run(store.create_site({
            "name": "Memory shop", "url": "https://memory.example",
            "status": "active", "capabilities": ["product_monitor"],
        }))
        site_id = UUID(str(site["id"]))
        category_response = client.post(
            f"/api/v1/sites/{site_id}/product-categories",
            json={"name": "Plans", "sort_order": 2}, headers=headers,
        )
        assert category_response.status_code == 201
        category_id = UUID(category_response.json()["id"])

        product_id = uuid4()
        store.products[product_id] = {
            "id": product_id, "site_id": site_id, "category_id": category_id,
            "scraped_category_id": category_id, "category_overridden": False,
            "external_key": "sku-1", "name": "Starter", "price": 8.5,
            "original_price": "10.0", "stock": 4,
            "availability_status": "available", "raw_tags": ["monthly"],
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-03T00:00:00Z",
            "last_observed_at": "2026-08-03T00:00:00Z",
        }
        for day, price in ((1, 10.0), (2, 9.0), (3, 8.5)):
            observation_id = uuid4()
            store.price_observations[observation_id] = {
                "id": observation_id, "product_id": product_id, "price": price,
                "stock": 4, "availability_status": "available",
                "observed_at": f"2026-08-0{day}T00:00:00Z",
                "source_run_id": None,
            }

        products = client.get(
            "/api/v1/products", params={"category_id": str(category_id)},
        )
        assert products.status_code == 200
        assert products.json()["total"] == 1
        assert products.json()["items"][0]["category_name"] == "Plans"

        detail = client.get(f"/api/v1/products/{product_id}")
        assert detail.status_code == 200
        assert detail.json()["price_history_summary"]["observation_count"] == 3
        history = client.get(
            f"/api/v1/products/{product_id}/price-history",
            params={"page": 1, "page_size": 2},
        )
        assert history.status_code == 200
        assert history.json()["total"] == 3
        assert [item["price"] for item in history.json()["items"]] == [8.5, 9.0]

        categories = client.get(
            "/api/v1/product-categories", params={"site_id": str(site_id)},
        )
        assert categories.status_code == 200
        assert categories.json()["items"][0]["product_count"] == 1

        for payload in ({"name": None}, {"sort_order": None}):
            invalid = client.patch(
                f"/api/v1/product-categories/{category_id}",
                json=payload, headers=headers,
            )
            assert invalid.status_code == 422

        unclassified = client.patch(
            f"/api/v1/products/{product_id}",
            json={"category_id": None}, headers=headers,
        )
        assert unclassified.status_code == 200
        assert unclassified.json()["category_id"] is None
        assert unclassified.json()["category_overridden"] is True
        automatic = client.patch(
            f"/api/v1/products/{product_id}",
            json={"category_mode": "automatic"}, headers=headers,
        )
        assert automatic.status_code == 200
        assert automatic.json()["category_id"] == str(category_id)
        assert automatic.json()["category_overridden"] is False

        asyncio.run(store.update_product_settings(site_id, {
            "enabled": True, "scraper_type": "manual",
        }))
        assert asyncio.run(
            store.list_due_product_sites(datetime.now(UTC))
        ) == []
        schedules = client.get(
            "/api/v1/schedules",
            params={"capability": "product_monitor", "enabled": True},
        )
        assert schedules.status_code == 200
        assert schedules.json()["total"] == 1
        schedule = schedules.json()["items"][0]
        assert schedule["enabled"] is True
        assert schedule["scraper_type"] == "manual"
        assert schedule["automatic_run_supported"] is False
        assert schedule["automatic_run_reason"] == "manual_catalog"


@pytest.mark.asyncio
async def test_manual_product_job_has_explicit_skipped_terminal_state() -> None:
    store = MemoryStore()
    site = await store.create_site({
        "name": "Manual catalog", "url": "https://manual.example",
        "status": "active", "capabilities": ["product_monitor"],
    })
    site_id = UUID(str(site["id"]))
    await store.update_product_settings(site_id, {
        "enabled": True, "scraper_type": "manual",
    })
    jobs = JobService(store, EventBroker())
    dispatcher = JobDispatcher(
        jobs, product_handler=ProductScraperService(store), interval_seconds=0.005,
    )
    task, _ = await jobs.enqueue_product_sync(site_id)
    dispatcher.start()
    try:
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            job = await store.get_job(task.job_id)
            if job and job["status"] == "skipped":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("manual product job did not reach skipped")
        assert job["result"]["skipped"] is True
        assert job["result"]["reason"] == "manual or non-scraping product catalog"
        assert job["finished_at"] is not None
    finally:
        await dispatcher.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scraper_type", "reason"),
    (("manual", "manual_catalog"), ("none", "scraper_not_configured")),
)
async def test_data_layer_schedule_describes_non_automatic_product_adapter(
    tmp_path, scraper_type: str, reason: str,
) -> None:
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    try:
        site = await store.create_site({
            "name": f"{scraper_type} catalog",
            "url": f"https://{scraper_type}.example",
            "status": "active", "capabilities": ["product_monitor"],
        })
        await store.update_product_settings(UUID(str(site["id"])), {
            "enabled": True, "scraper_type": scraper_type,
        })

        schedules, total = await store.list_schedules(
            page=1, page_size=20, capability="product_monitor", enabled=True,
        )

        assert total == 1
        assert schedules[0]["enabled"] is True
        assert schedules[0]["scraper_type"] == scraper_type
        assert schedules[0]["automatic_run_supported"] is False
        assert schedules[0]["automatic_run_reason"] == reason
        assert await store.list_due_product_sites(datetime.now(UTC)) == []
    finally:
        await store.close()
