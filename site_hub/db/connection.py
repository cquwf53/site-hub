"""SQLite connection and transaction primitives for Site Hub."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class Database:
    """Small connection factory with explicit, rollback-safe transactions.

    Connections are deliberately short lived.  This keeps the class safe to use
    from worker threads and avoids sharing sqlite3 connections across asyncio
    tasks.  Async callers should invoke repository methods via ``asyncio.to_thread``.
    """

    def __init__(self, path: str | Path, *, readonly: bool = False, timeout: float = 5.0):
        self.path = Path(path).expanduser().resolve()
        self.readonly = readonly
        self.timeout = timeout

    def initialize(self) -> None:
        """Create/update the schema.  Read-only databases cannot be initialized."""
        if self.readonly:
            raise RuntimeError("cannot initialize a read-only database")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        from .schema import apply_migrations

        apply_migrations(self)

    def close(self) -> None:
        """Compatibility no-op: this factory does not retain open connections."""

    def _open(self) -> sqlite3.Connection:
        if self.readonly:
            target = self.path.as_uri() + "?mode=ro"
            conn = sqlite3.connect(target, uri=True, timeout=self.timeout, isolation_level=None)
        else:
            conn = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        if not self.readonly:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield an autocommit connection for read-only or explicit SQL work."""
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Yield a transaction and guarantee commit or rollback.

        ``BEGIN IMMEDIATE`` is used for queue claims and migrations where the
        read/modify/write sequence must exclude another writer.
        """
        if self.readonly:
            raise RuntimeError("cannot start a write transaction on a read-only database")
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
