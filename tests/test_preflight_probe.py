from __future__ import annotations

import json
import sqlite3
import sys

from tools import preflight_probe
from tools.preflight_probe import (
    legacy_artifact_inventory,
    legacy_profile_inventory,
    nonterminal_legacy_jobs,
)


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


def test_artifact_inventory_hashes_only_referenced_top_level_files(tmp_path) -> None:
    database = tmp_path / "legacy.db"
    screenshots = tmp_path / "screenshots"
    screenshots.mkdir()
    (screenshots / "one.png").write_bytes(b"one")
    (screenshots / "two.png").write_bytes(b"two-two")
    (screenshots / "unreferenced.png").write_bytes(b"extra")
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE logs(id INTEGER PRIMARY KEY, screenshot TEXT);
            INSERT INTO logs VALUES(1, 'one.png');
            INSERT INTO logs VALUES(2, 'two.png');
            INSERT INTO logs VALUES(3, NULL);
            """
        )

    result = legacy_artifact_inventory(database, screenshots)

    assert result == {
        "referenced_files": 2,
        "referenced_bytes": 10,
        "missing_files": 0,
        "unsafe_references": 0,
        "duplicate_references": 0,
        "unreferenced_files": 1,
        "unreferenced_bytes": 5,
        "manifest_sha256": result["manifest_sha256"],
    }
    assert len(str(result["manifest_sha256"])) == 64


def test_artifact_inventory_reports_unsafe_missing_and_casefold_duplicates(tmp_path) -> None:
    database = tmp_path / "legacy.db"
    screenshots = tmp_path / "screenshots"
    screenshots.mkdir()
    (screenshots / "extra.png").write_bytes(b"extra")
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE logs(id INTEGER PRIMARY KEY, screenshot TEXT);
            INSERT INTO logs VALUES(1, '../escape.png');
            INSERT INTO logs VALUES(2, 'missing.png');
            INSERT INTO logs VALUES(3, 'MISSING.PNG');
            """
        )

    result = legacy_artifact_inventory(database, screenshots)

    assert result["unsafe_references"] == 1
    assert result["missing_files"] == 1
    assert result["duplicate_references"] == 1
    assert result["unreferenced_files"] == 1


def test_profile_inventory_uses_profile_copier_manifest_algorithm(tmp_path) -> None:
    source = tmp_path / "legacy-profiles"
    target = tmp_path / "sitehub-profiles"
    (source / "site-2" / "Default").mkdir(parents=True)
    (source / "site-1").mkdir()
    (source / "site-2" / "Default" / "Cookies").write_bytes(b"cookies")
    (source / "site-1" / "Preferences").write_bytes(b"preferences")

    result = legacy_profile_inventory(source, target)

    assert result["profiles"] == 2
    assert result["files"] == 2
    assert result["bytes"] == len(b"cookies") + len(b"preferences")
    assert len(str(result["sha256"])) == 64


def test_profile_probe_cli_names_source_then_target(
    tmp_path, monkeypatch, capsys,
) -> None:
    source = tmp_path / "legacy-profiles"
    target = tmp_path / "sitehub-profiles"
    (source / "site-7").mkdir(parents=True)
    (source / "site-7" / "Preferences").write_bytes(b"profile")
    monkeypatch.setattr(
        sys,
        "argv",
        ["preflight_probe.py", "legacy-profiles", str(source), str(target)],
    )

    assert preflight_probe.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["profiles"] == 1
    assert payload["files"] == 1
    assert payload["bytes"] == len(b"profile")
