from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

from site_hub.services.ports import SiteTypeConflict


from site_hub.services.store_support import _iso, _moment, _loads, _dumps, _uuid


class SiteStoreMixin:
    """Sites domain operations; transactions remain owned by each operation."""

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
        now = _iso(self._now())
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
        now = _iso(self._now())
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

    async def list_sites(self, *, page: int, page_size: int, q: str | None = None,
                         folder_id: str | None = None, tag_id: UUID | None = None,
                         site_type_id: UUID | None = None):
        return await self._call(self._list_sites_sync, page, page_size, q, folder_id, tag_id, site_type_id)

    def _list_sites_sync(self, page, page_size, q, folder_id, tag_id, site_type_id):
        clauses, params = ["s.status!='archived'"], []
        if q:
            clauses.append("(instr(casefold(s.name),casefold(?))>0 OR instr(casefold(COALESCE((SELECT e.url_base FROM site_endpoints e WHERE e.site_id=s.id ORDER BY e.is_default DESC,e.sort_order,e.id LIMIT 1),s.origin)),casefold(?))>0)")
            params.extend([q, q])
        if folder_id is not None:
            if folder_id == "null":
                clauses.append("(s.folder_id IS NULL OR s.folder_id=?)")
                params.append(self.ROOT_FOLDER_ID)
            else:
                clauses.append("s.folder_id=?")
                params.append(str(folder_id))
        if tag_id is not None:
            clauses.append("EXISTS(SELECT 1 FROM site_tags t WHERE t.site_id=s.id AND t.tag_id=?)")
            params.append(str(tag_id))
        if site_type_id is not None:
            clauses.append("s.site_type_id=?")
            params.append(str(site_type_id))
        where = " WHERE " + " AND ".join(clauses)
        with self.database.connect() as conn:
            conn.execute("BEGIN")
            total = conn.execute("SELECT COUNT(*) FROM sites s" + where, params).fetchone()[0]
            rows = conn.execute("""SELECT s.*,
                COALESCE((SELECT e.url_base FROM site_endpoints e WHERE e.site_id=s.id
                    ORDER BY e.is_default DESC,e.sort_order,e.id LIMIT 1),s.origin) public_url,
                (SELECT e.id FROM site_endpoints e WHERE e.site_id=s.id ORDER BY e.is_default DESC,e.sort_order,e.id LIMIT 1) default_endpoint_id,
                (SELECT COUNT(*) FROM site_accounts a WHERE a.site_id=s.id AND a.auth_state!='disabled') account_count,
                (SELECT COUNT(*) FROM products p WHERE p.site_id=s.id) product_count
                FROM sites s""" + where + " ORDER BY s.pinned DESC,s.sort_order,s.id LIMIT ? OFFSET ?",
                [*params, page_size, (page-1)*page_size]).fetchall()
            return [self._normalize_site(conn, dict(row)) for row in rows], total

    async def get_site_directory_stats(self) -> dict[str, Any]:
        return await self._call(self._get_site_directory_stats_sync)

    def _get_site_directory_stats_sync(self) -> dict[str, Any]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """SELECT folder_id,COUNT(*) AS site_count
                   FROM sites
                   WHERE status!='archived'
                   GROUP BY folder_id"""
            ).fetchall()
        total = 0
        unfiled = 0
        by_folder: dict[str, int] = {}
        for row in rows:
            count = int(row["site_count"])
            total += count
            folder_id = row["folder_id"]
            if folder_id is None or str(folder_id) == self.ROOT_FOLDER_ID:
                unfiled += count
            else:
                by_folder[str(folder_id)] = count
        return {"total": total, "unfiled": unfiled, "by_folder": by_folder}

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
            "visit_count": int(row.get("visit_count") or 0),
            "last_visited_at": row.get("last_visited_at"),
            "overview_hidden": bool(row.get("overview_hidden", 0)),
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
        now = _iso(self._now())
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
            "overview_hidden",
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
        now = _iso(self._now())
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
                    if key in {"pinned", "overview_hidden"}:
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

    async def list_overview_sites(self, *, limit: int = 8) -> dict[str, Any]:
        return await self._call(self._list_overview_sites_sync, limit)

    def _list_overview_sites_sync(self, limit: int) -> dict[str, Any]:
        limit = max(1, min(int(limit), 24))
        now = self._now()
        with self.database.connect() as conn:
            has_usage = conn.execute(
                """SELECT 1 FROM sites
                   WHERE visit_count!=0 OR last_visited_at IS NOT NULL
                   LIMIT 1"""
            ).fetchone() is not None
        rows = [
            row for row in self._site_rows()
            if row.get("status") not in {"archived", "disabled"}
            and not bool(row.get("overview_hidden"))
        ]

        def moment(value: Any) -> datetime | None:
            if value in (None, ""):
                return None
            try:
                return _moment(value)
            except (TypeError, ValueError):
                return None

        def created_timestamp(row: dict[str, Any]) -> float:
            value = moment(row.get("created_at"))
            return value.timestamp() if value is not None else 0.0

        def visited_timestamp(row: dict[str, Any]) -> float:
            value = moment(row.get("last_visited_at"))
            return value.timestamp() if value is not None else 0.0

        def score(row: dict[str, Any]) -> float:
            count = max(0, int(row.get("visit_count") or 0))
            visited = moment(row.get("last_visited_at"))
            if count == 0 or visited is None:
                return 0.0
            age_days = max(0.0, (now - visited).total_seconds() / 86400.0)
            return math.log1p(count) * (0.5 ** (age_days / 30.0))

        pinned = [row for row in rows if bool(row.get("pinned"))]
        unpinned_used = [
            row for row in rows
            if not bool(row.get("pinned"))
            and (int(row.get("visit_count") or 0) > 0 or row.get("last_visited_at"))
        ]
        starters = [
            row for row in rows
            if not bool(row.get("pinned"))
            and int(row.get("visit_count") or 0) == 0
            and not row.get("last_visited_at")
        ]
        pinned_by_frequency = sorted(
            pinned,
            key=lambda row: (
                score(row), visited_timestamp(row), created_timestamp(row), str(row["id"]),
            ),
            reverse=True,
        )
        frequent_rows = (
            pinned_by_frequency
            + sorted(
                unpinned_used,
                key=lambda row: (
                    score(row), visited_timestamp(row), created_timestamp(row), str(row["id"]),
                ),
                reverse=True,
            )
            + sorted(
                starters,
                key=lambda row: (created_timestamp(row), str(row["id"])),
                reverse=True,
            )
        )[:limit]
        recent_rows = sorted(
            (row for row in rows if row.get("last_visited_at")),
            key=lambda row: (
                visited_timestamp(row), created_timestamp(row), str(row["id"]),
            ),
            reverse=True,
        )[:limit]

        def recommend(row: dict[str, Any], reason: str) -> dict[str, Any]:
            result = dict(row)
            result["recommendation_reason"] = (
                "pinned" if bool(row.get("pinned")) else reason
            )
            return result

        return {
            "frequent": [
                recommend(
                    row,
                    "frequent" if int(row.get("visit_count") or 0) > 0 else "starter",
                )
                for row in frequent_rows
            ],
            "recent": [
                {**row, "recommendation_reason": "recent"} for row in recent_rows
            ],
            "has_usage": has_usage,
        }

    async def record_site_visit(self, site_id: UUID) -> bool:
        return await self._call(self._record_site_visit_sync, _uuid(site_id))

    def _record_site_visit_sync(self, site_id: str) -> bool:
        now = _iso(self._now())
        with self.database.transaction(immediate=True) as conn:
            changed = conn.execute(
                """UPDATE sites
                   SET visit_count=visit_count+1,last_visited_at=?,
                       row_version=row_version+1,updated_at=?
                   WHERE id=? AND status!='archived'""",
                (now, now, site_id),
            ).rowcount
        return bool(changed)

    async def reset_site_usage(self) -> int:
        return await self._call(self._reset_site_usage_sync)

    def _reset_site_usage_sync(self) -> int:
        now = _iso(self._now())
        with self.database.transaction(immediate=True) as conn:
            return int(conn.execute(
                """UPDATE sites
                   SET visit_count=0,last_visited_at=NULL,
                       row_version=row_version+1,updated_at=?
                   WHERE visit_count!=0 OR last_visited_at IS NOT NULL""",
                (now,),
            ).rowcount)

    async def delete_site(self, site_id: UUID) -> bool:
        return await self._call(self._archive_site_sync, _uuid(site_id))

    def _archive_site_sync(self, site_id: str) -> bool:
        now = _iso(self._now())
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
                    [*values, _iso(self._now()), folder_id],
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
                         (parent_id, _iso(self._now()), folder_id))
            conn.execute("UPDATE sites SET folder_id=?,updated_at=? WHERE folder_id=?",
                         (parent_id or self.ROOT_FOLDER_ID, _iso(self._now()), folder_id))
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
        now = _iso(self._now())
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
