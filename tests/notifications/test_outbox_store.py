from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from site_hub.db import Database, SiteHubRepository
from site_hub.notifications import (
    FAILURE_EVENT_TYPE,
    NotificationOutboxStore,
    NotificationPolicy,
)
from site_hub.services.secrets import EphemeralSecretProtector, SecretService


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def _database(tmp_path):
    database = Database(tmp_path / "notification-store.db")
    database.initialize()
    repository = SiteHubRepository(database)
    secrets = SecretService(
        database,
        EphemeralSecretProtector(b"notification-test-key-0123456789"),
    )
    return database, repository, secrets


def _event(repository: SiteHubRepository, suffix: str = "one"):
    return repository.create_event(
        event_type="product.changed",
        severity="warning",
        entity_type="product",
        entity_id=f"product-{suffix}",
        title=f"Price changed {suffix}",
        detail="A monitored price changed.",
        payload={"delta": 2},
        dedupe_key=f"business-event-{suffix}",
    )


def _channel(repository, secrets, name, *, enabled=True):
    secret_id = secrets.store(
        "notification.endpoint",
        f"https://hooks.example/{name}?token=secret-{name}",
    )
    return repository.create_notification_channel(
        name=name,
        channel_type="webhook",
        endpoint_secret_id=secret_id,
        enabled=enabled,
    )


def test_enabled_channel_fanout_is_idempotent_and_suppresses_failure_events(tmp_path):
    database, repository, secrets = _database(tmp_path)
    enabled_a = _channel(repository, secrets, "enabled-a")
    enabled_b = _channel(repository, secrets, "enabled-b")
    _channel(repository, secrets, "disabled", enabled=False)
    event = _event(repository)
    store = NotificationOutboxStore(database)

    first = store.enqueue_event(event["id"])
    second = store.enqueue_event(event["id"])

    assert first.created == 2
    assert first.existing == 0
    assert second.created == 0
    assert second.existing == 2
    assert set(first.outbox_ids) == set(second.outbox_ids)
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT channel_id,idempotency_key FROM notification_outbox ORDER BY channel_id"
        ).fetchall()
    assert {row["channel_id"] for row in rows} == {
        enabled_a["id"],
        enabled_b["id"],
    }
    assert len({row["idempotency_key"] for row in rows}) == 2

    failure = repository.create_event(
        event_type=FAILURE_EVENT_TYPE,
        severity="danger",
        entity_type="notification_outbox",
        title="Notification delivery failed",
        dedupe_key="synthetic-notification-failure",
    )
    suppressed = store.enqueue_event(failure["id"])
    assert suppressed.created == suppressed.existing == 0
    with database.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0] == 2


def test_claim_is_atomic_across_workers(tmp_path):
    database, repository, secrets = _database(tmp_path)
    _channel(repository, secrets, "atomic")
    event = _event(repository, "atomic")
    clock = MutableClock()
    policy = NotificationPolicy(backoff_base_seconds=0, backoff_max_seconds=0)
    store_a = NotificationOutboxStore(database, policy=policy, clock=clock)
    store_b = NotificationOutboxStore(database, policy=policy, clock=clock)
    queued = store_a.enqueue_event(event["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = list(
            pool.map(
                lambda item: item[0].claim(item[1]),
                ((store_a, "worker-a"), (store_b, "worker-b")),
            )
        )

    claimed = [lease for lease in leases if lease is not None]
    assert len(claimed) == 1
    assert claimed[0].outbox_id == queued.outbox_ids[0]
    assert claimed[0].attempt == 1
    row = store_a.get_outbox(queued.outbox_ids[0])
    assert row and row["status"] == "leased"


def test_expired_lease_recovers_with_backoff_and_rejects_stale_completion(tmp_path):
    database, repository, secrets = _database(tmp_path)
    _channel(repository, secrets, "recovery")
    event = _event(repository, "recovery")
    clock = MutableClock()
    policy = NotificationPolicy(
        lease_seconds=5,
        max_retries=2,
        backoff_base_seconds=10,
        backoff_max_seconds=40,
    )
    store = NotificationOutboxStore(database, policy=policy, clock=clock)
    queued = store.enqueue_event(event["id"])
    original = store.claim("worker-original")
    assert original is not None

    clock.advance(6)
    recovered = store.recover_expired()
    assert recovered.retried == 1
    assert recovered.dead == 0
    assert store.claim("worker-early") is None
    row = store.get_outbox(queued.outbox_ids[0])
    assert row and row["status"] == "pending"
    assert row["last_error"] == "lease_expired: notification delivery lease expired"

    clock.advance(10)
    replacement = store.claim("worker-replacement")
    assert replacement is not None
    assert replacement.attempt == 2
    assert replacement.lease_owner != original.lease_owner
    assert store.mark_sent(
        original.outbox_id, original.lease_owner, response_code=204
    ) is False
    assert store.mark_sent(
        replacement.outbox_id, replacement.lease_owner, response_code=204
    ) is True
    row = store.get_outbox(queued.outbox_ids[0])
    assert row and row["status"] == "sent" and row["attempt"] == 2


def test_final_expired_lease_becomes_dead_and_creates_one_actionable_event(tmp_path):
    database, repository, secrets = _database(tmp_path)
    _channel(repository, secrets, "expired-final")
    event = _event(repository, "expired-final")
    clock = MutableClock()
    policy = NotificationPolicy(
        lease_seconds=2,
        max_retries=0,
        backoff_base_seconds=0,
        backoff_max_seconds=0,
    )
    store = NotificationOutboxStore(database, policy=policy, clock=clock)
    queued = store.enqueue_event(event["id"])
    assert store.claim("worker-crashed") is not None

    clock.advance(3)
    recovered = store.recover_expired()

    assert recovered.dead == 1
    row = store.get_outbox(queued.outbox_ids[0])
    assert row and row["status"] == "dead"
    with database.connect() as conn:
        failures = conn.execute(
            "SELECT * FROM events WHERE type=?", (FAILURE_EVENT_TYPE,)
        ).fetchall()
    assert len(failures) == 1
    assert failures[0]["entity_type"] == "notification_outbox"
    assert failures[0]["entity_id"] == queued.outbox_ids[0]
    assert store.enqueue_event(failures[0]["id"]).created == 0

    # Recovery is idempotent and cannot create duplicate failure events.
    again = store.recover_expired()
    assert again.dead == 0
    with database.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE type=?", (FAILURE_EVENT_TYPE,)
        ).fetchone()[0] == 1
