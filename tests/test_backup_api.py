from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from site_hub.config import Settings
from site_hub.main import create_app
from site_hub.services.data_store import DataLayerStore


HOST = "127.0.0.1:8686"
ORIGIN = f"http://{HOST}"


def _settings(tmp_path) -> Settings:
    frontend = tmp_path / "frontend" / "dist"
    frontend.mkdir(parents=True, exist_ok=True)
    (frontend / "index.html").write_text(
        "<!doctype html><title>SiteHub</title>", encoding="utf-8",
    )
    return Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        frontend_dir=frontend,
    )


def _client(app):
    return TestClient(app, base_url=ORIGIN, headers={"Host": HOST})


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    return {"X-CSRF-Token": token, "Origin": ORIGIN}


def test_backup_api_create_verify_stage_and_cancel(tmp_path) -> None:
    settings = _settings(tmp_path)
    app = create_app(
        settings=settings,
        store=DataLayerStore(settings.db_path),
        start_background=False,
    )
    with _client(app) as client:
        headers = _csrf(client)
        assert client.get("/api/v1/backups").json() == {
            "items": [], "total": 0, "pending": None,
        }
        assert client.post("/api/v1/backups", json={"label": "Blocked"}).status_code == 403

        created = client.post(
            "/api/v1/backups", json={"label": "Before settings change"}, headers=headers,
        )
        assert created.status_code == 201
        backup = created.json()
        assert backup["label"] == "Before settings change"
        assert len(backup["database_sha256"]) == 64

        verified = client.post(
            f"/api/v1/backups/{backup['backup_id']}/verify", headers=headers,
        )
        assert verified.status_code == 200
        assert verified.json()["integrity_check"] == "ok"

        wrong = client.post(
            f"/api/v1/backups/{backup['backup_id']}/restore",
            json={"confirmation": "wrong"}, headers=headers,
        )
        assert wrong.status_code == 400
        assert wrong.json()["error"]["code"] == "CONFIRMATION_REQUIRED"

        staged = client.post(
            f"/api/v1/backups/{backup['backup_id']}/restore",
            json={"confirmation": backup["backup_id"]}, headers=headers,
        )
        assert staged.status_code == 202
        assert staged.json()["restart_required"] is True
        listed = client.get("/api/v1/backups").json()
        assert listed["pending"]["backup_id"] == backup["backup_id"]

        assert client.delete(
            "/api/v1/backups/restore-pending", headers=headers,
        ).status_code == 204
        assert client.get("/api/v1/backups").json()["pending"] is None


def test_staged_restore_applies_before_database_initialization_on_next_start(tmp_path) -> None:
    settings = _settings(tmp_path)
    first_app = create_app(
        settings=settings,
        store=DataLayerStore(settings.db_path),
        start_background=False,
    )
    with _client(first_app) as client:
        headers = _csrf(client)
        first = client.post(
            "/api/v1/sites",
            json={"name": "Kept", "url": "https://kept.example"},
            headers=headers,
        )
        assert first.status_code == 201
        backup = client.post(
            "/api/v1/backups", json={"label": "Known good"}, headers=headers,
        ).json()
        second = client.post(
            "/api/v1/sites",
            json={"name": "Removed by restore", "url": "https://removed.example"},
            headers=headers,
        )
        assert second.status_code == 201
        staged = client.post(
            f"/api/v1/backups/{backup['backup_id']}/restore",
            json={"confirmation": backup["backup_id"]}, headers=headers,
        )
        assert staged.status_code == 202

    second_app = create_app(
        settings=settings,
        store=DataLayerStore(settings.db_path),
        start_background=False,
    )
    with _client(second_app) as client:
        sites = client.get("/api/v1/sites", params={"page_size": 100}).json()
        assert [item["name"] for item in sites["items"]] == ["Kept"]
        backup_list = client.get("/api/v1/backups").json()
        assert backup_list["pending"] is None
        assert any(
            item.get("record", {}).get("kind") == "pre_restore"
            for item in backup_list["items"]
            if item.get("record")
        )


def test_failed_restored_database_initialization_rolls_back_original(tmp_path) -> None:
    settings = _settings(tmp_path)
    first_app = create_app(
        settings=settings,
        store=DataLayerStore(settings.db_path),
        start_background=False,
    )
    with _client(first_app) as client:
        headers = _csrf(client)
        assert client.post(
            "/api/v1/sites",
            json={"name": "Before backup", "url": "https://before.example"},
            headers=headers,
        ).status_code == 201
        backup = client.post(
            "/api/v1/backups", json={"label": "Restore candidate"}, headers=headers,
        ).json()
        assert client.post(
            "/api/v1/sites",
            json={"name": "Current data", "url": "https://current.example"},
            headers=headers,
        ).status_code == 201
        assert client.post(
            f"/api/v1/backups/{backup['backup_id']}/restore",
            json={"confirmation": backup["backup_id"]}, headers=headers,
        ).status_code == 202

    class FailingStore(DataLayerStore):
        async def initialize(self) -> None:
            await super().initialize()
            raise RuntimeError("simulated restored database validation failure")

    failing_app = create_app(
        settings=settings,
        store=FailingStore(settings.db_path),
        start_background=False,
    )
    with pytest.raises(RuntimeError, match="simulated restored database"):
        with _client(failing_app):
            pass

    recovered_app = create_app(
        settings=settings,
        store=DataLayerStore(settings.db_path),
        start_background=False,
    )
    with _client(recovered_app) as client:
        sites = client.get("/api/v1/sites", params={"page_size": 100}).json()
        assert {item["name"] for item in sites["items"]} == {
            "Before backup", "Current data",
        }
        assert client.get("/api/v1/backups").json()["pending"] is None
