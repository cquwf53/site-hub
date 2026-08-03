from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import site_hub.main as main_module
from site_hub.api.security import LocalRequestSecurityMiddleware
from site_hub.config import Settings


def _client() -> tuple[TestClient, Settings]:
    settings = Settings()
    app = FastAPI()
    app.add_middleware(
        LocalRequestSecurityMiddleware, settings=settings, max_body_bytes=64
    )

    @app.get("/read")
    async def read():
        return {"ok": True}

    @app.post("/echo")
    async def echo(request: Request):
        return {"size": len(await request.body())}

    return TestClient(app, base_url="http://127.0.0.1:8686"), settings


def test_mutations_always_require_csrf_and_security_headers() -> None:
    client, settings = _client()
    assert client.post("/echo", content=b"ok").status_code == 403
    response = client.post(
        "/echo", content=b"ok", headers={"X-CSRF-Token": settings.csrf_token}
    )
    assert response.status_code == 200
    assert response.json() == {"size": 2}
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_chunked_body_limit_cannot_bypass_missing_content_length() -> None:
    client, settings = _client()
    response = client.post(
        "/echo",
        content=iter((b"x" * 40, b"y" * 40)),
        headers={"X-CSRF-Token": settings.csrf_token},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "BODY_TOO_LARGE"


def test_host_and_origin_are_restricted_to_loopback() -> None:
    client, settings = _client()
    assert client.get("/read", headers={"Host": "evil.example"}).status_code == 400
    response = client.post(
        "/echo",
        content=b"ok",
        headers={
            "X-CSRF-Token": settings.csrf_token,
            "Origin": "https://evil.example",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CROSS_ORIGIN"


def test_production_runner_disables_query_bearing_access_logs(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    main_module.run()
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8686
    assert captured["access_log"] is False
