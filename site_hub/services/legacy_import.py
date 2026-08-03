"""One-time, rollback-safe import from the frozen VShop and AutoCheckin databases.

Planning is strictly read-only and never creates or initializes the target DB.
Execution preserves every legacy site one-to-one; it deliberately does not merge
same-domain business entries.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import urlsplit

from site_hub.db import Database, utc_now
from site_hub.services.secrets import SecretService, split_sensitive_url


LEGACY_NAMESPACE = uuid.UUID("95e5959e-0f6f-4fb4-a8d8-d67548527d5a")
FROZEN_SNAPSHOT_COUNTS: dict[str, dict[str, int]] = {
    "vshop": {"sites": 29, "products": 138, "categories": 37, "tags": 8, "sync_logs": 139},
    "autocheckin": {"sites": 9, "logs": 95, "scheduler_batches": 12, "scheduler_batch_items": 86},
}
_SENSITIVE_KEY = re.compile(
    r"password|passwd|secret|token|cookie|authorization|api[_-]?key|webhook",
    re.IGNORECASE,
)
_PROFILE_LOCK_NAMES = {
    "SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort",
}


def stable_id(source: str, entity_type: str, legacy_id: object) -> str:
    return str(uuid.uuid5(LEGACY_NAMESPACE, f"{source}:{entity_type}:{legacy_id}"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_hash(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()


@contextmanager
def _readonly(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        yield connection
    finally:
        connection.close()


def backup_sqlite_database(source_path: str | Path, destination_path: str | Path) -> str:
    """Create a transactionally consistent source snapshot with SQLite Backup API."""
    source_path, destination_path = Path(source_path), Path(destination_path)
    if destination_path.exists():
        raise FileExistsError(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with _readonly(source_path) as source:
        destination = sqlite3.connect(destination_path)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
    return _sha256_file(destination_path)


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _rows(connection: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    if table not in _tables(connection):
        return []
    return list(connection.execute(f'SELECT * FROM "{table}"'))


def _count(connection: sqlite3.Connection, table: str) -> int:
    if table not in _tables(connection):
        return 0
    return int(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])


def _get(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    return row[name] if name in row.keys() and row[name] is not None else default


def _json_load(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _now_or(value: Any) -> str:
    if value:
        return str(value)
    return utc_now()


def _origin_and_host(url_base: str) -> tuple[str, str]:
    parsed = urlsplit(url_base)
    host = (parsed.hostname or "unknown.invalid").lower()
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else url_base
    return origin, host


def _status(value: Any) -> str:
    lowered = str(value or "").lower()
    if lowered in {"active", "ok", "success", "enabled", "1"}:
        return "active"
    if lowered in {"error", "failed", "failure"}:
        return "error"
    if lowered in {"archived"}:
        return "archived"
    return "disabled" if lowered in {"disabled", "inactive", "0"} else "active"


def _run_status(value: Any) -> str:
    lowered = str(value or "").lower()
    if lowered in {"success", "succeeded", "ok", "completed", "done"}:
        return "succeeded"
    if lowered in {"skip", "skipped"}:
        return "skipped"
    if lowered in {"cancelled", "canceled"}:
        return "cancelled"
    if lowered in {"running", "processing", "started"}:
        return "effect_unknown"
    return "failed"


def _job_status(status: Any, result_status: Any = None) -> str:
    lowered = str(status or "").lower()
    if lowered in {"pending", "planned", "queued", "waiting"}:
        # Imported history must never become executable work on first boot.
        # The original value remains in result_json for forensic fidelity.
        return "cancelled"
    if lowered in {"running", "leased", "processing"}:
        return "effect_unknown"
    return _run_status(result_status or status)


def _batch_status(value: Any) -> str:
    lowered = str(value or "").lower()
    if lowered in {"completed", "success", "done"}:
        return "completed"
    if lowered in {"expired"}:
        return "expired"
    if lowered in {"cancelled", "canceled"}:
        return "cancelled"
    # Imported batches are historical records.  Leaving a legacy pending/running
    # batch active would suppress creation of today's new SiteHub batch.
    return "cancelled"


def _scrub_config(value: Any) -> tuple[Any, list[tuple[str, str]]]:
    """Replace values under sensitive-looking keys and return them separately."""
    extracted: list[tuple[str, str]] = []

    def visit(item: Any, path: str) -> Any:
        if isinstance(item, dict):
            result: dict[str, Any] = {}
            for raw_key, child in item.items():
                key = str(raw_key)
                binding = f"{path}.{key}".strip(".")
                if _SENSITIVE_KEY.search(key) and child not in (None, "", [], {}):
                    serialized = child if isinstance(child, str) else _json_dump(child)
                    extracted.append((binding, serialized))
                else:
                    result[key] = visit(child, binding)
            return result
        if isinstance(item, list):
            return [
                visit(child, f"{path}.{index}".strip("."))
                for index, child in enumerate(item)
            ]
        return item

    return visit(value, ""), extracted


def _runtime_capability_config(
    clean: Any, extracted: Iterable[tuple[str, str]],
) -> dict[str, Any]:
    """Add the dotted-path contract consumed by DataLayerStore at runtime."""
    result = dict(clean) if isinstance(clean, dict) else {"legacy_value": clean}
    result["_secret_fields"] = sorted({binding for binding, _ in extracted})
    return result


def _url_suffix(value: Any) -> str | None:
    text = str(value or "")
    positions = [position for marker in ("?", "#") if (position := text.find(marker)) >= 0]
    return text[min(positions):] if positions else None


def _sensitive_plaintexts(vshop: Path, autocheckin: Path) -> set[bytes]:
    """Collect only values the importer promises to encrypt, never for reporting."""
    values: set[str] = set()
    with _readonly(vshop) as source:
        for row in _rows(source, "sites"):
            suffix = _url_suffix(_get(row, "url"))
            if suffix:
                values.add(suffix)
            _, extracted = _scrub_config(_json_load(_get(row, "scraper_config"), {}))
            values.update(value for _, value in extracted)
        for row in _rows(source, "products"):
            suffix = _url_suffix(_get(row, "buy_url"))
            if suffix:
                values.add(suffix)
        for row in _rows(source, "sync_logs"):
            if _get(row, "error_message"):
                values.add(str(_get(row, "error_message")))
        for row in _rows(source, "events"):
            if _get(row, "detail"):
                values.add(str(_get(row, "detail")))
            _, extracted = _scrub_config(_json_load(_get(row, "payload"), {}))
            values.update(value for _, value in extracted)
    with _readonly(autocheckin) as source:
        for row in _rows(source, "sites"):
            suffix = _url_suffix(_get(row, "url"))
            if suffix:
                values.add(suffix)
            _, extracted = _scrub_config(_json_load(_get(row, "config"), {}))
            values.update(value for _, value in extracted)
        for table in ("logs", "scheduler_batch_items"):
            for row in _rows(source, table):
                if _get(row, "message"):
                    values.add(str(_get(row, "message")))
        for row in _rows(source, "config"):
            key = str(_get(row, "key", _get(row, "name", "")))
            value = _get(row, "value", _get(row, "value_json", ""))
            if _SENSITIVE_KEY.search(key) and value not in (None, ""):
                values.add(str(value))
            else:
                _, extracted = _scrub_config(_json_load(value, value))
                values.update(secret for _, secret in extracted)
    # Tiny values create meaningless/random binary matches in a serialized DB.
    return {value.encode("utf-8") for value in values if len(value.encode("utf-8")) >= 4}


def _assert_no_plaintext_leaks(target: sqlite3.Connection, values: Iterable[bytes]) -> int:
    image = target.serialize()
    checked = 0
    for value in values:
        checked += 1
        if value in image:
            raise RuntimeError("target plaintext scan found an encrypted legacy value")
    return checked


@dataclass(frozen=True)
class SourcePlan:
    source: str
    counts: dict[str, int]
    db_sha256: str
    path_hash: str


@dataclass(frozen=True)
class MigrationPlan:
    sources: tuple[SourcePlan, ...]
    target_site_count: int
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": [asdict(source) for source in self.sources],
            "target_site_count": self.target_site_count,
            "warnings": list(self.warnings),
        }


@dataclass
class MigrationReport:
    migration_run_id: str
    counts: dict[str, int] = field(default_factory=dict)
    profile_counts: dict[str, int] = field(default_factory=dict)
    snapshot_sha256: dict[str, str] = field(default_factory=dict)

    def increment(self, key: str, amount: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + amount

    def to_dict(self) -> dict[str, Any]:
        return {
            "migration_run_id": self.migration_run_id,
            "counts": dict(sorted(self.counts.items())),
            "profile_counts": dict(sorted(self.profile_counts.items())),
            "snapshot_sha256": dict(sorted(self.snapshot_sha256.items())),
        }


class LegacyImporter:
    def __init__(self, database: Database, secrets: SecretService):
        self.database = database
        self.secrets = secrets

    def plan(
        self, vshop_db: str | Path, autocheckin_db: str | Path, *,
        expected_counts: Mapping[str, Mapping[str, int]] | None = FROZEN_SNAPSHOT_COUNTS,
    ) -> MigrationPlan:
        """Inspect sources without opening or modifying the target database."""
        paths = {"vshop": Path(vshop_db), "autocheckin": Path(autocheckin_db)}
        wanted = {
            "vshop": ("sites", "products", "categories", "tags", "sync_logs", "price_history", "events"),
            "autocheckin": ("sites", "logs", "scheduler_batches", "scheduler_batch_items"),
        }
        plans: list[SourcePlan] = []
        warnings: list[str] = []
        for source, path in paths.items():
            with _readonly(path) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise RuntimeError(f"{source} database integrity check failed")
                counts = {table: _count(connection, table) for table in wanted[source]}
            if expected_counts is not None:
                for table, expected in expected_counts.get(source, {}).items():
                    actual = counts.get(table, 0)
                    if actual != expected:
                        raise ValueError(
                            f"{source}.{table} count drift: expected {expected}, found {actual}"
                        )
            plans.append(SourcePlan(source, counts, _sha256_file(path), _path_hash(path)))
        target_sites = sum(plan.counts.get("sites", 0) for plan in plans)
        if target_sites != 38 and expected_counts is not None:
            warnings.append(f"frozen baseline expected 38 sites; plan contains {target_sites}")
        return MigrationPlan(tuple(plans), target_sites, tuple(warnings))

    def execute(
        self, vshop_db: str | Path, autocheckin_db: str | Path, *,
        expected_counts: Mapping[str, Mapping[str, int]] | None = FROZEN_SNAPSHOT_COUNTS,
        profile_source_root: str | Path | None = None,
        profile_target_root: str | Path | None = None,
        snapshot_directory: str | Path | None = None,
        keep_snapshots: bool = False,
    ) -> MigrationReport:
        vshop_db, autocheckin_db = Path(vshop_db).resolve(), Path(autocheckin_db).resolve()
        target_db = self.database.path.resolve()
        if target_db in {vshop_db, autocheckin_db}:
            raise ValueError("target database must differ from both legacy source databases")
        if profile_source_root is not None:
            profile_source = Path(profile_source_root).resolve()
            if target_db == profile_source or profile_source in target_db.parents:
                raise ValueError("target database must not be located inside the profile source")

        source_plan = self.plan(vshop_db, autocheckin_db, expected_counts=expected_counts)
        # Staging names are intentionally unique so concurrent/repeated dry drills
        # cannot collide.  Imported entity and migration IDs remain deterministic.
        staging_token = str(uuid.uuid4())
        snapshot_base = (
            Path(snapshot_directory).resolve()
            if snapshot_directory is not None
            else target_db.parent / ".migration-staging"
        )
        snapshot_root = snapshot_base / staging_token
        for source_root in filter(None, [profile_source_root]):
            resolved = Path(source_root).resolve()
            if snapshot_root == resolved or resolved in snapshot_root.parents:
                raise ValueError("snapshot directory must not be inside the profile source")
        snapshot_root.mkdir(parents=True, exist_ok=False)
        vshop_snapshot = snapshot_root / "vshop.snapshot.db"
        auto_snapshot = snapshot_root / "autocheckin.snapshot.db"
        report: MigrationReport | None = None
        try:
            snapshot_hashes = {
                "vshop": backup_sqlite_database(vshop_db, vshop_snapshot),
                "autocheckin": backup_sqlite_database(autocheckin_db, auto_snapshot),
            }
            snapshot_plan = self.plan(
                vshop_snapshot, auto_snapshot, expected_counts=expected_counts
            )
            sensitive_plaintexts = _sensitive_plaintexts(vshop_snapshot, auto_snapshot)
            migration_id = stable_id(
                "autocheckin", "migration_run",
                ":".join(source.db_sha256 for source in snapshot_plan.sources),
            )
            report = MigrationReport(migration_id, snapshot_sha256=snapshot_hashes)
            started = utc_now()
            with self.database.transaction(immediate=True) as target:
                business_tables = (
                    "sites", "site_endpoints", "tags", "site_accounts", "browser_profiles",
                    "capability_instances", "products", "product_categories", "jobs", "job_batches",
                    "runs", "events", "notification_channels", "legacy_map", "migration_runs",
                )
                occupied = {
                    table: int(target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    for table in business_tables
                }
                occupied = {table: count for table, count in occupied.items() if count}
                non_root_folders = int(target.execute(
                    "SELECT COUNT(*) FROM folders WHERE id!=?",
                    ("00000000-0000-0000-0000-000000000001",),
                ).fetchone()[0])
                if non_root_folders:
                    occupied["folders"] = non_root_folders
                if occupied:
                    names = ", ".join(sorted(occupied))
                    raise RuntimeError(
                        f"legacy apply requires an empty target; existing data found in: {names}"
                    )
                target.execute(
                    """INSERT INTO migration_runs(
                        id,source,source_path_hash,source_db_sha256,dry_run,status,
                        counts_json,started_at
                    ) VALUES(?,?,?,?,0,'running','{}',?)""",
                    (migration_id, "vshop+autocheckin",
                     hashlib.sha256("".join(s.path_hash for s in source_plan.sources).encode()).hexdigest(),
                     hashlib.sha256("".join(snapshot_hashes.values()).encode()).hexdigest(),
                     started),
                )
                imported_root = stable_id("vshop", "folder", "imported-root")
                self._insert_folder(target, imported_root, None, "导入数据", 0)
                vshop_folder = stable_id("vshop", "folder", "vshop")
                auto_folder = stable_id("autocheckin", "folder", "autocheckin")
                self._insert_folder(target, vshop_folder, imported_root, "VShop", 0)
                self._insert_folder(target, auto_folder, imported_root, "AutoCheckin", 1)
                report.increment("folders", 3)
                with _readonly(vshop_snapshot) as source:
                    vshop_type_folders: dict[str, str] = {}
                    legacy_types = sorted({
                        str(_get(row, "kind", "other") or "other").strip() or "other"
                        for row in _rows(source, "sites")
                    })
                    for sort_order, legacy_type in enumerate(legacy_types):
                        folder_id = stable_id("vshop", "folder-kind", legacy_type)
                        self._insert_folder(
                            target, folder_id, vshop_folder, legacy_type, sort_order
                        )
                        vshop_type_folders[legacy_type] = folder_id
                        report.increment("folders")
                    self._import_vshop(source, target, vshop_type_folders, report)
                with _readonly(auto_snapshot) as source:
                    self._import_autocheckin(source, target, auto_folder, report)
                report.counts["plaintext_values_scanned"] = _assert_no_plaintext_leaks(
                    target, sensitive_plaintexts
                )
                target.execute(
                    """UPDATE migration_runs SET status='succeeded',counts_json=?,finished_at=?
                       WHERE id=?""",
                    (_json_dump(report.counts), utc_now(), migration_id),
                )
        except Exception:
            # The business import is fully rolled back.  No secret values or source
            # data are included in the raised/written error summary.
            raise
        finally:
            if not keep_snapshots:
                # Only these known SQLite files were created here; avoid broad deletion.
                for snapshot in (vshop_snapshot, auto_snapshot):
                    for suffix in ("-wal", "-shm", ""):
                        Path(str(snapshot) + suffix).unlink(missing_ok=True)
                snapshot_root.rmdir()
                try:
                    snapshot_base.rmdir()
                except OSError:
                    pass

        if profile_source_root is not None or profile_target_root is not None:
            if profile_source_root is None or profile_target_root is None:
                raise ValueError("both profile source and target roots are required")
            assert report is not None
            report.profile_counts = self.copy_autocheckin_profiles(
                profile_source_root, profile_target_root
            )
        assert report is not None
        return report

    @staticmethod
    def _insert_folder(
        target: sqlite3.Connection, identifier: str, parent_id: str | None,
        name: str, sort_order: int,
    ) -> None:
        now = utc_now()
        target.execute(
            "INSERT INTO folders(id,parent_id,name,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (identifier, parent_id, name, sort_order, now, now),
        )

    @staticmethod
    def _legacy_map(
        target: sqlite3.Connection, source: str, entity_type: str,
        legacy_id: object, new_id: str,
    ) -> None:
        target.execute(
            """INSERT INTO legacy_map(
                source,entity_type,legacy_id,new_id,imported_at
            ) VALUES(?,?,?,?,?)""",
            (source, entity_type, str(legacy_id), new_id, utc_now()),
        )

    def _insert_site(
        self, target: sqlite3.Connection, *, source: str, folder_id: str,
        row: sqlite3.Row, url: str, status: str, kind: str,
    ) -> tuple[str, str]:
        legacy_id = _get(row, "id")
        site_id = stable_id(source, "site", legacy_id)
        sensitive = split_sensitive_url(
            url, self.secrets, f"{source}.site.{legacy_id}.url_suffix", conn=target
        )
        origin, host = _origin_and_host(sensitive.url_base)
        now = utc_now()
        target.execute(
            """INSERT INTO sites(
                id,folder_id,name,origin,canonical_host,kind,status,description,note,icon,
                favicon_path,pinned,sort_order,visit_count,last_visited_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (site_id, folder_id, str(_get(row, "name", f"Imported {legacy_id}")),
             origin, host, kind or "other", status, str(_get(row, "description", "")),
             str(_get(row, "note", "")), str(_get(row, "icon", "")),
             _get(row, "favicon_path"), int(bool(_get(row, "pinned", 0))),
             int(_get(row, "sort_order", 0)), int(_get(row, "visit_count", 0)),
             _get(row, "last_visited_at"), _now_or(_get(row, "created_at")),
             _now_or(_get(row, "updated_at", now))),
        )
        endpoint_id = stable_id(source, "endpoint", legacy_id)
        target.execute(
            """INSERT INTO site_endpoints(
                id,site_id,role,label,url_base,url_suffix_secret_id,query_keys_json,
                is_default,sort_order,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (endpoint_id, site_id, "home" if source == "vshop" else "checkin", "",
             sensitive.url_base, sensitive.suffix_secret_id,
             _json_dump(list(sensitive.query_keys)), 1, 0, now, now),
        )
        self._legacy_map(target, source, "site", legacy_id, site_id)
        return site_id, endpoint_id

    def _bind_extracted(
        self, target: sqlite3.Connection, capability_id: str,
        source: str, legacy_id: object, extracted: Iterable[tuple[str, str]],
        *, account_id: str | None = None,
    ) -> None:
        for index, (binding, plaintext) in enumerate(extracted):
            secret_id = self.secrets.store(
                f"{source}.capability.{legacy_id}.{binding}", plaintext,
                secret_id=stable_id(source, "secret", f"{legacy_id}:{binding}:{index}"),
                conn=target,
            )
            # Runtime restoration reads capability_secrets.  Account binding is
            # retained as ownership metadata, never as the only reference.
            self.secrets.bind_capability(capability_id, binding, secret_id, conn=target)
            if account_id is not None:
                self.secrets.bind_account(account_id, binding, secret_id, conn=target)

    def _import_vshop(
        self, source: sqlite3.Connection, target: sqlite3.Connection,
        folder_ids: Mapping[str, str], report: MigrationReport,
    ) -> None:
        tag_ids: dict[object, str] = {}
        for row in _rows(source, "tags"):
            legacy_id = _get(row, "id")
            tag_id = stable_id("vshop", "tag", legacy_id)
            tag_ids[legacy_id] = tag_id
            now = utc_now()
            target.execute(
                """INSERT INTO tags(id,name,color,icon,sort_order,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (tag_id, str(_get(row, "name", f"Tag {legacy_id}")),
                 str(_get(row, "color", "#8b5cf6")), str(_get(row, "icon", "tag")),
                 int(_get(row, "sort_order", 0)), _now_or(_get(row, "created_at")), now),
            )
            self._legacy_map(target, "vshop", "tag", legacy_id, tag_id)
            report.increment("tags")

        site_ids: dict[object, str] = {}
        product_caps: dict[object, str] = {}
        for row in _rows(source, "sites"):
            legacy_id = _get(row, "id")
            legacy_type = str(_get(row, "kind", "other") or "other").strip() or "other"
            site_id, endpoint_id = self._insert_site(
                target, source="vshop", folder_id=folder_ids[legacy_type], row=row,
                url=str(_get(row, "url", "")), status=_status(_get(row, "status")),
                kind=legacy_type,
            )
            site_ids[legacy_id] = site_id
            bookmark_id = stable_id("vshop", "capability-bookmark", legacy_id)
            now = utc_now()
            target.execute(
                """INSERT INTO capability_instances(
                    id,site_id,code,endpoint_id,adapter,config_json,enabled,created_at,updated_at
                ) VALUES(?,?,'bookmark',?,'url','{}',1,?,?)""",
                (bookmark_id, site_id, endpoint_id, now, now),
            )
            report.increment("capability_instances")
            capabilities = _json_load(_get(row, "capabilities"), [])
            capability_text = _json_dump(capabilities).lower()
            scraper_type = str(_get(row, "scraper_type", "none") or "none")
            if scraper_type.lower() not in {"", "none", "null"} or "product" in capability_text:
                cap_id = stable_id("vshop", "capability-product", legacy_id)
                raw_config = _json_load(_get(row, "scraper_config"), {})
                clean_config, extracted = _scrub_config(raw_config)
                clean_config = _runtime_capability_config(clean_config, extracted)
                target.execute(
                    """INSERT INTO capability_instances(
                        id,site_id,code,endpoint_id,adapter,config_json,enabled,created_at,updated_at
                    ) VALUES(?,?,'product_monitor',?,?,?,?,?,?)""",
                    (cap_id, site_id, endpoint_id, scraper_type or "vshop-generic",
                     _json_dump(clean_config), 1, now, now),
                )
                self._bind_extracted(target, cap_id, "vshop", legacy_id, extracted)
                schedule_id = stable_id("vshop", "schedule-product", legacy_id)
                target.execute(
                    """INSERT INTO schedules(
                        id,capability_instance_id,timezone,policy_type,policy_json,enabled,
                        misfire_policy,max_attempts,backoff_json,created_at,updated_at
                    ) VALUES(?,?,'Asia/Shanghai','interval',?,0,'recover',1,'{}',?,?)""",
                    (schedule_id, cap_id,
                     _json_dump({"minutes": 60, "override": False}), now, now),
                )
                self._legacy_map(
                    target, "vshop", "schedule", legacy_id, schedule_id
                )
                product_caps[legacy_id] = cap_id
                report.increment("capability_instances")
                report.increment("schedules")
            report.increment("sites")
            report.increment("site_endpoints")

        for row in _rows(source, "site_tags"):
            site_id, tag_id = site_ids.get(_get(row, "site_id")), tag_ids.get(_get(row, "tag_id"))
            if site_id and tag_id:
                target.execute("INSERT INTO site_tags(site_id,tag_id) VALUES(?,?)", (site_id, tag_id))
                report.increment("site_tags")

        category_ids: dict[object, str] = {}
        for row in _rows(source, "categories"):
            legacy_id = _get(row, "id")
            site_id = site_ids[_get(row, "site_id")]
            category_id = stable_id("vshop", "category", legacy_id)
            category_ids[legacy_id] = category_id
            now = utc_now()
            target.execute(
                """INSERT INTO product_categories(
                    id,site_id,name,sort_order,created_at,updated_at
                ) VALUES(?,?,?,?,?,?)""",
                (category_id, site_id, str(_get(row, "name", "Uncategorized")),
                 int(_get(row, "sort_order", 0)), now, now),
            )
            self._legacy_map(target, "vshop", "category", legacy_id, category_id)
            report.increment("product_categories")

        product_ids: dict[object, str] = {}
        for row in _rows(source, "products"):
            legacy_id = _get(row, "id")
            product_id = stable_id("vshop", "product", legacy_id)
            product_ids[legacy_id] = product_id
            buy_url = str(_get(row, "buy_url", "") or "")
            buy = split_sensitive_url(
                buy_url, self.secrets, f"vshop.product.{legacy_id}.buy_url_suffix", conn=target
            ) if buy_url else None
            available = bool(_get(row, "is_available", 1))
            raw_tags = _json_load(_get(row, "tags"), [])
            now = utc_now()
            target.execute(
                """INSERT INTO products(
                    id,site_id,category_id,external_key,name,price,original_price,stock,
                    stock_text,sold,description,buy_url_base,buy_url_suffix_secret_id,
                    raw_tags_json,availability_status,last_observed_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (product_id, site_ids[_get(row, "site_id")],
                 category_ids.get(_get(row, "category_id")), str(legacy_id),
                 str(_get(row, "name", f"Product {legacy_id}")), _get(row, "price"),
                 None if _get(row, "original_price") is None else str(_get(row, "original_price")),
                 int(_get(row, "stock", -1)), _get(row, "stock_text"),
                 int(_get(row, "sold", 0)), _get(row, "description"),
                 buy.url_base if buy else None, buy.suffix_secret_id if buy else None,
                 _json_dump(raw_tags if isinstance(raw_tags, list) else []),
                 "available" if available else "sold_out", _get(row, "last_updated"),
                 _now_or(_get(row, "last_updated")), now),
            )
            self._legacy_map(target, "vshop", "product", legacy_id, product_id)
            report.increment("products")

        for row in _rows(source, "product_tags"):
            product_id, tag_id = product_ids.get(_get(row, "product_id")), tag_ids.get(_get(row, "tag_id"))
            if product_id and tag_id:
                target.execute("INSERT INTO product_tags(product_id,tag_id) VALUES(?,?)", (product_id, tag_id))
                report.increment("product_tags")

        for row in _rows(source, "price_history"):
            legacy_id = _get(row, "id")
            product_id = product_ids.get(_get(row, "product_id"))
            if not product_id:
                continue
            target.execute(
                """INSERT INTO price_observations(
                    id,product_id,price,stock,availability_status,observed_at,dedupe_key
                ) VALUES(?,?,?,?,?,?,?)""",
                (stable_id("vshop", "price_observation", legacy_id), product_id,
                 _get(row, "price"), _get(row, "stock"),
                 "available" if bool(_get(row, "is_available", 1)) else "sold_out",
                 _now_or(_get(row, "recorded_at")), f"legacy:vshop:price:{legacy_id}"),
            )
            self._legacy_map(
                target, "vshop", "price_observation", legacy_id,
                stable_id("vshop", "price_observation", legacy_id),
            )
            report.increment("price_observations")

        for row in _rows(source, "sync_logs"):
            legacy_id, legacy_site_id = _get(row, "id"), _get(row, "site_id")
            cap_id = product_caps.get(legacy_site_id)
            if cap_id is None:
                cap_id = stable_id("vshop", "capability-product", legacy_site_id)
                now = utc_now()
                target.execute(
                    """INSERT INTO capability_instances(
                        id,site_id,code,adapter,config_json,enabled,created_at,updated_at
                    ) VALUES(?,?,'product_monitor','legacy-disabled','{}',0,?,?)""",
                    (cap_id, site_ids[legacy_site_id], now, now),
                )
                product_caps[legacy_site_id] = cap_id
                report.increment("capability_instances")
            run_id = stable_id("vshop", "run", legacy_id)
            status = _run_status(_get(row, "status"))
            duration = _get(row, "duration_seconds")
            result: dict[str, Any] = {
                "products_found": int(_get(row, "products_found", 0)),
                "legacy_status": _get(row, "status"),
                "legacy_source": "vshop.sync_logs",
            }
            message = str(_get(row, "error_message", "") or "")
            if message:
                message_secret = self.secrets.store(
                    f"vshop.run.{legacy_id}.message", message,
                    secret_id=stable_id("vshop", "secret", f"run:{legacy_id}:message"), conn=target,
                )
                result["legacy_message_secret_id"] = message_secret
                message = "Legacy detail encrypted"
            created = _now_or(_get(row, "created_at"))
            target.execute(
                """INSERT INTO runs(
                    id,capability_instance_id,attempt_no,status,message,started_at,finished_at,
                    duration_ms,result_json,legacy_source,created_at
                ) VALUES(?,?,1,?,?,?,?,?,?,?,?)""",
                (run_id, cap_id, status, message, created, created,
                 int(float(duration) * 1000) if duration is not None else None,
                 _json_dump(result), "vshop", created),
            )
            self._legacy_map(target, "vshop", "run", legacy_id, run_id)
            report.increment("runs")

        for row in _rows(source, "events"):
            legacy_id = _get(row, "id")
            event_id = stable_id("vshop", "event", legacy_id)
            site_id = site_ids.get(_get(row, "site_id"))
            payload = _json_load(_get(row, "payload"), {})
            clean_payload, extracted = _scrub_config(payload)
            for index, (binding, plaintext) in enumerate(extracted):
                secret_id = self.secrets.store(
                    f"vshop.event.{legacy_id}.{binding}", plaintext,
                    secret_id=stable_id("vshop", "secret", f"event:{legacy_id}:{index}"), conn=target,
                )
                if isinstance(clean_payload, dict):
                    clean_payload.setdefault("encrypted_legacy_fields", []).append(secret_id)
            detail = str(_get(row, "detail", "") or "")
            if detail:
                detail_secret = self.secrets.store(
                    f"vshop.event.{legacy_id}.detail", detail,
                    secret_id=stable_id("vshop", "secret", f"event:{legacy_id}:detail"),
                    conn=target,
                )
                if isinstance(clean_payload, dict):
                    clean_payload["legacy_detail_secret_id"] = detail_secret
                detail = "Legacy detail encrypted"
            occurred = _now_or(_get(row, "created_at"))
            target.execute(
                """INSERT INTO events(
                    id,type,severity,entity_type,entity_id,title,detail,payload_json,
                    dedupe_key,occurred_at,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, str(_get(row, "type", "legacy")),
                 str(_get(row, "severity", "info")), "site", site_id,
                 str(_get(row, "title", "Imported event")), detail,
                 _json_dump(clean_payload), f"legacy:vshop:event:{legacy_id}", occurred, occurred),
            )
            if bool(_get(row, "is_read", 0)):
                target.execute(
                    "INSERT INTO event_state(event_id,read_at,updated_at) VALUES(?,?,?)",
                    (event_id, occurred, occurred),
                )
            self._legacy_map(target, "vshop", "event", legacy_id, event_id)
            report.increment("events")

        self._import_vshop_compatibility_records(source, target, report)

    def _import_vshop_compatibility_records(
        self, source: sqlite3.Connection, target: sqlite3.Connection,
        report: MigrationReport,
    ) -> None:
        """Preserve retired tool/meta rows without changing the audited 38-site baseline."""
        for row in _rows(source, "shop_tags_legacy"):
            legacy_id = _get(row, "id")
            matched = target.execute(
                "SELECT id FROM tags WHERE name=? COLLATE NOCASE",
                (str(_get(row, "name", "")),),
            ).fetchone()
            if matched:
                self._legacy_map(
                    target, "vshop", "shop_tag_legacy", legacy_id, matched["id"]
                )
                report.increment("legacy_shop_tags_mapped")

        legacy_product_links = _count(source, "product_shop_tags_legacy")
        if legacy_product_links:
            target.execute(
                "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
                ("legacy.vshop.compatibility", _json_dump({
                    "product_shop_tag_links_represented_by_product_tags": legacy_product_links,
                }), utc_now()),
            )
            report.increment("settings")

        tools: list[dict[str, Any]] = []
        for row in _rows(source, "tools_legacy"):
            legacy_id = _get(row, "id")
            record_id = stable_id("vshop", "legacy_tool", legacy_id)
            raw_url = str(_get(row, "url", "") or "")
            sensitive = split_sensitive_url(
                raw_url, self.secrets, f"vshop.tool.{legacy_id}.url_suffix", conn=target
            ) if raw_url else None
            tools.append({
                "id": record_id,
                "name": str(_get(row, "name", "")),
                "url_base": sensitive.url_base if sensitive else "",
                "url_suffix_secret_id": sensitive.suffix_secret_id if sensitive else None,
                "query_keys": list(sensitive.query_keys) if sensitive else [],
                "description": str(_get(row, "description", "")),
                "icon": str(_get(row, "icon", "")),
                "category": str(_get(row, "category", "")),
                "sort_order": int(_get(row, "sort_order", 0)),
                "created_at": _get(row, "created_at"),
            })
            self._legacy_map(target, "vshop", "legacy_tool", legacy_id, record_id)
            report.increment("legacy_tools")
        if tools:
            target.execute(
                "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
                ("legacy.vshop.tools", _json_dump(tools), utc_now()),
            )
            report.increment("settings")

        for index, row in enumerate(_rows(source, "meta")):
            key = str(_get(row, "key", f"meta_{index}"))
            value = _get(row, "value")
            if _SENSITIVE_KEY.search(key) and value not in (None, ""):
                secret_id = self.secrets.store(
                    f"vshop.meta.{key}", str(value),
                    secret_id=stable_id("vshop", "secret", f"meta:{key}"), conn=target,
                )
                stored: Any = {"$secret_id": secret_id}
            else:
                parsed = _json_load(value, value)
                clean, extracted = _scrub_config(parsed)
                refs: list[str] = []
                for secret_index, (binding, plaintext) in enumerate(extracted):
                    refs.append(self.secrets.store(
                        f"vshop.meta.{key}.{binding}", plaintext,
                        secret_id=stable_id(
                            "vshop", "secret", f"meta:{key}:{secret_index}"
                        ), conn=target,
                    ))
                stored = clean if not refs else {
                    "value": clean, "encrypted_legacy_fields": refs,
                }
            target.execute(
                """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                       updated_at=excluded.updated_at""",
                (f"legacy.vshop.meta.{key}", _json_dump(stored), utc_now()),
            )
            report.increment("settings")

    def _import_autocheckin(
        self, source: sqlite3.Connection, target: sqlite3.Connection,
        folder_id: str, report: MigrationReport,
    ) -> None:
        site_ids: dict[object, str] = {}
        account_ids: dict[object, str] = {}
        capability_ids: dict[object, str] = {}
        for row in _rows(source, "sites"):
            legacy_id = _get(row, "id")
            enabled = bool(_get(row, "enabled", 1))
            site_id, endpoint_id = self._insert_site(
                target, source="autocheckin", folder_id=folder_id, row=row,
                url=str(_get(row, "url", "")), status="active" if enabled else "disabled",
                kind="checkin",
            )
            site_ids[legacy_id] = site_id
            account_id = stable_id("autocheckin", "account", legacy_id)
            account_ids[legacy_id] = account_id
            now = utc_now()
            target.execute(
                """INSERT INTO site_accounts(
                    id,site_id,label,enabled,auth_state,sort_order,created_at,updated_at
                ) VALUES(?,?,?,?,'unverified',0,?,?)""",
                (account_id, site_id, "Default", int(enabled), now, now),
            )
            self._legacy_map(target, "autocheckin", "account", legacy_id, account_id)
            mode = str(_get(row, "mode", "playwright-generic") or "playwright-generic")
            adapter = {
                "browser": "playwright-generic", "playwright": "playwright-generic",
                "cookie": "cookie", "api": "api",
            }.get(mode.lower(), mode)
            raw_config = _json_load(_get(row, "config"), {})
            clean_config, extracted = _scrub_config(raw_config)
            clean_config = _runtime_capability_config(clean_config, extracted)
            capability_id = stable_id("autocheckin", "capability-checkin", legacy_id)
            capability_ids[legacy_id] = capability_id
            target.execute(
                """INSERT INTO capability_instances(
                    id,site_id,code,account_id,endpoint_id,adapter,config_json,enabled,
                    created_at,updated_at
                ) VALUES(?,?,'checkin',?,?,?,?,?,?,?)""",
                (capability_id, site_id, account_id, endpoint_id, adapter,
                 _json_dump(clean_config), int(enabled), now, now),
            )
            self._bind_extracted(
                target, capability_id, "autocheckin", legacy_id, extracted,
                account_id=account_id,
            )
            schedule_id = stable_id("autocheckin", "schedule", legacy_id)
            cron = str(_get(row, "cron", "") or "")
            policy_type = "cron" if cron else "manual"
            policy = {"expression": cron} if cron else {}
            target.execute(
                """INSERT INTO schedules(
                    id,capability_instance_id,timezone,policy_type,policy_json,enabled,
                    misfire_policy,max_attempts,backoff_json,created_at,updated_at
                ) VALUES(?,?,'Asia/Shanghai',?,?,0,'recover',1,'{}',?,?)""",
                (schedule_id, capability_id, policy_type, _json_dump(policy), now, now),
            )
            self._legacy_map(target, "autocheckin", "schedule", legacy_id, schedule_id)
            report.increment("sites")
            report.increment("site_endpoints")
            report.increment("site_accounts")
            report.increment("capability_instances")
            report.increment("schedules")

        for row in _rows(source, "logs"):
            legacy_id, legacy_site_id = _get(row, "id"), _get(row, "site_id")
            cap_id = capability_ids.get(legacy_site_id)
            if cap_id is None:
                raise RuntimeError("AutoCheckin log references a missing site")
            run_id = stable_id("autocheckin", "run", legacy_id)
            created = _now_or(_get(row, "created_at"))
            message = str(_get(row, "message", "") or "")
            result: dict[str, Any] = {
                "legacy_status": _get(row, "status"),
                "legacy_source": "autocheckin.logs",
            }
            if message:
                message_secret = self.secrets.store(
                    f"autocheckin.run.{legacy_id}.message", message,
                    secret_id=stable_id("autocheckin", "secret", f"run:{legacy_id}:message"),
                    conn=target,
                )
                result["legacy_message_secret_id"] = message_secret
                message = "Legacy detail encrypted"
            target.execute(
                """INSERT INTO runs(
                    id,capability_instance_id,account_id,attempt_no,status,message,started_at,
                    finished_at,duration_ms,result_json,legacy_source,created_at
                ) VALUES(?,?,?,1,?,?,?,?,?,?,?,?)""",
                (run_id, cap_id, account_ids[legacy_site_id], _run_status(_get(row, "status")),
                 message, created, created, _get(row, "duration_ms"), _json_dump(result),
                 "autocheckin", created),
            )
            screenshot = _get(row, "screenshot")
            if screenshot:
                artifact_id = stable_id("autocheckin", "artifact", legacy_id)
                target.execute(
                    """INSERT INTO artifacts(
                        id,run_id,role,relative_path,created_at
                    ) VALUES(?,?,'screenshot',?,?)""",
                    (artifact_id, run_id, str(screenshot), created),
                )
                self._legacy_map(target, "autocheckin", "artifact", legacy_id, artifact_id)
                report.increment("artifacts")
            self._legacy_map(target, "autocheckin", "run", legacy_id, run_id)
            report.increment("runs")

        batch_ids: dict[object, str] = {}
        for row in _rows(source, "scheduler_batches"):
            legacy_id = _get(row, "id")
            batch_id = stable_id("autocheckin", "batch", legacy_id)
            batch_ids[legacy_id] = batch_id
            created = _now_or(_get(row, "created_at"))
            legacy_trigger = str(_get(row, "trigger_reason", "legacy"))
            legacy_status = str(_get(row, "status", ""))
            trigger_reason = (
                f"legacy:autocheckin; trigger={legacy_trigger}; status={legacy_status}"
            )
            target.execute(
                """INSERT INTO job_batches(
                    id,business_date,timezone,planned_at,trigger_reason,status,started_at,
                    completed_at,created_at,updated_at
                ) VALUES(?,?,'Asia/Shanghai',?,?,?,?,?,?,?)""",
                (batch_id, _get(row, "schedule_date"),
                 _get(row, "planned_at", _get(row, "recovery_at")),
                 trigger_reason, _batch_status(_get(row, "status")),
                 _get(row, "started_at"), _get(row, "completed_at"), created,
                 _now_or(_get(row, "updated_at", created))),
            )
            self._legacy_map(target, "autocheckin", "batch", legacy_id, batch_id)
            report.increment("job_batches")

        for row in _rows(source, "scheduler_batch_items"):
            legacy_id, legacy_site_id = _get(row, "id"), _get(row, "site_id")
            cap_id = capability_ids.get(legacy_site_id)
            if cap_id is None:
                raise RuntimeError("AutoCheckin batch item references a missing site")
            job_id = stable_id("autocheckin", "job", legacy_id)
            status = _job_status(_get(row, "status"), _get(row, "result_status"))
            created = _now_or(_get(row, "started_at", _get(row, "finished_at")))
            payload = {
                "legacy_position": _get(row, "position"),
                "legacy_site_name": _get(row, "site_name", ""),
                "legacy_status": _get(row, "status"),
                "legacy_result_status": _get(row, "result_status"),
            }
            result: dict[str, Any] = {
                "legacy_status": _get(row, "status"),
                "legacy_result_status": _get(row, "result_status"),
                "legacy_source": "autocheckin.scheduler_batch_items",
            }
            message = str(_get(row, "message", "") or "")
            last_error = None
            if message:
                message_secret = self.secrets.store(
                    f"autocheckin.job.{legacy_id}.message", message,
                    secret_id=stable_id("autocheckin", "secret", f"job:{legacy_id}:message"),
                    conn=target,
                )
                result["legacy_message_secret_id"] = message_secret
                if status in {"failed", "effect_unknown"}:
                    last_error = "Legacy detail encrypted"
            target.execute(
                """INSERT INTO jobs(
                    id,batch_id,kind,site_id,account_id,capability_instance_id,business_date,planned_at,available_at,
                    priority,status,attempt,max_attempts,idempotency_key,payload_json,result_json,
                    last_error,started_at,finished_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, batch_ids.get(_get(row, "batch_id")), "checkin",
                 site_ids[legacy_site_id], account_ids[legacy_site_id], cap_id, None,
                 _get(row, "started_at"), created, -int(_get(row, "position", 0)), status,
                 1 if status not in {"queued"} else 0, 1,
                 f"legacy:autocheckin:item:{legacy_id}", _json_dump(payload),
                 _json_dump(result), last_error, _get(row, "started_at"),
                 _get(row, "finished_at"), created, created),
            )
            self._legacy_map(target, "autocheckin", "job", legacy_id, job_id)
            report.increment("jobs")

        self._import_autocheckin_config(source, target, report)

    def _import_autocheckin_config(
        self, source: sqlite3.Connection, target: sqlite3.Connection,
        report: MigrationReport,
    ) -> None:
        for index, row in enumerate(_rows(source, "config")):
            key = str(_get(row, "key", _get(row, "name", f"legacy_{index}")))
            value = _get(row, "value", _get(row, "value_json", ""))
            if _SENSITIVE_KEY.search(key) and value not in (None, ""):
                secret_id = self.secrets.store(
                    f"autocheckin.config.{key}", str(value),
                    secret_id=stable_id("autocheckin", "secret", f"config:{key}"), conn=target,
                )
                if "webhook" in key.lower():
                    channel_id = stable_id("autocheckin", "notification_channel", key)
                    now = utc_now()
                    target.execute(
                        """INSERT INTO notification_channels(
                            id,name,channel_type,enabled,endpoint_secret_id,config_json,
                            created_at,updated_at
                        ) VALUES(?,?,?,1,?,'{}',?,?)""",
                        (channel_id, f"Imported {key}", "webhook", secret_id, now, now),
                    )
                    self._legacy_map(
                        target, "autocheckin", "notification_channel", key, channel_id
                    )
                    report.increment("notification_channels")
                continue
            parsed = _json_load(value, value)
            clean, extracted = _scrub_config(parsed)
            if extracted:
                # Preserve nested secrets as encrypted records referenced by opaque IDs.
                refs: list[str] = []
                for secret_index, (binding, plaintext) in enumerate(extracted):
                    refs.append(self.secrets.store(
                        f"autocheckin.config.{key}.{binding}", plaintext,
                        secret_id=stable_id(
                            "autocheckin", "secret", f"config:{key}:{secret_index}"
                        ), conn=target,
                    ))
                clean = {"value": clean, "encrypted_legacy_fields": refs}
            target.execute(
                """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                       updated_at=excluded.updated_at""",
                (f"legacy.autocheckin.{key}", _json_dump(clean), utc_now()),
            )
            report.increment("settings")

    def copy_autocheckin_profiles(
        self, source_root: str | Path, target_root: str | Path,
    ) -> dict[str, int]:
        """Copy site-* profiles, verify hashes, then register account bindings."""
        copier = ProfileCopier(Path(source_root), Path(target_root))
        with self.database.connect() as source:
            expected_legacy_ids = {
                str(row[0]) for row in source.execute(
                    """SELECT legacy_id FROM legacy_map
                       WHERE source='autocheckin' AND entity_type='account'"""
                )
            }
        if not expected_legacy_ids:
            raise RuntimeError("target has no imported AutoCheckin accounts")
        manifests = copier.copy_all(expected_legacy_site_ids=expected_legacy_ids)
        with self.database.transaction(immediate=True) as target:
            for legacy_site_id, manifest in manifests.items():
                account_id = stable_id("autocheckin", "account", legacy_site_id)
                if not target.execute(
                    "SELECT 1 FROM site_accounts WHERE id=?", (account_id,)
                ).fetchone():
                    raise RuntimeError("profile has no imported AutoCheckin account")
                profile_id = stable_id("autocheckin", "browser_profile", legacy_site_id)
                target.execute(
                    """INSERT INTO browser_profiles(
                        id,account_id,relative_path,state,manifest_sha256,file_count,
                        total_bytes,created_at,updated_at
                    ) VALUES(?,?,?,'unverified',?,?,?,?,?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        relative_path=excluded.relative_path,
                        manifest_sha256=excluded.manifest_sha256,
                        file_count=excluded.file_count,total_bytes=excluded.total_bytes,
                        updated_at=excluded.updated_at""",
                    (profile_id, account_id, manifest.relative_path,
                     manifest.manifest_sha256, manifest.file_count, manifest.total_bytes,
                     utc_now(), utc_now()),
                )
                target.execute(
                    """INSERT INTO legacy_map(
                        source,entity_type,legacy_id,new_id,source_fingerprint,imported_at
                    ) VALUES('autocheckin','browser_profile',?,?,?,?)
                    ON CONFLICT(source,entity_type,legacy_id) DO UPDATE SET
                        source_fingerprint=excluded.source_fingerprint,
                        imported_at=excluded.imported_at""",
                    (str(legacy_site_id), profile_id, manifest.manifest_sha256, utc_now()),
                )
        return {
            "profiles": len(manifests),
            "files": sum(item.file_count for item in manifests.values()),
            "bytes": sum(item.total_bytes for item in manifests.values()),
        }


@dataclass(frozen=True)
class ProfileManifest:
    legacy_site_id: str
    relative_path: str
    manifest_sha256: str
    file_count: int
    total_bytes: int


class ProfileCopier:
    def __init__(self, source_root: Path, target_root: Path):
        self.source_root = source_root.resolve()
        self.target_root = target_root.resolve()
        if not self.source_root.is_dir():
            raise FileNotFoundError(self.source_root)
        if self.target_root == self.source_root or self.source_root in self.target_root.parents:
            raise ValueError("profile target must not be inside the source tree")

    @staticmethod
    def _included_files(root: Path) -> Iterator[Path]:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.name not in _PROFILE_LOCK_NAMES:
                yield path

    @classmethod
    def manifest(cls, root: Path) -> tuple[str, int, int]:
        aggregate = hashlib.sha256()
        count = total = 0
        for path in cls._included_files(root):
            relative = path.relative_to(root).as_posix()
            size = path.stat().st_size
            digest = _sha256_file(path)
            aggregate.update(relative.encode("utf-8"))
            aggregate.update(b"\0")
            aggregate.update(str(size).encode("ascii"))
            aggregate.update(b"\0")
            aggregate.update(digest.encode("ascii"))
            aggregate.update(b"\n")
            count += 1
            total += size
        return aggregate.hexdigest(), count, total

    def inventory(self) -> dict[str, ProfileManifest]:
        result: dict[str, ProfileManifest] = {}
        for directory in sorted(self.source_root.glob("site-*")):
            if not directory.is_dir():
                continue
            legacy_site_id = directory.name[5:]
            digest, count, total = self.manifest(directory)
            result[legacy_site_id] = ProfileManifest(
                legacy_site_id, stable_id("autocheckin", "account", legacy_site_id),
                digest, count, total,
            )
        return result

    def copy_all(
        self, *, expected_legacy_site_ids: set[str] | None = None,
    ) -> dict[str, ProfileManifest]:
        source_manifests = self.inventory()
        if expected_legacy_site_ids is not None:
            actual = set(source_manifests)
            if actual != expected_legacy_site_ids:
                missing = len(expected_legacy_site_ids - actual)
                unexpected = len(actual - expected_legacy_site_ids)
                raise RuntimeError(
                    "Profile source does not exactly match imported AutoCheckin accounts "
                    f"(missing={missing}, unexpected={unexpected})"
                )
        self.target_root.mkdir(parents=True, exist_ok=True)
        result: dict[str, ProfileManifest] = {}
        for legacy_site_id, source_manifest in source_manifests.items():
            relative_path = stable_id("autocheckin", "account", legacy_site_id)
            destination = self.target_root / relative_path
            if destination.exists():
                digest, count, total = self.manifest(destination)
                if (digest, count, total) != (
                    source_manifest.manifest_sha256,
                    source_manifest.file_count,
                    source_manifest.total_bytes,
                ):
                    raise RuntimeError("existing profile destination failed manifest verification")
            else:
                staging = self.target_root / f".{relative_path}.tmp-{uuid.uuid4()}"
                try:
                    staging.mkdir(parents=False)
                    source_dir = self.source_root / f"site-{legacy_site_id}"
                    for source_file in self._included_files(source_dir):
                        relative = source_file.relative_to(source_dir)
                        target_file = staging / relative
                        target_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source_file, target_file)
                    digest, count, total = self.manifest(staging)
                    if (digest, count, total) != (
                        source_manifest.manifest_sha256,
                        source_manifest.file_count,
                        source_manifest.total_bytes,
                    ):
                        raise RuntimeError("copied profile failed manifest verification")
                    os.replace(staging, destination)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            result[legacy_site_id] = ProfileManifest(
                legacy_site_id, relative_path, source_manifest.manifest_sha256,
                source_manifest.file_count, source_manifest.total_bytes,
            )
        return result


__all__ = [
    "FROZEN_SNAPSHOT_COUNTS",
    "LegacyImporter",
    "MigrationPlan",
    "MigrationReport",
    "ProfileCopier",
    "ProfileManifest",
    "SourcePlan",
    "backup_sqlite_database",
    "stable_id",
]
