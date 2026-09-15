from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from site_hub.db.job_queries import JOB_SELECT


from site_hub.services.store_support import _iso, _moment, _loads, _dumps, _uuid


class ScheduleStoreMixin:
    """Schedules domain operations; transactions remain owned by each operation."""

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
        now = _iso(self._now())
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
        now = _iso(self._now())
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
            rows = conn.execute(JOB_SELECT + " WHERE j.batch_id=? ORDER BY j.created_at,j.id", (batch_id,)).fetchall()
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
                          st.name site_name,st.status site_status,a.label account_label,
                          c.enabled capability_enabled,a.enabled account_enabled,a.auth_state,
                          a.verification_error,
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
            reason = None
            href = f"/sites/{row['site_id']}/accounts" if row.get("account_id") else f"/sites/{row['site_id']}/settings"
            if row.get("site_status") != "active":
                reason = "网站已停用或归档"
            elif row.get("account_id") and row.get("scraper_type") not in {"api", "cookie"} and row.get("auth_state") != "ready":
                reason = "需要登录并验证浏览器 Profile"
            elif row.get("account_id") and not row.get("account_enabled"):
                reason = "签到账号已停用"
            elif not row.get("capability_enabled") or not row["enabled"]:
                reason = "自动调度已暂停"
            elif not row["automatic_run_supported"]:
                reason = "手工商品目录不参与自动抓取" if row.get("scraper_type") == "manual" else "尚未配置商品抓取器"
            row["blocked_reason"] = reason
            row["next_action"] = {"label": "前往账号设置" if row.get("account_id") else "前往网站设置",
                                  "href": href, "description": reason} if reason else None
            if row.get("capability") == self.CHECKIN_CODE and not reason:
                now = self._now()
                local = now.astimezone(ZoneInfo("Asia/Shanghai"))
                if row["policy"].get("mode") in {"fixed_time", "custom_window"}:
                    from site_hub.scheduling.coordinator import ScheduleCoordinator
                    coordinator = ScheduleCoordinator(self, None)
                    planned = coordinator._account_override_planned_at(UUID(row["account_id"]), local.date(), row["policy"])
                    if planned < local and row.get("last_run_at") and _moment(row["last_run_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date() == local.date():
                        planned = coordinator._account_override_planned_at(UUID(row["account_id"]), local.date()+timedelta(days=1), row["policy"])
                    row["next_run_at"] = _iso(planned)
                else:
                    batch = self._get_batch_for_date(local.date().isoformat())
                    if batch and batch.get("status") in {"pending", "running"}:
                        row["next_run_at"] = batch.get("planned_at")
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
