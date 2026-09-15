"""Product monitoring rules evaluated inside the catalog write transaction."""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from site_hub.monitoring_models import MonitorPolicy, ProductWatchRule
from site_hub.services.store_support import _moment


def read_policy(conn, site_id: str) -> MonitorPolicy:
    row = conn.execute("SELECT value_json FROM settings WHERE key=?", (f"product.monitor.{site_id}",)).fetchone()
    return MonitorPolicy.model_validate(json.loads(row[0])) if row else MonitorPolicy()


def read_rule(conn, product_id: str) -> ProductWatchRule:
    row = conn.execute("SELECT watched,price_drop_amount,price_drop_percent FROM product_watch_rules WHERE product_id=?", (product_id,)).fetchone()
    return ProductWatchRule.model_validate(dict(row)) if row else ProductWatchRule()


def confirm_availability(conn, product_id: str, old_status: str, new_status: str,
                         job_id: str, confirmations: int) -> str:
    conn.execute("INSERT OR IGNORE INTO product_monitor_state(product_id) VALUES(?)", (product_id,))
    state = conn.execute("SELECT * FROM product_monitor_state WHERE product_id=?", (product_id,)).fetchone()
    count = 0
    pending = None
    if new_status != old_status:
        pending = new_status
        count = state["pending_count"] if state["pending_availability"] == new_status else 0
        already_observed = conn.execute(
            "SELECT 1 FROM price_observations WHERE dedupe_key=?", (f"{job_id}:{product_id}",)
        ).fetchone() is not None
        if confirmations == 1 or (state["last_job_id"] != job_id and not already_observed):
            count += 1
        if count >= confirmations:
            pending, count = None, 0
        else:
            new_status = old_status
    conn.execute("UPDATE product_monitor_state SET pending_availability=?,pending_count=?,last_job_id=? WHERE product_id=?",
                 (pending, count, job_id, product_id))
    return new_status


def qualifying_alerts(conn, *, product_id: str, old: dict[str, Any], price: float | None,
                      availability: str, policy: MonitorPolicy, now: str) -> list[dict[str, Any]]:
    rule = read_rule(conn, product_id)
    conn.execute("INSERT OR IGNORE INTO product_monitor_state(product_id,reference_price) VALUES(?,?)",
                 (product_id, old["price"]))
    state = dict(conn.execute("SELECT * FROM product_monitor_state WHERE product_id=?", (product_id,)).fetchone())
    reference = state["reference_price"] if state["reference_price"] is not None else old["price"]
    alerts = []
    selected = not policy.watched_only or rule.watched

    def due(column):
        value = state[column]
        return not value or _moment(now) >= _moment(value) + timedelta(minutes=policy.notification_cooldown_minutes)

    if price is not None:
        if reference is None or price > reference:
            reference = price
        amount = reference - price
        percent = amount / reference * 100 if reference and reference > 0 else 0
        min_amount = rule.price_drop_amount if rule.price_drop_amount is not None else policy.price_drop_amount
        min_percent = rule.price_drop_percent if rule.price_drop_percent is not None else policy.price_drop_percent
        notify = selected and amount > 0 and amount + 1e-9 >= min_amount and percent + 1e-9 >= min_percent and due("last_price_alert_at")
        if notify or (old["price"] is not None and price < old["price"]):
            alerts.append({"type": "price_drop", "product_id": product_id, "name": old["name"],
                           "old_price": reference if notify else old["price"], "new_price": price,
                           "notify": bool(notify)})
        if notify:
            conn.execute("UPDATE product_monitor_state SET last_price_alert_at=? WHERE product_id=?", (now, product_id))
            reference = price
        conn.execute("UPDATE product_monitor_state SET reference_price=? WHERE product_id=?", (reference, product_id))
    if old["availability_status"] in {"sold_out", "missing"} and availability == "available":
        notify = selected and due("last_stock_alert_at")
        alerts.append({"type": "back_in_stock", "product_id": product_id, "name": old["name"], "notify": bool(notify)})
        if notify:
            conn.execute("UPDATE product_monitor_state SET last_stock_alert_at=? WHERE product_id=?", (now, product_id))
    return alerts
