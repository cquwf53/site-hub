from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from site_hub.config import Settings
from site_hub.main import create_app
from site_hub.services.data_store import DataLayerStore
from site_hub.services.memory_store import MemoryStore


HOST = "127.0.0.1:8686"
ORIGIN = f"http://{HOST}"


def _settings(tmp_path) -> Settings:
    return Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        frontend_dir=tmp_path / "frontend" / "dist",
    )


def _insert_jobs(store: DataLayerStore) -> None:
    rows: list[tuple[str, str, str, str, str, str, str]] = []
    for status, count, created_at in (
        ("succeeded", 225, "2026-08-08T00:00:00.000Z"),
        ("queued", 35, "2026-08-07T00:00:00.000Z"),
        ("running", 20, "2026-08-07T00:00:00.000Z"),
    ):
        for index in range(count):
            identifier = str(uuid4())
            rows.append((
                identifier,
                "adapter_test",
                created_at,
                status,
                f"seed:{status}:{index}",
                created_at,
                created_at,
            ))
    with store.database.transaction(immediate=True) as conn:
        conn.executemany(
            """INSERT INTO jobs(
                   id,kind,available_at,status,idempotency_key,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?)""",
            rows,
        )


def test_jobs_multi_status_filter_counts_and_pages_in_sqlite(tmp_path):
    store = DataLayerStore(tmp_path / "data" / "site-hub.db")
    asyncio.run(store.initialize())
    _insert_jobs(store)

    # An empty collection preserves the historical no-filter behavior.
    _, unfiltered_total = asyncio.run(store.list_jobs(
        page=1, page_size=10, status=[],
    ))
    assert unfiltered_total == 280

    app = create_app(
        settings=_settings(tmp_path), store=store, start_background=False,
    )
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        pages = []
        for page_number, expected_size in ((1, 20), (2, 20), (3, 15)):
            response = client.get(
                "/api/v1/jobs",
                params=[
                    ("status", "queued"),
                    ("status", "running"),
                    ("page", str(page_number)),
                    ("page_size", "20"),
                ],
            )
            assert response.status_code == 200
            body = response.json()
            assert body["total"] == 55
            assert body["page"] == page_number
            assert len(body["items"]) == expected_size
            assert {item["status"] for item in body["items"]} <= {"queued", "running"}
            pages.extend(body["items"])

    assert len({item["id"] for item in pages}) == 55


@pytest.mark.asyncio
async def test_memory_store_matches_single_many_and_empty_job_status_semantics():
    store = MemoryStore()
    now = datetime.now(UTC)
    for index, status in enumerate(("queued", "running", "succeeded")):
        row, _ = await store.enqueue_job({
            "id": uuid4(),
            "kind": "adapter_test",
            "site_id": None,
            "account_id": None,
            "scheduled_for": now,
            "idempotency_key": f"memory:{index}",
            "payload": {},
        })
        store.jobs[UUID(str(row["id"]))]["status"] = status

    single, single_total = await store.list_jobs(
        page=1, page_size=10, status="queued",
    )
    many, many_total = await store.list_jobs(
        page=1, page_size=10, status=["queued", "running"],
    )
    unfiltered, unfiltered_total = await store.list_jobs(
        page=1, page_size=10, status=[],
    )

    assert single_total == 1
    assert [row["status"] for row in single] == ["queued"]
    assert many_total == 2
    assert {row["status"] for row in many} == {"queued", "running"}
    assert unfiltered_total == 3
    assert len(unfiltered) == 3
