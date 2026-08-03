from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import UUID

from site_hub.jobs.service import JobService, StaleLease
from site_hub.models import JobKind, JobStatus
from site_hub.scrapers.network import sanitize_log_value
from site_hub.services.bookmark_import import BookmarkImportService


logger = logging.getLogger(__name__)
SYSTEM_JOB_KINDS = (
    JobKind.BOOKMARK_IMPORT,
    JobKind.LOGIN_SESSION,
    JobKind.ADAPTER_TEST,
)
BUILTIN_ADAPTER_IDS = {"classic", "next_api", "custom_spa", "manual", "manager"}


class SystemExecutionError(RuntimeError):
    def __init__(self, message: str, *, error_kind: str = "execution", effect_unknown: bool = False) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.effect_unknown = effect_unknown


class BookmarkStaging:
    """Controlled, integrity-checked storage for uploaded bookmark exports."""

    def __init__(self, root: Path, *, max_bytes: int = 16 * 1024 * 1024) -> None:
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes

    async def stage(self, job_id: UUID, content: bytes) -> dict[str, Any]:
        if not content:
            raise ValueError("bookmark file is empty")
        if len(content) > self.max_bytes:
            raise ValueError(f"bookmark file exceeds {self.max_bytes} bytes")
        relative = Path("bookmarks") / f"{job_id}.html"
        digest = hashlib.sha256(content).hexdigest()
        await asyncio.to_thread(self._write_atomic, relative, content)
        return {"relative_path": relative.as_posix(), "sha256": digest, "size": len(content)}

    async def read_verified(self, payload: dict[str, Any]) -> bytes:
        relative = str(payload.get("staging_path") or "")
        path = self._resolve(relative)
        content = await asyncio.to_thread(path.read_bytes)
        if len(content) > self.max_bytes or len(content) != int(payload.get("size", -1)):
            raise SystemExecutionError("staged bookmark size mismatch")
        digest = hashlib.sha256(content).hexdigest()
        if digest != str(payload.get("sha256") or ""):
            raise SystemExecutionError("staged bookmark checksum mismatch")
        return content

    async def cleanup(self, payload: dict[str, Any]) -> None:
        relative = str(payload.get("staging_path") or "")
        if not relative:
            return
        with suppress(FileNotFoundError, ValueError):
            await asyncio.to_thread(self._resolve(relative).unlink)

    def _write_atomic(self, relative: Path, content: bytes) -> None:
        target = self._resolve(relative.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    def _resolve(self, relative: str) -> Path:
        candidate = Path(relative)
        if not relative or candidate.is_absolute():
            raise ValueError("invalid staging path")
        resolved = (self.root / candidate).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("staging path escapes the controlled directory")
        return resolved


class SystemJobDispatcher:
    """Executes durable Python control-plane jobs one at a time."""

    def __init__(self, service: JobService, *, supervisor: Any, staging: BookmarkStaging,
                 interval_seconds: float = 0.25) -> None:
        self.service = service
        self.supervisor = supervisor
        self.staging = staging
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._active_task: asyncio.Task[None] | None = None
        self._active_job_id: UUID | None = None
        self._user_cancelled: set[UUID] = set()
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if not self._task or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop(), name="system-job-dispatcher")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._active_task:
            self._active_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._active_task

    async def cancel_execution(self, job_id: UUID) -> None:
        job = await self.service.store.get_job(job_id)
        if not job or JobKind(str(job["kind"])) not in SYSTEM_JOB_KINDS:
            return
        self._user_cancelled.add(job_id)
        if self._active_job_id == job_id and self._active_task:
            self._active_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._active_task
        elif job["kind"] == JobKind.BOOKMARK_IMPORT.value:
            await self.staging.cleanup(job.get("payload") or {})

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            job = None
            for kind in SYSTEM_JOB_KINDS:
                if kind == JobKind.LOGIN_SESSION and self.supervisor.state != "ready":
                    continue
                job = await self.service.lease(kind, "python-system")
                if job:
                    break
            if not job:
                await asyncio.sleep(self.interval_seconds)
                continue
            self._active_job_id = UUID(str(job["id"]))
            self._active_task = asyncio.create_task(
                self._run(job), name=f"system-{job['kind']}-{job['id']}",
            )
            try:
                await self._active_task
            except asyncio.CancelledError:
                if self._stopping.is_set():
                    raise
            finally:
                self._active_task = None
                self._active_job_id = None

    async def _run(self, job: dict[str, Any]) -> None:
        job_id = UUID(str(job["id"]))
        token = str(job["lease_token"])
        heartbeat: asyncio.Task[None] | None = None
        terminal = False
        try:
            await self.service.mark_running(job_id, token)
            heartbeat = asyncio.create_task(self._heartbeat(job_id, token), name=f"system-heartbeat-{job_id}")
            result = await self._execute(job)
            completed = await self.service.complete(job_id, token, status=JobStatus.SUCCEEDED, result=result)
            terminal = completed["status"] != JobStatus.QUEUED.value
        except SystemExecutionError as exc:
            status = JobStatus.EFFECT_UNKNOWN if exc.effect_unknown else JobStatus.FAILED
            safe_error = sanitize_log_value(exc, max_length=500)
            with suppress(StaleLease):
                completed = await self.service.complete(
                    job_id, token, status=status, error_kind=exc.error_kind,
                    error_message=safe_error,
                )
                terminal = completed["status"] != JobStatus.QUEUED.value
        except StaleLease:
            logger.info("system job %s lost its lease", job_id)
        except asyncio.CancelledError:
            if job_id in self._user_cancelled:
                terminal = True
                if job["kind"] == JobKind.LOGIN_SESSION.value and job.get("account_id"):
                    with suppress(Exception):
                        await self.supervisor.close_session(UUID(str(job["account_id"])))
            raise
        except Exception as exc:
            safe_error = sanitize_log_value(exc, max_length=500)
            logger.error("system job %s failed: %s", job_id, safe_error)
            with suppress(StaleLease):
                completed = await self.service.complete(
                    job_id, token, status=JobStatus.FAILED, error_kind="infrastructure",
                    error_message=safe_error,
                )
                terminal = completed["status"] != JobStatus.QUEUED.value
        finally:
            if heartbeat:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            self._user_cancelled.discard(job_id)
            if terminal and job["kind"] == JobKind.BOOKMARK_IMPORT.value:
                await self.staging.cleanup(job.get("payload") or {})

    async def _execute(self, job: dict[str, Any]) -> dict[str, Any]:
        kind = JobKind(str(job["kind"]))
        payload = job.get("payload") or {}
        if kind == JobKind.BOOKMARK_IMPORT:
            content = await self.staging.read_verified(payload)
            report = await BookmarkImportService(self.service.store).import_bytes(content)
            return {**report, "source_sha256": payload["sha256"], "source_size": payload["size"]}
        if kind == JobKind.LOGIN_SESSION:
            account_id = UUID(str(job["account_id"]))
            setter = getattr(self.service.store, "set_account_auth_state", None)
            if setter:
                await setter(account_id, "opening")
            try:
                result = await self.supervisor.open_session(account_id)
            except asyncio.TimeoutError as exc:
                raise SystemExecutionError(
                    "login session outcome is unknown after worker timeout",
                    error_kind="timeout", effect_unknown=True,
                ) from exc
            state = str(result.get("state") or "opened")[:64]
            return {"account_id": str(account_id), "state": state, "worker_state": self.supervisor.state}
        if kind == JobKind.ADAPTER_TEST:
            adapter_id = str(payload.get("adapter_id") or "")
            worker_ids = {
                str(item.get("id") or item.get("adapterId") or "")
                for item in self.supervisor.adapters
            }
            if adapter_id not in BUILTIN_ADAPTER_IDS | worker_ids:
                raise SystemExecutionError("adapter not found", error_kind="not_found")
            source = "worker" if adapter_id in worker_ids else "python"
            return {"adapter_id": adapter_id, "source": source, "worker_state": self.supervisor.state}
        raise SystemExecutionError(f"unsupported system job kind: {kind}")

    async def _heartbeat(self, job_id: UUID, token: str) -> None:
        interval = max(3.0, self.service.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            if not await self.service.heartbeat(job_id, token, {"phase": "executing"}):
                return


__all__ = ["BookmarkStaging", "SystemJobDispatcher", "SystemExecutionError"]
