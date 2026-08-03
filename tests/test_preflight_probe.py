from __future__ import annotations

import sqlite3

from tools.preflight_probe import nonterminal_legacy_jobs


def test_nonterminal_probe_checks_batches_and_items(tmp_path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE scheduler_batches(id INTEGER PRIMARY KEY, status TEXT);
            CREATE TABLE scheduler_batch_items(
                id INTEGER PRIMARY KEY, status TEXT, result_status TEXT
            );
            INSERT INTO scheduler_batches VALUES(1, 'running');
            INSERT INTO scheduler_batches VALUES(2, 'completed');
            INSERT INTO scheduler_batch_items VALUES(1, 'completed', 'success');
            INSERT INTO scheduler_batch_items VALUES(2, 'pending', NULL);
            """
        )

    assert nonterminal_legacy_jobs(database) == 2
