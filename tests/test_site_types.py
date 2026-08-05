from __future__ import annotations

import asyncio
import sqlite3
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from site_hub.config import Settings
from site_hub.db import Database, MIGRATIONS, apply_migrations
from site_hub.db.connection import stable_uuid
from site_hub.main import create_app
from site_hub.services.backups import BackupService
from site_hub.services.data_store import DataLayerStore
from site_hub.services.memory_store import MemoryStore
from site_hub.services.ports import SiteTypeConflict


HOST = "127.0.0.1:8686"
ORIGIN = f"http://{HOST}"


def _settings(tmp_path) -> Settings:
    frontend = tmp_path / "frontend" / "dist"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text("<!doctype html><title>SiteHub</title>", encoding="utf-8")
    return Settings(project_root=tmp_path, data_dir=tmp_path / "data", frontend_dir=frontend)


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    return {"X-CSRF-Token": token, "Origin": ORIGIN}


def test_v2_upgrade_preserves_known_and_unknown_site_kinds(tmp_path):
    database = Database(tmp_path / "upgrade.db")
    apply_migrations(database, MIGRATIONS[:2])
    now = "2026-08-04T00:00:00.000Z"
    with database.transaction() as conn:
        conn.execute(
            """INSERT INTO folders(id,parent_id,name,sort_order,created_at,updated_at)
               VALUES('folder',NULL,'Imported',0,?,?)""",
            (now, now),
        )
        for identifier, kind in (("shop-site", "shop"), ("forum-site", "forum")):
            conn.execute(
                """INSERT INTO sites(
                       id,folder_id,name,origin,canonical_host,kind,status,description,note,
                       icon,pinned,sort_order,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,'active','','','',0,0,?,?)""",
                (
                    identifier, "folder", identifier, f"https://{identifier}.test",
                    f"{identifier}.test", kind, now, now,
                ),
            )

    apply_migrations(database)

    with database.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        rows = {
            row["slug"]: dict(row) for row in conn.execute(
                "SELECT * FROM site_types WHERE slug IN ('shop','forum')"
            )
        }
        assert rows["shop"]["name"] == "商城"
        assert rows["forum"]["id"] == stable_uuid("site-type:forum")
        sites = {
            row["id"]: dict(row) for row in conn.execute(
                "SELECT id,kind,site_type_id FROM sites"
            )
        }
        assert sites["shop-site"]["site_type_id"] == rows["shop"]["id"]
        assert sites["forum-site"] == {
            "id": "forum-site", "kind": "forum", "site_type_id": rows["forum"]["id"],
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    with database.transaction() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO sites(
                   id,folder_id,name,origin,canonical_host,kind,status,description,note,
                   icon,pinned,sort_order,created_at,updated_at
               ) VALUES('invalid','folder','Invalid','https://invalid.test','invalid.test',
                        'other','active','','','',0,0,?,?)""",
            (now, now),
        )


def test_staged_v2_restore_is_upgraded_before_sitehub_becomes_ready(tmp_path):
    settings = _settings(tmp_path)
    settings.prepare_directories()
    database = Database(settings.db_path)
    apply_migrations(database, MIGRATIONS[:2])
    now = "2026-08-04T00:00:00.000Z"

    def insert_site(identifier: str, kind: str) -> None:
        with database.transaction() as conn:
            conn.execute(
                """INSERT INTO sites(
                       id,folder_id,name,origin,canonical_host,kind,status,description,note,
                       icon,pinned,sort_order,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,'active','','','',0,0,?,?)""",
                (
                    identifier, "folder", identifier, f"https://{identifier}.test",
                    f"{identifier}.test", kind, now, now,
                ),
            )

    with database.transaction() as conn:
        conn.execute(
            """INSERT INTO folders(id,parent_id,name,sort_order,created_at,updated_at)
               VALUES('folder',NULL,'Imported',0,?,?)""",
            (now, now),
        )
    insert_site("kept-shop", "shop")

    backups = BackupService(settings.data_dir)
    candidate = backups.create_backup(label="V2 restore candidate")
    insert_site("removed-tool", "tool")
    backups.stage_restore(candidate.backup_id)

    app = create_app(
        settings=settings,
        store=DataLayerStore(settings.db_path),
        start_background=False,
    )
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        assert client.get("/api/v1/health/ready").status_code == 200
        sites = client.get("/api/v1/sites", params={"page_size": 100}).json()["items"]
        assert [(site["id"], site["kind"], site["site_type"]["slug"]) for site in sites] == [
            ("kept-shop", "shop", "shop"),
        ]
        assert {item["slug"] for item in client.get("/api/v1/site-types").json()} >= {
            "other", "shop", "checkin", "tool", "bookmark",
        }

    with database.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert backups.pending_restore() is None


@pytest.mark.asyncio
async def test_data_store_site_type_lifecycle_filter_and_bulk_assignment(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    try:
        builtins = await store.list_site_types()
        assert {row["slug"] for row in builtins} >= {"other", "shop", "checkin", "tool", "bookmark"}
        assert sum(1 for row in builtins if row["is_default"]) == 1
        other = next(row for row in builtins if row["is_default"])
        bookmark = next(row for row in builtins if row["slug"] == "bookmark")
        imported_bookmark = await store.create_site({
            "name": "Imported bookmark", "url": "https://bookmark.test",
            "kind": "bookmark",
        })
        assert imported_bookmark["site_type_id"] == bookmark["id"]
        assert imported_bookmark["site_type"]["slug"] == "bookmark"

        custom = await store.create_site_type({
            "name": "论坛", "slug": "forum", "color": "#7C3AED", "icon": "messages-square",
            "sort_order": 50, "enabled": True,
        })
        assert custom["color"] == "#7c3aed"
        site = await store.create_site({
            "name": "Community", "url": "https://community.test/topic",
            "site_type_id": custom["id"],
        })
        assert site["kind"] == "forum"
        assert site["site_type"]["name"] == "论坛"

        filtered, total = await store.list_sites(
            page=1, page_size=20, site_type_id=UUID(custom["id"]),
        )
        assert total == 1 and filtered[0]["id"] == site["id"]

        renamed = await store.update_site_type(UUID(custom["id"]), {
            "name": "社区", "slug": "community",
        })
        assert renamed and renamed["site_count"] == 1
        refreshed = await store.get_site(UUID(site["id"]))
        assert refreshed and refreshed["kind"] == "community"

        await store.update_site_type(UUID(custom["id"]), {"enabled": False})
        # Re-saving an existing disabled type is allowed; assigning it to a
        # different site is not.
        updated = await store.update_site(UUID(site["id"]), {
            "name": "Community Updated", "site_type_id": custom["id"],
        })
        assert updated and updated["name"] == "Community Updated"
        with pytest.raises(SiteTypeConflict) as disabled:
            await store.create_site({
                "name": "Blocked", "url": "https://blocked.test",
                "site_type_id": custom["id"],
            })
        assert disabled.value.code == "SITE_TYPE_DISABLED"
        with pytest.raises(SiteTypeConflict) as in_use:
            await store.delete_site_type(UUID(custom["id"]))
        assert in_use.value.code == "SITE_TYPE_IN_USE"

        with pytest.raises(SiteTypeConflict) as atomic:
            await store.bulk_set_site_type(
                [UUID(site["id"]), uuid4()], UUID(other["id"]),
            )
        assert atomic.value.code == "SITE_NOT_FOUND"
        after_failed_bulk = await store.get_site(UUID(site["id"]))
        assert after_failed_bulk and after_failed_bulk["kind"] == "community"

        bulk = await store.bulk_set_site_type(
            [UUID(site["id"])], UUID(other["id"]),
        )
        assert bulk["updated"] == 1
        after_bulk = await store.get_site(UUID(site["id"]))
        assert after_bulk and after_bulk["kind"] == "other"
        assert await store.delete_site_type(UUID(custom["id"])) is True
    finally:
        await store.close()


def test_site_type_api_contract_and_static_bulk_route(tmp_path):
    app = create_app(settings=_settings(tmp_path), store=MemoryStore(), start_background=False)
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        headers = _csrf(client)
        listing = client.get("/api/v1/site-types")
        assert listing.status_code == 200
        other = next(row for row in listing.json() if row["is_default"])

        created = client.post(
            "/api/v1/site-types",
            json={"name": "论坛", "slug": "forum", "color": "#7C3AED", "icon": "messages-square"},
            headers=headers,
        )
        assert created.status_code == 201
        site_type = created.json()
        site = client.post(
            "/api/v1/sites",
            json={"name": "Community", "url": "https://community.test", "site_type_id": site_type["id"]},
            headers=headers,
        ).json()
        assert site["site_type_id"] == site_type["id"]
        assert site["kind"] == "forum"

        mismatch = client.post(
            "/api/v1/sites",
            json={
                "name": "Mismatched", "url": "https://mismatched.test",
                "site_type_id": site_type["id"], "kind": "other",
            },
            headers=headers,
        )
        assert mismatch.status_code == 422
        assert mismatch.json()["error"]["code"] == "SITE_TYPE_MISMATCH"

        filtered = client.get("/api/v1/sites", params={"site_type_id": site_type["id"]}).json()
        assert filtered["total"] == 1
        moved = client.post(
            "/api/v1/sites/bulk/site-type",
            json={"site_ids": [site["id"]], "site_type_id": other["id"]},
            headers=headers,
        )
        assert moved.status_code == 200
        assert moved.json()["updated"] == 1
        assert client.get(f"/api/v1/sites/{site['id']}").json()["kind"] == "other"

        duplicate = client.post(
            "/api/v1/site-types",
            json={"name": "论坛", "slug": "forum-2"}, headers=headers,
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "SITE_TYPE_CONFLICT"
        protected = client.delete(
            f"/api/v1/site-types/{other['id']}", headers=headers,
        )
        assert protected.status_code == 409
        assert protected.json()["error"]["code"] == "SITE_TYPE_PROTECTED"
