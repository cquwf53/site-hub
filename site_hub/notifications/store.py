"""Atomic SQLite state transitions for the notification outbox."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from site_hub.db import Database
from site_hub.notifications.models import (
    DeliveryResult,
    EnqueueResult,
    NotificationLease,
    NotificationPolicy,
    RecoveryResult,
)


FAILURE_EVENT_TYPE = "notification.delivery_failed"
_FAILURE_DEDUPE_PREFIX = "notification-delivery-failed:"
_SAFE_ERROR_MESSAGES = {
    "endpoint_unavailable": "notification endpoint could not be decrypted",
    "invalid_endpoint": "webhook endpoint must be an HTTP or HTTPS URL",
    "unsupported_channel": "notification channel type is unsupported",
    "transport_error": "webhook transport failed",
    "delivery_timeout": "notification delivery exceeded its total timeout",
    "invalid_response": "webhook returned an invalid response",
    "redirect_missing": "webhook redirect did not provide a location",
    "redirect_limit": "webhook redirect limit was exceeded",
    "response_too_large": "webhook response exceeded the configured body limit",
    "http_client_error": "webhook rejected the notification",
    "http_server_error": "webhook was temporarily unavailable",
    "lease_expired": "notification delivery lease expired",
    "attempts_exhausted": "notification delivery attempts were exhausted",
    "delivery_error": "notification delivery failed",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    normalized = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def _safe_error(code: str) -> tuple[str, str]:
    normalized = re.sub(r"[^a-z0-9_.-]", "", str(code).casefold())[:64]
    if normalized not in _SAFE_ERROR_MESSAGES:
        normalized = "delivery_error"
    return normalized, _SAFE_ERROR_MESSAGES[normalized]


def _safe_response_code(value: str | int | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if re.fullmatch(r"[1-5][0-9]{2}", text) else None


class NotificationOutboxStore:
    """Synchronous SQLite store designed to be called through ``to_thread``."""

    def __init__(
        self,
        database: Database,
        *,
        policy: NotificationPolicy | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.database = database
        self.policy = policy or NotificationPolicy()
        self.clock = clock

    @staticmethod
    def idempotency_key(event_id: str, channel_id: str) -> str:
        digest = hashlib.sha256(
            f"{event_id}\0{channel_id}".encode("utf-8")
        ).hexdigest()
        return f"notification:v1:{digest}"

    def enqueue_event(self, event_id: str) -> EnqueueResult:
        """Fan one existing business event out to every currently enabled channel."""

        now = _iso(self.clock())
        with self.database.transaction(immediate=True) as conn:
            return self.enqueue_event_locked(conn, event_id, now=now)

    def enqueue_event_locked(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        *,
        now: str | None = None,
        channel_ids: tuple[str, ...] | None = None,
        respect_filters: bool = True,
    ) -> EnqueueResult:
        """Fan out an event using the caller's transaction.

        Business code uses this method immediately after inserting ``events`` so
        the event and every outbox row commit or roll back together.  A channel's
        optional ``event_types`` and ``min_severity`` config is evaluated here;
        neither the endpoint nor any other secret is loaded.
        """

        if not conn.in_transaction:
            raise RuntimeError("enqueue_event_locked requires an active transaction")

        timestamp = now or _iso(self.clock())
        event = conn.execute(
            "SELECT id,type,severity FROM events WHERE id=?", (event_id,)
        ).fetchone()
        if event is None:
            raise KeyError(event_id)
        # Delivery failures are actionable UI events, but never notifications.
        if event["type"] == FAILURE_EVENT_TYPE:
            return EnqueueResult(event_id, 0, 0, ())

        sql = "SELECT id,config_json FROM notification_channels WHERE enabled=1"
        params: list[Any] = []
        if channel_ids is not None:
            wanted = tuple(dict.fromkeys(str(value) for value in channel_ids))
            if not wanted:
                return EnqueueResult(event_id, 0, 0, ())
            sql += " AND id IN (" + ",".join("?" for _ in wanted) + ")"
            params.extend(wanted)
        channels = conn.execute(sql + " ORDER BY id", params).fetchall()

        created = existing = 0
        outbox_ids: list[str] = []
        for channel in channels:
            try:
                config = json.loads(channel["config_json"] or "{}")
            except (TypeError, ValueError):
                config = {}
            if respect_filters and not self._channel_accepts_event(
                config, str(event["type"]), str(event["severity"])
            ):
                continue
            channel_id = str(channel["id"])
            key = self.idempotency_key(event_id, channel_id)
            candidate_id = str(uuid4())
            inserted = conn.execute(
                """INSERT INTO notification_outbox(
                       id,event_id,channel_id,idempotency_key,status,attempt,
                       available_at,created_at,updated_at
                   ) VALUES(?,?,?,?, 'pending',0,?,?,?)
                   ON CONFLICT(idempotency_key) DO NOTHING""",
                (candidate_id, event_id, channel_id, key, timestamp, timestamp, timestamp),
            ).rowcount
            if inserted:
                created += 1
                outbox_ids.append(candidate_id)
            else:
                existing += 1
                row = conn.execute(
                    "SELECT id FROM notification_outbox WHERE idempotency_key=?",
                    (key,),
                ).fetchone()
                if row is not None:
                    outbox_ids.append(str(row["id"]))
        return EnqueueResult(event_id, created, existing, tuple(outbox_ids))

    @staticmethod
    def _channel_accepts_event(config: Any, event_type: str, severity: str) -> bool:
        if not isinstance(config, dict):
            return True
        configured_types = config.get("event_types")
        if isinstance(configured_types, list) and configured_types:
            if event_type not in {str(value) for value in configured_types}:
                return False
        levels = {"success": 0, "info": 1, "warning": 2, "danger": 3}
        minimum = str(config.get("min_severity") or "success")
        return levels.get(severity, 1) >= levels.get(minimum, 0)

    def recover_expired(self) -> RecoveryResult:
        now_dt = self.clock()
        with self.database.transaction(immediate=True) as conn:
            return self._recover_locked(conn, now_dt)

    def claim(self, worker_id: str) -> NotificationLease | None:
        """Recover expired work and atomically claim at most one ready item."""

        worker = str(worker_id)
        if not worker or len(worker) > 150:
            raise ValueError("worker id must contain 1 to 150 characters")
        # A fresh token for every claim prevents a late completion from an old
        # attempt succeeding when the same long-lived worker reclaims the row.
        owner = f"{worker}:{uuid4().hex}"
        now_dt = self.clock()
        now = _iso(now_dt)
        lease_until = _iso(now_dt + timedelta(seconds=self.policy.lease_seconds))
        with self.database.transaction(immediate=True) as conn:
            self._recover_locked(conn, now_dt)
            candidate = conn.execute(
                """SELECT o.id FROM notification_outbox o
                   JOIN notification_channels c ON c.id=o.channel_id
                   WHERE o.status='pending' AND o.available_at<=?
                     AND o.attempt<? AND c.enabled=1
                   ORDER BY o.available_at,o.created_at,o.id LIMIT 1""",
                (now, self.policy.max_attempts),
            ).fetchone()
            if candidate is None:
                return None
            updated = conn.execute(
                """UPDATE notification_outbox
                   SET status='leased',lease_owner=?,lease_until=?,attempt=attempt+1,
                       updated_at=?
                   WHERE id=? AND status='pending' AND available_at<=?""",
                (owner, lease_until, now, candidate["id"], now),
            ).rowcount
            if updated != 1:  # Defensive; BEGIN IMMEDIATE should make this unreachable.
                return None
            row = conn.execute(
                """SELECT o.*,c.channel_type,c.endpoint_secret_id,
                          e.type AS event_type,e.severity,e.entity_type,e.entity_id,
                          e.title,e.detail,e.payload_json,e.occurred_at
                   FROM notification_outbox o
                   JOIN notification_channels c ON c.id=o.channel_id
                   JOIN events e ON e.id=o.event_id
                   WHERE o.id=?""",
                (candidate["id"],),
            ).fetchone()
            if row is None:
                return None
            return self._lease_from_row(row)

    def mark_sent(
        self,
        outbox_id: str,
        lease_owner: str,
        *,
        response_code: str | int | None,
    ) -> bool:
        now = _iso(self.clock())
        with self.database.transaction(immediate=True) as conn:
            return conn.execute(
                """UPDATE notification_outbox
                   SET status='sent',response_code=?,last_error=NULL,
                       lease_owner=NULL,lease_until=NULL,updated_at=?
                   WHERE id=? AND status='leased' AND lease_owner=?""",
                (
                    _safe_response_code(response_code),
                    now,
                    outbox_id,
                    lease_owner,
                ),
            ).rowcount == 1

    def mark_failure(
        self,
        outbox_id: str,
        lease_owner: str,
        *,
        error_code: str,
        retryable: bool,
        response_code: str | int | None = None,
    ) -> DeliveryResult:
        """Persist a safe failure, scheduling retry or creating a final event."""

        now_dt = self.clock()
        now = _iso(now_dt)
        code, message = _safe_error(error_code)
        safe_response = _safe_response_code(response_code)
        with self.database.transaction(immediate=True) as conn:
            row = conn.execute(
                """SELECT id,event_id,channel_id,attempt FROM notification_outbox
                   WHERE id=? AND status='leased' AND lease_owner=?""",
                (outbox_id, lease_owner),
            ).fetchone()
            if row is None:
                return DeliveryResult(
                    outbox_id=outbox_id,
                    status="stale",
                    attempt=0,
                    response_code=safe_response,
                    error_code=code,
                )

            attempt = int(row["attempt"])
            if retryable and attempt < self.policy.max_attempts:
                retry_at = _iso(
                    now_dt + timedelta(seconds=self.policy.retry_delay(attempt))
                )
                conn.execute(
                    """UPDATE notification_outbox
                       SET status='pending',available_at=?,response_code=?,last_error=?,
                           lease_owner=NULL,lease_until=NULL,updated_at=?
                       WHERE id=? AND status='leased' AND lease_owner=?""",
                    (
                        retry_at,
                        safe_response,
                        f"{code}: {message}",
                        now,
                        outbox_id,
                        lease_owner,
                    ),
                )
                return DeliveryResult(
                    outbox_id=outbox_id,
                    status="pending",
                    attempt=attempt,
                    response_code=safe_response,
                    error_code=code,
                    retry_at=retry_at,
                )

            final_status = "dead" if retryable else "failed"
            updated = conn.execute(
                """UPDATE notification_outbox
                   SET status=?,response_code=?,last_error=?,lease_owner=NULL,
                       lease_until=NULL,updated_at=?
                   WHERE id=? AND status='leased' AND lease_owner=?""",
                (
                    final_status,
                    safe_response,
                    f"{code}: {message}",
                    now,
                    outbox_id,
                    lease_owner,
                ),
            ).rowcount
            if updated:
                self._insert_failure_event_locked(
                    conn,
                    outbox_id=outbox_id,
                    source_event_id=str(row["event_id"]),
                    channel_id=str(row["channel_id"]),
                    attempt=attempt,
                    reason_code=code,
                    now=now,
                )
            return DeliveryResult(
                outbox_id=outbox_id,
                status=final_status if updated else "stale",
                attempt=attempt,
                response_code=safe_response,
                error_code=code,
            )

    def get_outbox(self, outbox_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_outbox WHERE id=?", (outbox_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def _recover_locked(
        self, conn: sqlite3.Connection, now_dt: datetime
    ) -> RecoveryResult:
        now = _iso(now_dt)
        cancelled = conn.execute(
            """UPDATE notification_outbox
               SET status='cancelled',last_error='notification channel disabled',
                   lease_owner=NULL,lease_until=NULL,updated_at=?
               WHERE channel_id IN (
                   SELECT id FROM notification_channels WHERE enabled=0
               ) AND (
                   status='pending' OR
                   (status='leased' AND lease_until IS NOT NULL AND lease_until<=?)
               )""",
            (now, now),
        ).rowcount

        expired = conn.execute(
            """SELECT id,event_id,channel_id,attempt
               FROM notification_outbox
               WHERE status='leased' AND lease_until IS NOT NULL AND lease_until<=?
               ORDER BY lease_until,id""",
            (now,),
        ).fetchall()
        retried = dead = 0
        for row in expired:
            attempt = int(row["attempt"])
            if attempt >= self.policy.max_attempts:
                conn.execute(
                    """UPDATE notification_outbox
                       SET status='dead',last_error=?,lease_owner=NULL,lease_until=NULL,
                           updated_at=? WHERE id=? AND status='leased'""",
                    (
                        "lease_expired: " + _SAFE_ERROR_MESSAGES["lease_expired"],
                        now,
                        row["id"],
                    ),
                )
                self._insert_failure_event_locked(
                    conn,
                    outbox_id=str(row["id"]),
                    source_event_id=str(row["event_id"]),
                    channel_id=str(row["channel_id"]),
                    attempt=attempt,
                    reason_code="lease_expired",
                    now=now,
                )
                dead += 1
            else:
                retry_at = _iso(
                    now_dt + timedelta(seconds=self.policy.retry_delay(attempt))
                )
                conn.execute(
                    """UPDATE notification_outbox
                       SET status='pending',available_at=?,last_error=?,lease_owner=NULL,
                           lease_until=NULL,updated_at=?
                       WHERE id=? AND status='leased'""",
                    (
                        retry_at,
                        "lease_expired: " + _SAFE_ERROR_MESSAGES["lease_expired"],
                        now,
                        row["id"],
                    ),
                )
                retried += 1

        exhausted = conn.execute(
            """SELECT id,event_id,channel_id,attempt FROM notification_outbox
               WHERE status='pending' AND attempt>=? AND available_at<=?""",
            (self.policy.max_attempts, now),
        ).fetchall()
        for row in exhausted:
            changed = conn.execute(
                """UPDATE notification_outbox SET status='dead',last_error=?,
                       lease_owner=NULL,lease_until=NULL,updated_at=?
                   WHERE id=? AND status='pending'""",
                (
                    "attempts_exhausted: "
                    + _SAFE_ERROR_MESSAGES["attempts_exhausted"],
                    now,
                    row["id"],
                ),
            ).rowcount
            if changed:
                self._insert_failure_event_locked(
                    conn,
                    outbox_id=str(row["id"]),
                    source_event_id=str(row["event_id"]),
                    channel_id=str(row["channel_id"]),
                    attempt=int(row["attempt"]),
                    reason_code="attempts_exhausted",
                    now=now,
                )
                dead += 1
        return RecoveryResult(retried=retried, dead=dead, cancelled=cancelled)

    @staticmethod
    def _lease_from_row(row: sqlite3.Row) -> NotificationLease:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = None
        return NotificationLease(
            outbox_id=str(row["id"]),
            event_id=str(row["event_id"]),
            channel_id=str(row["channel_id"]),
            channel_type=str(row["channel_type"]),
            idempotency_key=str(row["idempotency_key"]),
            attempt=int(row["attempt"]),
            lease_owner=str(row["lease_owner"]),
            endpoint_secret_id=str(row["endpoint_secret_id"]),
            event_type=str(row["event_type"]),
            severity=str(row["severity"]),
            entity_type=str(row["entity_type"]),
            entity_id=str(row["entity_id"]) if row["entity_id"] is not None else None,
            title=str(row["title"]),
            detail=str(row["detail"]),
            payload=payload,
            occurred_at=str(row["occurred_at"]),
        )

    @staticmethod
    def _insert_failure_event_locked(
        conn: sqlite3.Connection,
        *,
        outbox_id: str,
        source_event_id: str,
        channel_id: str,
        attempt: int,
        reason_code: str,
        now: str,
    ) -> None:
        code, message = _safe_error(reason_code)
        conn.execute(
            """INSERT INTO events(
                   id,type,severity,entity_type,entity_id,title,detail,payload_json,
                   dedupe_key,occurred_at,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(dedupe_key) DO NOTHING""",
            (
                str(uuid4()),
                FAILURE_EVENT_TYPE,
                "danger",
                "notification_outbox",
                outbox_id,
                "Notification delivery failed",
                f"Delivery stopped after {attempt} attempt(s): {message}",
                _json(
                    {
                        "outbox_id": outbox_id,
                        "source_event_id": source_event_id,
                        "channel_id": channel_id,
                        "attempt": attempt,
                        "reason_code": code,
                        "notification_suppressed": True,
                    }
                ),
                _FAILURE_DEDUPE_PREFIX + outbox_id,
                now,
                now,
            ),
        )


__all__ = [
    "FAILURE_EVENT_TYPE",
    "NotificationOutboxStore",
]
