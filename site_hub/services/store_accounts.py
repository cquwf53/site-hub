from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from site_hub.scrapers.network import sanitize_log_value
from site_hub.services.ports import AccountNotReady


from site_hub.services.store_support import _iso, _loads, _dumps, _uuid


class AccountStoreMixin:
    """Accounts domain operations; transactions remain owned by each operation."""

    async def list_accounts(self, site_id: UUID) -> list[dict[str, Any]]:
        return await self._call(self._account_rows, _uuid(site_id))

    async def get_account(self, account_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._account_row, _uuid(account_id), True)

    def _account_rows(self, site_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """SELECT a.*,c.id capability_id,c.adapter,c.config_json,
                           c.enabled capability_enabled,
                           s.id schedule_id,s.policy_type,s.policy_json,
                           s.enabled schedule_enabled,
                          bp.relative_path profile_path,bp.state profile_state,
                          bp.manifest_sha256 profile_manifest_sha256,
                          bp.file_count profile_file_count,bp.total_bytes profile_total_bytes,
                          bp.last_verified_at profile_last_verified_at,
                          bp.verification_error profile_verification_error
                   FROM site_accounts a
                   LEFT JOIN capability_instances c ON c.account_id=a.id AND c.code='checkin'
                   LEFT JOIN schedules s ON s.capability_instance_id=c.id
                   LEFT JOIN browser_profiles bp ON bp.account_id=a.id
                   WHERE a.site_id=? AND a.auth_state!='disabled' ORDER BY a.sort_order,a.id""",
                (site_id,),
            ).fetchall()
            return [self._normalize_account(dict(row), runtime=False) for row in rows]

    def _account_row(self, account_id: str, runtime: bool = False) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                """SELECT a.*,c.id capability_id,c.adapter,c.config_json,
                           c.enabled capability_enabled,
                           s.id schedule_id,s.policy_type,s.policy_json,
                           s.enabled schedule_enabled,
                          bp.relative_path profile_path,bp.state profile_state,
                          bp.manifest_sha256 profile_manifest_sha256,
                          bp.file_count profile_file_count,bp.total_bytes profile_total_bytes,
                          bp.last_verified_at profile_last_verified_at,
                          bp.verification_error profile_verification_error
                   FROM site_accounts a
                   LEFT JOIN capability_instances c ON c.account_id=a.id AND c.code='checkin'
                   LEFT JOIN schedules s ON s.capability_instance_id=c.id
                   LEFT JOIN browser_profiles bp ON bp.account_id=a.id
                   WHERE a.id=? AND a.auth_state!='disabled' ORDER BY s.id LIMIT 1""",
                (account_id,),
            ).fetchone()
            return self._normalize_account(dict(row), runtime=runtime) if row else None

    def _normalize_account(self, row: dict[str, Any], *, runtime: bool) -> dict[str, Any]:
        config = _loads(row.get("config_json"), {})
        secret_fields = list(config.pop("_secret_fields", []))
        policy = _loads(row.get("policy_json"), {})
        override = None
        if row.get("policy_type") == "cron" and policy.get("mode") == "fixed_time":
            override = {"mode": "fixed_time", "time": policy.get("time"), "timezone": policy.get("timezone", "Asia/Shanghai")}
        elif row.get("policy_type") == "daily_random_window" and policy.get("mode") == "custom_window":
            override = {
                "mode": "custom_window", "start": policy.get("start"),
                "end": policy.get("end"), "timezone": policy.get("timezone", "Asia/Shanghai"),
            }
        result = {
            "id": row["id"], "site_id": row["site_id"], "name": row["label"],
            "label": row["label"],
            "adapter": row.get("adapter") or "playwright",
            "enabled": (
                bool(row["enabled"])
                and bool(row.get("capability_enabled"))
                and bool(row.get("schedule_enabled"))
            ),
            "schedule_override": override, "profile_key": row.get("profile_path") or row["id"],
            "config": config,
            "configured": bool(config or secret_fields), "configured_secret_fields": secret_fields,
            "login_state": row.get("auth_state") or "unverified",
            "auth_state": row.get("auth_state") or "unverified",
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "profile": {
                "relative_path": row.get("profile_path") or row["id"],
                "state": row.get("profile_state") or "unverified",
                "manifest_sha256": row.get("profile_manifest_sha256"),
                "file_count": row.get("profile_file_count"),
                "total_bytes": row.get("profile_total_bytes"),
                "last_verified_at": row.get("profile_last_verified_at"),
                "verification_error": row.get("profile_verification_error"),
            },
        }
        if runtime:
            result["runtime_config"] = self._restore_secrets(row.get("capability_id"), config, secret_fields)
        result["schedule"] = override
        return result

    async def create_account(self, site_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._call(self._create_account_sync, _uuid(site_id), payload)

    def _create_account_sync(self, site_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = str(uuid4())
        capability_id = str(uuid4())
        profile_id = str(uuid4())
        schedule_id = str(uuid4())
        adapter = str(payload.get("adapter", "playwright"))
        # Browser-backed accounts cannot be safely scheduled until a live
        # Profile verification succeeds. API/cookie adapters do not depend on
        # the browser Profile and retain their explicit enabled choice.
        enabled = bool(payload.get("enabled", False)) and adapter.lower() in {"api", "cookie"}
        clean, secrets = self._split_secrets(payload.get("config") or {})
        clean["_secret_fields"] = sorted(secrets)
        override = payload.get("schedule_override")
        policy_type, policy = self._schedule_policy(override)
        now = _iso(self._now())
        with self.database.transaction(immediate=True) as conn:
            if not conn.execute("SELECT 1 FROM sites WHERE id=?", (site_id,)).fetchone():
                raise LookupError("site not found")
            conn.execute(
                """INSERT INTO site_accounts(
                       id,site_id,label,enabled,auth_state,sort_order,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (account_id, site_id, payload["name"], int(enabled), "unverified", 0, now, now),
            )
            conn.execute(
                """INSERT INTO capability_instances(
                       id,site_id,code,account_id,adapter,config_version,config_json,
                       enabled,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    capability_id, site_id, self.CHECKIN_CODE, account_id,
                    adapter, 1, _dumps(clean),
                    int(enabled), now, now,
                ),
            )
            self._store_secrets(
                "account", account_id, capability_id, secrets, conn=conn,
            )
            conn.execute(
                """INSERT INTO browser_profiles(
                       id,account_id,relative_path,state,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?)""",
                (profile_id, account_id, account_id, "unverified", now, now),
            )
            conn.execute(
                """INSERT INTO schedules(
                       id,capability_instance_id,timezone,policy_type,policy_json,enabled,
                       misfire_policy,max_attempts,backoff_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    schedule_id, capability_id, "Asia/Shanghai", policy_type,
                    _dumps(policy), int(enabled), "recover", 3, "{}", now, now,
                ),
            )
        return self._account_row(account_id)  # type: ignore[return-value]

    async def update_account(self, account_id: UUID, payload: dict[str, Any]) -> dict[str, Any] | None:
        return await self._call(self._update_account_sync, _uuid(account_id), payload)

    def _update_account_sync(self, account_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self.database.transaction(immediate=True) as conn:
            account = conn.execute(
                "SELECT id,enabled,auth_state FROM site_accounts WHERE id=? AND auth_state!='disabled'", (account_id,)
            ).fetchone()
            if not account:
                return None
            capability = conn.execute(
                "SELECT * FROM capability_instances WHERE account_id=? AND code='checkin'", (account_id,)
            ).fetchone()
            schedule = conn.execute(
                "SELECT * FROM schedules WHERE capability_instance_id=? ORDER BY id LIMIT 1", (capability["id"],)
            ).fetchone() if capability else None
            if not capability:
                raise RuntimeError("account has no check-in capability")
            next_adapter = str(payload.get("adapter", capability["adapter"]) or "").lower()
            currently_enabled = (
                bool(account["enabled"])
                and bool(capability["enabled"])
                and bool(schedule and schedule["enabled"])
            )
            next_enabled = bool(payload["enabled"]) if "enabled" in payload else currently_enabled
            activation_requested = payload.get("enabled") is True or (
                "adapter" in payload and next_enabled
            )
            if (
                activation_requested
                and next_adapter not in {"api", "cookie"}
                and account["auth_state"] != "ready"
            ):
                raise AccountNotReady("Verify the browser Profile before enabling this account")
            now_value = self._now()
            now = _iso(now_value)
            account_fields: list[str] = []
            account_values: list[Any] = []
            if "name" in payload:
                account_fields.append("label=?"); account_values.append(payload["name"])
            if "enabled" in payload:
                account_fields.append("enabled=?"); account_values.append(int(bool(payload["enabled"])))
            if account_fields:
                account_values.extend([now, account_id])
                conn.execute(
                    f"UPDATE site_accounts SET {','.join(account_fields)},updated_at=? WHERE id=?",
                    account_values,
                )
            cap_changes: dict[str, Any] = {}
            if "adapter" in payload:
                cap_changes["adapter"] = str(payload["adapter"])
            if "enabled" in payload:
                cap_changes["enabled"] = payload["enabled"]
            self._update_capability_atomic(
                capability["id"], owner="account", account_id=account_id,
                changes=cap_changes, config=payload.get("config"),
                config_supplied=payload.get("config") is not None,
                clear_secrets=set(payload.get("clear_secrets") or []), conn=conn,
            )
            if schedule:
                schedule_fields: list[str] = []
                schedule_values: list[Any] = []
                if payload.get("clear_schedule_override"):
                    schedule_fields.extend(["policy_type=?", "policy_json=?"])
                    schedule_values.extend(["daily_random_window", _dumps({})])
                elif payload.get("schedule_override") is not None:
                    policy_type, policy = self._schedule_policy(payload["schedule_override"])
                    schedule_fields.extend(["policy_type=?", "policy_json=?"])
                    schedule_values.extend([policy_type, _dumps(policy)])
                if "enabled" in payload:
                    schedule_fields.append("enabled=?"); schedule_values.append(int(bool(payload["enabled"])))
                if schedule_fields:
                    schedule_values.extend([now, schedule["id"]])
                    conn.execute(
                        f"UPDATE schedules SET {','.join(schedule_fields)},updated_at=? WHERE id=?",
                        schedule_values,
                    )
            if payload.get("enabled") is True and next_adapter not in {"api", "cookie"}:
                self._backfill_enabled_browser_account_locked(
                    conn, account_id, now_value,
                )
            if "enabled" in payload and not bool(payload["enabled"]):
                self._cancel_queued_checkins_locked(conn, now, account_id=account_id)
        return self._account_row(account_id)

    async def delete_account(self, account_id: UUID) -> bool:
        return await self._call(self._archive_account_sync, _uuid(account_id))

    def _archive_account_sync(self, account_id: str) -> bool:
        now = _iso(self._now())
        with self.database.transaction(immediate=True) as conn:
            changed = conn.execute(
                "UPDATE site_accounts SET enabled=0,auth_state='disabled',updated_at=? WHERE id=? AND auth_state!='disabled'",
                (now, account_id),
            ).rowcount
            if not changed: return False
            conn.execute("UPDATE capability_instances SET enabled=0,updated_at=? WHERE account_id=?", (now, account_id))
            conn.execute(
                """UPDATE schedules SET enabled=0,updated_at=? WHERE capability_instance_id IN
                   (SELECT id FROM capability_instances WHERE account_id=?)""", (now, account_id)
            )
            self._cancel_queued_checkins_locked(conn, now, account_id=account_id)
            return True

    async def set_account_auth_state(self, account_id: UUID, state: str,
                                     error: str | None = None) -> dict[str, Any] | None:
        allowed = {"ready", "expired", "error", "unverified", "disabled"}
        normalized = state if state in allowed else "unverified"
        return await self._call(
            self.repository.update_site_account, _uuid(account_id), auth_state=normalized,
            verification_error=error,
        )

    async def set_account_profile_verification(
        self, account_id: UUID, state: str, error: str | None = None,
        verified_at: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically persist a read-only Profile verification result.

        ``busy`` is deliberately Profile-only: transient worker activity must
        not be squeezed into the terminal ``site_accounts.auth_state`` enum.
        """
        return await self._call(
            self._set_account_profile_verification_sync,
            _uuid(account_id), state, error, verified_at,
        )

    def _set_account_profile_verification_sync(
        self, account_id: str, state: str, error: str | None,
        verified_at: str | None,
    ) -> dict[str, Any] | None:
        normalized = str(state or "error").lower()
        if normalized not in {"ready", "expired", "error", "busy", "unverified"}:
            normalized = "error"
        safe_error = sanitize_log_value(error, max_length=500) if error else None
        now = _iso(self._now())
        success_at = _iso(verified_at) if normalized == "ready" and verified_at else now

        with self.database.transaction(immediate=True) as conn:
            account = conn.execute(
                "SELECT auth_state FROM site_accounts WHERE id=? AND auth_state!='disabled'",
                (account_id,),
            ).fetchone()
            if not account:
                return None

            if normalized == "busy":
                conn.execute(
                    """UPDATE browser_profiles
                       SET state='busy',verification_error=?,updated_at=?
                       WHERE account_id=? AND state!='archived'""",
                    (safe_error, now, account_id),
                )
            else:
                account_state = normalized if normalized in {"ready", "expired", "error"} else "unverified"
                profile_state = "ready" if normalized == "ready" else (
                    "unverified" if normalized == "unverified" else "error"
                )
                if normalized == "ready":
                    conn.execute(
                        """UPDATE site_accounts
                           SET auth_state='ready',last_verified_at=?,verification_error=NULL,updated_at=?
                           WHERE id=? AND auth_state!='disabled'""",
                        (success_at, now, account_id),
                    )
                    conn.execute(
                        """UPDATE browser_profiles
                           SET state='ready',last_verified_at=?,verification_error=NULL,updated_at=?
                           WHERE account_id=? AND state!='archived'""",
                        (success_at, now, account_id),
                    )
                else:
                    conn.execute(
                        """UPDATE site_accounts
                           SET enabled=0,auth_state=?,verification_error=?,updated_at=?
                           WHERE id=? AND auth_state!='disabled'""",
                        (account_state, safe_error, now, account_id),
                    )
                    conn.execute(
                        """UPDATE browser_profiles
                           SET state=?,verification_error=?,updated_at=?
                           WHERE account_id=? AND state!='archived'""",
                        (profile_state, safe_error, now, account_id),
                    )
                    conn.execute(
                        "UPDATE capability_instances SET enabled=0,updated_at=? WHERE account_id=? AND code='checkin'",
                        (now, account_id),
                    )
                    conn.execute(
                        """UPDATE schedules SET enabled=0,updated_at=? WHERE capability_instance_id IN
                           (SELECT id FROM capability_instances WHERE account_id=? AND code='checkin')""",
                        (now, account_id),
                    )
                    self._cancel_queued_checkins_locked(conn, now, account_id=account_id)
        return self._account_row(account_id)
