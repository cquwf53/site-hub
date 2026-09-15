from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from site_hub.config import Settings
from site_hub.jobs.dispatcher import JobDispatcher
from site_hub.jobs.service import JobService
from site_hub.main import create_app
from site_hub.models import JobKind, JobStatus
from site_hub.notifications import NotificationChannelService
from site_hub.scrapers.models import ScrapedProduct
from site_hub.services.data_store import DataLayerStore
from site_hub.services.events import EventBroker
from site_hub.services.secrets import EphemeralSecretProtector, SecretService


@pytest.fixture
async def catalog(tmp_path):
    instant = [datetime(2026, 9, 15, 1, tzinfo=UTC)]
    store = DataLayerStore(tmp_path / "site-hub.db", clock=lambda: instant[0])
    await store.initialize()
    store.secret_service = SecretService(store.database, EphemeralSecretProtector(b"monitor-test-secret-key-32bytes"))
    site = await store.create_site({"name": "Shop", "url": "https://shop.test", "capabilities": ["product_monitor"]})
    site_id = UUID(site["id"])
    jobs = JobService(store, EventBroker())

    async def sync(products, *, finish=True):
        task, _ = await jobs.enqueue_product_sync(site_id)
        job = await jobs.lease(JobKind.PRODUCT_SYNC, "test")
        await jobs.mark_running(task.job_id, job["lease_token"])
        result = await store.save_scrape_result(site_id, products, job_id=task.job_id, lease_token=job["lease_token"])
        if finish:
            await jobs.complete(task.job_id, job["lease_token"], status=JobStatus.SUCCEEDED)
        return task, job, result

    yield store, site_id, sync, instant
    await store.close()


def product(key, price=100, stock=0):
    return ScrapedProduct(name=f"Product {key}", external_key=key, price=price, stock=stock)


def event_payloads(store, event_type, *, eligible_only=False):
    with store.database.connect() as conn:
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT payload_json FROM events WHERE type=? ORDER BY occurred_at,id", (event_type,))
            if not eligible_only or json.loads(row[0]).get("notification_eligible", True)]


async def test_watch_thresholds_accumulate_and_cooldown_is_per_product(catalog):
    store, site_id, sync, instant = catalog
    await sync([product("a"), product("b")])
    rows, _ = await store.list_products(page=1, page_size=20, site_id=site_id)
    target = UUID(next(item["id"] for item in rows if item["external_key"] == "a"))
    policy = await store.get_monitor_policy(site_id)
    await store.update_monitor_policy(site_id, {**policy, "watched_only": True, "price_drop_amount": 8, "price_drop_percent": 10})
    await store.update_product_watch_rule(target, {"watched": True})
    await sync([product("a", 95), product("b", 50)])
    assert event_payloads(store, "price_drop", eligible_only=True) == []
    await sync([product("a", 90), product("b", 40)])
    payload = event_payloads(store, "price_drop", eligible_only=True)
    assert len(payload) == 1 and payload[0]["old_price"] == 100 and payload[0]["new_price"] == 90
    await sync([product("a", 80), product("b", 30)])
    assert len(event_payloads(store, "price_drop", eligible_only=True)) == 1
    instant[0] += timedelta(minutes=61)
    await sync([product("a", 75), product("b", 20)])
    assert len(event_payloads(store, "price_drop", eligible_only=True)) == 2
    watched, total = await store.list_products(page=1, page_size=20, watched=True)
    assert total == 1 and watched[0]["id"] == str(target) and watched[0]["watched"] is True


async def test_related_changes_share_one_notification_and_keep_channel_filters(catalog):
    store, site_id, sync, _ = catalog
    channels = NotificationChannelService(store.database, store.secret_service)
    await channels.create({"name": "Prices", "endpoint": "https://hooks.test/private?token=hidden",
                           "config": {"event_types": ["price_drop"], "min_severity": "warning"}})
    await sync([product("a"), product("b")])
    await sync([product("a", 90), product("b", 80)])
    alerts = event_payloads(store, "price_drop", eligible_only=True)
    assert len(alerts) == 1 and alerts[0]["count"] == 2
    assert {item["new_price"] for item in alerts[0]["items"]} == {90, 80}
    with store.database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1


async def test_stock_confirmation_requires_distinct_successful_jobs(catalog):
    store, site_id, sync, _ = catalog
    await sync([product("a", stock=0)])
    policy = await store.get_monitor_policy(site_id)
    await store.update_monitor_policy(site_id, {**policy, "stock_confirmations": 2})
    task, job, _ = await sync([product("a", stock=10)], finish=False)
    # Duplicate handler completion is not another independent observation.
    await store.save_scrape_result(site_id, [product("a", stock=10)], job_id=task.job_id, lease_token=job["lease_token"])
    rows, _ = await store.list_products(page=1, page_size=20)
    assert rows[0]["stock"] == 0 and rows[0]["availability_status"] == "sold_out"
    assert event_payloads(store, "back_in_stock") == []
    await JobService(store, EventBroker()).complete(task.job_id, job["lease_token"], status=JobStatus.SUCCEEDED)
    await sync([product("a", stock=10)])
    rows, _ = await store.list_products(page=1, page_size=20)
    assert rows[0]["stock"] == 10 and rows[0]["availability_status"] == "available"
    assert len(event_payloads(store, "back_in_stock")) == 1


async def test_catalog_shrink_and_wrong_page_preserve_existing_catalog(catalog):
    store, site_id, sync, _ = catalog
    await sync([product(str(index)) for index in range(20)])
    _, _, result = await sync([product("0", 1)] + [product(f"unrelated-{i}") for i in range(20)])
    assert result["catalog_anomaly"] is True and result["retained_count"] == 1
    rows, total = await store.list_products(page=1, page_size=200, site_id=site_id)
    assert total == 20 and all(row["price"] == 100 for row in rows)
    assert all(row["availability_status"] == "sold_out" for row in rows)
    assert len(event_payloads(store, "catalog_anomaly")) == 1
    _, _, empty = await sync([])
    assert empty["catalog_anomaly"] is True
    assert (await store.list_products(page=1, page_size=200))[1] == 20


async def test_invalid_scraper_price_rolls_back_whole_observation(catalog):
    store, site_id, sync, _ = catalog
    await sync([product("a"), product("b")])
    with pytest.raises(ValueError, match="invalid product price"):
        await sync([product("a", 50), product("b", float("nan"))])
    rows, _ = await store.list_products(page=1, page_size=20)
    assert {row["price"] for row in rows} == {100}
    assert not event_payloads(store, "price_drop", eligible_only=True)


async def test_site_query_pages_before_normalization_and_preserves_unicode_search(tmp_path, monkeypatch):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    for index in range(8):
        await store.create_site({"name": f"Straße {index} 10%", "url": f"https://site{index}.test"})
    seen = []
    normalize = store._normalize_site
    def count_normalized(conn, row):
        seen.append(row["id"])
        return normalize(conn, row)
    monkeypatch.setattr(store, "_normalize_site", count_normalized)
    rows, total = await store.list_sites(page=2, page_size=3, q="STRASSE")
    assert total == 8 and len(rows) == len(seen) == 3
    assert (await store.list_sites(page=1, page_size=20, q="%"))[1] == 8
    assert (await store.list_sites(page=1, page_size=20, q="_"))[1] == 0
    await store.close()


def test_monitoring_api_validation_csrf_and_openapi_contract(tmp_path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html></html>")
    settings = Settings(data_dir=tmp_path / "data", frontend_dir=frontend)
    store = DataLayerStore(settings.db_path)
    app = create_app(settings=settings, store=store, start_background=False)
    with TestClient(app, base_url="http://127.0.0.1:8686") as client:
        token = client.get("/api/v1/bootstrap").json()["csrf_token"]
        headers = {"X-CSRF-Token": token, "Origin": "http://127.0.0.1:8686"}
        site = client.post("/api/v1/sites", headers=headers, json={"name": "Shop", "url": "https://shop.test"}).json()
        path = f"/api/v1/sites/{site['id']}/monitor-policy"
        policy = client.get(path).json()
        assert policy["catalog_drop_threshold_percent"] == 70
        assert client.put(path, json=policy).status_code == 403
        assert client.put(path, headers=headers, json={**policy, "stock_confirmations": 0}).status_code == 422
        assert client.put(path, headers=headers, json={**policy, "price_drop_percent": 101}).status_code == 422
        assert client.put(path, headers=headers, json={**policy, "watched_only": True}).json()["watched_only"] is True
        task = client.post(f"/api/v1/sites/{site['id']}/sync", headers=headers).json()
        response = client.get(f"/api/v1/jobs/{task['jobId']}").json()
        assert response["scheduled_for"] and "error_message" in response and response["site_name"] == "Shop"
        assert "payload" not in response and "lease_token" not in response
        schema = client.get("/api/v1/openapi.json").json()
        assert "scheduled_for" in schema["components"]["schemas"]["JobOut"]["properties"]


async def test_retry_proof_cannot_survive_a_later_worker_loss(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    site = await store.create_site({"name": "API", "url": "https://api.test"})
    account = await store.create_account(UUID(site["id"]), {"name": "Account", "adapter": "api", "enabled": True})
    jobs = JobService(store, EventBroker())
    task, _ = await jobs.enqueue_checkin(UUID(account["id"]))
    first = await jobs.lease(JobKind.CHECKIN, "first")
    await jobs.mark_running(task.job_id, first["lease_token"])
    await jobs.complete(task.job_id, first["lease_token"], status=JobStatus.FAILED,
                        error_kind="infrastructure", result={"metadata": {"remoteEffect": "not_started"}})
    second = await jobs.lease(JobKind.CHECKIN, "second")
    assert second["payload"]["retry_context"]["remote_effect"] == "not_started"
    assert second["result"] is None
    await jobs.mark_running(task.job_id, second["lease_token"])
    await store.recover_expired_jobs(datetime.now(UTC) + timedelta(minutes=2))
    recovered = await store.get_job(task.job_id)
    assert recovered["payload"]["retry_context"]["remote_effect"] == "possible"
    await store.close()


async def test_product_dispatcher_survives_transient_claim_failure():
    recovered = asyncio.Event()
    class Service:
        count = 0
        async def recover(self):
            return []
        async def lease(self, *args):
            self.count += 1
            if self.count == 1:
                raise RuntimeError("database temporarily unavailable")
            recovered.set()
            return None
    service = Service()
    dispatcher = JobDispatcher(service, interval_seconds=0.01, product_concurrency=1)
    dispatcher.start()
    try:
        await asyncio.wait_for(recovered.wait(), 1)
        assert not dispatcher._task.done()
        assert dispatcher.last_error is None
    finally:
        await dispatcher.stop()


async def test_unknown_stock_is_not_positive_restock_evidence(catalog):
    store, site_id, sync, _ = catalog
    await sync([product("a", stock=0)])
    policy = await store.get_monitor_policy(site_id)
    await store.update_monitor_policy(site_id, {**policy, "stock_confirmations": 2})
    await sync([product("a", stock=10)])
    await sync([product("a", stock=-1)])
    await sync([product("a", stock=10)])
    rows, _ = await store.list_products(page=1, page_size=20)
    assert rows[0]["availability_status"] == "sold_out"
    assert not event_payloads(store, "back_in_stock")
    await sync([product("a", stock=10)])
    assert len(event_payloads(store, "back_in_stock")) == 1


@pytest.mark.parametrize("missing", [False, True])
async def test_interleaved_duplicate_observations_cannot_complete_confirmation(catalog, missing):
    from uuid import uuid4
    store, site_id, sync, _ = catalog
    initial = [product("a", stock=0), product("b", stock=5)]
    await sync(initial)
    policy = await store.get_monitor_policy(site_id)
    await store.update_monitor_policy(site_id, {**policy, "stock_confirmations": 3})
    # Exercise storage's idempotency guard independently of dispatcher serialization.
    jobs = JobService(store, EventBroker())
    submissions = []
    for _ in range(3):
        task, _ = await jobs.enqueue_product_sync(site_id)
        token = str(uuid4())
        with store.database.transaction(immediate=True) as conn:
            conn.execute("UPDATE jobs SET status='leased',attempt=1,lease_token=? WHERE id=?", (token, str(task.job_id)))
        await jobs.mark_running(task.job_id, token)
        submissions.append((task.job_id, token))
    observed = [product("b", stock=5)] if missing else [product("a", stock=10), product("b", stock=5)]
    for index in (0, 1, 0):
        job_id, token = submissions[index]
        await store.save_scrape_result(site_id, observed, job_id=job_id, lease_token=token)
    rows, _ = await store.list_products(page=1, page_size=20)
    target = next(row for row in rows if row["external_key"] == "a")
    assert target["availability_status"] == "sold_out"
    job_id, token = submissions[2]
    await store.save_scrape_result(site_id, observed, job_id=job_id, lease_token=token)
    target = await store.get_product(UUID(target["id"]))
    assert target["availability_status"] == ("missing" if missing else "available")


async def test_same_site_syncs_serialize_while_other_sites_can_run(catalog):
    store, site_id, _, _ = catalog
    jobs = JobService(store, EventBroker())
    first, _ = await jobs.enqueue_product_sync(site_id)
    second, _ = await jobs.enqueue_product_sync(site_id)
    leased = await jobs.lease(JobKind.PRODUCT_SYNC, "worker-a")
    assert leased["id"] == str(first.job_id)
    assert await jobs.lease(JobKind.PRODUCT_SYNC, "worker-b") is None
    other = await store.create_site({"name": "Other", "url": "https://other.test"})
    third, _ = await jobs.enqueue_product_sync(UUID(other["id"]))
    assert (await jobs.lease(JobKind.PRODUCT_SYNC, "worker-b"))["id"] == str(third.job_id)
    await jobs.complete(first.job_id, leased["lease_token"], status=JobStatus.SUCCEEDED)
    assert (await jobs.lease(JobKind.PRODUCT_SYNC, "worker-c"))["id"] == str(second.job_id)


def test_schema_upgrade_creates_consistent_old_schema_backup(tmp_path):
    import sqlite3
    from site_hub.db import Database, MIGRATIONS, SCHEMA_VERSION, apply_migrations
    from site_hub.services.backups import BackupService
    settings = Settings(data_dir=tmp_path / "data", frontend_dir=tmp_path / "frontend")
    settings.frontend_dir.mkdir()
    (settings.frontend_dir / "index.html").write_text("<html></html>")
    settings.data_dir.mkdir()
    apply_migrations(Database(settings.db_path), MIGRATIONS[:4])
    app = create_app(settings=settings, start_background=False)
    with TestClient(app, base_url="http://127.0.0.1:8686"):
        pass
    records = [entry.record for entry in BackupService(settings.data_dir).list_backups() if entry.record]
    assert len(records) == 1 and records[0].label == f"Before schema upgrade 4 -> {SCHEMA_VERSION}"
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    backup_path = settings.data_dir / "backups" / records[0].backup_id / records[0].database_file
    with sqlite3.connect(backup_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4


async def test_notification_loop_recovers_from_transient_database_failure():
    import threading
    from site_hub.notifications.executor import NotificationOutboxExecutor
    from site_hub.notifications.models import NotificationPolicy
    claimed = threading.Event()
    class Store:
        policy = NotificationPolicy()
        attempts = 0
        def claim(self, worker_id):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("database locked")
            claimed.set()
            return None
    executor = NotificationOutboxExecutor(Store(), None)
    stop = asyncio.Event()
    task = asyncio.create_task(executor.run_forever(stop, poll_interval_seconds=0.01))
    try:
        assert await asyncio.to_thread(claimed.wait, 2)
        assert not task.done()
        assert executor.last_error is None
    finally:
        stop.set()
        await asyncio.wait_for(task, 2)
