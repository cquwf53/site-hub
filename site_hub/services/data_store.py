from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from site_hub.db import Database, SiteHubRepository
from site_hub.notifications import NotificationOutboxStore
from site_hub.scrapers.network import sanitize_log_value
from site_hub.services.ports import AccountNotReady, SiteTypeConflict, StaleJobWrite


T = TypeVar("T")


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    value = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _uuid(value: UUID | str) -> str:
    return str(UUID(str(value)))


class DataLayerStore:
    """Async control-plane adapter over the synchronous, durable SQLite layer.

    The adapter deliberately owns API-shaped joins and queue state transitions;
    the lower data layer stays small and reusable. Every blocking database call
    runs in a worker thread, while claim/heartbeat/finish transitions use
    ``BEGIN IMMEDIATE`` and a per-lease random token.
    """

    ROOT_FOLDER_ID = "00000000-0000-0000-0000-000000000001"
    CHECKIN_CODE = "checkin"
    PRODUCT_CODE = "product_monitor"

    def __init__(self, path: str | Path) -> None:
        self.database = Database(path)
        self.repository = SiteHubRepository(self.database)
        self.notification_outbox = NotificationOutboxStore(self.database)
        self.secret_service: Any | None = None

    async def _call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.to_thread(func, *args, **kwargs)

    async def initialize(self) -> None:
        await self._call(self.database.initialize)
        await self._call(self._ensure_defaults)
        # The data-layer service is optional only during staged startup/tests.
        # Production Windows builds provide DPAPI and never fall back to plaintext.
        try:
            from site_hub.services.secrets import SecretService

            self.secret_service = SecretService(self.database)
        except (ImportError, RuntimeError):
            self.secret_service = None

    async def close(self) -> None:
        await self._call(self.database.close)

    async def ping(self) -> bool:
        try:
            return bool(await self._call(self._ping_sync))
        except sqlite3.Error:
            return False

    def _ping_sync(self) -> bool:
        with self.database.connect() as conn:
            return conn.execute("SELECT 1").fetchone()[0] == 1

    def _ensure_defaults(self) -> None:
        with self.database.transaction(immediate=True) as conn:
            now = _iso(datetime.now(UTC))
            conn.execute(
                """INSERT OR IGNORE INTO folders(id,parent_id,name,sort_order,created_at,updated_at)
                   VALUES(?,NULL,'Sites',0,?,?)""",
                (self.ROOT_FOLDER_ID, now, now),
            )
            interrupted_accounts = [
                str(row[0]) for row in conn.execute(
                    """SELECT DISTINCT bp.account_id
                       FROM browser_profiles bp
                       JOIN capability_instances c
                         ON c.account_id=bp.account_id AND c.code='checkin'
                       WHERE bp.state='busy'
                         AND lower(coalesce(c.adapter,'')) NOT IN ('api','cookie')"""
                )
            ]
            conn.execute(
                """UPDATE browser_profiles
                   SET state='unverified',verification_error='Previous verification was interrupted',
                       updated_at=?
                   WHERE state='busy'""",
                (now,),
            )
            for account_id in interrupted_accounts:
                conn.execute(
                    """UPDATE site_accounts
                       SET enabled=0,auth_state='unverified',
                           verification_error='Previous verification was interrupted',updated_at=?
                       WHERE id=? AND auth_state!='disabled'""",
                    (now, account_id),
                )
                conn.execute(
                    """UPDATE capability_instances SET enabled=0,updated_at=?
                       WHERE account_id=? AND code='checkin'""",
                    (now, account_id),
                )
                conn.execute(
                    """UPDATE schedules SET enabled=0,updated_at=?
                       WHERE capability_instance_id IN (
                           SELECT id FROM capability_instances
                           WHERE account_id=? AND code='checkin'
                       )""",
                    (now, account_id),
                )
                self._cancel_queued_checkins_locked(
                    conn, now, account_id=account_id,
                )
            self._repair_batch_states_locked(conn, now)
        defaults = {
            "scheduler.timezone": "Asia/Shanghai",
            "scheduler.checkin_window_start": "08:00:00",
            "scheduler.checkin_window_end": "22:00:00",
            "scheduler.product_interval_minutes": "60",
            "notifications.config": _dumps({
                "enabled": True,
                "price_drop": True,
                "back_in_stock": True,
                "checkin_failed": True,
                "checkin_success": False,
                "product_sync_failed": True,
            }),
        }
        existing = self.repository.list_settings()
        for key, value in defaults.items():
            if key not in existing:
                self.repository.set_setting(key, value)

    # -- Controlled site types --------------------------------------
    @staticmethod
    def _resolve_site_type_locked(
        conn: sqlite3.Connection, *, site_type_id: str | None = None,
        kind: str | None = None, require_enabled: bool = True,
    ) -> sqlite3.Row:
        normalized_kind = str(kind).strip().lower() if kind is not None else None
        if site_type_id is not None:
            row = conn.execute("SELECT * FROM site_types WHERE id=?", (site_type_id,)).fetchone()
            if row is not None and normalized_kind is not None and row["slug"].lower() != normalized_kind:
                raise SiteTypeConflict(
                    "site_type_id and kind refer to different site types",
                    code="SITE_TYPE_MISMATCH",
                )
        else:
            row = conn.execute(
                "SELECT * FROM site_types WHERE slug=? COLLATE NOCASE",
                (normalized_kind or "other",),
            ).fetchone()
        if row is None:
            raise SiteTypeConflict("Site type not found", code="SITE_TYPE_NOT_FOUND")
        if require_enabled and not bool(row["enabled"]):
            raise SiteTypeConflict("Site type is disabled", code="SITE_TYPE_DISABLED")
        return row

    @staticmethod
    def _normalize_site_type(
        conn: sqlite3.Connection, row: sqlite3.Row,
    ) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["is_system"] = bool(result["is_system"])
        result["is_default"] = bool(result["is_default"])
        result["site_count"] = int(conn.execute(
            "SELECT COUNT(*) FROM sites WHERE site_type_id=?", (row["id"],)
        ).fetchone()[0])
        return result

    async def list_site_types(self, *, enabled: bool | None = None) -> list[dict[str, Any]]:
        return await self._call(self.repository.list_site_types, enabled=enabled)

    async def get_site_type(self, site_type_id: UUID) -> dict[str, Any] | None:
        return await self._call(self.repository.get_site_type, _uuid(site_type_id))

    async def create_site_type(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._call(
                self.repository.create_site_type,
                name=payload["name"], slug=payload["slug"],
                color=payload.get("color", "#64748b"), icon=payload.get("icon", "globe"),
                sort_order=int(payload.get("sort_order", 0)),
                enabled=bool(payload.get("enabled", True)),
            )
        except sqlite3.IntegrityError as exc:
            raise SiteTypeConflict(
                "A site type with this name or slug already exists",
            ) from exc

    async def update_site_type(
        self, site_type_id: UUID, payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        try:
            return await self._call(
                self._update_site_type_sync, _uuid(site_type_id), payload,
            )
        except sqlite3.IntegrityError as exc:
            raise SiteTypeConflict(
                "A site type with this name or slug already exists",
            ) from exc

    def _update_site_type_sync(
        self, site_type_id: str, payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        allowed = {"name", "slug", "color", "icon", "sort_order", "enabled"}
        unsupported = set(payload) - allowed
        if unsupported:
            raise ValueError(f"unsupported site type field: {sorted(unsupported)[0]}")
        now = _iso(datetime.now(UTC))
        with self.database.transaction(immediate=True) as conn:
            current = conn.execute("SELECT * FROM site_types WHERE id=?", (site_type_id,)).fetchone()
            if current is None:
                return None
            values = dict(payload)
            if bool(current["is_system"]):
                if "slug" in values and str(values["slug"]).strip().lower() != current["slug"]:
                    raise SiteTypeConflict(
                        "Built-in site type slugs cannot be changed",
                        code="SITE_TYPE_PROTECTED",
                    )
                if values.get("enabled") is False:
                    raise SiteTypeConflict(
                        "Built-in site types cannot be disabled",
                        code="SITE_TYPE_PROTECTED",
                    )
            for key in ("name", "slug", "icon"):
                if key in values:
                    values[key] = str(values[key]).strip()
            if "slug" in values:
                values["slug"] = str(values["slug"]).lower()
            if "color" in values:
                values["color"] = str(values["color"]).lower()
            if "enabled" in values:
                values["enabled"] = int(bool(values["enabled"]))
            if values:
                conn.execute(
                    f"UPDATE site_types SET {','.join(key + '=?' for key in values)},updated_at=? WHERE id=?",
                    [*values.values(), now, site_type_id],
                )
                if "slug" in values:
                    conn.execute(
                        "UPDATE sites SET kind=?,row_version=row_version+1,updated_at=? WHERE site_type_id=?",
                        (values["slug"], now, site_type_id),
                    )
            row = conn.execute("SELECT * FROM site_types WHERE id=?", (site_type_id,)).fetchone()
            return self._normalize_site_type(conn, row)

    async def delete_site_type(self, site_type_id: UUID) -> bool:
        return await self._call(self._delete_site_type_sync, _uuid(site_type_id))

    def _delete_site_type_sync(self, site_type_id: str) -> bool:
        with self.database.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM site_types WHERE id=?", (site_type_id,)).fetchone()
            if row is None:
                return False
            if bool(row["is_system"]):
                raise SiteTypeConflict(
                    "Built-in site types cannot be deleted", code="SITE_TYPE_PROTECTED",
                )
            if conn.execute(
                "SELECT 1 FROM sites WHERE site_type_id=? LIMIT 1", (site_type_id,)
            ).fetchone():
                raise SiteTypeConflict(
                    "Move the assigned sites before deleting this type",
                    code="SITE_TYPE_IN_USE",
                )
            return conn.execute("DELETE FROM site_types WHERE id=?", (site_type_id,)).rowcount == 1

    async def bulk_set_site_type(
        self, site_ids: list[UUID], site_type_id: UUID,
    ) -> dict[str, Any]:
        normalized_ids = list(dict.fromkeys(_uuid(item) for item in site_ids))
        return await self._call(
            self._bulk_set_site_type_sync, normalized_ids, _uuid(site_type_id),
        )

    def _bulk_set_site_type_sync(
        self, site_ids: list[str], site_type_id: str,
    ) -> dict[str, Any]:
        now = _iso(datetime.now(UTC))
        with self.database.transaction(immediate=True) as conn:
            site_type = self._resolve_site_type_locked(conn, site_type_id=site_type_id)
            placeholders = ",".join("?" for _ in site_ids)
            existing = {
                str(row["id"]) for row in conn.execute(
                    f"SELECT id FROM sites WHERE status!='archived' AND id IN ({placeholders})",
                    site_ids,
                )
            }
            missing = [site_id for site_id in site_ids if site_id not in existing]
            if missing:
                raise SiteTypeConflict(
                    "One or more sites were not found", code="SITE_NOT_FOUND",
                )
            conn.execute(
                f"""UPDATE sites SET site_type_id=?,kind=?,row_version=row_version+1,updated_at=?
                    WHERE id IN ({placeholders})""",
                [site_type["id"], site_type["slug"], now, *site_ids],
            )
        return {
            "updated": len(site_ids), "site_type_id": site_type_id, "site_ids": site_ids,
        }

    # -- Sites -------------------------------------------------------
    async def list_sites(self, *, page: int, page_size: int, q: str | None = None,
                         folder_id: str | None = None, tag_id: UUID | None = None,
                         site_type_id: UUID | None = None):
        rows = await self._call(self._site_rows)
        if q:
            needle = q.casefold()
            rows = [row for row in rows if needle in row["name"].casefold() or needle in row["url"].casefold()]
        if folder_id is not None:
            expected = self.ROOT_FOLDER_ID if folder_id == "null" else folder_id
            rows = [row for row in rows if str(row.get("folder_id")) == str(expected)]
        if tag_id is not None:
            rows = [row for row in rows if any(str(tag.get("id")) == str(tag_id) for tag in row.get("tags", []))]
        if site_type_id is not None:
            rows = [row for row in rows if str(row.get("site_type_id")) == str(site_type_id)]
        total = len(rows)
        start = (page - 1) * page_size
        return rows[start:start + page_size], total

    async def get_site(self, site_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._site_row, _uuid(site_id))

    async def get_site_runtime(self, site_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._site_runtime_row, _uuid(site_id))

    async def list_site_urls_for_import(self) -> set[str]:
        return await self._call(self._list_site_urls_for_import_sync)

    def _list_site_urls_for_import_sync(self) -> set[str]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """SELECT e.url_base,e.url_suffix_secret_id FROM site_endpoints e
                   JOIN sites s ON s.id=e.site_id WHERE s.status!='archived'"""
            ).fetchall()
        result: set[str] = set()
        for row in rows:
            url = str(row["url_base"])
            if row["url_suffix_secret_id"]:
                if self.secret_service is None:
                    raise RuntimeError("encrypted URL suffix cannot be restored for duplicate detection")
                from site_hub.services.secrets import restore_sensitive_url
                url = restore_sensitive_url(url, row["url_suffix_secret_id"], self.secret_service)
            result.add(url)
        return result

    def _site_runtime_row(self, site_id: str) -> dict[str, Any] | None:
        site = self._site_row(site_id)
        if not site:
            return None
        with self.database.connect() as conn:
            endpoint = conn.execute(
                """SELECT url_base,url_suffix_secret_id FROM site_endpoints WHERE site_id=?
                   ORDER BY is_default DESC,sort_order,id LIMIT 1""", (site_id,)
            ).fetchone()
        if endpoint and endpoint["url_suffix_secret_id"]:
            if self.secret_service is None:
                raise RuntimeError("encrypted URL suffix cannot be restored")
            from site_hub.services.secrets import restore_sensitive_url
            site["url"] = restore_sensitive_url(
                endpoint["url_base"], endpoint["url_suffix_secret_id"], self.secret_service
            )
            site["origin"] = site["url"]
        return site

    def _site_rows(self) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """SELECT s.*,
                          COALESCE((SELECT e.url_base FROM site_endpoints e
                                    WHERE e.site_id=s.id ORDER BY e.is_default DESC,e.sort_order,e.id LIMIT 1),s.origin) AS public_url,
                          (SELECT e.id FROM site_endpoints e WHERE e.site_id=s.id
                           ORDER BY e.is_default DESC,e.sort_order,e.id LIMIT 1) AS default_endpoint_id,
                          (SELECT COUNT(*) FROM site_accounts a WHERE a.site_id=s.id) AS account_count,
                          (SELECT COUNT(*) FROM products p WHERE p.site_id=s.id) AS product_count
                   FROM sites s WHERE s.status!='archived' ORDER BY s.pinned DESC,s.sort_order,s.id"""
            ).fetchall()
            return [self._normalize_site(conn, dict(row)) for row in rows]

    def _site_row(self, site_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                """SELECT s.*,
                          COALESCE((SELECT e.url_base FROM site_endpoints e
                                    WHERE e.site_id=s.id ORDER BY e.is_default DESC,e.sort_order,e.id LIMIT 1),s.origin) AS public_url,
                          (SELECT e.id FROM site_endpoints e WHERE e.site_id=s.id
                           ORDER BY e.is_default DESC,e.sort_order,e.id LIMIT 1) AS default_endpoint_id,
                          (SELECT COUNT(*) FROM site_accounts a WHERE a.site_id=s.id) AS account_count,
                          (SELECT COUNT(*) FROM products p WHERE p.site_id=s.id) AS product_count
                   FROM sites s WHERE s.id=? AND s.status!='archived'""",
                (site_id,),
            ).fetchone()
            if not row:
                return None
            result = self._normalize_site(conn, dict(row))
            capability = conn.execute(
                """SELECT adapter,config_json FROM capability_instances
                   WHERE site_id=? AND account_id IS NULL AND code=? ORDER BY id LIMIT 1""",
                (site_id, self.PRODUCT_CODE),
            ).fetchone()
            config = _loads(capability["config_json"], {}) if capability else {}
            secret_fields = list(config.pop("_secret_fields", []))
            result.update({
                "adapter": capability["adapter"] if capability else None,
                "config": config,
                "configured": bool(config or secret_fields),
                "configured_secret_fields": secret_fields,
            })
            return result

    def _normalize_site(self, conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
        capabilities = [item["code"] for item in conn.execute(
            "SELECT DISTINCT code FROM capability_instances WHERE site_id=? AND enabled=1 ORDER BY code",
            (row["id"],),
        )]
        tags = [dict(item) for item in conn.execute(
            """SELECT t.id,t.name,t.color,t.icon FROM tags t JOIN site_tags st ON st.tag_id=t.id
               WHERE st.site_id=? ORDER BY t.sort_order,t.id""",
            (row["id"],),
        )]
        type_row = conn.execute(
            "SELECT * FROM site_types WHERE id=?", (row["site_type_id"],)
        ).fetchone()
        site_type = self._normalize_site_type(conn, type_row) if type_row is not None else None
        endpoint = conn.execute(
            """SELECT url_base,url_suffix_secret_id,query_keys_json
               FROM site_endpoints WHERE site_id=?
               ORDER BY is_default DESC,sort_order,id LIMIT 1""",
            (row["id"],),
        ).fetchone()
        public_url = str(row["public_url"])
        if endpoint is not None and endpoint["url_suffix_secret_id"]:
            query_keys = _loads(endpoint["query_keys_json"], [])
            if isinstance(query_keys, list) and query_keys:
                public_url = str(endpoint["url_base"]) + "?" + urlencode(
                    [(str(key), "<redacted>") for key in query_keys]
                )
            else:
                public_url = str(endpoint["url_base"]) + "#<redacted>"
        return {
            "id": row["id"], "folder_id": row.get("folder_id"), "name": row["name"],
            "url": public_url, "origin": public_url,
            "kind": row["kind"], "site_type_id": row.get("site_type_id"),
            "site_type": site_type, "capabilities": capabilities or ["bookmark"],
            "description": row["description"], "note": row["note"], "icon": row["icon"],
            "status": row["status"], "pinned": bool(row["pinned"]),
            "favicon_path": row.get("favicon_path"), "tags": tags,
            "default_endpoint_id": row.get("default_endpoint_id"),
            "checkin_account_count": int(row.get("account_count") or 0),
            "account_count": int(row.get("account_count") or 0),
            "product_count": int(row.get("product_count") or 0),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    async def create_site(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._call(self._create_site_sync, payload)

    def _create_site_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload["url"])
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        site_id = str(uuid4())
        endpoint_id = str(uuid4())
        now = _iso(datetime.now(UTC))
        requested = set(payload.get("capabilities") or ["bookmark"])
        requested.add("bookmark")
        tag_ids = list(dict.fromkeys(str(tag) for tag in payload.get("tag_ids") or []))
        with self.database.transaction(immediate=True) as conn:
            site_type = self._resolve_site_type_locked(
                conn,
                site_type_id=str(payload["site_type_id"]) if payload.get("site_type_id") else None,
                kind=str(payload["kind"]) if payload.get("kind") is not None else None,
            )
            conn.execute(
                """INSERT INTO sites(
                       id,folder_id,name,origin,canonical_host,kind,site_type_id,status,description,note,
                       icon,pinned,sort_order,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    site_id, str(payload.get("folder_id") or self.ROOT_FOLDER_ID),
                    str(payload["name"]).strip(), origin, (parts.hostname or "").lower(),
                    site_type["slug"], site_type["id"], payload.get("status", "active"),
                    payload.get("description", ""), payload.get("note", ""),
                    payload.get("icon", ""), int(bool(payload.get("pinned", False))),
                    int(payload.get("sort_order", 0)), now, now,
                ),
            )
            url_base, suffix_id, query_keys = self._protect_url(
                url, f"site:{site_id}:home", conn=conn,
            )
            conn.execute(
                """INSERT INTO site_endpoints(
                       id,site_id,role,label,url_base,url_suffix_secret_id,query_keys_json,
                       is_default,sort_order,created_at,updated_at
                   ) VALUES(?,?,'home','',?,?,?,1,0,?,?)""",
                (endpoint_id, site_id, url_base, suffix_id, _dumps(list(query_keys)), now, now),
            )
            for code in sorted(str(item) for item in requested):
                if code == self.CHECKIN_CODE:
                    continue
                if code == self.PRODUCT_CODE:
                    self._ensure_product_capability_locked(
                        conn, site_id, enabled=True, default_enabled=True,
                    )
                else:
                    conn.execute(
                        """INSERT INTO capability_instances(
                               id,site_id,code,account_id,endpoint_id,adapter,config_version,
                               config_json,enabled,created_at,updated_at
                           ) VALUES(?,?,?,NULL,NULL,'none',1,'{}',1,?,?)""",
                        (str(uuid4()), site_id, code, now, now),
                    )
            if tag_ids:
                conn.executemany(
                    "INSERT INTO site_tags(site_id,tag_id) VALUES(?,?)",
                    ((site_id, tag_id) for tag_id in tag_ids),
                )
        return self._site_row(site_id)  # type: ignore[return-value]

    async def update_site(self, site_id: UUID, payload: dict[str, Any]) -> dict[str, Any] | None:
        return await self._call(self._update_site_sync, _uuid(site_id), payload)

    def _update_site_sync(self, site_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        values = dict(payload)
        tag_ids = values.pop("tag_ids", None)
        capabilities = values.pop("capabilities", None)
        adapter = values.pop("adapter", None)
        config = values.pop("config", None)
        clear_secrets = set(values.pop("clear_secrets", []) or [])
        allowed = {
            "folder_id", "name", "origin", "canonical_host", "kind", "site_type_id", "status",
            "description", "note", "icon", "favicon_path", "pinned", "sort_order",
            "visit_count", "last_visited_at",
        }
        if "folder_id" in values and values["folder_id"] is None:
            values["folder_id"] = self.ROOT_FOLDER_ID
        url: str | None = None
        if "url" in values:
            url = str(values.pop("url"))
            parts = urlsplit(url)
            values.update(origin=f"{parts.scheme}://{parts.netloc}", canonical_host=parts.hostname or "")
        unsupported = set(values) - allowed
        if unsupported:
            raise ValueError(f"unsupported site field: {sorted(unsupported)[0]}")

        requested = ({str(item) for item in capabilities} | {"bookmark"}) if capabilities is not None else None
        normalized_tag_ids = (
            list(dict.fromkeys(str(tag) for tag in tag_ids)) if tag_ids is not None else None
        )
        obsolete_secret_ids: set[str] = set()
        now = _iso(datetime.now(UTC))
        with self.database.transaction(immediate=True) as conn:
            current_site = conn.execute(
                "SELECT site_type_id FROM sites WHERE id=?", (site_id,)
            ).fetchone()
            if not current_site:
                return None

            if "site_type_id" in values or "kind" in values:
                if "site_type_id" in values and values["site_type_id"] is None:
                    raise SiteTypeConflict(
                        "A site must have a primary type", code="SITE_TYPE_REQUIRED",
                    )
                if "kind" in values and not str(values["kind"] or "").strip():
                    raise SiteTypeConflict(
                        "A site must have a primary type", code="SITE_TYPE_REQUIRED",
                    )
                site_type = self._resolve_site_type_locked(
                    conn,
                    site_type_id=(
                        str(values["site_type_id"]) if "site_type_id" in values else None
                    ),
                    kind=str(values["kind"]) if "kind" in values else None,
                    require_enabled=False,
                )
                if (
                    not bool(site_type["enabled"])
                    and str(site_type["id"]) != str(current_site["site_type_id"])
                ):
                    raise SiteTypeConflict("Site type is disabled", code="SITE_TYPE_DISABLED")
                values["site_type_id"] = site_type["id"]
                values["kind"] = site_type["slug"]

            if url is not None:
                endpoint = conn.execute(
                    """SELECT id,url_suffix_secret_id FROM site_endpoints WHERE site_id=?
                       ORDER BY is_default DESC,sort_order,id LIMIT 1""",
                    (site_id,),
                ).fetchone()
                url_base, suffix_id, query_keys = self._protect_url(
                    url, f"site:{site_id}:home", conn=conn,
                )
                if endpoint:
                    conn.execute(
                        """UPDATE site_endpoints SET url_base=?,url_suffix_secret_id=?,query_keys_json=?,updated_at=? WHERE id=?""",
                        (url_base, suffix_id, _dumps(list(query_keys)), now, endpoint["id"]),
                    )
                    old_secret = endpoint["url_suffix_secret_id"]
                    if old_secret and old_secret != suffix_id:
                        obsolete_secret_ids.add(str(old_secret))
                else:
                    conn.execute(
                        """INSERT INTO site_endpoints(
                               id,site_id,role,label,url_base,url_suffix_secret_id,query_keys_json,
                               is_default,sort_order,created_at,updated_at
                           ) VALUES(?,?,'home','',?,?,?,1,0,?,?)""",
                        (
                            str(uuid4()), site_id, url_base, suffix_id,
                            _dumps(list(query_keys)), now, now,
                        ),
                    )

            if values:
                fields: list[str] = []
                field_values: list[Any] = []
                for key, value in values.items():
                    if key == "pinned":
                        value = int(bool(value))
                    elif key == "canonical_host":
                        value = str(value).lower()
                    fields.append(f"{key}=?")
                    field_values.append(value)
                fields.extend(["row_version=row_version+1", "updated_at=?"])
                conn.execute(
                    f"UPDATE sites SET {','.join(fields)} WHERE id=?",
                    [*field_values, now, site_id],
                )

            if normalized_tag_ids is not None:
                conn.execute("DELETE FROM site_tags WHERE site_id=?", (site_id,))
                conn.executemany(
                    "INSERT INTO site_tags(site_id,tag_id) VALUES(?,?)",
                    ((site_id, tag_id) for tag_id in normalized_tag_ids),
                )

            if requested is not None:
                existing = {
                    row["code"]: row for row in conn.execute(
                        """SELECT * FROM capability_instances
                           WHERE site_id=? AND account_id IS NULL""",
                        (site_id,),
                    )
                }
                for code in requested - existing.keys():
                    if code == self.CHECKIN_CODE:
                        continue
                    if code == self.PRODUCT_CODE:
                        self._ensure_product_capability_locked(
                            conn, site_id, enabled=True, default_enabled=True,
                        )
                    else:
                        conn.execute(
                            """INSERT INTO capability_instances(
                                   id,site_id,code,account_id,endpoint_id,adapter,config_version,
                                   config_json,enabled,created_at,updated_at
                               ) VALUES(?,?,?,NULL,NULL,'none',1,'{}',1,?,?)""",
                            (str(uuid4()), site_id, code, now, now),
                        )
                for code, capability in existing.items():
                    if code == self.CHECKIN_CODE:
                        continue
                    if code == self.PRODUCT_CODE:
                        self._ensure_product_capability_locked(
                            conn, site_id, enabled=code in requested,
                        )
                    else:
                        conn.execute(
                            """UPDATE capability_instances SET enabled=?,updated_at=?
                               WHERE id=?""",
                            (int(code in requested), now, capability["id"]),
                        )

            if adapter is not None or config is not None or clear_secrets:
                product = self._ensure_product_capability_locked(
                    conn, site_id,
                    default_enabled=(requested is None or self.PRODUCT_CODE in requested),
                )
                changes: dict[str, Any] = {}
                if adapter is not None:
                    changes["adapter"] = adapter
                self._update_capability_atomic(
                    product["id"], owner="capability", account_id=None,
                    changes=changes, config=config, config_supplied=config is not None,
                    clear_secrets=clear_secrets, conn=conn,
                    obsolete_secret_ids=obsolete_secret_ids,
                )

        if self.secret_service:
            for secret_id in obsolete_secret_ids:
                self.secret_service.delete_if_unreferenced(secret_id)
        return self._site_row(site_id)

    async def delete_site(self, site_id: UUID) -> bool:
        return await self._call(self._archive_site_sync, _uuid(site_id))

    def _refresh_batch_state_locked(
        self, conn: sqlite3.Connection, batch_id: str | None, now: str,
    ) -> None:
        if not batch_id:
            return
        summary = conn.execute(
            """SELECT b.status,b.started_at,b.completed_at,COUNT(j.id) item_count,
                      SUM(CASE WHEN j.status='queued' THEN 1 ELSE 0 END) queued_count,
                      SUM(CASE WHEN j.status IN ('leased','running') THEN 1 ELSE 0 END) active_count,
                      MIN(j.started_at) first_started_at,MAX(j.finished_at) last_finished_at
               FROM job_batches b LEFT JOIN jobs j ON j.batch_id=b.id
               WHERE b.id=? GROUP BY b.id""",
            (batch_id,),
        ).fetchone()
        if summary is None or int(summary["item_count"] or 0) == 0:
            return
        if summary["status"] in {"expired", "cancelled"}:
            return
        queued = int(summary["queued_count"] or 0)
        active = int(summary["active_count"] or 0)
        started_at = summary["started_at"] or summary["first_started_at"]
        if active or queued:
            # A batch becomes running as soon as its first job is leased.  A
            # queued retry keeps the already-started batch open.
            if active or started_at:
                status = "running"
                started_at = started_at or now
            else:
                status = "pending"
            completed_at = None
        else:
            status = "completed"
            started_at = started_at or now
            completed_at = (
                summary["completed_at"] or summary["last_finished_at"] or now
            )
        if (
            summary["status"] == status
            and summary["started_at"] == started_at
            and summary["completed_at"] == completed_at
        ):
            return
        conn.execute(
            """UPDATE job_batches
               SET status=?,started_at=?,completed_at=?,updated_at=? WHERE id=?""",
            (status, started_at, completed_at, now, batch_id),
        )

    def _repair_batch_states_locked(
        self, conn: sqlite3.Connection, now: str,
    ) -> None:
        """Repair batch summaries after an interrupted or older application run."""
        batches = conn.execute(
            """SELECT b.id,b.planned_at,b.trigger_reason,b.status,b.started_at,
                      COUNT(DISTINCT j.planned_at) distinct_job_plans,
                      MIN(j.planned_at) only_job_plan,
                      SUM(CASE WHEN j.status IN ('queued','leased','running') THEN 1 ELSE 0 END) active_count
               FROM job_batches b JOIN jobs j ON j.batch_id=b.id
               GROUP BY b.id"""
        ).fetchall()
        for batch in batches:
            if batch["status"] in {"expired", "cancelled"}:
                continue
            # Older builds could apply misfire recovery after every job had
            # already finished.  In that exact case no job received the new
            # timestamp, so the original one-shot plan is unambiguous.
            if (
                batch["trigger_reason"] == "regenerated"
                and batch["status"] == "pending"
                and batch["started_at"] is None
                and int(batch["active_count"] or 0) == 0
                and int(batch["distinct_job_plans"] or 0) == 1
                and batch["only_job_plan"]
                and batch["only_job_plan"] != batch["planned_at"]
            ):
                conn.execute(
                    """UPDATE job_batches
                       SET planned_at=?,trigger_reason='normal',updated_at=? WHERE id=?""",
                    (batch["only_job_plan"], now, batch["id"]),
                )
            self._refresh_batch_state_locked(conn, str(batch["id"]), now)

    def _cancel_queued_checkins_locked(
        self, conn: sqlite3.Connection, now: str, *, site_id: str | None = None,
        account_id: str | None = None,
    ) -> None:
        if (site_id is None) == (account_id is None):
            raise ValueError("exactly one queued check-in owner is required")
        column, owner_id = ("site_id", site_id) if site_id is not None else ("account_id", account_id)
        batch_ids = [
            str(row["batch_id"])
            for row in conn.execute(
                f"""SELECT DISTINCT batch_id FROM jobs
                    WHERE kind='checkin' AND status='queued' AND {column}=?
                      AND batch_id IS NOT NULL""",
                (owner_id,),
            )
        ]
        conn.execute(
            f"""UPDATE jobs SET status='cancelled',lease_owner=NULL,lease_token=NULL,lease_until=NULL,
                       result_json=?,last_error=NULL,finished_at=?,updated_at=?
                WHERE kind='checkin' AND status='queued' AND {column}=?""",
            (_dumps({"reason_code": "disabled", "cancelled_before_start": True}),
             now, now, owner_id),
        )
        for batch_id in batch_ids:
            self._refresh_batch_state_locked(conn, batch_id, now)

    @staticmethod
    def _backfill_enabled_browser_account_locked(
        conn: sqlite3.Connection, account_id: str, now_utc: datetime,
    ) -> bool:
        """Attach a newly enabled default browser account to today's pending batch.

        Account/capability/schedule activation and this insert share the caller's
        ``BEGIN IMMEDIATE`` transaction.  The daily idempotency key remains the
        final guard if batch creation or another PATCH races this operation.
        """
        eligible = conn.execute(
            """SELECT c.id capability_id,c.site_id
               FROM site_accounts a
               JOIN capability_instances c
                 ON c.account_id=a.id AND c.code='checkin'
               JOIN schedules s ON s.capability_instance_id=c.id
               WHERE a.id=? AND a.enabled=1 AND a.auth_state='ready'
                 AND c.enabled=1 AND lower(coalesce(c.adapter,'')) NOT IN ('api','cookie')
                 AND s.enabled=1 AND s.timezone='Asia/Shanghai'
                 AND s.policy_type='daily_random_window'
                 AND json_extract(s.policy_json,'$.mode') IS NULL
               ORDER BY s.id LIMIT 1""",
            (account_id,),
        ).fetchone()
        if eligible is None:
            return False

        timezone = ZoneInfo("Asia/Shanghai")
        local = now_utc.astimezone(timezone).replace(second=0, microsecond=0)
        date_key = local.date().isoformat()
        idempotency_key = f"checkin:{account_id}:{date_key}"
        if conn.execute(
            "SELECT 1 FROM jobs WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone():
            return False

        batch = conn.execute(
            """SELECT * FROM job_batches
               WHERE business_date=? AND timezone='Asia/Shanghai'
                 AND status='pending' AND schedule_id IS NULL
                 AND trigger_reason IN ('normal','regenerated')
               ORDER BY created_at DESC,id DESC LIMIT 1""",
            (date_key,),
        ).fetchone()
        if batch is None or batch["planned_at"] is None:
            return False
        try:
            planned = datetime.fromisoformat(
                str(batch["planned_at"]).replace("Z", "+00:00")
            )
        except ValueError:
            return False
        if planned.tzinfo is None:
            planned = planned.replace(tzinfo=UTC)
        effective = planned.astimezone(UTC)

        # Match ScheduleCoordinator's one-shot misfire rule before exposing the
        # new due job to the faster worker polling loop.
        local_now_utc = local.astimezone(UTC)
        if (
            batch["trigger_reason"] == "normal"
            and local_now_utc > effective + timedelta(minutes=5)
        ):
            latest = datetime.combine(local.date(), time(23, 0), timezone)
            recovery = min(local + timedelta(hours=1), latest)
            if recovery > local:
                effective = recovery.astimezone(UTC)
                effective_text = _iso(effective)
                conn.execute(
                    """UPDATE job_batches
                       SET planned_at=?,trigger_reason='regenerated',updated_at=?
                       WHERE id=? AND status='pending' AND trigger_reason='normal'""",
                    (effective_text, _iso(now_utc), batch["id"]),
                )
                conn.execute(
                    """UPDATE jobs SET planned_at=?,available_at=?,updated_at=?
                       WHERE batch_id=? AND status='queued'""",
                    (effective_text, effective_text, _iso(now_utc), batch["id"]),
                )

        effective_text = _iso(effective)
        now_text = _iso(now_utc)
        try:
            conn.execute(
                """INSERT INTO jobs(
                       id,batch_id,kind,site_id,account_id,capability_instance_id,
                       business_date,planned_at,available_at,priority,status,max_attempts,
                       idempotency_key,payload_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid4()), batch["id"], "checkin", eligible["site_id"],
                    account_id, eligible["capability_id"], date_key,
                    effective_text, effective_text, 50, "queued", 3,
                    idempotency_key, _dumps({"batch_id": batch["id"]}),
                    now_text, now_text,
                ),
            )
        except sqlite3.IntegrityError:
            if not conn.execute(
                "SELECT 1 FROM jobs WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone():
                raise
            return False
        return True

    def _archive_site_sync(self, site_id: str) -> bool:
        now = _iso(datetime.now(UTC))
        with self.database.transaction(immediate=True) as conn:
            changed = conn.execute(
                "UPDATE sites SET status='archived',updated_at=?,row_version=row_version+1 WHERE id=? AND status!='archived'",
                (now, site_id),
            ).rowcount
            if not changed: return False
            conn.execute("UPDATE site_accounts SET enabled=0,auth_state='disabled',updated_at=? WHERE site_id=?", (now, site_id))
            conn.execute("UPDATE capability_instances SET enabled=0,updated_at=? WHERE site_id=?", (now, site_id))
            conn.execute(
                """UPDATE schedules SET enabled=0,updated_at=? WHERE capability_instance_id IN
                   (SELECT id FROM capability_instances WHERE site_id=?)""", (now, site_id)
            )
            self._cancel_queued_checkins_locked(conn, now, site_id=site_id)
            return True

    async def get_endpoint_runtime(self, endpoint_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._endpoint_runtime_sync, _uuid(endpoint_id))

    def _endpoint_runtime_sync(self, endpoint_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT id,site_id,role,label,url_base,url_suffix_secret_id FROM site_endpoints WHERE id=?",
                (endpoint_id,),
            ).fetchone()
        if not row:
            return None
        result = dict(row); result["url"] = result.pop("url_base")
        secret_id = result.pop("url_suffix_secret_id")
        if secret_id:
            if self.secret_service is None: raise RuntimeError("encrypted endpoint cannot be opened")
            from site_hub.services.secrets import restore_sensitive_url
            result["url"] = restore_sensitive_url(result["url"], secret_id, self.secret_service)
        return result

    # -- Accounts ----------------------------------------------------
    async def list_accounts(self, site_id: UUID) -> list[dict[str, Any]]:
        return await self._call(self._account_rows, _uuid(site_id))

    async def get_account(self, account_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._account_row, _uuid(account_id), True)

    def _account_rows(self, site_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """SELECT a.*,c.id capability_id,c.adapter,c.config_json,
                           c.enabled capability_enabled,
                           s.id schedule_id,s.policy_type,s.policy_json,
                           s.enabled schedule_enabled,
                          bp.relative_path profile_path,bp.state profile_state,
                          bp.manifest_sha256 profile_manifest_sha256,
                          bp.file_count profile_file_count,bp.total_bytes profile_total_bytes,
                          bp.last_verified_at profile_last_verified_at,
                          bp.verification_error profile_verification_error
                   FROM site_accounts a
                   LEFT JOIN capability_instances c ON c.account_id=a.id AND c.code='checkin'
                   LEFT JOIN schedules s ON s.capability_instance_id=c.id
                   LEFT JOIN browser_profiles bp ON bp.account_id=a.id
                   WHERE a.site_id=? AND a.auth_state!='disabled' ORDER BY a.sort_order,a.id""",
                (site_id,),
            ).fetchall()
            return [self._normalize_account(dict(row), runtime=False) for row in rows]

    def _account_row(self, account_id: str, runtime: bool = False) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                """SELECT a.*,c.id capability_id,c.adapter,c.config_json,
                           c.enabled capability_enabled,
                           s.id schedule_id,s.policy_type,s.policy_json,
                           s.enabled schedule_enabled,
                          bp.relative_path profile_path,bp.state profile_state,
                          bp.manifest_sha256 profile_manifest_sha256,
                          bp.file_count profile_file_count,bp.total_bytes profile_total_bytes,
                          bp.last_verified_at profile_last_verified_at,
                          bp.verification_error profile_verification_error
                   FROM site_accounts a
                   LEFT JOIN capability_instances c ON c.account_id=a.id AND c.code='checkin'
                   LEFT JOIN schedules s ON s.capability_instance_id=c.id
                   LEFT JOIN browser_profiles bp ON bp.account_id=a.id
                   WHERE a.id=? AND a.auth_state!='disabled' ORDER BY s.id LIMIT 1""",
                (account_id,),
            ).fetchone()
            return self._normalize_account(dict(row), runtime=runtime) if row else None

    def _normalize_account(self, row: dict[str, Any], *, runtime: bool) -> dict[str, Any]:
        config = _loads(row.get("config_json"), {})
        secret_fields = list(config.pop("_secret_fields", []))
        policy = _loads(row.get("policy_json"), {})
        override = None
        if row.get("policy_type") == "cron" and policy.get("mode") == "fixed_time":
            override = {"mode": "fixed_time", "time": policy.get("time"), "timezone": policy.get("timezone", "Asia/Shanghai")}
        elif row.get("policy_type") == "daily_random_window" and policy.get("mode") == "custom_window":
            override = {
                "mode": "custom_window", "start": policy.get("start"),
                "end": policy.get("end"), "timezone": policy.get("timezone", "Asia/Shanghai"),
            }
        result = {
            "id": row["id"], "site_id": row["site_id"], "name": row["label"],
            "label": row["label"],
            "adapter": row.get("adapter") or "playwright",
            "enabled": (
                bool(row["enabled"])
                and bool(row.get("capability_enabled"))
                and bool(row.get("schedule_enabled"))
            ),
            "schedule_override": override, "profile_key": row.get("profile_path") or row["id"],
            "config": config,
            "configured": bool(config or secret_fields), "configured_secret_fields": secret_fields,
            "login_state": row.get("auth_state") or "unverified",
            "auth_state": row.get("auth_state") or "unverified",
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "profile": {
                "relative_path": row.get("profile_path") or row["id"],
                "state": row.get("profile_state") or "unverified",
                "manifest_sha256": row.get("profile_manifest_sha256"),
                "file_count": row.get("profile_file_count"),
                "total_bytes": row.get("profile_total_bytes"),
                "last_verified_at": row.get("profile_last_verified_at"),
                "verification_error": row.get("profile_verification_error"),
            },
        }
        if runtime:
            result["runtime_config"] = self._restore_secrets(row.get("capability_id"), config, secret_fields)
        result["schedule"] = override
        return result

    async def create_account(self, site_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._call(self._create_account_sync, _uuid(site_id), payload)

    def _create_account_sync(self, site_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = str(uuid4())
        capability_id = str(uuid4())
        profile_id = str(uuid4())
        schedule_id = str(uuid4())
        adapter = str(payload.get("adapter", "playwright"))
        # Browser-backed accounts cannot be safely scheduled until a live
        # Profile verification succeeds. API/cookie adapters do not depend on
        # the browser Profile and retain their explicit enabled choice.
        enabled = bool(payload.get("enabled", False)) and adapter.lower() in {"api", "cookie"}
        clean, secrets = self._split_secrets(payload.get("config") or {})
        clean["_secret_fields"] = sorted(secrets)
        override = payload.get("schedule_override")
        policy_type, policy = self._schedule_policy(override)
        now = _iso(datetime.now(UTC))
        with self.database.transaction(immediate=True) as conn:
            if not conn.execute("SELECT 1 FROM sites WHERE id=?", (site_id,)).fetchone():
                raise LookupError("site not found")
            conn.execute(
                """INSERT INTO site_accounts(
                       id,site_id,label,enabled,auth_state,sort_order,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (account_id, site_id, payload["name"], int(enabled), "unverified", 0, now, now),
            )
            conn.execute(
                """INSERT INTO capability_instances(
                       id,site_id,code,account_id,adapter,config_version,config_json,
                       enabled,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    capability_id, site_id, self.CHECKIN_CODE, account_id,
                    adapter, 1, _dumps(clean),
                    int(enabled), now, now,
                ),
            )
            self._store_secrets(
                "account", account_id, capability_id, secrets, conn=conn,
            )
            conn.execute(
                """INSERT INTO browser_profiles(
                       id,account_id,relative_path,state,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?)""",
                (profile_id, account_id, account_id, "unverified", now, now),
            )
            conn.execute(
                """INSERT INTO schedules(
                       id,capability_instance_id,timezone,policy_type,policy_json,enabled,
                       misfire_policy,max_attempts,backoff_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    schedule_id, capability_id, "Asia/Shanghai", policy_type,
                    _dumps(policy), int(enabled), "recover", 3, "{}", now, now,
                ),
            )
        return self._account_row(account_id)  # type: ignore[return-value]

    async def update_account(self, account_id: UUID, payload: dict[str, Any]) -> dict[str, Any] | None:
        return await self._call(self._update_account_sync, _uuid(account_id), payload)

    def _update_account_sync(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self.database.transaction(immediate=True) as conn:
            account = conn.execute(
                "SELECT id,enabled,auth_state FROM site_accounts WHERE id=? AND auth_state!='disabled'", (account_id,)
            ).fetchone()
            if not account:
                return None
            capability = conn.execute(
                "SELECT * FROM capability_instances WHERE account_id=? AND code='checkin'", (account_id,)
            ).fetchone()
            schedule = conn.execute(
                "SELECT * FROM schedules WHERE capability_instance_id=? ORDER BY id LIMIT 1", (capability["id"],)
            ).fetchone() if capability else None
            if not capability:
                raise RuntimeError("account has no check-in capability")
            next_adapter = str(payload.get("adapter", capability["adapter"]) or "").lower()
            currently_enabled = (
                bool(account["enabled"])
                and bool(capability["enabled"])
                and bool(schedule and schedule["enabled"])
            )
            next_enabled = bool(payload["enabled"]) if "enabled" in payload else currently_enabled
            activation_requested = payload.get("enabled") is True or (
                "adapter" in payload and next_enabled
            )
            if (
                activation_requested
                and next_adapter not in {"api", "cookie"}
                and account["auth_state"] != "ready"
            ):
                raise AccountNotReady("Verify the browser Profile before enabling this account")
            now_value = datetime.now(UTC)
            now = _iso(now_value)
            account_fields: list[str] = []
            account_values: list[Any] = []
            if "name" in payload:
                account_fields.append("label=?"); account_values.append(payload["name"])
            if "enabled" in payload:
                account_fields.append("enabled=?"); account_values.append(int(bool(payload["enabled"])))
            if account_fields:
                account_values.extend([now, account_id])
                conn.execute(
                    f"UPDATE site_accounts SET {','.join(account_fields)},updated_at=? WHERE id=?",
                    account_values,
                )
            cap_changes: dict[str, Any] = {}
            if "adapter" in payload:
                cap_changes["adapter"] = str(payload["adapter"])
            if "enabled" in payload:
                cap_changes["enabled"] = payload["enabled"]
            self._update_capability_atomic(
                capability["id"], owner="account", account_id=account_id,
                changes=cap_changes, config=payload.get("config"),
                config_supplied=payload.get("config") is not None,
                clear_secrets=set(payload.get("clear_secrets") or []), conn=conn,
            )
            if schedule:
                schedule_fields: list[str] = []
                schedule_values: list[Any] = []
                if payload.get("clear_schedule_override"):
                    schedule_fields.extend(["policy_type=?", "policy_json=?"])
                    schedule_values.extend(["daily_random_window", _dumps({})])
                elif payload.get("schedule_override") is not None:
                    policy_type, policy = self._schedule_policy(payload["schedule_override"])
                    schedule_fields.extend(["policy_type=?", "policy_json=?"])
                    schedule_values.extend([policy_type, _dumps(policy)])
                if "enabled" in payload:
                    schedule_fields.append("enabled=?"); schedule_values.append(int(bool(payload["enabled"])))
                if schedule_fields:
                    schedule_values.extend([now, schedule["id"]])
                    conn.execute(
                        f"UPDATE schedules SET {','.join(schedule_fields)},updated_at=? WHERE id=?",
                        schedule_values,
                    )
            if payload.get("enabled") is True and next_adapter not in {"api", "cookie"}:
                self._backfill_enabled_browser_account_locked(
                    conn, account_id, now_value,
                )
            if "enabled" in payload and not bool(payload["enabled"]):
                self._cancel_queued_checkins_locked(conn, now, account_id=account_id)
        return self._account_row(account_id)

    async def delete_account(self, account_id: UUID) -> bool:
        return await self._call(self._archive_account_sync, _uuid(account_id))

    def _archive_account_sync(self, account_id: str) -> bool:
        now = _iso(datetime.now(UTC))
        with self.database.transaction(immediate=True) as conn:
            changed = conn.execute(
                "UPDATE site_accounts SET enabled=0,auth_state='disabled',updated_at=? WHERE id=? AND auth_state!='disabled'",
                (now, account_id),
            ).rowcount
            if not changed: return False
            conn.execute("UPDATE capability_instances SET enabled=0,updated_at=? WHERE account_id=?", (now, account_id))
            conn.execute(
                """UPDATE schedules SET enabled=0,updated_at=? WHERE capability_instance_id IN
                   (SELECT id FROM capability_instances WHERE account_id=?)""", (now, account_id)
            )
            self._cancel_queued_checkins_locked(conn, now, account_id=account_id)
            return True

    async def set_account_auth_state(self, account_id: UUID, state: str,
                                     error: str | None = None) -> dict[str, Any] | None:
        allowed = {"ready", "expired", "error", "unverified", "disabled"}
        normalized = state if state in allowed else "unverified"
        return await self._call(
            self.repository.update_site_account, _uuid(account_id), auth_state=normalized,
            verification_error=error,
        )

    async def set_account_profile_verification(
        self, account_id: UUID, state: str, error: str | None = None,
        verified_at: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically persist a read-only Profile verification result.

        ``busy`` is deliberately Profile-only: transient worker activity must
        not be squeezed into the terminal ``site_accounts.auth_state`` enum.
        """
        return await self._call(
            self._set_account_profile_verification_sync,
            _uuid(account_id), state, error, verified_at,
        )

    def _set_account_profile_verification_sync(
        self, account_id: str, state: str, error: str | None,
        verified_at: str | None,
    ) -> dict[str, Any] | None:
        normalized = str(state or "error").lower()
        if normalized not in {"ready", "expired", "error", "busy", "unverified"}:
            normalized = "error"
        safe_error = sanitize_log_value(error, max_length=500) if error else None
        now = _iso(datetime.now(UTC))
        success_at = _iso(verified_at) if normalized == "ready" and verified_at else now

        with self.database.transaction(immediate=True) as conn:
            account = conn.execute(
                "SELECT auth_state FROM site_accounts WHERE id=? AND auth_state!='disabled'",
                (account_id,),
            ).fetchone()
            if not account:
                return None

            if normalized == "busy":
                conn.execute(
                    """UPDATE browser_profiles
                       SET state='busy',verification_error=?,updated_at=?
                       WHERE account_id=? AND state!='archived'""",
                    (safe_error, now, account_id),
                )
            else:
                account_state = normalized if normalized in {"ready", "expired", "error"} else "unverified"
                profile_state = "ready" if normalized == "ready" else (
                    "unverified" if normalized == "unverified" else "error"
                )
                if normalized == "ready":
                    conn.execute(
                        """UPDATE site_accounts
                           SET auth_state='ready',last_verified_at=?,verification_error=NULL,updated_at=?
                           WHERE id=? AND auth_state!='disabled'""",
                        (success_at, now, account_id),
                    )
                    conn.execute(
                        """UPDATE browser_profiles
                           SET state='ready',last_verified_at=?,verification_error=NULL,updated_at=?
                           WHERE account_id=? AND state!='archived'""",
                        (success_at, now, account_id),
                    )
                else:
                    conn.execute(
                        """UPDATE site_accounts
                           SET enabled=0,auth_state=?,verification_error=?,updated_at=?
                           WHERE id=? AND auth_state!='disabled'""",
                        (account_state, safe_error, now, account_id),
                    )
                    conn.execute(
                        """UPDATE browser_profiles
                           SET state=?,verification_error=?,updated_at=?
                           WHERE account_id=? AND state!='archived'""",
                        (profile_state, safe_error, now, account_id),
                    )
                    conn.execute(
                        "UPDATE capability_instances SET enabled=0,updated_at=? WHERE account_id=? AND code='checkin'",
                        (now, account_id),
                    )
                    conn.execute(
                        """UPDATE schedules SET enabled=0,updated_at=? WHERE capability_instance_id IN
                           (SELECT id FROM capability_instances WHERE account_id=? AND code='checkin')""",
                        (now, account_id),
                    )
                    self._cancel_queued_checkins_locked(conn, now, account_id=account_id)
        return self._account_row(account_id)

    # -- Product monitoring ------------------------------------------
    def _ensure_product_capability_sync(
        self,
        site_id: str,
        *,
        enabled: bool | None = None,
        default_enabled: bool = False,
        adapter: str = "none",
    ) -> dict[str, Any]:
        """Ensure a site-level product capability has exactly the runnable pieces.

        This is intentionally a write-only helper. Read endpoints never call it
        unless their caller explicitly requested creation. An explicit ``enabled``
        value synchronizes the capability and its schedule; ``None`` preserves an
        existing paused schedule (notably during legacy cutover rehearsals).
        """
        with self.database.transaction(immediate=True) as conn:
            return self._ensure_product_capability_locked(
                conn, site_id, enabled=enabled, default_enabled=default_enabled,
                adapter=adapter,
            )

    def _ensure_product_capability_locked(
        self,
        conn: sqlite3.Connection,
        site_id: str,
        *,
        enabled: bool | None = None,
        default_enabled: bool = False,
        adapter: str = "none",
    ) -> dict[str, Any]:
        """Conn-aware product capability/schedule upsert for a caller-owned transaction."""
        now = _iso(datetime.now(UTC))
        capability = conn.execute(
            """SELECT * FROM capability_instances
               WHERE site_id=? AND code=? AND account_id IS NULL""",
            (site_id, self.PRODUCT_CODE),
        ).fetchone()
        if capability is None:
            capability_id = str(uuid4())
            effective_enabled = bool(default_enabled if enabled is None else enabled)
            conn.execute(
                """INSERT INTO capability_instances(
                       id,site_id,code,account_id,endpoint_id,adapter,config_version,
                       config_json,enabled,created_at,updated_at
                   ) VALUES(?,?,?,NULL,NULL,?,1,'{}',?,?,?)""",
                (
                    capability_id, site_id, self.PRODUCT_CODE, adapter,
                    int(effective_enabled), now, now,
                ),
            )
        else:
            capability_id = str(capability["id"])
            effective_enabled = bool(capability["enabled"])
            if enabled is not None:
                effective_enabled = bool(enabled)
                conn.execute(
                    "UPDATE capability_instances SET enabled=?,updated_at=? WHERE id=?",
                    (int(effective_enabled), now, capability_id),
                )

        schedule = conn.execute(
            "SELECT * FROM schedules WHERE capability_instance_id=? ORDER BY id LIMIT 1",
            (capability_id,),
        ).fetchone()
        if schedule is None:
            setting = conn.execute(
                "SELECT value_json FROM settings WHERE key='scheduler.product_interval_minutes'"
            ).fetchone()
            try:
                interval = int(_loads(setting["value_json"], 60)) if setting else 60
            except (TypeError, ValueError):
                interval = 60
            conn.execute(
                """INSERT INTO schedules(
                       id,capability_instance_id,timezone,policy_type,policy_json,enabled,
                       next_run_at,misfire_policy,max_attempts,backoff_json,created_at,updated_at
                   ) VALUES(?,?,'Asia/Shanghai','interval',?,?,NULL,'recover',1,'{}',?,?)""",
                (
                    str(uuid4()), capability_id,
                    _dumps({"minutes": interval, "override": False}),
                    int(effective_enabled), now, now,
                ),
            )
        elif enabled is not None:
            conn.execute(
                "UPDATE schedules SET enabled=?,updated_at=? WHERE capability_instance_id=?",
                (int(bool(enabled)), now, capability_id),
            )
        current = conn.execute(
            "SELECT * FROM capability_instances WHERE id=?", (capability_id,),
        ).fetchone()
        assert current is not None
        return dict(current)

    async def get_product_settings(self, site_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._product_settings, _uuid(site_id), False)

    def _product_settings(self, site_id: str, create_default: bool = False) -> dict[str, Any] | None:
        site = self.repository.get_site(site_id)
        if not site:
            return None
        capability = self.repository.find_capability(site_id=site_id, code=self.PRODUCT_CODE)
        if not capability and not create_default:
            global_interval = int(self.repository.get_setting("scheduler.product_interval_minutes", "60"))
            return {
                "site_id": site_id, "enabled": False, "interval_override_minutes": None,
                "interval_minutes": global_interval, "effective_interval_minutes": global_interval,
                "scraper_type": "none", "configured": False, "configured_secret_fields": [],
                "config": {},
            }
        if create_default:
            capability = self._ensure_product_capability_sync(
                site_id, default_enabled=False,
            )
        assert capability is not None
        with self.database.connect() as conn:
            schedule = conn.execute(
                "SELECT * FROM schedules WHERE capability_instance_id=? ORDER BY id LIMIT 1", (capability["id"],)
            ).fetchone()
        config = _loads(capability.get("config_json"), {})
        secret_fields = list(config.pop("_secret_fields", []))
        policy = _loads(schedule["policy_json"], {}) if schedule else {}
        override = policy.get("minutes") if policy.get("override") else None
        global_interval = int(self.repository.get_setting("scheduler.product_interval_minutes", "60"))
        return {
            "site_id": site_id,
            "enabled": bool(capability["enabled"]) and bool(schedule and schedule["enabled"]),
            "interval_override_minutes": override,
            "interval_minutes": int(override or global_interval),
            "effective_interval_minutes": int(override or global_interval),
            "scraper_type": capability.get("adapter") or "none",
            "configured": bool(config or secret_fields), "configured_secret_fields": secret_fields,
            "config": config,
        }

    async def update_product_settings(self, site_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._call(self._update_product_settings_sync, _uuid(site_id), payload)

    async def get_product_runtime(self, site_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._product_runtime_sync, _uuid(site_id))

    def _product_runtime_sync(self, site_id: str) -> dict[str, Any] | None:
        settings = self._product_settings(site_id, False)
        if not settings:
            return None
        capability = self.repository.find_capability(site_id=site_id, code=self.PRODUCT_CODE)
        if not capability:
            return None
        config = _loads(capability.get("config_json"), {})
        names = list(config.pop("_secret_fields", []))
        settings["runtime_config"] = self._restore_secrets(capability["id"], config, names)
        return settings

    async def save_scrape_result(
        self, site_id: UUID, products: list[Any], *, job_id: UUID, lease_token: str,
    ) -> dict[str, int]:
        return await self._call(
            self._save_scrape_result_sync,
            _uuid(site_id), products, _uuid(job_id), lease_token,
        )

    def _save_scrape_result_sync(
        self, site_id: str, products: list[Any], job_id: str, lease_token: str,
    ) -> dict[str, int]:
        now = _iso(datetime.now(UTC)); created = updated = missing = 0
        obsolete_secret_ids: set[str] = set()
        with self.database.transaction(immediate=True) as conn:
            current_job = conn.execute(
                """SELECT j.kind,j.status,j.site_id,j.capability_instance_id,j.lease_token,
                          c.site_id capability_site_id,c.code capability_code
                   FROM jobs j
                   LEFT JOIN capability_instances c ON c.id=j.capability_instance_id
                   WHERE j.id=?""",
                (job_id,),
            ).fetchone()
            if (
                current_job is None
                or current_job["kind"] != "product_sync"
                or current_job["status"] != "running"
                or current_job["site_id"] != site_id
                or current_job["capability_site_id"] != site_id
                or current_job["capability_code"] != self.PRODUCT_CODE
                or current_job["lease_token"] != lease_token
            ):
                raise StaleJobWrite("product result rejected because its job lease is no longer current")
            run = conn.execute("SELECT id FROM runs WHERE job_id=? ORDER BY attempt_no DESC LIMIT 1", (job_id,)).fetchone()
            run_id = run["id"] if run else None
            category_rows = list(conn.execute(
                "SELECT id,name,source_name FROM product_categories WHERE site_id=?",
                (site_id,),
            ))
            # Source labels remain stable when a user renames the visible
            # category. Case-folded lookup also prevents scraper-created
            # categories that differ only by case.
            category_ids = {
                str(row["source_name"] or row["name"]).casefold(): row["id"]
                for row in category_rows
            }
            display_category_ids = {
                str(row["name"]).casefold(): row["id"] for row in category_rows
            }
            category_source_names = {
                row["id"]: row["source_name"] for row in category_rows
            }
            seen: set[str] = set()
            for index, raw in enumerate(products[:10_000]):
                item = raw.as_dict() if hasattr(raw, "as_dict") else dict(raw)
                name = str(item.get("name") or "").strip()[:500]
                if not name: continue
                category_name = str(item.get("category_name") or "全部").strip()[:200] or "全部"
                category_key = category_name.casefold()
                category_id = category_ids.get(category_key)
                if not category_id:
                    category_id = display_category_ids.get(category_key)
                    if category_id and category_source_names.get(category_id) is None:
                        conn.execute(
                            "UPDATE product_categories SET source_name=?,updated_at=? WHERE id=?",
                            (category_name, now, category_id),
                        )
                    elif not category_id:
                        category_id = str(uuid4())
                        conn.execute(
                            """INSERT INTO product_categories(
                                   id,site_id,name,sort_order,created_at,updated_at,source_name
                               ) VALUES(?,?,?,?,?,?,?)""",
                            (category_id, site_id, category_name, len(category_ids), now, now, category_name),
                        )
                        display_category_ids[category_key] = category_id
                    category_ids[category_key] = category_id
                    category_source_names[category_id] = category_name
                external = str(item.get("external_key") or "").strip() or hashlib.sha256(
                    f"{category_name}\0{name}".encode("utf-8")
                ).hexdigest()
                seen.add(external)
                old = conn.execute("SELECT * FROM products WHERE site_id=? AND external_key=?", (site_id, external)).fetchone()
                buy_url = str(item.get("buy_url") or "").strip()
                url_base = suffix_id = None; query_keys: tuple[str, ...] = ()
                if buy_url:
                    parsed = urlsplit(buy_url)
                    if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None and parsed.password is None:
                        url_base, suffix_id, query_keys = self._protect_url(
                            buy_url, f"product:{site_id}:{external}:buy", conn=conn
                        )
                endpoint_id = old["buy_endpoint_id"] if old and old["buy_endpoint_id"] else None
                if url_base:
                    if endpoint_id:
                        old_endpoint = conn.execute(
                            "SELECT url_suffix_secret_id FROM site_endpoints WHERE id=?", (endpoint_id,)
                        ).fetchone()
                        conn.execute(
                            """UPDATE site_endpoints SET label=?,url_base=?,url_suffix_secret_id=?,query_keys_json=?,updated_at=?
                               WHERE id=?""", (name, url_base, suffix_id, _dumps(list(query_keys)), now, endpoint_id)
                        )
                        if old_endpoint and old_endpoint["url_suffix_secret_id"] and old_endpoint["url_suffix_secret_id"] != suffix_id:
                            obsolete_secret_ids.add(old_endpoint["url_suffix_secret_id"])
                    else:
                        endpoint_id = str(uuid4())
                        conn.execute(
                            """INSERT INTO site_endpoints(id,site_id,role,label,url_base,url_suffix_secret_id,query_keys_json,
                                      is_default,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                            (endpoint_id, site_id, "other", name, url_base, suffix_id, _dumps(list(query_keys)), 0, index, now, now),
                        )
                stock = int(item.get("stock", -1) if item.get("stock") is not None else -1)
                availability = "sold_out" if stock == 0 else "available"
                product_id = old["id"] if old else str(uuid4())
                effective_category_id = (
                    old["category_id"]
                    if old and bool(old["category_overridden"])
                    else category_id
                )
                values = (
                    effective_category_id, category_id, name, item.get("price"), item.get("original_price"), stock,
                    item.get("stock_text"), int(item.get("sold") or 0), str(item.get("description") or "")[:4000] or None,
                    endpoint_id, url_base, suffix_id, _dumps(item.get("tags") or []), availability, now, now,
                )
                if old:
                    if old["buy_url_suffix_secret_id"] and old["buy_url_suffix_secret_id"] != suffix_id:
                        obsolete_secret_ids.add(old["buy_url_suffix_secret_id"])
                    conn.execute(
                        """UPDATE products SET category_id=?,scraped_category_id=?,name=?,price=?,original_price=?,stock=?,stock_text=?,sold=?,
                                  description=?,buy_endpoint_id=?,buy_url_base=?,buy_url_suffix_secret_id=?,raw_tags_json=?,availability_status=?,
                                  availability_reason=NULL,last_observed_at=?,updated_at=? WHERE id=?""",
                        (*values, product_id),
                    )
                    updated += 1
                    if old["price"] is not None and item.get("price") is not None and float(old["price"]) != float(item["price"]):
                        event_type = "price_drop" if float(item["price"]) < float(old["price"]) else "price_changed"
                        self._insert_event(
                            conn,
                            site_id,
                            run_id,
                            event_type,
                            "warning" if event_type == "price_drop" else "info",
                            f"{name} price dropped" if event_type == "price_drop" else f"{name} price changed",
                            {"product_id": product_id, "old_price": old["price"], "new_price": item["price"]},
                            now,
                        )
                    if old["availability_status"] in {"sold_out", "missing"} and availability == "available":
                        self._insert_event(
                            conn,
                            site_id,
                            run_id,
                            "back_in_stock",
                            "success",
                            f"{name} is back in stock",
                            {"product_id": product_id, "stock": stock},
                            now,
                        )
                else:
                    conn.execute(
                        """INSERT INTO products(id,site_id,category_id,scraped_category_id,external_key,name,price,original_price,stock,stock_text,
                                  sold,description,buy_endpoint_id,buy_url_base,buy_url_suffix_secret_id,raw_tags_json,availability_status,
                                  last_observed_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (product_id, site_id, values[0], values[1], external, *values[2:-1], now, values[-1]),
                    )
                    created += 1
                conn.execute(
                    """INSERT OR IGNORE INTO price_observations(id,product_id,price,stock,availability_status,observed_at,source_run_id,dedupe_key)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (str(uuid4()), product_id, item.get("price"), stock, availability, now, run_id, f"{job_id}:{product_id}"),
                )
            existing = list(conn.execute("SELECT id,external_key,name FROM products WHERE site_id=? AND availability_status!='missing'", (site_id,)))
            for row in existing:
                if row["external_key"] not in seen:
                    conn.execute(
                        "UPDATE products SET availability_status='missing',availability_reason='not observed in latest sync',updated_at=? WHERE id=?",
                        (now, row["id"]),
                    ); missing += 1
            self._insert_event(conn, site_id, run_id, "product_sync", "success", "Product sync completed",
                               {"created": created, "updated": updated, "missing": missing}, now)
        if self.secret_service:
            for secret_id in obsolete_secret_ids: self.secret_service.delete_if_unreferenced(secret_id)
        return {"created": created, "updated": updated, "missing": missing}

    def _insert_event(self, conn: sqlite3.Connection, site_id: str, run_id: str | None, event_type: str,
                      severity: str, title: str, payload: dict[str, Any], now: str,
                      detail: str = "") -> str:
        event_id = str(uuid4())
        conn.execute(
            """INSERT INTO events(id,type,severity,entity_type,entity_id,run_id,title,detail,payload_json,occurred_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, event_type, severity, "site", site_id, run_id, title, detail, _dumps(payload), now, now),
        )
        if self._notification_enabled_locked(conn, event_type):
            self.notification_outbox.enqueue_event_locked(conn, event_id, now=now)
        return event_id

    @staticmethod
    def _notification_enabled_locked(conn: sqlite3.Connection, event_type: str) -> bool:
        toggle = {
            "price_drop": "price_drop",
            "back_in_stock": "back_in_stock",
            "checkin_failed": "checkin_failed",
            "checkin_effect_unknown": "checkin_failed",
            "checkin_success": "checkin_success",
            "product_sync_failed": "product_sync_failed",
        }.get(event_type)
        if toggle is None:
            return False
        row = conn.execute(
            "SELECT value_json FROM settings WHERE key='notifications.config'"
        ).fetchone()
        config: Any = {}
        if row is not None:
            config = _loads(row["value_json"], {})
            if isinstance(config, str):
                config = _loads(config, {})
        if not isinstance(config, dict):
            config = {}
        if config.get("enabled", True) is False:
            return False
        defaults = {
            "price_drop": True,
            "back_in_stock": True,
            "checkin_failed": True,
            "checkin_success": False,
            "product_sync_failed": True,
        }
        return bool(config.get(toggle, defaults[toggle]))

    def _update_product_settings_sync(self, site_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = self._product_settings(site_id, True)
        if current is None:
            raise LookupError("site not found")
        capability = self.repository.find_capability(site_id=site_id, code=self.PRODUCT_CODE)
        assert capability
        cap_changes: dict[str, Any] = {}
        if "enabled" in payload:
            cap_changes["enabled"] = payload["enabled"]
        if payload.get("scraper_type") is not None:
            cap_changes["adapter"] = payload["scraper_type"]
        global_interval = int(self.repository.get_setting("scheduler.product_interval_minutes", "60"))
        override = None if payload.get("clear_interval_override") else payload.get("interval_override_minutes", current["interval_override_minutes"])
        policy = {"minutes": int(override or global_interval), "override": override is not None}
        with self.database.transaction(immediate=True) as conn:
            self._update_capability_atomic(
                capability["id"], owner="capability", account_id=None,
                changes=cap_changes, config=payload.get("config"),
                config_supplied=payload.get("config") is not None,
                clear_secrets=set(payload.get("clear_secrets") or []), conn=conn,
            )
            schedule = conn.execute(
                "SELECT * FROM schedules WHERE capability_instance_id=? ORDER BY id LIMIT 1", (capability["id"],)
            ).fetchone()
            if schedule:
                fields = ["policy_json=?"]
                values: list[Any] = [_dumps(policy)]
                if "enabled" in payload:
                    fields.append("enabled=?")
                    values.append(int(bool(payload["enabled"])))
                values.extend([_iso(datetime.now(UTC)), schedule["id"]])
                conn.execute(
                    f"UPDATE schedules SET {','.join(fields)},updated_at=? WHERE id=?", values
                )
        return self._product_settings(site_id, True)  # type: ignore[return-value]

    async def list_due_product_sites(self, now: datetime) -> list[dict[str, Any]]:
        return await self._call(self._due_product_sites, _iso(now))

    def _due_product_sites(self, now: str | None) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """SELECT c.site_id,s.policy_json FROM schedules s
                   JOIN capability_instances c ON c.id=s.capability_instance_id
                   WHERE c.code='product_monitor' AND c.enabled=1 AND s.enabled=1
                     AND lower(trim(coalesce(c.adapter,''))) NOT IN ('','none','manual')
                      AND (s.next_run_at IS NULL OR s.next_run_at<=?) ORDER BY c.site_id""",
                (now,),
            ).fetchall()
            result = []
            for row in rows:
                policy = _loads(row["policy_json"], {})
                result.append({"site_id": row["site_id"], "interval_override_minutes": policy.get("minutes") if policy.get("override") else None})
            return result

    async def mark_product_enqueued(self, site_id: UUID, next_run_at: datetime) -> None:
        await self._call(self._mark_product_enqueued, _uuid(site_id), _iso(next_run_at))

    def _mark_product_enqueued(self, site_id: str, next_run_at: str | None) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                """UPDATE schedules SET next_run_at=?,updated_at=? WHERE capability_instance_id IN
                   (SELECT id FROM capability_instances WHERE site_id=? AND code='product_monitor' AND account_id IS NULL)""",
                (next_run_at, _iso(datetime.now(UTC)), site_id),
            )

    # -- Durable job queue -------------------------------------------
    async def enqueue_job(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        return await self._call(self._enqueue_job_sync, payload)

    def _enqueue_job_sync(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        kind = str(payload["kind"])
        capability = None
        if kind == "checkin":
            account_id = _uuid(payload["account_id"])
            with self.database.connect() as conn:
                capability = conn.execute(
                    "SELECT * FROM capability_instances WHERE account_id=? AND code='checkin'", (account_id,)
                ).fetchone()
        elif kind == "product_sync":
            site_id = _uuid(payload["site_id"])
            capability = self._ensure_product_capability_sync(
                site_id, default_enabled=True,
            )
        elif kind not in {"bookmark_import", "login_session", "adapter_test"}:
            raise ValueError(f"unsupported job kind: {kind}")
        if kind in {"checkin", "product_sync"} and not capability:
            raise LookupError("capability not configured")
        try:
            row = self.repository.create_job(
                job_id=_uuid(payload["id"]),
                capability_instance_id=capability["id"] if capability else None,
                kind=kind,
                site_id=_uuid(payload["site_id"]) if payload.get("site_id") else None,
                account_id=_uuid(payload["account_id"]) if payload.get("account_id") else None,
                idempotency_key=payload["idempotency_key"], available_at=_iso(payload["scheduled_for"]),
                planned_at=_iso(payload["scheduled_for"]), max_attempts=int(payload.get("max_attempts", 3)),
                priority=int(payload.get("priority", 0)), payload=payload.get("payload") or {},
            )
            created = True
        except sqlite3.IntegrityError as exc:
            if "idempotency" not in str(exc).lower() and "unique" not in str(exc).lower():
                raise
            with self.database.connect() as conn:
                existing = conn.execute("SELECT * FROM jobs WHERE idempotency_key=?", (payload["idempotency_key"],)).fetchone()
            if not existing:
                raise
            row, created = dict(existing), False
        return self._normalize_job(row), created

    async def get_job(self, job_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._get_job_sync, _uuid(job_id))

    def _get_job_sync(self, job_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(self._job_select() + " WHERE j.id=?", (job_id,)).fetchone()
            return self._normalize_job(dict(row)) if row else None

    async def list_jobs(self, *, page: int, page_size: int, kind: str | None = None,
                        status: str | None = None, site_id: UUID | None = None,
                        account_id: UUID | None = None):
        return await self._call(self._list_jobs_sync, page, page_size, kind, status,
                                _uuid(site_id) if site_id else None, _uuid(account_id) if account_id else None)

    def _list_jobs_sync(self, page: int, page_size: int, kind: str | None, status: str | None,
                        site_id: str | None, account_id: str | None):
        clauses, params = [], []
        if kind:
            clauses.append("j.kind=?"); params.append(kind)
        if status:
            clauses.append("j.status=?"); params.append(status)
        if site_id:
            clauses.append("j.site_id=?"); params.append(site_id)
        if account_id:
            clauses.append("j.account_id=?"); params.append(account_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM jobs j" + where,
                params,
            ).fetchone()[0]
            rows = conn.execute(
                self._job_select() + where + " ORDER BY j.created_at DESC,j.id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            return [self._normalize_job(dict(row)) for row in rows], int(total)

    async def lease_next_job(self, *, kind: str, worker_instance: str,
                             now: datetime, lease_expires_at: datetime) -> dict[str, Any] | None:
        return await self._call(self._lease_job_sync, kind, worker_instance, _iso(now), _iso(lease_expires_at))

    def _lease_job_sync(self, kind: str, worker: str, now: str, lease_until: str) -> dict[str, Any] | None:
        token = str(uuid4())
        with self.database.transaction(immediate=True) as conn:
            candidate = conn.execute(
                """SELECT j.id FROM jobs j
                   WHERE j.kind=? AND j.status='queued' AND j.available_at<=? AND j.attempt<j.max_attempts
                   ORDER BY j.priority DESC,j.available_at,j.created_at,j.id LIMIT 1""",
                (kind, now),
            ).fetchone()
            if not candidate:
                return None
            changed = conn.execute(
                """UPDATE jobs SET status='leased',lease_owner=?,lease_token=?,lease_until=?,attempt=attempt+1,
                          started_at=COALESCE(started_at,?),updated_at=? WHERE id=? AND status='queued'""",
                (worker, token, lease_until, now, now, candidate["id"]),
            ).rowcount
            if changed != 1:
                return None
            claimed = conn.execute(
                "SELECT batch_id FROM jobs WHERE id=?", (candidate["id"],),
            ).fetchone()
            if claimed and claimed["batch_id"]:
                self._refresh_batch_state_locked(
                    conn, str(claimed["batch_id"]), now,
                )
            row = conn.execute(self._job_select() + " WHERE j.id=?", (candidate["id"],)).fetchone()
            return self._normalize_job(dict(row))

    async def mark_job_running(self, job_id: UUID, lease_token: str, now: datetime) -> dict[str, Any] | None:
        return await self._call(self._mark_running_sync, _uuid(job_id), lease_token, _iso(now))

    def _mark_running_sync(self, job_id: str, token: str, now: str) -> dict[str, Any] | None:
        with self.database.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] != "leased" or row["lease_token"] != token:
                return None
            conn.execute("UPDATE jobs SET status='running',started_at=COALESCE(started_at,?),updated_at=? WHERE id=?", (now, now, job_id))
            self._refresh_batch_state_locked(conn, row["batch_id"], now)
            run = conn.execute("SELECT id FROM runs WHERE job_id=? AND attempt_no=?", (job_id, row["attempt"])).fetchone()
            if not run:
                conn.execute(
                    """INSERT INTO runs(id,job_id,capability_instance_id,account_id,attempt_no,status,message,started_at,result_json,created_at)
                       VALUES(?,?,?,?,?,'running','',?,'{}',?)""",
                    (str(uuid4()), job_id, row["capability_instance_id"], row["account_id"],
                     row["attempt"], now, now),
                )
            current = conn.execute(self._job_select() + " WHERE j.id=?", (job_id,)).fetchone()
            return self._normalize_job(dict(current))

    async def heartbeat_job(self, job_id: UUID, lease_token: str, now: datetime,
                            lease_expires_at: datetime, progress: dict[str, Any] | None = None) -> bool:
        return await self._call(self._heartbeat_sync, _uuid(job_id), lease_token, _iso(now), _iso(lease_expires_at), progress)

    def _heartbeat_sync(self, job_id: str, token: str, now: str, lease_until: str,
                        progress: dict[str, Any] | None) -> bool:
        with self.database.transaction(immediate=True) as conn:
            row = conn.execute("SELECT progress_json FROM jobs WHERE id=? AND status IN ('leased','running') AND lease_token=?", (job_id, token)).fetchone()
            if not row:
                return False
            merged = _loads(row["progress_json"], {})
            state = merged.get("__state")
            if progress:
                merged.update(progress)
            if state:
                merged["__state"] = state
            return conn.execute(
                "UPDATE jobs SET lease_until=?,progress_json=?,updated_at=? WHERE id=? AND status IN ('leased','running') AND lease_token=?",
                (lease_until, _dumps(merged), now, job_id, token),
            ).rowcount == 1

    async def complete_job(self, job_id: UUID, lease_token: str, *, status: str,
                           result: dict[str, Any] | None, error_kind: str | None,
                           error_message: str | None, now: datetime) -> dict[str, Any] | None:
        return await self._call(self._complete_sync, _uuid(job_id), lease_token, status, result,
                                error_kind, error_message, _iso(now))

    def _complete_sync(self, job_id: str, token: str, status: str, result: dict[str, Any] | None,
                       error_kind: str | None, error_message: str | None, now: str) -> dict[str, Any] | None:
        db_status = status if status in {"succeeded", "failed", "skipped", "effect_unknown", "cancelled"} else "failed"
        public_result = dict(result or {})
        if error_kind:
            public_result["_error_kind"] = error_kind
        with self.database.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] not in {"leased", "running"} or row["lease_token"] != token:
                return None
            retrying = db_status == "failed" and error_kind == "infrastructure" and row["attempt"] < row["max_attempts"]
            final_job_status = "queued" if retrying else db_status
            conn.execute(
                """UPDATE jobs SET status=?,result_json=?,last_error=?,lease_owner=NULL,lease_token=NULL,
                          lease_until=NULL,available_at=CASE WHEN ?='queued' THEN ? ELSE available_at END,
                          finished_at=CASE WHEN ?='queued' THEN NULL ELSE ? END,updated_at=?
                   WHERE id=? AND status IN ('leased','running') AND lease_token=?""",
                (final_job_status, _dumps(public_result), error_message, final_job_status, now,
                 final_job_status, now, now, job_id, token),
            )
            run = conn.execute("SELECT id FROM runs WHERE job_id=? AND attempt_no=?", (job_id, row["attempt"])).fetchone()
            if run:
                run_id = str(run["id"])
                conn.execute(
                    """UPDATE runs SET status=?,reason_code=?,message=?,finished_at=?,result_json=? WHERE id=?""",
                    (db_status, error_kind, error_message or "", now, _dumps(result or {}), run_id),
                )
            else:
                run_id = str(uuid4())
                conn.execute(
                    """INSERT INTO runs(id,job_id,capability_instance_id,account_id,attempt_no,status,reason_code,message,started_at,finished_at,result_json,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, job_id, row["capability_instance_id"], row["account_id"], row["attempt"],
                     db_status, error_kind, error_message or "", row["started_at"] or now, now,
                     _dumps(result or {}), now),
                )
            if not retrying:
                self._insert_job_result_event_locked(
                    conn, row=dict(row), run_id=run_id, status=db_status,
                    result=result or {}, error_kind=error_kind, now=now,
                )
            self._refresh_batch_state_locked(conn, row["batch_id"], now)
            current = conn.execute(self._job_select() + " WHERE j.id=?", (job_id,)).fetchone()
            return self._normalize_job(dict(current))

    def _insert_job_result_event_locked(
        self,
        conn: sqlite3.Connection,
        *,
        row: dict[str, Any],
        run_id: str | None,
        status: str,
        result: dict[str, Any],
        error_kind: str | None,
        now: str,
    ) -> None:
        kind = str(row.get("kind") or "")
        if kind not in {"checkin", "product_sync"}:
            return
        site_id = row.get("site_id")
        if site_id is None and row.get("account_id"):
            account = conn.execute(
                "SELECT site_id FROM site_accounts WHERE id=?", (row["account_id"],)
            ).fetchone()
            site_id = account["site_id"] if account else None
        if site_id is None:
            return
        raw_reason = result.get("reason_code") or error_kind or status
        reason = str(raw_reason).casefold()
        if not re.fullmatch(r"[a-z0-9_.-]{1,80}", reason):
            reason = "unknown"
        payload = {
            "job_id": row["id"],
            "account_id": row.get("account_id"),
            "status": status,
            "reason_code": reason,
        }
        if kind == "checkin":
            if status == "succeeded":
                event_type, severity, title = "checkin_success", "success", "Check-in completed"
            elif status == "skipped":
                event_type, severity, title = "checkin_skipped", "info", "Check-in skipped"
            elif status == "effect_unknown":
                event_type, severity, title = (
                    "checkin_effect_unknown", "warning", "Check-in outcome needs confirmation"
                )
            elif status == "failed":
                event_type, severity, title = "checkin_failed", "danger", "Check-in failed"
            else:
                return
        elif status in {"failed", "effect_unknown"}:
            event_type, severity, title = (
                "product_sync_failed",
                "danger" if status == "failed" else "warning",
                "Product sync failed" if status == "failed" else "Product sync outcome is unknown",
            )
        else:
            return
        self._insert_event(
            conn, str(site_id), run_id, event_type, severity, title, payload, now
        )

    async def cancel_job(self, job_id: UUID, now: datetime) -> dict[str, Any] | None:
        return await self._call(self._cancel_sync, _uuid(job_id), _iso(now))

    def _cancel_sync(self, job_id: str, now: str) -> dict[str, Any] | None:
        with self.database.transaction(immediate=True) as conn:
            existing = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if existing is None or existing["status"] not in {"queued", "leased", "running"}:
                return None
            uncertain_checkin = (
                existing["kind"] == "checkin"
                and existing["status"] in {"leased", "running"}
            )
            if uncertain_checkin:
                result = _loads(existing["result_json"], {})
                if not isinstance(result, dict):
                    result = {}
                result.update({
                    "_error_kind": "cancel_requested",
                    "reason_code": "cancel_requested",
                    "cancel_requested": True,
                    "requires_reconcile": True,
                })
                conn.execute(
                    """UPDATE jobs SET status='effect_unknown',result_json=?,last_error=?,
                              lease_owner=NULL,lease_token=NULL,lease_until=NULL,finished_at=?,updated_at=?
                       WHERE id=?""",
                    (_dumps(result), "Cancellation requested after check-in execution began; remote outcome requires reconciliation",
                     now, now, job_id),
                )
                conn.execute(
                    """UPDATE runs SET status='effect_unknown',reason_code='cancel_requested',
                              message='cancellation requested; remote outcome requires reconciliation',finished_at=?
                       WHERE job_id=? AND status='running'""",
                    (now, job_id),
                )
                run = conn.execute(
                    "SELECT id FROM runs WHERE job_id=? ORDER BY attempt_no DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
                self._insert_job_result_event_locked(
                    conn, row=dict(existing), run_id=str(run["id"]) if run else None,
                    status="effect_unknown", result=result,
                    error_kind="cancel_requested", now=now,
                )
            else:
                conn.execute(
                    """UPDATE jobs SET status='cancelled',lease_owner=NULL,lease_token=NULL,lease_until=NULL,
                              result_json=?,last_error=NULL,finished_at=?,updated_at=? WHERE id=?""",
                    (_dumps({"reason_code": "cancelled", "cancelled_before_start": existing["status"] == "queued"}),
                     now, now, job_id),
                )
                conn.execute(
                    """UPDATE runs SET status='cancelled',reason_code='cancelled',message='cancelled by user',
                              finished_at=? WHERE job_id=? AND status='running'""", (now, job_id)
                )
            self._refresh_batch_state_locked(conn, existing["batch_id"], now)
            row = conn.execute(self._job_select() + " WHERE j.id=?", (job_id,)).fetchone()
            return self._normalize_job(dict(row))

    async def retry_job(self, job_id: UUID, now: datetime) -> dict[str, Any] | None:
        return await self._call(self._retry_sync, _uuid(job_id), _iso(now))

    def _retry_sync(self, job_id: str, now: str) -> dict[str, Any] | None:
        with self.database.transaction(immediate=True) as conn:
            existing = conn.execute(
                "SELECT batch_id FROM jobs WHERE id=?", (job_id,),
            ).fetchone()
            changed = conn.execute(
                """UPDATE jobs SET status='queued',available_at=?,planned_at=?,lease_owner=NULL,
                          lease_token=NULL,lease_until=NULL,progress_json='{}',result_json='{}',last_error=NULL,
                          started_at=NULL,finished_at=NULL,max_attempts=MAX(max_attempts,attempt+3),updated_at=?
                   WHERE id=? AND status IN ('failed','cancelled','effect_unknown')""",
                (now, now, now, job_id),
            ).rowcount
            if not changed:
                return None
            self._refresh_batch_state_locked(
                conn, existing["batch_id"] if existing else None, now,
            )
            row = conn.execute(self._job_select() + " WHERE j.id=?", (job_id,)).fetchone()
            return self._normalize_job(dict(row))

    async def recover_expired_jobs(self, now: datetime) -> list[dict[str, Any]]:
        return await self._call(self._recover_sync, _iso(now))

    def _recover_sync(self, now: str) -> list[dict[str, Any]]:
        with self.database.transaction(immediate=True) as conn:
            ids = [row["id"] for row in conn.execute(
                "SELECT id FROM jobs WHERE status IN ('leased','running') AND lease_until IS NOT NULL AND lease_until<=?", (now,)
            )]
            batch_ids: set[str] = set()
            for job_id in ids:
                job_row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if job_row is None:
                    continue
                if job_row["batch_id"]:
                    batch_ids.add(str(job_row["batch_id"]))
                terminal = int(job_row["attempt"]) >= int(job_row["max_attempts"])
                conn.execute(
                    """UPDATE jobs SET status=CASE WHEN attempt>=max_attempts THEN 'effect_unknown' ELSE 'queued' END,
                              available_at=?,lease_owner=NULL,lease_token=NULL,lease_until=NULL,
                              result_json=CASE WHEN attempt>=max_attempts THEN ? ELSE result_json END,
                              finished_at=CASE WHEN attempt>=max_attempts THEN ? ELSE NULL END,
                              last_error='worker lease expired',updated_at=? WHERE id=?""",
                    (now, _dumps({"_error_kind": "worker_lost", "reason_code": "worker_lost"}),
                     now, now, job_id),
                )
                conn.execute(
                    """UPDATE runs SET status='effect_unknown',reason_code='worker_lost',
                              message='worker lease expired before outcome was confirmed',finished_at=?
                       WHERE job_id=? AND status='running'""", (now, job_id)
                )
                if terminal:
                    run = conn.execute(
                        "SELECT id FROM runs WHERE job_id=? AND attempt_no=?",
                        (job_id, job_row["attempt"]),
                    ).fetchone()
                    self._insert_job_result_event_locked(
                        conn, row=dict(job_row), run_id=str(run["id"]) if run else None,
                        status="effect_unknown", result={"reason_code": "worker_lost"},
                        error_kind="worker_lost", now=now,
                    )
            for batch_id in batch_ids:
                self._refresh_batch_state_locked(conn, batch_id, now)
            return [self._normalize_job(dict(row)) for row in conn.execute(
                self._job_select() + (" WHERE j.id IN (%s)" % ",".join("?" for _ in ids)), ids
            )] if ids else []

    @classmethod
    def _job_select(cls) -> str:
        return """SELECT j.*,c.code capability_code
                  FROM jobs j LEFT JOIN capability_instances c ON c.id=j.capability_instance_id"""

    @classmethod
    def _normalize_job(cls, raw: dict[str, Any]) -> dict[str, Any]:
        progress = _loads(raw.get("progress_json"), {})
        status = raw.get("status")
        result = _loads(raw.get("result_json"), {})
        error_kind = result.pop("_error_kind", None)
        code = raw.get("capability_code")
        kind = raw.get("kind") or ("checkin" if code == cls.CHECKIN_CODE else "product_sync")
        return {
            "id": raw["id"], "kind": kind,
            "status": status, "site_id": raw.get("site_id"), "account_id": raw.get("account_id"),
            "scheduled_for": raw.get("planned_at") or raw.get("available_at"),
            "attempt": int(raw.get("attempt") or 0), "max_attempts": int(raw.get("max_attempts") or 1),
            "progress": progress or None, "result": result or None, "error_kind": error_kind,
            "error_message": raw.get("last_error"), "created_at": raw["created_at"],
            "started_at": raw.get("started_at"), "finished_at": raw.get("finished_at"),
            "lease_token": raw.get("lease_token"), "lease_expires_at": raw.get("lease_until"),
            "worker_instance": raw.get("lease_owner"), "idempotency_key": raw.get("idempotency_key"),
            "payload": _loads(raw.get("payload_json"), {}),
        }

    # -- Settings and schedules -------------------------------------
    async def get_settings(self) -> dict[str, str]:
        values = await self._call(self.repository.list_settings)
        return {key: str(value) for key, value in values.items()}

    async def update_settings(self, values: dict[str, str]) -> dict[str, str]:
        for key, value in values.items():
            await self._call(self.repository.set_setting, key, value)
        return await self.get_settings()

    async def get_batch_for_date(self, date_key: str) -> dict[str, Any] | None:
        return await self._call(self._get_batch_for_date, date_key)

    async def list_batches(self, *, page: int, page_size: int, status: str | None = None,
                           date_from: str | None = None, date_to: str | None = None):
        return await self._call(self._list_batches_sync, page, page_size, status, date_from, date_to)

    def _list_batches_sync(self, page: int, page_size: int, status: str | None,
                           date_from: str | None, date_to: str | None):
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("b.status=?"); params.append(status)
        if date_from:
            clauses.append("b.business_date>=?"); params.append(date_from)
        if date_to:
            clauses.append("b.business_date<=?"); params.append(date_to)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM job_batches b" + where, params).fetchone()[0])
            rows = conn.execute(
                """SELECT b.*,COUNT(j.id) item_count,
                          SUM(CASE WHEN j.status='succeeded' THEN 1 ELSE 0 END) succeeded_count,
                          SUM(CASE WHEN j.status IN ('failed','effect_unknown') THEN 1 ELSE 0 END) failed_count
                   FROM job_batches b LEFT JOIN jobs j ON j.batch_id=b.id""" + where +
                " GROUP BY b.id ORDER BY b.business_date DESC,b.created_at DESC,b.id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return [self._normalize_batch(dict(row)) for row in rows], total

    async def get_batch(self, batch_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._get_batch_sync, _uuid(batch_id))

    def _get_batch_sync(self, batch_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                """SELECT b.*,COUNT(j.id) item_count,
                          SUM(CASE WHEN j.status='succeeded' THEN 1 ELSE 0 END) succeeded_count,
                          SUM(CASE WHEN j.status IN ('failed','effect_unknown') THEN 1 ELSE 0 END) failed_count
                   FROM job_batches b LEFT JOIN jobs j ON j.batch_id=b.id
                   WHERE b.id=? GROUP BY b.id""",
                (batch_id,),
            ).fetchone()
        return self._normalize_batch(dict(row)) if row else None

    def _get_batch_for_date(self, date_key: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM job_batches WHERE business_date=? AND status!='cancelled' ORDER BY created_at DESC LIMIT 1",
                (date_key,),
            ).fetchone()
            return self._normalize_batch(dict(row)) if row else None

    async def create_batch(self, payload: dict[str, Any], account_ids: list[UUID]) -> dict[str, Any]:
        return await self._call(self._create_batch_sync, payload, account_ids)

    def _create_batch_sync(self, payload: dict[str, Any], account_ids: list[UUID]) -> dict[str, Any]:
        date_key = str(payload["date_key"])
        planned = _iso(payload["planned_at"])
        batch_id = str(uuid4())
        now = _iso(datetime.now(UTC))
        with self.database.transaction(immediate=True) as conn:
            conn.execute(
                """INSERT INTO job_batches(
                       id,schedule_id,business_date,timezone,planned_at,trigger_reason,status,
                       created_at,updated_at
                   ) VALUES(?,NULL,?,'Asia/Shanghai',?,'normal','pending',?,?)""",
                (batch_id, date_key, planned, now, now),
            )
            for account_id in account_ids:
                account = _uuid(account_id)
                capability = conn.execute(
                    """SELECT id,site_id FROM capability_instances
                       WHERE account_id=? AND code='checkin' AND enabled=1""",
                    (account,),
                ).fetchone()
                if capability:
                    try:
                        conn.execute(
                            """INSERT INTO jobs(
                                   id,batch_id,kind,site_id,account_id,capability_instance_id,
                                   business_date,planned_at,available_at,priority,status,max_attempts,
                                   idempotency_key,payload_json,created_at,updated_at
                               ) VALUES(?,?,'checkin',?,?,?,?,?,?,50,'queued',3,?,?,?,?)""",
                            (
                                str(uuid4()), batch_id, capability["site_id"], account,
                                capability["id"], date_key, planned, planned,
                                f"checkin:{account}:{date_key}",
                                _dumps({"batch_id": batch_id}), now, now,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        if not conn.execute(
                            "SELECT 1 FROM jobs WHERE idempotency_key=?",
                            (f"checkin:{account}:{date_key}",),
                        ).fetchone():
                            raise
            batch = conn.execute(
                "SELECT * FROM job_batches WHERE id=?", (batch_id,),
            ).fetchone()
        return self._normalize_batch(dict(batch))

    async def regenerate_batch(self, date_key: str, planned_at: datetime) -> dict[str, Any] | None:
        return await self._call(self._regenerate_batch_sync, date_key, _iso(planned_at))

    def _regenerate_batch_sync(self, date_key: str, planned: str) -> dict[str, Any] | None:
        now = _iso(datetime.now(UTC))
        with self.database.transaction(immediate=True) as conn:
            batch = conn.execute(
                """SELECT * FROM job_batches
                   WHERE business_date=? AND status!='cancelled'
                   ORDER BY created_at DESC LIMIT 1""",
                (date_key,),
            ).fetchone()
            if (
                batch is None
                or batch["status"] != "pending"
                or batch["started_at"] is not None
                or conn.execute(
                    "SELECT 1 FROM jobs WHERE batch_id=? AND status!='queued' LIMIT 1",
                    (batch["id"],),
                ).fetchone()
            ):
                return None
            conn.execute(
                """UPDATE job_batches
                   SET planned_at=?,trigger_reason='regenerated',updated_at=? WHERE id=?""",
                (planned, now, batch["id"]),
            )
            conn.execute(
                """UPDATE jobs SET planned_at=?,available_at=?,updated_at=?
                   WHERE batch_id=? AND status='queued'""",
                (planned, planned, now, batch["id"]),
            )
            row = conn.execute(
                "SELECT * FROM job_batches WHERE id=?", (batch["id"],),
            ).fetchone()
        return self._normalize_batch(dict(row))

    async def list_batch_items(self, batch_id: UUID) -> list[dict[str, Any]]:
        return await self._call(self._list_batch_items_sync, _uuid(batch_id))

    def _list_batch_items_sync(self, batch_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(self._job_select() + " WHERE j.batch_id=? ORDER BY j.created_at,j.id", (batch_id,)).fetchall()
            result = [self._normalize_job(dict(row)) for row in rows]
            for item in result: item["job_id"] = item["id"]
            return result

    async def list_default_schedule_accounts(self) -> list[dict[str, Any]]:
        return await self._call(self._schedule_accounts, False)

    async def list_override_schedule_accounts(self) -> list[dict[str, Any]]:
        return await self._call(self._schedule_accounts, True)

    async def list_schedules(self, *, page: int, page_size: int,
                             capability: str | None = None, enabled: bool | None = None):
        return await self._call(self._list_schedules_sync, page, page_size, capability, enabled)

    def _list_schedules_sync(self, page: int, page_size: int,
                             capability: str | None, enabled: bool | None):
        clauses: list[str] = []
        params: list[Any] = []
        if capability:
            clauses.append("c.code=?"); params.append(capability)
        if enabled is not None:
            clauses.append("s.enabled=?"); params.append(int(enabled))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        base = """ FROM schedules s
                  JOIN capability_instances c ON c.id=s.capability_instance_id
                  JOIN sites st ON st.id=c.site_id
                  LEFT JOIN site_accounts a ON a.id=c.account_id
                  LEFT JOIN jobs active_job ON active_job.id=(
                      SELECT candidate.id FROM jobs candidate
                      WHERE candidate.capability_instance_id=c.id
                        AND candidate.status IN ('queued','leased','running')
                      ORDER BY candidate.created_at DESC,candidate.id DESC LIMIT 1
                  )
                  LEFT JOIN jobs terminal_job ON terminal_job.id=(
                      SELECT candidate.id FROM jobs candidate
                      WHERE candidate.capability_instance_id=c.id
                        AND candidate.status IN ('succeeded','failed','skipped','cancelled','effect_unknown')
                      ORDER BY candidate.created_at DESC,candidate.id DESC LIMIT 1
                  )"""
        with self.database.connect() as conn:
            total = int(conn.execute("SELECT COUNT(*)" + base + where, params).fetchone()[0])
            rows = conn.execute(
                """SELECT s.*,c.code capability,c.site_id,c.account_id,c.adapter scraper_type,
                          st.name site_name,a.label account_label,
                          active_job.id current_job_id,active_job.status current_status,
                          active_job.created_at current_job_at,
                          terminal_job.id last_job_id,terminal_job.status last_status,
                          terminal_job.finished_at last_run_at,
                          json_extract(terminal_job.result_json,'$._error_kind') last_error_kind,
                          terminal_job.last_error last_error_message""" + base + where +
                " ORDER BY s.enabled DESC,s.next_run_at IS NULL,s.next_run_at,st.name,a.label LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        result = []
        for raw in rows:
            row = dict(raw)
            row["enabled"] = bool(row.get("enabled"))
            row["policy"] = _loads(row.pop("policy_json", None), {})
            row["backoff"] = _loads(row.pop("backoff_json", None), {})
            if row.get("capability") == self.PRODUCT_CODE:
                scraper_type = str(row.get("scraper_type") or "none").strip().casefold()
                row["automatic_run_supported"] = scraper_type not in {"", "none", "manual"}
                row["automatic_run_reason"] = (
                    "manual_catalog" if scraper_type == "manual"
                    else "scraper_not_configured" if scraper_type in {"", "none"}
                    else None
                )
            else:
                row["automatic_run_supported"] = True
                row["automatic_run_reason"] = None
            result.append(row)
        return result, total

    def _schedule_accounts(self, overrides: bool) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """SELECT a.id,a.site_id,a.label name,s.policy_json
                   FROM site_accounts a JOIN capability_instances c ON c.account_id=a.id AND c.code='checkin'
                   JOIN schedules s ON s.capability_instance_id=c.id
                   WHERE a.enabled=1 AND c.enabled=1 AND s.enabled=1
                     AND ((?=0 AND s.policy_type='daily_random_window' AND json_extract(s.policy_json,'$.mode') IS NULL)
                       OR (?=1 AND (s.policy_type='cron' OR json_extract(s.policy_json,'$.mode')='custom_window')))
                   ORDER BY a.id""",
                (int(overrides), int(overrides)),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                policy = _loads(item.pop("policy_json"), {})
                item["schedule_override"] = policy
                result.append(item)
            return result

    @staticmethod
    def _normalize_batch(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["date_key"] = result.get("business_date")
        return result

    # -- Artifacts ----------------------------------------------------
    async def get_artifact(self, artifact_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._get_artifact_sync, _uuid(artifact_id))

    def _get_artifact_sync(self, artifact_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                """SELECT a.*,r.job_id FROM artifacts a LEFT JOIN runs r ON r.id=a.run_id WHERE a.id=?""", (artifact_id,)
            ).fetchone()
            return self._normalize_artifact(dict(row)) if row else None

    async def create_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._call(self._create_artifact_sync, payload)

    def _create_artifact_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = payload.get("run_id")
        if not run_id and payload.get("job_id"):
            with self.database.connect() as conn:
                run = conn.execute("SELECT id FROM runs WHERE job_id=? ORDER BY attempt_no DESC LIMIT 1", (str(payload["job_id"]),)).fetchone()
                run_id = run["id"] if run else None
        row = self.repository.create_artifact(
            artifact_id=str(payload.get("id") or uuid4()), run_id=str(run_id) if run_id else None,
            role=payload.get("kind") or payload.get("role") or "artifact",
            relative_path=payload["relative_path"], sha256=payload.get("sha256"),
            mime_type=payload.get("mime_type"), size=payload.get("size"),
        )
        row["job_id"] = str(payload.get("job_id")) if payload.get("job_id") else None
        return self._normalize_artifact(row)

    @staticmethod
    def _normalize_artifact(row: dict[str, Any]) -> dict[str, Any]:
        relative = row["relative_path"]
        return {
            "id": row["id"], "run_id": row.get("run_id"), "job_id": row.get("job_id"),
            "role": row.get("role") or "artifact", "kind": row.get("role") or "artifact",
            "filename": Path(relative).name, "relative_path": relative,
            "size": int(row.get("size") or 0), "sha256": row.get("sha256") or "",
            "mime_type": row.get("mime_type"), "created_at": row["created_at"],
        }

    # -- Supporting UI resources ------------------------------------
    async def list_folders(self) -> list[dict[str, Any]]:
        return await self._call(self.repository.list_folders)

    async def create_folder(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._call(
            self.repository.create_folder, payload["name"],
            parent_id=str(payload["parent_id"]) if payload.get("parent_id") else None,
            sort_order=int(payload.get("sort_order", 0)),
        )

    async def update_folder(self, folder_id: UUID, payload: dict[str, Any]) -> dict[str, Any] | None:
        return await self._call(self._update_folder_sync, _uuid(folder_id), payload)

    def _update_folder_sync(self, folder_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"name", "parent_id", "sort_order"}
        changes = {key: value for key, value in payload.items() if key in allowed}
        with self.database.transaction(immediate=True) as conn:
            current = conn.execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()
            if not current:
                return None
            if "name" in changes:
                changes["name"] = str(changes["name"]).strip()
            if "sort_order" in changes:
                changes["sort_order"] = int(changes["sort_order"])
            if "parent_id" in changes:
                parent_id = str(changes["parent_id"]) if changes["parent_id"] else None
                if parent_id == folder_id:
                    raise ValueError("folder cannot be its own parent")
                if parent_id:
                    parent = conn.execute("SELECT id FROM folders WHERE id=?", (parent_id,)).fetchone()
                    if not parent:
                        raise ValueError("parent folder not found")
                    descendant = conn.execute(
                        """WITH RECURSIVE descendants(id) AS (
                               SELECT id FROM folders WHERE parent_id=?
                               UNION ALL
                               SELECT f.id FROM folders f JOIN descendants d ON f.parent_id=d.id
                           ) SELECT 1 FROM descendants WHERE id=? LIMIT 1""",
                        (folder_id, parent_id),
                    ).fetchone()
                    if descendant:
                        raise ValueError("folder hierarchy cannot contain a cycle")
                changes["parent_id"] = parent_id
            if changes:
                values = list(changes.values())
                conn.execute(
                    f"UPDATE folders SET {','.join(key + '=?' for key in changes)},updated_at=? WHERE id=?",
                    [*values, _iso(datetime.now(UTC)), folder_id],
                )
            row = conn.execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()
        return dict(row)

    async def delete_folder(self, folder_id: UUID) -> bool:
        return await self._call(self._delete_folder_sync, _uuid(folder_id))

    def _delete_folder_sync(self, folder_id: str) -> bool:
        if folder_id == self.ROOT_FOLDER_ID:
            return False
        with self.database.transaction(immediate=True) as conn:
            row = conn.execute("SELECT parent_id FROM folders WHERE id=?", (folder_id,)).fetchone()
            if not row:
                return False
            parent_id = row["parent_id"]
            conn.execute("UPDATE folders SET parent_id=?,updated_at=? WHERE parent_id=?",
                         (parent_id, _iso(datetime.now(UTC)), folder_id))
            conn.execute("UPDATE sites SET folder_id=?,updated_at=? WHERE folder_id=?",
                         (parent_id or self.ROOT_FOLDER_ID, _iso(datetime.now(UTC)), folder_id))
            return conn.execute("DELETE FROM folders WHERE id=?", (folder_id,)).rowcount == 1

    async def list_tags(self) -> list[dict[str, Any]]:
        return await self._call(self.repository.list_tags)

    async def create_tag(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._call(
            self.repository.create_tag, payload["name"], color=payload.get("color", "#8b5cf6"),
            icon=payload.get("icon", "tag"), sort_order=int(payload.get("sort_order", 0)),
        )

    async def update_tag(self, tag_id: UUID, payload: dict[str, Any]) -> dict[str, Any] | None:
        return await self._call(self._update_tag_sync, _uuid(tag_id), payload)

    def _update_tag_sync(self, tag_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"name", "color", "icon", "sort_order"}
        fields = [(key, value) for key, value in payload.items() if key in allowed]
        if not fields:
            with self.database.connect() as conn:
                row = conn.execute("SELECT * FROM tags WHERE id=?", (tag_id,)).fetchone()
                return dict(row) if row else None
        now = _iso(datetime.now(UTC))
        with self.database.transaction() as conn:
            values = [value for _, value in fields]
            changed = conn.execute(
                f"UPDATE tags SET {','.join(key + '=?' for key, _ in fields)},updated_at=? WHERE id=?",
                [*values, now, tag_id],
            ).rowcount
            row = conn.execute("SELECT * FROM tags WHERE id=?", (tag_id,)).fetchone() if changed else None
            return dict(row) if row else None

    async def delete_tag(self, tag_id: UUID) -> bool:
        return await self._call(self._delete_tag_sync, _uuid(tag_id))

    def _delete_tag_sync(self, tag_id: str) -> bool:
        with self.database.transaction() as conn:
            return conn.execute("DELETE FROM tags WHERE id=?", (tag_id,)).rowcount == 1

    async def list_products(self, *, page: int, page_size: int, site_id: UUID | None = None,
                            q: str | None = None, availability_status: str | None = None,
                            category_id: UUID | None = None):
        return await self._call(
            self._list_products_sync, page, page_size, _uuid(site_id) if site_id else None,
            q, availability_status, _uuid(category_id) if category_id else None,
        )

    def _list_products_sync(self, page: int, page_size: int, site_id: str | None,
                            q: str | None, availability: str | None,
                            category_id: str | None):
        clauses, params = [], []
        if site_id:
            clauses.append("p.site_id=?"); params.append(site_id)
        if q:
            clauses.append("(p.name LIKE ? OR COALESCE(p.description,'') LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        if availability:
            clauses.append("p.availability_status=?"); params.append(availability)
        if category_id:
            clauses.append("p.category_id=?"); params.append(category_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM products p" + where, params).fetchone()[0])
            rows = conn.execute(
                """SELECT p.*,s.name site_name,pc.name category_name,
                           scraped_pc.name scraped_category_name,
                           COALESCE(e.url_base,p.buy_url_base) AS url
                    FROM products p JOIN sites s ON s.id=p.site_id
                    LEFT JOIN product_categories pc ON pc.id=p.category_id
                    LEFT JOIN product_categories scraped_pc ON scraped_pc.id=p.scraped_category_id
                    LEFT JOIN site_endpoints e ON e.id=p.buy_endpoint_id""" + where +
                " ORDER BY p.updated_at DESC,p.id LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            return [self._normalize_product(dict(row)) for row in rows], total

    async def get_product(self, product_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._get_product_sync, _uuid(product_id))

    def _get_product_sync(self, product_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                """SELECT p.*,s.name site_name,pc.name category_name,
                          scraped_pc.name scraped_category_name,
                          COALESCE(e.url_base,p.buy_url_base) AS url
                   FROM products p JOIN sites s ON s.id=p.site_id
                   LEFT JOIN product_categories pc ON pc.id=p.category_id
                   LEFT JOIN product_categories scraped_pc ON scraped_pc.id=p.scraped_category_id
                   LEFT JOIN site_endpoints e ON e.id=p.buy_endpoint_id
                   WHERE p.id=?""",
                (product_id,),
            ).fetchone()
            if row is None:
                return None
            result = self._normalize_product(dict(row))
            result["tags"] = [dict(item) for item in conn.execute(
                """SELECT t.id,t.name,t.color,t.icon FROM tags t
                   JOIN product_tags pt ON pt.tag_id=t.id
                   WHERE pt.product_id=? ORDER BY t.sort_order,t.name,t.id""",
                (product_id,),
            )]
            stats = conn.execute(
                """SELECT COUNT(*) observation_count,MIN(price) min_price,MAX(price) max_price,
                          MIN(observed_at) first_observed_at,MAX(observed_at) last_observed_at
                   FROM price_observations WHERE product_id=?""",
                (product_id,),
            ).fetchone()
            result["price_history_summary"] = dict(stats) if stats else {
                "observation_count": 0, "min_price": None, "max_price": None,
                "first_observed_at": None, "last_observed_at": None,
            }
            return result

    async def list_product_price_history(
        self, product_id: UUID, *, page: int, page_size: int,
        date_from: datetime | None = None, date_to: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int] | None:
        return await self._call(
            self._list_product_price_history_sync, _uuid(product_id), page, page_size,
            _iso(date_from), _iso(date_to),
        )

    def _list_product_price_history_sync(
        self, product_id: str, page: int, page_size: int,
        date_from: str | None, date_to: str | None,
    ) -> tuple[list[dict[str, Any]], int] | None:
        clauses = ["product_id=?"]
        params: list[Any] = [product_id]
        if date_from:
            clauses.append("observed_at>=?"); params.append(date_from)
        if date_to:
            clauses.append("observed_at<=?"); params.append(date_to)
        where = " WHERE " + " AND ".join(clauses)
        with self.database.connect() as conn:
            if conn.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone() is None:
                return None
            total = int(conn.execute(
                "SELECT COUNT(*) FROM price_observations" + where, params,
            ).fetchone()[0])
            rows = conn.execute(
                """SELECT id,product_id,price,stock,availability_status,observed_at,source_run_id
                   FROM price_observations""" + where +
                " ORDER BY observed_at DESC,id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            return [dict(row) for row in rows], total

    async def list_product_categories(
        self, *, page: int, page_size: int, site_id: UUID | None = None,
        q: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        return await self._call(
            self._list_product_categories_sync, page, page_size,
            _uuid(site_id) if site_id else None, q,
        )

    def _list_product_categories_sync(
        self, page: int, page_size: int, site_id: str | None, q: str | None,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if site_id:
            clauses.append("pc.site_id=?"); params.append(site_id)
        if q:
            clauses.append("(pc.name LIKE ? OR s.name LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.connect() as conn:
            total = int(conn.execute(
                """SELECT COUNT(*) FROM product_categories pc
                   JOIN sites s ON s.id=pc.site_id""" + where,
                params,
            ).fetchone()[0])
            rows = conn.execute(
                """SELECT pc.*,s.name site_name,COUNT(p.id) product_count
                   FROM product_categories pc JOIN sites s ON s.id=pc.site_id
                   LEFT JOIN products p ON p.category_id=pc.id""" + where +
                " GROUP BY pc.id ORDER BY s.name COLLATE NOCASE,pc.sort_order,pc.name COLLATE NOCASE,pc.id LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            return [dict(row) for row in rows], total

    async def create_product_category(
        self, site_id: UUID, payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self._call(
            self._create_product_category_sync, _uuid(site_id), payload,
        )

    def _create_product_category_sync(
        self, site_id: str, payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        name = str(payload["name"]).strip()
        now = _iso(datetime.now(UTC)); category_id = str(uuid4())
        with self.database.transaction(immediate=True) as conn:
            site = conn.execute(
                "SELECT name FROM sites WHERE id=? AND status!='archived'", (site_id,),
            ).fetchone()
            if site is None:
                return None
            duplicate = conn.execute(
                "SELECT 1 FROM product_categories WHERE site_id=? AND name=? COLLATE NOCASE",
                (site_id, name),
            ).fetchone()
            if duplicate:
                raise ValueError("A category with this name already exists for the site")
            conn.execute(
                """INSERT INTO product_categories(id,site_id,name,sort_order,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (category_id, site_id, name, int(payload.get("sort_order", 0)), now, now),
            )
            return {
                "id": category_id, "site_id": site_id, "site_name": site["name"],
                "name": name, "sort_order": int(payload.get("sort_order", 0)),
                "product_count": 0, "created_at": now, "updated_at": now,
            }

    async def update_product_category(
        self, category_id: UUID, payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self._call(
            self._update_product_category_sync, _uuid(category_id), payload,
        )

    def _update_product_category_sync(
        self, category_id: str, payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        with self.database.transaction(immediate=True) as conn:
            current = conn.execute(
                "SELECT * FROM product_categories WHERE id=?", (category_id,),
            ).fetchone()
            if current is None:
                return None
            name = str(payload.get("name", current["name"])).strip()
            if conn.execute(
                """SELECT 1 FROM product_categories
                   WHERE site_id=? AND id!=? AND name=? COLLATE NOCASE""",
                (current["site_id"], category_id, name),
            ).fetchone():
                raise ValueError("A category with this name already exists for the site")
            now = _iso(datetime.now(UTC))
            conn.execute(
                "UPDATE product_categories SET name=?,sort_order=?,updated_at=? WHERE id=?",
                (name, int(payload.get("sort_order", current["sort_order"])), now, category_id),
            )
            row = conn.execute(
                """SELECT pc.*,s.name site_name,
                          (SELECT COUNT(*) FROM products p WHERE p.category_id=pc.id) product_count
                   FROM product_categories pc JOIN sites s ON s.id=pc.site_id WHERE pc.id=?""",
                (category_id,),
            ).fetchone()
            return dict(row) if row else None

    async def delete_product_category(self, category_id: UUID) -> bool:
        return await self._call(self._delete_product_category_sync, _uuid(category_id))

    def _delete_product_category_sync(self, category_id: str) -> bool:
        with self.database.transaction(immediate=True) as conn:
            return conn.execute(
                "DELETE FROM product_categories WHERE id=?", (category_id,),
            ).rowcount == 1

    async def assign_product_category(
        self, product_id: UUID, category_id: UUID | None, *, automatic: bool = False,
    ) -> dict[str, Any] | None:
        return await self._call(
            self._assign_product_category_sync, _uuid(product_id),
            _uuid(category_id) if category_id else None, automatic,
        )

    def _assign_product_category_sync(
        self, product_id: str, category_id: str | None, automatic: bool,
    ) -> dict[str, Any] | None:
        with self.database.transaction(immediate=True) as conn:
            product = conn.execute(
                "SELECT site_id,scraped_category_id FROM products WHERE id=?", (product_id,),
            ).fetchone()
            if product is None:
                return None
            if category_id and not automatic:
                category = conn.execute(
                    "SELECT site_id FROM product_categories WHERE id=?", (category_id,),
                ).fetchone()
                if category is None:
                    raise LookupError("Product category not found")
                if category["site_id"] != product["site_id"]:
                    raise ValueError("A product can only use a category from the same site")
            effective_category_id = product["scraped_category_id"] if automatic else category_id
            conn.execute(
                """UPDATE products
                   SET category_id=?,category_overridden=?,updated_at=? WHERE id=?""",
                (
                    effective_category_id,
                    0 if automatic else 1,
                    _iso(datetime.now(UTC)),
                    product_id,
                ),
            )
        return self._get_product_sync(product_id)

    @staticmethod
    def _normalize_product(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"], "site_id": row["site_id"], "site_name": row.get("site_name"),
            "category_id": row.get("category_id"), "category_name": row.get("category_name"),
            "scraped_category_id": row.get("scraped_category_id"),
            "scraped_category_name": row.get("scraped_category_name"),
            "category_overridden": bool(row.get("category_overridden", 0)),
            "external_key": row.get("external_key"), "name": row["name"], "url": row.get("url"),
            "price": row.get("price"), "original_price": row.get("original_price"),
            "stock": row.get("stock"), "availability_status": row.get("availability_status"),
            "buy_endpoint_id": row.get("buy_endpoint_id"),
            "availability_reason": row.get("availability_reason"), "description": row.get("description"),
            "raw_tags": _loads(row.get("raw_tags_json"), []),
            "updated_at": row.get("updated_at"), "last_observed_at": row.get("last_observed_at"),
        }

    async def list_events(self, *, page: int, page_size: int, site_id: UUID | None = None,
                          unread: bool | None = None, dismissed: bool | None = None,
                          severity: str | None = None, inbox: bool = False):
        return await self._call(
            self._list_events_sync, page, page_size, _uuid(site_id) if site_id else None,
            unread, dismissed, severity, inbox,
        )

    def _list_events_sync(self, page: int, page_size: int, site_id: str | None,
                          unread: bool | None, dismissed: bool | None, severity: str | None,
                          inbox: bool):
        clauses, params = [], []
        if site_id:
            clauses.append("(e.entity_type='site' AND e.entity_id=? OR c.site_id=?)"); params.extend([site_id, site_id])
        if unread is True:
            clauses.append("es.read_at IS NULL")
        if dismissed is True:
            clauses.append("es.dismissed_at IS NOT NULL")
        elif dismissed is False:
            clauses.append("es.dismissed_at IS NULL")
        if inbox:
            clauses.append("e.severity IN ('warning','danger')")
        if severity:
            clauses.append("e.severity=?"); params.append("danger" if severity == "error" else severity)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        join = """ FROM events e LEFT JOIN event_state es ON es.event_id=e.id
                    LEFT JOIN runs r ON r.id=e.run_id
                    LEFT JOIN capability_instances c ON c.id=r.capability_instance_id"""
        with self.database.connect() as conn:
            total = int(conn.execute("SELECT COUNT(*)" + join + where, params).fetchone()[0])
            rows = conn.execute(
                "SELECT e.*,es.read_at,es.dismissed_at" + join + where +
                " ORDER BY e.occurred_at DESC,e.id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["message"] = item.get("detail") or ""
                item["payload_json"] = _loads(item.get("payload_json"), {})
                if item.get("severity") == "danger":
                    item["severity"] = "error"
                result.append(item)
            return result, total

    async def update_event(self, event_id: UUID, *, read: bool | None = None,
                           dismissed: bool | None = None) -> dict[str, Any] | None:
        return await self._call(self._update_event_sync, _uuid(event_id), read, dismissed)

    def _update_event_sync(self, event_id: str, read: bool | None, dismissed: bool | None):
        now = _iso(datetime.now(UTC))
        with self.database.transaction() as conn:
            event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if not event:
                return None
            conn.execute(
                """INSERT OR IGNORE INTO event_state(event_id,updated_at) VALUES(?,?)""", (event_id, now)
            )
            if read is not None:
                conn.execute("UPDATE event_state SET read_at=?,updated_at=? WHERE event_id=?", (now if read else None, now, event_id))
            if dismissed is not None:
                conn.execute("UPDATE event_state SET dismissed_at=?,updated_at=? WHERE event_id=?", (now if dismissed else None, now, event_id))
            state = conn.execute("SELECT * FROM event_state WHERE event_id=?", (event_id,)).fetchone()
            result = dict(event); result.update(dict(state)); result["message"] = result.get("detail") or ""
            result["payload_json"] = _loads(result.get("payload_json"), {})
            return result

    async def mark_all_events_read(self, *, severity: str | None = None, inbox: bool = False) -> int:
        return await self._call(self._mark_all_events_read_sync, severity, inbox)

    def _mark_all_events_read_sync(self, severity: str | None, inbox: bool) -> int:
        now = _iso(datetime.now(UTC))
        stored_severity = "danger" if severity == "error" else severity
        clauses: list[str] = []
        params: list[Any] = []
        if inbox:
            clauses.append("severity IN ('warning','danger')")
        if stored_severity:
            clauses.append("severity=?"); params.append(stored_severity)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.transaction() as conn:
            rows = conn.execute("SELECT id FROM events" + where, params).fetchall()
            for row in rows:
                conn.execute(
                    """INSERT INTO event_state(event_id,read_at,updated_at) VALUES(?,?,?)
                       ON CONFLICT(event_id) DO UPDATE SET read_at=excluded.read_at,updated_at=excluded.updated_at""",
                    (row["id"], now, now),
                )
            return len(rows)

    async def list_runs(self, *, page: int, page_size: int, status: str | None = None):
        return await self._call(self._list_runs_sync, page, page_size, status)

    def _list_runs_sync(self, page: int, page_size: int, status: str | None):
        where = " WHERE r.status=?" if status else ""
        params = [status] if status else []
        select = """SELECT r.*,COALESCE(j.site_id,c.site_id) site_id,
                           COALESCE(j.kind,CASE c.code WHEN 'checkin' THEN 'checkin'
                                                     WHEN 'product_monitor' THEN 'product_sync'
                                                     ELSE c.code END) kind,
                           s.name site_name,a.label account_label
                    FROM runs r LEFT JOIN jobs j ON j.id=r.job_id
                    LEFT JOIN capability_instances c ON c.id=r.capability_instance_id
                    LEFT JOIN sites s ON s.id=COALESCE(j.site_id,c.site_id)
                    LEFT JOIN site_accounts a ON a.id=COALESCE(r.account_id,j.account_id)"""
        with self.database.connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM runs r" + where, params).fetchone()[0])
            rows = conn.execute(
                select + where + " ORDER BY r.started_at DESC,r.id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            return [self._normalize_run(dict(row)) for row in rows], total

    async def get_run(self, run_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._get_run_sync, _uuid(run_id))

    async def get_run_legacy_detail(self, run_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._run_legacy_detail_sync, _uuid(run_id))

    def _run_legacy_detail_sync(self, run_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute("SELECT result_json FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        secret_id = _loads(row["result_json"], {}).get("legacy_message_secret_id")
        if not secret_id:
            return {"message": None, "available": False}
        if self.secret_service is None:
            raise RuntimeError("encrypted legacy detail cannot be decrypted")
        return {"message": self.secret_service.reveal(secret_id).decode("utf-8"), "available": True}

    def _get_run_sync(self, run_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                """SELECT r.*,COALESCE(j.site_id,c.site_id) site_id,
                          COALESCE(j.kind,CASE c.code WHEN 'checkin' THEN 'checkin'
                                                    WHEN 'product_monitor' THEN 'product_sync'
                                                    ELSE c.code END) kind,
                          s.name site_name,a.label account_label
                   FROM runs r LEFT JOIN jobs j ON j.id=r.job_id
                   LEFT JOIN capability_instances c ON c.id=r.capability_instance_id
                   LEFT JOIN sites s ON s.id=COALESCE(j.site_id,c.site_id)
                   LEFT JOIN site_accounts a ON a.id=COALESCE(r.account_id,j.account_id)
                   WHERE r.id=?""",
                (run_id,),
            ).fetchone()
            return self._normalize_run(dict(row)) if row else None

    @staticmethod
    def _normalize_run(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["result_json"] = _loads(result.get("result_json"), {})
        secret_id = result["result_json"].pop("legacy_message_secret_id", None)
        result["has_legacy_detail"] = bool(secret_id)
        return result

    # -- Encrypted secret binding ------------------------------------
    def _protect_url(self, url: str, purpose: str, *, conn: sqlite3.Connection | None = None) -> tuple[str, str | None, tuple[str, ...]]:
        if "?" not in url and "#" not in url:
            return url, None, ()
        if self.secret_service is None:
            raise RuntimeError("encrypted URL storage is unavailable; query and fragment were not persisted")
        from site_hub.services.secrets import split_sensitive_url
        protected = split_sensitive_url(url, self.secret_service, purpose, conn=conn)
        return protected.url_base, protected.suffix_secret_id, protected.query_keys

    @staticmethod
    def _split_secrets(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        sensitive = (
            "password", "passwd", "token", "secret", "cookie", "authorization",
            "api_key", "api-key", "apikey", "session", "webhook", "credential",
        )
        secrets: dict[str, str] = {}
        def sensitive_value(value: Any) -> bool:
            if not isinstance(value, str):
                return False
            if re.match(r"(?i)^\s*(?:bearer|basic)\s+\S+", value):
                return True
            if re.search(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", value):
                return True
            try:
                parsed = urlsplit(value)
            except ValueError:
                return False
            if parsed.scheme.casefold() in {"http", "https"} and parsed.netloc:
                if parsed.username is not None or parsed.password is not None:
                    return True
                if parsed.query or parsed.fragment:
                    return True
            try:
                structured = json.loads(value)
            except (TypeError, ValueError):
                return False
            if not isinstance(structured, (dict, list)):
                return False
            _, nested = DataLayerStore._split_secrets(structured)
            return bool(nested)

        def remember(current: tuple[str, ...], child: Any) -> None:
            if isinstance(child, (dict, list)):
                raise ValueError(f"secret config field {'.'.join(current)} must be a scalar value")
            if child not in (None, ""):
                secrets[".".join(current)] = str(child)

        def walk(value: Any, path: tuple[str, ...]) -> Any:
            if isinstance(value, dict):
                result: dict[str, Any] = {}
                for key, child in value.items():
                    current = (*path, str(key))
                    if any(marker in str(key).casefold() for marker in sensitive) or sensitive_value(child):
                        remember(current, child)
                        continue
                    result[str(key)] = walk(child, current)
                return result
            if isinstance(value, list):
                result: list[Any] = []
                for index, child in enumerate(value):
                    current = (*path, str(index))
                    if sensitive_value(child):
                        remember(current, child)
                        result.append(None)
                    else:
                        result.append(walk(child, current))
                return result
            return value
        return walk(config, ()), secrets

    def _update_capability_atomic(
        self, capability_id: str, *, owner: str, account_id: str | None,
        changes: dict[str, Any], config: dict[str, Any] | None,
        config_supplied: bool, clear_secrets: set[str],
        conn: sqlite3.Connection | None = None,
        obsolete_secret_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Update config, encrypted bindings and capability metadata as one write."""
        if conn is None:
            deferred: set[str] = set()
            with self.database.transaction(immediate=True) as owned:
                result = self._update_capability_atomic(
                    capability_id, owner=owner, account_id=account_id,
                    changes=changes, config=config, config_supplied=config_supplied,
                    clear_secrets=clear_secrets, conn=owned,
                    obsolete_secret_ids=deferred,
                )
            if self.secret_service:
                for secret_id in deferred:
                    self.secret_service.delete_if_unreferenced(secret_id)
            return result
        current = conn.execute(
            "SELECT * FROM capability_instances WHERE id=?", (capability_id,)
        ).fetchone()
        if current is None:
            raise RuntimeError("capability no longer exists")
        updates = dict(changes)
        if config_supplied:
            clean, secrets = self._split_secrets(config or {})
            previous = set(_loads(current["config_json"], {}).get("_secret_fields", []))
            cleared = clear_secrets - secrets.keys()
            self._clear_secrets(
                account_id, capability_id, cleared, conn=conn,
                obsolete_secret_ids=obsolete_secret_ids,
            )
            self._store_secrets(owner, account_id, capability_id, secrets, conn=conn)
            clean["_secret_fields"] = sorted((previous | secrets.keys()) - cleared)
            updates["config_json"] = clean
        elif clear_secrets:
            existing = _loads(current["config_json"], {})
            self._clear_secrets(
                account_id, capability_id, clear_secrets, conn=conn,
                obsolete_secret_ids=obsolete_secret_ids,
            )
            existing["_secret_fields"] = sorted(
                set(existing.get("_secret_fields", [])) - clear_secrets
            )
            updates["config_json"] = existing
        allowed = {"endpoint_id", "adapter", "config_version", "config_json", "enabled"}
        fields: list[str] = []
        values: list[Any] = []
        for key, value in updates.items():
            if key not in allowed:
                raise ValueError(f"unsupported capability field: {key}")
            if key == "config_json" and not isinstance(value, str):
                value = _dumps(value)
            if key == "enabled":
                value = int(bool(value))
            fields.append(f"{key}=?")
            values.append(value)
        if fields:
            fields.append("updated_at=?")
            values.extend([_iso(datetime.now(UTC)), capability_id])
            conn.execute(
                f"UPDATE capability_instances SET {','.join(fields)} WHERE id=?", values
            )
        return dict(conn.execute(
            "SELECT * FROM capability_instances WHERE id=?", (capability_id,)
        ).fetchone())

    def _store_secrets(self, owner: str, account_id: str | None, capability_id: str,
                       values: dict[str, str], *, conn: sqlite3.Connection | None = None) -> None:
        if not values:
            return
        if self.secret_service is None:
            raise RuntimeError("encrypted secret storage is unavailable; secret values were not persisted")
        if conn is None:
            with self.database.transaction(immediate=True) as owned:
                self._store_secrets(
                    owner, account_id, capability_id, values, conn=owned
                )
            return
        for name, plaintext in values.items():
            existing = conn.execute(
                "SELECT secret_id FROM capability_secrets WHERE capability_instance_id=? AND name=?",
                (capability_id, name),
            ).fetchone()
            if existing:
                secret_id = existing["secret_id"]
                self.secret_service.rotate(secret_id, plaintext, conn=conn)
            else:
                secret_id = self.secret_service.store(f"{owner}:{name}", plaintext, conn=conn)
                self.secret_service.bind_capability(capability_id, name, secret_id, conn=conn)
            if account_id:
                self.secret_service.bind_account(account_id, name, secret_id, conn=conn)

    def _clear_secrets(self, account_id: str | None, capability_id: str,
                       names: set[str], *, conn: sqlite3.Connection | None = None,
                       obsolete_secret_ids: set[str] | None = None) -> None:
        if not names:
            return
        if conn is None:
            deferred: set[str] = set()
            with self.database.transaction(immediate=True) as owned:
                self._clear_secrets(
                    account_id, capability_id, names, conn=owned,
                    obsolete_secret_ids=deferred,
                )
            if self.secret_service:
                for secret_id in deferred:
                    self.secret_service.delete_if_unreferenced(secret_id)
            return
        secret_ids: set[str] = set()
        placeholders = ",".join("?" for _ in names)
        params = [capability_id, *sorted(names)]
        secret_ids.update(row["secret_id"] for row in conn.execute(
            f"SELECT secret_id FROM capability_secrets WHERE capability_instance_id=? AND name IN ({placeholders})",
            params,
        ))
        conn.execute(
            f"DELETE FROM capability_secrets WHERE capability_instance_id=? AND name IN ({placeholders})", params
        )
        if account_id:
            account_params = [account_id, *sorted(names)]
            secret_ids.update(row["secret_id"] for row in conn.execute(
                f"SELECT secret_id FROM account_secrets WHERE account_id=? AND name IN ({placeholders})",
                account_params,
            ))
            conn.execute(
                f"DELETE FROM account_secrets WHERE account_id=? AND name IN ({placeholders})", account_params
            )
        if obsolete_secret_ids is not None:
            obsolete_secret_ids.update(secret_ids)
        else:
            for secret_id in secret_ids:
                try:
                    conn.execute("DELETE FROM secrets WHERE id=?", (secret_id,))
                except sqlite3.IntegrityError:
                    # Another entity still references this encrypted value.
                    pass

    def _restore_secrets(self, capability_id: str | None, config: dict[str, Any],
                         names: list[str]) -> dict[str, Any]:
        result = dict(config)
        if not capability_id or not names or self.secret_service is None:
            return result
        with self.database.connect() as conn:
            refs = {row["name"]: row["secret_id"] for row in conn.execute(
                "SELECT name,secret_id FROM capability_secrets WHERE capability_instance_id=?", (capability_id,)
            )}
        for name in names:
            secret_id = refs.get(name)
            if secret_id:
                self._set_path(result, name, self.secret_service.reveal(secret_id).decode("utf-8"))
        return result

    @staticmethod
    def _set_path(target: dict[str, Any], dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        current: Any = target
        for index, part in enumerate(parts[:-1]):
            next_is_index = parts[index + 1].isdigit()
            if isinstance(current, list):
                position = int(part)
                while len(current) <= position:
                    current.append([] if next_is_index else {})
                current = current[position]
            else:
                if part not in current or not isinstance(current[part], (dict, list)):
                    current[part] = [] if next_is_index else {}
                current = current[part]
        final = parts[-1]
        if isinstance(current, list):
            position = int(final)
            while len(current) <= position:
                current.append(None)
            current[position] = value
        else:
            current[final] = value

    @staticmethod
    def _schedule_policy(override: Any | None) -> tuple[str, dict[str, Any]]:
        if hasattr(override, "model_dump"):
            override = override.model_dump(exclude_none=True)
        override = dict(override or {})
        if not override:
            return "daily_random_window", {}
        if override.get("mode") == "fixed_time":
            clock = str(override["time"])
            hour, minute, *_ = clock.split(":")
            return "cron", {
                "mode": "fixed_time", "time": clock, "timezone": override.get("timezone", "Asia/Shanghai"),
                "expression": f"{int(minute)} {int(hour)} * * *",
            }
        return "daily_random_window", {
            "mode": "custom_window", "start": override["start"], "end": override["end"],
            "timezone": override.get("timezone", "Asia/Shanghai"),
        }
