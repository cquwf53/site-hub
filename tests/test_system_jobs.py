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
from site_hub.services.data_store import DataLayerStore
from site_hub.services.events import EventBroker
from site_hub.services.memory_store import MemoryStore
from site_hub.services.ports import StaleJobWrite


class FakeSupervisor:
    state = "ready"
    adapters = [{"id": "worker-adapter"}]

    def __init__(self) -> None:
        self.opened = asyncio.Event()
        self.release = asyncio.Event()
        self.closed: list[UUID] = []

    async def open_session(self, account_id: UUID):
        self.opened.set()
        await self.release.wait()
        return {"state": "opened", "ignored_secret": "must-not-enter-result"}

    async def close_session(self, account_id: UUID):
        self.closed.append(account_id)
        return {"closed": True}


async def wait_for_job(store, job_id: UUID, statuses: set[str], timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        row = await store.get_job(job_id)
        if row and row["status"] in statuses:
            return row
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach {statuses}")


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
