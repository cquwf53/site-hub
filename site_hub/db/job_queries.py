from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from site_hub.db.connection import Database


JOB_SELECT = """SELECT j.*,c.code capability_code
                FROM jobs j LEFT JOIN capability_instances c ON c.id=j.capability_instance_id"""


def normalize_job_statuses(status: str | Iterable[str] | None) -> tuple[str, ...]:
    """Normalize the public single-or-many status filter without changing its order."""
    if status is None:
        return ()
    values = (status,) if isinstance(status, str) else tuple(status)
    return tuple(dict.fromkeys(str(value) for value in values))


def list_job_rows(
    database: Database,
    *,
    page: int,
    page_size: int,
    kind: str | None = None,
    status: str | Iterable[str] | None = None,
    site_id: str | None = None,
    account_id: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Filter, count, and page jobs entirely inside SQLite."""
    clauses: list[str] = []
    params: list[Any] = []
    if kind:
        clauses.append("j.kind=?")
        params.append(kind)
    statuses = normalize_job_statuses(status)
    if statuses:
        clauses.append("j.status IN (" + ",".join("?" for _ in statuses) + ")")
        params.extend(statuses)
    if site_id:
        clauses.append("j.site_id=?")
        params.append(site_id)
    if account_id:
        clauses.append("j.account_id=?")
        params.append(account_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""

    with database.connect() as conn:
        total = int(conn.execute(
            "SELECT COUNT(*) FROM jobs j" + where,
            params,
        ).fetchone()[0])
        rows = conn.execute(
            JOB_SELECT + where
            + " ORDER BY j.created_at DESC,j.id DESC LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    return [dict(row) for row in rows], total


__all__ = ["JOB_SELECT", "list_job_rows", "normalize_job_statuses"]
