from __future__ import annotations

import hashlib
import math
import sqlite3
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from site_hub.services.ports import StaleJobWrite


from site_hub.services.product_monitor import read_policy, read_rule, confirm_availability, qualifying_alerts
from site_hub.services.store_support import _iso, _loads, _dumps, _uuid


class ProductStoreMixin:
    """Products domain operations; transactions remain owned by each operation."""

    def _ensure_product_capability_sync(
        self,
        site_id: str,
        *,
        enabled: bool | None = None,
        default_enabled: bool = False,
        adapter: str = "none",
    ) -> dict[str, Any]:
        """Ensure a site-level product capability has exactly the runnable pieces.

        This is intentionally a write-only helper. Read endpoints never call it
        unless their caller explicitly requested creation. An explicit ``enabled``
        value synchronizes the capability and its schedule; ``None`` preserves an
        existing paused schedule (notably during legacy cutover rehearsals).
        """
        with self.database.transaction(immediate=True) as conn:
            return self._ensure_product_capability_locked(
                conn, site_id, enabled=enabled, default_enabled=default_enabled,
                adapter=adapter,
            )

    def _ensure_product_capability_locked(
        self,
        conn: sqlite3.Connection,
        site_id: str,
        *,
        enabled: bool | None = None,
        default_enabled: bool = False,
        adapter: str = "none",
    ) -> dict[str, Any]:
        """Conn-aware product capability/schedule upsert for a caller-owned transaction."""
        now = _iso(self._now())
        capability = conn.execute(
            """SELECT * FROM capability_instances
               WHERE site_id=? AND code=? AND account_id IS NULL""",
            (site_id, self.PRODUCT_CODE),
        ).fetchone()
        if capability is None:
            capability_id = str(uuid4())
            effective_enabled = bool(default_enabled if enabled is None else enabled)
            conn.execute(
                """INSERT INTO capability_instances(
                       id,site_id,code,account_id,endpoint_id,adapter,config_version,
                       config_json,enabled,created_at,updated_at
                   ) VALUES(?,?,?,NULL,NULL,?,1,'{}',?,?,?)""",
                (
                    capability_id, site_id, self.PRODUCT_CODE, adapter,
                    int(effective_enabled), now, now,
                ),
            )
        else:
            capability_id = str(capability["id"])
            effective_enabled = bool(capability["enabled"])
            if enabled is not None:
                effective_enabled = bool(enabled)
                conn.execute(
                    "UPDATE capability_instances SET enabled=?,updated_at=? WHERE id=?",
                    (int(effective_enabled), now, capability_id),
                )

        schedule = conn.execute(
            "SELECT * FROM schedules WHERE capability_instance_id=? ORDER BY id LIMIT 1",
            (capability_id,),
        ).fetchone()
        if schedule is None:
            setting = conn.execute(
                "SELECT value_json FROM settings WHERE key='scheduler.product_interval_minutes'"
            ).fetchone()
            try:
                interval = int(_loads(setting["value_json"], 60)) if setting else 60
            except (TypeError, ValueError):
                interval = 60
            conn.execute(
                """INSERT INTO schedules(
                       id,capability_instance_id,timezone,policy_type,policy_json,enabled,
                       next_run_at,misfire_policy,max_attempts,backoff_json,created_at,updated_at
                   ) VALUES(?,?,'Asia/Shanghai','interval',?,?,NULL,'recover',1,'{}',?,?)""",
                (
                    str(uuid4()), capability_id,
                    _dumps({"minutes": interval, "override": False}),
                    int(effective_enabled), now, now,
                ),
            )
        elif enabled is not None:
            conn.execute(
                "UPDATE schedules SET enabled=?,updated_at=? WHERE capability_instance_id=?",
                (int(bool(enabled)), now, capability_id),
            )
        current = conn.execute(
            "SELECT * FROM capability_instances WHERE id=?", (capability_id,),
        ).fetchone()
        assert current is not None
        return dict(current)

    async def get_product_settings(self, site_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._product_settings, _uuid(site_id), False)

    def _product_settings(self, site_id: str, create_default: bool = False) -> dict[str, Any] | None:
        site = self.repository.get_site(site_id)
        if not site:
            return None
        capability = self.repository.find_capability(site_id=site_id, code=self.PRODUCT_CODE)
        if not capability and not create_default:
            global_interval = int(self.repository.get_setting("scheduler.product_interval_minutes", "60"))
            return {
                "site_id": site_id, "enabled": False, "interval_override_minutes": None,
                "interval_minutes": global_interval, "effective_interval_minutes": global_interval,
                "scraper_type": "none", "configured": False, "configured_secret_fields": [],
                "config": {},
            }
        if create_default:
            capability = self._ensure_product_capability_sync(
                site_id, default_enabled=False,
            )
        assert capability is not None
        with self.database.connect() as conn:
            schedule = conn.execute(
                "SELECT * FROM schedules WHERE capability_instance_id=? ORDER BY id LIMIT 1", (capability["id"],)
            ).fetchone()
        config = _loads(capability.get("config_json"), {})
        secret_fields = list(config.pop("_secret_fields", []))
        policy = _loads(schedule["policy_json"], {}) if schedule else {}
        override = policy.get("minutes") if policy.get("override") else None
        global_interval = int(self.repository.get_setting("scheduler.product_interval_minutes", "60"))
        return {
            "site_id": site_id,
            "enabled": bool(capability["enabled"]) and bool(schedule and schedule["enabled"]),
            "interval_override_minutes": override,
            "interval_minutes": int(override or global_interval),
            "effective_interval_minutes": int(override or global_interval),
            "scraper_type": capability.get("adapter") or "none",
            "configured": bool(config or secret_fields), "configured_secret_fields": secret_fields,
            "config": config,
        }

    async def update_product_settings(self, site_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._call(self._update_product_settings_sync, _uuid(site_id), payload)

    async def get_product_runtime(self, site_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._product_runtime_sync, _uuid(site_id))

    def _product_runtime_sync(self, site_id: str) -> dict[str, Any] | None:
        settings = self._product_settings(site_id, False)
        if not settings:
            return None
        capability = self.repository.find_capability(site_id=site_id, code=self.PRODUCT_CODE)
        if not capability:
            return None
        config = _loads(capability.get("config_json"), {})
        names = list(config.pop("_secret_fields", []))
        settings["runtime_config"] = self._restore_secrets(capability["id"], config, names)
        return settings

    async def save_scrape_result(
        self, site_id: UUID, products: list[Any], *, job_id: UUID, lease_token: str,
    ) -> dict[str, int]:
        return await self._call(
            self._save_scrape_result_sync,
            _uuid(site_id), products, _uuid(job_id), lease_token,
        )

    def _save_scrape_result_sync(
        self, site_id: str, products: list[Any], job_id: str, lease_token: str,
    ) -> dict[str, int]:
        now = _iso(self._now()); created = updated = missing = 0
        obsolete_secret_ids: set[str] = set()
        with self.database.transaction(immediate=True) as conn:
            current_job = conn.execute(
                """SELECT j.kind,j.status,j.site_id,j.capability_instance_id,j.lease_token,
                          c.site_id capability_site_id,c.code capability_code
                   FROM jobs j
                   LEFT JOIN capability_instances c ON c.id=j.capability_instance_id
                   WHERE j.id=?""",
                (job_id,),
            ).fetchone()
            if (
                current_job is None
                or current_job["kind"] != "product_sync"
                or current_job["status"] != "running"
                or current_job["site_id"] != site_id
                or current_job["capability_site_id"] != site_id
                or current_job["capability_code"] != self.PRODUCT_CODE
                or current_job["lease_token"] != lease_token
            ):
                raise StaleJobWrite("product result rejected because its job lease is no longer current")
            run = conn.execute("SELECT id FROM runs WHERE job_id=? ORDER BY attempt_no DESC LIMIT 1", (job_id,)).fetchone()
            run_id = run["id"] if run else None
            policy = read_policy(conn, site_id)
            normalized_products = {}
            for raw in products:
                item = raw.as_dict() if hasattr(raw, "as_dict") else dict(raw)
                name = str(item.get("name") or "").strip()[:500]
                if not name:
                    continue
                price = item.get("price")
                if price is not None and (not isinstance(price, (float, int)) or not math.isfinite(price) or price < 0):
                    raise ValueError("Scraper returned an invalid product price")
                category = str(item.get("category_name") or "全部").strip()[:200] or "全部"
                key = str(item.get("external_key") or "").strip() or hashlib.sha256(f"{category}\0{name}".encode()).hexdigest()
                normalized_products[key] = item
            products = list(normalized_products.values())
            prior_rows = conn.execute(
                "SELECT external_key FROM products WHERE site_id=? AND availability_status!='missing'",
                (site_id,),
            ).fetchall()
            prior_count = len(prior_rows)
            retained = len({row["external_key"] for row in prior_rows}.intersection(normalized_products))
            suspicious = prior_count >= policy.catalog_minimum_size and (prior_count-retained)*100/prior_count >= policy.catalog_drop_threshold_percent
            if not products and prior_count < policy.catalog_minimum_size:
                return {"created": 0, "updated": 0, "missing": 0}
            if suspicious or len(products) > 10000:
                detail = {"previous_count": prior_count, "observed_count": len(products), "retained_count": retained}
                self._insert_event(conn, site_id, run_id, "catalog_anomaly", "warning",
                    "商品抓取数量异常，已保留原目录", detail, now,
                    detail="请检查抓取器和站点页面，确认目录完整后再同步。")
                return {"created": 0, "updated": 0, "missing": 0, "catalog_anomaly": True, **detail}
            alerts = []
            category_rows = list(conn.execute(
                "SELECT id,name,source_name FROM product_categories WHERE site_id=?",
                (site_id,),
            ))
            # Source labels remain stable when a user renames the visible
            # category. Case-folded lookup also prevents scraper-created
            # categories that differ only by case.
            category_ids = {
                str(row["source_name"] or row["name"]).casefold(): row["id"]
                for row in category_rows
            }
            display_category_ids = {
                str(row["name"]).casefold(): row["id"] for row in category_rows
            }
            category_source_names = {
                row["id"]: row["source_name"] for row in category_rows
            }
            seen: set[str] = set()
            for index, raw in enumerate(products[:10_000]):
                item = raw.as_dict() if hasattr(raw, "as_dict") else dict(raw)
                name = str(item.get("name") or "").strip()[:500]
                if not name: continue
                category_name = str(item.get("category_name") or "全部").strip()[:200] or "全部"
                category_key = category_name.casefold()
                category_id = category_ids.get(category_key)
                if not category_id:
                    category_id = display_category_ids.get(category_key)
                    if category_id and category_source_names.get(category_id) is None:
                        conn.execute(
                            "UPDATE product_categories SET source_name=?,updated_at=? WHERE id=?",
                            (category_name, now, category_id),
                        )
                    elif not category_id:
                        category_id = str(uuid4())
                        conn.execute(
                            """INSERT INTO product_categories(
                                   id,site_id,name,sort_order,created_at,updated_at,source_name
                               ) VALUES(?,?,?,?,?,?,?)""",
                            (category_id, site_id, category_name, len(category_ids), now, now, category_name),
                        )
                        display_category_ids[category_key] = category_id
                    category_ids[category_key] = category_id
                    category_source_names[category_id] = category_name
                external = str(item.get("external_key") or "").strip() or hashlib.sha256(
                    f"{category_name}\0{name}".encode("utf-8")
                ).hexdigest()
                seen.add(external)
                old = conn.execute("SELECT * FROM products WHERE site_id=? AND external_key=?", (site_id, external)).fetchone()
                buy_url = str(item.get("buy_url") or "").strip()
                url_base = suffix_id = None; query_keys: tuple[str, ...] = ()
                if buy_url:
                    parsed = urlsplit(buy_url)
                    if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None and parsed.password is None:
                        url_base, suffix_id, query_keys = self._protect_url(
                            buy_url, f"product:{site_id}:{external}:buy", conn=conn
                        )
                endpoint_id = old["buy_endpoint_id"] if old and old["buy_endpoint_id"] else None
                if url_base:
                    if endpoint_id:
                        old_endpoint = conn.execute(
                            "SELECT url_suffix_secret_id FROM site_endpoints WHERE id=?", (endpoint_id,)
                        ).fetchone()
                        conn.execute(
                            """UPDATE site_endpoints SET label=?,url_base=?,url_suffix_secret_id=?,query_keys_json=?,updated_at=?
                               WHERE id=?""", (name, url_base, suffix_id, _dumps(list(query_keys)), now, endpoint_id)
                        )
                        if old_endpoint and old_endpoint["url_suffix_secret_id"] and old_endpoint["url_suffix_secret_id"] != suffix_id:
                            obsolete_secret_ids.add(old_endpoint["url_suffix_secret_id"])
                    else:
                        endpoint_id = str(uuid4())
                        conn.execute(
                            """INSERT INTO site_endpoints(id,site_id,role,label,url_base,url_suffix_secret_id,query_keys_json,
                                      is_default,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                            (endpoint_id, site_id, "other", name, url_base, suffix_id, _dumps(list(query_keys)), 0, index, now, now),
                        )
                stock = int(item.get("stock", -1) if item.get("stock") is not None else -1)
                observed_stock = stock
                availability = "unknown" if stock < 0 else "sold_out" if stock == 0 else "available"
                product_id = old["id"] if old else str(uuid4())
                if old:
                    if stock < 0:
                        # An unreadable stock count is not evidence of a restock.
                        availability, stock = old["availability_status"], old["stock"]
                    confirmed = confirm_availability(conn, product_id, old["availability_status"], availability, job_id, policy.stock_confirmations)
                    if confirmed != availability:
                        stock = old["stock"]
                    availability = confirmed
                effective_category_id = (
                    old["category_id"]
                    if old and bool(old["category_overridden"])
                    else category_id
                )
                values = (
                    effective_category_id, category_id, name, item.get("price"), item.get("original_price"), stock,
                    item.get("stock_text"), int(item.get("sold") or 0), str(item.get("description") or "")[:4000] or None,
                    endpoint_id, url_base, suffix_id, _dumps(item.get("tags") or []), availability, now, now,
                )
                if old:
                    if old["buy_url_suffix_secret_id"] and old["buy_url_suffix_secret_id"] != suffix_id:
                        obsolete_secret_ids.add(old["buy_url_suffix_secret_id"])
                    conn.execute(
                        """UPDATE products SET category_id=?,scraped_category_id=?,name=?,price=?,original_price=?,stock=?,stock_text=?,sold=?,
                                  description=?,buy_endpoint_id=?,buy_url_base=?,buy_url_suffix_secret_id=?,raw_tags_json=?,availability_status=?,
                                  availability_reason=NULL,last_observed_at=?,updated_at=? WHERE id=?""",
                        (*values, product_id),
                    )
                    updated += 1
                    alerts.extend(qualifying_alerts(conn, product_id=product_id, old=dict(old),
                        price=item.get("price"), availability=availability, policy=policy, now=now))
                    if old["price"] is not None and item.get("price") is not None and item["price"] > old["price"]:
                        self._insert_event(conn, site_id, run_id, "price_changed", "info",
                            f"{name} price changed", {"product_id": product_id, "old_price": old["price"], "new_price": item["price"]}, now)
                else:
                    conn.execute(
                        """INSERT INTO products(id,site_id,category_id,scraped_category_id,external_key,name,price,original_price,stock,stock_text,
                                  sold,description,buy_endpoint_id,buy_url_base,buy_url_suffix_secret_id,raw_tags_json,availability_status,
                                  last_observed_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (product_id, site_id, values[0], values[1], external, *values[2:-1], now, values[-1]),
                    )
                    created += 1
                    conn.execute("INSERT OR IGNORE INTO product_monitor_state(product_id,reference_price) VALUES(?,?)", (product_id, item.get("price")))
                if observed_stock < 0:
                    conn.execute("UPDATE products SET availability_reason=? WHERE id=?",
                        ("最新库存未能读取，保留上次已确认状态" if old else "尚未读取到库存", product_id))
                conn.execute(
                    """INSERT OR IGNORE INTO price_observations(id,product_id,price,stock,availability_status,observed_at,source_run_id,dedupe_key)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (str(uuid4()), product_id, item.get("price"), stock, availability, now, run_id, f"{job_id}:{product_id}"),
                )
            existing = list(conn.execute("SELECT id,external_key,name,availability_status FROM products WHERE site_id=? AND availability_status!='missing'", (site_id,)))
            for row in existing:
                if row["external_key"] not in seen:
                    confirmed = confirm_availability(conn, row["id"], row["availability_status"], "missing", job_id, policy.stock_confirmations)
                    conn.execute(
                        """INSERT OR IGNORE INTO price_observations(id,product_id,price,stock,availability_status,observed_at,source_run_id,dedupe_key)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (str(uuid4()), row["id"], None, None, "missing", now, run_id, f"{job_id}:{row['id']}"),
                    )
                    if confirmed != "missing":
                        continue
                    conn.execute(
                        "UPDATE products SET availability_status='missing',availability_reason='not observed in latest sync',updated_at=? WHERE id=?",
                        (now, row["id"]),
                    ); missing += 1
            for event_type, severity, notify in (
                ("price_drop", "warning", True), ("back_in_stock", "success", True),
                ("price_drop", "info", False), ("back_in_stock", "info", False),
            ):
                group = [{key: value for key, value in item.items() if key != "notify"}
                         for item in alerts if item["type"] == event_type and item["notify"] == notify]
                if not group:
                    continue
                if len(group) == 1:
                    alert = group[0]
                    payload = {key: value for key, value in alert.items() if key not in {"type", "name"}}
                    title = f"{alert['name']} " + ("price dropped" if event_type == "price_drop" else "is back in stock")
                else:
                    payload = {"items": group, "count": len(group), "job_id": job_id}
                    title = f"{len(group)} 件商品" + ("降价" if event_type == "price_drop" else "恢复库存")
                payload["notification_eligible"] = notify
                self._insert_event(conn, site_id, run_id, event_type, severity, title, payload, now, notify=notify)
            self._insert_event(conn, site_id, run_id, "product_sync", "success", "Product sync completed",
                               {"created": created, "updated": updated, "missing": missing}, now)
        if self.secret_service:
            for secret_id in obsolete_secret_ids: self.secret_service.delete_if_unreferenced(secret_id)
        return {"created": created, "updated": updated, "missing": missing}

    def _update_product_settings_sync(self, site_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = self._product_settings(site_id, True)
        if current is None:
            raise LookupError("site not found")
        capability = self.repository.find_capability(site_id=site_id, code=self.PRODUCT_CODE)
        assert capability
        cap_changes: dict[str, Any] = {}
        if "enabled" in payload:
            cap_changes["enabled"] = payload["enabled"]
        if payload.get("scraper_type") is not None:
            cap_changes["adapter"] = payload["scraper_type"]
        global_interval = int(self.repository.get_setting("scheduler.product_interval_minutes", "60"))
        override = None if payload.get("clear_interval_override") else payload.get("interval_override_minutes", current["interval_override_minutes"])
        policy = {"minutes": int(override or global_interval), "override": override is not None}
        with self.database.transaction(immediate=True) as conn:
            self._update_capability_atomic(
                capability["id"], owner="capability", account_id=None,
                changes=cap_changes, config=payload.get("config"),
                config_supplied=payload.get("config") is not None,
                clear_secrets=set(payload.get("clear_secrets") or []), conn=conn,
            )
            schedule = conn.execute(
                "SELECT * FROM schedules WHERE capability_instance_id=? ORDER BY id LIMIT 1", (capability["id"],)
            ).fetchone()
            if schedule:
                fields = ["policy_json=?"]
                values: list[Any] = [_dumps(policy)]
                if "enabled" in payload:
                    fields.append("enabled=?")
                    values.append(int(bool(payload["enabled"])))
                values.extend([_iso(self._now()), schedule["id"]])
                conn.execute(
                    f"UPDATE schedules SET {','.join(fields)},updated_at=? WHERE id=?", values
                )
        return self._product_settings(site_id, True)  # type: ignore[return-value]

    async def list_due_product_sites(self, now: datetime) -> list[dict[str, Any]]:
        return await self._call(self._due_product_sites, _iso(now))

    def _due_product_sites(self, now: str | None) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """SELECT c.site_id,s.policy_json FROM schedules s
                   JOIN capability_instances c ON c.id=s.capability_instance_id
                   WHERE c.code='product_monitor' AND c.enabled=1 AND s.enabled=1
                     AND lower(trim(coalesce(c.adapter,''))) NOT IN ('','none','manual')
                      AND (s.next_run_at IS NULL OR s.next_run_at<=?) ORDER BY c.site_id""",
                (now,),
            ).fetchall()
            result = []
            for row in rows:
                policy = _loads(row["policy_json"], {})
                result.append({"site_id": row["site_id"], "interval_override_minutes": policy.get("minutes") if policy.get("override") else None})
            return result

    async def mark_product_enqueued(self, site_id: UUID, next_run_at: datetime) -> None:
        await self._call(self._mark_product_enqueued, _uuid(site_id), _iso(next_run_at))

    def _mark_product_enqueued(self, site_id: str, next_run_at: str | None) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                """UPDATE schedules SET next_run_at=?,updated_at=? WHERE capability_instance_id IN
                   (SELECT id FROM capability_instances WHERE site_id=? AND code='product_monitor' AND account_id IS NULL)""",
                (next_run_at, _iso(self._now()), site_id),
            )

    async def list_products(self, *, page: int, page_size: int, site_id: UUID | None = None,
                            q: str | None = None, availability_status: str | None = None,
                            category_id: UUID | None = None, watched: bool | None = None):
        return await self._call(
            self._list_products_sync, page, page_size, _uuid(site_id) if site_id else None,
            q, availability_status, _uuid(category_id) if category_id else None, watched,
        )

    def _list_products_sync(self, page: int, page_size: int, site_id: str | None,
                            q: str | None, availability: str | None,
                            category_id: str | None, watched: bool | None = None):
        clauses, params = [], []
        if site_id:
            clauses.append("p.site_id=?"); params.append(site_id)
        if q:
            clauses.append("(p.name LIKE ? OR COALESCE(p.description,'') LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        if availability:
            clauses.append("p.availability_status=?"); params.append(availability)
        if category_id:
            clauses.append("p.category_id=?"); params.append(category_id)
        if watched is not None:
            clauses.append("COALESCE((SELECT w.watched FROM product_watch_rules w WHERE w.product_id=p.id),0)=?")
            params.append(int(watched))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM products p" + where, params).fetchone()[0])
            rows = conn.execute(
                """SELECT p.*,(SELECT w.watched FROM product_watch_rules w WHERE w.product_id=p.id) AS watched,s.name site_name,pc.name category_name,
                           scraped_pc.name scraped_category_name,
                           COALESCE(e.url_base,p.buy_url_base) AS url
                    FROM products p JOIN sites s ON s.id=p.site_id
                    LEFT JOIN product_categories pc ON pc.id=p.category_id
                    LEFT JOIN product_categories scraped_pc ON scraped_pc.id=p.scraped_category_id
                    LEFT JOIN site_endpoints e ON e.id=p.buy_endpoint_id""" + where +
                " ORDER BY p.updated_at DESC,p.id LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            return [self._normalize_product(dict(row)) for row in rows], total

    async def get_product(self, product_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._get_product_sync, _uuid(product_id))

    def _get_product_sync(self, product_id: str) -> dict[str, Any] | None:
        with self.database.connect() as conn:
            row = conn.execute(
                """SELECT p.*,(SELECT w.watched FROM product_watch_rules w WHERE w.product_id=p.id) AS watched,s.name site_name,pc.name category_name,
                          scraped_pc.name scraped_category_name,
                          COALESCE(e.url_base,p.buy_url_base) AS url
                   FROM products p JOIN sites s ON s.id=p.site_id
                   LEFT JOIN product_categories pc ON pc.id=p.category_id
                   LEFT JOIN product_categories scraped_pc ON scraped_pc.id=p.scraped_category_id
                   LEFT JOIN site_endpoints e ON e.id=p.buy_endpoint_id
                   WHERE p.id=?""",
                (product_id,),
            ).fetchone()
            if row is None:
                return None
            result = self._normalize_product(dict(row))
            result["tags"] = [dict(item) for item in conn.execute(
                """SELECT t.id,t.name,t.color,t.icon FROM tags t
                   JOIN product_tags pt ON pt.tag_id=t.id
                   WHERE pt.product_id=? ORDER BY t.sort_order,t.name,t.id""",
                (product_id,),
            )]
            stats = conn.execute(
                """SELECT COUNT(*) observation_count,MIN(price) min_price,MAX(price) max_price,
                          MIN(observed_at) first_observed_at,MAX(observed_at) last_observed_at
                   FROM price_observations WHERE product_id=?""",
                (product_id,),
            ).fetchone()
            result["price_history_summary"] = dict(stats) if stats else {
                "observation_count": 0, "min_price": None, "max_price": None,
                "first_observed_at": None, "last_observed_at": None,
            }
            return result

    async def list_product_price_history(
        self, product_id: UUID, *, page: int, page_size: int,
        date_from: datetime | None = None, date_to: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int] | None:
        return await self._call(
            self._list_product_price_history_sync, _uuid(product_id), page, page_size,
            _iso(date_from), _iso(date_to),
        )

    def _list_product_price_history_sync(
        self, product_id: str, page: int, page_size: int,
        date_from: str | None, date_to: str | None,
    ) -> tuple[list[dict[str, Any]], int] | None:
        clauses = ["product_id=?"]
        params: list[Any] = [product_id]
        if date_from:
            clauses.append("observed_at>=?"); params.append(date_from)
        if date_to:
            clauses.append("observed_at<=?"); params.append(date_to)
        where = " WHERE " + " AND ".join(clauses)
        with self.database.connect() as conn:
            if conn.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone() is None:
                return None
            total = int(conn.execute(
                "SELECT COUNT(*) FROM price_observations" + where, params,
            ).fetchone()[0])
            rows = conn.execute(
                """SELECT id,product_id,price,stock,availability_status,observed_at,source_run_id
                   FROM price_observations""" + where +
                " ORDER BY observed_at DESC,id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            return [dict(row) for row in rows], total

    async def list_product_categories(
        self, *, page: int, page_size: int, site_id: UUID | None = None,
        q: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        return await self._call(
            self._list_product_categories_sync, page, page_size,
            _uuid(site_id) if site_id else None, q,
        )

    def _list_product_categories_sync(
        self, page: int, page_size: int, site_id: str | None, q: str | None,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if site_id:
            clauses.append("pc.site_id=?"); params.append(site_id)
        if q:
            clauses.append("(pc.name LIKE ? OR s.name LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.connect() as conn:
            total = int(conn.execute(
                """SELECT COUNT(*) FROM product_categories pc
                   JOIN sites s ON s.id=pc.site_id""" + where,
                params,
            ).fetchone()[0])
            rows = conn.execute(
                """SELECT pc.*,s.name site_name,COUNT(p.id) product_count
                   FROM product_categories pc JOIN sites s ON s.id=pc.site_id
                   LEFT JOIN products p ON p.category_id=pc.id""" + where +
                " GROUP BY pc.id ORDER BY s.name COLLATE NOCASE,pc.sort_order,pc.name COLLATE NOCASE,pc.id LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            return [dict(row) for row in rows], total

    async def create_product_category(
        self, site_id: UUID, payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self._call(
            self._create_product_category_sync, _uuid(site_id), payload,
        )

    def _create_product_category_sync(
        self, site_id: str, payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        name = str(payload["name"]).strip()
        now = _iso(self._now()); category_id = str(uuid4())
        with self.database.transaction(immediate=True) as conn:
            site = conn.execute(
                "SELECT name FROM sites WHERE id=? AND status!='archived'", (site_id,),
            ).fetchone()
            if site is None:
                return None
            duplicate = conn.execute(
                "SELECT 1 FROM product_categories WHERE site_id=? AND name=? COLLATE NOCASE",
                (site_id, name),
            ).fetchone()
            if duplicate:
                raise ValueError("A category with this name already exists for the site")
            conn.execute(
                """INSERT INTO product_categories(id,site_id,name,sort_order,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (category_id, site_id, name, int(payload.get("sort_order", 0)), now, now),
            )
            return {
                "id": category_id, "site_id": site_id, "site_name": site["name"],
                "name": name, "sort_order": int(payload.get("sort_order", 0)),
                "product_count": 0, "created_at": now, "updated_at": now,
            }

    async def update_product_category(
        self, category_id: UUID, payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self._call(
            self._update_product_category_sync, _uuid(category_id), payload,
        )

    def _update_product_category_sync(
        self, category_id: str, payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        with self.database.transaction(immediate=True) as conn:
            current = conn.execute(
                "SELECT * FROM product_categories WHERE id=?", (category_id,),
            ).fetchone()
            if current is None:
                return None
            name = str(payload.get("name", current["name"])).strip()
            if conn.execute(
                """SELECT 1 FROM product_categories
                   WHERE site_id=? AND id!=? AND name=? COLLATE NOCASE""",
                (current["site_id"], category_id, name),
            ).fetchone():
                raise ValueError("A category with this name already exists for the site")
            now = _iso(self._now())
            conn.execute(
                "UPDATE product_categories SET name=?,sort_order=?,updated_at=? WHERE id=?",
                (name, int(payload.get("sort_order", current["sort_order"])), now, category_id),
            )
            row = conn.execute(
                """SELECT pc.*,s.name site_name,
                          (SELECT COUNT(*) FROM products p WHERE p.category_id=pc.id) product_count
                   FROM product_categories pc JOIN sites s ON s.id=pc.site_id WHERE pc.id=?""",
                (category_id,),
            ).fetchone()
            return dict(row) if row else None

    async def delete_product_category(self, category_id: UUID) -> bool:
        return await self._call(self._delete_product_category_sync, _uuid(category_id))

    def _delete_product_category_sync(self, category_id: str) -> bool:
        with self.database.transaction(immediate=True) as conn:
            return conn.execute(
                "DELETE FROM product_categories WHERE id=?", (category_id,),
            ).rowcount == 1

    async def assign_product_category(
        self, product_id: UUID, category_id: UUID | None, *, automatic: bool = False,
    ) -> dict[str, Any] | None:
        return await self._call(
            self._assign_product_category_sync, _uuid(product_id),
            _uuid(category_id) if category_id else None, automatic,
        )

    def _assign_product_category_sync(
        self, product_id: str, category_id: str | None, automatic: bool,
    ) -> dict[str, Any] | None:
        with self.database.transaction(immediate=True) as conn:
            product = conn.execute(
                "SELECT site_id,scraped_category_id FROM products WHERE id=?", (product_id,),
            ).fetchone()
            if product is None:
                return None
            if category_id and not automatic:
                category = conn.execute(
                    "SELECT site_id FROM product_categories WHERE id=?", (category_id,),
                ).fetchone()
                if category is None:
                    raise LookupError("Product category not found")
                if category["site_id"] != product["site_id"]:
                    raise ValueError("A product can only use a category from the same site")
            effective_category_id = product["scraped_category_id"] if automatic else category_id
            conn.execute(
                """UPDATE products
                   SET category_id=?,category_overridden=?,updated_at=? WHERE id=?""",
                (
                    effective_category_id,
                    0 if automatic else 1,
                    _iso(self._now()),
                    product_id,
                ),
            )
        return self._get_product_sync(product_id)

    @staticmethod
    def _normalize_product(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"], "site_id": row["site_id"], "site_name": row.get("site_name"),
            "category_id": row.get("category_id"), "category_name": row.get("category_name"),
            "scraped_category_id": row.get("scraped_category_id"),
            "scraped_category_name": row.get("scraped_category_name"),
            "category_overridden": bool(row.get("category_overridden", 0)),
            "external_key": row.get("external_key"), "name": row["name"], "url": row.get("url"),
            "watched": bool(row.get("watched")),
            "price": row.get("price"), "original_price": row.get("original_price"),
            "stock": row.get("stock"), "availability_status": row.get("availability_status"),
            "buy_endpoint_id": row.get("buy_endpoint_id"),
            "availability_reason": row.get("availability_reason"), "description": row.get("description"),
            "raw_tags": _loads(row.get("raw_tags_json"), []),
            "updated_at": row.get("updated_at"), "last_observed_at": row.get("last_observed_at"),
        }


    async def get_monitor_policy(self, site_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._monitor_policy_sync, _uuid(site_id), None)

    async def update_monitor_policy(self, site_id: UUID, payload: dict[str, Any]) -> dict[str, Any] | None:
        return await self._call(self._monitor_policy_sync, _uuid(site_id), payload)

    def _monitor_policy_sync(self, site_id: str, payload: dict[str, Any] | None):
        from site_hub.monitoring_models import MonitorPolicy
        with self.database.transaction(immediate=payload is not None) as conn:
            if not conn.execute("SELECT 1 FROM sites WHERE id=? AND status!='archived'", (site_id,)).fetchone():
                return None
            if payload is not None:
                policy = MonitorPolicy.model_validate(payload)
                conn.execute("""INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?)
                    ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                    (f"product.monitor.{site_id}", _dumps(policy.model_dump()), _iso(self._now())))
            return read_policy(conn, site_id).model_dump()

    async def get_product_watch_rule(self, product_id: UUID) -> dict[str, Any] | None:
        return await self._call(self._watch_rule_sync, _uuid(product_id), None)

    async def update_product_watch_rule(self, product_id: UUID, payload: dict[str, Any]) -> dict[str, Any] | None:
        return await self._call(self._watch_rule_sync, _uuid(product_id), payload)

    def _watch_rule_sync(self, product_id: str, payload: dict[str, Any] | None):
        from site_hub.monitoring_models import ProductWatchRule
        with self.database.transaction(immediate=payload is not None) as conn:
            if not conn.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
                return None
            if payload is not None:
                rule = ProductWatchRule.model_validate(payload)
                conn.execute("""INSERT INTO product_watch_rules(product_id,watched,price_drop_amount,price_drop_percent)
                    VALUES(?,?,?,?) ON CONFLICT(product_id) DO UPDATE SET watched=excluded.watched,
                    price_drop_amount=excluded.price_drop_amount,price_drop_percent=excluded.price_drop_percent""",
                    (product_id, int(rule.watched), rule.price_drop_amount, rule.price_drop_percent))
                conn.execute("UPDATE product_monitor_state SET reference_price=(SELECT price FROM products WHERE id=?),last_price_alert_at=NULL WHERE product_id=?", (product_id, product_id))
            return read_rule(conn, product_id).model_dump()
