"""Small read-only probes used by the Windows cutover preflight."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


ACTIVE_LEGACY_STATUSES = (
    "pending",
    "planned",
    "queued",
    "waiting",
    "running",
    "leased",
    "processing",
    "started",
)


def nonterminal_legacy_jobs(database: Path) -> int:
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        placeholders = ",".join("?" for _ in ACTIVE_LEGACY_STATUSES)
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        active = 0
        if "scheduler_batch_items" in tables:
            active += int(connection.execute(
                f"""SELECT COUNT(*) FROM scheduler_batch_items
                    WHERE lower(coalesce(status,'')) IN ({placeholders})
                       OR lower(coalesce(result_status,'')) IN ({placeholders})""",
                (*ACTIVE_LEGACY_STATUSES, *ACTIVE_LEGACY_STATUSES),
            ).fetchone()[0])
        if "scheduler_batches" in tables:
            active += int(connection.execute(
                f"""SELECT COUNT(*) FROM scheduler_batches
                    WHERE lower(coalesce(status,'')) IN ({placeholders})""",
                ACTIVE_LEGACY_STATUSES,
            ).fetchone()[0])
        return active


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("legacy-nonterminal",))
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    if args.command == "legacy-nonterminal":
        print(nonterminal_legacy_jobs(args.database))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
