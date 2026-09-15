"""Safe, self-contained SQLite backup and offline restore primitives.

The web layer deliberately does not live here.  A caller may create and verify
backups while SiteHub is running, but restoring is an offline maintenance
operation.  ``restore_backup_offline`` acquires the same data-directory lock as
the application and therefore refuses to replace a database owned by a running
SiteHub process.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from site_hub.services.single_instance import SingleInstanceError, SingleInstanceLock


BACKUP_FORMAT_VERSION = 1
DATABASE_FILENAME = "site-hub.db"
MANIFEST_FILENAME = "manifest.json"
PENDING_RESTORE_FILENAME = "restore-pending.json"
_BACKUP_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BACKUP_KINDS = frozenset({"manual", "automatic", "pre_restore"})
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_LABEL_LENGTH = 200


class BackupError(RuntimeError):
    """Base class for backup failures safe for an API layer to translate."""


class BackupPathError(BackupError):
    """A requested backup path escaped or violated the managed layout."""


class BackupNotFoundError(BackupError):
    """The requested managed backup does not exist."""


class BackupValidationError(BackupError):
    """A manifest, digest, or SQLite integrity check failed."""


class RestoreRequiresOfflineError(BackupError):
    """The database is still owned or used by a running SiteHub instance."""


class BackupRestoreError(BackupError):
    """An offline restore failed, with rollback details in the message."""


@dataclass(frozen=True, slots=True)
class BackupRecord:
    backup_id: str
    created_at: str
    kind: Literal["manual", "automatic", "pre_restore"]
    label: str | None
    database_file: str
    database_size_bytes: int
    database_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BackupListItem:
    backup_id: str
    status: Literal["available", "invalid"]
    record: BackupRecord | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["record"] = self.record.to_dict() if self.record is not None else None
        return result


@dataclass(frozen=True, slots=True)
class BackupVerification:
    record: BackupRecord
    verified_at: str
    integrity_check: Literal["ok"] = "ok"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["record"] = self.record.to_dict()
        return result


@dataclass(frozen=True, slots=True)
class RestoreResult:
    restored_backup: BackupRecord
    rollback_backup: BackupRecord
    restored_at: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["restored_backup"] = self.restored_backup.to_dict()
        result["rollback_backup"] = self.rollback_backup.to_dict()
        return result


@dataclass(frozen=True, slots=True)
class PendingRestore:
    backup_id: str
    staged_at: str
    database_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BackupService:
    """Manage verified SQLite backups beneath ``<data_dir>/backups`` only.

    ``create_backup`` uses ``sqlite3.Connection.backup`` and is safe during
    normal WAL-mode writes.  ``restore_backup_offline`` must be called by a
    maintenance process after the FastAPI lifespan and all database users have
    stopped.  It enforces that contract by acquiring SiteHub's single-instance
    lock before touching the live database.
    """

    def __init__(self, data_dir: str | Path, *, timeout: float = 5.0) -> None:
        raw_data_dir = Path(data_dir).expanduser()
        self.data_dir = raw_data_dir.resolve()
        self.database_path = self.data_dir / DATABASE_FILENAME
        self.backups_dir = self.data_dir / "backups"
        self.timeout = max(float(timeout), 0.1)
        self._mutex = threading.RLock()

    def create_backup(
        self,
        *,
        label: str | None = None,
        kind: Literal["manual", "automatic", "pre_restore"] = "manual",
    ) -> BackupRecord:
        """Create and fully verify one consistent backup."""
        normalized_label = self._normalize_label(label)
        if kind not in _BACKUP_KINDS:
            raise ValueError("unsupported backup kind")
        with self._mutex:
            return self._create_backup_locked(label=normalized_label, kind=kind)

    def list_backups(self) -> list[BackupListItem]:
        """List managed backups without hashing every database on each request.

        Manifest structure and the recorded file size are still checked.  Use
        ``verify_backup`` before download or restore for SHA-256 and SQLite
        ``integrity_check`` validation.
        """
        with self._mutex:
            root = self._ensure_backup_root()
            items: list[BackupListItem] = []
            for entry in root.iterdir():
                if not entry.is_dir() or entry.name.startswith(".tmp-"):
                    continue
                try:
                    record = self._load_record(entry.name, verify_contents=False)
                except BackupError as exc:
                    items.append(
                        BackupListItem(
                            backup_id=entry.name,
                            status="invalid",
                            error=str(exc),
                        )
                    )
                else:
                    items.append(
                        BackupListItem(
                            backup_id=record.backup_id,
                            status="available",
                            record=record,
                        )
                    )
            return sorted(
                items,
                key=lambda item: (
                    item.record.created_at if item.record is not None else "",
                    item.backup_id,
                ),
                reverse=True,
            )

    def verify_backup(self, backup_id: str) -> BackupVerification:
        """Validate the manifest, size, SHA-256, and SQLite integrity."""
        with self._mutex:
            record = self._load_record(backup_id, verify_contents=True)
            verification = BackupVerification(record=record, verified_at=_utc_now())
            self._save_verification(verification)
            return verification

    def latest_verification(self) -> dict[str, Any] | None:
        """Return the last completed integrity check, including checks from the UI."""
        with self._mutex:
            root = self._ensure_backup_root()
            path = root / "verification-latest.json"
            if not path.exists():
                return None
            self._assert_regular_managed_file(path, root, "verification record")
            try:
                if path.stat().st_size > _MAX_MANIFEST_BYTES:
                    return None
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or not isinstance(value.get("verified_at"), str):
                    return None
                self._validate_backup_id(value["backup_id"])
                if not _is_utc_timestamp(value["verified_at"]) or value["integrity_check"] != "ok":
                    return None
                return {key: value[key] for key in ("backup_id", "verified_at", "integrity_check")}
            except (BackupError, OSError, ValueError, TypeError, KeyError):
                return None

    def _save_verification(self, verification: BackupVerification) -> None:
        root = self._ensure_backup_root()
        path = root / "verification-latest.json"
        temporary = root / f".verification-{uuid.uuid4().hex}.tmp"
        try:
            _write_json_fsync(temporary, {
                "backup_id": verification.record.backup_id,
                "verified_at": verification.verified_at,
                "integrity_check": verification.integrity_check,
            })
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def retention_guard(self):
        """Serialize cleanup with staging/cancelling a restore request."""
        with self._mutex:
            yield self._load_pending_restore() is None

    def prune_automatic_backups(self, keep_count: int) -> int:
        """Retain newest automatic snapshots, all manual/rollback and staged backups.

        Unknown files make a directory ineligible; deletion never recurses.
        The maintenance caller first creates a fresh verified snapshot.
        """
        if isinstance(keep_count, bool) or keep_count < 1:
            raise ValueError("at least one automatic backup must be retained")
        with self._mutex:
            pending = self._load_pending_restore()
            records = [item.record for item in self.list_backups()
                       if item.record is not None and item.record.kind == "automatic"]
            if not records:
                return 0
            self._load_record(records[0].backup_id, verify_contents=True)
            protected = {record.backup_id for record in records[:keep_count]}
            if pending is not None:
                protected.add(pending.backup_id)
            removed = 0
            for record in records:
                if record.backup_id in protected:
                    continue
                directory = self._safe_backup_dir(record.backup_id, must_exist=True)
                children = list(directory.iterdir())
                if {child.name for child in children} != {DATABASE_FILENAME, MANIFEST_FILENAME}:
                    continue
                for child in children:
                    self._assert_regular_managed_file(child, directory, "backup file")
                (directory / DATABASE_FILENAME).unlink()
                (directory / MANIFEST_FILENAME).unlink()
                directory.rmdir()
                removed += 1
            return removed

    def stage_restore(self, backup_id: str) -> PendingRestore:
        """Verify a backup and arrange an offline restore for the next startup."""
        with self._mutex:
            record = self._load_record(backup_id, verify_contents=True)
            pending = PendingRestore(
                backup_id=record.backup_id,
                staged_at=_utc_now(),
                database_sha256=record.database_sha256,
            )
            root = self._ensure_backup_root()
            marker = root / PENDING_RESTORE_FILENAME
            self._assert_direct_child(marker, root)
            temporary = root / f".{PENDING_RESTORE_FILENAME}.{uuid.uuid4().hex}.tmp"
            self._assert_direct_child(temporary, root)
            try:
                _write_json_fsync(
                    temporary,
                    {
                        "format_version": BACKUP_FORMAT_VERSION,
                        **pending.to_dict(),
                    },
                )
                os.replace(temporary, marker)
                _fsync_directory(root)
            finally:
                temporary.unlink(missing_ok=True)
            return pending

    def pending_restore(self) -> PendingRestore | None:
        """Return the staged restore request, if one exists and is valid."""
        with self._mutex:
            return self._load_pending_restore()

    def cancel_pending_restore(self) -> bool:
        with self._mutex:
            marker = self._pending_restore_path()
            if not marker.exists():
                return False
            if marker.is_symlink() or not marker.is_file():
                raise BackupPathError("pending restore marker is not a regular file")
            marker.unlink()
            _fsync_directory(marker.parent)
            return True

    def apply_pending_restore_with_instance_lock(self) -> RestoreResult | None:
        """Apply a staged restore after the caller owns SiteHub's instance lock.

        This is intended for the FastAPI startup path before the data store is
        initialized.  It deliberately does not acquire a second Windows mutex;
        callers must already own the same ``SingleInstanceLock`` used by the
        running application.
        """
        with self._mutex:
            pending = self._load_pending_restore()
            if pending is None:
                return None
            selected = self._load_record(pending.backup_id, verify_contents=True)
            if selected.database_sha256 != pending.database_sha256:
                raise BackupValidationError(
                    "pending restore backup no longer matches the staged digest"
                )
            # Keep the marker until the application has successfully opened,
            # migrated, and recovered the restored database.  Startup calls
            # ``confirm_pending_restore`` only after those checks pass.  This
            # lets it put the pre-restore snapshot back if initialization
            # fails, instead of leaving an unbootable database installed.
            return self._restore_selected_locked(selected)

    def confirm_pending_restore(self, backup_id: str) -> bool:
        """Remove a staged marker after the restored database is ready."""
        with self._mutex:
            pending = self._load_pending_restore()
            if pending is None:
                return False
            if pending.backup_id != backup_id:
                raise BackupValidationError(
                    "pending restore identifier changed before confirmation"
                )
            marker = self._pending_restore_path()
            marker.unlink()
            _fsync_directory(marker.parent)
            return True

    def rollback_applied_restore_with_instance_lock(
        self, result: RestoreResult,
    ) -> None:
        """Reinstall the pre-restore snapshot after startup validation fails.

        The caller must still own SiteHub's process-wide instance lock and must
        have stopped/closed every database user.  The pending marker is only
        removed after the rollback database has been installed successfully.
        """
        with self._mutex:
            pending = self._load_pending_restore()
            if pending is None or pending.backup_id != result.restored_backup.backup_id:
                raise BackupValidationError(
                    "pending restore marker no longer matches the applied restore"
                )
            rollback = self._load_record(
                result.rollback_backup.backup_id, verify_contents=True,
            )
            self._require_quiescent_database()
            try:
                self._install_database(rollback)
            except BaseException as exc:
                # Preserve the marker for diagnosis/recovery when even the
                # verified rollback cannot be installed.
                raise BackupRestoreError(
                    "restored database failed startup validation and automatic rollback failed"
                ) from exc
            marker = self._pending_restore_path()
            marker.unlink()
            _fsync_directory(marker.parent)

    def restore_backup_offline(self, backup_id: str) -> RestoreResult:
        """Restore a verified backup while refusing any live SiteHub owner.

        This method is intentionally synchronous.  A graphical API should
        validate/stage the request first, stop the application, close database
        users, and invoke this method from its maintenance/restart helper.  It
        must not be called as an in-process HTTP handler while SiteHub is live.
        """
        with self._mutex:
            # Validate before any lock, checkpoint, or rollback-side effect.
            selected = self._load_record(backup_id, verify_contents=True)
            instance_lock = SingleInstanceLock(self.data_dir)
            try:
                instance_lock.acquire()
            except SingleInstanceError as exc:
                raise RestoreRequiresOfflineError(
                    "restore requires SiteHub to be fully stopped"
                ) from exc

            try:
                # Validate again after obtaining the maintenance lock so a file
                # cannot be swapped between preflight and installation.
                selected = self._load_record(backup_id, verify_contents=True)
                return self._restore_selected_locked(selected)
            finally:
                instance_lock.release()

    # The shorter alias remains explicit about the same offline enforcement and
    # makes the service convenient for maintenance CLI/API adapters.
    def restore_backup(self, backup_id: str) -> RestoreResult:
        return self.restore_backup_offline(backup_id)

    def _restore_selected_locked(self, selected: BackupRecord) -> RestoreResult:
        self._require_quiescent_database()
        rollback = self._create_backup_locked(
            label=f"Before restoring {selected.backup_id}",
            kind="pre_restore",
        )
        self._load_record(rollback.backup_id, verify_contents=True)
        try:
            self._install_database(selected)
        except BaseException as restore_exc:
            try:
                self._install_database(rollback)
            except BaseException as rollback_exc:
                raise BackupRestoreError(
                    "restore failed and the automatic rollback also failed"
                ) from rollback_exc
            raise BackupRestoreError(
                "restore failed; the original database was restored from the rollback backup"
            ) from restore_exc
        return RestoreResult(
            restored_backup=selected,
            rollback_backup=rollback,
            restored_at=_utc_now(),
        )

    def _pending_restore_path(self) -> Path:
        root = self._ensure_backup_root()
        marker = root / PENDING_RESTORE_FILENAME
        self._assert_direct_child(marker, root)
        return marker

    def _load_pending_restore(self) -> PendingRestore | None:
        marker = self._pending_restore_path()
        if not marker.exists():
            return None
        if marker.is_symlink() or not marker.is_file():
            raise BackupPathError("pending restore marker is not a regular file")
        try:
            if marker.stat().st_size > _MAX_MANIFEST_BYTES:
                raise BackupValidationError("pending restore marker is too large")
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BackupValidationError("pending restore marker is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("format_version") != BACKUP_FORMAT_VERSION:
            raise BackupValidationError("pending restore marker version is invalid")
        backup_id = payload.get("backup_id")
        self._validate_backup_id(backup_id)
        staged_at = payload.get("staged_at")
        digest = payload.get("database_sha256")
        if not isinstance(staged_at, str) or not _is_utc_timestamp(staged_at):
            raise BackupValidationError("pending restore timestamp is invalid")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise BackupValidationError("pending restore SHA-256 is invalid")
        return PendingRestore(
            backup_id=backup_id,
            staged_at=staged_at,
            database_sha256=digest,
        )

    def _create_backup_locked(self, *, label: str | None, kind: str) -> BackupRecord:
        root = self._ensure_backup_root()
        source_path = self._require_live_database()
        backup_id = _new_backup_id()
        final_dir = self._safe_backup_dir(backup_id, must_exist=False)
        if final_dir.exists():  # UUID makes this practically impossible.
            raise BackupError("generated backup identifier already exists")
        temporary_dir = root / f".tmp-{backup_id}-{uuid.uuid4().hex}"
        self._assert_direct_child(temporary_dir, root)
        temporary_dir.mkdir(mode=0o700)
        destination_path = temporary_dir / DATABASE_FILENAME
        try:
            source_uri = source_path.as_uri() + "?mode=ro"
            source = sqlite3.connect(
                source_uri,
                uri=True,
                timeout=self.timeout,
                isolation_level=None,
            )
            destination = sqlite3.connect(destination_path, timeout=self.timeout)
            try:
                source.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
                source.backup(destination, pages=256, sleep=0.01)
                destination.commit()
            except sqlite3.Error as exc:
                raise BackupError("SQLite backup failed") from exc
            finally:
                destination.close()
                source.close()

            size_bytes = destination_path.stat().st_size
            sha256 = _sha256_file(destination_path)
            created_at = _utc_now()
            manifest = {
                "format_version": BACKUP_FORMAT_VERSION,
                "backup_id": backup_id,
                "created_at": created_at,
                "kind": kind,
                "label": label,
                "database": {
                    "file": DATABASE_FILENAME,
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                },
            }
            _write_json_fsync(temporary_dir / MANIFEST_FILENAME, manifest)
            record = self._record_from_manifest(manifest, expected_id=backup_id)
            self._verify_database_file(destination_path, record)
            temporary_dir.replace(final_dir)
            _fsync_directory(root)
            self._save_verification(BackupVerification(record=record, verified_at=_utc_now()))
            return record
        except BaseException:
            shutil.rmtree(temporary_dir, ignore_errors=True)
            raise

    def _load_record(self, backup_id: str, *, verify_contents: bool) -> BackupRecord:
        backup_dir = self._safe_backup_dir(backup_id, must_exist=True)
        manifest_path = backup_dir / MANIFEST_FILENAME
        database_path = backup_dir / DATABASE_FILENAME
        self._assert_regular_managed_file(manifest_path, backup_dir, "backup manifest")
        self._assert_regular_managed_file(database_path, backup_dir, "backup database")
        try:
            if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
                raise BackupValidationError("backup manifest is too large")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BackupValidationError("backup manifest is not valid JSON") from exc
        record = self._record_from_manifest(manifest, expected_id=backup_id)
        try:
            actual_size = database_path.stat().st_size
        except OSError as exc:
            raise BackupValidationError("backup database cannot be read") from exc
        if actual_size != record.database_size_bytes:
            raise BackupValidationError("backup database size does not match its manifest")
        if verify_contents:
            self._verify_database_file(database_path, record)
        return record

    def _record_from_manifest(self, manifest: Any, *, expected_id: str) -> BackupRecord:
        if not isinstance(manifest, dict):
            raise BackupValidationError("backup manifest root must be an object")
        if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
            raise BackupValidationError("unsupported backup manifest version")
        backup_id = manifest.get("backup_id")
        if backup_id != expected_id or not isinstance(backup_id, str):
            raise BackupValidationError("backup manifest identifier mismatch")
        self._validate_backup_id(backup_id)
        created_at = manifest.get("created_at")
        if not isinstance(created_at, str) or not _is_utc_timestamp(created_at):
            raise BackupValidationError("backup manifest timestamp is invalid")
        kind = manifest.get("kind")
        if not isinstance(kind, str) or kind not in _BACKUP_KINDS:
            raise BackupValidationError("backup manifest kind is invalid")
        label = manifest.get("label")
        try:
            normalized_label = self._normalize_label(label)
        except (TypeError, ValueError) as exc:
            raise BackupValidationError("backup manifest label is invalid") from exc
        if normalized_label != label:
            raise BackupValidationError("backup manifest label is not normalized")
        database = manifest.get("database")
        if not isinstance(database, dict) or database.get("file") != DATABASE_FILENAME:
            raise BackupValidationError("backup manifest database file is invalid")
        size_bytes = database.get("size_bytes")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
            raise BackupValidationError("backup manifest database size is invalid")
        sha256 = database.get("sha256")
        if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
            raise BackupValidationError("backup manifest SHA-256 is invalid")
        return BackupRecord(
            backup_id=backup_id,
            created_at=created_at,
            kind=kind,  # type: ignore[arg-type]
            label=label,
            database_file=DATABASE_FILENAME,
            database_size_bytes=size_bytes,
            database_sha256=sha256,
        )

    def _verify_database_file(self, path: Path, record: BackupRecord) -> None:
        try:
            size_bytes = path.stat().st_size
        except OSError as exc:
            raise BackupValidationError("backup database cannot be read") from exc
        if size_bytes != record.database_size_bytes:
            raise BackupValidationError("backup database size does not match its manifest")
        if _sha256_file(path) != record.database_sha256:
            raise BackupValidationError("backup database SHA-256 does not match its manifest")
        uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=self.timeout)
            try:
                rows = connection.execute("PRAGMA integrity_check").fetchall()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise BackupValidationError("backup database is not a readable SQLite database") from exc
        if rows != [("ok",)]:
            raise BackupValidationError("backup database failed SQLite integrity_check")

    def _install_database(self, record: BackupRecord) -> None:
        source = self._safe_backup_dir(record.backup_id, must_exist=True) / DATABASE_FILENAME
        self._verify_database_file(source, record)
        staged = self.data_dir / f".{DATABASE_FILENAME}.restore-{uuid.uuid4().hex}.tmp"
        self._assert_direct_child(staged, self.data_dir)
        try:
            with source.open("rb") as source_stream, staged.open("xb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
                target_stream.flush()
                os.fsync(target_stream.fileno())
            self._verify_database_file(staged, record)
            self._remove_quiescent_sidecars()
            try:
                os.replace(staged, self.database_path)
            except (OSError, PermissionError) as exc:
                raise RestoreRequiresOfflineError(
                    "database file is still open; restore requires a fully stopped service"
                ) from exc
            _fsync_directory(self.data_dir)
            self._verify_database_file(self.database_path, record)
        finally:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass

    def _require_quiescent_database(self) -> None:
        path = self._require_live_database()
        try:
            connection = sqlite3.connect(
                path,
                timeout=self.timeout,
                isolation_level=None,
            )
            try:
                connection.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
                connection.execute("PRAGMA locking_mode = EXCLUSIVE")
                connection.execute("BEGIN EXCLUSIVE")
                connection.execute("COMMIT")
                journal_mode = str(
                    connection.execute("PRAGMA journal_mode").fetchone()[0]
                ).casefold()
                if journal_mode == "wal":
                    checkpoint = connection.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
                    if checkpoint is not None and int(checkpoint[0]) != 0:
                        raise RestoreRequiresOfflineError(
                            "database has an active WAL reader; restore requires a fully stopped service"
                        )
            finally:
                connection.close()
        except RestoreRequiresOfflineError:
            raise
        except sqlite3.Error as exc:
            raise RestoreRequiresOfflineError(
                "database is busy; restore requires a fully stopped service"
            ) from exc

    def _remove_quiescent_sidecars(self) -> None:
        # The rollback backup was already verified and WAL was checkpointed.
        # Removing stale sidecars prevents an old WAL from being replayed onto
        # the newly installed database.
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(self.database_path) + suffix)
            self._assert_direct_child(sidecar, self.data_dir)
            try:
                sidecar.unlink(missing_ok=True)
            except OSError as exc:
                raise RestoreRequiresOfflineError(
                    "database sidecar is still open; restore requires a fully stopped service"
                ) from exc

    def _require_live_database(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        expected = self.data_dir / DATABASE_FILENAME
        if expected.is_symlink():
            raise BackupPathError("live database must not be a symbolic link")
        try:
            resolved = expected.resolve(strict=True)
        except FileNotFoundError as exc:
            raise BackupNotFoundError("live SiteHub database does not exist") from exc
        if resolved.parent != self.data_dir.resolve() or not resolved.is_file():
            raise BackupPathError("live database is outside the managed data directory")
        return resolved

    def _ensure_backup_root(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.backups_dir.is_symlink():
            raise BackupPathError("backup directory must not be a symbolic link")
        self.backups_dir.mkdir(parents=False, exist_ok=True)
        resolved = self.backups_dir.resolve(strict=True)
        expected = self.data_dir.resolve() / "backups"
        if resolved != expected or not resolved.is_dir():
            raise BackupPathError("backup directory is outside the managed data directory")
        return resolved

    def _safe_backup_dir(self, backup_id: str, *, must_exist: bool) -> Path:
        self._validate_backup_id(backup_id)
        root = self._ensure_backup_root()
        candidate = root / backup_id
        self._assert_direct_child(candidate, root)
        if candidate.is_symlink():
            raise BackupPathError("backup directory must not be a symbolic link")
        if must_exist and not candidate.is_dir():
            raise BackupNotFoundError("backup does not exist")
        if candidate.exists() and candidate.resolve(strict=True).parent != root:
            raise BackupPathError("backup directory escaped the managed backup root")
        return candidate

    @staticmethod
    def _validate_backup_id(backup_id: str) -> None:
        if not isinstance(backup_id, str) or _BACKUP_ID_RE.fullmatch(backup_id) is None:
            raise BackupPathError("invalid backup identifier")

    @staticmethod
    def _assert_direct_child(candidate: Path, parent: Path) -> None:
        if candidate.parent.resolve() != parent.resolve():
            raise BackupPathError("managed path escaped its parent directory")

    @staticmethod
    def _assert_regular_managed_file(path: Path, parent: Path, label: str) -> None:
        if path.is_symlink():
            raise BackupPathError(f"{label} must not be a symbolic link")
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise BackupValidationError(f"{label} is missing") from exc
        if resolved.parent != parent.resolve() or not resolved.is_file():
            raise BackupPathError(f"{label} is outside the managed backup directory")

    @staticmethod
    def _normalize_label(label: Any) -> str | None:
        if label is None:
            return None
        if not isinstance(label, str):
            raise TypeError("backup label must be a string")
        normalized = label.strip()
        if not normalized:
            return None
        if len(normalized) > _MAX_LABEL_LENGTH:
            raise ValueError("backup label is too long")
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise ValueError("backup label contains control characters")
        return normalized


def _new_backup_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{uuid.uuid4().hex[:12]}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _is_utc_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return value.endswith("Z") and parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(parsed)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BackupValidationError("backup database cannot be hashed") from exc
    return digest.hexdigest()


def _write_json_fsync(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "BACKUP_FORMAT_VERSION",
    "PENDING_RESTORE_FILENAME",
    "BackupError",
    "BackupListItem",
    "BackupNotFoundError",
    "BackupPathError",
    "BackupRecord",
    "BackupRestoreError",
    "BackupService",
    "BackupValidationError",
    "BackupVerification",
    "PendingRestore",
    "RestoreRequiresOfflineError",
    "RestoreResult",
]
