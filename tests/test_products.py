from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from site_hub.config import Settings
from site_hub.jobs.service import JobService
from site_hub.main import create_app
from site_hub.models import JobKind
from site_hub.scrapers.models import ScrapedProduct
from site_hub.services.data_store import DataLayerStore
from site_hub.services.events import EventBroker


HOST = "127.0.0.1:8686"
ORIGIN = f"http://{HOST}"


def _settings(tmp_path) -> Settings:
    frontend = tmp_path / "frontend" / "dist"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text("<!doctype html><title>SiteHub</title>", encoding="utf-8")
    return Settings(project_root=tmp_path, data_dir=tmp_path / "data", frontend_dir=frontend)


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    return {"X-CSRF-Token": token, "Origin": ORIGIN}


def _seed_product(store: DataLayerStore, site_id: str, category_id: str) -> str:
    product_id = str(uuid4())
    with store.database.transaction(immediate=True) as connection:
        connection.execute(
            """INSERT INTO products(
                   id,site_id,category_id,external_key,name,price,original_price,stock,
                   description,raw_tags_json,availability_status,last_observed_at,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                product_id, site_id, category_id, "sku-1", "Starter Plan", 8.5, "10.0", 12,
                "Entry plan", '["monthly"]', "available", "2026-08-03T03:00:00.000Z",
                "2026-08-01T01:00:00.000Z", "2026-08-03T03:00:00.000Z",
            ),
        )
        for index, price in enumerate((10.0, 9.0, 8.5), start=1):
            connection.execute(
                """INSERT INTO price_observations(
                       id,product_id,price,stock,availability_status,observed_at,dedupe_key
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    str(uuid4()), product_id, price, 12, "available",
                    f"2026-08-0{index}T03:00:00.000Z", f"observation-{index}",
                ),
            )
    return product_id


def test_product_detail_price_history_and_category_crud_api(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    app = create_app(settings=_settings(tmp_path), store=store, start_background=False)
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        headers = _csrf(client)
        site = asyncio.run(store.create_site({
            "name": "Store A", "url": "https://store-a.test", "status": "active",
        }))
        created = client.post(
            f"/api/v1/sites/{site['id']}/product-categories",
            json={"name": " Plans ", "sort_order": 3}, headers=headers,
        )
        assert created.status_code == 201
        category = created.json()
        assert category["name"] == "Plans"
        assert category["product_count"] == 0

        duplicate = client.post(
            f"/api/v1/sites/{site['id']}/product-categories",
            json={"name": "plans"}, headers=headers,
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "CATEGORY_CONFLICT"

        product_id = _seed_product(store, str(site["id"]), category["id"])
        products = client.get(
            "/api/v1/products", params={"category_id": category["id"], "page_size": 1},
        ).json()
        assert products["total"] == 1
        assert products["items"][0]["category_name"] == "Plans"

        detail = client.get(f"/api/v1/products/{product_id}")
        assert detail.status_code == 200
        assert detail.json()["raw_tags"] == ["monthly"]
        assert detail.json()["price_history_summary"] == {
            "observation_count": 3,
            "min_price": 8.5,
            "max_price": 10.0,
            "first_observed_at": "2026-08-01T03:00:00.000Z",
            "last_observed_at": "2026-08-03T03:00:00.000Z",
        }

        history = client.get(
            f"/api/v1/products/{product_id}/price-history",
            params={"page": 1, "page_size": 2},
        ).json()
        assert history["total"] == 3
        assert [item["price"] for item in history["items"]] == [8.5, 9.0]

        renamed = client.patch(
            f"/api/v1/product-categories/{category['id']}",
            json={"name": "Subscriptions", "sort_order": 1}, headers=headers,
        )
        assert renamed.status_code == 200
        assert renamed.json()["product_count"] == 1

        cleared = client.patch(
            f"/api/v1/products/{product_id}", json={"category_id": None}, headers=headers,
        )
        assert cleared.status_code == 200
        assert cleared.json()["category_id"] is None
        assert client.delete(
            f"/api/v1/product-categories/{category['id']}", headers=headers,
        ).status_code == 204


def test_product_category_scope_and_query_validation(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    app = create_app(settings=_settings(tmp_path), store=store, start_background=False)
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        headers = _csrf(client)
        first = asyncio.run(store.create_site({"name": "First", "url": "https://first.test"}))
        second = asyncio.run(store.create_site({"name": "Second", "url": "https://second.test"}))
        first_category = client.post(
            f"/api/v1/sites/{first['id']}/product-categories", json={"name": "First category"}, headers=headers,
        ).json()
        second_category = client.post(
            f"/api/v1/sites/{second['id']}/product-categories", json={"name": "Second category"}, headers=headers,
        ).json()
        product_id = _seed_product(store, str(first["id"]), first_category["id"])

        mismatch = client.patch(
            f"/api/v1/products/{product_id}", json={"category_id": second_category["id"]}, headers=headers,
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["error"]["code"] == "CATEGORY_SITE_MISMATCH"
        assert client.get(
            "/api/v1/products", params={"availability_status": "invented"},
        ).status_code == 422
        assert client.get(
            f"/api/v1/products/{product_id}/price-history",
            params={"date_from": "2026-08-04T00:00:00Z", "date_to": "2026-08-01T00:00:00Z"},
        ).status_code == 422
        missing = client.get(
            f"/api/v1/products/{UUID('00000000-0000-0000-0000-000000000099')}/price-history",
        )
        assert missing.status_code == 404
        mixed_timezones = client.get(
            f"/api/v1/products/{product_id}/price-history",
            params={
                "date_from": "2026-08-01T00:00:00",
                "date_to": "2026-08-04T00:00:00+08:00",
            },
        )
        assert mixed_timezones.status_code == 200
        assert mixed_timezones.json()["total"] == 3


@pytest.mark.asyncio
async def test_manual_category_survives_sync_and_source_rename(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    site = await store.create_site({
        "name": "Shop",
        "url": "https://shop.example",
        "status": "active",
        "capabilities": ["product_monitor"],
    })
    site_id = UUID(str(site["id"]))
    jobs = JobService(store, EventBroker(), lease_seconds=30)
    task, _ = await jobs.enqueue_product_sync(site_id)
    leased = await jobs.lease(JobKind.PRODUCT_SYNC, "category-test")
    assert leased is not None
    lease_token = str(leased["lease_token"])
    await jobs.mark_running(task.job_id, lease_token)
    try:
        await store.save_scrape_result(
            site_id,
            [ScrapedProduct(
                name="Plan", external_key="plan", category_name="Plans", price=10,
            )],
            job_id=task.job_id,
            lease_token=lease_token,
        )
        products, _ = await store.list_products(page=1, page_size=20, site_id=site_id)
        product = products[0]
        source_category_id = product["scraped_category_id"]
        assert product["category_id"] == source_category_id
        assert product["category_overridden"] is False

        manual = await store.create_product_category(site_id, {"name": "Favorites"})
        assert manual is not None
        assigned = await store.assign_product_category(
            UUID(product["id"]), UUID(manual["id"]),
        )
        assert assigned is not None and assigned["category_overridden"] is True

        renamed = await store.update_product_category(
            UUID(source_category_id), {"name": "Subscriptions"},
        )
        assert renamed is not None
        await store.save_scrape_result(
            site_id,
            [ScrapedProduct(
                name="Plan", external_key="plan", category_name="pLaNs", price=9,
            )],
            job_id=task.job_id,
            lease_token=lease_token,
        )
        after_sync = await store.get_product(UUID(product["id"]))
        assert after_sync is not None
        assert after_sync["category_id"] == manual["id"]
        assert after_sync["scraped_category_id"] == source_category_id
        assert after_sync["scraped_category_name"] == "Subscriptions"
        assert after_sync["category_overridden"] is True
        categories, total = await store.list_product_categories(
            page=1, page_size=20, site_id=site_id,
        )
        assert total == 2
        assert {item["name"] for item in categories} == {"Subscriptions", "Favorites"}

        automatic = await store.assign_product_category(
            UUID(product["id"]), None, automatic=True,
        )
        assert automatic is not None
        assert automatic["category_id"] == source_category_id
        assert automatic["category_overridden"] is False
    finally:
        await store.close()
