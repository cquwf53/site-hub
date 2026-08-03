from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from site_hub.db import Database, SiteHubRepository
from site_hub.notifications import (
    FAILURE_EVENT_TYPE,
    NotificationOutboxExecutor,
    NotificationOutboxStore,
    NotificationPolicy,
    WebhookResponse,
)
from site_hub.services.secrets import EphemeralSecretProtector, SecretService


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class SequenceTransport:
    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.requests = []

    async def request(self, request):
        self.requests.append(request)
        if not self.outcomes:
            raise AssertionError("transport received an unexpected request")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class SleepingTransport:
    def __init__(self) -> None:
        self.requests = []

    async def request(self, request):
        self.requests.append(request)
        await asyncio.sleep(1)
        raise RuntimeError(f"request failed: {request.url}; Bearer timeout-token")


class TrackingSecrets:
    def __init__(self, service: SecretService) -> None:
        self.service = service
        self.revealed: list[str] = []

    def reveal(self, secret_id: str) -> bytes:
        self.revealed.append(secret_id)
        return self.service.reveal(secret_id)


def _environment(
    tmp_path,
    *,
    endpoint: str = "https://hooks.example/notify?token=endpoint-secret",
    channel_type: str = "webhook",
    policy: NotificationPolicy | None = None,
    transport=None,
):
    database = Database(tmp_path / "notification-executor.db")
    database.initialize()
    repository = SiteHubRepository(database)
    secrets = SecretService(
        database,
        EphemeralSecretProtector(b"executor-test-key-012345678901"),
    )
    endpoint_secret_id = secrets.store("notification.endpoint", endpoint)
    repository.create_notification_channel(
        name="primary",
        channel_type=channel_type,
        endpoint_secret_id=endpoint_secret_id,
    )
    event = repository.create_event(
        event_type="product.stock_changed",
        severity="warning",
        entity_type="product",
        entity_id="product-1",
        title="Stock changed",
        detail="Inventory changed.",
        payload={"stock": 2},
        dedupe_key="executor-business-event",
    )
    clock = MutableClock()
    store = NotificationOutboxStore(database, policy=policy, clock=clock)
    queued = store.enqueue_event(event["id"])
    tracked_secrets = TrackingSecrets(secrets)
    executor = NotificationOutboxExecutor(
        store,
        tracked_secrets,
        transport=transport,
        worker_id="test-worker",
    )
    return database, store, executor, clock, queued, tracked_secrets


def _persisted_delivery_text(database: Database) -> str:
    with database.connect() as conn:
        outbox = [tuple(row) for row in conn.execute(
            "SELECT status,response_code,last_error FROM notification_outbox"
        )]
        events = [tuple(row) for row in conn.execute(
            "SELECT type,title,detail,payload_json,dedupe_key FROM events"
        )]
    return repr((outbox, events))


def test_retry_uses_backoff_and_keeps_the_same_idempotency_key(tmp_path):
    leaked = "endpoint-secret"
    transport = SequenceTransport(
        RuntimeError(
            "https://hooks.example/notify?token=endpoint-secret Authorization: Bearer hidden"
        ),
        WebhookResponse(204),
    )
    policy = NotificationPolicy(
        max_retries=2,
        backoff_base_seconds=10,
        backoff_max_seconds=40,
        total_timeout_seconds=1,
        request_timeout_seconds=0.5,
    )
    database, store, executor, clock, queued, tracked = _environment(
        tmp_path, policy=policy, transport=transport
    )

    first = asyncio.run(executor.run_once())
    assert first is not None
    assert first.status == "pending"
    assert first.attempt == 1
    assert first.error_code == "transport_error"
    assert first.retry_at == "2026-02-03T04:05:16.000Z"
    assert asyncio.run(executor.run_once()) is None

    clock.advance(10)
    second = asyncio.run(executor.run_once())
    assert second is not None
    assert second.status == "sent"
    assert second.attempt == 2
    assert second.response_code == "204"
    assert len(transport.requests) == 2
    assert {
        request.headers["Idempotency-Key"] for request in transport.requests
    } == {store.get_outbox(queued.outbox_ids[0])["idempotency_key"]}
    assert leaked not in repr(transport.requests[0])
    assert "Inventory changed" not in repr(transport.requests[0])
    assert len(tracked.revealed) == 2
    row = store.get_outbox(queued.outbox_ids[0])
    assert row and row["status"] == "sent" and row["last_error"] is None
    assert leaked not in repr(first)
    assert leaked not in _persisted_delivery_text(database)


def test_retry_exhaustion_persists_dead_and_creates_non_recursive_event(tmp_path):
    transport = SequenceTransport(
        WebhookResponse(503, body=b"upstream token=response-secret"),
        WebhookResponse(503),
    )
    policy = NotificationPolicy(
        max_retries=1,
        backoff_base_seconds=4,
        backoff_max_seconds=4,
        total_timeout_seconds=1,
    )
    database, store, executor, clock, queued, _ = _environment(
        tmp_path, policy=policy, transport=transport
    )

    first = asyncio.run(executor.run_once())
    assert first and first.status == "pending"
    clock.advance(4)
    final = asyncio.run(executor.run_once())

    assert final is not None
    assert final.status == "dead"
    assert final.attempt == 2
    assert final.response_code == "503"
    assert final.error_code == "http_server_error"
    row = store.get_outbox(queued.outbox_ids[0])
    assert row and row["status"] == "dead"
    with database.connect() as conn:
        failures = conn.execute(
            "SELECT * FROM events WHERE type=?", (FAILURE_EVENT_TYPE,)
        ).fetchall()
    assert len(failures) == 1
    assert failures[0]["entity_id"] == queued.outbox_ids[0]
    suppressed = asyncio.run(executor.enqueue_event(failures[0]["id"]))
    assert suppressed.created == suppressed.existing == 0
    persisted = _persisted_delivery_text(database)
    assert "response-secret" not in persisted
    assert "endpoint-secret" not in persisted


def test_total_timeout_is_bounded_and_never_persists_secret_exception_text(tmp_path):
    transport = SleepingTransport()
    policy = NotificationPolicy(
        max_retries=0,
        total_timeout_seconds=0.01,
        request_timeout_seconds=1,
    )
    database, store, executor, _, queued, tracked = _environment(
        tmp_path, policy=policy, transport=transport
    )

    result = asyncio.run(executor.run_once())

    assert result is not None
    assert result.status == "dead"
    assert result.error_code == "delivery_timeout"
    assert tracked.revealed
    row = store.get_outbox(queued.outbox_ids[0])
    assert row and row["status"] == "dead"
    safe_text = repr(result) + _persisted_delivery_text(database)
    assert "endpoint-secret" not in safe_text
    assert "timeout-token" not in safe_text
    assert "hooks.example" not in safe_text


def test_non_http_endpoint_is_permanent_failure_without_transport_call(tmp_path):
    transport = SequenceTransport()
    policy = NotificationPolicy(max_retries=3, total_timeout_seconds=1)
    database, store, executor, _, queued, tracked = _environment(
        tmp_path,
        endpoint="file:///tmp/hook?token=file-secret",
        policy=policy,
        transport=transport,
    )

    result = asyncio.run(executor.run_once())

    assert result is not None
    assert result.status == "failed"
    assert result.attempt == 1
    assert result.error_code == "invalid_endpoint"
    assert transport.requests == []
    assert tracked.revealed
    row = store.get_outbox(queued.outbox_ids[0])
    assert row and row["status"] == "failed"
    assert "file-secret" not in repr(result)
    assert "file-secret" not in _persisted_delivery_text(database)


def test_redirect_is_validated_followed_and_bounded(tmp_path):
    transport = SequenceTransport(
        WebhookResponse(
            302,
            headers={"Location": "/next?token=redirect-secret"},
            body=b"redirecting",
        ),
        WebhookResponse(200, body=b"ok"),
    )
    policy = NotificationPolicy(max_redirects=1, total_timeout_seconds=1)
    _, _, executor, _, _, _ = _environment(
        tmp_path, policy=policy, transport=transport
    )

    result = asyncio.run(executor.run_once())

    assert result and result.status == "sent"
    assert len(transport.requests) == 2
    assert transport.requests[1].url.endswith("/next?token=redirect-secret")
    assert "redirect-secret" not in repr(transport.requests[1])
    assert "redirect-secret" not in repr(result)


def test_redirect_limit_and_response_body_limit_fail_safely(tmp_path):
    redirect_transport = SequenceTransport(
        WebhookResponse(
            302,
            headers={
                "Location": "https://redirect.example/x?token=redirect-limit-secret"
            },
        )
    )
    redirect_policy = NotificationPolicy(
        max_retries=0,
        max_redirects=0,
        total_timeout_seconds=1,
    )
    redirect_dir = tmp_path / "redirect"
    redirect_dir.mkdir()
    database, _, redirect_executor, _, _, _ = _environment(
        redirect_dir, policy=redirect_policy, transport=redirect_transport
    )
    redirect_result = asyncio.run(redirect_executor.run_once())
    assert redirect_result and redirect_result.status == "failed"
    assert redirect_result.error_code == "redirect_limit"
    assert "redirect-limit-secret" not in repr(redirect_result)
    assert "redirect-limit-secret" not in _persisted_delivery_text(database)

    body_transport = SequenceTransport(WebhookResponse(200, body=b"12345"))
    body_policy = NotificationPolicy(
        max_retries=0,
        response_body_limit=4,
        total_timeout_seconds=1,
    )
    body_dir = tmp_path / "body"
    body_dir.mkdir()
    _, body_store, body_executor, _, body_queued, _ = _environment(
        body_dir, policy=body_policy, transport=body_transport
    )
    body_result = asyncio.run(body_executor.run_once())
    assert body_result and body_result.status == "failed"
    assert body_result.error_code == "response_too_large"
    row = body_store.get_outbox(body_queued.outbox_ids[0])
    assert row and row["status"] == "failed"
