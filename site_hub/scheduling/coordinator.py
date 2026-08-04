from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from site_hub.jobs.service import JobService
from site_hub.scrapers.network import sanitize_log_value
from site_hub.services.ports import Store


logger = logging.getLogger(__name__)


class ScheduleCoordinator:
    def __init__(self, store: Store, jobs: JobService, *, tick_seconds: float = 30,
                 timezone: str = "Asia/Shanghai", random_source: random.Random | None = None) -> None:
        self.store = store
        self.jobs = jobs
        self.tick_seconds = tick_seconds
        self.timezone = ZoneInfo(timezone)
        self.random = random_source or random.SystemRandom()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.last_tick_at: datetime | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if not self._task or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop(), name="schedule-coordinator")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError): await self._task

    async def tick(self, now: datetime | None = None) -> None:
        now_utc = now.astimezone(UTC) if now else datetime.now(UTC)
        local = now_utc.astimezone(self.timezone).replace(second=0, microsecond=0)
        self.last_tick_at = now_utc
        await self._ensure_and_dispatch_daily_batch(local)
        await self._dispatch_account_overrides(local)
        await self._dispatch_due_products(now_utc)

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick(); self.last_error = None
            except Exception as exc:
                self.last_error = sanitize_log_value(exc, max_length=500)
                logger.error("scheduler tick failed: %s", self.last_error)
            await asyncio.sleep(self.tick_seconds)

    async def _ensure_and_dispatch_daily_batch(self, local: datetime) -> None:
        date_key = local.date().isoformat()
        batch = await self.store.get_batch_for_date(date_key)
        if not batch:
            accounts = await self.store.list_default_schedule_accounts()
            planned = await self.random_planned_at(local.date())
            batch = await self.store.create_batch(
                {"date_key": date_key, "planned_at": planned.astimezone(UTC)},
                [UUID(str(account["id"])) for account in accounts],
            )
        planned = _as_datetime(batch["planned_at"])
        if planned.tzinfo is None: planned = planned.replace(tzinfo=UTC)
        now_utc = local.astimezone(UTC)
        if (
            batch.get("status") == "pending"
            and not batch.get("started_at")
            and batch.get("trigger_reason", "normal") == "normal"
            and now_utc > planned.astimezone(UTC) + timedelta(minutes=5)
        ):
            latest = datetime.combine(local.date(), time(23, 0), self.timezone)
            recovery = min(local + timedelta(hours=1), latest)
            if recovery > local:
                regenerated = await self.store.regenerate_batch(date_key, recovery.astimezone(UTC))
                if regenerated:
                    return
        if now_utc < planned.astimezone(UTC): return
        for item in await self.store.list_batch_items(UUID(str(batch["id"]))):
            if item.get("job_id"):
                continue
            account_id = UUID(str(item["account_id"]))
            await self.jobs.enqueue_checkin(
                account_id,
                scheduled_for=planned.astimezone(UTC),
                idempotency_key=f"checkin:{account_id}:{date_key}",
                payload={"batch_id": batch["id"], "batch_item_id": item.get("id")},
                priority=50,
            )

    async def _dispatch_account_overrides(self, local: datetime) -> None:
        for account in await self.store.list_override_schedule_accounts():
            override = account.get("schedule_override") or {}
            account_id = UUID(str(account["id"]))
            try:
                planned = self._account_override_planned_at(account_id, local.date(), override)
            except (KeyError, TypeError, ValueError):
                logger.warning("invalid schedule override for account %s", account_id)
                continue
            if local < planned.replace(second=0, microsecond=0):
                continue
            await self.jobs.enqueue_checkin(
                account_id,
                scheduled_for=planned.astimezone(UTC),
                idempotency_key=f"checkin:{account_id}:{local.date().isoformat()}",
                priority=60,
            )

    def _account_override_planned_at(
        self, account_id: UUID, day: date, override: dict[str, Any]
    ) -> datetime:
        if override.get("mode") == "fixed_time":
            return datetime.combine(day, time.fromisoformat(str(override["time"])), self.timezone)
        if override.get("mode") != "custom_window":
            raise ValueError("unsupported schedule override")
        start = datetime.combine(day, time.fromisoformat(str(override["start"])), self.timezone)
        end = datetime.combine(day, time.fromisoformat(str(override["end"])), self.timezone)
        seconds = int((end - start).total_seconds())
        if seconds <= 0:
            raise ValueError("schedule window end must be after start")
        digest = hashlib.sha256(f"{account_id}:{day.isoformat()}".encode()).digest()
        return start + timedelta(seconds=int.from_bytes(digest[:8], "big") % seconds)

    async def _dispatch_due_products(self, now: datetime) -> None:
        settings = await self.store.get_settings()
        global_interval = int(settings.get("scheduler.product_interval_minutes", "60"))
        for due in await self.store.list_due_product_sites(now):
            site_id = UUID(str(due["site_id"]))
            interval = int(due.get("interval_override_minutes") or global_interval)
            bucket = int(now.timestamp()) // (interval * 60)
            await self.jobs.enqueue_product_sync(
                site_id,
                scheduled_for=now,
                idempotency_key=f"product:{site_id}:{interval}:{bucket}",
            )
            await self.store.mark_product_enqueued(site_id, now + timedelta(minutes=interval))

    async def random_planned_at(self, day: date) -> datetime:
        settings = await self.store.get_settings()
        start = time.fromisoformat(settings.get("scheduler.checkin_window_start", "08:00:00"))
        end = time.fromisoformat(settings.get("scheduler.checkin_window_end", "22:00:00"))
        start_dt = datetime.combine(day, start, self.timezone)
        end_dt = datetime.combine(day, end, self.timezone)
        seconds = int((end_dt - start_dt).total_seconds())
        if seconds <= 0: raise ValueError("check-in window end must be after start")
        return start_dt + timedelta(seconds=self.random.randrange(seconds))


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime): return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
