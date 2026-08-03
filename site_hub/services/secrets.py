"""Encrypted secret storage with a Windows DPAPI production backend.

The production default is deliberately unavailable off Windows.  Tests must
explicitly inject :class:`EphemeralSecretProtector`; there is no plaintext fallback.
Secret-list operations expose metadata only.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Protocol
from urllib.parse import parse_qsl

from site_hub.db import Database, new_uuid, utc_now


@dataclass(frozen=True)
class ProtectedSecret:
    ciphertext: bytes
    entropy: bytes


class SecretProtector(Protocol):
    provider: str

    def protect(self, plaintext: bytes, purpose: str) -> ProtectedSecret: ...

    def unprotect(self, protected: ProtectedSecret, purpose: str) -> bytes: ...


class WindowsDPAPIProtector:
    """DPAPI CurrentUser encryption; ciphertext only works for this OS user."""

    provider = "windows-dpapi-current-user"
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.c_ulong),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows DPAPI is available only on Windows")
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32

    @classmethod
    def _blob(cls, data: bytes) -> tuple["WindowsDPAPIProtector._DATA_BLOB", ctypes.Array]:
        buffer = ctypes.create_string_buffer(data, max(1, len(data)))
        blob = cls._DATA_BLOB(
            len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        )
        return blob, buffer

    @staticmethod
    def _zero(buffer: ctypes.Array) -> None:
        ctypes.memset(ctypes.addressof(buffer), 0, ctypes.sizeof(buffer))

    def protect(self, plaintext: bytes, purpose: str) -> ProtectedSecret:
        entropy = os.urandom(32)
        source, source_buffer = self._blob(plaintext)
        entropy_blob, entropy_buffer = self._blob(entropy)
        output = self._DATA_BLOB()
        try:
            ok = self._crypt32.CryptProtectData(
                ctypes.byref(source), purpose, ctypes.byref(entropy_blob), None, None,
                self._CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
            )
            if not ok:
                raise ctypes.WinError()
            ciphertext = ctypes.string_at(output.pbData, output.cbData)
            return ProtectedSecret(ciphertext, entropy)
        finally:
            self._zero(source_buffer)
            self._zero(entropy_buffer)
            if output.pbData:
                self._kernel32.LocalFree(output.pbData)

    def unprotect(self, protected: ProtectedSecret, purpose: str) -> bytes:
        source, source_buffer = self._blob(protected.ciphertext)
        entropy_blob, entropy_buffer = self._blob(protected.entropy)
        output = self._DATA_BLOB()
        try:
            ok = self._crypt32.CryptUnprotectData(
                ctypes.byref(source), None, ctypes.byref(entropy_blob), None, None,
                self._CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
            )
            if not ok:
                raise ctypes.WinError()
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._zero(source_buffer)
            self._zero(entropy_buffer)
            if output.pbData:
                self._kernel32.LocalFree(output.pbData)


class EphemeralSecretProtector:
    """Explicit, authenticated test backend.  Never selected automatically."""

    provider = "test"

    def __init__(self, key: bytes):
        if len(key) < 16:
            raise ValueError("test secret key must be at least 16 bytes")
        self._key = bytes(key)

    def _stream(self, nonce: bytes, length: int) -> bytes:
        chunks: list[bytes] = []
        counter = 0
        while sum(map(len, chunks)) < length:
            chunks.append(hmac.new(
                self._key, b"stream\0" + nonce + counter.to_bytes(8, "big"), hashlib.sha256
            ).digest())
            counter += 1
        return b"".join(chunks)[:length]

    def protect(self, plaintext: bytes, purpose: str) -> ProtectedSecret:
        nonce = os.urandom(16)
        stream = self._stream(nonce, len(plaintext))
        encrypted = bytes(left ^ right for left, right in zip(plaintext, stream))
        body = b"TS1" + nonce + encrypted
        tag = hmac.new(
            self._key, b"tag\0" + purpose.encode("utf-8") + body, hashlib.sha256
        ).digest()
        return ProtectedSecret(body + tag, b"")

    def unprotect(self, protected: ProtectedSecret, purpose: str) -> bytes:
        value = protected.ciphertext
        if len(value) < 51 or not value.startswith(b"TS1"):
            raise ValueError("invalid protected test secret")
        body, supplied_tag = value[:-32], value[-32:]
        expected_tag = hmac.new(
            self._key, b"tag\0" + purpose.encode("utf-8") + body, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(supplied_tag, expected_tag):
            raise ValueError("protected test secret failed authentication")
        nonce, encrypted = body[3:19], body[19:]
        stream = self._stream(nonce, len(encrypted))
        return bytes(left ^ right for left, right in zip(encrypted, stream))


def get_default_protector(*, platform: str | None = None) -> SecretProtector:
    current = sys.platform if platform is None else platform
    if current != "win32":
        raise RuntimeError(
            "secret storage requires Windows DPAPI; tests must explicitly inject a test backend"
        )
    return WindowsDPAPIProtector()


class SecretService:
    def __init__(self, database: Database, protector: SecretProtector | None = None):
        self.database = database
        self.protector = protector if protector is not None else get_default_protector()
        if self.protector.provider not in {"windows-dpapi-current-user", "test"}:
            raise ValueError("unsupported secret protector; plaintext storage is forbidden")

    @contextmanager
    def _write_connection(
        self, conn: sqlite3.Connection | None,
    ) -> Iterator[sqlite3.Connection]:
        if conn is not None:
            yield conn
        else:
            with self.database.transaction() as owned:
                yield owned

    def store(
        self, purpose: str, plaintext: str | bytes, *, secret_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        raw = plaintext.encode("utf-8") if isinstance(plaintext, str) else bytes(plaintext)
        protected = self.protector.protect(raw, purpose)
        if protected.ciphertext == raw:
            raise RuntimeError("secret protector returned plaintext; refusing storage")
        identifier, now = secret_id or new_uuid(), utc_now()
        with self._write_connection(conn) as target:
            target.execute(
                """INSERT INTO secrets(
                    id,provider,purpose,ciphertext,entropy,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (identifier, self.protector.provider, purpose, protected.ciphertext,
                 protected.entropy, now),
            )
        return identifier

    def reveal(self, secret_id: str) -> bytes:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT provider,purpose,ciphertext,entropy FROM secrets WHERE id=?",
                (secret_id,),
            ).fetchone()
        if row is None:
            raise KeyError(secret_id)
        if row["provider"] != self.protector.provider:
            raise RuntimeError("configured protector cannot decrypt this secret provider")
        return self.protector.unprotect(
            ProtectedSecret(bytes(row["ciphertext"]), bytes(row["entropy"])), row["purpose"]
        )

    def rotate(
        self, secret_id: str, plaintext: str | bytes, *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        with (self.database.connect() if conn is None else _borrowed(conn)) as source:
            row = source.execute("SELECT purpose FROM secrets WHERE id=?", (secret_id,)).fetchone()
        if row is None:
            raise KeyError(secret_id)
        raw = plaintext.encode("utf-8") if isinstance(plaintext, str) else bytes(plaintext)
        protected = self.protector.protect(raw, row["purpose"])
        if protected.ciphertext == raw:
            raise RuntimeError("secret protector returned plaintext; refusing storage")
        with self._write_connection(conn) as target:
            target.execute(
                """UPDATE secrets SET provider=?,ciphertext=?,entropy=?,rotated_at=? WHERE id=?""",
                (self.protector.provider, protected.ciphertext, protected.entropy,
                 utc_now(), secret_id),
            )

    def list_metadata(self) -> list[dict[str, object]]:
        with self.database.connect() as conn:
            return [dict(row) for row in conn.execute(
                """SELECT id,provider,purpose,created_at,rotated_at,
                          length(ciphertext) AS ciphertext_bytes
                   FROM secrets ORDER BY created_at,id"""
            )]

    def delete_if_unreferenced(self, secret_id: str) -> bool:
        try:
            with self.database.transaction() as conn:
                return conn.execute("DELETE FROM secrets WHERE id=?", (secret_id,)).rowcount == 1
        except sqlite3.IntegrityError:
            return False

    def bind_account(
        self, account_id: str, name: str, secret_id: str, *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        with self._write_connection(conn) as target:
            target.execute(
                """INSERT INTO account_secrets(account_id,name,secret_id) VALUES(?,?,?)
                   ON CONFLICT(account_id,name) DO UPDATE SET secret_id=excluded.secret_id""",
                (account_id, name, secret_id),
            )

    def bind_capability(
        self, capability_id: str, name: str, secret_id: str, *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        with self._write_connection(conn) as target:
            target.execute(
                """INSERT INTO capability_secrets(capability_instance_id,name,secret_id)
                   VALUES(?,?,?) ON CONFLICT(capability_instance_id,name)
                   DO UPDATE SET secret_id=excluded.secret_id""",
                (capability_id, name, secret_id),
            )


@contextmanager
def _borrowed(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    yield conn


@dataclass(frozen=True)
class SensitiveUrl:
    url_base: str
    suffix_secret_id: str | None
    query_keys: tuple[str, ...]


def split_sensitive_url(
    url: str, service: SecretService, purpose: str, *,
    secret_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> SensitiveUrl:
    """Remove and encrypt an exact URL query/fragment suffix."""
    positions = [position for marker in ("?", "#") if (position := url.find(marker)) >= 0]
    if not positions:
        return SensitiveUrl(url, None, ())
    split_at = min(positions)
    base, suffix = url[:split_at], url[split_at:]
    query = ""
    if suffix.startswith("?"):
        query = suffix[1:].split("#", 1)[0]
    query_keys = tuple(dict.fromkeys(name for name, _ in parse_qsl(
        query, keep_blank_values=True, strict_parsing=False
    )))
    stored_secret_id = service.store(
        purpose, suffix, secret_id=secret_id, conn=conn,
    )
    return SensitiveUrl(base, stored_secret_id, query_keys)


def restore_sensitive_url(url_base: str, secret_id: str | None, service: SecretService) -> str:
    if secret_id is None:
        return url_base
    suffix = service.reveal(secret_id).decode("utf-8")
    if not suffix.startswith(("?", "#")):
        raise ValueError("stored URL suffix is invalid")
    return url_base + suffix


__all__ = [
    "ProtectedSecret",
    "SecretProtector",
    "SecretService",
    "SensitiveUrl",
    "EphemeralSecretProtector",
    "WindowsDPAPIProtector",
    "get_default_protector",
    "restore_sensitive_url",
    "split_sensitive_url",
]
