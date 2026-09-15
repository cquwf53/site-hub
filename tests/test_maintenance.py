from __future__ import annotations

import hashlib
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from site_hub.api.maintenance_routes import router
from site_hub.db import Database
from site_hub.services.backups import BackupService
from site_hub.services.maintenance import MaintenanceBusyError, MaintenanceService, _stamp


@pytest.fixture
def maintenance(tmp_path):
    data = tmp_path / "data"
    database = Database(data / "site-hub.db")
    database.initialize()
    service = MaintenanceService(database, BackupService(data), data)
    service.initialize()
    return service


def _configure(service, **changes):
    return service.update_settings({**service.get_settings(), **changes})


def _artifact(service, name, *, run_status="succeeded", job_status=None,
              relative=None, retention_until=None, tampered=False, recent_file=False):
    old = datetime.now(UTC) - timedelta(days=90)
    path = service.data_dir / "artifacts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"verified screenshot")
    if not recent_file:
        os.utime(path, (old.timestamp(), old.timestamp()))
    with service.database.transaction() as connection:
        job_id = None
        if job_status is not None:
            job_id = "job-" + name
            connection.execute(
                """INSERT INTO jobs(id,kind,available_at,status,idempotency_key,created_at,updated_at)
                   VALUES(?,'checkin',?,?,?,?,?)""",
                (job_id, _stamp(old), job_status, job_id, _stamp(old), _stamp(old)),
            )
        connection.execute(
            """INSERT INTO runs(id,job_id,status,started_at,finished_at,created_at)
               VALUES(?,?,?,?,?,?)""",
            (name, job_id, run_status, _stamp(old),
             None if run_status == "running" else _stamp(old), _stamp(old)),
        )
        connection.execute(
            """INSERT INTO artifacts(id,run_id,relative_path,sha256,size,retention_until,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (name, name, relative or name,
             "0" * 64 if tampered else hashlib.sha256(path.read_bytes()).hexdigest(),
             path.stat().st_size, retention_until, _stamp(old)),
        )
    return path


def test_defaults_persist_due_time_and_disable_cleanup(maintenance):
    service = maintenance
    before = service.status()
    due = datetime.fromisoformat(before["next_run_at"].replace("Z", "+00:00"))
    assert timedelta(hours=23) < due - datetime.now(UTC) <= timedelta(hours=24)
    reopened = MaintenanceService(service.database, service.backups, service.data_dir)
    reopened.initialize()
    assert reopened.status()["next_run_at"] == before["next_run_at"]
    old = _artifact(service, "kept.png")
    result = service.run_once()
    assert old.is_file()
    assert result["latest_backup"]["kind"] == "manual"
    assert result["latest_verification"]["integrity_check"] == "ok"
    assert result["last_cleanup"] == dict.fromkeys(("backups", "logs", "screenshots", "price_history"), 0)
    _configure(service, automatic_backup_enabled=False)
    assert service.status()["next_run_at"] is None
    with pytest.raises(ValidationError):
        _configure(service, backup_keep_count=0)


def test_due_schedule_runs_once_and_survives_restart(maintenance, monkeypatch):
    import site_hub.services.maintenance as module
    assert maintenance.run_due() is None
    future = datetime.now(UTC) + timedelta(hours=25)
    monkeypatch.setattr(module, "_now", lambda: future)
    result = maintenance.run_due()
    assert result["latest_backup"]["kind"] == "automatic"
    assert result["last_success_at"] == _stamp(future)
    assert maintenance.run_due() is None
    restarted = MaintenanceService(maintenance.database, maintenance.backups, maintenance.data_dir)
    restarted.initialize()
    assert restarted.run_due() is None
    assert restarted.status()["latest_verification"]["backup_id"] == result["latest_backup"]["backup_id"]


def test_backup_failure_prevents_every_cleanup_and_releases_lock(maintenance, monkeypatch):
    stale = _artifact(maintenance, "keep-on-failure.png")
    _configure(maintenance, retention_enabled=True)
    original = maintenance.backups.create_backup
    def fail(**kwargs):
        raise OSError("private source path must not appear in status")
    monkeypatch.setattr(maintenance.backups, "create_backup", fail)
    with pytest.raises(RuntimeError, match="MAINTENANCE_FAILED"):
        maintenance.run_once()
    assert stale.exists()
    status = maintenance.status()
    assert status["running"] is False
    assert status["last_error"] == "MAINTENANCE_FAILED"
    monkeypatch.setattr(maintenance.backups, "create_backup", original)
    assert maintenance.run_once()["last_error"] is None


def test_retention_preserves_manual_rollback_latest_and_staged_backups(maintenance):
    backup = maintenance.backups
    manual = backup.create_backup(kind="manual")
    rollback = backup.create_backup(kind="pre_restore")
    staged = backup.create_backup(kind="automatic")
    expired = backup.create_backup(kind="automatic")
    newest = backup.create_backup(kind="automatic")
    backup.stage_restore(staged.backup_id)
    assert backup.prune_automatic_backups(1) == 1
    identifiers = {item.backup_id for item in backup.list_backups()}
    assert {manual.backup_id, rollback.backup_id, staged.backup_id, newest.backup_id} <= identifiers
    assert expired.backup_id not in identifiers


def test_pending_restore_postpones_all_maintenance_cleanup(maintenance):
    stale = _artifact(maintenance, "keep-for-restore.png")
    staged = maintenance.backups.create_backup()
    maintenance.backups.stage_restore(staged.backup_id)
    _configure(maintenance, retention_enabled=True)
    result = maintenance.run_once()
    assert stale.exists()
    assert result["last_cleanup"]["screenshots"] == 0
    assert maintenance.backups.pending_restore().backup_id == staged.backup_id


def test_screenshot_retention_only_removes_verified_old_completed_files(maintenance):
    removed = _artifact(maintenance, "expired.png")
    protected = [
        _artifact(maintenance, "running.png", run_status="running"),
        _artifact(maintenance, "queued-job.png", job_status="queued"),
        _artifact(maintenance, "retained.png", retention_until=_stamp(datetime.now(UTC)+timedelta(days=1))),
        _artifact(maintenance, "tampered.png", tampered=True),
        _artifact(maintenance, "recent-file.png", recent_file=True),
        _artifact(maintenance, "traversal.png", relative="../outside.png"),
    ]
    outside = maintenance.data_dir / "outside.png"
    outside.write_bytes(b"never delete outside artifacts")
    _configure(maintenance, retention_enabled=True)
    result = maintenance.run_once()
    assert result["last_cleanup"]["screenshots"] == 1
    assert not removed.exists()
    assert all(path.exists() for path in protected)
    assert outside.read_bytes() == b"never delete outside artifacts"
    with maintenance.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == len(protected)


def test_reparse_artifact_and_log_paths_are_not_followed(maintenance, tmp_path):
    from site_hub.services.maintenance import _managed_file
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.log.1").write_text("keep", encoding="utf-8")
    root = maintenance.data_dir / "logs"
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available for this Windows user")
    assert _managed_file(root, "victim.log.1") is None
    assert maintenance._prune_logs(datetime.now(UTC), 1) == 0
    assert (outside / "victim.log.1").exists()


def test_log_cleanup_keeps_active_names_and_open_archives(maintenance):
    import logging
    logs = maintenance.data_dir / "logs"
    logs.mkdir()
    old = (datetime.now(UTC) - timedelta(days=90)).timestamp()
    names = ("startup.log", "server.log.1", "server.log.2", "notes.txt", "server-2025-01-01.log")
    for name in names:
        path = logs / name
        path.write_text("log", encoding="utf-8")
        os.utime(path, (old, old))
    handler = logging.FileHandler(logs / "server.log.2")
    try:
        count = maintenance._prune_logs(datetime.now(UTC), 30)
        assert count == 2
        assert (logs / "startup.log").exists()
        assert (logs / "server.log.2").exists()
        assert (logs / "notes.txt").exists()
        assert not (logs / "server.log.1").exists()
    finally:
        handler.close()


@pytest.mark.skipif(os.name != "nt", reason="production Windows exclusive-open check")
def test_log_cleanup_refuses_archive_opened_by_another_writer(maintenance):
    logs = maintenance.data_dir / "logs"
    logs.mkdir()
    path = logs / "external.log.1"
    path.write_text("live", encoding="utf-8")
    old = (datetime.now(UTC) - timedelta(days=90)).timestamp()
    os.utime(path, (old, old))
    with path.open("ab"):
        assert maintenance._prune_logs(datetime.now(UTC), 30) == 0
        assert path.exists()
    assert maintenance._prune_logs(datetime.now(UTC), 30) == 1


def test_price_retention_keeps_latest_per_product_and_active_runs(maintenance):
    old = _stamp(datetime.now(UTC) - timedelta(days=500))
    newer = _stamp(datetime.now(UTC) - timedelta(days=400))
    with maintenance.database.transaction() as connection:
        folder = "test-folder"
        connection.execute(
            "INSERT INTO folders(id,name,created_at,updated_at) VALUES(?,'Root',?,?)", (folder, old, old),
        )
        site_type = connection.execute("SELECT id,slug FROM site_types WHERE is_default=1").fetchone()
        connection.execute(
            """INSERT INTO sites(id,folder_id,name,origin,canonical_host,kind,site_type_id,created_at,updated_at)
               VALUES('site',?,'Shop','https://example.test','example.test',?,?,?,?)""",
            (folder, site_type["slug"], site_type["id"], old, old),
        )
        for product in ("a", "b"):
            connection.execute(
                "INSERT INTO products(id,site_id,name,created_at,updated_at) VALUES(?,'site',?,?,?)",
                (product, product, old, old),
            )
        connection.execute(
            "INSERT INTO runs(id,status,started_at,created_at) VALUES('active','running',?,?)", (old, old),
        )
        for identifier, product, time, run in (
            ("a-old", "a", old, None), ("a-latest", "a", newer, None),
            ("b-only", "b", old, None), ("active-observation", "a", old, "active"),
        ):
            connection.execute(
                """INSERT INTO price_observations(id,product_id,price,availability_status,observed_at,source_run_id)
                   VALUES(?,?,1,'available',?,?)""", (identifier, product, time, run),
            )
    assert maintenance._prune_prices(datetime.now(UTC), 365) == 1
    with maintenance.database.connect() as connection:
        assert {row[0] for row in connection.execute("SELECT id FROM price_observations")} == {
            "a-latest", "b-only", "active-observation",
        }


def test_concurrent_run_and_configuration_are_rejected(maintenance, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    original = maintenance.backups.create_backup
    def pause(**kwargs):
        entered.set()
        assert release.wait(10)
        return original(**kwargs)
    monkeypatch.setattr(maintenance.backups, "create_backup", pause)
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(maintenance.run_once()))
    worker.start()
    try:
        assert entered.wait(10)
        with pytest.raises(MaintenanceBusyError):
            maintenance.run_once()
        with pytest.raises(MaintenanceBusyError):
            _configure(maintenance, retention_enabled=True)
    finally:
        release.set()
        worker.join(10)
    assert len(outcomes) == 1


def test_maintenance_api_contract_and_validation(maintenance):
    app = FastAPI()
    app.state.maintenance = maintenance
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as client:
        settings = client.get("/api/v1/settings/maintenance").json()
        assert settings["retention_enabled"] is False
        invalid = client.put("/api/v1/settings/maintenance", json={**settings, "backup_interval_hours": 0})
        assert invalid.status_code == 422
        saved = client.put("/api/v1/settings/maintenance", json={**settings, "backup_interval_hours": 12})
        assert saved.status_code == 200
        result = client.post("/api/v1/maintenance/run")
        assert result.status_code == 200
        assert result.json()["latest_backup"]["kind"] == "manual"
        assert client.get("/api/v1/maintenance/status").json()["last_success_at"]


@pytest.mark.asyncio
async def test_stop_waits_for_manual_backup_thread(maintenance, monkeypatch):
    import asyncio
    entered = threading.Event()
    release = threading.Event()
    original = maintenance.backups.create_backup
    def pause(**kwargs):
        entered.set()
        assert release.wait(10)
        return original(**kwargs)
    monkeypatch.setattr(maintenance.backups, "create_backup", pause)
    running = asyncio.create_task(asyncio.to_thread(maintenance.run_once))
    assert await asyncio.to_thread(entered.wait, 10)
    stopping = asyncio.create_task(maintenance.stop())
    try:
        await asyncio.sleep(0.05)
        assert not stopping.done()
        with pytest.raises(MaintenanceBusyError):
            maintenance.run_once()
    finally:
        release.set()
        await running
        await stopping


def test_integrated_maintenance_routes_require_csrf(tmp_path):
    from site_hub.config import Settings
    from site_hub.main import create_app
    from site_hub.services.data_store import DataLayerStore
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<!doctype html><title>SiteHub</title>", encoding="utf-8")
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", frontend_dir=frontend)
    app = create_app(settings=settings, store=DataLayerStore(settings.db_path), start_background=False)
    origin = "http://127.0.0.1:8686"
    with TestClient(app, base_url=origin, headers={"Host": "127.0.0.1:8686"}) as client:
        configured = client.get("/api/v1/settings/maintenance")
        assert configured.status_code == 200
        assert client.post("/api/v1/maintenance/run").status_code == 403
        token = client.get("/api/v1/bootstrap").json()["csrf_token"]
        result = client.post("/api/v1/maintenance/run", headers={"Origin": origin, "X-CSRF-Token": token})
        assert result.status_code == 200
        assert result.json()["latest_verification"]["integrity_check"] == "ok"


@pytest.mark.parametrize("payload", ({"backup_id": "../escape", "verified_at": "2026-09-15T00:00:00Z", "integrity_check": "ok"}, {"verified_at": 123}))
def test_corrupt_verification_metadata_does_not_hide_backup_status(maintenance, payload):
    import json
    backup = maintenance.backups.create_backup()
    marker = maintenance.data_dir / "backups" / "verification-latest.json"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    status = maintenance.status()
    assert status["latest_backup"]["backup_id"] == backup.backup_id
    assert status["latest_verification"] is None
