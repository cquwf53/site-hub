from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from site_hub.db.job_queries import JOB_SELECT, list_job_rows


from site_hub.services.store_support import _iso, _loads, _dumps, _uuid


class JobStoreMixin:
    """Jobs domain operations; transactions remain owned by each operation."""

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
            row = conn.execute(JOB_SELECT + " WHERE j.id=?", (job_id,)).fetchone()
            return self._normalize_job(dict(row)) if row else None

    async def list_jobs(self, *, page: int, page_size: int, kind: str | None = None,
                        status: str | Iterable[str] | None = None, site_id: UUID | None = None,
                        account_id: UUID | None = None):
        return await self._call(self._list_jobs_sync, page, page_size, kind, status,
                                _uuid(site_id) if site_id else None, _uuid(account_id) if account_id else None)

    def _list_jobs_sync(self, page: int, page_size: int, kind: str | None,
                        status: str | Iterable[str] | None,
                        site_id: str | None, account_id: str | None):
        rows, total = list_job_rows(
            self.database,
            page=page,
            page_size=page_size,
            kind=kind,
            status=status,
            site_id=site_id,
            account_id=account_id,
        )
        return [self._normalize_job(row) for row in rows], total

    async def lease_next_job(self, *, kind: str, worker_instance: str,
                             now: datetime, lease_expires_at: datetime) -> dict[str, Any] | None:
        return await self._call(self._lease_job_sync, kind, worker_instance, _iso(now), _iso(lease_expires_at))

    def _lease_job_sync(self, kind: str, worker: str, now: str, lease_until: str) -> dict[str, Any] | None:
        token = str(uuid4())
        with self.database.transaction(immediate=True) as conn:
            candidate = conn.execute(
                """SELECT j.id FROM jobs j
                   WHERE j.kind=? AND j.status='queued' AND j.available_at<=? AND j.attempt<j.max_attempts
                     AND (j.kind!='product_sync' OR NOT EXISTS(
                         SELECT 1 FROM jobs active WHERE active.kind='product_sync'
                         AND active.site_id=j.site_id AND active.status IN ('leased','running')
                     ))
                   ORDER BY j.priority DESC,j.available_at,j.created_at,j.id LIMIT 1""",
                (kind, now),
            ).fetchone()
            if not candidate:
                return None
            changed = conn.execute(
                """UPDATE jobs SET status='leased',lease_owner=?,lease_token=?,lease_until=?,attempt=attempt+1,result_json='{}',
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
            row = conn.execute(JOB_SELECT + " WHERE j.id=?", (candidate["id"],)).fetchone()
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
            current = conn.execute(JOB_SELECT + " WHERE j.id=?", (job_id,)).fetchone()
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
            if retrying:
                conn.execute("UPDATE jobs SET payload_json=? WHERE id=?", (self._retry_payload(dict(row), public_result), job_id))
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
            current = conn.execute(JOB_SELECT + " WHERE j.id=?", (job_id,)).fetchone()
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
            row = conn.execute(JOB_SELECT + " WHERE j.id=?", (job_id,)).fetchone()
            return self._normalize_job(dict(row))

    async def retry_job(self, job_id: UUID, now: datetime) -> dict[str, Any] | None:
        return await self._call(self._retry_sync, _uuid(job_id), _iso(now))

    def _retry_sync(self, job_id: str, now: str) -> dict[str, Any] | None:
        with self.database.transaction(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,),
            ).fetchone()
            changed = conn.execute(
                """UPDATE jobs SET status='queued',available_at=?,planned_at=?,lease_owner=NULL,
                          lease_token=NULL,lease_until=NULL,progress_json='{}',result_json='{}',payload_json=?,last_error=NULL,
                          started_at=NULL,finished_at=NULL,max_attempts=MAX(max_attempts,attempt+3),updated_at=?
                   WHERE id=? AND status IN ('failed','cancelled','effect_unknown')""",
                (now, now, self._retry_payload(existing), now, job_id),
            ).rowcount
            if not changed:
                return None
            self._refresh_batch_state_locked(
                conn, existing["batch_id"] if existing else None, now,
            )
            row = conn.execute(JOB_SELECT + " WHERE j.id=?", (job_id,)).fetchone()
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
                conn.execute("UPDATE jobs SET payload_json=? WHERE id=?", (self._retry_payload(dict(job_row), {}), job_id))
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
                JOB_SELECT + (" WHERE j.id IN (%s)" % ",".join("?" for _ in ids)), ids
            )] if ids else []

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
            "site_name": raw.get("site_name"), "account_label": raw.get("account_label"),
            "updated_at": raw.get("updated_at"),
            "scheduled_for": raw.get("planned_at") or raw.get("available_at"),
            "attempt": int(raw.get("attempt") or 0), "max_attempts": int(raw.get("max_attempts") or 1),
            "progress": progress or None, "result": result or None, "error_kind": error_kind,
            "error_message": raw.get("last_error"), "created_at": raw["created_at"],
            "started_at": raw.get("started_at"), "finished_at": raw.get("finished_at"),
            "lease_token": raw.get("lease_token"), "lease_expires_at": raw.get("lease_until"),
            "worker_instance": raw.get("lease_owner"), "idempotency_key": raw.get("idempotency_key"),
            "payload": _loads(raw.get("payload_json"), {}),
        }


    @staticmethod
    def _retry_payload(row, result=None) -> str:
        row = dict(row) if row is not None else {}
        payload = _loads(row.get("payload_json"), {})
        payload = dict(payload) if isinstance(payload, dict) else {}
        if row.get("kind") == "checkin":
            previous = _loads(row.get("result_json"), {}) if result is None else result
            metadata = previous.get("metadata", {}) if isinstance(previous, dict) else {}
            effect = metadata.get("remoteEffect") if isinstance(metadata, dict) else None
            payload["retry_context"] = {"remote_effect": "not_started" if effect == "not_started" else "possible"}
        return _dumps(payload)
