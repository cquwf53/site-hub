from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, Awaitable, Callable
from uuid import UUID

from site_hub.jobs.service import JobService, StaleLease
from site_hub.models import JobKind, JobStatus
from site_hub.scrapers.network import sanitize_log_value
from site_hub.services.ports import StaleJobWrite


logger = logging.getLogger(__name__)
ProductHandler = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]


class JobDispatcher:
    """Runs product jobs locally and hands check-in jobs to the Node supervisor."""

    def __init__(self, service: JobService, *, product_handler: ProductHandler | None = None,
                 interval_seconds: float = 1.0, product_concurrency: int = 3) -> None:
        self.service = service
        self.product_handler = product_handler or self._unavailable_product_handler
        self.interval_seconds = interval_seconds
        self.product_concurrency = asyncio.Semaphore(product_concurrency)
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.last_error: str | None = None
        self._inflight: set[asyncio.Task[Any]] = set()
        self._inflight_by_job: dict[UUID, asyncio.Task[Any]] = {}

    def start(self) -> None:
        if not self._task or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop(), name="product-job-dispatcher")

    async def stop(self, grace_seconds: float = 20) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError): await self._task
        if self._inflight:
            _, pending = await asyncio.wait(self._inflight, timeout=grace_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def cancel_execution(self, job_id: UUID) -> bool:
        """Cancel an active product handler and wait for its cancellation cleanup."""
        task = self._inflight_by_job.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return True

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._dispatch_forever()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = sanitize_log_value(exc, max_length=500)
                logger.error("dispatcher recovered from error: %s", self.last_error)
                await asyncio.sleep(self.interval_seconds)

    async def _dispatch_forever(self) -> None:
        while not self._stopping.is_set():
            await self.service.recover()
            await self.product_concurrency.acquire()
            try:
                job = await self.service.lease(JobKind.PRODUCT_SYNC, "python-product")
                self.last_error = None
            except BaseException:
                self.product_concurrency.release()
                raise
            if not job:
                self.product_concurrency.release()
                await asyncio.sleep(self.interval_seconds); continue
            task = asyncio.create_task(self._run_product(job), name=f"product-{job['id']}")
            job_id = UUID(str(job["id"]))
            self._inflight.add(task)
            self._inflight_by_job[job_id] = task

            def finished(done: asyncio.Task[Any], *, identifier: UUID = job_id) -> None:
                self._inflight.discard(done)
                if self._inflight_by_job.get(identifier) is done:
                    self._inflight_by_job.pop(identifier, None)

            task.add_done_callback(finished)

    async def _run_product(self, job: dict[str, Any]) -> None:
        job_id = UUID(str(job["id"])); token = str(job["lease_token"])
        heartbeat: asyncio.Task[None] | None = None
        try:
            await self.service.mark_running(job_id, token)
            heartbeat = asyncio.create_task(self._heartbeat(job_id, token), name=f"product-heartbeat-{job_id}")
            getter = getattr(self.service.store, "get_site_runtime", self.service.store.get_site)
            site = await getter(UUID(str(job["site_id"])))
            if not site: raise LookupError("site was deleted")
            result = await self.product_handler(site, job)
            if result.get("catalog_anomaly"):
                await self.service.complete(job_id, token, status=JobStatus.FAILED, result=result,
                    error_kind="catalog_anomaly", error_message="商品抓取数量异常，已保留原目录；请检查抓取器后再同步")
                return
            status = JobStatus.SKIPPED if result.get("skipped") else JobStatus.SUCCEEDED
            await self.service.complete(job_id, token, status=status, result=result)
        except StaleLease:
            logger.info("product job %s lost its lease", job_id)
        except StaleJobWrite:
            logger.info("product job %s result was rejected after its lease changed", job_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            safe_error = sanitize_log_value(exc, max_length=500)
            logger.error("product job %s failed: %s", job_id, safe_error)
            with suppress(StaleLease):
                await self.service.complete(job_id, token, status=JobStatus.FAILED,
                                            error_kind="execution", error_message=safe_error)
        finally:
            if heartbeat:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError, Exception): await heartbeat
            self.product_concurrency.release()

    async def _heartbeat(self, job_id: UUID, token: str) -> None:
        interval = max(3.0, self.service.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            if not await self.service.heartbeat(job_id, token, {"phase": "scraping"}):
                return

    @staticmethod
    async def _unavailable_product_handler(site: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("product scraper service is not configured")
