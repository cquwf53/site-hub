from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from site_hub.db import Database, SiteHubRepository
from site_hub.notifications import NotificationOutboxStore


from site_hub.services.store_support import T, _iso, _loads, _dumps
from site_hub.services.store_sites import SiteStoreMixin
from site_hub.services.store_accounts import AccountStoreMixin
from site_hub.services.store_products import ProductStoreMixin
from site_hub.services.store_jobs import JobStoreMixin
from site_hub.services.store_schedules import ScheduleStoreMixin
from site_hub.services.store_activity import ActivityStoreMixin


class DataLayerStore(SiteStoreMixin, AccountStoreMixin, ProductStoreMixin, JobStoreMixin, ScheduleStoreMixin, ActivityStoreMixin):
    """Async control-plane adapter over the synchronous, durable SQLite layer.

    The adapter deliberately owns API-shaped joins and queue state transitions;
    the lower data layer stays small and reusable. Every blocking database call
    runs in a worker thread, while claim/heartbeat/finish transitions use
    ``BEGIN IMMEDIATE`` and a per-lease random token.
    """

    ROOT_FOLDER_ID = "00000000-0000-0000-0000-000000000001"
    CHECKIN_CODE = "checkin"
    PRODUCT_CODE = "product_monitor"

    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock
        self.database = Database(path)
        self.repository = SiteHubRepository(self.database)
        self.notification_outbox = NotificationOutboxStore(self.database)
        self.secret_service: Any | None = None

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

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


    # -- Sites -------------------------------------------------------


    # -- Accounts ----------------------------------------------------


    # -- Product monitoring ------------------------------------------


    # -- Durable job queue -------------------------------------------


    # -- Settings and schedules -------------------------------------
    async def get_settings(self) -> dict[str, str]:
        values = await self._call(self.repository.list_settings)
        return {key: str(value) for key, value in values.items()}

    async def update_settings(self, values: dict[str, str]) -> dict[str, str]:
        for key, value in values.items():
            await self._call(self.repository.set_setting, key, value)
        return await self.get_settings()


    # -- Artifacts ----------------------------------------------------


    # -- Supporting UI resources ------------------------------------


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
