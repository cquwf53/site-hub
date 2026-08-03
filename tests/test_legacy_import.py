from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import tools.migrate_legacy as migrate_cli
import site_hub.services.legacy_import as legacy_import_module

from site_hub.db import Database
from site_hub.services.legacy_import import (
    ArtifactCopier,
    FROZEN_SNAPSHOT_COUNTS,
    LegacyImporter,
    ProfileCopier,
    profile_inventory_summary,
    _batch_status,
    _job_status,
    _normalize_autocheckin_adapter,
    backup_sqlite_database,
    stable_id,
)
from site_hub.services.secrets import (
    SecretService,
    EphemeralSecretProtector,
    restore_sensitive_url,
)
from site_hub.services.data_store import DataLayerStore


def test_legacy_nonterminal_jobs_are_archived_not_requeued() -> None:
    for status in ("pending", "planned", "queued", "waiting"):
        assert _job_status(status) == "cancelled"
    assert _job_status("running") == "effect_unknown"
    for status in ("pending", "planned", "queued", "waiting", "running", "started", "processing"):
        assert _batch_status(status) == "cancelled"
    assert _batch_status("completed") == "completed"


@pytest.mark.parametrize(
    "adapter",
    (
        "xkool", "nodeloc", "nodeseek", "hundredxlabs", "api123nhh",
        "jianzhile", "btschool", "52pojie",
    ),
)
def test_autocheckin_dedicated_adapters_are_preserved(adapter: str) -> None:
    assert _normalize_autocheckin_adapter(adapter, "playwright") == adapter


def test_autocheckin_100xlabs_adapter_uses_worker_manifest_id() -> None:
    assert _normalize_autocheckin_adapter(
        " 100xLabs ", "playwright"
    ) == "hundredxlabs"


@pytest.mark.parametrize(
    ("config_adapter", "legacy_mode", "expected"),
    (
        (None, "playwright", "playwright-generic"),
        (None, "browser", "playwright-generic"),
        (None, "api", "api"),
        (None, "cookie", "cookie"),
        ("PLAYWRIGHT", "cookie", "playwright-generic"),
        ("future-adapter", "cookie", "cookie"),
        ("future-adapter", "future-mode", "playwright-generic"),
    ),
)
def test_autocheckin_adapter_falls_back_to_a_supported_legacy_mode(
    config_adapter: str | None, legacy_mode: str, expected: str,
) -> None:
    assert _normalize_autocheckin_adapter(config_adapter, legacy_mode) == expected


def _make_vshop(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sites(
          id INTEGER PRIMARY KEY,name TEXT,url TEXT,scraper_type TEXT,scraper_config TEXT,
          status TEXT,created_at TEXT,sort_order INTEGER,note TEXT,kind TEXT,
          capabilities TEXT,icon TEXT,description TEXT,pinned INTEGER,visit_count INTEGER
        );
        CREATE TABLE tags(id INTEGER PRIMARY KEY,name TEXT,color TEXT,icon TEXT,sort_order INTEGER,created_at TEXT);
        CREATE TABLE site_tags(site_id INTEGER,tag_id INTEGER);
        CREATE TABLE categories(id INTEGER PRIMARY KEY,site_id INTEGER,name TEXT,sort_order INTEGER);
        CREATE TABLE products(
          id INTEGER PRIMARY KEY,site_id INTEGER,category_id INTEGER,name TEXT,price REAL,
          original_price TEXT,stock INTEGER,stock_text TEXT,sold INTEGER,description TEXT,
          buy_url TEXT,tags TEXT,is_available INTEGER,last_updated TEXT
        );
        CREATE TABLE product_tags(product_id INTEGER,tag_id INTEGER);
        CREATE TABLE price_history(id INTEGER PRIMARY KEY,product_id INTEGER,price REAL,stock INTEGER,is_available INTEGER,recorded_at TEXT);
        CREATE TABLE sync_logs(id INTEGER PRIMARY KEY,site_id INTEGER,status TEXT,products_found INTEGER,duration_seconds REAL,error_message TEXT,created_at TEXT);
        CREATE TABLE events(id INTEGER PRIMARY KEY,type TEXT,severity TEXT,site_id INTEGER,product_id INTEGER,title TEXT,detail TEXT,payload TEXT,is_read INTEGER,created_at TEXT);
        INSERT INTO sites VALUES(
          1,'Shop','https://shop.test/path?token=fake-url-secret#view','demo',
          '{"headers":{"Authorization":"fake-auth-secret"},"page_size":20}',
          'active','2026-01-01T00:00:00Z',0,'','shop','["products"]','','',0,0
        );
        INSERT INTO tags VALUES(1,'Sale','#ff0000','tag',0,'2026-01-01T00:00:00Z');
        INSERT INTO site_tags VALUES(1,1);
        INSERT INTO categories VALUES(1,1,'Main',0);
        INSERT INTO products VALUES(
          1,1,1,'Widget',9.5,'12.0',2,'2 left',0,'',
          'https://shop.test/buy?coupon=fake-coupon-secret','["Sale"]',1,
          '2026-01-01T01:00:00Z'
        );
        INSERT INTO product_tags VALUES(1,1);
        INSERT INTO price_history VALUES(1,1,9.5,2,1,'2026-01-01T01:00:00Z');
        INSERT INTO sync_logs VALUES(1,1,'success',1,0.5,'','2026-01-01T01:00:00Z');
        INSERT INTO events VALUES(1,'price_changed','info',1,1,'Changed','',
          '{"access_token":"fake-event-secret"}',0,'2026-01-01T01:00:00Z');
        """
    )
    connection.commit()
    connection.close()


def _make_auto(path: Path, *, orphan_job: bool = False) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sites(
          id INTEGER PRIMARY KEY,name TEXT,url TEXT,mode TEXT,config TEXT,cron TEXT,
          enabled INTEGER,created_at TEXT,updated_at TEXT,sort_order INTEGER
        );
        CREATE TABLE logs(
          id INTEGER PRIMARY KEY,site_id INTEGER,status TEXT,message TEXT,screenshot TEXT,
          duration_ms INTEGER,created_at TEXT
        );
        CREATE TABLE scheduler_batches(
          id INTEGER PRIMARY KEY,schedule_date TEXT,planned_at TEXT,recovery_at TEXT,
          trigger_reason TEXT,status TEXT,started_at TEXT,completed_at TEXT,
          created_at TEXT,updated_at TEXT
        );
        CREATE TABLE scheduler_batch_items(
          id INTEGER PRIMARY KEY,batch_id INTEGER,site_id INTEGER,site_name TEXT,
          position INTEGER,status TEXT,result_status TEXT,message TEXT,
          started_at TEXT,finished_at TEXT
        );
        CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT);
        INSERT INTO sites VALUES(
          1,'Checkin','https://checkin.test/daily?account=fake-account-secret','cookie',
          '{"cookie":"fake-cookie-secret","wait_ms":100}',
          '0 8 * * *',1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',0
        );
        INSERT INTO logs VALUES(1,1,'success','signed in','one.png',100,'2026-01-01T08:00:00Z');
        INSERT INTO scheduler_batches VALUES(
          1,'2026-01-01','2026-01-01T08:00:00Z',NULL,'normal','completed',
          '2026-01-01T08:00:00Z','2026-01-01T08:01:00Z',
          '2026-01-01T07:00:00Z','2026-01-01T08:01:00Z'
        );
        INSERT INTO config VALUES('scheduler.jitter','15');
        """
    )
    site_id = 999 if orphan_job else 1
    connection.execute(
        """INSERT INTO scheduler_batch_items VALUES(
          1,1,?,'Checkin',0,'completed','success','ok',
          '2026-01-01T08:00:00Z','2026-01-01T08:01:00Z')""",
        (site_id,),
    )
    connection.commit()
    connection.close()


def _importer(target: Path) -> tuple[Database, LegacyImporter, SecretService]:
    database = Database(target)
    protector = EphemeralSecretProtector(b"legacy-import-test-key-32-bytes")
    service = SecretService(database, protector)
    return database, LegacyImporter(database, service), service


def test_autocheckin_adapter_is_removed_before_sensitive_config_is_stored(tmp_path):
    vshop, auto, target = tmp_path / "vshop.db", tmp_path / "auto.db", tmp_path / "target.db"
    _make_vshop(vshop)
    _make_auto(auto)
    sensitive_value = "fake-dedicated-auth-secret"
    with sqlite3.connect(auto) as connection:
        connection.execute(
            """INSERT INTO sites(
                   id,name,url,mode,config,cron,enabled,created_at,updated_at,sort_order
               ) VALUES(2,?,?,?,?,?,1,?,?,1)""",
            (
                "Dedicated", "https://sub.100xlabs.space/dashboard", "playwright",
                json.dumps({
                    "adapter": "100xlabs",
                    "headless": False,
                    "headers": {"Authorization": sensitive_value},
                }),
                "0 8 * * *", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
            ),
        )

    database, importer, secrets = _importer(target)
    database.initialize()
    importer.execute(vshop, auto, expected_counts=None)
    capability_id = stable_id("autocheckin", "capability-checkin", 2)
    account_id = stable_id("autocheckin", "account", 2)
    with database.connect() as connection:
        capability = connection.execute(
            "SELECT adapter,config_json FROM capability_instances WHERE id=?",
            (capability_id,),
        ).fetchone()
        assert capability is not None
        assert capability["adapter"] == "hundredxlabs"
        stored_config = json.loads(capability["config_json"])
        assert "adapter" not in stored_config
        assert stored_config["headless"] is False
        assert stored_config["headers"] == {}
        assert stored_config["_secret_fields"] == ["headers.Authorization"]

    runtime_store = DataLayerStore(target)
    runtime_store.secret_service = secrets
    account = asyncio.run(runtime_store.get_account(UUID(account_id)))
    assert account is not None
    assert account["adapter"] == "hundredxlabs"
    assert account["runtime_config"]["headers"]["Authorization"] == sensitive_value
    assert sensitive_value.encode() not in target.read_bytes()


def test_disabled_empty_legacy_webhook_does_not_create_a_channel(tmp_path) -> None:
    vshop, auto, target = tmp_path / "vshop.db", tmp_path / "auto.db", tmp_path / "target.db"
    _make_vshop(vshop)
    _make_auto(auto)
    with sqlite3.connect(auto) as connection:
        connection.executemany(
            "INSERT INTO config(key,value) VALUES(?,?)",
            (
                ("notify.enabled", "false"),
                ("notify.webhook_type", "legacy-type"),
                ("notify.webhook_url", ""),
            ),
        )
    database, importer, _ = _importer(target)
    database.initialize()
    report = importer.execute(vshop, auto, expected_counts=None)

    assert report.counts.get("notification_channels", 0) == 0
    with database.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM notification_channels"
        ).fetchone()[0] == 0
        webhook_type = connection.execute(
            "SELECT value_json FROM settings WHERE key=?",
            ("legacy.autocheckin.notify.webhook_type",),
        ).fetchone()[0]
        assert json.loads(webhook_type) == "legacy-type"
        assert connection.execute(
            "SELECT count(*) FROM secrets WHERE purpose LIKE '%notify.webhook%'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("enabled", (False, True))
def test_valid_legacy_webhook_preserves_enabled_state(tmp_path, enabled: bool) -> None:
    vshop, auto, target = tmp_path / "vshop.db", tmp_path / "auto.db", tmp_path / "target.db"
    endpoint = "https://hooks.test/notify?token=fake-webhook-secret"
    _make_vshop(vshop)
    _make_auto(auto)
    with sqlite3.connect(auto) as connection:
        connection.executemany(
            "INSERT INTO config(key,value) VALUES(?,?)",
            (
                ("notify.enabled", json.dumps(enabled)),
                ("notify.webhook_type", "legacy-type"),
                ("notify.webhook_url", endpoint),
            ),
        )
    database, importer, secrets = _importer(target)
    database.initialize()
    importer.execute(vshop, auto, expected_counts=None)

    with database.connect() as connection:
        channel = connection.execute(
            "SELECT enabled,endpoint_secret_id FROM notification_channels"
        ).fetchone()
        assert channel is not None
        assert bool(channel["enabled"]) is enabled
        assert channel["endpoint_secret_id"] == stable_id(
            "autocheckin", "secret", "config:notify.webhook_url",
        )
    assert secrets.reveal(channel["endpoint_secret_id"]).decode() == endpoint
    assert endpoint.encode() not in target.read_bytes()


@pytest.mark.parametrize(
    "endpoint",
    ("file:///tmp/hook", "https://user:pass@hooks.test/notify", "not-a-url"),
)
def test_invalid_legacy_webhook_is_encrypted_but_not_activated(
    tmp_path, endpoint: str,
) -> None:
    vshop, auto, target = tmp_path / "vshop.db", tmp_path / "auto.db", tmp_path / "target.db"
    _make_vshop(vshop)
    _make_auto(auto)
    with sqlite3.connect(auto) as connection:
        connection.executemany(
            "INSERT INTO config(key,value) VALUES(?,?)",
            (
                ("notify.enabled", "true"),
                ("notify.webhook_url", endpoint),
            ),
        )
    database, importer, _ = _importer(target)
    database.initialize()
    importer.execute(vshop, auto, expected_counts=None)

    with database.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM notification_channels"
        ).fetchone()[0] == 0
        stored = json.loads(connection.execute(
            "SELECT value_json FROM settings WHERE key=?",
            ("legacy.autocheckin.notify.webhook_url",),
        ).fetchone()[0])
        assert stored["valid_endpoint"] is False
        assert stored["$secret_id"] == stable_id(
            "autocheckin", "secret", "config:notify.webhook_url",
        )
    assert endpoint.encode() not in target.read_bytes()


def _write_cutover_receipt(
    path: Path,
    *,
    vshop: Path,
    autocheckin: Path,
    target: Path,
    profile_source: Path,
    profile_target: Path,
    artifact_source: Path | None = None,
    artifact_target: Path | None = None,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> Path:
    issued = issued_at or datetime.now(UTC)
    expires = expires_at or issued + timedelta(minutes=30)
    artifact_source = artifact_source or path.parent / "legacy-artifacts"
    artifact_target = artifact_target or path.parent / "copied-artifacts"
    artifact_source.mkdir(parents=True, exist_ok=True)
    profile_source.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(autocheckin) as connection:
        references = [
            (str(legacy_id), str(filename))
            for legacy_id, filename in connection.execute(
                """SELECT id,screenshot FROM logs
                   WHERE trim(coalesce(screenshot,''))!='' ORDER BY id"""
            )
        ]
    for legacy_id, filename in references:
        candidate = artifact_source / filename
        if not candidate.exists():
            candidate.write_bytes(f"artifact-{legacy_id}".encode())
    artifact_inventory = ArtifactCopier(
        autocheckin, artifact_source, artifact_target
    ).inventory()
    profile_inventory = profile_inventory_summary(
        ProfileCopier(profile_source, profile_target).inventory()
    )
    document = {
        "kind": migrate_cli.RECEIPT_KIND,
        "version": migrate_cli.RECEIPT_VERSION,
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "sitehub_project": str(migrate_cli.PROJECT_ROOT),
        "sources": {
            "vshop": {
                "database": str(vshop.resolve()),
                "sha256": hashlib.sha256(vshop.read_bytes()).hexdigest(),
            },
            "autocheckin": {
                "database": str(autocheckin.resolve()),
                "sha256": hashlib.sha256(autocheckin.read_bytes()).hexdigest(),
            },
        },
        "target_database": str(target.resolve()),
        "profile_source": str(profile_source.resolve()),
        "profile_target": str(profile_target.resolve()),
        "profile_manifest": profile_inventory,
        "artifact_source": str(artifact_source.resolve()),
        "artifact_target": str(artifact_target.resolve()),
        "artifact_manifest": {
            "sha256": artifact_inventory.manifest_sha256,
            "files": artifact_inventory.referenced_files,
            "bytes": artifact_inventory.referenced_bytes,
        },
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _cli_apply_args(
    *, vshop: Path, autocheckin: Path, target: Path, receipt: Path | None = None,
) -> list[str]:
    result = [
        "apply", "--vshop-db", str(vshop), "--autocheckin-db", str(autocheckin),
        "--target-db", str(target), "--allow-count-drift",
    ]
    if receipt is not None:
        result.extend(("--preflight-receipt", str(receipt)))
    return result


def _patch_cli_test_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(database: Database) -> SecretService:
        return SecretService(
            database, EphemeralSecretProtector(b"migration-cli-test-key-32-bytes"),
        )

    monkeypatch.setattr(migrate_cli, "SecretService", factory)


def test_plan_and_dry_run_do_not_create_target(tmp_path):
    vshop, auto, target = tmp_path / "vshop.db", tmp_path / "auto.db", tmp_path / "target.db"
    _make_vshop(vshop)
    _make_auto(auto)
    _, importer, _ = _importer(target)
    plan = importer.plan(vshop, auto, expected_counts=None)
    assert plan.target_site_count == 2
    assert not target.exists()


def test_full_apply_requires_a_current_hash_bound_preflight_receipt(tmp_path):
    vshop, auto = tmp_path / "vshop.db", tmp_path / "auto.db"
    target, receipt = tmp_path / "target.db", tmp_path / "receipt.json"
    profile_source, profile_target = tmp_path / "profiles", tmp_path / "copied"
    _make_vshop(vshop)
    _make_auto(auto)
    profile_source.mkdir()

    with pytest.raises(SystemExit, match="requires --preflight-receipt"):
        migrate_cli.main(_cli_apply_args(vshop=vshop, autocheckin=auto, target=target))
    assert not target.exists()

    expired_at = datetime.now(UTC) - timedelta(minutes=1)
    _write_cutover_receipt(
        receipt, vshop=vshop, autocheckin=auto, target=target,
        profile_source=profile_source, profile_target=profile_target,
        issued_at=expired_at - timedelta(minutes=30), expires_at=expired_at,
    )
    with pytest.raises(SystemExit, match="receipt has expired"):
        migrate_cli.main(_cli_apply_args(
            vshop=vshop, autocheckin=auto, target=target, receipt=receipt,
        ))
    assert not target.exists()

    _write_cutover_receipt(
        receipt, vshop=vshop, autocheckin=auto, target=target,
        profile_source=profile_source, profile_target=profile_target,
    )
    with sqlite3.connect(vshop) as connection:
        connection.execute("UPDATE sites SET note='changed after preflight' WHERE id=1")
    with pytest.raises(SystemExit, match="changed after preflight"):
        migrate_cli.main(_cli_apply_args(
            vshop=vshop, autocheckin=auto, target=target, receipt=receipt,
        ))
    assert not target.exists()


def test_failed_business_apply_removes_only_its_new_target_files(tmp_path, monkeypatch):
    vshop, auto = tmp_path / "vshop.db", tmp_path / "auto.db"
    target, receipt = tmp_path / "target.db", tmp_path / "receipt.json"
    profile_source, profile_target = tmp_path / "profiles", tmp_path / "copied"
    _make_vshop(vshop)
    _make_auto(auto)
    profile_source.mkdir()
    _write_cutover_receipt(
        receipt, vshop=vshop, autocheckin=auto, target=target,
        profile_source=profile_source, profile_target=profile_target,
    )
    _patch_cli_test_secrets(monkeypatch)

    def fail_import(self, *args, **kwargs):
        Path(str(target) + "-wal").write_bytes(b"owned-wal")
        Path(str(target) + "-shm").write_bytes(b"owned-shm")
        raise RuntimeError("forced business import failure")

    monkeypatch.setattr(migrate_cli.LegacyImporter, "execute", fail_import)
    with pytest.raises(RuntimeError, match="forced business import failure"):
        migrate_cli.main(_cli_apply_args(
            vshop=vshop, autocheckin=auto, target=target, receipt=receipt,
        ))
    assert not target.exists()
    assert not Path(str(target) + "-wal").exists()
    assert not Path(str(target) + "-shm").exists()

    target.write_bytes(b"pre-existing-target")
    with pytest.raises(SystemExit, match="requires a new target"):
        migrate_cli.main(_cli_apply_args(
            vshop=vshop, autocheckin=auto, target=target, receipt=receipt,
        ))
    assert target.read_bytes() == b"pre-existing-target"


def test_profile_failure_preserves_bound_target_for_profiles_only_resume(
    tmp_path, monkeypatch,
):
    vshop, auto = tmp_path / "vshop.db", tmp_path / "auto.db"
    target, receipt = tmp_path / "target.db", tmp_path / "receipt.json"
    profile_source, profile_target = tmp_path / "profiles", tmp_path / "copied"
    _make_vshop(vshop)
    _make_auto(auto)
    profile_source.mkdir()
    cookies = profile_source / "site-1" / "Default" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"profile-fixture")
    _write_cutover_receipt(
        receipt, vshop=vshop, autocheckin=auto, target=target,
        profile_source=profile_source, profile_target=profile_target,
    )
    _patch_cli_test_secrets(monkeypatch)
    original_copy = LegacyImporter.copy_autocheckin_profiles

    def fail_profiles(self, *args, **kwargs):
        raise RuntimeError("forced Profile copy failure")

    monkeypatch.setattr(
        LegacyImporter, "copy_autocheckin_profiles", fail_profiles,
    )
    full_args = _cli_apply_args(
        vshop=vshop, autocheckin=auto, target=target, receipt=receipt,
    ) + [
        "--copy-profiles", "--profile-source", str(profile_source),
        "--profile-target", str(profile_target),
    ]

    with pytest.raises(RuntimeError, match="forced Profile copy failure"):
        migrate_cli.main(full_args)
    assert target.is_file()
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT count(*) FROM migration_runs WHERE status='succeeded'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM settings WHERE key=?",
            (migrate_cli.RECEIPT_BINDING_SETTING,),
        ).fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM browser_profiles").fetchone()[0] == 0

    monkeypatch.setattr(
        LegacyImporter, "copy_autocheckin_profiles", original_copy,
    )
    resume_args = _cli_apply_args(vshop=vshop, autocheckin=auto, target=target) + [
        "--copy-profiles", "--profiles-only",
        "--profile-source", str(profile_source),
        "--profile-target", str(profile_target),
    ]
    assert migrate_cli.main(resume_args) == 0
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT count(*) FROM browser_profiles").fetchone()[0] == 1


def test_profile_source_change_after_receipt_is_rejected_before_target_write(tmp_path):
    vshop, auto = tmp_path / "vshop.db", tmp_path / "auto.db"
    target, receipt = tmp_path / "target.db", tmp_path / "receipt.json"
    profile_source, profile_target = tmp_path / "profiles", tmp_path / "copied"
    _make_vshop(vshop)
    _make_auto(auto)
    profile_file = profile_source / "site-1" / "Default" / "Cookies"
    profile_file.parent.mkdir(parents=True)
    profile_file.write_bytes(b"before-preflight")
    _write_cutover_receipt(
        receipt, vshop=vshop, autocheckin=auto, target=target,
        profile_source=profile_source, profile_target=profile_target,
    )

    profile_file.write_bytes(b"changed-after-preflight")
    with pytest.raises(SystemExit, match="Profile source manifest changed"):
        migrate_cli.main(_cli_apply_args(
            vshop=vshop, autocheckin=auto, target=target, receipt=receipt,
        ))
    assert not target.exists()


def test_profile_source_change_is_rejected_before_profiles_only_resume_write(
    tmp_path, monkeypatch,
):
    vshop, auto = tmp_path / "vshop.db", tmp_path / "auto.db"
    target, receipt = tmp_path / "target.db", tmp_path / "receipt.json"
    profile_source, profile_target = tmp_path / "profiles", tmp_path / "copied"
    _make_vshop(vshop)
    _make_auto(auto)
    profile_file = profile_source / "site-1" / "Default" / "Cookies"
    profile_file.parent.mkdir(parents=True)
    profile_file.write_bytes(b"receipt-bound")
    _write_cutover_receipt(
        receipt, vshop=vshop, autocheckin=auto, target=target,
        profile_source=profile_source, profile_target=profile_target,
    )
    _patch_cli_test_secrets(monkeypatch)
    assert migrate_cli.main(_cli_apply_args(
        vshop=vshop, autocheckin=auto, target=target, receipt=receipt,
    )) == 0

    profile_file.write_bytes(b"drifted-before-resume")
    resume_args = _cli_apply_args(
        vshop=vshop, autocheckin=auto, target=target,
    ) + [
        "--copy-profiles", "--profiles-only",
        "--profile-source", str(profile_source),
        "--profile-target", str(profile_target),
    ]
    with pytest.raises(SystemExit, match="Profile source manifest changed"):
        migrate_cli.main(resume_args)
    assert not profile_target.exists()
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT count(*) FROM browser_profiles"
        ).fetchone()[0] == 0


def test_artifact_copier_binds_verified_metadata_and_is_idempotent(tmp_path):
    vshop, auto, target = tmp_path / "vshop.db", tmp_path / "auto.db", tmp_path / "target.db"
    source_root, target_root = tmp_path / "legacy-artifacts", tmp_path / "artifacts"
    _make_vshop(vshop)
    _make_auto(auto)
    source_root.mkdir()
    payload = b"\x89PNG\r\n\x1a\nverified-screenshot"
    (source_root / "one.png").write_bytes(payload)
    (source_root / "unreferenced.png").write_bytes(b"not-imported")

    database, importer, _ = _importer(target)
    database.initialize()
    importer.execute(vshop, auto, expected_counts=None)

    inventory = ArtifactCopier(auto, source_root, target_root).inventory()
    assert inventory.referenced_files == 1
    assert inventory.referenced_bytes == len(payload)
    assert inventory.unreferenced_files == 1
    assert inventory.unreferenced_bytes == len(b"not-imported")
    with pytest.raises(RuntimeError, match="manifest changed"):
        importer.copy_autocheckin_artifacts(
            auto, source_root, target_root, expected_manifest="0" * 64,
        )
    assert not target_root.exists()

    counts = importer.copy_autocheckin_artifacts(
        auto, source_root, target_root,
        expected_manifest=inventory.manifest_sha256,
    )
    assert counts == {
        "artifacts": 1,
        "files": 1,
        "bytes": len(payload),
        "unreferenced_files": 1,
        "unreferenced_bytes": len(b"not-imported"),
    }
    destination = target_root / "legacy" / "autocheckin" / "one.png"
    assert destination.read_bytes() == payload
    assert not (target_root / "legacy" / "autocheckin" / "unreferenced.png").exists()
    digest = hashlib.sha256(payload).hexdigest()
    with database.connect() as connection:
        artifact = connection.execute(
            "SELECT * FROM artifacts WHERE id=?",
            (stable_id("autocheckin", "artifact", 1),),
        ).fetchone()
        assert artifact["relative_path"] == "legacy/autocheckin/one.png"
        assert artifact["sha256"] == digest
        assert artifact["size"] == len(payload)
        assert artifact["mime_type"] == "image/png"
        assert connection.execute(
            """SELECT source_fingerprint FROM legacy_map
               WHERE source='autocheckin' AND entity_type='artifact' AND legacy_id='1'"""
        ).fetchone()[0] == digest

    assert importer.copy_autocheckin_artifacts(
        auto, source_root, target_root,
        expected_manifest=inventory.manifest_sha256,
    ) == counts
    destination.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="SHA-256"):
        importer.copy_autocheckin_artifacts(
            auto, source_root, target_root,
            expected_manifest=inventory.manifest_sha256,
        )


def test_artifact_inventory_rejects_unsafe_and_casefold_duplicate_references(tmp_path):
    auto = tmp_path / "auto.db"
    source_root, target_root = tmp_path / "legacy-artifacts", tmp_path / "artifacts"
    _make_auto(auto)
    source_root.mkdir()
    (source_root / "one.png").write_bytes(b"one")
    with sqlite3.connect(auto) as connection:
        connection.execute("UPDATE logs SET screenshot='../escape.png' WHERE id=1")
    with pytest.raises(RuntimeError, match="safe flat filename"):
        ArtifactCopier(auto, source_root, target_root).inventory()

    with sqlite3.connect(auto) as connection:
        connection.execute("UPDATE logs SET screenshot='one.png' WHERE id=1")
        connection.execute(
            """INSERT INTO logs(id,site_id,status,message,screenshot,duration_ms,created_at)
               VALUES(2,1,'success','','ONE.PNG',1,'2026-01-02T08:00:00Z')"""
        )
    with pytest.raises(RuntimeError, match="duplicate filenames"):
        ArtifactCopier(auto, source_root, target_root).inventory()


def test_artifact_failure_preserves_bound_target_for_artifacts_only_resume(
    tmp_path, monkeypatch,
):
    vshop, auto = tmp_path / "vshop.db", tmp_path / "auto.db"
    target, receipt = tmp_path / "target.db", tmp_path / "receipt.json"
    profile_source, profile_target = tmp_path / "profiles", tmp_path / "profiles-copy"
    artifact_source, artifact_target = tmp_path / "legacy-artifacts", tmp_path / "artifacts"
    _make_vshop(vshop)
    _make_auto(auto)
    profile_source.mkdir()
    artifact_source.mkdir()
    (artifact_source / "one.png").write_bytes(b"artifact-fixture")
    _write_cutover_receipt(
        receipt, vshop=vshop, autocheckin=auto, target=target,
        profile_source=profile_source, profile_target=profile_target,
        artifact_source=artifact_source, artifact_target=artifact_target,
    )
    _patch_cli_test_secrets(monkeypatch)
    original_copy = LegacyImporter.copy_autocheckin_artifacts

    def fail_artifacts(self, *args, **kwargs):
        raise RuntimeError("forced artifact copy failure")

    monkeypatch.setattr(LegacyImporter, "copy_autocheckin_artifacts", fail_artifacts)
    full_args = _cli_apply_args(
        vshop=vshop, autocheckin=auto, target=target, receipt=receipt,
    ) + [
        "--copy-artifacts", "--artifact-source", str(artifact_source),
        "--artifact-target", str(artifact_target),
    ]
    with pytest.raises(RuntimeError, match="forced artifact copy failure"):
        migrate_cli.main(full_args)
    assert target.is_file()
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT count(*) FROM migration_runs WHERE status='succeeded'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM settings WHERE key=?",
            (migrate_cli.RECEIPT_BINDING_SETTING,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM artifacts WHERE sha256 IS NOT NULL"
        ).fetchone()[0] == 0

    monkeypatch.setattr(LegacyImporter, "copy_autocheckin_artifacts", original_copy)
    resume_args = _cli_apply_args(vshop=vshop, autocheckin=auto, target=target) + [
        "--copy-artifacts", "--artifacts-only",
        "--artifact-source", str(artifact_source),
        "--artifact-target", str(artifact_target),
    ]
    assert migrate_cli.main(resume_args) == 0
    with sqlite3.connect(target) as connection:
        artifact = connection.execute(
            "SELECT relative_path,sha256,size,mime_type FROM artifacts"
        ).fetchone()
        assert artifact[0] == "legacy/autocheckin/one.png"
        assert len(artifact[1]) == 64
        assert artifact[2] == len(b"artifact-fixture")
        assert artifact[3] == "image/png"


def test_artifact_source_change_after_receipt_is_rejected_before_target_write(tmp_path):
    vshop, auto = tmp_path / "vshop.db", tmp_path / "auto.db"
    target, receipt = tmp_path / "target.db", tmp_path / "receipt.json"
    profile_source, profile_target = tmp_path / "profiles", tmp_path / "profiles-copy"
    artifact_source, artifact_target = tmp_path / "legacy-artifacts", tmp_path / "artifacts"
    _make_vshop(vshop)
    _make_auto(auto)
    profile_source.mkdir()
    artifact_source.mkdir()
    screenshot = artifact_source / "one.png"
    screenshot.write_bytes(b"before-preflight")
    _write_cutover_receipt(
        receipt, vshop=vshop, autocheckin=auto, target=target,
        profile_source=profile_source, profile_target=profile_target,
        artifact_source=artifact_source, artifact_target=artifact_target,
    )
    screenshot.write_bytes(b"after-preflight")
    with pytest.raises(SystemExit, match="artifact source manifest changed"):
        migrate_cli.main(_cli_apply_args(
            vshop=vshop, autocheckin=auto, target=target, receipt=receipt,
        ))
    assert not target.exists()


def test_analyze_reports_artifact_inventory_without_creating_target(tmp_path, capsys):
    vshop, auto, target = tmp_path / "vshop.db", tmp_path / "auto.db", tmp_path / "target.db"
    artifact_source = tmp_path / "legacy-artifacts"
    _make_vshop(vshop)
    _make_auto(auto)
    artifact_source.mkdir()
    (artifact_source / "one.png").write_bytes(b"artifact")
    (artifact_source / "orphan.png").write_bytes(b"orphan")
    assert migrate_cli.main([
        "analyze", "--vshop-db", str(vshop), "--autocheckin-db", str(auto),
        "--target-db", str(target), "--allow-count-drift",
        "--include-artifact-inventory", "--artifact-source", str(artifact_source),
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["artifacts"]["referenced_files"] == 1
    assert output["artifacts"]["referenced_bytes"] == len(b"artifact")
    assert output["artifacts"]["unreferenced_files"] == 1
    assert not target.exists()


def test_full_fixture_import_encrypts_sensitive_values_and_maps_entities(tmp_path):
    vshop, auto, target = tmp_path / "vshop.db", tmp_path / "auto.db", tmp_path / "target.db"
    _make_vshop(vshop)
    _make_auto(auto)
    database, importer, secrets = _importer(target)
    database.initialize()
    report = importer.execute(vshop, auto, expected_counts=None)
    assert report.counts["sites"] == 2
    assert report.counts["products"] == 1
    assert report.counts["jobs"] == 1
    assert report.counts["schedules"] == 2

    with database.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM sites").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM legacy_map").fetchone()[0] >= 10
        row = connection.execute(
            """SELECT url_base,url_suffix_secret_id FROM site_endpoints
               WHERE site_id=?""",
            (stable_id("vshop", "site", 1),),
        ).fetchone()
        assert "?" not in row["url_base"] and "#" not in row["url_base"]
        assert row["url_suffix_secret_id"] == stable_id(
            "vshop", "secret", "site:1:url_suffix",
        )
        restored = restore_sensitive_url(row["url_base"], row["url_suffix_secret_id"], secrets)
        assert restored == "https://shop.test/path?token=fake-url-secret#view"
        product_url = connection.execute(
            """SELECT buy_url_base,buy_url_suffix_secret_id FROM products
               WHERE id=?""",
            (stable_id("vshop", "product", 1),),
        ).fetchone()
        assert product_url["buy_url_suffix_secret_id"] == stable_id(
            "vshop", "secret", "product:1:buy_url_suffix",
        )
        auto_endpoint = connection.execute(
            """SELECT url_suffix_secret_id FROM site_endpoints
               WHERE site_id=?""",
            (stable_id("autocheckin", "site", 1),),
        ).fetchone()
        assert auto_endpoint["url_suffix_secret_id"] == stable_id(
            "autocheckin", "secret", "site:1:url_suffix",
        )
        capability = connection.execute(
            """SELECT adapter,config_json FROM capability_instances
               WHERE site_id=? AND code='checkin'""",
            (stable_id("autocheckin", "site", 1),),
        ).fetchone()
        assert capability["adapter"] == "cookie"
        config = capability["config_json"]
        assert "fake-cookie-secret" not in config
        assert '"adapter"' not in config
        assert '"_secret_fields":["cookie"]' in config
        capability_id = stable_id("autocheckin", "capability-checkin", 1)
        account_id = stable_id("autocheckin", "account", 1)
        capability_ref = connection.execute(
            """SELECT secret_id FROM capability_secrets
               WHERE capability_instance_id=? AND name='cookie'""",
            (capability_id,),
        ).fetchone()[0]
        account_ref = connection.execute(
            """SELECT secret_id FROM account_secrets
               WHERE account_id=? AND name='cookie'""",
            (account_id,),
        ).fetchone()[0]
        assert capability_ref == account_ref
        checkin_schedule = connection.execute(
            """SELECT s.* FROM schedules s
               JOIN capability_instances c ON c.id=s.capability_instance_id
               WHERE c.account_id=? AND c.code='checkin'""",
            (account_id,),
        ).fetchone()
        assert checkin_schedule is not None
        assert checkin_schedule["policy_type"] == "daily_random_window"
        assert json.loads(checkin_schedule["policy_json"]) == {}
        assert checkin_schedule["enabled"] == 0
        assert checkin_schedule["next_run_at"] is None
        assert checkin_schedule["max_attempts"] == 3
        product_schedule = connection.execute(
            """SELECT s.* FROM schedules s
               JOIN capability_instances c ON c.id=s.capability_instance_id
               WHERE c.site_id=? AND c.code='product_monitor'""",
            (stable_id("vshop", "site", 1),),
        ).fetchone()
        assert product_schedule is not None
        assert product_schedule["id"] == stable_id("vshop", "schedule-product", 1)
        assert product_schedule["capability_instance_id"] == stable_id(
            "vshop", "capability-product", 1
        )
        assert product_schedule["timezone"] == "Asia/Shanghai"
        assert product_schedule["policy_type"] == "interval"
        assert product_schedule["enabled"] == 0
        assert product_schedule["next_run_at"] is None
        assert tuple(connection.execute(
            "SELECT json_extract(?, '$.minutes'), json_extract(?, '$.override')",
            (product_schedule["policy_json"], product_schedule["policy_json"]),
        ).fetchone()) == (60, 0)
        assert connection.execute(
            """SELECT new_id FROM legacy_map
               WHERE source='vshop' AND entity_type='schedule' AND legacy_id='1'"""
        ).fetchone()[0] == product_schedule["id"]
        vshop_path = connection.execute(
            """WITH RECURSIVE tree(id,path) AS (
                   SELECT id,name FROM folders WHERE parent_id IS NULL
                   UNION ALL
                   SELECT child.id,tree.path || '/' || child.name
                   FROM folders child JOIN tree ON child.parent_id=tree.id
               )
               SELECT path FROM tree WHERE id=(SELECT folder_id FROM sites WHERE id=?)""",
            (stable_id("vshop", "site", 1),),
        ).fetchone()[0]
        auto_path = connection.execute(
            """WITH RECURSIVE tree(id,path) AS (
                   SELECT id,name FROM folders WHERE parent_id IS NULL
                   UNION ALL
                   SELECT child.id,tree.path || '/' || child.name
                   FROM folders child JOIN tree ON child.parent_id=tree.id
               )
               SELECT path FROM tree WHERE id=(SELECT folder_id FROM sites WHERE id=?)""",
            (stable_id("autocheckin", "site", 1),),
        ).fetchone()[0]
        assert vshop_path == "导入数据/VShop/shop"
        assert auto_path == "导入数据/AutoCheckin"
        vshop_result = connection.execute(
            "SELECT result_json FROM runs WHERE id=?",
            (stable_id("vshop", "run", 1),),
        ).fetchone()[0]
        auto_result = connection.execute(
            "SELECT result_json FROM runs WHERE id=?",
            (stable_id("autocheckin", "run", 1),),
        ).fetchone()[0]
        job_result = connection.execute(
            "SELECT result_json FROM jobs WHERE id=?",
            (stable_id("autocheckin", "job", 1),),
        ).fetchone()[0]
        assert '"legacy_status":"success"' in vshop_result
        assert '"legacy_status":"success"' in auto_result
        assert '"legacy_result_status":"success"' in job_result

    runtime_store = DataLayerStore(target)
    runtime_store.secret_service = secrets
    account = asyncio.run(runtime_store.get_account(UUID(account_id)))
    assert account is not None
    assert account["configured_secret_fields"] == ["cookie"]
    assert account["runtime_config"]["cookie"] == "fake-cookie-secret"
    assert account["runtime_config"]["wait_ms"] == 100

    profile_source = tmp_path / "legacy-profiles"
    profile_file = profile_source / "site-1" / "Default" / "Cookies"
    profile_file.parent.mkdir(parents=True)
    profile_file.write_bytes(b"profile-fixture")
    profile_counts = importer.copy_autocheckin_profiles(
        profile_source, tmp_path / "target-profiles"
    )
    assert profile_counts == {"profiles": 1, "files": 1, "bytes": 15}
    # Profile import is an independently resumable/idempotent phase.
    assert importer.copy_autocheckin_profiles(
        profile_source, tmp_path / "target-profiles"
    ) == profile_counts
    with database.connect() as connection:
        profile_row = connection.execute(
            "SELECT * FROM browser_profiles WHERE account_id=?", (account_id,)
        ).fetchone()
        assert profile_row is not None
        assert len(profile_row["manifest_sha256"]) == 64

    raw_target = target.read_bytes()
    for plaintext in (
        b"fake-url-secret", b"fake-auth-secret", b"fake-coupon-secret",
        b"fake-event-secret", b"fake-account-secret", b"fake-cookie-secret",
    ):
        assert plaintext not in raw_target


def test_failed_import_rolls_back_every_business_row(tmp_path):
    vshop, auto, target = tmp_path / "vshop.db", tmp_path / "auto.db", tmp_path / "target.db"
    _make_vshop(vshop)
    _make_auto(auto, orphan_job=True)
    database, importer, _ = _importer(target)
    database.initialize()
    with pytest.raises(RuntimeError, match="missing site"):
        importer.execute(vshop, auto, expected_counts=None)
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM sites").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM secrets").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM legacy_map").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM migration_runs").fetchone()[0] == 0


def test_import_refuses_a_target_with_existing_business_data(tmp_path):
    vshop, auto, target = tmp_path / "vshop.db", tmp_path / "auto.db", tmp_path / "target.db"
    _make_vshop(vshop)
    _make_auto(auto)
    database, importer, _ = _importer(target)
    database.initialize()
    store = DataLayerStore(target)
    asyncio.run(store.initialize())
    asyncio.run(store.create_site({"name": "Existing", "url": "https://existing.test"}))

    with pytest.raises(RuntimeError, match="requires an empty target"):
        importer.execute(vshop, auto, expected_counts=None)

    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM sites").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM legacy_map").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM migration_runs").fetchone()[0] == 0


def test_sqlite_backup_api_and_profile_copy_manifest(tmp_path):
    source_db, copied_db = tmp_path / "source.db", tmp_path / "copy.db"
    _make_vshop(source_db)
    digest = backup_sqlite_database(source_db, copied_db)
    assert len(digest) == 64
    with sqlite3.connect(copied_db) as connection:
        assert connection.execute("SELECT count(*) FROM sites").fetchone()[0] == 1

    source_profiles, target_profiles = tmp_path / "profiles", tmp_path / "copied-profiles"
    profile = source_profiles / "site-1" / "Default"
    profile.mkdir(parents=True)
    (profile / "Cookies").write_bytes(b"profile-fixture")
    (source_profiles / "site-1" / "SingletonLock").write_bytes(b"excluded")
    copier = ProfileCopier(source_profiles, target_profiles)
    inventory = copier.inventory()
    copied = copier.copy_all()
    assert copied["1"].manifest_sha256 == inventory["1"].manifest_sha256
    assert copied["1"].file_count == 1
    destination = target_profiles / stable_id("autocheckin", "account", 1)
    assert (destination / "Default" / "Cookies").read_bytes() == b"profile-fixture"
    assert not (destination / "SingletonLock").exists()


def test_profile_copier_rejects_bidirectional_tree_overlap(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="must not overlap"):
        ProfileCopier(source, source / "target")

    target = tmp_path / "target"
    nested_source = target / "source"
    nested_source.mkdir(parents=True)
    with pytest.raises(ValueError, match="must not overlap"):
        ProfileCopier(nested_source, target)


@pytest.mark.parametrize("tree_name", ("source", "target"))
def test_profile_copier_rejects_reparse_points(
    tmp_path, monkeypatch, tree_name: str,
) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    marker = (source if tree_name == "source" else target) / "reparse-marker"
    marker.mkdir()
    real_check = legacy_import_module._is_reparse_point
    monkeypatch.setattr(
        legacy_import_module,
        "_is_reparse_point",
        lambda path: path.name == "reparse-marker" or real_check(path),
    )
    with pytest.raises(ValueError, match="reparse point"):
        ProfileCopier(source, target)


@pytest.mark.integration
def test_real_frozen_database_backup_rehearsal(tmp_path):
    vshop_source = Path(
        r"C:\Users\31587\.gemini\antigravity\scratch\vshop_monitor\data\vshop.db"
    )
    auto_source = Path(r"E:\myprojects\auto-checkin-hub\data\checkin.db")
    if not vshop_source.is_file() or not auto_source.is_file():
        pytest.skip("audited legacy databases are unavailable")
    vshop_copy, auto_copy = tmp_path / "vshop.copy.db", tmp_path / "auto.copy.db"
    backup_sqlite_database(vshop_source, vshop_copy)
    backup_sqlite_database(auto_source, auto_copy)
    database, importer, secret_service = _importer(tmp_path / "rehearsal-target.db")
    database.initialize()
    report = importer.execute(
        vshop_copy, auto_copy, expected_counts=FROZEN_SNAPSHOT_COUNTS
    )
    assert report.counts["sites"] == 38
    assert report.counts["products"] == 138
    assert report.counts["jobs"] == 86
    assert report.counts["schedules"] == 33
    with database.connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            """SELECT count(*) FROM site_endpoints
               WHERE instr(url_base,'?')>0 OR instr(url_base,'#')>0"""
        ).fetchone()[0] == 0
        real_account_id = connection.execute(
            "SELECT id FROM site_accounts ORDER BY sort_order,id LIMIT 1"
        ).fetchone()[0]
    runtime_store = DataLayerStore(database.path)
    runtime_store.secret_service = secret_service
    real_account = asyncio.run(runtime_store.get_account(UUID(real_account_id)))
    assert real_account is not None
    assert isinstance(real_account["runtime_config"], dict)
    assert real_account["configured"]

    source_urls: list[tuple[str, int, str]] = []
    for source_name, source_path in (("vshop", vshop_copy), ("autocheckin", auto_copy)):
        with sqlite3.connect(source_path) as source:
            source_urls.extend(
                (source_name, int(legacy_id), str(url))
                for legacy_id, url in source.execute("SELECT id,url FROM sites")
            )
    assert sum("?" in url or "#" in url for _, _, url in source_urls) == 3
    with database.connect() as connection:
        product_capabilities = connection.execute(
            "SELECT count(*) FROM capability_instances WHERE code='product_monitor'"
        ).fetchone()[0]
        product_schedules = connection.execute(
            """SELECT count(*) FROM schedules s
               JOIN capability_instances c ON c.id=s.capability_instance_id
               WHERE c.code='product_monitor' AND s.timezone='Asia/Shanghai'
                 AND s.policy_type='interval' AND s.enabled=0
                 AND json_extract(s.policy_json,'$.minutes')=60
                 AND json_extract(s.policy_json,'$.override')=0"""
        ).fetchone()[0]
        assert product_capabilities == 24
        assert product_schedules == product_capabilities
        checkin_schedules = connection.execute(
            """SELECT count(*) FROM schedules s
               JOIN capability_instances c ON c.id=s.capability_instance_id
               WHERE c.code='checkin' AND s.timezone='Asia/Shanghai'
                 AND s.policy_type='daily_random_window' AND s.policy_json='{}'
                 AND s.enabled=0 AND s.next_run_at IS NULL AND s.max_attempts=3"""
        ).fetchone()[0]
        assert checkin_schedules == 9
        imported_adapters = {
            str(legacy_id): str(adapter)
            for legacy_id, adapter in connection.execute(
                """SELECT m.legacy_id,c.adapter
                   FROM legacy_map m
                   JOIN site_accounts a ON a.id=m.new_id
                   JOIN capability_instances c
                     ON c.account_id=a.id AND c.code='checkin'
                   WHERE m.source='autocheckin' AND m.entity_type='account'"""
            )
        }
        assert imported_adapters == {
            "1": "nodeloc",
            "2": "playwright-generic",
            "3": "nodeseek",
            "4": "btschool",
            "5": "jianzhile",
            "6": "xkool",
            "7": "hundredxlabs",
            "8": "api123nhh",
            "9": "52pojie",
        }
        # The frozen source has notifications disabled, an empty webhook URL,
        # and only a legacy webhook type selector.  It must not manufacture a
        # channel or its legacy mapping.
        assert connection.execute(
            "SELECT count(*) FROM notification_channels"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM legacy_map").fetchone()[0] == 690
        for source_name, legacy_id, source_url in source_urls:
            endpoint = connection.execute(
                """SELECT e.url_base,e.url_suffix_secret_id
                   FROM legacy_map m JOIN site_endpoints e ON e.site_id=m.new_id
                   WHERE m.source=? AND m.entity_type='site' AND m.legacy_id=?
                   ORDER BY e.is_default DESC,e.sort_order LIMIT 1""",
                (source_name, str(legacy_id)),
            ).fetchone()
            assert endpoint is not None
            assert restore_sensitive_url(
                endpoint["url_base"], endpoint["url_suffix_secret_id"], secret_service
            ) == source_url
    assert report.counts["plaintext_values_scanned"] >= 3


@pytest.mark.profile
def test_real_autocheckin_profile_inventory_is_frozen() -> None:
    profile_root = Path(r"E:\myprojects\auto-checkin-hub\data\browser-profiles")
    if not profile_root.is_dir():
        pytest.skip("audited AutoCheckin profiles are unavailable")
    inventory = ProfileCopier(profile_root, profile_root.parent / "sitehub-unused").inventory()
    aggregate = hashlib.sha256()
    for key, item in sorted(inventory.items()):
        aggregate.update(key.encode())
        aggregate.update(b"\0")
        aggregate.update(item.manifest_sha256.encode())
        aggregate.update(b"\n")
    assert len(inventory) == 9
    assert sum(item.file_count for item in inventory.values()) == 5_990
    assert sum(item.total_bytes for item in inventory.values()) == 1_254_631_644
    assert aggregate.hexdigest() == (
        "4666bdc971199c470a18141e2263d2046da0bb01aacb3a84709d81318a2b0919"
    )


@pytest.mark.integration
def test_real_autocheckin_artifact_inventory_is_frozen(tmp_path) -> None:
    auto_source = Path(r"E:\myprojects\auto-checkin-hub\data\checkin.db")
    artifact_source = Path(r"E:\myprojects\auto-checkin-hub\data\screenshots")
    if not auto_source.is_file() or not artifact_source.is_dir():
        pytest.skip("audited AutoCheckin screenshots are unavailable")
    auto_copy = tmp_path / "auto.copy.db"
    backup_sqlite_database(auto_source, auto_copy)
    inventory = ArtifactCopier(
        auto_copy, artifact_source, tmp_path / "unused-artifact-target"
    ).inventory()
    assert inventory.referenced_files == 83
    assert inventory.referenced_bytes == 21_648_632
    assert inventory.unreferenced_files == 8
    assert inventory.unreferenced_bytes == 1_772_179
    assert inventory.manifest_sha256 == (
        "764763f9e8cf8e1f39f2d755ca97ddc070cd037300fd1484553fb77d112cbe60"
    )
