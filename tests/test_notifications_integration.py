from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from site_hub.config import Settings
from site_hub.jobs.service import JobService
from site_hub.main import create_app
from site_hub.models import JobKind, JobStatus
from site_hub.notifications import NotificationChannelService
from site_hub.scrapers.models import ScrapedProduct
from site_hub.services.data_store import DataLayerStore
from site_hub.services.events import EventBroker
from site_hub.services.secrets import EphemeralSecretProtector, SecretService


HOST = "127.0.0.1:8686"
ORIGIN = f"http://{HOST}"


class EphemeralSecretStore(DataLayerStore):
    async def initialize(self) -> None:
        await super().initialize()
        self.secret_service = SecretService(
            self.database,
            EphemeralSecretProtector(b"notification-integration-key-32b"),
        )


def _settings(tmp_path) -> Settings:
    frontend = tmp_path / "frontend" / "dist"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text("<!doctype html><title>SiteHub</title>", encoding="utf-8")
    return Settings(project_root=tmp_path, data_dir=tmp_path / "data", frontend_dir=frontend)


def _csrf(client: TestClient) -> dict[str, str]:
    response = client.get("/api/v1/bootstrap")
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"], "Origin": ORIGIN}


def _error(response) -> dict:
    assert "error" in response.json()
    return response.json()["error"]


def test_notification_channel_api_is_csrf_protected_and_never_echoes_endpoint(tmp_path):
    store = EphemeralSecretStore(tmp_path / "data" / "site-hub.db")
    app = create_app(settings=_settings(tmp_path), store=store, start_background=False)
    first_endpoint = "https://hooks.example/private?token=first-api-secret"
    second_endpoint = "https://hooks.example/private?token=second-api-secret"
    payload = {
        "name": "Primary", "channel_type": "webhook", "enabled": True,
        "endpoint": first_endpoint,
        "config": {"event_types": ["price_drop"], "min_severity": "warning"},
    }

    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        readiness = client.get("/api/v1/health/ready")
        assert readiness.status_code == 200
        assert readiness.json()["components"]["secrets"] == "ok"
        blocked = client.post("/api/v1/notification-channels", json=payload)
        assert blocked.status_code == 403
        assert _error(blocked)["code"] == "CSRF_FAILED"
        headers = _csrf(client)

        invalid = client.post(
            "/api/v1/notification-channels",
            json={**payload, "endpoint": "file:///tmp/private"}, headers=headers,
        )
        assert invalid.status_code == 422
        assert _error(invalid)["code"] == "VALIDATION_ERROR"

        created = client.post("/api/v1/notification-channels", json=payload, headers=headers)
        assert created.status_code == 201
        channel = created.json()
        assert channel["endpoint_configured"] is True
        assert "endpoint" not in channel
        assert first_endpoint not in created.text

        listed = client.get("/api/v1/notification-channels")
        assert listed.status_code == 200 and listed.json()["total"] == 1
        assert first_endpoint not in listed.text and "endpoint" not in listed.json()["items"][0]

        updated = client.patch(
            f"/api/v1/notification-channels/{channel['id']}",
            json={"name": "Renamed", "endpoint": second_endpoint, "enabled": False},
            headers=headers,
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "Renamed" and updated.json()["enabled"] is False
        assert second_endpoint not in updated.text and "endpoint" not in updated.json()

        disabled_test = client.post(
            f"/api/v1/notification-channels/{channel['id']}/test", headers=headers,
        )
        assert disabled_test.status_code == 409
        assert _error(disabled_test)["code"] == "CHANNEL_DISABLED"

        missing = client.patch(
            f"/api/v1/notification-channels/{uuid4()}", json={"name": "Missing"}, headers=headers,
        )
        assert missing.status_code == 404 and _error(missing)["code"] == "NOT_FOUND"

        preferences = client.put(
            "/api/v1/settings/notifications",
            json={"enabled": False, "price_drop": False}, headers=headers,
        )
        assert preferences.status_code == 200
        assert preferences.json()["enabled"] is False
        assert client.get("/api/v1/settings/notifications").json()["enabled"] is False
        bad_preferences = client.put(
            "/api/v1/settings/notifications", json={"enabled": "false"}, headers=headers,
        )
        assert bad_preferences.status_code == 422
        assert _error(bad_preferences)["code"] == "VALIDATION_ERROR"

        deleted = client.delete(f"/api/v1/notification-channels/{channel['id']}", headers=headers)
        assert deleted.status_code == 204
        missing_delete = client.delete(f"/api/v1/notification-channels/{channel['id']}", headers=headers)
        assert missing_delete.status_code == 404 and _error(missing_delete)["code"] == "NOT_FOUND"

    database_bytes = b"".join(path.read_bytes() for path in (tmp_path / "data").glob("site-hub.db*"))
    assert first_endpoint.encode() not in database_bytes
    assert second_endpoint.encode() not in database_bytes


async def _business_store(tmp_path):
    store = EphemeralSecretStore(tmp_path / "business.db")
    await store.initialize()
    assert store.secret_service is not None
    channels = NotificationChannelService(store.database, store.secret_service)
    await channels.create({
        "name": "All business events", "channel_type": "webhook",
        "endpoint": "https://hooks.example/business?token=encrypted",
        "config": {"event_types": [], "min_severity": "success"},
    })
    site = await store.create_site({
        "name": "Shop", "url": "https://shop.example", "status": "active",
        "capabilities": ["product_monitor"],
    })
    return store, UUID(str(site["id"]))


def _event_outbox_counts(store: DataLayerStore, *types: str) -> dict[str, tuple[int, int]]:
    placeholders = ",".join("?" for _ in types)
    with store.database.connect() as connection:
        rows = connection.execute(
            f"""SELECT e.type,COUNT(DISTINCT e.id) event_count,COUNT(o.id) outbox_count
                FROM events e LEFT JOIN notification_outbox o ON o.event_id=e.id
                WHERE e.type IN ({placeholders}) GROUP BY e.type""",
            types,
        ).fetchall()
    result = {event_type: (0, 0) for event_type in types}
    result.update({row["type"]: (int(row["event_count"]), int(row["outbox_count"])) for row in rows})
    return result


@pytest.mark.asyncio
async def test_product_change_events_and_outbox_share_the_product_transaction(tmp_path):
    store, site_id = await _business_store(tmp_path)
    jobs = JobService(store, EventBroker(), lease_seconds=30)
    task, _ = await jobs.enqueue_product_sync(site_id)
    leased = await jobs.lease(JobKind.PRODUCT_SYNC, "notification-test")
    assert leased and leased["id"] == str(task.job_id)
    lease_token = str(leased["lease_token"])
    await jobs.mark_running(task.job_id, lease_token)
    job_id = task.job_id
    initial = ScrapedProduct(name="Plan", external_key="plan", price=10, stock=0)
    changed = ScrapedProduct(name="Plan", external_key="plan", price=8, stock=2)
    try:
        await store.save_scrape_result(
            site_id, [initial], job_id=job_id, lease_token=lease_token,
        )
        with store.database.transaction() as connection:
            connection.execute(
                """CREATE TRIGGER fail_notification_enqueue BEFORE INSERT ON notification_outbox
                   BEGIN SELECT RAISE(ABORT,'forced notification rollback'); END"""
            )
        with pytest.raises(sqlite3.IntegrityError, match="forced notification rollback"):
            await store.save_scrape_result(
                site_id, [changed], job_id=job_id, lease_token=lease_token,
            )
        with store.database.connect() as connection:
            product = connection.execute(
                "SELECT price,stock,availability_status FROM products WHERE site_id=? AND external_key='plan'",
                (str(site_id),),
            ).fetchone()
            assert tuple(product) == (10.0, 0, "sold_out")
        assert _event_outbox_counts(store, "price_drop", "back_in_stock") == {
            "price_drop": (0, 0), "back_in_stock": (0, 0),
        }

        with store.database.transaction() as connection:
            connection.execute("DROP TRIGGER fail_notification_enqueue")
        await store.save_scrape_result(
            site_id, [changed], job_id=job_id, lease_token=lease_token,
        )
        assert _event_outbox_counts(store, "price_drop", "back_in_stock", "product_sync") == {
            "price_drop": (1, 1), "back_in_stock": (1, 1), "product_sync": (2, 0),
        }

        await store.update_settings({"notifications.config": json.dumps({"enabled": False})})
        await store.save_scrape_result(
            site_id, [ScrapedProduct(name="Plan", external_key="plan", price=7, stock=2)],
            job_id=job_id, lease_token=lease_token,
        )
        assert _event_outbox_counts(store, "price_drop")["price_drop"] == (2, 1)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_checkin_and_product_failures_notify_only_at_final_terminal_attempt(tmp_path):
    store, site_id = await _business_store(tmp_path)
    jobs = JobService(store, EventBroker(), lease_seconds=1)
    try:
        account = await store.create_account(site_id, {
            # Notification retry behavior is independent of browser login.
            "name": "Primary", "adapter": "api", "enabled": True,
        })
        checkin, _ = await jobs.enqueue_checkin(UUID(str(account["id"])))
        for attempt in range(1, 4):
            leased = await jobs.lease(JobKind.CHECKIN, f"checkin-{attempt}")
            assert leased, f"check-in lease missing on attempt {attempt}"
            token = str(leased["lease_token"])
            await jobs.mark_running(checkin.job_id, token)
            row = await jobs.complete(
                checkin.job_id, token, status=JobStatus.FAILED,
                result={"reason_code": "worker_lost"}, error_kind="infrastructure",
                error_message="worker unavailable",
            )
            expected = (0, 0) if attempt < 3 else (1, 1)
            assert _event_outbox_counts(store, "checkin_failed")["checkin_failed"] == expected
            assert row["status"] == ("queued" if attempt < 3 else "failed")

        product, _ = await jobs.enqueue_product_sync(site_id)
        for attempt in range(1, 4):
            leased = await jobs.lease(JobKind.PRODUCT_SYNC, f"product-{attempt}")
            assert leased
            token = str(leased["lease_token"])
            await jobs.mark_running(product.job_id, token)
            await jobs.complete(
                product.job_id, token, status=JobStatus.FAILED,
                result={"reason_code": "timeout"}, error_kind="infrastructure",
                error_message="upstream unavailable",
            )
            expected = (0, 0) if attempt < 3 else (1, 1)
            assert _event_outbox_counts(store, "product_sync_failed")["product_sync_failed"] == expected
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_expired_final_checkin_lease_creates_effect_unknown_notification(tmp_path):
    store, site_id = await _business_store(tmp_path)
    jobs = JobService(store, EventBroker(), lease_seconds=1)
    try:
        account = await store.create_account(site_id, {
            # Lease recovery is tested with an already-runnable direct adapter.
            "name": "Lease account", "adapter": "api", "enabled": True,
        })
        task, _ = await jobs.enqueue_checkin(UUID(str(account["id"])))
        for attempt in range(1, 4):
            leased = await jobs.lease(JobKind.CHECKIN, f"expired-{attempt}")
            assert leased, f"expired check-in lease missing on attempt {attempt}"
            await jobs.mark_running(task.job_id, str(leased["lease_token"]))
            with store.database.transaction() as connection:
                connection.execute(
                    "UPDATE jobs SET lease_until=? WHERE id=?",
                    ((datetime.now(UTC) - timedelta(seconds=2)).isoformat(), str(task.job_id)),
                )
            recovered = await jobs.recover()
            assert recovered[0]["status"] == ("queued" if attempt < 3 else "effect_unknown")
            expected = (0, 0) if attempt < 3 else (1, 1)
            assert _event_outbox_counts(store, "checkin_effect_unknown")["checkin_effect_unknown"] == expected
    finally:
        await store.close()


def test_notification_executor_lifecycle_stops_cleanly(monkeypatch, tmp_path):
    class TrackingExecutor:
        instances = []

        def __init__(self, store, secrets):
            self.started = False
            self.stopped = False
            self.__class__.instances.append(self)

        async def run_forever(self, stop_event):
            self.started = True
            await stop_event.wait()
            self.stopped = True

    monkeypatch.setattr("site_hub.main.NotificationOutboxExecutor", TrackingExecutor)
    store = EphemeralSecretStore(tmp_path / "data" / "site-hub.db")
    app = create_app(settings=_settings(tmp_path), store=store, start_background=True)
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        assert client.get("/api/v1/health/live").status_code == 200
        assert TrackingExecutor.instances and TrackingExecutor.instances[0].started
    assert TrackingExecutor.instances[0].stopped is True
