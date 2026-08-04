from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from site_hub.config import Settings
from site_hub.jobs.service import JobService
from site_hub.main import create_app
from site_hub.services.data_store import DataLayerStore
from site_hub.services.events import EventBroker
from site_hub.services.memory_store import MemoryStore
from site_hub.services.ports import AccountNotReady
from site_hub.services.secrets import EphemeralSecretProtector, SecretService


HOST = "127.0.0.1:8686"
ORIGIN = f"http://{HOST}"


def _settings(tmp_path, *, with_frontend: bool = True) -> Settings:
    frontend = tmp_path / "frontend" / "dist"
    if with_frontend:
        frontend.mkdir(parents=True)
        (frontend / "index.html").write_text("<!doctype html><title>SiteHub</title>", encoding="utf-8")
    return Settings(project_root=tmp_path, data_dir=tmp_path / "data", frontend_dir=frontend)


def _csrf(client: TestClient) -> dict[str, str]:
    response = client.get("/api/v1/bootstrap")
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"], "Origin": ORIGIN}


def test_local_security_and_readiness_contract(tmp_path):
    app = create_app(settings=_settings(tmp_path), store=MemoryStore(), start_background=False)
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        assert client.get("/api/v1/health/live").json() == {"status": "ok"}
        ready = client.get("/api/v1/health/ready")
        assert ready.status_code == 200
        assert ready.json()["components"] == {
            "database": "ok",
            "frontend": "ok",
            "secrets": "skipped",
            "directories": {"data": "ok", "profiles": "ok", "artifacts": "ok", "logs": "ok"},
            "scheduler": "skipped",
            "dispatcher": "skipped",
            "system_dispatcher": "skipped",
            "notification_outbox": "skipped",
            "worker": "skipped",
        }

        invalid_host = client.get("/api/v1/bootstrap", headers={"Host": "attacker.invalid"})
        assert invalid_host.status_code == 400
        assert invalid_host.json()["error"]["code"] == "INVALID_HOST"

        no_csrf = client.post("/api/v1/folders", json={"name": "Blocked"})
        assert no_csrf.status_code == 403
        assert no_csrf.json()["error"]["code"] == "CSRF_FAILED"

        headers = _csrf(client)
        cross_origin = client.post(
            "/api/v1/folders", json={"name": "Blocked"},
            headers={**headers, "Origin": "https://attacker.invalid"},
        )
        assert cross_origin.status_code == 403
        assert cross_origin.json()["error"]["code"] == "CROSS_ORIGIN"
        assert client.post("/api/v1/folders", json={"name": "Allowed"}, headers=headers).status_code == 201


def test_legacy_detail_requires_explicit_csrf_protected_reveal(tmp_path):
    app = create_app(settings=_settings(tmp_path), store=MemoryStore(), start_background=False)
    path = "/api/v1/runs/00000000-0000-0000-0000-000000000001/legacy-detail"
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        assert client.get(path).status_code in {404, 405}
        assert client.post(path, json={"confirm": True}).status_code == 403
        response = client.post(path, json={"confirm": False}, headers=_csrf(client))
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "CONFIRMATION_REQUIRED"


def test_product_settings_missing_site_is_consistently_not_found(tmp_path):
    app = create_app(settings=_settings(tmp_path), store=MemoryStore(), start_background=False)
    missing = "00000000-0000-0000-0000-000000000099"
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        assert client.get(f"/api/v1/sites/{missing}/product-settings").status_code == 404
        response = client.put(
            f"/api/v1/sites/{missing}/product-settings",
            json={"enabled": True}, headers=_csrf(client),
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


def test_profile_verify_route_queues_login_job_with_verify_action(tmp_path):
    store = MemoryStore()
    site = asyncio.run(store.create_site({
        "name": "Check-in", "url": "https://example.test/account", "status": "active",
    }))
    account = asyncio.run(store.create_account(UUID(str(site["id"])), {
        "name": "Primary", "adapter": "worker-adapter", "enabled": False,
    }))
    app = create_app(settings=_settings(tmp_path), store=store, start_background=False)

    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        response = client.post(
            f"/api/v1/accounts/{account['id']}/login-session/verify",
            headers=_csrf(client),
        )
        assert response.status_code == 200
        task = response.json()
        assert task["kind"] == "login_session"
        queued = asyncio.run(store.get_job(UUID(task["jobId"])))
        assert queued and queued["payload"] == {"action": "verify"}
        blocked = client.patch(
            f"/api/v1/accounts/{account['id']}", json={"enabled": True},
            headers=_csrf(client),
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"] == {
            "code": "ACCOUNT_NOT_READY",
            "message": "Verify the browser Profile before enabling this account",
            "field": "enabled",
        }
        blocked_run = client.post(
            f"/api/v1/accounts/{account['id']}/run", headers=_csrf(client),
        )
        assert blocked_run.status_code == 409
        assert blocked_run.json()["error"]["code"] == "ACCOUNT_NOT_READY"
        assert blocked_run.json()["error"]["message"] == (
            "Verify the browser Profile before running check-in"
        )

        direct = asyncio.run(store.create_account(UUID(str(site["id"])), {
            "name": "Direct", "adapter": "api", "enabled": True,
        }))
        with pytest.raises(AccountNotReady):
            asyncio.run(store.update_account(UUID(str(direct["id"])), {
                "adapter": "playwright-generic",
            }))


def test_cancel_route_notifies_python_product_dispatcher(tmp_path):
    store = MemoryStore()
    app = create_app(settings=_settings(tmp_path), store=store, start_background=False)

    class CancelSpy:
        def __init__(self) -> None:
            self.job_ids: list[UUID] = []

        async def cancel_execution(self, job_id: UUID) -> bool:
            self.job_ids.append(job_id)
            return True

    spy = CancelSpy()
    app.state.dispatcher = spy
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        headers = _csrf(client)
        site = client.post(
            "/api/v1/sites",
            json={
                "name": "Shop", "url": "https://shop.example",
                "capabilities": ["product_monitor"],
            },
            headers=headers,
        ).json()
        task = client.post(
            f"/api/v1/sites/{site['id']}/sync", headers=headers,
        ).json()
        response = client.post(
            f"/api/v1/jobs/{task['jobId']}/cancel", headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert spy.job_ids == [UUID(task["jobId"])]


def test_builtin_product_adapter_manifests_have_editable_schemas(tmp_path):
    app = create_app(settings=_settings(tmp_path), store=MemoryStore(), start_background=False)
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        manifests = client.get("/api/v1/adapters").json()
    product = {
        item["id"]: item for item in manifests
        if "product_monitor" in item["capabilities"]
    }
    assert set(product) == {"classic", "next_api", "custom_spa", "manual", "manager"}
    assert "api_path" in product["next_api"]["site_config_schema"]["properties"]
    assert product["next_api"]["secret_fields"] == [
        "headers.Authorization", "headers.X-Api-Key",
    ]
    assert "probe_order" in product["manager"]["site_config_schema"]["properties"]


def test_readiness_reports_component_failures(tmp_path):
    app = create_app(
        settings=_settings(tmp_path, with_frontend=False), store=MemoryStore(), start_background=False,
    )
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "NOT_READY"
        assert error["context"]["components"]["frontend"] == "error"


def test_folder_batch_and_unified_schedule_routes(tmp_path):
    store = MemoryStore()
    app = create_app(settings=_settings(tmp_path), store=store, start_background=False)
    app.state.supervisor.adapters = [{
        "id": "test-adapter", "name": "Test adapter", "version": "1.0.0",
        "capabilities": ["checkin"],
    }]
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        headers = _csrf(client)
        parent = client.post("/api/v1/folders", json={"name": "Parent"}, headers=headers).json()
        child = client.post(
            "/api/v1/folders", json={"name": "Child", "parent_id": parent["id"]}, headers=headers,
        ).json()
        cycle = client.patch(
            f"/api/v1/folders/{parent['id']}", json={"parent_id": child["id"]}, headers=headers,
        )
        assert cycle.status_code == 409
        renamed = client.patch(
            f"/api/v1/folders/{child['id']}", json={"name": "Renamed"}, headers=headers,
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Renamed"

        site = client.post(
            "/api/v1/sites",
            json={"name": "Example", "url": "https://example.test/catalog", "folder_id": child["id"]},
            headers=headers,
        ).json()
        account = client.post(
            f"/api/v1/sites/{site['id']}/accounts",
            json={
                "label": "Primary", "adapter": "test-adapter",
                "schedule": {"mode": "fixed_time", "time": "09:30"},
            },
            headers=headers,
        ).json()
        assert account["profile"] == {
            "relative_path": account["id"], "state": "unverified",
            "manifest_sha256": None, "file_count": None, "total_bytes": None,
            "last_verified_at": None, "verification_error": None,
        }
        schedules = client.get("/api/v1/schedules", params={"capability": "checkin"}).json()
        assert schedules["total"] == 1
        assert schedules["items"][0]["policy"]["mode"] == "fixed_time"

        batch = asyncio.run(store.create_batch(
            {"date_key": "2026-08-03", "planned_at": datetime(2026, 8, 3, 1, 30, tzinfo=UTC)},
            [UUID(account["id"])],
        ))
        batches = client.get("/api/v1/batches", params={"date_from": "2026-08-03"}).json()
        assert batches["total"] == 1
        detail = client.get(f"/api/v1/batches/{batch['id']}")
        assert detail.status_code == 200
        assert detail.json()["items"][0]["account_id"] == account["id"]

        deleted = client.delete(f"/api/v1/folders/{child['id']}", headers=headers)
        assert deleted.status_code == 204
        assert client.get(f"/api/v1/sites/{site['id']}").json()["folder_id"] == parent["id"]
        protected = client.delete(
            "/api/v1/folders/00000000-0000-0000-0000-000000000001", headers=headers,
        )
        assert protected.status_code == 409


def test_account_adapter_must_advertise_checkin_capability(tmp_path):
    store = MemoryStore()
    app = create_app(settings=_settings(tmp_path), store=store, start_background=False)
    app.state.supervisor.adapters = [{
        "id": "api", "name": "HTTP API", "version": "1.0.0",
        "capabilities": ["checkin"],
    }]
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        headers = _csrf(client)
        site = client.post(
            "/api/v1/sites",
            json={"name": "Example", "url": "https://example.test"},
            headers=headers,
        ).json()
        response = client.post(
            f"/api/v1/sites/{site['id']}/accounts",
            json={"label": "Invalid", "adapter": "classic"},
            headers=headers,
        )
        assert response.status_code == 422
        assert response.json()["error"] == {
            "code": "ADAPTER_CAPABILITY_MISMATCH",
            "message": "Adapter 'classic' does not support checkin",
            "field": "adapter",
        }
        unavailable = client.post(
            f"/api/v1/sites/{site['id']}/accounts",
            json={"label": "Invalid", "adapter": "none"},
            headers=headers,
        )
        assert unavailable.status_code == 422
        assert unavailable.json()["error"]["code"] == "ADAPTER_NOT_AVAILABLE"
        product_mismatch = client.put(
            f"/api/v1/sites/{site['id']}/product-settings",
            json={"scraper_type": "api"},
            headers=headers,
        )
        assert product_mismatch.status_code == 422
        assert product_mismatch.json()["error"] == {
            "code": "ADAPTER_CAPABILITY_MISMATCH",
            "message": "Adapter 'api' does not support product_monitor",
            "field": "scraper_type",
        }
        assert asyncio.run(store.list_accounts(UUID(site["id"]))) == []


def test_bookmark_import_preserves_hierarchy_and_skips_only_exact_urls(tmp_path):
    store = MemoryStore()
    app = create_app(settings=_settings(tmp_path), store=store, start_background=True)
    exported = b"""<!DOCTYPE NETSCAPE-Bookmark-file-1>
    <DL><p>
      <DT><H3>Work</H3>
      <DL><p>
        <DT><A HREF=\"https://example.test/a?token=one\">Existing</A>
        <DT><H3>Nested</H3>
        <DL><p>
          <DT><A HREF=\"https://example.test/b\">Different path</A>
          <DT><A HREF=\"javascript:alert(1)\">Invalid</A>
        </DL><p>
      </DL><p>
    </DL><p>"""
    with TestClient(app, base_url=ORIGIN, headers={"Host": HOST}) as client:
        headers = _csrf(client)
        existing = client.post(
            "/api/v1/sites", json={"name": "Existing", "url": "https://example.test/a?token=one"},
            headers=headers,
        )
        assert existing.status_code == 201
        started = client.post(
            "/api/v1/imports/bookmarks", files={"file": ("bookmarks.html", exported, "text/html")},
            headers=headers,
        )
        assert started.status_code == 202
        task = started.json()
        assert task["kind"] == "bookmark_import"
        for _ in range(100):
            state = client.get(f"/api/v1/jobs/{task['jobId']}").json()
            if state["status"] not in {"queued", "leased", "running"}:
                break
            time.sleep(0.01)
        assert state["status"] == "succeeded"
        assert {key: state["result"][key] for key in (
            "total", "imported", "duplicates", "invalid", "folders_created",
        )} == {
            "total": 3, "imported": 1, "duplicates": 1, "invalid": 1, "folders_created": 2,
        }
        assert state["result"]["source_size"] == len(exported)
        assert not list((tmp_path / "data" / "staging" / "bookmarks").glob("*.html"))
        sites = client.get("/api/v1/sites", params={"page_size": 20}).json()["items"]
        assert {item["url"] for item in sites} == {
            "https://example.test/a?token=one", "https://example.test/b",
        }
        folders = client.get("/api/v1/folders").json()
        work = next(item for item in folders if item["name"] == "Work")
        nested = next(item for item in folders if item["name"] == "Nested")
        assert nested["parent_id"] == work["id"]


@pytest.mark.asyncio
async def test_sensitive_url_and_nested_account_secrets_never_reach_plaintext_database(tmp_path):
    database_path = tmp_path / "site-hub.db"
    store = DataLayerStore(database_path)
    await store.initialize()
    store.secret_service = SecretService(
        store.database, EphemeralSecretProtector(b"control-plane-test-key-32-bytes!!"),
    )
    url_secret = "url-secret-never-plaintext"
    account_secret = "account-secret-never-plaintext"
    webhook_secret = "webhook-secret-never-plaintext"
    api_key_secret = "api-key-secret-never-plaintext"
    callback_secret = "callback-secret-never-plaintext"
    try:
        site = await store.create_site({
            "name": "Sensitive",
            "url": f"https://private.test/catalog?token={url_secret}#account",
            "capabilities": ["bookmark"],
        })
        site = await store.update_site(UUID(str(site["id"])), {
            "adapter": "next-api",
            "config": {
                "catalog_path": "/products",
                "Authorization": "site-adapter-secret-never-plaintext",
                "callback_url": "https://private.test/products?key=site-callback-secret",
            },
        })
        assert site is not None
        assert site["config"] == {"catalog_path": "/products"}
        assert site["configured_secret_fields"] == ["Authorization", "callback_url"]
        account = await store.create_account(UUID(str(site["id"])), {
            "name": "Primary", "adapter": "arbitrary-worker-adapter", "enabled": True,
            "config": {
                "headers": {"Authorization": account_secret},
                "nested": [{"cookie": "cookie-secret-never-plaintext"}],
                "webhook": f"https://hooks.test/{webhook_secret}",
                "X-Api-Key": api_key_secret,
                "callback_url": f"https://private.test/callback#{callback_secret}",
                "visible": "ordinary-value",
            },
        })
        with store.database.transaction() as connection:
            connection.execute(
                """UPDATE browser_profiles SET state='ready',manifest_sha256=?,file_count=7,
                          total_bytes=4096,last_verified_at=?,verification_error=NULL
                   WHERE account_id=?""",
                ("a" * 64, "2026-08-03T10:00:00Z", str(account["id"])),
            )

        assert site["url"] == "https://private.test/catalog?token=%3Credacted%3E"
        assert url_secret not in site["url"]
        runtime_site = await store.get_site_runtime(UUID(str(site["id"])))
        assert runtime_site and runtime_site["url"].endswith(f"?token={url_secret}#account")
        public_account = (await store.list_accounts(UUID(str(site["id"]))))[0]
        assert "runtime_config" not in public_account
        assert public_account["config"]["visible"] == "ordinary-value"
        assert "Authorization" not in public_account["config"].get("headers", {})
        assert account_secret not in str(public_account)
        assert public_account["profile"] == {
            "relative_path": str(account["id"]), "state": "ready",
            "manifest_sha256": "a" * 64, "file_count": 7, "total_bytes": 4096,
            "last_verified_at": "2026-08-03T10:00:00Z", "verification_error": None,
        }
        # A frontend-style edit round-trip contains only public ordinary config.
        # Existing encrypted fields must remain bound instead of being erased.
        updated_account = await store.update_account(UUID(str(account["id"])), {
            "name": "Primary renamed", "config": public_account["config"],
        })
        assert updated_account and updated_account["config"]["visible"] == "ordinary-value"
        runtime_account = await store.get_account(UUID(str(account["id"])))
        assert runtime_account
        assert runtime_account["runtime_config"]["headers"]["Authorization"] == account_secret
        assert runtime_account["runtime_config"]["nested"][0]["cookie"] == "cookie-secret-never-plaintext"
        assert runtime_account["runtime_config"]["webhook"].endswith(webhook_secret)
        assert runtime_account["runtime_config"]["X-Api-Key"] == api_key_secret
        assert runtime_account["runtime_config"]["callback_url"].endswith(f"#{callback_secret}")

        # The site adapter follows the same safe round-trip contract.
        updated_site = await store.update_site(UUID(str(site["id"])), {
            "name": "Sensitive renamed", "config": site["config"],
        })
        assert updated_site and updated_site["config"] == {"catalog_path": "/products"}
        assert updated_site["configured_secret_fields"] == ["Authorization", "callback_url"]
        runtime_product = await store.get_product_runtime(UUID(str(site["id"])))
        assert runtime_product
        assert runtime_product["runtime_config"]["Authorization"] == "site-adapter-secret-never-plaintext"
        assert runtime_product["runtime_config"]["callback_url"].endswith("site-callback-secret")

        cleared_account = await store.update_account(UUID(str(account["id"])), {
            "config": updated_account["config"], "clear_secrets": ["nested.0.cookie"],
        })
        assert cleared_account and "nested.0.cookie" not in cleared_account["configured_secret_fields"]
        cleared_account_runtime = await store.get_account(UUID(str(account["id"])))
        assert cleared_account_runtime
        assert "cookie" not in cleared_account_runtime["runtime_config"]["nested"][0]

        cleared_site = await store.update_site(UUID(str(site["id"])), {
            "config": updated_site["config"], "clear_secrets": ["Authorization"],
        })
        assert cleared_site and "Authorization" not in cleared_site["configured_secret_fields"]
        cleared_product_runtime = await store.get_product_runtime(UUID(str(site["id"])))
        assert cleared_product_runtime
        assert "Authorization" not in cleared_product_runtime["runtime_config"]

        stored_bytes = b"".join(path.read_bytes() for path in tmp_path.glob("site-hub.db*"))
        assert url_secret.encode() not in stored_bytes
        assert account_secret.encode() not in stored_bytes
        assert b"cookie-secret-never-plaintext" not in stored_bytes
        assert webhook_secret.encode() not in stored_bytes
        assert api_key_secret.encode() not in stored_bytes
        assert callback_secret.encode() not in stored_bytes
        assert b"site-adapter-secret-never-plaintext" not in stored_bytes
        assert b"site-callback-secret" not in stored_bytes
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_site_creation_is_atomic_across_url_capabilities_schedule_and_tags(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()

    class FailingProtector:
        provider = "test"

        def protect(self, plaintext: bytes, purpose: str):
            raise RuntimeError("injected site URL secret failure")

        def unprotect(self, protected, purpose: str) -> bytes:
            raise AssertionError("not reached")

    def assert_empty() -> None:
        with store.database.connect() as connection:
            counts = tuple(connection.execute(
                """SELECT (SELECT COUNT(*) FROM sites),
                          (SELECT COUNT(*) FROM site_endpoints),
                          (SELECT COUNT(*) FROM capability_instances),
                          (SELECT COUNT(*) FROM schedules),
                          (SELECT COUNT(*) FROM site_tags),
                          (SELECT COUNT(*) FROM secrets)"""
            ).fetchone())
        assert counts == (0, 0, 0, 0, 0, 0)

    try:
        store.secret_service = SecretService(store.database, FailingProtector())
        with pytest.raises(RuntimeError, match="injected site URL secret failure"):
            await store.create_site({
                "name": "Atomic secret failure",
                "url": "https://atomic.test/catalog?token=secret",
                "capabilities": ["bookmark", "product_monitor"],
            })
        assert_empty()

        # This fails at the final tag binding, after the URL secret, capabilities,
        # and product schedule have all been staged in the same transaction.
        store.secret_service = SecretService(
            store.database, EphemeralSecretProtector(b"site-create-atomic-test-key-32!!"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            await store.create_site({
                "name": "Atomic late failure",
                "url": "https://atomic.test/catalog?token=secret",
                "capabilities": ["bookmark", "product_monitor"],
                "tag_ids": ["00000000-0000-0000-0000-000000000099"],
            })
        assert_empty()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_site_update_rolls_back_every_domain_and_cleans_old_secrets_only_after_commit(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    protector = EphemeralSecretProtector(b"site-update-atomic-test-key-32!!")
    store.secret_service = SecretService(store.database, protector)

    class FailingProtector:
        provider = "test"

        def protect(self, plaintext: bytes, purpose: str):
            raise RuntimeError("injected product secret failure")

        def unprotect(self, protected, purpose: str) -> bytes:
            return protector.unprotect(protected, purpose)

    try:
        first_tag = await store.create_tag({"name": "Before"})
        second_tag = await store.create_tag({"name": "After"})
        site = await store.create_site({
            "name": "Before", "url": "https://before.test/home?token=old-url",
            "capabilities": ["bookmark", "product_monitor"],
            "tag_ids": [first_tag["id"]],
        })
        site = await store.update_site(UUID(str(site["id"])), {
            "adapter": "classic",
            "config": {"visible": "before", "token": "old-product-token"},
        })
        assert site is not None
        with store.database.transaction() as connection:
            product = connection.execute(
                """SELECT id FROM capability_instances
                   WHERE site_id=? AND code='product_monitor' AND account_id IS NULL""",
                (str(site["id"]),),
            ).fetchone()
            endpoint = connection.execute(
                """SELECT url_suffix_secret_id FROM site_endpoints WHERE site_id=?
                   ORDER BY is_default DESC,sort_order,id LIMIT 1""",
                (str(site["id"]),),
            ).fetchone()
            product_secret = connection.execute(
                """SELECT secret_id FROM capability_secrets
                   WHERE capability_instance_id=? AND name='token'""",
                (product["id"],),
            ).fetchone()
            connection.execute(
                "UPDATE schedules SET enabled=0 WHERE capability_instance_id=?",
                (product["id"],),
            )
        old_url_secret_id = str(endpoint["url_suffix_secret_id"])
        old_product_secret_id = str(product_secret["secret_id"])

        store.secret_service.protector = FailingProtector()
        with pytest.raises(RuntimeError, match="injected product secret failure"):
            await store.update_site(UUID(str(site["id"])), {
                "name": "After", "description": "must roll back",
                "url": "https://after.test/home",
                "tag_ids": [second_tag["id"]],
                "adapter": "next_api",
                "config": {"visible": "after", "password": "new-product-password"},
                "clear_secrets": ["token"],
            })
        store.secret_service.protector = protector

        unchanged = await store.get_site(UUID(str(site["id"])))
        runtime = await store.get_site_runtime(UUID(str(site["id"])))
        assert unchanged and runtime
        assert unchanged["name"] == "Before"
        assert unchanged["description"] == ""
        assert unchanged["adapter"] == "classic"
        assert unchanged["config"] == {"visible": "before"}
        assert unchanged["configured_secret_fields"] == ["token"]
        assert [tag["id"] for tag in unchanged["tags"]] == [first_tag["id"]]
        assert runtime["url"] == "https://before.test/home?token=old-url"
        with store.database.connect() as connection:
            assert connection.execute(
                "SELECT enabled FROM schedules WHERE capability_instance_id=?",
                (product["id"],),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM secrets WHERE id IN (?,?)",
                (old_url_secret_id, old_product_secret_id),
            ).fetchone()[0] == 2

        committed = await store.update_site(UUID(str(site["id"])), {
            "name": "After", "url": "https://after.test/home",
            "tag_ids": [second_tag["id"]], "adapter": "next_api",
            "config": {"visible": "after"}, "clear_secrets": ["token"],
        })
        assert committed
        assert committed["name"] == "After"
        assert committed["adapter"] == "next_api"
        assert committed["configured_secret_fields"] == []
        assert [tag["id"] for tag in committed["tags"]] == [second_tag["id"]]
        with store.database.connect() as connection:
            assert connection.execute(
                "SELECT enabled FROM schedules WHERE capability_instance_id=?",
                (product["id"],),
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM secrets WHERE id IN (?,?)",
                (old_url_secret_id, old_product_secret_id),
            ).fetchone()[0] == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_get_product_settings_has_no_database_side_effect(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    try:
        site = await store.create_site({
            "name": "Read only settings", "url": "https://example.test",
            "capabilities": ["bookmark"],
        })
        with store.database.connect() as connection:
            before = tuple(connection.execute(
                "SELECT (SELECT COUNT(*) FROM capability_instances),"
                "(SELECT COUNT(*) FROM schedules)"
            ).fetchone())
        settings = await store.get_product_settings(UUID(str(site["id"])))
        with store.database.connect() as connection:
            after = tuple(connection.execute(
                "SELECT (SELECT COUNT(*) FROM capability_instances),"
                "(SELECT COUNT(*) FROM schedules)"
            ).fetchone())
        assert settings == {
            "site_id": str(site["id"]), "enabled": False,
            "interval_override_minutes": None, "interval_minutes": 60,
            "effective_interval_minutes": 60, "scraper_type": "none",
            "configured": False, "configured_secret_fields": [],
            "config": {},
        }
        assert after == before
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_product_capability_always_has_a_synchronized_interval_schedule(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    try:
        created_with_product = await store.create_site({
            "name": "Scheduled at creation", "url": "https://created.test",
            "capabilities": ["bookmark", "product_monitor"],
        })
        bookmark_only = await store.create_site({
            "name": "Configured later", "url": "https://later.test",
            "capabilities": ["bookmark"],
        })

        configured = await store.update_site(UUID(str(bookmark_only["id"])), {
            "adapter": "next_api", "config": {"api_path": "/api/products"},
        })
        assert configured and configured["adapter"] == "next_api"

        with store.database.connect() as connection:
            rows = connection.execute(
                """SELECT c.site_id,c.enabled capability_enabled,s.enabled schedule_enabled,
                          s.policy_type,s.policy_json
                   FROM capability_instances c
                   JOIN schedules s ON s.capability_instance_id=c.id
                   WHERE c.code='product_monitor' AND c.site_id IN (?,?)
                   ORDER BY c.site_id""",
                (str(created_with_product["id"]), str(bookmark_only["id"])),
            ).fetchall()
        assert len(rows) == 2
        assert all(row["capability_enabled"] == 1 for row in rows)
        assert all(row["schedule_enabled"] == 1 for row in rows)
        assert all(row["policy_type"] == "interval" for row in rows)

        due = await store.list_due_product_sites(datetime.now(UTC))
        due_ids = {str(item["site_id"]) for item in due}
        # The interval schedule remains enabled and visible, but a site without
        # an automatic scraper must not manufacture hourly skipped jobs.
        assert str(created_with_product["id"]) not in due_ids
        assert str(bookmark_only["id"]) in due_ids

        disabled = await store.update_product_settings(
            UUID(str(bookmark_only["id"])), {"enabled": False},
        )
        assert disabled["enabled"] is False
        with store.database.connect() as connection:
            state = connection.execute(
                """SELECT c.enabled capability_enabled,s.enabled schedule_enabled
                   FROM capability_instances c
                   JOIN schedules s ON s.capability_instance_id=c.id
                   WHERE c.site_id=? AND c.code='product_monitor'""",
                (str(bookmark_only["id"]),),
            ).fetchone()
        assert tuple(state) == (0, 0)
        due_after_disable = await store.list_due_product_sites(datetime.now(UTC))
        assert str(bookmark_only["id"]) not in {
            str(item["site_id"]) for item in due_after_disable
        }
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_product_settings_reports_a_paused_schedule_as_disabled(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    try:
        site = await store.create_site({
            "name": "Paused import", "url": "https://paused.test",
            "capabilities": ["bookmark", "product_monitor"],
        })
        with store.database.transaction() as connection:
            connection.execute(
                """UPDATE schedules SET enabled=0 WHERE capability_instance_id IN (
                       SELECT id FROM capability_instances
                       WHERE site_id=? AND code='product_monitor'
                   )""",
                (str(site["id"]),),
            )

        settings = await store.get_product_settings(UUID(str(site["id"])))
        assert settings and settings["enabled"] is False

        # Editing adapter details must not silently unpause an imported schedule.
        await store.update_site(UUID(str(site["id"])), {"adapter": "classic"})
        still_paused = await store.get_product_settings(UUID(str(site["id"])))
        assert still_paused and still_paused["enabled"] is False

        enabled = await store.update_product_settings(
            UUID(str(site["id"])), {"enabled": True},
        )
        assert enabled["enabled"] is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_checkin_account_reports_and_explicitly_unpauses_the_full_schedule_chain(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    try:
        site = await store.create_site({
            "name": "Imported account", "url": "https://checkin-paused.test",
            "capabilities": ["bookmark"],
        })
        account = await store.create_account(UUID(str(site["id"])), {
            "name": "Primary", "adapter": "playwright-generic", "enabled": True,
            "config": {},
        })
        with store.database.transaction() as connection:
            connection.execute(
                """UPDATE schedules SET enabled=0 WHERE capability_instance_id IN (
                       SELECT id FROM capability_instances WHERE account_id=? AND code='checkin'
                   )""",
                (str(account["id"]),),
            )

        public = (await store.list_accounts(UUID(str(site["id"]))))[0]
        assert public["enabled"] is False
        assert public["auth_state"] == "unverified"
        assert public["login_state"] == "unverified"

        # Ordinary edits preserve the cutover pause.
        edited = await store.update_account(UUID(str(account["id"])), {"name": "Renamed"})
        assert edited and edited["enabled"] is False

        with pytest.raises(AccountNotReady):
            await store.update_account(UUID(str(account["id"])), {"enabled": True})
        await store.set_account_profile_verification(
            UUID(str(account["id"])), "ready", verified_at="2026-08-03T12:00:00.000Z",
        )
        enabled = await store.update_account(UUID(str(account["id"])), {"enabled": True})
        assert enabled and enabled["enabled"] is True
        with store.database.connect() as connection:
            state = connection.execute(
                """SELECT a.enabled,c.enabled,s.enabled
                   FROM site_accounts a
                   JOIN capability_instances c ON c.account_id=a.id AND c.code='checkin'
                   JOIN schedules s ON s.capability_instance_id=c.id
                   WHERE a.id=?""",
                (str(account["id"]),),
            ).fetchone()
        assert tuple(state) == (1, 1, 1)

        direct = await store.create_account(UUID(str(site["id"])), {
            "name": "Direct", "adapter": "api", "enabled": True,
        })
        with pytest.raises(AccountNotReady):
            await store.update_account(UUID(str(direct["id"])), {
                "adapter": "playwright-generic",
            })
        with pytest.raises(AccountNotReady):
            await store.update_account(UUID(str(direct["id"])), {
                "adapter": "playwright-generic", "enabled": True,
            })
        unchanged = await store.get_account(UUID(str(direct["id"])))
        assert unchanged and unchanged["adapter"] == "api" and unchanged["enabled"] is True

        paused = await store.update_account(UUID(str(direct["id"])), {"enabled": False})
        assert paused and paused["enabled"] is False
        switched = await store.update_account(UUID(str(direct["id"])), {
            "adapter": "playwright-generic",
        })
        assert switched and switched["adapter"] == "playwright-generic"
        assert switched["enabled"] is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_account_config_and_secret_updates_roll_back_together(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()
    protector = EphemeralSecretProtector(b"atomic-secret-test-key-32-bytes!!")
    store.secret_service = SecretService(store.database, protector)

    class FailingProtector:
        provider = "test"

        def protect(self, plaintext: bytes, purpose: str):
            raise RuntimeError("injected secret protection failure")

        def unprotect(self, protected, purpose: str) -> bytes:
            return protector.unprotect(protected, purpose)

    try:
        site = await store.create_site({"name": "Atomic", "url": "https://atomic.test"})
        account = await store.create_account(UUID(str(site["id"])), {
            "name": "Before", "adapter": "api", "enabled": True,
            "config": {"visible": "before", "token": "old-token"},
        })
        store.secret_service.protector = FailingProtector()
        with pytest.raises(RuntimeError, match="injected secret protection failure"):
            await store.update_account(UUID(str(account["id"])), {
                "name": "After", "config": {"visible": "after", "password": "new-password"},
                "clear_secrets": ["token"],
            })
        store.secret_service.protector = protector

        public = (await store.list_accounts(UUID(str(site["id"]))))[0]
        runtime = await store.get_account(UUID(str(account["id"])))
        assert public["name"] == "Before"
        assert public["config"] == {"visible": "before"}
        assert public["configured_secret_fields"] == ["token"]
        assert runtime and runtime["runtime_config"]["token"] == "old-token"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_account_creation_rolls_back_every_owned_row_when_secret_storage_fails(tmp_path):
    store = DataLayerStore(tmp_path / "site-hub.db")
    await store.initialize()

    class FailingProtector:
        provider = "test"

        def protect(self, plaintext: bytes, purpose: str):
            raise RuntimeError("injected account secret failure")

        def unprotect(self, protected, purpose: str) -> bytes:
            raise AssertionError("not reached")

    store.secret_service = SecretService(store.database, FailingProtector())
    try:
        site = await store.create_site({"name": "Atomic create", "url": "https://atomic.test"})
        with pytest.raises(RuntimeError, match="injected account secret failure"):
            await store.create_account(UUID(str(site["id"])), {
                "name": "Primary", "adapter": "api", "enabled": True,
                "config": {"visible": "ordinary", "token": "new-token"},
            })
        with store.database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM site_accounts").fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM capability_instances WHERE code='checkin'"
            ).fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM browser_profiles").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0
    finally:
        await store.close()


def test_secret_split_protects_nested_tokens_json_strings_and_url_userinfo():
    body_json = '{"token":"string-secret","visible":"ok"}'
    clean, secrets = DataLayerStore._split_secrets({
        "body": {"token": "nested-secret", "visible": "ok"},
        "serialized_body": body_json,
        "checkinUrl": "https://alice:password@example.test/checkin",
    })
    assert clean == {"body": {"visible": "ok"}}
    assert secrets == {
        "body.token": "nested-secret",
        "serialized_body": body_json,
        "checkinUrl": "https://alice:password@example.test/checkin",
    }


@pytest.mark.asyncio
async def test_checkin_idempotency_is_account_and_shanghai_business_date_based():
    store = MemoryStore()
    site = await store.create_site({"name": "Example", "url": "https://example.test", "status": "active"})
    account = await store.create_account(UUID(str(site["id"])), {
        # This test exercises idempotency, not browser authentication.
        "name": "Primary", "adapter": "api", "enabled": True,
    })
    service = JobService(store, EventBroker())
    due = datetime(2026, 8, 3, 16, 30, tzinfo=UTC)  # 2026-08-04 in Asia/Shanghai.
    first, created = await service.enqueue_checkin(
        UUID(str(account["id"])), scheduled_for=due, idempotency_key="caller-controlled-one",
    )
    second, created_again = await service.enqueue_checkin(
        UUID(str(account["id"])), scheduled_for=due, idempotency_key="caller-controlled-two",
    )
    assert created is True and created_again is False
    assert first.job_id == second.job_id
    stored = await store.get_job(first.job_id)
    assert stored and stored["idempotency_key"] == f"checkin:{account['id']}:2026-08-04"
    assert stored["max_attempts"] == 3


@pytest.mark.asyncio
async def test_inbox_filters_actionable_severities_and_maps_error(tmp_path):
    store = DataLayerStore(tmp_path / "events.db")
    await store.initialize()
    try:
        for severity in ("success", "info", "warning", "danger"):
            store.repository.create_event(
                event_type=f"test.{severity}", severity=severity, entity_type="system",
                title=severity.title(), detail=f"{severity} detail",
            )
        inbox, total = await store.list_events(page=1, page_size=20, inbox=True)
        assert total == 2
        assert {item["severity"] for item in inbox} == {"warning", "error"}
        errors, error_total = await store.list_events(
            page=1, page_size=20, severity="error", inbox=True,
        )
        assert error_total == 1 and errors[0]["type"] == "test.danger"

        assert await store.mark_all_events_read(inbox=True) == 2
        with store.database.connect() as connection:
            marked = {
                row[0] for row in connection.execute(
                    """SELECT e.severity FROM event_state es
                       JOIN events e ON e.id=es.event_id WHERE es.read_at IS NOT NULL"""
                )
            }
        assert marked == {"warning", "danger"}
    finally:
        await store.close()
