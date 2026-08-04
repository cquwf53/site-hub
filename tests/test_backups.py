from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from site_hub.services.backups import (
    BackupNotFoundError,
    BackupPathError,
    BackupRestoreError,
    BackupService,
    BackupValidationError,
    RestoreRequiresOfflineError,
)
from site_hub.services.single_instance import SingleInstanceLock


def _create_database(path: Path, value: str = "initial") -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE state (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO state(id, value) VALUES(1, ?)", (value,))
        connection.commit()
    finally:
        connection.close()


def _read_value(path: Path) -> str:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        return str(connection.execute("SELECT value FROM state WHERE id = 1").fetchone()[0])
    finally:
        connection.close()


def _update_value(path: Path, value: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE state SET value = ? WHERE id = 1", (value,))
        connection.commit()
    finally:
        connection.close()


def _backup_database_path(data_dir: Path, backup_id: str) -> Path:
    return data_dir / "backups" / backup_id / "site-hub.db"


def test_backup_is_consistent_and_manifest_is_verifiable_during_wal_write(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database_path = data_dir / "site-hub.db"
    _create_database(database_path, "committed")
    writer = sqlite3.connect(database_path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE state SET value = 'uncommitted' WHERE id = 1")
    try:
        service = BackupService(data_dir)
        record = service.create_backup(label="  Before maintenance  ")
    finally:
        writer.rollback()
        writer.close()

    backup_path = _backup_database_path(data_dir, record.backup_id)
    assert _read_value(backup_path) == "committed"
    assert record.label == "Before maintenance"
    assert record.database_size_bytes == backup_path.stat().st_size
    assert record.database_sha256 == hashlib.sha256(backup_path.read_bytes()).hexdigest()

    manifest = json.loads(
        (backup_path.parent / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["backup_id"] == record.backup_id
    assert manifest["database"] == {
        "file": "site-hub.db",
        "sha256": record.database_sha256,
        "size_bytes": record.database_size_bytes,
    }
    verification = service.verify_backup(record.backup_id)
    assert verification.record == record
    assert verification.integrity_check == "ok"
    listed = service.list_backups()
    assert [(item.backup_id, item.status) for item in listed] == [
        (record.backup_id, "available")
    ]


def test_verify_detects_same_size_database_tampering(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _create_database(data_dir / "site-hub.db")
    service = BackupService(data_dir)
    record = service.create_backup()
    backup_path = _backup_database_path(data_dir, record.backup_id)
    payload = bytearray(backup_path.read_bytes())
    payload[-1] ^= 0x01
    backup_path.write_bytes(payload)

    # Fast listing checks manifest shape and size; explicit verification performs
    # the intentionally more expensive digest and integrity checks.
    assert service.list_backups()[0].status == "available"
    with pytest.raises(BackupValidationError, match="SHA-256"):
        service.verify_backup(record.backup_id)


def test_manifest_cannot_redirect_database_outside_backup_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _create_database(data_dir / "site-hub.db")
    service = BackupService(data_dir)
    record = service.create_backup()
    manifest_path = data_dir / "backups" / record.backup_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database"]["file"] = "../../outside.db"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupValidationError, match="database file"):
        service.verify_backup(record.backup_id)
    item = service.list_backups()[0]
    assert item.status == "invalid"
    assert item.record is None


@pytest.mark.parametrize(
    "malicious_id",
    ["../escape", "..\\escape", ".", "backup/child", "C:\\outside"],
)
def test_backup_identifiers_reject_path_traversal(
    tmp_path: Path, malicious_id: str
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _create_database(data_dir / "site-hub.db")
    service = BackupService(data_dir)

    with pytest.raises(BackupPathError, match="identifier"):
        service.verify_backup(malicious_id)
    with pytest.raises(BackupPathError, match="identifier"):
        service.restore_backup_offline(malicious_id)


def test_restore_refuses_hot_database_owned_by_sitehub(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database_path = data_dir / "site-hub.db"
    _create_database(database_path, "backup-value")
    service = BackupService(data_dir, timeout=0.2)
    backup = service.create_backup()
    _update_value(database_path, "live-value")

    owner = SingleInstanceLock(data_dir)
    owner.acquire()
    try:
        with pytest.raises(RestoreRequiresOfflineError, match="fully stopped"):
            service.restore_backup_offline(backup.backup_id)
    finally:
        owner.release()

    assert _read_value(database_path) == "live-value"
    assert len(service.list_backups()) == 1  # no rollback backup was created


def test_offline_restore_creates_verified_rollback_before_replacement(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database_path = data_dir / "site-hub.db"
    _create_database(database_path, "old-value")
    service = BackupService(data_dir)
    selected = service.create_backup(label="Known good")
    _update_value(database_path, "current-value")

    result = service.restore_backup_offline(selected.backup_id)

    assert result.restored_backup.backup_id == selected.backup_id
    assert result.rollback_backup.kind == "pre_restore"
    assert _read_value(database_path) == "old-value"
    rollback_path = _backup_database_path(data_dir, result.rollback_backup.backup_id)
    assert _read_value(rollback_path) == "current-value"
    assert service.verify_backup(result.rollback_backup.backup_id).integrity_check == "ok"
    assert len(service.list_backups()) == 2


def test_invalid_restore_is_rejected_before_rollback_or_live_database_change(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database_path = data_dir / "site-hub.db"
    _create_database(database_path, "backup-value")
    service = BackupService(data_dir)
    selected = service.create_backup()
    _update_value(database_path, "current-value")
    backup_path = _backup_database_path(data_dir, selected.backup_id)
    payload = bytearray(backup_path.read_bytes())
    payload[-1] ^= 0x01
    backup_path.write_bytes(payload)

    with pytest.raises(BackupValidationError, match="SHA-256"):
        service.restore_backup_offline(selected.backup_id)

    assert _read_value(database_path) == "current-value"
    assert len(service.list_backups()) == 1


def test_failed_install_automatically_restores_original_from_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database_path = data_dir / "site-hub.db"
    _create_database(database_path, "backup-value")
    service = BackupService(data_dir)
    selected = service.create_backup()
    _update_value(database_path, "current-value")
    real_install = service._install_database

    def fail_after_selected_install(record):
        real_install(record)
        if record.backup_id == selected.backup_id:
            raise OSError("simulated post-install failure")

    monkeypatch.setattr(service, "_install_database", fail_after_selected_install)
    with pytest.raises(BackupRestoreError, match="original database was restored"):
        service.restore_backup_offline(selected.backup_id)

    assert _read_value(database_path) == "current-value"
    rollback_items = [
        item.record
        for item in service.list_backups()
        if item.record is not None and item.record.kind == "pre_restore"
    ]
    assert len(rollback_items) == 1
    assert service.verify_backup(rollback_items[0].backup_id).integrity_check == "ok"


def test_labels_and_missing_live_database_are_rejected_without_partial_files(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    service = BackupService(data_dir)
    with pytest.raises(BackupNotFoundError, match="database does not exist"):
        service.create_backup()
    assert service.list_backups() == []

    _create_database(data_dir / "site-hub.db")
    with pytest.raises(ValueError, match="control characters"):
        service.create_backup(label="bad\nlabel")
    assert service.list_backups() == []


def test_staged_restore_is_applied_only_with_startup_instance_lock(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database_path = data_dir / "site-hub.db"
    _create_database(database_path, "known-good")
    service = BackupService(data_dir)
    selected = service.create_backup(label="Known good")
    _update_value(database_path, "current")

    pending = service.stage_restore(selected.backup_id)
    assert pending.backup_id == selected.backup_id
    assert service.pending_restore() == pending

    owner = SingleInstanceLock(data_dir)
    owner.acquire()
    try:
        result = service.apply_pending_restore_with_instance_lock()
    finally:
        owner.release()

    assert result is not None
    assert result.restored_backup.backup_id == selected.backup_id
    assert result.rollback_backup.kind == "pre_restore"
    assert _read_value(database_path) == "known-good"
    assert service.pending_restore() == pending
    assert service.confirm_pending_restore(selected.backup_id) is True
    assert service.pending_restore() is None


def test_applied_restore_can_roll_back_when_startup_validation_fails(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database_path = data_dir / "site-hub.db"
    _create_database(database_path, "known-good")
    service = BackupService(data_dir)
    selected = service.create_backup(label="Known good")
    _update_value(database_path, "current")
    service.stage_restore(selected.backup_id)

    owner = SingleInstanceLock(data_dir)
    owner.acquire()
    try:
        result = service.apply_pending_restore_with_instance_lock()
        assert result is not None
        assert _read_value(database_path) == "known-good"
        service.rollback_applied_restore_with_instance_lock(result)
    finally:
        owner.release()

    assert _read_value(database_path) == "current"
    assert service.pending_restore() is None


def test_pending_restore_can_be_replaced_cancelled_and_detects_tampering(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database_path = data_dir / "site-hub.db"
    _create_database(database_path, "first")
    service = BackupService(data_dir)
    first = service.create_backup(label="First")
    _update_value(database_path, "second")
    second = service.create_backup(label="Second")

    service.stage_restore(first.backup_id)
    assert service.stage_restore(second.backup_id).backup_id == second.backup_id
    assert service.cancel_pending_restore() is True
    assert service.cancel_pending_restore() is False

    service.stage_restore(first.backup_id)
    backup_path = _backup_database_path(data_dir, first.backup_id)
    payload = bytearray(backup_path.read_bytes())
    payload[-1] ^= 0x01
    backup_path.write_bytes(payload)
    owner = SingleInstanceLock(data_dir)
    owner.acquire()
    try:
        with pytest.raises(BackupValidationError, match="SHA-256"):
            service.apply_pending_restore_with_instance_lock()
    finally:
        owner.release()
    assert service.pending_restore() is not None
    assert _read_value(database_path) == "second"
