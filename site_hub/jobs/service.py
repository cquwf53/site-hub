from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from site_hub.models import JobKind, JobOut, JobStatus, TaskRef
from site_hub.services.events import EventBroker
from site_hub.services.ports import Store


def utcnow() -> datetime:
    return datetime.now(UTC)


class JobConflict(RuntimeError): pass
class JobNotFound(LookupError): pass
class StaleLease(RuntimeError): pass


class JobService:
    def __init__(self, store: Store, events: EventBroker, *, lease_seconds: int = 45) -> None:
        self.store = store
        self.events = events
        self.lease_seconds = lease_seconds

    async def enqueue_checkin(
        self,
        account_id: UUID,
        *,
        scheduled_for: datetime | None = None,
        idempotency_key: str | None = None,
        payload: dict[str, Any] | None = None,
        priority: int = 100,
    ) -> tuple[TaskRef, bool]:
        account = await self.store.get_account(account_id)
        if not account:
            raise JobNotFound("check-in account not found")
        site = await self.store.get_site(UUID(str(account["site_id"])))
        if not site:
            raise JobNotFound("site not found")
        now = utcnow()
        due = scheduled_for or now
        business_date = due.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        job, created = await self.store.enqueue_job({
            "id": uuid4(),
            "kind": JobKind.CHECKIN.value,
            "site_id": UUID(str(site["id"])),
            "account_id": UUID(str(account["id"])),
            "scheduled_for": due,
            "idempotency_key": f"checkin:{account_id}:{business_date}",
            "priority": priority,
            "max_attempts": 3,
            "payload": payload or {},
        })
        await self._publish(job)
        return self.task_ref(job, deduplicated=not created), created

    async def enqueue_product_sync(
        self,
        site_id: UUID,
        *,
        scheduled_for: datetime | None = None,
        idempotency_key: str | None = None,
        payload: dict[str, Any] | None = None,
        priority: int = 100,
    ) -> tuple[TaskRef, bool]:
        if not await self.store.get_site(site_id):
            raise JobNotFound("site not found")
        now = utcnow()
        job, created = await self.store.enqueue_job({
            "id": uuid4(),
            "kind": JobKind.PRODUCT_SYNC.value,
            "site_id": site_id,
            "account_id": None,
            "scheduled_for": scheduled_for or now,
            "idempotency_key": idempotency_key or f"product:{site_id}:manual:{uuid4()}",
            "priority": priority,
            "max_attempts": 3,
            "payload": payload or {},
        })
        await self._publish(job)
        return self.task_ref(job, deduplicated=not created), created

    async def enqueue_bookmark_import(
        self, *, job_id: UUID, relative_path: str, sha256: str, size: int,
    ) -> tuple[TaskRef, bool]:
        return await self._enqueue_system(
            JobKind.BOOKMARK_IMPORT, job_id=job_id, max_attempts=3,
            payload={"staging_path": relative_path, "sha256": sha256, "size": size},
        )

    async def enqueue_login_session(self, account_id: UUID) -> tuple[TaskRef, bool]:
        account = await self.store.get_account(account_id)
        if not account:
            raise JobNotFound("check-in account not found")
        site_id = UUID(str(account["site_id"]))
        if not await self.store.get_site(site_id):
            raise JobNotFound("site not found")
        return await self._enqueue_system(
            JobKind.LOGIN_SESSION, site_id=site_id, account_id=account_id, max_attempts=2,
        )

    async def enqueue_adapter_test(self, adapter_id: str) -> tuple[TaskRef, bool]:
        if not adapter_id or len(adapter_id) > 200:
            raise ValueError("invalid adapter id")
        return await self._enqueue_system(
            JobKind.ADAPTER_TEST, max_attempts=2, payload={"adapter_id": adapter_id},
        )

    async def _enqueue_system(
        self, kind: JobKind, *, job_id: UUID | None = None, site_id: UUID | None = None,
        account_id: UUID | None = None, max_attempts: int = 1,
        payload: dict[str, Any] | None = None,
    ) -> tuple[TaskRef, bool]:
        identifier = job_id or uuid4()
        job, created = await self.store.enqueue_job({
            "id": identifier,
            "kind": kind.value,
            "site_id": site_id,
            "account_id": account_id,
            "scheduled_for": utcnow(),
            "idempotency_key": f"{kind.value}:{identifier}",
            "priority": 100,
            "max_attempts": max_attempts,
            "payload": payload or {},
        })
        await self._publish(job)
        return self.task_ref(job, deduplicated=not created), created

    async def lease(self, kind: JobKind, worker_instance: str) -> dict[str, Any] | None:
        now = utcnow()
        job = await self.store.lease_next_job(
            kind=kind.value,
            worker_instance=worker_instance,
            now=now,
            lease_expires_at=now + timedelta(seconds=self.lease_seconds),
        )
        if job: await self._publish(job)
        return job

    async def mark_running(self, job_id: UUID, lease_token: str) -> dict[str, Any]:
        job = await self.store.mark_job_running(job_id, lease_token, utcnow())
        if not job: raise StaleLease("job lease is no longer current")
        await self._publish(job); return job

    async def heartbeat(self, job_id: UUID, lease_token: str, progress: dict[str, Any] | None = None) -> bool:
        now = utcnow()
        ok = await self.store.heartbeat_job(
            job_id, lease_token, now, now + timedelta(seconds=self.lease_seconds), progress
        )
        if ok:
            job = await self.store.get_job(job_id)
            if job: await self._publish(job)
        return ok

    async def complete(
        self,
        job_id: UUID,
        lease_token: str,
        *,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error_kind: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        if status not in (
            JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.SKIPPED,
            JobStatus.EFFECT_UNKNOWN, JobStatus.CANCELLED,
        ):
            raise ValueError("completion requires a terminal status")
        job = await self.store.complete_job(
            job_id, lease_token, status=status.value, result=result,
            error_kind=error_kind, error_message=error_message, now=utcnow(),
        )
        if not job: raise StaleLease("stale completion rejected")
        await self._publish(job); return job

    async def cancel(self, job_id: UUID) -> dict[str, Any]:
        existing = await self.store.get_job(job_id)
        if not existing: raise JobNotFound("job not found")
        job = await self.store.cancel_job(job_id, utcnow())
        if not job: raise JobConflict("terminal job cannot be cancelled")
        await self._publish(job); return job

    async def retry(self, job_id: UUID) -> dict[str, Any]:
        existing = await self.store.get_job(job_id)
        if not existing: raise JobNotFound("job not found")
        job = await self.store.retry_job(job_id, utcnow())
        if not job: raise JobConflict("only failed or cancelled jobs can be retried")
        await self._publish(job); return job

    async def recover(self) -> list[dict[str, Any]]:
        jobs = await self.store.recover_expired_jobs(utcnow())
        for job in jobs: await self._publish(job)
        return jobs

    async def _publish(self, job: dict[str, Any]) -> None:
        await self.events.publish("task", self.public_job(job))

    @staticmethod
    def public_job(job: dict[str, Any]) -> dict[str, Any]:
        hidden = {"payload", "payload_json", "lease_token", "config", "secrets"}
        return {key: value for key, value in job.items() if key not in hidden}

    @staticmethod
    def task_ref(job: dict[str, Any], *, deduplicated: bool = False) -> TaskRef:
        job_id = UUID(str(job["id"]))
        return TaskRef(
            job_id=job_id,
            kind=JobKind(job["kind"]),
            status=JobStatus(job["status"]),
            deduplicated=deduplicated,
            href=f"/api/v1/jobs/{job_id}",
        )

    @staticmethod
    def job_out(job: dict[str, Any]) -> JobOut:
        return JobOut.model_validate(JobService.public_job(job))
