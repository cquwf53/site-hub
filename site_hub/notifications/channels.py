"""Secret-safe CRUD and test-event operations for notification channels."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from site_hub.db import Database
from site_hub.notifications.store import NotificationOutboxStore
from site_hub.services.secrets import SecretService


_EVENT_TYPE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_SEVERITIES = {"success", "info", "warning", "danger"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uuid(value: UUID | str) -> str:
    return str(UUID(str(value)))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _endpoint(value: Any) -> str:
    endpoint = str(value or "")
    if endpoint != endpoint.strip() or not endpoint or len(endpoint) > 8192:
        raise ValueError("webhook endpoint is invalid")
    if any(character in endpoint for character in ("\r", "\n", "\x00")):
        raise ValueError("webhook endpoint is invalid")
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
    except ValueError:
        raise ValueError("webhook endpoint is invalid") from None
    if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
        raise ValueError("webhook endpoint must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials in webhook authority are forbidden")
    return endpoint


def _config(value: Any) -> dict[str, Any]:
    if value is None:
        return {"event_types": [], "min_severity": "success"}
    if not isinstance(value, dict):
        raise ValueError("notification channel config must be an object")
    unknown = set(value) - {"event_types", "min_severity"}
    if unknown:
        raise ValueError("notification channel config contains unsupported fields")
    event_types = value.get("event_types") or []
    if not isinstance(event_types, list) or len(event_types) > 100:
        raise ValueError("event_types must be a list with at most 100 entries")
    normalized_types: list[str] = []
    for item in event_types:
        if not isinstance(item, str):
            raise ValueError("event_types entries must be strings")
        event_type = item.casefold()
        if not _EVENT_TYPE.fullmatch(event_type):
            raise ValueError("event_types contains an invalid event type")
        if event_type not in normalized_types:
            normalized_types.append(event_type)
    severity = str(value.get("min_severity") or "success").casefold()
    if severity not in _SEVERITIES:
        raise ValueError("min_severity is invalid")
    return {"event_types": normalized_types, "min_severity": severity}


def _enabled(value: Any) -> int:
    if not isinstance(value, bool):
        raise ValueError("enabled must be a boolean")
    return int(value)


class NotificationChannelService:
    """Async facade; endpoint plaintext never leaves create/update methods."""

    def __init__(self, database: Database, secrets: SecretService) -> None:
        self.database = database
        self.secrets = secrets
        self.outbox = NotificationOutboxStore(database)

    async def list(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list)

    def _list(self) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """SELECT c.*,
                          SUM(CASE WHEN o.status='pending' THEN 1 ELSE 0 END) pending_count,
                          SUM(CASE WHEN o.status='sent' THEN 1 ELSE 0 END) sent_count,
                          SUM(CASE WHEN o.status IN ('failed','dead') THEN 1 ELSE 0 END) failed_count
                   FROM notification_channels c
                   LEFT JOIN notification_outbox o ON o.channel_id=c.id
                   GROUP BY c.id ORDER BY c.created_at,c.id"""
            ).fetchall()
        return [self._public(dict(row)) for row in rows]

    async def get(self, channel_id: UUID | str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get, _uuid(channel_id))

    def _get(self, channel_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                """SELECT c.*,
                          SUM(CASE WHEN o.status='pending' THEN 1 ELSE 0 END) pending_count,
                          SUM(CASE WHEN o.status='sent' THEN 1 ELSE 0 END) sent_count,
                          SUM(CASE WHEN o.status IN ('failed','dead') THEN 1 ELSE 0 END) failed_count
                   FROM notification_channels c
                   LEFT JOIN notification_outbox o ON o.channel_id=c.id
                   WHERE c.id=? GROUP BY c.id""",
                (channel_id,),
            ).fetchone()
        return self._public(dict(row)) if row is not None else None

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._create, payload)

    def _create(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"name", "channel_type", "enabled", "endpoint", "config"}
        if set(payload) - allowed:
            raise ValueError("notification channel contains unsupported fields")
        name = str(payload.get("name") or "").strip()
        if not name or len(name) > 120:
            raise ValueError("channel name must contain 1 to 120 characters")
        channel_type = str(payload.get("channel_type") or "webhook").casefold()
        if channel_type != "webhook":
            raise ValueError("only webhook notification channels are supported")
        endpoint = _endpoint(payload.get("endpoint"))
        config = _config(payload.get("config"))
        identifier, now = str(uuid4()), _now()
        with self.database.transaction(immediate=True) as conn:
            secret_id = self.secrets.store(
                f"notification-channel:{identifier}:endpoint", endpoint, conn=conn
            )
            conn.execute(
                """INSERT INTO notification_channels(
                       id,name,channel_type,enabled,endpoint_secret_id,config_json,
                       created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    identifier,
                    name,
                    channel_type,
                    _enabled(payload.get("enabled", True)),
                    secret_id,
                    _json(config),
                    now,
                    now,
                ),
            )
        result = self._get(identifier)
        assert result is not None
        return result

    async def update(
        self, channel_id: UUID | str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._update, _uuid(channel_id), payload)

    def _update(self, channel_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"name", "enabled", "endpoint", "config"}
        if set(payload) - allowed:
            raise ValueError("notification channel update contains unsupported fields")
        with self.database.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM notification_channels WHERE id=?", (channel_id,)
            ).fetchone()
            if row is None:
                return None
            values: dict[str, Any] = {}
            if "name" in payload:
                name = str(payload.get("name") or "").strip()
                if not name or len(name) > 120:
                    raise ValueError("channel name must contain 1 to 120 characters")
                values["name"] = name
            if "enabled" in payload:
                values["enabled"] = _enabled(payload["enabled"])
            if "config" in payload:
                values["config_json"] = _json(_config(payload["config"]))
            if "endpoint" in payload:
                self.secrets.rotate(
                    str(row["endpoint_secret_id"]), _endpoint(payload["endpoint"]), conn=conn
                )
            now = _now()
            if values:
                assignments = ",".join(f"{key}=?" for key in values)
                conn.execute(
                    f"UPDATE notification_channels SET {assignments},updated_at=? WHERE id=?",
                    (*values.values(), now, channel_id),
                )
            elif "endpoint" in payload:
                conn.execute(
                    "UPDATE notification_channels SET updated_at=? WHERE id=?",
                    (now, channel_id),
                )
            if values.get("enabled") == 0:
                conn.execute(
                    """UPDATE notification_outbox
                       SET status='cancelled',lease_owner=NULL,lease_until=NULL,
                           last_error='notification channel disabled',updated_at=?
                       WHERE channel_id=? AND status IN ('pending','leased')""",
                    (now, channel_id),
                )
        return self._get(channel_id)

    async def delete(self, channel_id: UUID | str) -> bool:
        return await asyncio.to_thread(self._delete, _uuid(channel_id))

    def _delete(self, channel_id: str) -> bool:
        with self.database.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT endpoint_secret_id FROM notification_channels WHERE id=?",
                (channel_id,),
            ).fetchone()
            if row is None:
                return False
            secret_id = str(row["endpoint_secret_id"])
            conn.execute("DELETE FROM notification_channels WHERE id=?", (channel_id,))
        self.secrets.delete_if_unreferenced(secret_id)
        return True

    async def enqueue_test(self, channel_id: UUID | str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._enqueue_test, _uuid(channel_id))

    def _enqueue_test(self, channel_id: str) -> dict[str, Any] | None:
        now, event_id = _now(), str(uuid4())
        with self.database.transaction(immediate=True) as conn:
            channel = conn.execute(
                "SELECT id FROM notification_channels WHERE id=? AND enabled=1",
                (channel_id,),
            ).fetchone()
            if channel is None:
                return None
            conn.execute(
                """INSERT INTO events(
                       id,type,severity,entity_type,entity_id,title,detail,payload_json,
                       occurred_at,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    "notification.test",
                    "info",
                    "notification_channel",
                    channel_id,
                    "SiteHub notification test",
                    "This is a test notification from SiteHub.",
                    "{}",
                    now,
                    now,
                ),
            )
            result = self.outbox.enqueue_event_locked(
                conn,
                event_id,
                now=now,
                channel_ids=(channel_id,),
                respect_filters=False,
            )
        if not result.outbox_ids:
            return None
        return {"outbox_id": result.outbox_ids[0], "status": "pending"}

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        try:
            config = json.loads(row.get("config_json") or "{}")
        except (TypeError, ValueError):
            config = {}
        return {
            "id": row["id"],
            "name": row["name"],
            "channel_type": row["channel_type"],
            "enabled": bool(row["enabled"]),
            "endpoint_configured": bool(row.get("endpoint_secret_id")),
            "config": config,
            "delivery": {
                "pending": int(row.get("pending_count") or 0),
                "sent": int(row.get("sent_count") or 0),
                "failed": int(row.get("failed_count") or 0),
            },
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


__all__ = ["NotificationChannelService"]
