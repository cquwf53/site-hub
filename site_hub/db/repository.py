"""Typed, conservative data-access methods for the shared control plane."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from .connection import Database


def new_uuid() -> str:
    return str(uuid.uuid4())


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utc_after(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class SiteHubRepository:
    """Synchronous repository used by API threads and background workers."""

    def __init__(self, database: Database):
        self.database = database

    # -- Folders ---------------------------------------------------------
    def create_folder(
        self, name: str, *, parent_id: str | None = None, sort_order: int = 0,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        identifier, now = folder_id or new_uuid(), utc_now()
        with self.database.transaction() as conn:
            conn.execute(
                "INSERT INTO folders(id,parent_id,name,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (identifier, parent_id, name.strip(), sort_order, now, now),
            )
            return dict(conn.execute("SELECT * FROM folders WHERE id=?", (identifier,)).fetchone())

    def list_folders(self) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM folders ORDER BY parent_id IS NOT NULL, parent_id, sort_order, name, id"
            )]

    # -- Sites -----------------------------------------------------------
    def create_site(
        self, *, folder_id: str, name: str, origin: str, canonical_host: str,
        kind: str = "other", status: str = "active", description: str = "",
        note: str = "", icon: str = "", pinned: bool = False,
        sort_order: int = 0, site_id: str | None = None,
    ) -> dict[str, Any]:
        identifier, now = site_id or new_uuid(), utc_now()
        with self.database.transaction() as conn:
            conn.execute(
                """INSERT INTO sites(
                    id,folder_id,name,origin,canonical_host,kind,status,description,note,
                    icon,pinned,sort_order,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, folder_id, name.strip(), origin, canonical_host.lower(), kind,
                 status, description, note, icon, int(pinned), sort_order, now, now),
            )
            return dict(conn.execute("SELECT * FROM sites WHERE id=?", (identifier,)).fetchone())

    def get_site(self, site_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            return _row(conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone())

    def list_sites(
        self, *, folder_id: str | None = None, status: str | None = None,
        tag_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if folder_id is not None:
            clauses.append("s.folder_id=?")
            params.append(folder_id)
        if status is not None:
            clauses.append("s.status=?")
            params.append(status)
        if tag_id is not None:
            clauses.append("EXISTS(SELECT 1 FROM site_tags st WHERE st.site_id=s.id AND st.tag_id=?)")
            params.append(tag_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.connect() as conn:
            return [dict(r) for r in conn.execute(
                f"SELECT s.* FROM sites s{where} ORDER BY s.pinned DESC,s.sort_order,s.id",
                params,
            )]

    def update_site(self, site_id: str, **changes: Any) -> dict[str, Any] | None:
        allowed = {
            "folder_id", "name", "origin", "canonical_host", "kind", "status",
            "description", "note", "icon", "favicon_path", "pinned", "sort_order",
            "visit_count", "last_visited_at",
        }
        fields, values = [], []
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError(f"unsupported site field: {key}")
            if key == "pinned":
                value = int(bool(value))
            fields.append(f"{key}=?")
            values.append(value)
        if not fields:
            return self.get_site(site_id)
        fields.extend(["row_version=row_version+1", "updated_at=?"])
        values.extend([utc_now(), site_id])
        with self.database.transaction() as conn:
            conn.execute(f"UPDATE sites SET {','.join(fields)} WHERE id=?", values)
            return _row(conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone())

    def delete_site(self, site_id: str) -> bool:
        """Delete a site and its owned records; referenced secrets remain reusable."""
        with self.database.transaction() as conn:
            return conn.execute("DELETE FROM sites WHERE id=?", (site_id,)).rowcount == 1

    def create_endpoint(
        self, *, site_id: str, role: str, url_base: str, label: str = "",
        url_suffix_secret_id: str | None = None, query_keys: Sequence[str] = (),
        is_default: bool = False, sort_order: int = 0, endpoint_id: str | None = None,
    ) -> dict[str, Any]:
        identifier, now = endpoint_id or new_uuid(), utc_now()
        with self.database.transaction() as conn:
            conn.execute(
                """INSERT INTO site_endpoints(
                    id,site_id,role,label,url_base,url_suffix_secret_id,query_keys_json,
                    is_default,sort_order,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, site_id, role, label, url_base, url_suffix_secret_id,
                 _json(list(query_keys)), int(is_default), sort_order, now, now),
            )
            return dict(conn.execute("SELECT * FROM site_endpoints WHERE id=?", (identifier,)).fetchone())

    def list_endpoints(self, site_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM site_endpoints WHERE site_id=? ORDER BY role,is_default DESC,sort_order,id",
                (site_id,),
            )]

    # -- Tags ------------------------------------------------------------
    def create_tag(
        self, name: str, *, color: str = "#8b5cf6", icon: str = "tag",
        sort_order: int = 0, tag_id: str | None = None,
    ) -> dict[str, Any]:
        identifier, now = tag_id or new_uuid(), utc_now()
        with self.database.transaction() as conn:
            conn.execute(
                "INSERT INTO tags(id,name,color,icon,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (identifier, name.strip(), color, icon, sort_order, now, now),
            )
            return dict(conn.execute("SELECT * FROM tags WHERE id=?", (identifier,)).fetchone())

    def list_tags(self) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM tags ORDER BY sort_order,id")]

    def set_site_tags(self, site_id: str, tag_ids: Iterable[str]) -> None:
        ids = list(dict.fromkeys(tag_ids))
        with self.database.transaction() as conn:
            conn.execute("DELETE FROM site_tags WHERE site_id=?", (site_id,))
            conn.executemany(
                "INSERT INTO site_tags(site_id,tag_id) VALUES(?,?)",
                ((site_id, tag_id) for tag_id in ids),
            )

    # -- Accounts, profiles and capabilities ---------------------------
    def create_site_account(
        self, *, site_id: str, label: str, enabled: bool = True,
        auth_state: str = "unverified", sort_order: int = 0,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        identifier, now = account_id or new_uuid(), utc_now()
        with self.database.transaction() as conn:
            conn.execute(
                """INSERT INTO site_accounts(
                    id,site_id,label,enabled,auth_state,sort_order,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (identifier, site_id, label, int(enabled), auth_state, sort_order, now, now),
            )
            return dict(conn.execute("SELECT * FROM site_accounts WHERE id=?", (identifier,)).fetchone())

    def list_site_accounts(self, site_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM site_accounts WHERE site_id=? ORDER BY sort_order,id", (site_id,)
            )]

    list_checkin_accounts = list_site_accounts

    def get_site_account(self, account_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            return _row(conn.execute(
                "SELECT * FROM site_accounts WHERE id=?", (account_id,)
            ).fetchone())

    def update_site_account(self, account_id: str, **changes: Any) -> dict[str, Any] | None:
        allowed = {
            "label", "enabled", "auth_state", "last_verified_at",
            "verification_error", "sort_order",
        }
        fields: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError(f"unsupported account field: {key}")
            if key == "enabled":
                value = int(bool(value))
            fields.append(f"{key}=?")
            values.append(value)
        if not fields:
            return self.get_site_account(account_id)
        fields.append("updated_at=?")
        values.extend([utc_now(), account_id])
        with self.database.transaction() as conn:
            conn.execute(f"UPDATE site_accounts SET {','.join(fields)} WHERE id=?", values)
            return _row(conn.execute(
                "SELECT * FROM site_accounts WHERE id=?", (account_id,)
            ).fetchone())

    def delete_site_account(self, account_id: str) -> bool:
        with self.database.transaction() as conn:
            return conn.execute(
                "DELETE FROM site_accounts WHERE id=?", (account_id,)
            ).rowcount == 1

    def create_browser_profile(
        self, *, account_id: str, relative_path: str, state: str = "unverified",
        manifest_sha256: str | None = None, file_count: int | None = None,
        total_bytes: int | None = None, profile_id: str | None = None,
    ) -> dict[str, Any]:
        identifier, now = profile_id or new_uuid(), utc_now()
        with self.database.transaction() as conn:
            conn.execute(
                """INSERT INTO browser_profiles(
                    id,account_id,relative_path,state,manifest_sha256,file_count,total_bytes,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (identifier, account_id, relative_path, state, manifest_sha256, file_count,
                 total_bytes, now, now),
            )
            return dict(conn.execute("SELECT * FROM browser_profiles WHERE id=?", (identifier,)).fetchone())

    def create_capability(
        self, *, site_id: str, code: str, adapter: str = "none",
        account_id: str | None = None, endpoint_id: str | None = None,
        config: Mapping[str, Any] | None = None, config_version: int = 1,
        enabled: bool = True, capability_id: str | None = None,
    ) -> dict[str, Any]:
        identifier, now = capability_id or new_uuid(), utc_now()
        with self.database.transaction() as conn:
            conn.execute(
                """INSERT INTO capability_instances(
                    id,site_id,code,account_id,endpoint_id,adapter,config_version,config_json,
                    enabled,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, site_id, code, account_id, endpoint_id, adapter, config_version,
                 _json(config or {}), int(enabled), now, now),
            )
            return dict(conn.execute("SELECT * FROM capability_instances WHERE id=?", (identifier,)).fetchone())

    def list_capabilities(self, site_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM capability_instances WHERE site_id=? ORDER BY code,account_id,id",
                (site_id,),
            )]

    def get_capability(self, capability_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            return _row(conn.execute(
                "SELECT * FROM capability_instances WHERE id=?", (capability_id,)
            ).fetchone())

    def find_capability(
        self, *, site_id: str, code: str, account_id: str | None = None,
    ) -> dict[str, Any] | None:
        if account_id is None:
            sql = (
                "SELECT * FROM capability_instances "
                "WHERE site_id=? AND code=? AND account_id IS NULL"
            )
            params: tuple[Any, ...] = (site_id, code)
        else:
            sql = (
                "SELECT * FROM capability_instances "
                "WHERE site_id=? AND code=? AND account_id=?"
            )
            params = (site_id, code, account_id)
        with self.database.connect() as conn:
            return _row(conn.execute(sql, params).fetchone())

    def update_capability(self, capability_id: str, **changes: Any) -> dict[str, Any] | None:
        allowed = {"endpoint_id", "adapter", "config_version", "config_json", "enabled"}
        fields: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError(f"unsupported capability field: {key}")
            if key == "config_json" and not isinstance(value, str):
                value = _json(value)
            if key == "enabled":
                value = int(bool(value))
            fields.append(f"{key}=?")
            values.append(value)
        if not fields:
            return self.get_capability(capability_id)
        fields.append("updated_at=?")
        values.extend([utc_now(), capability_id])
        with self.database.transaction() as conn:
            conn.execute(
                f"UPDATE capability_instances SET {','.join(fields)} WHERE id=?", values
            )
            return _row(conn.execute(
                "SELECT * FROM capability_instances WHERE id=?", (capability_id,)
            ).fetchone())

    # -- Settings --------------------------------------------------------
    def set_setting(self, key: str, value: Any) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                       updated_at=excluded.updated_at""",
                (key, _json(value), utc_now()),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.database.connect() as conn:
            row = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row["value_json"]) if row else default

    def list_settings(self) -> dict[str, Any]:
        with self.database.connect() as conn:
            return {r["key"]: json.loads(r["value_json"]) for r in conn.execute(
                "SELECT key,value_json FROM settings ORDER BY key"
            )}

    # -- Schedules, batches and jobs -------------------------------------
    def create_schedule(
        self, *, capability_instance_id: str, policy_type: str,
        policy: Mapping[str, Any] | None = None, timezone: str = "Asia/Shanghai",
        enabled: bool = False, next_run_at: str | None = None,
        misfire_policy: str = "recover", max_attempts: int = 1,
        backoff: Mapping[str, Any] | None = None, schedule_id: str | None = None,
    ) -> dict[str, Any]:
        identifier, now = schedule_id or new_uuid(), utc_now()
        with self.database.transaction() as conn:
            conn.execute(
                """INSERT INTO schedules(
                    id,capability_instance_id,timezone,policy_type,policy_json,enabled,
                    next_run_at,misfire_policy,max_attempts,backoff_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, capability_instance_id, timezone, policy_type,
                 _json(policy or {}), int(enabled), next_run_at, misfire_policy,
                 max_attempts, _json(backoff or {}), now, now),
            )
            return dict(conn.execute("SELECT * FROM schedules WHERE id=?", (identifier,)).fetchone())

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            return _row(conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone())

    def list_due_schedules(self, *, due_at: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        due_at = due_at or utc_now()
        with self.database.connect() as conn:
            return [dict(row) for row in conn.execute(
                """SELECT * FROM schedules
                   WHERE enabled=1 AND next_run_at IS NOT NULL AND next_run_at<=?
                   ORDER BY next_run_at,id LIMIT ?""",
                (due_at, limit),
            )]

    def update_schedule(self, schedule_id: str, **changes: Any) -> dict[str, Any] | None:
        allowed = {
            "timezone", "policy_type", "policy_json", "enabled", "next_run_at",
            "misfire_policy", "max_attempts", "backoff_json",
        }
        fields: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError(f"unsupported schedule field: {key}")
            if key in {"policy_json", "backoff_json"} and not isinstance(value, str):
                value = _json(value)
            if key == "enabled":
                value = int(bool(value))
            fields.append(f"{key}=?")
            values.append(value)
        if not fields:
            return self.get_schedule(schedule_id)
        fields.append("updated_at=?")
        values.extend([utc_now(), schedule_id])
        with self.database.transaction() as conn:
            conn.execute(f"UPDATE schedules SET {','.join(fields)} WHERE id=?", values)
            return _row(conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone())

    def create_batch(
        self, *, business_date: str | None, planned_at: str | None,
        schedule_id: str | None = None, timezone: str = "Asia/Shanghai",
        trigger_reason: str = "normal", status: str = "pending",
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        identifier, now = batch_id or new_uuid(), utc_now()
        with self.database.transaction() as conn:
            conn.execute(
                """INSERT INTO job_batches(
                    id,schedule_id,business_date,timezone,planned_at,trigger_reason,status,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (identifier, schedule_id, business_date, timezone, planned_at, trigger_reason,
                 status, now, now),
            )
            return dict(conn.execute("SELECT * FROM job_batches WHERE id=?", (identifier,)).fetchone())

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            return _row(conn.execute("SELECT * FROM job_batches WHERE id=?", (batch_id,)).fetchone())

    def get_batch_by_business_date(
        self, business_date: str, *, schedule_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            if schedule_id is None:
                return _row(conn.execute(
                    """SELECT * FROM job_batches WHERE business_date=?
                       ORDER BY created_at DESC,id DESC LIMIT 1""",
                    (business_date,),
                ).fetchone())
            return _row(conn.execute(
                """SELECT * FROM job_batches WHERE business_date=? AND schedule_id=?
                   ORDER BY created_at DESC,id DESC LIMIT 1""",
                (business_date, schedule_id),
            ).fetchone())

    def update_batch(self, batch_id: str, **changes: Any) -> dict[str, Any] | None:
        allowed = {
            "planned_at", "trigger_reason", "status", "started_at", "completed_at",
        }
        fields: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError(f"unsupported batch field: {key}")
            fields.append(f"{key}=?")
            values.append(value)
        if not fields:
            return self.get_batch(batch_id)
        fields.append("updated_at=?")
        values.extend([utc_now(), batch_id])
        with self.database.transaction() as conn:
            conn.execute(f"UPDATE job_batches SET {','.join(fields)} WHERE id=?", values)
            return _row(conn.execute("SELECT * FROM job_batches WHERE id=?", (batch_id,)).fetchone())

    def regenerate_batch(self, batch_id: str, *, retry_at: str | None = None) -> int:
        """Requeue only non-running, non-successful batch jobs for an explicit regeneration."""
        now = utc_now()
        with self.database.transaction(immediate=True) as conn:
            changed = conn.execute(
                """UPDATE jobs SET status='queued',attempt=0,available_at=?,lease_owner=NULL,
                       lease_token=NULL,lease_until=NULL,last_error=NULL,finished_at=NULL,updated_at=?
                   WHERE batch_id=? AND status IN ('failed','skipped','effect_unknown','cancelled')""",
                (retry_at or now, now, batch_id),
            ).rowcount
            conn.execute(
                """UPDATE job_batches SET status='pending',started_at=NULL,completed_at=NULL,
                       updated_at=? WHERE id=?""",
                (now, batch_id),
            )
            return changed

    def create_job(
        self, *, capability_instance_id: str | None = None, idempotency_key: str,
        kind: str | None = None, site_id: str | None = None,
        account_id: str | None = None,
        available_at: str | None = None, batch_id: str | None = None,
        business_date: str | None = None, planned_at: str | None = None,
        max_attempts: int = 1, priority: int = 0,
        payload: Mapping[str, Any] | None = None, status: str = "queued",
        job_id: str | None = None,
    ) -> dict[str, Any]:
        identifier, now = job_id or new_uuid(), utc_now()
        with self.database.transaction() as conn:
            if capability_instance_id:
                capability = conn.execute(
                    "SELECT code,site_id,account_id FROM capability_instances WHERE id=?",
                    (capability_instance_id,),
                ).fetchone()
                if capability is None:
                    raise LookupError("capability instance not found")
                site_id = site_id or capability["site_id"]
                account_id = account_id or capability["account_id"]
                if kind is None:
                    kind = {
                        "checkin": "checkin",
                        "product_monitor": "product_sync",
                        "bookmark": "bookmark_import",
                    }.get(str(capability["code"]))
            if kind not in {"checkin", "product_sync", "bookmark_import", "login_session", "adapter_test"}:
                raise ValueError(f"invalid job kind: {kind}")
            conn.execute(
                """INSERT INTO jobs(
                    id,batch_id,kind,site_id,account_id,capability_instance_id,business_date,planned_at,available_at,
                    priority,status,max_attempts,idempotency_key,payload_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, batch_id, kind, site_id, account_id, capability_instance_id, business_date, planned_at,
                 available_at or now, priority, status, max_attempts, idempotency_key,
                 _json(payload or {}), now, now),
            )
            return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone())

    def list_batch_jobs(self, batch_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM jobs WHERE batch_id=? ORDER BY planned_at,created_at,id", (batch_id,)
            )]

    list_batch_items = list_batch_jobs

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            return _row(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list_jobs(
        self, *, status: str | None = None, capability_code: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("j.status=?")
            params.append(status)
        if capability_code is not None:
            clauses.append("c.code=?")
            params.append(capability_code)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self.database.connect() as conn:
            return [dict(row) for row in conn.execute(
                f"""SELECT j.* FROM jobs j
                    JOIN capability_instances c ON c.id=j.capability_instance_id
                    {where} ORDER BY j.created_at DESC,j.id DESC LIMIT ?""",
                params,
            )]

    def claim_next_job(
        self, worker_id: str, *, lease_seconds: int = 120,
        capability_codes: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim one due job, optionally restricted to worker capabilities."""
        now, lease_until, lease_token = utc_now(), _utc_after(lease_seconds), new_uuid()
        code_filter = ""
        params: list[Any] = [now, now]
        if capability_codes:
            placeholders = ",".join("?" for _ in capability_codes)
            code_filter = f" AND c.code IN ({placeholders})"
            params.extend(capability_codes)
        with self.database.transaction(immediate=True) as conn:
            candidate = conn.execute(
                f"""SELECT j.id FROM jobs j
                   JOIN capability_instances c ON c.id=j.capability_instance_id
                   WHERE ((j.status='queued' AND j.available_at<=?)
                       OR (j.status='leased' AND j.lease_until IS NOT NULL AND j.lease_until<=?))
                     AND j.attempt < j.max_attempts{code_filter}
                   ORDER BY j.priority DESC,j.available_at,j.created_at,j.id LIMIT 1""",
                params,
            ).fetchone()
            if candidate is None:
                return None
            changed = conn.execute(
                """UPDATE jobs SET status='leased',lease_owner=?,lease_token=?,lease_until=?,
                       attempt=attempt+1,started_at=COALESCE(started_at,?),updated_at=?
                   WHERE id=? AND ((status='queued' AND available_at<=?)
                       OR (status='leased' AND lease_until IS NOT NULL AND lease_until<=?))""",
                (worker_id, lease_token, lease_until, now, now, candidate["id"], now, now),
            ).rowcount
            if changed != 1:
                return None
            return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (candidate["id"],)).fetchone())

    def heartbeat_job(
        self, job_id: str, worker_id: str, *, lease_seconds: int = 120,
        lease_token: str | None = None,
    ) -> bool:
        token_clause = " AND lease_token=?" if lease_token is not None else ""
        params: list[Any] = [_utc_after(lease_seconds), utc_now(), job_id, worker_id]
        if lease_token is not None:
            params.append(lease_token)
        with self.database.transaction(immediate=True) as conn:
            return conn.execute(
                f"""UPDATE jobs SET lease_until=?,updated_at=?
                   WHERE id=? AND status IN ('leased','running') AND lease_owner=?{token_clause}""",
                params,
            ).rowcount == 1

    def start_job(
        self, job_id: str, worker_id: str, *, lease_token: str | None = None,
    ) -> bool:
        token_clause = " AND lease_token=?" if lease_token is not None else ""
        params: list[Any] = [utc_now(), job_id, worker_id]
        if lease_token is not None:
            params.append(lease_token)
        with self.database.transaction(immediate=True) as conn:
            return conn.execute(
                f"""UPDATE jobs SET status='running',updated_at=?
                   WHERE id=? AND status='leased' AND lease_owner=?{token_clause}""",
                params,
            ).rowcount == 1

    def finish_job(
        self, job_id: str, worker_id: str, *, status: str,
        error: str | None = None, result: Mapping[str, Any] | None = None,
        lease_token: str | None = None,
    ) -> bool:
        allowed = {"succeeded", "failed", "skipped", "effect_unknown", "cancelled"}
        if status not in allowed:
            raise ValueError(f"invalid terminal job status: {status}")
        now = utc_now()
        token_clause = " AND lease_token=?" if lease_token is not None else ""
        params: list[Any] = [status, error, _json(result or {}), now, now, job_id, worker_id]
        if lease_token is not None:
            params.append(lease_token)
        with self.database.transaction(immediate=True) as conn:
            return conn.execute(
                f"""UPDATE jobs SET status=?,last_error=?,result_json=?,lease_owner=NULL,
                       lease_token=NULL,lease_until=NULL,finished_at=?,updated_at=?
                   WHERE id=? AND status IN ('leased','running') AND lease_owner=?{token_clause}""",
                params,
            ).rowcount == 1

    def update_job_progress(
        self, job_id: str, worker_id: str, progress: Mapping[str, Any], *,
        lease_token: str | None = None,
    ) -> bool:
        token_clause = " AND lease_token=?" if lease_token is not None else ""
        params: list[Any] = [_json(progress), utc_now(), job_id, worker_id]
        if lease_token is not None:
            params.append(lease_token)
        with self.database.transaction() as conn:
            return conn.execute(
                f"""UPDATE jobs SET progress_json=?,updated_at=?
                   WHERE id=? AND status IN ('leased','running') AND lease_owner=?{token_clause}""",
                params,
            ).rowcount == 1

    def cancel_job(self, job_id: str) -> bool:
        now = utc_now()
        with self.database.transaction(immediate=True) as conn:
            return conn.execute(
                """UPDATE jobs SET status='cancelled',lease_owner=NULL,lease_token=NULL,
                       lease_until=NULL,finished_at=?,updated_at=?
                   WHERE id=? AND status IN ('queued','leased','running')""",
                (now, now, job_id),
            ).rowcount == 1

    def retry_job(self, job_id: str, *, available_at: str | None = None) -> bool:
        now = utc_now()
        with self.database.transaction(immediate=True) as conn:
            return conn.execute(
                """UPDATE jobs SET status='queued',available_at=?,lease_owner=NULL,
                       lease_token=NULL,lease_until=NULL,last_error=NULL,finished_at=NULL,
                       updated_at=?
                   WHERE id=? AND status IN ('failed','skipped','effect_unknown','cancelled')""",
                (available_at or now, now, job_id),
            ).rowcount == 1

    def requeue_expired_jobs(self, *, retry_at: str | None = None) -> int:
        now = utc_now()
        with self.database.transaction(immediate=True) as conn:
            return conn.execute(
                """UPDATE jobs SET
                     status=CASE WHEN attempt>=max_attempts THEN 'failed' ELSE 'queued' END,
                     available_at=CASE WHEN attempt>=max_attempts THEN available_at ELSE ? END,
                     last_error=CASE WHEN attempt>=max_attempts
                       THEN COALESCE(last_error,'lease expired after maximum attempts')
                       ELSE last_error END,
                     lease_owner=NULL,lease_token=NULL,lease_until=NULL,updated_at=?
                   WHERE status IN ('leased','running') AND lease_until IS NOT NULL AND lease_until<=?""",
                (retry_at or now, now, now),
            ).rowcount

    # -- Runs, artifacts and events -------------------------------------
    def create_run(
        self, *, capability_instance_id: str, status: str = "running",
        job_id: str | None = None, account_id: str | None = None,
        attempt_no: int = 1, message: str = "", result: Mapping[str, Any] | None = None,
        legacy_source: str | None = None, started_at: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        identifier, now = run_id or new_uuid(), utc_now()
        start = started_at or now
        with self.database.transaction() as conn:
            conn.execute(
                """INSERT INTO runs(
                    id,job_id,capability_instance_id,account_id,attempt_no,status,message,
                    started_at,result_json,legacy_source,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, job_id, capability_instance_id, account_id, attempt_no, status,
                 message, start, _json(result or {}), legacy_source, now),
            )
            return dict(conn.execute("SELECT * FROM runs WHERE id=?", (identifier,)).fetchone())

    def finish_run(
        self, run_id: str, *, status: str, reason_code: str | None = None,
        message: str = "", result: Mapping[str, Any] | None = None,
        duration_ms: int | None = None, finished_at: str | None = None,
    ) -> dict[str, Any] | None:
        finished = finished_at or utc_now()
        with self.database.transaction() as conn:
            conn.execute(
                """UPDATE runs SET status=?,reason_code=?,message=?,finished_at=?,duration_ms=?,
                       result_json=? WHERE id=?""",
                (status, reason_code, message, finished, duration_ms, _json(result or {}), run_id),
            )
            return _row(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def create_artifact(
        self, *, relative_path: str, run_id: str | None = None,
        role: str = "screenshot", sha256: str | None = None,
        mime_type: str | None = None, size: int | None = None,
        retention_until: str | None = None, artifact_id: str | None = None,
    ) -> dict[str, Any]:
        identifier = artifact_id or new_uuid()
        with self.database.transaction() as conn:
            conn.execute(
                """INSERT INTO artifacts(
                    id,run_id,role,relative_path,sha256,mime_type,size,retention_until,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (identifier, run_id, role, relative_path, sha256, mime_type, size,
                 retention_until, utc_now()),
            )
            return dict(conn.execute("SELECT * FROM artifacts WHERE id=?", (identifier,)).fetchone())

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            return _row(conn.execute(
                "SELECT * FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone())

    def create_event(
        self, *, event_type: str, severity: str, entity_type: str, title: str,
        entity_id: str | None = None, run_id: str | None = None, detail: str = "",
        payload: Mapping[str, Any] | None = None, dedupe_key: str | None = None,
        occurred_at: str | None = None, event_id: str | None = None,
    ) -> dict[str, Any]:
        identifier, now = event_id or new_uuid(), utc_now()
        with self.database.transaction() as conn:
            conn.execute(
                """INSERT INTO events(
                    id,type,severity,entity_type,entity_id,run_id,title,detail,payload_json,
                    dedupe_key,occurred_at,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, event_type, severity, entity_type, entity_id, run_id, title,
                 detail, _json(payload or {}), dedupe_key, occurred_at or now, now),
            )
            return dict(conn.execute("SELECT * FROM events WHERE id=?", (identifier,)).fetchone())

    def list_events(self, *, unread_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        where = "WHERE es.read_at IS NULL" if unread_only else ""
        with self.database.connect() as conn:
            return [dict(r) for r in conn.execute(
                f"""SELECT e.*,es.read_at,es.dismissed_at FROM events e
                    LEFT JOIN event_state es ON es.event_id=e.id {where}
                    ORDER BY e.occurred_at DESC,e.id DESC LIMIT ?""",
                (limit,),
            )]

    def mark_event_read(self, event_id: str, *, read_at: str | None = None) -> None:
        now = read_at or utc_now()
        with self.database.transaction() as conn:
            conn.execute(
                """INSERT INTO event_state(event_id,read_at,updated_at) VALUES(?,?,?)
                   ON CONFLICT(event_id) DO UPDATE SET read_at=excluded.read_at,
                       updated_at=excluded.updated_at""",
                (event_id, now, now),
            )

    # -- Notification channels and outbox -------------------------------
    def create_notification_channel(
        self, *, name: str, channel_type: str, endpoint_secret_id: str,
        enabled: bool = True, config: Mapping[str, Any] | None = None,
        channel_id: str | None = None,
    ) -> dict[str, Any]:
        identifier, now = channel_id or new_uuid(), utc_now()
        with self.database.transaction() as conn:
            conn.execute(
                """INSERT INTO notification_channels(
                    id,name,channel_type,enabled,endpoint_secret_id,config_json,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (identifier, name, channel_type, int(enabled), endpoint_secret_id,
                 _json(config or {}), now, now),
            )
            return dict(conn.execute(
                "SELECT * FROM notification_channels WHERE id=?", (identifier,)
            ).fetchone())

    def list_notification_channels(self) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM notification_channels ORDER BY name,id"
            )]

    def enqueue_notification(
        self, *, event_id: str, channel_id: str, idempotency_key: str,
        available_at: str | None = None, outbox_id: str | None = None,
    ) -> dict[str, Any]:
        identifier, now = outbox_id or new_uuid(), utc_now()
        with self.database.transaction() as conn:
            conn.execute(
                """INSERT INTO notification_outbox(
                    id,event_id,channel_id,idempotency_key,available_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (identifier, event_id, channel_id, idempotency_key,
                 available_at or now, now, now),
            )
            return dict(conn.execute(
                "SELECT * FROM notification_outbox WHERE id=?", (identifier,)
            ).fetchone())

    def claim_notification(
        self, worker_id: str, *, lease_seconds: int = 120,
    ) -> dict[str, Any] | None:
        now, lease_until = utc_now(), _utc_after(lease_seconds)
        with self.database.transaction(immediate=True) as conn:
            candidate = conn.execute(
                """SELECT id FROM notification_outbox
                   WHERE ((status='pending' AND available_at<=?)
                     OR (status='leased' AND lease_until IS NOT NULL AND lease_until<=?))
                   ORDER BY available_at,created_at,id LIMIT 1""",
                (now, now),
            ).fetchone()
            if candidate is None:
                return None
            conn.execute(
                """UPDATE notification_outbox SET status='leased',lease_owner=?,
                       lease_until=?,attempt=attempt+1,updated_at=? WHERE id=?""",
                (worker_id, lease_until, now, candidate["id"]),
            )
            return dict(conn.execute(
                "SELECT * FROM notification_outbox WHERE id=?", (candidate["id"],)
            ).fetchone())

    def finish_notification(
        self, outbox_id: str, worker_id: str, *, status: str,
        response_code: str | None = None, error: str | None = None,
    ) -> bool:
        if status not in {"sent", "failed", "dead", "cancelled"}:
            raise ValueError(f"invalid terminal notification status: {status}")
        with self.database.transaction(immediate=True) as conn:
            return conn.execute(
                """UPDATE notification_outbox SET status=?,response_code=?,last_error=?,
                       lease_owner=NULL,lease_until=NULL,updated_at=?
                   WHERE id=? AND status='leased' AND lease_owner=?""",
                (status, response_code, error, utc_now(), outbox_id, worker_id),
            ).rowcount == 1


Repository = SiteHubRepository
