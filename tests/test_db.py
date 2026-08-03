from __future__ import annotations

import sqlite3

import pytest

from site_hub.db import Database, SiteHubRepository


def _control_plane(database: Database) -> tuple[SiteHubRepository, str, str]:
    repository = SiteHubRepository(database)
    folder = repository.create_folder("Imported")
    site = repository.create_site(
        folder_id=folder["id"], name="Example", origin="https://example.test",
        canonical_host="example.test",
    )
    bookmark = repository.create_capability(
        site_id=site["id"], code="bookmark", adapter="url"
    )
    account = repository.create_site_account(site_id=site["id"], label="Default")
    checkin = repository.create_capability(
        site_id=site["id"], code="checkin", account_id=account["id"],
        adapter="test",
    )
    return repository, bookmark["id"], checkin["id"]


def test_schema_wal_foreign_keys_and_rollback(tmp_path):
    database = Database(tmp_path / "site-hub.db")
    database.initialize()
    database.initialize()  # checksum-verified idempotence

    with database.connect() as connection:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "folders", "sites", "site_endpoints", "tags", "site_accounts",
            "browser_profiles", "capability_instances", "secrets", "products",
            "price_observations", "schedules", "job_batches", "jobs", "runs",
            "artifacts", "events", "notification_outbox", "legacy_map",
            "migration_runs",
        } <= tables
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    with pytest.raises(RuntimeError):
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO settings(key,value_json,updated_at) VALUES('rollback','{}','now')"
            )
            raise RuntimeError("force rollback")
    with database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM settings WHERE key='rollback'"
        ).fetchone() is None


def test_repository_job_state_machine_and_capability_filter(tmp_path):
    database = Database(tmp_path / "queue.db")
    database.initialize()
    repository, bookmark_id, checkin_id = _control_plane(database)
    repository.create_job(
        capability_instance_id=bookmark_id, idempotency_key="bookmark:1"
    )
    checkin_job = repository.create_job(
        capability_instance_id=checkin_id, idempotency_key="checkin:1"
    )

    claimed = repository.claim_next_job("python-worker", capability_codes=["checkin"])
    assert claimed is not None
    assert claimed["id"] == checkin_job["id"]
    assert claimed["status"] == "leased"
    assert claimed["lease_token"]
    token = claimed["lease_token"]
    assert repository.start_job(claimed["id"], "python-worker", lease_token=token)
    assert repository.update_job_progress(
        claimed["id"], "python-worker", {"step": 1}, lease_token=token
    )
    assert repository.heartbeat_job(
        claimed["id"], "python-worker", lease_token=token
    )
    assert repository.finish_job(
        claimed["id"], "python-worker", status="succeeded",
        result={"ok": True}, lease_token=token,
    )
    finished = repository.get_job(claimed["id"])
    assert finished and finished["status"] == "succeeded"
    assert finished["lease_token"] is None

    remaining = repository.claim_next_job("node-worker", capability_codes=["bookmark"])
    assert remaining and remaining["capability_instance_id"] == bookmark_id
    assert not repository.finish_job(
        remaining["id"], "node-worker", status="succeeded", lease_token="stale"
    )


def test_readonly_database_refuses_writes(tmp_path):
    path = tmp_path / "readonly.db"
    database = Database(path)
    database.initialize()
    readonly = Database(path, readonly=True)
    with pytest.raises(RuntimeError):
        with readonly.transaction():
            pass
    with readonly.connect() as connection, pytest.raises(sqlite3.OperationalError):
        connection.execute("INSERT INTO settings VALUES('x','{}','now')")
