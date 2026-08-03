from __future__ import annotations

import sys

import pytest

from site_hub.db import Database
from site_hub.services.secrets import (
    SecretService,
    EphemeralSecretProtector,
    get_default_protector,
    restore_sensitive_url,
    split_sensitive_url,
)


def _service(tmp_path) -> tuple[Database, SecretService]:
    database = Database(tmp_path / "secrets.db")
    database.initialize()
    service = SecretService(
        database, EphemeralSecretProtector(b"0123456789abcdef-test-key")
    )
    return database, service


def test_explicit_test_backend_encrypts_and_metadata_is_safe(tmp_path):
    database, service = _service(tmp_path)
    plaintext = b"fake-cookie-value"
    secret_id = service.store("test.cookie", plaintext)
    assert service.reveal(secret_id) == plaintext
    metadata = service.list_metadata()
    assert metadata[0]["id"] == secret_id
    assert "ciphertext" not in metadata[0]
    assert "entropy" not in metadata[0]
    with database.connect() as connection:
        stored = connection.execute(
            "SELECT ciphertext FROM secrets WHERE id=?", (secret_id,)
        ).fetchone()[0]
    assert bytes(stored) != plaintext
    assert plaintext not in bytes(stored)


def test_url_query_and_fragment_are_encrypted_exactly(tmp_path):
    database, service = _service(tmp_path)
    original = "https://example.test/path?a=fake-secret&empty=#frag"
    with database.transaction() as connection:
        split = split_sensitive_url(
            original, service, "test.url", conn=connection
        )
    assert split.url_base == "https://example.test/path"
    assert split.query_keys == ("a", "empty")
    assert split.suffix_secret_id is not None
    assert restore_sensitive_url(split.url_base, split.suffix_secret_id, service) == original
    assert b"fake-secret" not in (tmp_path / "secrets.db").read_bytes()


def test_non_windows_default_has_no_plaintext_fallback():
    with pytest.raises(RuntimeError):
        get_default_protector(platform="linux")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI only")
def test_windows_dpapi_current_user_round_trip(tmp_path):
    database = Database(tmp_path / "dpapi.db")
    database.initialize()
    service = SecretService(database)
    secret_id = service.store("test.dpapi", b"dpapi-round-trip")
    assert service.reveal(secret_id) == b"dpapi-round-trip"
    assert service.list_metadata()[0]["provider"] == "windows-dpapi-current-user"
