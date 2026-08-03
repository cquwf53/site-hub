from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest

from site_hub.db import Database, SiteHubRepository
from site_hub.notifications import NotificationChannelService, NotificationOutboxStore
from site_hub.services.secrets import EphemeralSecretProtector, SecretService


def _environment(tmp_path):
    database = Database(tmp_path / "notification-channels.db")
    database.initialize()
    repository = SiteHubRepository(database)
    secrets = SecretService(
        database,
        EphemeralSecretProtector(b"channel-service-test-key-0123456789"),
    )
    return database, repository, secrets, NotificationChannelService(database, secrets)


def _run(awaitable):
    return asyncio.run(awaitable)


def _create(service, *, name="primary", endpoint=None, enabled=True, config=None):
    return _run(service.create({
        "name": name,
        "channel_type": "webhook",
        "endpoint": endpoint or f"https://hooks.example/{name}?token=secret-{name}",
        "enabled": enabled,
        "config": config or {},
    }))


def _event(repository, suffix, *, event_type="product.changed", severity="warning"):
    return repository.create_event(
        event_type=event_type,
        severity=severity,
        entity_type="product",
        entity_id=f"product-{suffix}",
        title=f"Event {suffix}",
        detail="Business event detail.",
        payload={"suffix": suffix},
        dedupe_key=f"channel-test-{suffix}",
    )


def test_crud_encrypts_endpoint_and_never_returns_plaintext(tmp_path):
    database, _, secrets, service = _environment(tmp_path)
    old_endpoint = "https://hooks.example/private?token=old-super-secret"
    new_endpoint = "https://hooks.example/private?token=new-super-secret"

    created = _create(
        service,
        endpoint=old_endpoint,
        config={"event_types": ["PRODUCT.CHANGED", "product.changed"], "min_severity": "WARNING"},
    )

    assert created["endpoint_configured"] is True
    assert created["config"] == {"event_types": ["product.changed"], "min_severity": "warning"}
    assert "endpoint" not in created
    assert "secret" not in json.dumps(created)
    assert _run(service.list()) == [created]
    assert _run(service.get(created["id"])) == created

    with database.connect() as conn:
        channel = conn.execute(
            "SELECT endpoint_secret_id,config_json FROM notification_channels WHERE id=?",
            (created["id"],),
        ).fetchone()
        secret = conn.execute(
            "SELECT ciphertext,entropy FROM secrets WHERE id=?",
            (channel["endpoint_secret_id"],),
        ).fetchone()
    persisted = bytes(secret["ciphertext"]) + bytes(secret["entropy"]) + channel["config_json"].encode()
    assert old_endpoint.encode() not in persisted
    assert secrets.reveal(channel["endpoint_secret_id"]).decode() == old_endpoint

    updated = _run(service.update(created["id"], {
        "name": "renamed",
        "endpoint": new_endpoint,
        "config": {"event_types": [], "min_severity": "info"},
    }))
    assert updated and updated["name"] == "renamed"
    assert "endpoint" not in updated
    assert secrets.reveal(channel["endpoint_secret_id"]).decode() == new_endpoint

    assert _run(service.delete(created["id"])) is True
    assert _run(service.delete(created["id"])) is False
    assert _run(service.get(created["id"])) is None
    with pytest.raises(KeyError):
        secrets.reveal(channel["endpoint_secret_id"])


def test_channel_filters_apply_event_type_and_minimum_severity(tmp_path):
    database, repository, _, service = _environment(tmp_path)
    accepted = _create(
        service,
        name="accepted",
        config={"event_types": ["product.changed"], "min_severity": "warning"},
    )
    _create(
        service,
        name="wrong-type",
        config={"event_types": ["checkin.failed"], "min_severity": "success"},
    )
    _create(
        service,
        name="too-low",
        config={"event_types": [], "min_severity": "danger"},
    )
    event = _event(repository, "filtered", severity="warning")

    result = NotificationOutboxStore(database).enqueue_event(event["id"])

    assert result.created == 1
    with database.connect() as conn:
        rows = conn.execute("SELECT channel_id FROM notification_outbox").fetchall()
    assert [row["channel_id"] for row in rows] == [accepted["id"]]


def test_event_and_outbox_rows_roll_back_in_the_same_transaction(tmp_path):
    database, _, _, service = _environment(tmp_path)
    _create(service)
    store = NotificationOutboxStore(database)
    event_id = str(uuid4())
    now = "2026-08-03T12:00:00.000Z"

    with pytest.raises(RuntimeError, match="force rollback"):
        with database.transaction(immediate=True) as conn:
            conn.execute(
                """INSERT INTO events(
                       id,type,severity,entity_type,title,detail,payload_json,
                       occurred_at,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (event_id, "product.changed", "warning", "product", "Rolled back", "", "{}", now, now),
            )
            queued = store.enqueue_event_locked(conn, event_id, now=now)
            assert queued.created == 1
            raise RuntimeError("force rollback")

    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE id=?", (event_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_id=?", (event_id,)).fetchone()[0] == 0


def test_test_notification_bypasses_filters_but_only_for_its_channel(tmp_path):
    database, _, _, service = _environment(tmp_path)
    target = _create(
        service,
        name="target",
        config={"event_types": ["never.matches"], "min_severity": "danger"},
    )
    other = _create(service, name="other")

    queued = _run(service.enqueue_test(target["id"]))

    assert queued and queued["status"] == "pending"
    with database.connect() as conn:
        outbox = conn.execute(
            "SELECT * FROM notification_outbox WHERE id=?", (queued["outbox_id"],)
        ).fetchone()
        event = conn.execute("SELECT * FROM events WHERE id=?", (outbox["event_id"],)).fetchone()
    assert outbox["channel_id"] == target["id"]
    assert outbox["channel_id"] != other["id"]
    assert event["type"] == "notification.test"
    assert event["entity_id"] == target["id"]


def test_disabling_channel_cancels_pending_and_leased_deliveries(tmp_path):
    database, repository, _, service = _environment(tmp_path)
    channel = _create(service)
    store = NotificationOutboxStore(database)
    first = store.enqueue_event(_event(repository, "disable-a")["id"])
    second = store.enqueue_event(_event(repository, "disable-b")["id"])
    lease = store.claim("delivery-worker")
    assert lease is not None

    updated = _run(service.update(channel["id"], {"enabled": False}))

    assert updated and updated["enabled"] is False
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT id,status,lease_owner,lease_until,last_error FROM notification_outbox ORDER BY id"
        ).fetchall()
    assert {row["id"] for row in rows} == set(first.outbox_ids + second.outbox_ids)
    assert {row["status"] for row in rows} == {"cancelled"}
    assert all(row["lease_owner"] is None and row["lease_until"] is None for row in rows)
    assert all(row["last_error"] == "notification channel disabled" for row in rows)
    assert store.mark_sent(lease.outbox_id, lease.lease_owner, response_code=204) is False
    assert store.enqueue_event(_event(repository, "after-disable")["id"]).created == 0
    assert _run(service.enqueue_test(channel["id"])) is None


def test_locked_enqueue_is_idempotent_and_returns_the_existing_row(tmp_path):
    database, repository, _, service = _environment(tmp_path)
    channel = _create(service)
    event = _event(repository, "duplicate")
    store = NotificationOutboxStore(database)

    with database.transaction(immediate=True) as conn:
        first = store.enqueue_event_locked(conn, event["id"])
        duplicate = store.enqueue_event_locked(conn, event["id"])

    assert first.created == 1 and first.existing == 0
    assert duplicate.created == 0 and duplicate.existing == 1
    assert duplicate.outbox_ids == first.outbox_ids
    assert store.idempotency_key(event["id"], channel["id"]).startswith("notification:v1:")
    with database.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_id=? AND channel_id=?",
            (event["id"], channel["id"]),
        ).fetchone()[0] == 1


def test_invalid_or_disabled_test_channel_creates_no_orphan_event(tmp_path):
    database, _, _, service = _environment(tmp_path)
    disabled = _create(service, name="disabled", enabled=False)

    assert _run(service.enqueue_test(disabled["id"])) is None
    assert _run(service.enqueue_test(str(uuid4()))) is None
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0


def test_channel_payload_rejects_ambiguous_types_and_unknown_fields(tmp_path):
    database, _, _, service = _environment(tmp_path)
    valid = {
        "name": "strict",
        "channel_type": "webhook",
        "endpoint": "https://hooks.example/strict",
    }

    with pytest.raises(ValueError, match="enabled must be a boolean"):
        _run(service.create({**valid, "enabled": "false"}))
    with pytest.raises(ValueError, match="entries must be strings"):
        _run(service.create({**valid, "config": {"event_types": [123]}}))
    with pytest.raises(ValueError, match="unsupported fields"):
        _run(service.create({**valid, "endpoint_copy": "should-not-be-stored"}))
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notification_channels").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0


def test_locked_enqueue_refuses_an_autocommit_connection(tmp_path):
    database, repository, _, service = _environment(tmp_path)
    _create(service)
    event = _event(repository, "no-transaction")
    store = NotificationOutboxStore(database)

    with database.connect() as conn:
        assert conn.in_transaction is False
        with pytest.raises(RuntimeError, match="active transaction"):
            store.enqueue_event_locked(conn, event["id"])

    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0
