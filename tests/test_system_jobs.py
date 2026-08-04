from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from site_hub.jobs.service import JobService, StaleLease
from site_hub.jobs.dispatcher import JobDispatcher
from site_hub.jobs.system_dispatcher import BookmarkStaging, SystemExecutionError, SystemJobDispatcher
from site_hub.models import JobKind, JobStatus
from site_hub.scrapers.models import ScrapedProduct
from site_hub.services import data_store as data_store_module
from site_hub.services.data_store import DataLayerStore
from site_hub.services.events import EventBroker
from site_hub.services.memory_store import MemoryStore
from site_hub.services.ports import AccountNotReady, StaleJobWrite


class FakeSupervisor:
    state = "ready"
    adapters = [{"id": "worker-adapter"}]

    def __init__(self) -> None:
        self.opened = asyncio.Event()
        self.release = asyncio.Event()
        self.closed: list[UUID] = []
        self.verified: list[UUID] = []
        self.verify_result = {
            "state": "ready",
            "authenticatedAt": "2026-08-03T12:00:00.000Z",
            "currentUrl": "https://example.test/account",
        }

    async def open_session(self, account_id: UUID):
        self.opened.set()
        await self.release.wait()
        return {"state": "opened", "ignored_secret": "must-not-enter-result"}

    async def close_session(self, account_id: UUID):
        self.closed.append(account_id)
        return {"closed": True}

    async def verify_session(self, account_id: UUID):
        self.verified.append(account_id)
        return dict(self.verify_result)


async def wait_for_job(store, job_id: UUID, statuses: set[str], timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        row = await store.get_job(job_id)
        if row and row["status"] in statuses:
            return row
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach {statuses}")


@pytest.mark.asyncio
async def test_schedule_listing_includes_latest_job_state(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    try:
        site = await store.create_site({
            "name": "Shop", "url": "https://shop.example", "status": "active",
            "capabilities": ["product_monitor"],
        })
        site_id = UUID(str(site["id"]))
        await store.update_product_settings(site_id, {"enabled": True})
        jobs = JobService(store, EventBroker())
        completed_task, created = await jobs.enqueue_product_sync(site_id)
        assert created is True
        leased = await jobs.lease(JobKind.PRODUCT_SYNC, "schedule-list-test")
        assert leased is not None
        lease_token = str(leased["lease_token"])
        await jobs.mark_running(completed_task.job_id, lease_token)
        await jobs.complete(
            completed_task.job_id,
            lease_token,
            status=JobStatus.SUCCEEDED,
            result={"created": 1},
        )
        current_task, created = await jobs.enqueue_product_sync(site_id)
        assert created is True

        schedules, total = await store.list_schedules(
            page=1, page_size=20, capability="product_monitor",
        )
        assert total == 1
        assert schedules[0]["current_job_id"] == str(current_task.job_id)
        assert schedules[0]["current_status"] == "queued"
        assert schedules[0]["last_job_id"] == str(completed_task.job_id)
        assert schedules[0]["last_status"] == "succeeded"
        assert schedules[0]["last_run_at"] is not None
        assert schedules[0]["last_error_message"] is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_global_adapter_job_is_durable_and_visible_in_run_history(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    jobs = JobService(store, EventBroker(), lease_seconds=1)
    dispatcher = SystemJobDispatcher(
        jobs, supervisor=FakeSupervisor(), staging=BookmarkStaging(tmp_path / "staging"),
        interval_seconds=0.01,
    )
    try:
        task, created = await jobs.enqueue_adapter_test("classic")
        assert created
        dispatcher.start()
        row = await wait_for_job(store, task.job_id, {"succeeded"})
        assert row["result"] == {
            "adapter_id": "classic", "source": "python", "worker_state": "ready",
        }
        assert row["payload"] == {"adapter_id": "classic"}
        runs, total = await store.list_runs(page=1, page_size=20)
        assert total == 1
        assert runs[0]["job_id"] == str(task.job_id)
        assert runs[0]["kind"] == "adapter_test"
        assert runs[0]["capability_instance_id"] is None
    finally:
        await dispatcher.stop()
        await store.close()


@pytest.mark.asyncio
async def test_queued_profile_verification_atomically_updates_account_and_profile(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    supervisor = FakeSupervisor()
    jobs = JobService(store, EventBroker(), lease_seconds=1)
    dispatcher = SystemJobDispatcher(
        jobs, supervisor=supervisor, staging=BookmarkStaging(tmp_path / "staging"),
        interval_seconds=0.01,
    )
    try:
        site = await store.create_site({
            "name": "Site", "url": "https://example.test", "status": "active",
        })
        account = await store.create_account(UUID(str(site["id"])), {
            "name": "Primary", "adapter": "worker-adapter", "enabled": False,
        })
        account_id = UUID(str(account["id"]))
        task, created = await jobs.enqueue_login_session(account_id, action="verify")
        assert created is True
        queued = await store.get_job(task.job_id)
        assert queued and queued["payload"] == {"action": "verify"}

        dispatcher.start()
        completed = await wait_for_job(store, task.job_id, {"succeeded"})
        assert completed["result"] == {
            "account_id": str(account_id), "action": "verify", "state": "ready",
            "worker_state": "ready",
        }
        assert supervisor.verified == [account_id]
        verified = await store.get_account(account_id)
        assert verified and verified["auth_state"] == "ready"
        assert verified["profile"]["state"] == "ready"
        assert verified["profile"]["last_verified_at"] == "2026-08-03T12:00:00.000Z"
    finally:
        await dispatcher.stop()
        await store.close()


@pytest.mark.asyncio
async def test_completed_busy_profile_verification_is_retryable_not_permanent_busy(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    supervisor = FakeSupervisor()
    supervisor.verify_result = {
        "state": "busy",
        "error": "Profile is in use; close the browser and retry",
    }
    jobs = JobService(store, EventBroker(), lease_seconds=1)
    dispatcher = SystemJobDispatcher(
        jobs, supervisor=supervisor, staging=BookmarkStaging(tmp_path / "staging"),
        interval_seconds=0.01,
    )
    try:
        site = await store.create_site({
            "name": "Site", "url": "https://example.test", "status": "active",
        })
        account = await store.create_account(UUID(str(site["id"])), {
            "name": "Primary", "adapter": "worker-adapter", "enabled": False,
        })
        account_id = UUID(str(account["id"]))
        task, _ = await jobs.enqueue_login_session(account_id, action="verify")

        dispatcher.start()
        completed = await wait_for_job(store, task.job_id, {"failed"})
        assert completed["error_kind"] == "needs_login"
        assert "close the browser" in completed["error_message"]
        verified = await store.get_account(account_id)
        assert verified and verified["auth_state"] == "unverified"
        assert verified["profile"]["state"] == "unverified"
        assert verified["profile"]["verification_error"] == supervisor.verify_result["error"]
    finally:
        await dispatcher.stop()
        await store.close()


@pytest.mark.asyncio
async def test_busy_profile_state_does_not_overwrite_terminal_account_auth_state(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    try:
        site = await store.create_site({
            "name": "Site", "url": "https://example.test", "status": "active",
        })
        account = await store.create_account(UUID(str(site["id"])), {
            "name": "Primary", "adapter": "worker-adapter", "enabled": False,
        })
        account_id = UUID(str(account["id"]))
        await store.set_account_profile_verification(
            account_id, "ready", verified_at="2026-08-03T12:00:00.000Z",
        )
        busy = await store.set_account_profile_verification(account_id, "busy")
        assert busy and busy["auth_state"] == "ready"
        assert busy["profile"]["state"] == "busy"
        assert busy["profile"]["last_verified_at"] == "2026-08-03T12:00:00.000Z"

        enabled = await store.update_account(account_id, {"enabled": True})
        assert enabled and enabled["enabled"] is True
        expired = await store.set_account_profile_verification(
            account_id, "expired", "Login expired",
        )
        assert expired and expired["auth_state"] == "expired"
        assert expired["profile"]["state"] == "error"
        assert expired["enabled"] is False
        with store.database.connect() as connection:
            flags = connection.execute(
                """SELECT a.enabled,c.enabled,s.enabled FROM site_accounts a
                   JOIN capability_instances c ON c.account_id=a.id AND c.code='checkin'
                   JOIN schedules s ON s.capability_instance_id=c.id WHERE a.id=?""",
                (str(account_id),),
            ).fetchone()
        assert tuple(flags) == (0, 0, 0)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_new_browser_account_stays_disabled_until_profile_is_verified(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    try:
        site = await store.create_site({
            "name": "Site", "url": "https://example.test", "status": "active",
        })
        browser = await store.create_account(UUID(str(site["id"])), {
            "name": "Browser", "adapter": "worker-adapter", "enabled": True,
        })
        direct = await store.create_account(UUID(str(site["id"])), {
            "name": "Direct", "adapter": "api", "enabled": True,
        })
        assert browser["enabled"] is False
        assert browser["auth_state"] == "unverified"
        assert direct["enabled"] is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_interrupted_profile_verification_disables_automation_on_restart(tmp_path):
    database_path = tmp_path / "site-hub.db"
    store = DataLayerStore(database_path)
    await store.initialize()
    site = await store.create_site({
        "name": "Site", "url": "https://example.test", "status": "active",
    })
    account = await store.create_account(UUID(str(site["id"])), {
        "name": "Browser", "adapter": "worker-adapter", "enabled": False,
    })
    account_id = UUID(str(account["id"]))
    await store.set_account_profile_verification(account_id, "ready")
    enabled = await store.update_account(account_id, {"enabled": True})
    assert enabled and enabled["enabled"] is True
    task, _ = await JobService(store, EventBroker()).enqueue_checkin(account_id)
    busy = await store.set_account_profile_verification(account_id, "busy")
    assert busy and busy["profile"]["state"] == "busy" and busy["enabled"] is True
    await store.close()

    reopened = DataLayerStore(database_path)
    await reopened.initialize()
    try:
        recovered = await reopened.get_account(account_id)
        assert recovered and recovered["auth_state"] == "unverified"
        assert recovered["profile"]["state"] == "unverified"
        assert recovered["enabled"] is False
        assert (await reopened.get_job(task.job_id))["status"] == "cancelled"
        with reopened.database.connect() as connection:
            flags = connection.execute(
                """SELECT a.enabled,c.enabled,s.enabled FROM site_accounts a
                   JOIN capability_instances c ON c.account_id=a.id AND c.code='checkin'
                   JOIN schedules s ON s.capability_instance_id=c.id WHERE a.id=?""",
                (str(account_id),),
            ).fetchone()
        assert tuple(flags) == (0, 0, 0)
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_bookmark_staging_is_integrity_checked_and_cleaned_on_queued_cancel(tmp_path):
    store = MemoryStore()
    jobs = JobService(store, EventBroker(), lease_seconds=1)
    staging = BookmarkStaging(tmp_path / "staging")
    dispatcher = SystemJobDispatcher(jobs, supervisor=FakeSupervisor(), staging=staging)
    job_id = uuid4()
    staged = await staging.stage(job_id, b'<A HREF="https://example.test/?token=secret">One</A>')
    task, _ = await jobs.enqueue_bookmark_import(
        job_id=job_id, relative_path=staged["relative_path"],
        sha256=staged["sha256"], size=staged["size"],
    )
    public = jobs.public_job((await store.get_job(task.job_id)) or {})
    assert "payload" not in public
    await jobs.cancel(task.job_id)
    await dispatcher.cancel_execution(task.job_id)
    assert (await store.get_job(task.job_id))["status"] == "cancelled"
    assert not (tmp_path / "staging" / staged["relative_path"]).exists()

    staged = await staging.stage(uuid4(), b"safe")
    with pytest.raises(SystemExecutionError, match="checksum"):
        await staging.read_verified({
            "staging_path": staged["relative_path"], "sha256": "0" * 64, "size": 4,
        })
    with pytest.raises(ValueError, match="escapes"):
        await staging.read_verified({"staging_path": "../outside", "sha256": "", "size": 0})


@pytest.mark.asyncio
async def test_running_login_job_can_be_cancelled_and_expired_lease_recovers(tmp_path):
    store = MemoryStore()
    site = await store.create_site({"name": "Site", "url": "https://example.test", "status": "active"})
    account = await store.create_account(site["id"], {
        "name": "Primary", "adapter": "worker-adapter", "enabled": True,
    })
    jobs = JobService(store, EventBroker(), lease_seconds=1)
    supervisor = FakeSupervisor()
    dispatcher = SystemJobDispatcher(
        jobs, supervisor=supervisor, staging=BookmarkStaging(tmp_path / "staging"),
        interval_seconds=0.01,
    )
    task, _ = await jobs.enqueue_login_session(UUID(str(account["id"])))
    dispatcher.start()
    await asyncio.wait_for(supervisor.opened.wait(), timeout=2)
    await jobs.cancel(task.job_id)
    await dispatcher.cancel_execution(task.job_id)
    assert (await store.get_job(task.job_id))["status"] == "cancelled"
    assert supervisor.closed == [UUID(str(account["id"]))]
    await dispatcher.stop()

    retry, _ = await jobs.enqueue_adapter_test("classic")
    leased = await jobs.lease(retry.kind, "crashed-worker")
    assert leased and leased["id"] == retry.job_id
    store.jobs[retry.job_id]["lease_expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    recovered = await jobs.recover()
    assert recovered and recovered[0]["status"] == "queued"
    assert (await store.get_job(retry.job_id))["attempt"] == 1


@pytest.mark.asyncio
async def test_running_product_cancel_stops_handler_and_rejects_stale_business_write(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    site = await store.create_site({
        "name": "Shop", "url": "https://shop.example", "status": "active",
        "capabilities": ["product_monitor"],
    })
    site_id = UUID(str(site["id"]))
    jobs = JobService(store, EventBroker(), lease_seconds=30)
    started = asyncio.Event()
    release = asyncio.Event()
    handler_cancelled = asyncio.Event()

    async def product_handler(_site, job):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            handler_cancelled.set()
            raise
        await store.save_scrape_result(
            site_id, [ScrapedProduct(name="Must not persist", external_key="cancelled")],
            job_id=UUID(str(job["id"])), lease_token=str(job["lease_token"]),
        )
        return {"created": 1}

    dispatcher = JobDispatcher(
        jobs, product_handler=product_handler, interval_seconds=0.01, product_concurrency=1,
    )
    try:
        task, _ = await jobs.enqueue_product_sync(site_id)
        dispatcher.start()
        await asyncio.wait_for(started.wait(), timeout=2)
        running = await store.get_job(task.job_id)
        assert running and running["status"] == "running"
        lease_token = str(running["lease_token"])

        await jobs.cancel(task.job_id)
        assert await dispatcher.cancel_execution(task.job_id) is True
        await asyncio.wait_for(handler_cancelled.wait(), timeout=2)

        with pytest.raises(StaleJobWrite):
            await store.save_scrape_result(
                site_id, [ScrapedProduct(name="Stale write", external_key="stale")],
                job_id=task.job_id, lease_token=lease_token,
            )
        products, total = await store.list_products(
            page=1, page_size=20, site_id=site_id,
        )
        assert products == []
        assert total == 0
        assert (await store.get_job(task.job_id))["status"] == "cancelled"
    finally:
        await dispatcher.stop()
        await store.close()


@pytest.mark.asyncio
async def test_disabling_default_account_and_archiving_site_cancel_queued_checkins(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    try:
        site = await store.create_site({
            "name": "Check-in site", "url": "https://checkin.example", "status": "active",
        })
        site_id = UUID(str(site["id"]))
        default_account = await store.create_account(site_id, {
            "name": "Default", "adapter": "api", "enabled": True,
        })
        override_account = await store.create_account(site_id, {
            "name": "Override", "adapter": "api", "enabled": True,
            "schedule_override": {
                "mode": "fixed_time", "time": "09:30", "timezone": "Asia/Shanghai",
            },
        })
        assert default_account["schedule_override"] is None
        assert override_account["schedule_override"]["mode"] == "fixed_time"

        due = datetime.now(UTC) + timedelta(days=1)
        date_key = due.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        batch = await store.create_batch(
            {"date_key": date_key, "planned_at": due},
            [UUID(str(default_account["id"]))],
        )
        default_items = await store.list_batch_items(UUID(str(batch["id"])))
        assert len(default_items) == 1 and default_items[0]["status"] == "queued"

        jobs = JobService(store, EventBroker())
        override_task, created = await jobs.enqueue_checkin(
            UUID(str(override_account["id"])), scheduled_for=due,
        )
        assert created is True

        disabled = await store.update_account(
            UUID(str(default_account["id"])), {"enabled": False},
        )
        assert disabled and disabled["enabled"] is False
        default_job = await store.get_job(UUID(str(default_items[0]["id"])))
        assert default_job and default_job["status"] == "cancelled"
        assert default_job["result"] == {
            "reason_code": "disabled", "cancelled_before_start": True,
        }
        assert (await store.get_job(override_task.job_id))["status"] == "queued"

        assert await store.delete_site(site_id) is True
        override_job = await store.get_job(override_task.job_id)
        assert override_job and override_job["status"] == "cancelled"
        assert override_job["result"] == {
            "reason_code": "disabled", "cancelled_before_start": True,
        }
    finally:
        await store.close()


def _freeze_data_store_clock(monkeypatch, instant: datetime) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(data_store_module, "datetime", FrozenDateTime)


async def _ready_browser_account(store: DataLayerStore, site_id: UUID) -> dict:
    account = await store.create_account(site_id, {
        "name": "Browser", "adapter": "worker-adapter", "enabled": False,
    })
    verified = await store.set_account_profile_verification(
        UUID(str(account["id"])), "ready", verified_at="2026-08-04T00:00:00Z",
    )
    assert verified and verified["auth_state"] == "ready"
    return verified


@pytest.mark.asyncio
async def test_enabling_ready_browser_account_backfills_today_pending_batch_once(
    tmp_path, monkeypatch,
):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    try:
        site = await store.create_site({
            "name": "Check-in site", "url": "https://checkin.example", "status": "active",
        })
        site_id = UUID(str(site["id"]))
        account = await _ready_browser_account(store, site_id)
        planned = datetime(2026, 8, 4, 9, 47, 6, tzinfo=UTC)
        batch = await store.create_batch(
            {"date_key": "2026-08-04", "planned_at": planned}, [],
        )
        _freeze_data_store_clock(
            monkeypatch, datetime(2026, 8, 4, 1, 0, tzinfo=UTC),
        )

        enabled = await store.update_account(UUID(str(account["id"])), {"enabled": True})
        enabled_again = await store.update_account(
            UUID(str(account["id"])), {"enabled": True},
        )

        assert enabled and enabled["enabled"] is True
        assert enabled_again and enabled_again["enabled"] is True
        with store.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE account_id=? AND kind='checkin'",
                (str(account["id"]),),
            ).fetchall()
        assert len(rows) == 1
        job = rows[0]
        assert job["batch_id"] == str(batch["id"])
        assert job["business_date"] == "2026-08-04"
        assert job["planned_at"] == "2026-08-04T09:47:06.000Z"
        assert job["available_at"] == job["planned_at"]
        assert job["idempotency_key"] == f"checkin:{account['id']}:2026-08-04"
        assert (job["priority"], job["status"], job["max_attempts"]) == (50, "queued", 3)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_backfilled_browser_account_applies_existing_misfire_recovery(
    tmp_path, monkeypatch,
):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    try:
        site = await store.create_site({
            "name": "Check-in site", "url": "https://checkin.example", "status": "active",
        })
        site_id = UUID(str(site["id"]))
        existing = await store.create_account(site_id, {
            "name": "API", "adapter": "api", "enabled": True,
        })
        browser = await _ready_browser_account(store, site_id)
        batch = await store.create_batch(
            {
                "date_key": "2026-08-04",
                "planned_at": datetime(2026, 8, 4, 2, 0, tzinfo=UTC),
            },
            [UUID(str(existing["id"]))],
        )
        _freeze_data_store_clock(
            monkeypatch, datetime(2026, 8, 4, 4, 0, tzinfo=UTC),
        )  # 12:00 Shanghai; recover once at 13:00.

        await store.update_account(UUID(str(browser["id"])), {"enabled": True})

        with store.database.connect() as conn:
            stored_batch = conn.execute(
                "SELECT * FROM job_batches WHERE id=?", (str(batch["id"]),),
            ).fetchone()
            jobs = conn.execute(
                "SELECT * FROM jobs WHERE batch_id=? ORDER BY account_id", (str(batch["id"]),),
            ).fetchall()
        assert stored_batch["trigger_reason"] == "regenerated"
        assert stored_batch["planned_at"] == "2026-08-04T05:00:00.000Z"
        assert len(jobs) == 2
        assert all(job["planned_at"] == stored_batch["planned_at"] for job in jobs)
        assert all(job["available_at"] == stored_batch["planned_at"] for job in jobs)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_backfilled_browser_account_runs_immediately_after_recovery_cutoff(
    tmp_path, monkeypatch,
):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    try:
        site = await store.create_site({
            "name": "Check-in site", "url": "https://checkin.example", "status": "active",
        })
        account = await _ready_browser_account(store, UUID(str(site["id"])))
        batch = await store.create_batch(
            {
                "date_key": "2026-08-04",
                "planned_at": datetime(2026, 8, 4, 2, 0, tzinfo=UTC),
            },
            [],
        )
        _freeze_data_store_clock(
            monkeypatch, datetime(2026, 8, 4, 15, 15, tzinfo=UTC),
        )  # 23:15 Shanghai; the existing rule no longer defers.

        await store.update_account(UUID(str(account["id"])), {"enabled": True})

        with store.database.connect() as conn:
            stored_batch = conn.execute(
                "SELECT * FROM job_batches WHERE id=?", (str(batch["id"]),),
            ).fetchone()
            job = conn.execute(
                "SELECT * FROM jobs WHERE account_id=? AND kind='checkin'",
                (str(account["id"]),),
            ).fetchone()
        assert stored_batch["trigger_reason"] == "normal"
        assert stored_batch["planned_at"] == "2026-08-04T02:00:00.000Z"
        assert job["available_at"] == stored_batch["planned_at"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_batch_backfill_skips_override_and_rejects_failed_browser_profile(
    tmp_path, monkeypatch,
):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    try:
        site = await store.create_site({
            "name": "Check-in site", "url": "https://checkin.example", "status": "active",
        })
        site_id = UUID(str(site["id"]))
        override = await store.create_account(site_id, {
            "name": "Override", "adapter": "worker-adapter", "enabled": False,
            "schedule_override": {
                "mode": "fixed_time", "time": "09:30", "timezone": "Asia/Shanghai",
            },
        })
        await store.set_account_profile_verification(
            UUID(str(override["id"])), "ready", verified_at="2026-08-04T00:00:00Z",
        )
        failed = await store.create_account(site_id, {
            "name": "Failed", "adapter": "worker-adapter", "enabled": False,
        })
        await store.set_account_profile_verification(
            UUID(str(failed["id"])), "error", error="verification failed",
        )
        await store.create_batch(
            {
                "date_key": "2026-08-04",
                "planned_at": datetime(2026, 8, 4, 9, 47, 6, tzinfo=UTC),
            },
            [],
        )
        _freeze_data_store_clock(
            monkeypatch, datetime(2026, 8, 4, 1, 0, tzinfo=UTC),
        )

        enabled_override = await store.update_account(
            UUID(str(override["id"])), {"enabled": True},
        )
        with pytest.raises(AccountNotReady):
            await store.update_account(UUID(str(failed["id"])), {"enabled": True})

        assert enabled_override and enabled_override["enabled"] is True
        with store.database.connect() as conn:
            assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
            failed_state = conn.execute(
                """SELECT a.enabled,c.enabled capability_enabled,s.enabled schedule_enabled
                   FROM site_accounts a
                   JOIN capability_instances c ON c.account_id=a.id AND c.code='checkin'
                   JOIN schedules s ON s.capability_instance_id=c.id
                   WHERE a.id=?""",
                (str(failed["id"]),),
            ).fetchone()
        assert tuple(failed_state) == (0, 0, 0)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_checkin_cancel_distinguishes_queued_from_running_and_requires_reconcile(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    try:
        site = await store.create_site({
            "name": "Check-in site", "url": "https://checkin.example", "status": "active",
        })
        site_id = UUID(str(site["id"]))
        queued_account = await store.create_account(site_id, {
            "name": "Queued", "adapter": "api", "enabled": True,
        })
        running_account = await store.create_account(site_id, {
            "name": "Running", "adapter": "api", "enabled": True,
        })
        jobs = JobService(store, EventBroker(), lease_seconds=30)

        queued_task, _ = await jobs.enqueue_checkin(UUID(str(queued_account["id"])))
        queued = await jobs.cancel(queued_task.job_id)
        assert queued["status"] == "cancelled"
        assert queued["result"] == {
            "reason_code": "cancelled", "cancelled_before_start": True,
        }

        running_task, _ = await jobs.enqueue_checkin(UUID(str(running_account["id"])))
        leased = await jobs.lease(JobKind.CHECKIN, "test-worker")
        assert leased and UUID(str(leased["id"])) == running_task.job_id
        token = str(leased["lease_token"])
        await jobs.mark_running(running_task.job_id, token)

        uncertain = await jobs.cancel(running_task.job_id)
        assert uncertain["status"] == "effect_unknown"
        assert uncertain["error_kind"] == "cancel_requested"
        assert uncertain["result"] == {
            "reason_code": "cancel_requested",
            "cancel_requested": True,
            "requires_reconcile": True,
        }
        with pytest.raises(StaleLease):
            await jobs.complete(
                running_task.job_id, token, status=JobStatus.SUCCEEDED,
                result={"reason_code": "completed_after_cancel"},
            )

        runs, total = await store.list_runs(page=1, page_size=20)
        assert total == 1
        assert runs[0]["status"] == "effect_unknown"
        assert runs[0]["reason_code"] == "cancel_requested"

        retried = await jobs.retry(running_task.job_id)
        assert retried["status"] == "queued"
        reconciler = await jobs.lease(JobKind.CHECKIN, "reconcile-worker")
        assert reconciler and UUID(str(reconciler["id"])) == running_task.job_id
        assert reconciler["attempt"] == 2
    finally:
        await store.close()
