"""Versioned SQLite schema.

Migrations are sequences of individual statements instead of ``executescript``;
this preserves the surrounding explicit transaction on every supported Python.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .connection import Database


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        payload = "\n-- statement --\n".join(s.strip() for s in self.statements)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


V1_STATEMENTS = (
    """CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        checksum TEXT NOT NULL,
        applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )""",
    """CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )""",
    """CREATE TABLE folders (
        id TEXT PRIMARY KEY,
        parent_id TEXT REFERENCES folders(id) ON DELETE RESTRICT,
        name TEXT NOT NULL CHECK(length(trim(name)) > 0),
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX uq_folders_root_name ON folders(name COLLATE NOCASE) WHERE parent_id IS NULL",
    "CREATE UNIQUE INDEX uq_folders_child_name ON folders(parent_id, name COLLATE NOCASE) WHERE parent_id IS NOT NULL",
    """CREATE TABLE secrets (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL CHECK(provider IN ('windows-dpapi-current-user','test')),
        purpose TEXT NOT NULL,
        ciphertext BLOB NOT NULL,
        entropy BLOB NOT NULL,
        created_at TEXT NOT NULL,
        rotated_at TEXT
    )""",
    """CREATE TABLE sites (
        id TEXT PRIMARY KEY,
        folder_id TEXT NOT NULL REFERENCES folders(id) ON DELETE RESTRICT,
        name TEXT NOT NULL CHECK(length(trim(name)) > 0),
        origin TEXT NOT NULL,
        canonical_host TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'other',
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','error','archived')),
        description TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        icon TEXT NOT NULL DEFAULT '',
        favicon_path TEXT,
        pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1)),
        sort_order INTEGER NOT NULL DEFAULT 0,
        visit_count INTEGER NOT NULL DEFAULT 0 CHECK(visit_count >= 0),
        last_visited_at TEXT,
        row_version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_sites_folder_order ON sites(folder_id, pinned DESC, sort_order, id)",
    "CREATE INDEX idx_sites_host ON sites(canonical_host)",
    """CREATE TABLE site_endpoints (
        id TEXT PRIMARY KEY,
        site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK(role IN ('home','bookmark','product_source','checkin','other')),
        label TEXT NOT NULL DEFAULT '',
        url_base TEXT NOT NULL,
        url_suffix_secret_id TEXT REFERENCES secrets(id) ON DELETE RESTRICT,
        query_keys_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(query_keys_json)),
        is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)),
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_site_endpoints_site ON site_endpoints(site_id, role, sort_order)",
    "CREATE UNIQUE INDEX uq_site_endpoint_default_role ON site_endpoints(site_id, role) WHERE is_default = 1",
    """CREATE TABLE tags (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        color TEXT NOT NULL DEFAULT '#8b5cf6',
        icon TEXT NOT NULL DEFAULT 'tag',
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE site_tags (
        site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
        tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY(site_id, tag_id)
    ) WITHOUT ROWID""",
    "CREATE INDEX idx_site_tags_tag ON site_tags(tag_id, site_id)",
    """CREATE TABLE site_accounts (
        id TEXT PRIMARY KEY,
        site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
        label TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
        auth_state TEXT NOT NULL DEFAULT 'unverified' CHECK(auth_state IN ('unverified','ready','expired','error','disabled')),
        last_verified_at TEXT,
        verification_error TEXT,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(site_id, label)
    )""",
    """CREATE TABLE browser_profiles (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL UNIQUE REFERENCES site_accounts(id) ON DELETE CASCADE,
        relative_path TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL DEFAULT 'unverified' CHECK(state IN ('unverified','ready','busy','error','archived')),
        manifest_sha256 TEXT,
        file_count INTEGER,
        total_bytes INTEGER,
        last_verified_at TEXT,
        verification_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE capability_instances (
        id TEXT PRIMARY KEY,
        site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
        code TEXT NOT NULL CHECK(code IN ('bookmark','product_monitor','checkin')),
        account_id TEXT REFERENCES site_accounts(id) ON DELETE CASCADE,
        endpoint_id TEXT REFERENCES site_endpoints(id) ON DELETE SET NULL,
        adapter TEXT NOT NULL DEFAULT 'none',
        config_version INTEGER NOT NULL DEFAULT 1,
        config_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(config_json)),
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK((code = 'checkin' AND account_id IS NOT NULL) OR (code != 'checkin' AND account_id IS NULL))
    )""",
    "CREATE UNIQUE INDEX uq_capability_account ON capability_instances(site_id, code, account_id) WHERE account_id IS NOT NULL",
    "CREATE UNIQUE INDEX uq_capability_site ON capability_instances(site_id, code) WHERE account_id IS NULL",
    """CREATE TABLE account_secrets (
        account_id TEXT NOT NULL REFERENCES site_accounts(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        secret_id TEXT NOT NULL REFERENCES secrets(id) ON DELETE RESTRICT,
        PRIMARY KEY(account_id, name)
    ) WITHOUT ROWID""",
    """CREATE TABLE capability_secrets (
        capability_instance_id TEXT NOT NULL REFERENCES capability_instances(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        secret_id TEXT NOT NULL REFERENCES secrets(id) ON DELETE RESTRICT,
        PRIMARY KEY(capability_instance_id, name)
    ) WITHOUT ROWID""",
    """CREATE TABLE product_categories (
        id TEXT PRIMARY KEY,
        site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_product_categories_site ON product_categories(site_id, sort_order, id)",
    """CREATE TABLE products (
        id TEXT PRIMARY KEY,
        site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
        category_id TEXT REFERENCES product_categories(id) ON DELETE SET NULL,
        external_key TEXT,
        name TEXT NOT NULL,
        price REAL,
        original_price TEXT,
        stock INTEGER NOT NULL DEFAULT -1,
        stock_text TEXT,
        sold INTEGER NOT NULL DEFAULT 0,
        description TEXT,
        buy_endpoint_id TEXT REFERENCES site_endpoints(id) ON DELETE SET NULL,
        buy_url_base TEXT,
        buy_url_suffix_secret_id TEXT REFERENCES secrets(id) ON DELETE RESTRICT,
        raw_tags_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(raw_tags_json)),
        availability_status TEXT NOT NULL DEFAULT 'available' CHECK(availability_status IN ('available','sold_out','missing','delisted','unknown')),
        availability_reason TEXT,
        last_observed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_products_site ON products(site_id, category_id, id)",
    "CREATE UNIQUE INDEX uq_products_external_key ON products(site_id, external_key) WHERE external_key IS NOT NULL",
    """CREATE TABLE product_tags (
        product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY(product_id, tag_id)
    ) WITHOUT ROWID""",
    "CREATE INDEX idx_product_tags_tag ON product_tags(tag_id, product_id)",
    """CREATE TABLE price_observations (
        id TEXT PRIMARY KEY,
        product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        price REAL,
        stock INTEGER,
        availability_status TEXT NOT NULL CHECK(availability_status IN ('available','sold_out','missing','delisted','unknown')),
        observed_at TEXT NOT NULL,
        source_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
        dedupe_key TEXT UNIQUE
    )""",
    "CREATE INDEX idx_price_observations_product ON price_observations(product_id, observed_at, id)",
    """CREATE TABLE schedules (
        id TEXT PRIMARY KEY,
        capability_instance_id TEXT NOT NULL REFERENCES capability_instances(id) ON DELETE CASCADE,
        timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
        policy_type TEXT NOT NULL CHECK(policy_type IN ('manual','cron','interval','daily_random_window')),
        policy_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(policy_json)),
        enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
        next_run_at TEXT,
        misfire_policy TEXT NOT NULL DEFAULT 'recover' CHECK(misfire_policy IN ('skip','run_now','recover')),
        max_attempts INTEGER NOT NULL DEFAULT 1 CHECK(max_attempts >= 1),
        backoff_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(backoff_json)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_schedules_due ON schedules(enabled, next_run_at)",
    """CREATE TABLE job_batches (
        id TEXT PRIMARY KEY,
        schedule_id TEXT REFERENCES schedules(id) ON DELETE SET NULL,
        business_date TEXT,
        timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
        planned_at TEXT,
        trigger_reason TEXT NOT NULL DEFAULT 'normal',
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','completed','expired','cancelled')),
        started_at TEXT,
        completed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_job_batches_business_date ON job_batches(business_date, status)",
    """CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        batch_id TEXT REFERENCES job_batches(id) ON DELETE SET NULL,
        kind TEXT NOT NULL CHECK(kind IN ('checkin','product_sync','bookmark_import','login_session','adapter_test')),
        site_id TEXT REFERENCES sites(id) ON DELETE SET NULL,
        account_id TEXT REFERENCES site_accounts(id) ON DELETE SET NULL,
        capability_instance_id TEXT REFERENCES capability_instances(id) ON DELETE SET NULL,
        business_date TEXT,
        planned_at TEXT,
        available_at TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','leased','running','succeeded','failed','skipped','effect_unknown','cancelled')),
        attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
        max_attempts INTEGER NOT NULL DEFAULT 1 CHECK(max_attempts >= 1),
        lease_owner TEXT,
        lease_token TEXT,
        lease_until TEXT,
        idempotency_key TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
        progress_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(progress_json)),
        result_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(result_json)),
        last_error TEXT,
        started_at TEXT,
        finished_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_jobs_claim ON jobs(status, available_at, priority DESC, created_at)",
    "CREATE INDEX idx_jobs_kind_claim ON jobs(kind, status, available_at, priority DESC, created_at)",
    "CREATE INDEX idx_jobs_site ON jobs(site_id, created_at DESC)",
    "CREATE INDEX idx_jobs_account ON jobs(account_id, created_at DESC)",
    "CREATE INDEX idx_jobs_batch ON jobs(batch_id, created_at)",
    """CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
        capability_instance_id TEXT REFERENCES capability_instances(id) ON DELETE SET NULL,
        account_id TEXT REFERENCES site_accounts(id) ON DELETE SET NULL,
        attempt_no INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','skipped','effect_unknown','cancelled')),
        reason_code TEXT,
        message TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,
        finished_at TEXT,
        duration_ms INTEGER,
        result_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(result_json)),
        legacy_source TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX uq_runs_job_attempt ON runs(job_id, attempt_no) WHERE job_id IS NOT NULL",
    "CREATE INDEX idx_runs_capability_time ON runs(capability_instance_id, started_at DESC)",
    """CREATE TABLE artifacts (
        id TEXT PRIMARY KEY,
        run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
        role TEXT NOT NULL DEFAULT 'screenshot',
        relative_path TEXT NOT NULL UNIQUE,
        sha256 TEXT,
        mime_type TEXT,
        size INTEGER,
        retention_until TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE events (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'info' CHECK(severity IN ('success','info','warning','danger')),
        entity_type TEXT NOT NULL,
        entity_id TEXT,
        run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
        title TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
        dedupe_key TEXT UNIQUE,
        occurred_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_events_time ON events(occurred_at DESC, id)",
    """CREATE TABLE event_state (
        event_id TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
        read_at TEXT,
        dismissed_at TEXT,
        updated_at TEXT NOT NULL
    ) WITHOUT ROWID""",
    """CREATE TABLE notification_channels (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        channel_type TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
        endpoint_secret_id TEXT NOT NULL REFERENCES secrets(id) ON DELETE RESTRICT,
        config_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(config_json)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE notification_outbox (
        id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        channel_id TEXT NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
        idempotency_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','leased','sent','failed','dead','cancelled')),
        attempt INTEGER NOT NULL DEFAULT 0,
        available_at TEXT NOT NULL,
        lease_owner TEXT,
        lease_until TEXT,
        response_code TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_outbox_claim ON notification_outbox(status, available_at, created_at)",
    """CREATE TABLE legacy_map (
        source TEXT NOT NULL CHECK(source IN ('vshop','autocheckin')),
        entity_type TEXT NOT NULL,
        legacy_id TEXT NOT NULL,
        new_id TEXT NOT NULL,
        source_fingerprint TEXT,
        imported_at TEXT NOT NULL,
        PRIMARY KEY(source, entity_type, legacy_id),
        UNIQUE(source, entity_type, new_id)
    ) WITHOUT ROWID""",
    "CREATE INDEX idx_legacy_map_new_id ON legacy_map(new_id)",
    """CREATE TABLE migration_runs (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        source_path_hash TEXT NOT NULL,
        source_db_sha256 TEXT,
        dry_run INTEGER NOT NULL CHECK(dry_run IN (0,1)),
        status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','rolled_back')),
        counts_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(counts_json)),
        error_summary TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT
    )""",
)

V2_STATEMENTS = (
    # Keep the scraper's category separate from the user's effective choice.
    # Existing imported products initially follow their current category.
    "ALTER TABLE product_categories ADD COLUMN source_name TEXT",
    "ALTER TABLE products ADD COLUMN scraped_category_id TEXT REFERENCES product_categories(id) ON DELETE SET NULL",
    "ALTER TABLE products ADD COLUMN category_overridden INTEGER NOT NULL DEFAULT 0 CHECK(category_overridden IN (0,1))",
    "UPDATE product_categories SET source_name=name",
    "UPDATE products SET scraped_category_id=category_id",
    "CREATE INDEX idx_products_scraped_category ON products(site_id, scraped_category_id, id)",
    "CREATE INDEX idx_jobs_capability_status_created ON jobs(capability_instance_id, status, created_at DESC, id DESC)",
)


MIGRATIONS = (
    Migration(1, "initial-unified-schema", V1_STATEMENTS),
    Migration(2, "preserve-manual-product-categories", V2_STATEMENTS),
)
SCHEMA_VERSION = MIGRATIONS[-1].version


def apply_migrations(database: "Database", migrations: Iterable[Migration] = MIGRATIONS) -> None:
    """Apply pending migrations atomically and verify applied checksums."""
    ordered = tuple(sorted(migrations, key=lambda item: item.version))
    with database.transaction(immediate=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        applied: dict[int, sqlite3.Row] = {}
        if exists:
            applied = {
                int(row["version"]): row
                for row in conn.execute("SELECT version, name, checksum FROM schema_migrations")
            }

        for migration in ordered:
            previous = applied.get(migration.version)
            if previous:
                if previous["checksum"] != migration.checksum:
                    raise RuntimeError(
                        f"migration {migration.version} checksum mismatch; database may be incompatible"
                    )
                continue
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version,name,checksum) VALUES(?,?,?)",
                (migration.version, migration.name, migration.checksum),
            )
        if ordered:
            conn.execute(f"PRAGMA user_version = {ordered[-1].version}")
