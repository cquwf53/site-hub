from __future__ import annotations

import asyncio
import math
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from site_hub.db.connection import stable_uuid
from site_hub.db.job_queries import normalize_job_statuses
from site_hub.scrapers.network import sanitize_log_value
from site_hub.services.ports import AccountNotReady, SiteTypeConflict


def _now() -> datetime:
    return datetime.now(UTC)


def _moment(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _public_config(value: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Mirror the public-store contract without retaining test secrets."""
    markers = (
        "password", "passwd", "token", "secret", "cookie", "authorization",
        "api_key", "api-key", "apikey", "session", "webhook", "credential",
    )
    secret_fields: list[str] = []

    def walk(item: Any, path: tuple[str, ...]) -> Any:
        if isinstance(item, dict):
            result: dict[str, Any] = {}
            for raw_key, child in item.items():
                key = str(raw_key)
                current = (*path, key)
                if any(marker in key.casefold() for marker in markers):
                    if child not in (None, ""):
                        secret_fields.append(".".join(current))
                    continue
                result[key] = walk(child, current)
            return result
        if isinstance(item, list):
            return [walk(child, (*path, str(index))) for index, child in enumerate(item)]
        return item

    return walk(value, ()), sorted(secret_fields)


class MemoryStore:
    """Reference Store used by tests and as a safe degraded-mode fallback.

    Production uses the data-layer adapter; keeping the same contract makes the
    control plane testable without importing or mutating data-layer internals.
    """

    def __init__(self) -> None:
        self.ROOT_FOLDER_ID = UUID("00000000-0000-0000-0000-000000000001")
        self._lock = asyncio.Lock()
        now = _now().isoformat()
        self.folders: dict[UUID, dict[str, Any]] = {
            self.ROOT_FOLDER_ID: {
                "id": self.ROOT_FOLDER_ID, "parent_id": None, "name": "Sites",
                "sort_order": 0, "created_at": now, "updated_at": now,
            }
        }
        self.sites: dict[UUID, dict[str, Any]] = {}
        self.site_types: dict[UUID, dict[str, Any]] = {}
        for name, slug, color, icon, sort_order, is_default in (
            ("其他", "other", "#64748b", "globe", 900, True),
            ("商城", "shop", "#4f46e5", "shopping-bag", 10, False),
            ("签到", "checkin", "#059669", "calendar-check", 20, False),
            ("工具", "tool", "#d97706", "wrench", 30, False),
            ("书签", "bookmark", "#0284c7", "bookmark", 40, False),
        ):
            identifier = UUID(stable_uuid(f"site-type:{slug}"))
            self.site_types[identifier] = {
                "id": identifier, "name": name, "slug": slug, "color": color,
                "icon": icon, "sort_order": sort_order, "enabled": True,
                "is_system": True, "is_default": is_default,
                "created_at": now, "updated_at": now,
            }
        self.accounts: dict[UUID, dict[str, Any]] = {}
        self.product_settings: dict[UUID, dict[str, Any]] = {}
        self.product_schedules: dict[UUID, dict[str, Any]] = {}
        self.product_categories: dict[UUID, dict[str, Any]] = {}
        self.products: dict[UUID, dict[str, Any]] = {}
        self.price_observations: dict[UUID, dict[str, Any]] = {}
        self.jobs: dict[UUID, dict[str, Any]] = {}
        self.batches: dict[str, dict[str, Any]] = {}
        self.batch_items: dict[UUID, list[dict[str, Any]]] = {}
        self.artifacts: dict[UUID, dict[str, Any]] = {}
        self.settings: dict[str, str] = {
            "scheduler.timezone": "Asia/Shanghai",
            "scheduler.checkin_window_start": "08:00:00",
            "scheduler.checkin_window_end": "22:00:00",
            "scheduler.product_interval_minutes": "60",
        }

    async def initialize(self) -> None: pass
    async def close(self) -> None: pass
    async def ping(self) -> bool: return True

    def _resolve_site_type(self, *, site_type_id=None, kind=None, require_enabled=True):
        normalized_kind = str(kind).strip().lower() if kind is not None else None
        row = None
        if site_type_id is not None:
            row = self.site_types.get(UUID(str(site_type_id)))
            if row is not None and normalized_kind is not None and row["slug"] != normalized_kind:
                raise SiteTypeConflict(
                    "site_type_id and kind refer to different site types",
                    code="SITE_TYPE_MISMATCH",
                )
        else:
            wanted = normalized_kind or "other"
            row = next((item for item in self.site_types.values() if item["slug"] == wanted), None)
        if row is None:
            raise SiteTypeConflict("Site type not found", code="SITE_TYPE_NOT_FOUND")
        if require_enabled and not row["enabled"]:
            raise SiteTypeConflict("Site type is disabled", code="SITE_TYPE_DISABLED")
        return row

    def _public_site_type(self, row):
        result = deepcopy(row)
        result["site_count"] = sum(
            1 for site in self.sites.values()
            if UUID(str(site.get("site_type_id"))) == UUID(str(row["id"]))
        )
        return result

    def _public_site(self, row):
        result = deepcopy(row)
        result.setdefault("visit_count", 0)
        result.setdefault("last_visited_at", None)
        result.setdefault("overview_hidden", False)
        type_row = self.site_types.get(UUID(str(result["site_type_id"])))
        result["site_type"] = self._public_site_type(type_row) if type_row else None
        return result

    async def list_site_types(self, *, enabled=None):
        rows = list(self.site_types.values())
        if enabled is not None:
            rows = [row for row in rows if row["enabled"] is bool(enabled)]
        rows.sort(key=lambda row: (
            not row["enabled"], row["sort_order"], row["name"].casefold(), str(row["id"]),
        ))
        return [self._public_site_type(row) for row in rows]

    async def get_site_type(self, site_type_id):
        row = self.site_types.get(UUID(str(site_type_id)))
        return self._public_site_type(row) if row else None

    async def create_site_type(self, payload):
        name = str(payload["name"]).strip()
        slug = str(payload["slug"]).strip().lower()
        if any(row["name"].casefold() == name.casefold() or row["slug"] == slug
               for row in self.site_types.values()):
            raise SiteTypeConflict("A site type with this name or slug already exists")
        identifier = uuid4(); now = _now().isoformat()
        row = {
            "id": identifier, "name": name, "slug": slug,
            "color": str(payload.get("color", "#64748b")).lower(),
            "icon": str(payload.get("icon", "globe")).strip(),
            "sort_order": int(payload.get("sort_order", 0)),
            "enabled": bool(payload.get("enabled", True)),
            "is_system": False, "is_default": False,
            "created_at": now, "updated_at": now,
        }
        self.site_types[identifier] = row
        return self._public_site_type(row)

    async def update_site_type(self, site_type_id, payload):
        identifier = UUID(str(site_type_id)); row = self.site_types.get(identifier)
        if row is None:
            return None
        values = dict(payload)
        if row["is_system"]:
            if "slug" in values and str(values["slug"]).strip().lower() != row["slug"]:
                raise SiteTypeConflict(
                    "Built-in site type slugs cannot be changed", code="SITE_TYPE_PROTECTED",
                )
            if values.get("enabled") is False:
                raise SiteTypeConflict(
                    "Built-in site types cannot be disabled", code="SITE_TYPE_PROTECTED",
                )
        next_name = str(values.get("name", row["name"])).strip()
        next_slug = str(values.get("slug", row["slug"])).strip().lower()
        if any(
            other_id != identifier and (
                other["name"].casefold() == next_name.casefold() or other["slug"] == next_slug
            ) for other_id, other in self.site_types.items()
        ):
            raise SiteTypeConflict("A site type with this name or slug already exists")
        old_slug = row["slug"]
        for key in ("name", "slug", "color", "icon", "sort_order", "enabled"):
            if key in values:
                row[key] = values[key]
        row["name"] = str(row["name"]).strip()
        row["slug"] = str(row["slug"]).strip().lower()
        row["color"] = str(row["color"]).lower()
        row["icon"] = str(row["icon"]).strip()
        row["sort_order"] = int(row["sort_order"])
        row["enabled"] = bool(row["enabled"])
        row["updated_at"] = _now().isoformat()
        if row["slug"] != old_slug:
            for site in self.sites.values():
                if UUID(str(site["site_type_id"])) == identifier:
                    site["kind"] = row["slug"]
                    site["updated_at"] = _now().isoformat()
        return self._public_site_type(row)

    async def delete_site_type(self, site_type_id):
        identifier = UUID(str(site_type_id)); row = self.site_types.get(identifier)
        if row is None:
            return False
        if row["is_system"]:
            raise SiteTypeConflict(
                "Built-in site types cannot be deleted", code="SITE_TYPE_PROTECTED",
            )
        if any(UUID(str(site["site_type_id"])) == identifier for site in self.sites.values()):
            raise SiteTypeConflict(
                "Move the assigned sites before deleting this type", code="SITE_TYPE_IN_USE",
            )
        del self.site_types[identifier]
        return True

    async def bulk_set_site_type(self, site_ids, site_type_id):
        type_row = self._resolve_site_type(site_type_id=site_type_id)
        identifiers = list(dict.fromkeys(UUID(str(item)) for item in site_ids))
        if any(item not in self.sites or self.sites[item].get("status") == "archived"
               for item in identifiers):
            raise SiteTypeConflict("One or more sites were not found", code="SITE_NOT_FOUND")
        now = _now().isoformat()
        for identifier in identifiers:
            self.sites[identifier].update(
                site_type_id=type_row["id"], kind=type_row["slug"], updated_at=now,
            )
        return {
            "updated": len(identifiers), "site_type_id": type_row["id"],
            "site_ids": identifiers,
        }

    async def list_sites(self, *, page: int, page_size: int, q: str | None = None, folder_id=None, tag_id=None, site_type_id=None):
        rows = [row for row in self.sites.values() if row.get("status") != "archived"]
        if q:
            needle = q.casefold()
            rows = [r for r in rows if needle in r["name"].casefold() or needle in r["url"].casefold()]
        if folder_id is not None: rows = [r for r in rows if str(r.get("folder_id")) == str(folder_id)]
        if tag_id is not None: rows = [r for r in rows if any(str(tag.get("id")) == str(tag_id) for tag in r.get("tags", []))]
        if site_type_id is not None: rows = [r for r in rows if str(r.get("site_type_id")) == str(site_type_id)]
        rows.sort(key=lambda r: r["id"])
        start = (page - 1) * page_size
        return [self._public_site(row) for row in rows[start:start + page_size]], len(rows)

    async def get_site_directory_stats(self):
        rows = [row for row in self.sites.values() if row.get("status") != "archived"]
        by_folder: dict[str, int] = {}
        unfiled = 0
        for row in rows:
            folder_id = row.get("folder_id")
            if folder_id is None or UUID(str(folder_id)) == self.ROOT_FOLDER_ID:
                unfiled += 1
            else:
                key = str(folder_id)
                by_folder[key] = by_folder.get(key, 0) + 1
        return {"total": len(rows), "unfiled": unfiled, "by_folder": by_folder}

    async def get_site(self, site_id):
        row=self.sites.get(UUID(str(site_id)))
        return self._public_site(row) if row and row.get("status")!="archived" else None

    async def get_site_runtime(self, site_id):
        return await self.get_site(site_id)

    async def create_site(self, payload):
        async with self._lock:
            site_id = uuid4()
            endpoint_id = uuid4()
            site_type = self._resolve_site_type(
                site_type_id=payload.get("site_type_id"), kind=payload.get("kind"),
            )
            row = {"id": site_id, "favicon_path": None, "tags": [], "checkin_account_count": 0,
                   "product_count": 0, "adapter": None, "config": {}, "configured": False,
                   "configured_secret_fields": [], "created_at": _now().isoformat(),
                   "updated_at": None, "folder_id": self.ROOT_FOLDER_ID,
                   "status": "active", "pinned": False, "visit_count": 0,
                   "last_visited_at": None, "overview_hidden": False,
                   "row_version": 1,
                   "default_endpoint_id": endpoint_id, **payload,
                   "site_type_id": site_type["id"], "kind": site_type["slug"]}
            row.pop("tag_ids", None)
            self.sites[site_id] = row
            if "product_monitor" in set(payload.get("capabilities") or []):
                self._ensure_product_schedule(site_id)
            return self._public_site(row)

    async def update_site(self, site_id, payload):
        site_id = UUID(str(site_id))
        if site_id not in self.sites: return None
        payload = dict(payload); payload.pop("tag_ids", None)
        allowed = {
            "folder_id", "name", "url", "origin", "canonical_host", "kind",
            "site_type_id", "capabilities", "description", "note", "icon",
            "favicon_path", "status", "pinned", "sort_order", "overview_hidden",
            "adapter", "config", "clear_secrets",
        }
        unsupported = set(payload) - allowed
        if unsupported:
            raise ValueError(f"unsupported site field: {sorted(unsupported)[0]}")
        if "site_type_id" in payload or "kind" in payload:
            if "site_type_id" in payload and payload["site_type_id"] is None:
                raise SiteTypeConflict("A site must have a primary type", code="SITE_TYPE_REQUIRED")
            if "kind" in payload and not str(payload["kind"] or "").strip():
                raise SiteTypeConflict("A site must have a primary type", code="SITE_TYPE_REQUIRED")
            site_type = self._resolve_site_type(
                site_type_id=payload.get("site_type_id") if "site_type_id" in payload else None,
                kind=payload.get("kind") if "kind" in payload else None,
                require_enabled=False,
            )
            if (
                not site_type["enabled"]
                and UUID(str(site_type["id"])) != UUID(str(self.sites[site_id]["site_type_id"]))
            ):
                raise SiteTypeConflict("Site type is disabled", code="SITE_TYPE_DISABLED")
            payload["site_type_id"] = site_type["id"]
            payload["kind"] = site_type["slug"]
        clear_secrets = set(payload.pop("clear_secrets", []) or [])
        if "config" in payload:
            clean, new_secrets = _public_config(payload["config"] or {})
            previous = set(self.sites[site_id].get("configured_secret_fields", []))
            payload["config"] = clean
            payload["configured_secret_fields"] = sorted((previous | set(new_secrets)) - clear_secrets)
            payload["configured"] = bool(clean or payload["configured_secret_fields"])
        elif clear_secrets:
            remaining = set(self.sites[site_id].get("configured_secret_fields", [])) - clear_secrets
            payload["configured_secret_fields"] = sorted(remaining)
            payload["configured"] = bool(self.sites[site_id].get("config") or remaining)
        for key in ("pinned", "overview_hidden"):
            if key in payload:
                payload[key] = bool(payload[key])
        self.sites[site_id].update(payload); self.sites[site_id]["updated_at"] = _now().isoformat()
        return self._public_site(self.sites[site_id])

    async def list_overview_sites(self, *, limit=8):
        limit = max(1, min(int(limit), 24))
        now = _now()
        rows = [
            self._public_site(row) for row in self.sites.values()
            if row.get("status", "active") not in {"archived", "disabled"}
            and not bool(row.get("overview_hidden", False))
        ]

        def moment(value):
            if value in (None, ""):
                return None
            try:
                return _moment(value)
            except (TypeError, ValueError):
                return None

        def timestamp(value):
            value = moment(value)
            return value.timestamp() if value is not None else 0.0

        def score(row):
            count = max(0, int(row.get("visit_count") or 0))
            visited = moment(row.get("last_visited_at"))
            if count == 0 or visited is None:
                return 0.0
            age_days = max(0.0, (now - visited).total_seconds() / 86400.0)
            return math.log1p(count) * (0.5 ** (age_days / 30.0))

        pinned = [row for row in rows if bool(row.get("pinned"))]
        used = [
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
        created_key = lambda row: (timestamp(row.get("created_at")), str(row["id"]))
        frequent = (
            sorted(
                pinned,
                key=lambda row: (score(row), timestamp(row.get("last_visited_at")), *created_key(row)),
                reverse=True,
            )
            + sorted(
                used,
                key=lambda row: (score(row), timestamp(row.get("last_visited_at")), *created_key(row)),
                reverse=True,
            )
            + sorted(starters, key=created_key, reverse=True)
        )[:limit]
        recent = sorted(
            (row for row in rows if row.get("last_visited_at")),
            key=lambda row: (timestamp(row.get("last_visited_at")), *created_key(row)),
            reverse=True,
        )[:limit]

        def recommend(row, reason):
            result = deepcopy(row)
            result["recommendation_reason"] = "pinned" if row.get("pinned") else reason
            return result

        return {
            "frequent": [
                recommend(row, "frequent" if int(row.get("visit_count") or 0) > 0 else "starter")
                for row in frequent
            ],
            "recent": [
                {**deepcopy(row), "recommendation_reason": "recent"} for row in recent
            ],
            "has_usage": any(
                int(row.get("visit_count") or 0) > 0 or row.get("last_visited_at")
                for row in self.sites.values()
            ),
        }

    async def record_site_visit(self, site_id):
        identifier = UUID(str(site_id))
        async with self._lock:
            row = self.sites.get(identifier)
            if row is None or row.get("status") == "archived":
                return False
            row["visit_count"] = int(row.get("visit_count") or 0) + 1
            row["last_visited_at"] = _now().isoformat()
            row["row_version"] = int(row.get("row_version") or 0) + 1
            row["updated_at"] = _now().isoformat()
            return True

    async def reset_site_usage(self):
        changed = 0
        async with self._lock:
            for row in self.sites.values():
                if int(row.get("visit_count") or 0) != 0 or row.get("last_visited_at") is not None:
                    row["visit_count"] = 0
                    row["last_visited_at"] = None
                    row["row_version"] = int(row.get("row_version") or 0) + 1
                    row["updated_at"] = _now().isoformat()
                    changed += 1
        return changed

    async def get_endpoint_runtime(self, endpoint_id):
        wanted = UUID(str(endpoint_id))
        for row in self.sites.values():
            candidate = row.get("default_endpoint_id")
            if candidate is not None and UUID(str(candidate)) == wanted:
                return {
                    "id": wanted, "site_id": row["id"], "role": "home",
                    "label": "", "url": row["url"],
                }
        return None

    async def delete_site(self, site_id):
        site_id = UUID(str(site_id))
        if site_id not in self.sites or self.sites[site_id].get("status")=="archived": return False
        self.sites[site_id]["status"]="archived"
        for value in self.accounts.values():
            if value["site_id"] == site_id: value.update(enabled=False,auth_state="disabled")
        now = _now()
        for job in self.jobs.values():
            if job.get("kind") == "checkin" and job.get("site_id") == site_id and job["status"] == "queued":
                job.update(
                    status="cancelled", finished_at=now,
                    result={"reason_code": "disabled", "cancelled_before_start": True},
                )
        return True

    async def list_site_urls_for_import(self):
        return {str(row["url"]) for row in self.sites.values() if row.get("status") != "archived"}

    async def list_accounts(self, site_id):
        site_id = UUID(str(site_id))
        return deepcopy([a for a in self.accounts.values() if a["site_id"] == site_id and a.get("auth_state")!="disabled"])
    async def get_account(self, account_id): return deepcopy(self.accounts.get(UUID(str(account_id))))

    async def create_account(self, site_id, payload):
        site_id = UUID(str(site_id))
        account_id = uuid4()
        config = payload.get("config") or {}
        public_payload = dict(payload)
        public_payload.pop("config", None)
        adapter = str(public_payload.get("adapter") or "playwright")
        public_payload["enabled"] = bool(public_payload.get("enabled", False)) and adapter.lower() in {"api", "cookie"}
        public_config, secret_fields = _public_config(config)
        row = {"id": account_id, "site_id": site_id, "profile_key": str(account_id),
               "profile": {"relative_path": str(account_id), "state": "unverified",
                           "manifest_sha256": None, "file_count": None, "total_bytes": None,
                           "last_verified_at": None, "verification_error": None},
               "configured": bool(public_config or secret_fields), "configured_secret_fields": secret_fields,
               "config": public_config,
               "auth_state": "unverified", "login_state": "unverified", "created_at": _now().isoformat(),
               "updated_at": None, **public_payload}
        self.accounts[account_id] = row
        if site_id in self.sites: self.sites[site_id]["checkin_account_count"] += 1
        return deepcopy(row)

    async def update_account(self, account_id, payload):
        account_id = UUID(str(account_id)); row = self.accounts.get(account_id)
        if not row: return None
        payload = dict(payload)
        next_adapter = str(payload.get("adapter", row.get("adapter")) or "").lower()
        next_enabled = bool(payload["enabled"]) if "enabled" in payload else bool(row.get("enabled"))
        activation_requested = payload.get("enabled") is True or (
            "adapter" in payload and next_enabled
        )
        if (
            activation_requested
            and next_adapter not in {"api", "cookie"}
            and row.get("auth_state") != "ready"
        ):
            raise AccountNotReady("Verify the browser Profile before enabling this account")
        clear_secrets = set(payload.pop("clear_secrets", []) or [])
        if "config" in payload:
            clean, new_secrets = _public_config(payload["config"] or {})
            previous = set(row.get("configured_secret_fields", []))
            row["config"] = clean
            row["configured_secret_fields"] = sorted((previous | set(new_secrets)) - clear_secrets)
            row["configured"] = bool(clean or row["configured_secret_fields"])
            payload.pop("config")
        elif clear_secrets:
            row["configured_secret_fields"] = sorted(
                set(row.get("configured_secret_fields", [])) - clear_secrets
            )
            row["configured"] = bool(row.get("config") or row["configured_secret_fields"])
        payload.pop("clear_schedule_override", None)
        row.update(payload); row["updated_at"] = _now().isoformat()
        if "enabled" in payload and not bool(payload["enabled"]):
            now = _now()
            for job in self.jobs.values():
                if job.get("kind") == "checkin" and job.get("account_id") == account_id and job["status"] == "queued":
                    job.update(
                        status="cancelled", finished_at=now,
                        result={"reason_code": "disabled", "cancelled_before_start": True},
                    )
        return deepcopy(row)

    async def delete_account(self, account_id):
        account_id = UUID(str(account_id)); row = self.accounts.get(account_id)
        if not row or row.get("auth_state")=="disabled": return False
        row.update(enabled=False,auth_state="disabled")
        if row["site_id"] in self.sites: self.sites[row["site_id"]]["checkin_account_count"] -= 1
        now = _now()
        for job in self.jobs.values():
            if job.get("kind") == "checkin" and job.get("account_id") == account_id and job["status"] == "queued":
                job.update(
                    status="cancelled", finished_at=now,
                    result={"reason_code": "disabled", "cancelled_before_start": True},
                )
        return True

    async def set_account_auth_state(self, account_id, state, error=None):
        row = self.accounts.get(UUID(str(account_id)))
        if not row: return None
        row["auth_state"] = state; row["login_state"] = state
        row["verification_error"] = error; row["updated_at"] = _now().isoformat()
        return deepcopy(row)

    async def set_account_profile_verification(
        self, account_id, state, error=None, verified_at=None,
    ):
        row = self.accounts.get(UUID(str(account_id)))
        if not row or row.get("auth_state") == "disabled": return None
        normalized = state if state in {"ready", "expired", "error", "busy", "unverified"} else "error"
        safe_error = sanitize_log_value(error, max_length=500) if error else None
        profile = row.setdefault("profile", {
            "relative_path": str(account_id), "state": "unverified",
            "manifest_sha256": None, "file_count": None, "total_bytes": None,
            "last_verified_at": None, "verification_error": None,
        })
        if normalized == "busy":
            profile["state"] = "busy"
            profile["verification_error"] = safe_error
        else:
            account_state = normalized if normalized in {"ready", "expired", "error"} else "unverified"
            row["auth_state"] = account_state; row["login_state"] = account_state
            row["verification_error"] = safe_error
            profile["state"] = "ready" if normalized == "ready" else (
                "unverified" if normalized == "unverified" else "error"
            )
            profile["verification_error"] = safe_error
            if normalized == "ready":
                timestamp = verified_at or _now().isoformat()
                row["last_verified_at"] = timestamp
                profile["last_verified_at"] = timestamp
                row["verification_error"] = None
                profile["verification_error"] = None
            else:
                row["enabled"] = False
                now = _now()
                for job in self.jobs.values():
                    if job.get("kind") == "checkin" and job.get("account_id") == UUID(str(account_id)) and job["status"] == "queued":
                        job.update(
                            status="cancelled", finished_at=now,
                            result={"reason_code": "disabled", "cancelled_before_start": True},
                        )
        row["updated_at"] = _now().isoformat()
        return deepcopy(row)

    def _ensure_product_schedule(self, site_id: UUID) -> tuple[dict[str, Any], dict[str, Any]]:
        interval = int(self.settings["scheduler.product_interval_minutes"])
        settings = self.product_settings.setdefault(site_id, {
            "site_id": site_id, "enabled": False, "interval_override_minutes": None,
            "interval_minutes": interval, "effective_interval_minutes": interval,
            "scraper_type": "none", "configured": False,
            "configured_secret_fields": [], "config": {},
        })
        schedule = self.product_schedules.setdefault(site_id, {
            "id": uuid4(), "site_id": site_id, "enabled": False,
            "timezone": "Asia/Shanghai", "policy_type": "interval",
            "next_run_at": None, "misfire_policy": "recover", "max_attempts": 1,
            "created_at": _now().isoformat(), "updated_at": _now().isoformat(),
        })
        return settings, schedule

    @staticmethod
    def _automatic_product_support(scraper_type: Any) -> tuple[bool, str | None]:
        normalized = str(scraper_type or "none").strip().casefold()
        if normalized == "manual":
            return False, "manual_catalog"
        if normalized in {"", "none"}:
            return False, "scraper_not_configured"
        return True, None

    async def get_product_settings(self, site_id):
        site_id = UUID(str(site_id))
        if site_id not in self.sites or self.sites[site_id].get("status") == "archived":
            return None
        row = self.product_settings.get(site_id)
        if row is None:
            return {
                "site_id": site_id, "enabled": False, "interval_override_minutes": None,
                "interval_minutes": int(self.settings["scheduler.product_interval_minutes"]),
                "effective_interval_minutes": int(self.settings["scheduler.product_interval_minutes"]),
                "scraper_type": "none", "configured": False,
                "configured_secret_fields": [], "config": {},
            }
        return deepcopy(row)
    async def update_product_settings(self, site_id, payload):
        site_id = UUID(str(site_id))
        if site_id not in self.sites or self.sites[site_id].get("status") == "archived":
            raise LookupError("site not found")
        row, schedule = self._ensure_product_schedule(site_id)
        payload = dict(payload)
        clear_secrets = set(payload.pop("clear_secrets", []) or [])
        if "config" in payload:
            clean, new_secrets = _public_config(payload.pop("config") or {})
            previous = set(row.get("configured_secret_fields", []))
            row["config"] = clean
            row["configured_secret_fields"] = sorted((previous | set(new_secrets)) - clear_secrets)
            row["configured"] = bool(clean or row["configured_secret_fields"])
        elif clear_secrets:
            row["configured_secret_fields"] = sorted(
                set(row.get("configured_secret_fields", [])) - clear_secrets
            )
            row["configured"] = bool(row.get("config") or row["configured_secret_fields"])
        if payload.pop("clear_interval_override", False): payload["interval_override_minutes"] = None
        row.update(payload)
        row["effective_interval_minutes"] = row.get("interval_override_minutes") or int(self.settings["scheduler.product_interval_minutes"])
        row["interval_minutes"] = row["effective_interval_minutes"]
        schedule.update(
            enabled=bool(row.get("enabled")), updated_at=_now().isoformat(),
        )
        return deepcopy(row)

    async def get_product_runtime(self, site_id):
        row = await self.get_product_settings(site_id)
        if row is None:
            return None
        row["runtime_config"] = deepcopy(row.get("config") or {})
        return row

    def _public_product(self, raw: dict[str, Any]) -> dict[str, Any]:
        row = deepcopy(raw)
        site_id = UUID(str(row["site_id"]))
        category_id = row.get("category_id")
        category = self.product_categories.get(UUID(str(category_id))) if category_id else None
        scraped_category_id = row.get("scraped_category_id")
        scraped_category = (
            self.product_categories.get(UUID(str(scraped_category_id)))
            if scraped_category_id else None
        )
        row["id"] = UUID(str(row["id"]))
        row["site_id"] = site_id
        row["site_name"] = self.sites.get(site_id, {}).get("name")
        row["category_id"] = UUID(str(category_id)) if category_id else None
        row["category_name"] = category.get("name") if category else None
        row["scraped_category_id"] = UUID(str(scraped_category_id)) if scraped_category_id else None
        row["scraped_category_name"] = scraped_category.get("name") if scraped_category else None
        row["category_overridden"] = bool(row.get("category_overridden", False))
        row.setdefault("raw_tags", deepcopy(row.pop("raw_tags_json", [])))
        return row

    async def list_products(self, *, page, page_size, site_id=None, q=None,
                            availability_status=None, category_id=None):
        rows = list(self.products.values())
        if site_id is not None:
            wanted_site = UUID(str(site_id))
            rows = [row for row in rows if UUID(str(row["site_id"])) == wanted_site]
        if category_id is not None:
            wanted_category = UUID(str(category_id))
            rows = [row for row in rows if row.get("category_id") and UUID(str(row["category_id"])) == wanted_category]
        if q:
            needle = str(q).casefold()
            rows = [row for row in rows if needle in str(row.get("name") or "").casefold()
                    or needle in str(row.get("external_key") or "").casefold()]
        if availability_status:
            rows = [row for row in rows if row.get("availability_status") == availability_status]
        rows.sort(key=lambda row: (str(row.get("updated_at") or ""), str(row["id"])), reverse=True)
        start = (page - 1) * page_size
        return [self._public_product(row) for row in rows[start:start + page_size]], len(rows)

    async def get_product(self, product_id):
        row = self.products.get(UUID(str(product_id)))
        if row is None:
            return None
        result = self._public_product(row)
        result.setdefault("tags", [])
        history = [item for item in self.price_observations.values()
                   if UUID(str(item["product_id"])) == UUID(str(product_id))]
        prices = [float(item["price"]) for item in history if item.get("price") is not None]
        observed = [_moment(item["observed_at"]) for item in history]
        result["price_history_summary"] = {
            "observation_count": len(history),
            "min_price": min(prices) if prices else None,
            "max_price": max(prices) if prices else None,
            "first_observed_at": min(observed).isoformat() if observed else None,
            "last_observed_at": max(observed).isoformat() if observed else None,
        }
        return result

    async def list_product_price_history(self, product_id, *, page, page_size,
                                         date_from=None, date_to=None):
        wanted = UUID(str(product_id))
        if wanted not in self.products:
            return None
        lower = _moment(date_from) if date_from is not None else None
        upper = _moment(date_to) if date_to is not None else None
        rows = [deepcopy(item) for item in self.price_observations.values()
                if UUID(str(item["product_id"])) == wanted]
        if lower is not None:
            rows = [row for row in rows if _moment(row["observed_at"]) >= lower]
        if upper is not None:
            rows = [row for row in rows if _moment(row["observed_at"]) <= upper]
        rows.sort(key=lambda row: (_moment(row["observed_at"]), str(row["id"])), reverse=True)
        start = (page - 1) * page_size
        return rows[start:start + page_size], len(rows)

    def _public_product_category(self, raw: dict[str, Any]) -> dict[str, Any]:
        row = deepcopy(raw)
        site_id = UUID(str(row["site_id"]))
        category_id = UUID(str(row["id"]))
        row.update(
            id=category_id, site_id=site_id,
            site_name=self.sites.get(site_id, {}).get("name"),
            product_count=sum(
                1 for product in self.products.values()
                if product.get("category_id") and UUID(str(product["category_id"])) == category_id
            ),
        )
        return row

    async def list_product_categories(self, *, page, page_size, site_id=None, q=None):
        rows = list(self.product_categories.values())
        if site_id is not None:
            wanted = UUID(str(site_id))
            rows = [row for row in rows if UUID(str(row["site_id"])) == wanted]
        if q:
            needle = str(q).casefold()
            rows = [row for row in rows if needle in str(row["name"]).casefold()
                    or needle in str(self.sites.get(UUID(str(row["site_id"])), {}).get("name") or "").casefold()]
        rows.sort(key=lambda row: (
            str(self.sites.get(UUID(str(row["site_id"])), {}).get("name") or "").casefold(),
            int(row.get("sort_order", 0)), str(row["name"]).casefold(), str(row["id"]),
        ))
        start = (page - 1) * page_size
        return [self._public_product_category(row) for row in rows[start:start + page_size]], len(rows)

    async def create_product_category(self, site_id, payload):
        site_id = UUID(str(site_id))
        if site_id not in self.sites or self.sites[site_id].get("status") == "archived":
            return None
        name = str(payload["name"]).strip()
        async with self._lock:
            if any(UUID(str(row["site_id"])) == site_id and str(row["name"]).casefold() == name.casefold()
                   for row in self.product_categories.values()):
                raise ValueError("A category with this name already exists for the site")
            now = _now().isoformat(); category_id = uuid4()
            row = {"id": category_id, "site_id": site_id, "name": name,
                   "source_name": None,
                   "sort_order": int(payload.get("sort_order", 0)),
                   "created_at": now, "updated_at": now}
            self.product_categories[category_id] = row
            return self._public_product_category(row)

    async def update_product_category(self, category_id, payload):
        category_id = UUID(str(category_id))
        async with self._lock:
            row = self.product_categories.get(category_id)
            if row is None:
                return None
            if payload.get("name") is None and "name" in payload:
                raise ValueError("category name must not be null")
            if payload.get("sort_order") is None and "sort_order" in payload:
                raise ValueError("category sort order must not be null")
            name = str(payload.get("name", row["name"])).strip()
            if any(other_id != category_id and UUID(str(other["site_id"])) == UUID(str(row["site_id"]))
                   and str(other["name"]).casefold() == name.casefold()
                   for other_id, other in self.product_categories.items()):
                raise ValueError("A category with this name already exists for the site")
            row.update(name=name, sort_order=int(payload.get("sort_order", row["sort_order"])),
                       updated_at=_now().isoformat())
            return self._public_product_category(row)

    async def delete_product_category(self, category_id):
        category_id = UUID(str(category_id))
        async with self._lock:
            if self.product_categories.pop(category_id, None) is None:
                return False
            for product in self.products.values():
                if product.get("category_id") and UUID(str(product["category_id"])) == category_id:
                    product["category_id"] = None
                    product["updated_at"] = _now().isoformat()
                if product.get("scraped_category_id") and UUID(str(product["scraped_category_id"])) == category_id:
                    product["scraped_category_id"] = None
            return True

    async def assign_product_category(self, product_id, category_id, *, automatic=False):
        product_id = UUID(str(product_id))
        wanted_category = UUID(str(category_id)) if category_id else None
        async with self._lock:
            product = self.products.get(product_id)
            if product is None:
                return None
            if wanted_category is not None and not automatic:
                category = self.product_categories.get(wanted_category)
                if category is None:
                    raise LookupError("Product category not found")
                if UUID(str(category["site_id"])) != UUID(str(product["site_id"])):
                    raise ValueError("A product can only use a category from the same site")
            product["category_id"] = (
                product.get("scraped_category_id") if automatic else wanted_category
            )
            product["category_overridden"] = not automatic
            product["updated_at"] = _now().isoformat()
            return self._public_product(product)

    async def list_due_product_sites(self, now):
        current = _moment(now)
        rows = []
        for site_id, schedule in self.product_schedules.items():
            settings = self.product_settings.get(site_id) or {}
            next_run = schedule.get("next_run_at")
            if not settings.get("enabled") or not schedule.get("enabled"):
                continue
            supported, _ = self._automatic_product_support(settings.get("scraper_type"))
            if not supported:
                continue
            if next_run is not None and _moment(next_run) > current:
                continue
            rows.append({
                "site_id": site_id,
                "interval_override_minutes": settings.get("interval_override_minutes"),
            })
        return deepcopy(rows)

    async def mark_product_enqueued(self, site_id, next_run_at):
        site_id = UUID(str(site_id))
        _, schedule = self._ensure_product_schedule(site_id)
        schedule.update(next_run_at=next_run_at, updated_at=_now().isoformat())

    async def enqueue_job(self, payload):
        async with self._lock:
            existing = next((j for j in self.jobs.values() if j["idempotency_key"] == payload["idempotency_key"]), None)
            if existing: return deepcopy(existing), False
            job_id = UUID(str(payload.get("id") or uuid4()))
            row = {"id": job_id, "status": "queued", "attempt": 0, "max_attempts": 3,
                   "progress": None, "result": None, "error_kind": None, "error_message": None,
                   "lease_token": None, "lease_expires_at": None, "worker_instance": None,
                   "created_at": _now(), "started_at": None, "finished_at": None, **payload}
            self.jobs[job_id] = row
            return deepcopy(row), True
    async def get_job(self, job_id): return deepcopy(self.jobs.get(UUID(str(job_id))))
    async def list_jobs(self, *, page, page_size, kind=None, status=None, site_id=None, account_id=None):
        rows = list(self.jobs.values())
        if kind: rows = [r for r in rows if r["kind"] == kind]
        statuses = set(normalize_job_statuses(status))
        if statuses: rows = [r for r in rows if r["status"] in statuses]
        if site_id is not None: rows = [r for r in rows if r.get("site_id") == site_id]
        if account_id is not None: rows = [r for r in rows if r.get("account_id") == account_id]
        rows.sort(key=lambda r: r["created_at"], reverse=True); start=(page-1)*page_size
        return deepcopy(rows[start:start+page_size]), len(rows)
    async def lease_next_job(self, *, kind, worker_instance, now, lease_expires_at):
        async with self._lock:
            row = next((r for r in self.jobs.values() if r["kind"] == kind and r["status"] == "queued" and r["scheduled_for"] <= now), None)
            if not row: return None
            row.update(status="leased", lease_token=str(uuid4()), lease_expires_at=lease_expires_at,
                       worker_instance=worker_instance, attempt=row["attempt"] + 1)
            return deepcopy(row)
    async def mark_job_running(self, job_id, lease_token, now):
        row=self.jobs.get(UUID(str(job_id)))
        if not row or row["lease_token"] != lease_token or row["status"] != "leased": return None
        row.update(status="running", started_at=row["started_at"] or now); return deepcopy(row)
    async def heartbeat_job(self, job_id, lease_token, now, lease_expires_at, progress=None):
        row=self.jobs.get(UUID(str(job_id)))
        if not row or row["lease_token"] != lease_token or row["status"] not in ("leased","running"): return False
        row.update(lease_expires_at=lease_expires_at, progress=progress or row["progress"]); return True
    async def complete_job(self, job_id, lease_token, *, status, result, error_kind, error_message, now):
        row=self.jobs.get(UUID(str(job_id)))
        if not row or row["lease_token"] != lease_token or row["status"] not in ("leased","running"): return None
        retrying = status == "failed" and error_kind == "infrastructure" and row["attempt"] < row["max_attempts"]
        row.update(status="queued" if retrying else status, result=result, error_kind=error_kind,
                   error_message=error_message, finished_at=None if retrying else now,
                   lease_token=None, lease_expires_at=None)
        if retrying: row["scheduled_for"] = now
        return deepcopy(row)
    async def cancel_job(self, job_id, now):
        row=self.jobs.get(UUID(str(job_id)))
        if not row or row["status"] not in ("queued", "leased", "running"): return None
        if row.get("kind") == "checkin" and row["status"] in ("leased", "running"):
            row.update(
                status="effect_unknown", finished_at=now, lease_token=None, lease_expires_at=None,
                worker_instance=None,
                result={
                    "reason_code": "cancel_requested", "cancel_requested": True,
                    "requires_reconcile": True,
                },
                error_kind="cancel_requested",
                error_message="Cancellation requested after check-in execution began; remote outcome requires reconciliation",
            )
        else:
            row.update(
                status="cancelled", finished_at=now, lease_token=None, lease_expires_at=None,
                worker_instance=None, error_kind=None, error_message=None,
                result={"reason_code": "cancelled", "cancelled_before_start": row["status"] == "queued"},
            )
        return deepcopy(row)
    async def retry_job(self, job_id, now):
        row=self.jobs.get(UUID(str(job_id)))
        if not row or row["status"] not in ("failed","cancelled","effect_unknown"): return None
        row.update(status="queued", scheduled_for=now, lease_token=None, lease_expires_at=None,
                   max_attempts=max(row["max_attempts"], row["attempt"] + 3),
                   finished_at=None, started_at=None, progress=None, result=None,
                   worker_instance=None, error_kind=None, error_message=None); return deepcopy(row)
    async def recover_expired_jobs(self, now):
        recovered=[]
        for row in self.jobs.values():
            if row["status"] in ("leased","running") and row["lease_expires_at"] and row["lease_expires_at"] <= now:
                if row["attempt"] < row["max_attempts"]: row.update(status="queued", lease_token=None, lease_expires_at=None)
                else: row.update(status="effect_unknown", error_kind="infrastructure", error_message="lease expired", finished_at=now)
                recovered.append(deepcopy(row))
        return recovered

    async def get_settings(self): return dict(self.settings)
    async def update_settings(self, values): self.settings.update(values); return dict(self.settings)
    async def list_batches(self, *, page, page_size, status=None, date_from=None, date_to=None):
        rows = list(self.batches.values())
        if status: rows = [row for row in rows if row.get("status") == status]
        if date_from: rows = [row for row in rows if str(row.get("date_key", "")) >= date_from]
        if date_to: rows = [row for row in rows if str(row.get("date_key", "")) <= date_to]
        rows.sort(key=lambda row: (str(row.get("date_key", "")), str(row.get("created_at", ""))), reverse=True)
        start = (page - 1) * page_size
        return deepcopy(rows[start:start + page_size]), len(rows)
    async def get_batch(self, batch_id):
        wanted = UUID(str(batch_id))
        row = next((item for item in self.batches.values() if UUID(str(item["id"])) == wanted), None)
        return deepcopy(row) if row else None
    async def get_batch_for_date(self, date_key): return deepcopy(self.batches.get(date_key))
    async def create_batch(self, payload, account_ids):
        batch_id=uuid4()
        row={"id":batch_id,"status":"pending","trigger_reason":"normal","recovery_at":None,
             "started_at":None,"completed_at":None,**payload}; self.batches[payload["date_key"]]=row
        self.batch_items[batch_id]=[{"id":uuid4(),"batch_id":batch_id,"account_id":account_id,
            "position":i+1,"status":"pending","job_id":None} for i,account_id in enumerate(account_ids)]
        return deepcopy(row)
    async def regenerate_batch(self, date_key, planned_at):
        row=self.batches.get(date_key)
        if not row or row["status"]!="pending" or row["started_at"]: return None
        row["planned_at"]=planned_at; return deepcopy(row)
    async def list_batch_items(self,batch_id): return deepcopy(self.batch_items.get(UUID(str(batch_id)),[]))
    async def list_default_schedule_accounts(self): return deepcopy([a for a in self.accounts.values() if a["enabled"] and not a.get("schedule_override")])
    async def list_override_schedule_accounts(self): return deepcopy([a for a in self.accounts.values() if a["enabled"] and a.get("schedule_override")])
    async def list_schedules(self, *, page, page_size, capability=None, enabled=None):
        rows = []
        for account in self.accounts.values():
            if account.get("auth_state") == "disabled": continue
            policy = account.get("schedule_override") or {"mode": "daily_random_window"}
            policy_type = "daily_random_window" if policy.get("mode") == "daily_random_window" else "cron"
            rows.append({
                "id": str(account["id"]), "capability": "checkin",
                "site_id": account["site_id"], "account_id": account["id"],
                "site_name": self.sites.get(UUID(str(account["site_id"])), {}).get("name"),
                "account_label": account.get("label") or account.get("name"),
                "timezone": policy.get("timezone", "Asia/Shanghai"),
                "policy_type": policy_type,
                "policy": deepcopy(policy), "enabled": bool(account.get("enabled", True)),
                "next_run_at": None,
                "automatic_run_supported": True, "automatic_run_reason": None,
            })
        for site_id, schedule in self.product_schedules.items():
            settings = self.product_settings.get(site_id) or {}
            scraper_type = str(settings.get("scraper_type") or "none")
            supported, reason = self._automatic_product_support(scraper_type)
            candidates = [job for job in self.jobs.values()
                          if job.get("kind") == "product_sync"
                          and job.get("site_id") is not None
                          and UUID(str(job["site_id"])) == site_id]
            active = max(
                (job for job in candidates if job.get("status") in {"queued", "leased", "running"}),
                key=lambda job: (_moment(job["created_at"]), str(job["id"])),
                default=None,
            )
            latest = max(
                (job for job in candidates if job.get("status") in {
                    "succeeded", "failed", "skipped", "cancelled", "effect_unknown",
                }),
                key=lambda job: (_moment(job["created_at"]), str(job["id"])),
                default=None,
            )
            interval = int(settings.get("interval_override_minutes")
                           or self.settings["scheduler.product_interval_minutes"])
            rows.append({
                "id": str(schedule["id"]), "capability": "product_monitor",
                "site_id": site_id, "site_name": self.sites.get(site_id, {}).get("name"),
                "account_id": None, "account_label": None,
                "timezone": schedule.get("timezone", "Asia/Shanghai"),
                "policy_type": "interval",
                "policy": {
                    "minutes": interval,
                    "override": settings.get("interval_override_minutes") is not None,
                },
                "enabled": bool(schedule.get("enabled") and settings.get("enabled")),
                "next_run_at": schedule.get("next_run_at"),
                "misfire_policy": schedule.get("misfire_policy", "recover"),
                "max_attempts": schedule.get("max_attempts", 1),
                "scraper_type": scraper_type,
                "automatic_run_supported": supported,
                "automatic_run_reason": reason,
                "current_job_id": str(active["id"]) if active else None,
                "current_status": active.get("status") if active else None,
                "current_job_at": active.get("created_at") if active else None,
                "last_job_id": str(latest["id"]) if latest else None,
                "last_status": latest.get("status") if latest else None,
                "last_run_at": latest.get("finished_at") if latest else None,
                "last_error_kind": latest.get("error_kind") if latest else None,
                "last_error_message": latest.get("error_message") if latest else None,
            })
        if capability: rows = [row for row in rows if row["capability"] == capability]
        if enabled is not None: rows = [row for row in rows if row["enabled"] is enabled]
        rows.sort(key=lambda row: (
            not bool(row["enabled"]), row.get("next_run_at") is None,
            str(row.get("next_run_at") or ""), str(row.get("site_name") or "").casefold(),
            str(row.get("account_label") or "").casefold(), str(row["id"]),
        ))
        start = (page - 1) * page_size
        return deepcopy(rows[start:start + page_size]), len(rows)

    async def list_folders(self):
        return deepcopy(sorted(self.folders.values(), key=lambda row: (row["parent_id"] is not None, str(row["parent_id"] or ""), row["sort_order"], row["name"].casefold())))
    async def create_folder(self, payload):
        folder_id = uuid4(); now = _now().isoformat()
        parent_id = UUID(str(payload["parent_id"])) if payload.get("parent_id") else None
        if parent_id is not None and parent_id not in self.folders: raise ValueError("parent folder not found")
        row = {"id": folder_id, "parent_id": parent_id, "name": str(payload["name"]).strip(),
               "sort_order": int(payload.get("sort_order", 0)), "created_at": now, "updated_at": now}
        self.folders[folder_id] = row
        return deepcopy(row)
    async def update_folder(self, folder_id, payload):
        folder_id = UUID(str(folder_id)); row = self.folders.get(folder_id)
        if not row: return None
        parent_id = payload.get("parent_id", row["parent_id"])
        parent_id = UUID(str(parent_id)) if parent_id else None
        if parent_id == folder_id: raise ValueError("folder cannot be its own parent")
        cursor = parent_id
        while cursor is not None:
            if cursor == folder_id: raise ValueError("folder hierarchy cannot contain a cycle")
            parent = self.folders.get(cursor)
            if not parent: raise ValueError("parent folder not found")
            cursor = parent["parent_id"]
        if "name" in payload: row["name"] = str(payload["name"]).strip()
        if "parent_id" in payload: row["parent_id"] = parent_id
        if "sort_order" in payload: row["sort_order"] = int(payload["sort_order"])
        row["updated_at"] = _now().isoformat()
        return deepcopy(row)
    async def delete_folder(self, folder_id):
        folder_id = UUID(str(folder_id)); row = self.folders.get(folder_id)
        if not row or folder_id == self.ROOT_FOLDER_ID: return False
        parent_id = row["parent_id"]
        for child in self.folders.values():
            if child.get("parent_id") == folder_id: child["parent_id"] = parent_id
        destination = parent_id or self.ROOT_FOLDER_ID
        for site in self.sites.values():
            if UUID(str(site.get("folder_id") or self.ROOT_FOLDER_ID)) == folder_id:
                site["folder_id"] = destination
        del self.folders[folder_id]
        return True
    async def get_artifact(self, artifact_id): return deepcopy(self.artifacts.get(UUID(str(artifact_id))))
    async def create_artifact(self,payload):
        artifact_id=UUID(str(payload.get("id") or uuid4())); row={"id":artifact_id,"created_at":_now(),**payload}; self.artifacts[artifact_id]=row; return deepcopy(row)
