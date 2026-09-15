"""Persistent local maintenance with conservative, explicitly enabled retention."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import logging
import os
import re
import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from site_hub.db import Database
from site_hub.services.backups import BackupService

_SETTINGS_KEY = "maintenance.settings.v1"
_STATE_KEY = "maintenance.state.v1"
_CLEANUP_KEYS = ("backups", "logs", "screenshots", "price_history")
_LOG_ARCHIVE = re.compile(r".+\.log\.\d+$|.+[.-]\d{4}-\d{2}-\d{2}(?:\.\d+)?\.log$")
logger = logging.getLogger(__name__)


class MaintenanceBusyError(RuntimeError):
    pass


class MaintenanceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    automatic_backup_enabled: bool = True
    backup_interval_hours: int = Field(default=24, ge=1, le=720)
    retention_enabled: bool = False
    backup_keep_count: int = Field(default=7, ge=1, le=365)
    log_retention_days: int = Field(default=30, ge=1, le=3650)
    screenshot_retention_days: int = Field(default=30, ge=1, le=3650)
    price_history_retention_days: int = Field(default=365, ge=1, le=3650)


class MaintenanceBackup(BaseModel):
    backup_id: str
    created_at: str
    kind: Literal["manual", "automatic", "pre_restore"]
    label: str | None
    database_file: str
    database_size_bytes: int
    database_sha256: str


class MaintenanceVerification(BaseModel):
    backup_id: str
    verified_at: str
    integrity_check: Literal["ok"]


class MaintenanceCleanup(BaseModel):
    backups: int = 0
    logs: int = 0
    screenshots: int = 0
    price_history: int = 0


class MaintenanceStatus(BaseModel):
    running: bool
    last_run_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    next_run_at: str | None = None
    latest_backup: MaintenanceBackup | None = None
    latest_verification: MaintenanceVerification | None = None
    last_cleanup: MaintenanceCleanup = Field(default_factory=MaintenanceCleanup)


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _is_reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _managed_file(root: Path, relative: str) -> Path | None:
    """Reject absolute paths, traversal, alternate streams and every reparse component."""
    if not relative or "\\" in relative or ":" in relative:
        return None
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts) or Path(relative).is_absolute():
        return None
    try:
        if not root.is_dir() or _is_reparse(root):
            return None
        candidate = root
        for part in parts:
            candidate = candidate / part
            if _is_reparse(candidate):
                return None
        if not candidate.is_file() or not candidate.resolve().is_relative_to(root.resolve()):
            return None
        return candidate
    except OSError:
        return None


def _remove_inactive_log(path: Path) -> bool:
    # An exclusive Windows handle refuses any file still held by another logger.
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        )
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.CreateFileW(str(path), 0x00010000, 0, None, 3, 0x04000000, None)
        if handle == ctypes.c_void_p(-1).value:
            return False
        kernel.CloseHandle(handle)
        return True
    # Only the Windows implementation is used in production.
    import fcntl
    try:
        with path.open("rb") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            path.unlink()
        return True
    except OSError:
        return False


class MaintenanceService:
    def __init__(self, database: Database, backups: BackupService, data_dir: str | Path):
        self.database = database
        self.backups = backups
        self.data_dir = Path(data_dir).resolve()
        self._run_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._task: asyncio.Task[None] | None = None
        self._stopping: asyncio.Event | None = None
        self._closed = False

    def _read(self, key: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return {}
        value = json.loads(row["value_json"])
        if not isinstance(value, dict):
            raise ValueError("invalid maintenance configuration")
        return value

    def _write(self, key: str, value: dict[str, Any]) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                   updated_at=excluded.updated_at""",
                (key, json.dumps(value, separators=(",", ":")), _stamp(_now())),
            )

    def initialize(self) -> None:
        with self._state_lock:
            settings = self.get_settings()
            state = self._read(_STATE_KEY)
            if "next_run_at" not in state:
                state["next_run_at"] = (
                    _stamp(_now() + timedelta(hours=settings["backup_interval_hours"]))
                    if settings["automatic_backup_enabled"] else None
                )
                self._write(_STATE_KEY, state)

    def get_settings(self) -> dict[str, Any]:
        return MaintenanceSettings.model_validate(self._read(_SETTINGS_KEY)).model_dump()

    def update_settings(self, value: dict[str, Any]) -> dict[str, Any]:
        settings = MaintenanceSettings.model_validate(value).model_dump()
        if not self._run_lock.acquire(blocking=False):
            raise MaintenanceBusyError("maintenance is running")
        try:
            with self._state_lock:
                previous = self.get_settings()
                state = self._read(_STATE_KEY)
                if (settings["automatic_backup_enabled"] != previous["automatic_backup_enabled"]
                        or settings["backup_interval_hours"] != previous["backup_interval_hours"]):
                    state["next_run_at"] = (
                        _stamp(_now() + timedelta(hours=settings["backup_interval_hours"]))
                        if settings["automatic_backup_enabled"] else None
                    )
                with self.database.transaction(immediate=True) as connection:
                    for key, document in ((_SETTINGS_KEY, settings), (_STATE_KEY, state)):
                        connection.execute(
                            """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
                               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                               updated_at=excluded.updated_at""",
                            (key, json.dumps(document, separators=(",", ":")), _stamp(_now())),
                        )
            return settings
        finally:
            self._run_lock.release()

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            state = self._read(_STATE_KEY)
        items = self.backups.list_backups()
        latest = next((item.record for item in items if item.record is not None), None)
        return MaintenanceStatus(
            running=self._run_lock.locked(),
            last_run_at=state.get("last_run_at"),
            last_success_at=state.get("last_success_at"),
            last_error=state.get("last_error"),
            next_run_at=state.get("next_run_at"),
            latest_backup=latest.to_dict() if latest is not None else None,
            latest_verification=self.backups.latest_verification(),
            last_cleanup=state.get("last_cleanup", {}),
        ).model_dump()

    async def start(self) -> None:
        if self._task is not None:
            return
        await asyncio.to_thread(self.initialize)
        self._closed = False
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="sitehub-maintenance")

    async def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            assert self._stopping is not None
            self._stopping.set()
            # Do not cancel to_thread: filesystem work must finish before the
            # application's database owner/instance lock can be released.
            await self._task
            self._task = None
        # A manual HTTP maintenance run may also be using a worker thread.
        await asyncio.to_thread(self._wait_for_idle)

    def _wait_for_idle(self) -> None:
        with self._run_lock:
            pass

    async def _loop(self) -> None:
        assert self._stopping is not None
        while not self._stopping.is_set():
            try:
                await asyncio.to_thread(self.run_due)
            except Exception:
                logger.error("MAINTENANCE_EXECUTION_FAILED")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=30)
            except TimeoutError:
                pass

    def run_due(self) -> dict[str, Any] | None:
        settings = self.get_settings()
        state = self._read(_STATE_KEY)
        due = _parse(state.get("next_run_at"))
        if not settings["automatic_backup_enabled"] or due is None or due > _now():
            return None
        try:
            return self.run_once(manual=False)
        except MaintenanceBusyError:
            return None

    def run_once(self, *, manual: bool = True) -> dict[str, Any]:
        if self._closed:
            raise MaintenanceBusyError("maintenance is stopping")
        if not self._run_lock.acquire(blocking=False):
            raise MaintenanceBusyError("maintenance is running")
        if self._closed:
            self._run_lock.release()
            raise MaintenanceBusyError("maintenance is stopping")
        failure: Exception | None = None
        counts = dict.fromkeys(_CLEANUP_KEYS, 0)
        settings = MaintenanceSettings().model_dump()
        started = _now()
        try:
            settings = self.get_settings()
            with self._state_lock:
                state = self._read(_STATE_KEY)
                state.update(last_run_at=_stamp(started), last_error=None)
                self._write(_STATE_KEY, state)
            backup = self.backups.create_backup(
                kind="manual" if manual else "automatic",
                label="Manual maintenance" if manual else "Scheduled maintenance",
            )
            self.backups.verify_backup(backup.backup_id)
            if settings["retention_enabled"]:
                # A staged restore protects its files and associated records:
                # postpone all retention until the restore was applied/cancelled.
                with self.backups.retention_guard() as permitted:
                    if permitted:
                        counts["backups"] = self.backups.prune_automatic_backups(settings["backup_keep_count"])
                        counts["screenshots"] = self._prune_screenshots(started, settings["screenshot_retention_days"])
                        counts["logs"] = self._prune_logs(started, settings["log_retention_days"])
                        counts["price_history"] = self._prune_prices(started, settings["price_history_retention_days"])
        except Exception as exc:
            failure = exc
        finally:
            try:
                with self._state_lock:
                    state = self._read(_STATE_KEY)
                    state["last_cleanup"] = counts
                    state["last_error"] = "MAINTENANCE_FAILED" if failure is not None else None
                    if failure is None:
                        state["last_success_at"] = _stamp(_now())
                    state["next_run_at"] = (
                        _stamp(started + timedelta(hours=settings["backup_interval_hours"]))
                        if settings["automatic_backup_enabled"] else None
                    )
                    self._write(_STATE_KEY, state)
            finally:
                self._run_lock.release()
        if failure is not None:
            raise RuntimeError("MAINTENANCE_FAILED") from failure
        return self.status()

    def _prune_screenshots(self, now: datetime, days: int) -> int:
        cutoff = _stamp(now - timedelta(days=days))
        root = self.data_dir / "artifacts"
        removed = 0
        with self.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                """SELECT a.* FROM artifacts a JOIN runs r ON r.id=a.run_id
                   LEFT JOIN jobs j ON j.id=r.job_id
                   WHERE a.role='screenshot' AND a.created_at<?
                     AND r.status IN ('succeeded','failed','skipped','effect_unknown','cancelled')
                     AND r.finished_at IS NOT NULL AND r.finished_at<?
                     AND (j.id IS NULL OR j.status IN ('succeeded','failed','skipped','effect_unknown','cancelled'))
                     AND (a.retention_until IS NULL OR a.retention_until<=?)
                   ORDER BY a.created_at LIMIT 250""",
                (cutoff, cutoff, _stamp(now)),
            ).fetchall()
            for row in rows:
                path = _managed_file(root, row["relative_path"])
                if path is None or not row["sha256"] or row["size"] is None:
                    continue
                try:
                    before = path.stat()
                    if before.st_size != row["size"] or before.st_mtime >= (now - timedelta(days=days)).timestamp():
                        continue
                    digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    after = path.stat()
                    if (digest.hexdigest() != row["sha256"] or
                            (after.st_ino, after.st_mtime_ns, after.st_size) !=
                            (before.st_ino, before.st_mtime_ns, before.st_size)):
                        continue
                    path.unlink()
                except OSError:
                    continue
                connection.execute("DELETE FROM artifacts WHERE id=?", (row["id"],))
                removed += 1
        return removed

    def _prune_logs(self, now: datetime, days: int) -> int:
        root = self.data_dir / "logs"
        if not root.exists() or _is_reparse(root):
            return 0
        active: set[Path] = set()
        for handler_ref in logging._handlerList:
            handler = handler_ref()
            if isinstance(handler, logging.FileHandler):
                active.add(Path(handler.baseFilename).resolve())
        removed = 0
        cutoff = (now - timedelta(days=days)).timestamp()
        for entry in sorted(root.iterdir()):
            if removed >= 250:
                break
            if not _LOG_ARCHIVE.fullmatch(entry.name):
                continue
            path = _managed_file(root, entry.name)
            if path is None or path.resolve() in active or path.stat().st_mtime >= cutoff:
                continue
            if _remove_inactive_log(path):
                removed += 1
        return removed

    def _prune_prices(self, now: datetime, days: int) -> int:
        cutoff = _stamp(now - timedelta(days=days))
        with self.database.transaction(immediate=True) as connection:
            # Preserve each product's newest observation even when the whole
            # history predates the retention window, and never prune active runs.
            return connection.execute(
                """DELETE FROM price_observations WHERE id IN (
                   SELECT p.id FROM price_observations p
                   LEFT JOIN runs r ON r.id=p.source_run_id
                   LEFT JOIN jobs j ON j.id=r.job_id
                   WHERE p.observed_at<?
                     AND (r.id IS NULL OR (r.status!='running' AND r.finished_at IS NOT NULL))
                     AND (j.id IS NULL OR j.status IN ('succeeded','failed','skipped','effect_unknown','cancelled'))
                     AND EXISTS (SELECT 1 FROM price_observations newer
                       WHERE newer.product_id=p.product_id
                         AND (newer.observed_at>p.observed_at
                              OR (newer.observed_at=p.observed_at AND newer.id>p.id)))
                   ORDER BY p.observed_at LIMIT 5000)""",
                (cutoff,),
            ).rowcount
