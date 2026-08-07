from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from site_hub.config import Settings
from site_hub.main import create_app
from site_hub.services.data_store import DataLayerStore


HOST = "127.0.0.1:8686"
ORIGIN = f"http://{HOST}"


async def _seed(store: DataLayerStore) -> tuple[str, str]:
    await store.initialize()
    first = await store.create_folder({"name": "First"})
    second = await store.create_folder({"name": "Second"})

    async def create(name: str, folder_id: str | None):
        return await store.create_site({
            "name": name,
            "url": f"https://{name.casefold()}.example.test",
            "folder_id": folder_id,
        })

    await create("Alpha", first["id"])
    await create("Beta", first["id"])
    await create("Gamma", second["id"])
    await create("Unfiled", None)
    archived = await create("Archived", first["id"])
    assert await store.delete_site(archived["id"])
    return str(first["id"]), str(second["id"])


def test_site_directory_stats_are_global_and_exclude_archived_sites(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        frontend_dir=tmp_path / "frontend" / "dist",
    )
    store = DataLayerStore(settings.data_dir / "site-hub.db")
    first_id, second_id = asyncio.run(_seed(store))

    stats = asyncio.run(store.get_site_directory_stats())
    assert stats == {
        "total": 4,
        "unfiled": 1,
        "by_folder": {first_id: 2, second_id: 1},
    }

    app = create_app(settings=settings, store=store, start_background=False)
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        response = client.get("/api/v1/sites/stats")

    assert response.status_code == 200
    assert response.json() == stats
