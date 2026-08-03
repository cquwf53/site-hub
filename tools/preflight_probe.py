"""Small read-only probes used by the Windows cutover preflight."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from site_hub.services.legacy_import import (  # noqa: E402
    ProfileCopier,
    profile_inventory_summary,
)


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def legacy_artifact_inventory(database: Path, source_root: Path) -> dict[str, int | str]:
    """Validate and hash only screenshot files referenced by legacy log rows."""
    root = source_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "logs" not in tables:
            raise RuntimeError("legacy database has no logs table")
        references = [
            (str(row[0]), str(row[1]).strip())
            for row in connection.execute(
                "SELECT id,screenshot FROM logs WHERE trim(coalesce(screenshot,''))!='' ORDER BY id"
            )
        ]

    seen: set[str] = set()
    referenced_names: set[str] = set()
    missing = unsafe = duplicates = referenced_bytes = 0
    valid_entries: list[tuple[str, int, str]] = []
    for _legacy_id, raw_name in references:
        candidate_name = Path(raw_name)
        normalized = raw_name.casefold()
        if (
            not raw_name
            or candidate_name.is_absolute()
            or candidate_name.name != raw_name
            or "/" in raw_name
            or "\\" in raw_name
            or candidate_name.drive
            or raw_name in {".", ".."}
        ):
            unsafe += 1
            continue
        if normalized in seen:
            duplicates += 1
            continue
        seen.add(normalized)
        referenced_names.add(normalized)
        candidate = (root / raw_name).resolve()
        if candidate.parent != root or not candidate.is_file():
            missing += 1
            continue
        size = candidate.stat().st_size
        digest = _sha256_file(candidate)
        valid_entries.append((raw_name, size, digest.lower()))
        referenced_bytes += size

    aggregate = hashlib.sha256()
    for raw_name, size, digest in sorted(valid_entries, key=lambda item: item[0]):
        aggregate.update(raw_name.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(str(size).encode("ascii"))
        aggregate.update(b"\0")
        aggregate.update(digest.encode("ascii"))
        aggregate.update(b"\n")

    source_files = [path for path in root.iterdir() if path.is_file()]
    unreferenced = [path for path in source_files if path.name.casefold() not in referenced_names]
    return {
        "referenced_files": len(references),
        "referenced_bytes": referenced_bytes,
        "missing_files": missing,
        "unsafe_references": unsafe,
        "duplicate_references": duplicates,
        "unreferenced_files": len(unreferenced),
        "unreferenced_bytes": sum(path.stat().st_size for path in unreferenced),
        "manifest_sha256": aggregate.hexdigest(),
    }


def legacy_profile_inventory(
    source_root: Path, target_root: Path,
) -> dict[str, int | str]:
    manifests = ProfileCopier(source_root, target_root).inventory()
    return profile_inventory_summary(manifests)


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    nonterminal_parser = commands.add_parser("legacy-nonterminal")
    nonterminal_parser.add_argument("database", type=Path)
    artifact_parser = commands.add_parser("legacy-artifacts")
    artifact_parser.add_argument("database", type=Path)
    artifact_parser.add_argument("source_root", type=Path)
    profile_parser = commands.add_parser("legacy-profiles")
    profile_parser.add_argument("source_root", type=Path)
    profile_parser.add_argument("target_root", type=Path)
    args = parser.parse_args()
    if args.command == "legacy-nonterminal":
        print(nonterminal_legacy_jobs(args.database))
    elif args.command == "legacy-artifacts":
        print(json.dumps(legacy_artifact_inventory(args.database, args.source_root), sort_keys=True))
    elif args.command == "legacy-profiles":
        print(json.dumps(
            legacy_profile_inventory(args.source_root, args.target_root),
            sort_keys=True,
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
