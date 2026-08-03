from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from site_hub.scrapers.network import sanitize_log_value
from site_hub.services.ports import AccountNotReady


def _now() -> datetime:
    return datetime.now(UTC)


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
        self.accounts: dict[UUID, dict[str, Any]] = {}
        self.product_settings: dict[UUID, dict[str, Any]] = {}
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

    async def list_sites(self, *, page: int, page_size: int, q: str | None = None, folder_id=None, tag_id=None):
        rows = [row for row in self.sites.values() if row.get("status") != "archived"]
        if q:
            needle = q.casefold()
            rows = [r for r in rows if needle in r["name"].casefold() or needle in r["url"].casefold()]
        if folder_id is not None: rows = [r for r in rows if str(r.get("folder_id")) == str(folder_id)]
        if tag_id is not None: rows = [r for r in rows if any(str(tag.get("id")) == str(tag_id) for tag in r.get("tags", []))]
        rows.sort(key=lambda r: r["id"])
        start = (page - 1) * page_size
        return deepcopy(rows[start:start + page_size]), len(rows)

    async def get_site(self, site_id):
        row=self.sites.get(UUID(str(site_id)))
        return deepcopy(row) if row and row.get("status")!="archived" else None

    async def create_site(self, payload):
        async with self._lock:
            site_id = uuid4()
            row = {"id": site_id, "favicon_path": None, "tags": [], "checkin_account_count": 0,
                   "product_count": 0, "adapter": None, "config": {}, "configured": False,
                   "configured_secret_fields": [], "created_at": _now().isoformat(),
                   "updated_at": None, **payload}
            row.pop("tag_ids", None)
            self.sites[site_id] = row
            return deepcopy(row)

    async def update_site(self, site_id, payload):
        site_id = UUID(str(site_id))
        if site_id not in self.sites: return None
        payload = dict(payload); payload.pop("tag_ids", None)
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
        self.sites[site_id].update(payload); self.sites[site_id]["updated_at"] = _now().isoformat()
        return deepcopy(self.sites[site_id])

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
        row = self.product_settings.setdefault(site_id, {"site_id": site_id, "enabled": False,
            "interval_override_minutes": None, "effective_interval_minutes": 60,
            "scraper_type": "none", "configured": False, "configured_secret_fields": [],
            "config": {}})
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
        return deepcopy(row)
    async def list_due_product_sites(self, now): return []
    async def mark_product_enqueued(self, site_id, next_run_at): pass

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
        if status: rows = [r for r in rows if r["status"] == status]
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
            rows.append({
                "id": str(account["id"]), "capability": "checkin",
                "site_id": account["site_id"], "account_id": account["id"],
                "timezone": policy.get("timezone", "Asia/Shanghai"),
                "policy": deepcopy(policy), "enabled": bool(account.get("enabled", True)),
                "next_run_at": None,
            })
        if capability: rows = [row for row in rows if row["capability"] == capability]
        if enabled is not None: rows = [row for row in rows if row["enabled"] is enabled]
        start = (page - 1) * page_size
        return rows[start:start + page_size], len(rows)

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
