from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


from site_hub.services.store_support import _iso, _loads, _dumps, _uuid


class ActivityStoreMixin:
    """Activity domain operations; transactions remain owned by each operation."""

    def _insert_event(self, conn: sqlite3.Connection, site_id: str, run_id: str | None, event_type: str,
                      severity: str, title: str, payload: dict[str, Any], now: str,
                      detail: str = "", *, notify: bool = True) -> str:
        event_id = str(uuid4())
        conn.execute(
            """INSERT INTO events(id,type,severity,entity_type,entity_id,run_id,title,detail,payload_json,occurred_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, event_type, severity, "site", site_id, run_id, title, detail, _dumps(payload), now, now),
        )
        if notify and self._notification_enabled_locked(conn, event_type):
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
        now = _iso(self._now())
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
        now = _iso(self._now())
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
