from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from site_hub.config import Settings
from site_hub.db import Database, MIGRATIONS, apply_migrations
from site_hub.main import create_app
from site_hub.services.data_store import DataLayerStore
from site_hub.services.memory_store import MemoryStore
from site_hub.services.secrets import EphemeralSecretProtector, SecretService


HOST = "127.0.0.1:8686"
ORIGIN = f"http://{HOST}"


def _settings(tmp_path) -> Settings:
    frontend = tmp_path / "frontend" / "dist"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text(
        "<!doctype html><title>SiteHub</title>", encoding="utf-8",
    )
    return Settings(
        project_root=tmp_path, data_dir=tmp_path / "data", frontend_dir=frontend,
    )


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    return {"X-CSRF-Token": token, "Origin": ORIGIN}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def test_v3_upgrade_adds_hidden_preference_without_changing_existing_rows(tmp_path):
    database = Database(tmp_path / "v3-upgrade.db")
    apply_migrations(database, MIGRATIONS[:3])
    now = "2026-08-06T00:00:00.000Z"
    with database.transaction() as conn:
        conn.execute(
            """INSERT INTO folders(id,parent_id,name,sort_order,created_at,updated_at)
               VALUES('folder',NULL,'Imported',0,?,?)""",
            (now, now),
        )
        type_id = conn.execute(
            "SELECT id FROM site_types WHERE slug='other'"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO sites(
                   id,folder_id,name,origin,canonical_host,kind,site_type_id,status,
                   description,note,icon,pinned,sort_order,created_at,updated_at
               ) VALUES('site','folder','Existing','https://example.test','example.test',
                        'other',?,'active','','','',0,0,?,?)""",
            (type_id, now, now),
        )

    apply_migrations(database)

    with database.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        row = conn.execute(
            "SELECT overview_hidden FROM sites WHERE id='site'"
        ).fetchone()
        assert row is not None and row["overview_hidden"] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.asyncio
async def test_overview_ranking_filters_and_usage_reset(tmp_path):
    store = DataLayerStore(tmp_path / "overview.db")
    await store.initialize()
    try:
        sites = {}
        for name, extra in (
            ("Pinned", {"pinned": True}),
            ("Fresh frequent", {}),
            ("Recent low", {}),
            ("Old frequent", {}),
            ("Starter", {}),
            ("Hidden", {}),
            ("Disabled", {"status": "disabled"}),
        ):
            sites[name] = await store.create_site({
                "name": name, "url": f"https://{name.lower().replace(' ', '-')}.test",
                **extra,
            })

        now = datetime.now(UTC)
        values = {
            "Fresh frequent": (4, now - timedelta(days=1)),
            "Recent low": (1, now - timedelta(minutes=1)),
            "Old frequent": (100, now - timedelta(days=120)),
            "Hidden": (1000, now),
            "Disabled": (1000, now),
        }
        with store.database.transaction() as conn:
            for name, (count, visited) in values.items():
                conn.execute(
                    "UPDATE sites SET visit_count=?,last_visited_at=? WHERE id=?",
                    (count, _iso(visited), str(sites[name]["id"])),
                )
            conn.execute(
                "UPDATE sites SET overview_hidden=1 WHERE id=?",
                (str(sites["Hidden"]["id"]),),
            )

        overview = await store.list_overview_sites(limit=8)
        assert overview["has_usage"] is True
        frequent_names = [row["name"] for row in overview["frequent"]]
        assert frequent_names[:4] == [
            "Pinned", "Fresh frequent", "Recent low", "Old frequent",
        ]
        assert frequent_names[-1] == "Starter"
        assert [row["recommendation_reason"] for row in overview["frequent"]] == [
            "pinned", "frequent", "frequent", "frequent", "starter",
        ]
        assert [row["name"] for row in overview["recent"]] == [
            "Recent low", "Fresh frequent", "Old frequent",
        ]
        assert {row["recommendation_reason"] for row in overview["recent"]} == {"recent"}
        assert all(row["name"] not in {"Hidden", "Disabled"}
                   for group in (overview["frequent"], overview["recent"])
                   for row in group)

        with pytest.raises(ValueError, match="visit_count"):
            await store.update_site(UUID(str(sites["Starter"]["id"])), {"visit_count": 99})
        hidden = await store.update_site(
            UUID(str(sites["Starter"]["id"])), {"overview_hidden": True},
        )
        assert hidden and hidden["overview_hidden"] is True

        # Each call uses an immediate transaction, so concurrent opens cannot
        # lose increments through a read/modify/write race.
        assert all(await asyncio.gather(*(
            store.record_site_visit(UUID(str(sites["Pinned"]["id"])))
            for _ in range(20)
        )))
        pinned = await store.get_site(UUID(str(sites["Pinned"]["id"])))
        assert pinned and pinned["visit_count"] == 20

        changed = await store.reset_site_usage()
        assert changed == 6
        reset = await store.list_overview_sites(limit=8)
        assert reset["has_usage"] is False
        assert reset["recent"] == []

        # Hidden usage still keeps the global reset control available even
        # though hidden sites never appear in either overview list.
        with store.database.transaction() as conn:
            conn.execute(
                "UPDATE sites SET visit_count=1,last_visited_at=? WHERE id=?",
                (_iso(now), str(sites["Hidden"]["id"])),
            )
        hidden_usage = await store.list_overview_sites(limit=8)
        assert hidden_usage["has_usage"] is True
        assert all(row["name"] != "Hidden" for row in hidden_usage["recent"])
        assert await store.reset_site_usage() == 1
    finally:
        await store.close()


def test_site_open_counts_only_success_and_never_exposes_sensitive_url(tmp_path, monkeypatch):
    store = DataLayerStore(tmp_path / "site-open.db")
    app = create_app(settings=_settings(tmp_path), store=store, start_background=False)
    router_module = importlib.import_module("site_hub.api.router")
    secret = "query-secret-must-never-leak"
    opened: list[str] = []

    with TestClient(
        app, base_url=ORIGIN, headers={"Host": HOST}, raise_server_exceptions=False,
    ) as client:
        store.secret_service = SecretService(
            store.database,
            EphemeralSecretProtector(b"overview-site-open-test-key-32-bytes"),
        )
        headers = _csrf(client)
        created = client.post(
            "/api/v1/sites",
            json={"name": "Private", "url": f"https://private.test/home?token={secret}"},
            headers=headers,
        )
        assert created.status_code == 201
        site = created.json()
        assert secret not in created.text

        monkeypatch.setattr(router_module, "_launch_url", opened.append)
        response = client.post(f"/api/v1/sites/{site['id']}/open", headers=headers)
        assert response.status_code == 204
        assert opened == [f"https://private.test/home?token={secret}"]
        current = client.get(f"/api/v1/sites/{site['id']}").json()
        assert current["visit_count"] == 1
        assert current["last_visited_at"] is not None
        assert secret not in str(current)

        generic = client.post(
            f"/api/v1/endpoints/{site['default_endpoint_id']}/open", headers=headers,
        )
        assert generic.status_code == 204
        assert client.get(f"/api/v1/sites/{site['id']}").json()["visit_count"] == 1

        def fail_with_sensitive_url(url: str) -> None:
            raise RuntimeError(f"browser rejected {url}")

        monkeypatch.setattr(router_module, "_launch_url", fail_with_sensitive_url)
        failed = client.post(f"/api/v1/sites/{site['id']}/open", headers=headers)
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == "BROWSER_OPEN_FAILED"
        assert secret not in failed.text
        assert client.get(f"/api/v1/sites/{site['id']}").json()["visit_count"] == 1

        monkeypatch.setattr(router_module, "_launch_url", opened.append)

        async def fail_to_record(_site_id: UUID) -> bool:
            raise RuntimeError("injected usage write failure")

        monkeypatch.setattr(store, "record_site_visit", fail_to_record)
        best_effort = client.post(f"/api/v1/sites/{site['id']}/open", headers=headers)
        assert best_effort.status_code == 204

        reset = client.post("/api/v1/overview/sites/reset-usage", headers=headers)
        assert reset.status_code == 200 and reset.json() == {"reset": 1}
        overview = client.get("/api/v1/overview/sites").json()
        assert overview["has_usage"] is False and overview["recent"] == []


@pytest.mark.asyncio
async def test_memory_store_matches_overview_usage_contract():
    store = MemoryStore()
    site = await store.create_site({"name": "Memory", "url": "https://memory.test"})
    overview = await store.list_overview_sites(limit=8)
    assert overview["has_usage"] is False
    assert overview["frequent"][0]["recommendation_reason"] == "starter"
    assert overview["recent"] == []

    assert await store.record_site_visit(site["id"])
    visited = await store.list_overview_sites(limit=8)
    assert visited["has_usage"] is True
    assert visited["frequent"][0]["recommendation_reason"] == "frequent"
    assert visited["recent"][0]["recommendation_reason"] == "recent"
    with pytest.raises(ValueError, match="last_visited_at"):
        await store.update_site(site["id"], {"last_visited_at": _now_for_test()})

    assert await store.reset_site_usage() == 1
    assert (await store.get_site(site["id"]))["visit_count"] == 0

    await store.update_site(site["id"], {"overview_hidden": True})
    assert await store.record_site_visit(site["id"])
    hidden_usage = await store.list_overview_sites(limit=8)
    assert hidden_usage["has_usage"] is True
    assert hidden_usage["frequent"] == [] and hidden_usage["recent"] == []


def _now_for_test() -> str:
    return datetime.now(UTC).isoformat()
