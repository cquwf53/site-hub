#!/usr/bin/env python3
"""Analyze, dry-run, or apply the VShop + AutoCheckin migration."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from site_hub.db import Database  # noqa: E402
from site_hub.services.legacy_import import (  # noqa: E402
    FROZEN_SNAPSHOT_COUNTS,
    LegacyImporter,
    ProfileCopier,
)
from site_hub.services.secrets import SecretService  # noqa: E402


RECEIPT_KIND = "sitehub-cutover-preflight"
RECEIPT_VERSION = 1
RECEIPT_MAX_LIFETIME = timedelta(minutes=60)
RECEIPT_FUTURE_SKEW = timedelta(minutes=5)
RECEIPT_BINDING_SETTING = "migration.cutover_receipt.v1"
_RECEIPT_MAX_BYTES = 64 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _receipt_time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"preflight receipt has no valid {field}")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SystemExit(f"preflight receipt has no valid {field}") from exc
    if parsed.tzinfo is None:
        raise SystemExit(f"preflight receipt {field} must include a UTC offset")
    return parsed.astimezone(UTC)


def _receipt_path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"preflight receipt has no valid {field}")
    return Path(value).expanduser().resolve()


def _check_frozen_source(path: Path, expected_sha256: object, label: str) -> None:
    if not path.is_file():
        raise SystemExit(f"{label} source database is missing: {path}")
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise SystemExit(f"preflight receipt has no valid {label} SHA-256")
    actual = _sha256_file(path)
    if not hmac.compare_digest(actual, expected):
        raise SystemExit(
            f"{label} source database changed after preflight; generate a new receipt"
        )
    wal_path = Path(str(path) + "-wal")
    if wal_path.is_file() and wal_path.stat().st_size > 0:
        raise SystemExit(
            f"{label} source database has a non-empty WAL after preflight; "
            "stop the legacy service and generate a new receipt"
        )


def _validate_receipt_document(
    receipt: object,
    *,
    vshop_db: Path,
    autocheckin_db: Path,
    target_db: Path,
    profile_source: Path | None,
    profile_target: Path | None,
    check_time: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise SystemExit("preflight receipt must contain a JSON object")
    if receipt.get("kind") != RECEIPT_KIND or receipt.get("version") != RECEIPT_VERSION:
        raise SystemExit("preflight receipt kind or version is unsupported")

    issued_at = _receipt_time(receipt.get("issued_at"), "issued_at")
    expires_at = _receipt_time(receipt.get("expires_at"), "expires_at")
    lifetime = expires_at - issued_at
    if lifetime <= timedelta(0) or lifetime > RECEIPT_MAX_LIFETIME:
        raise SystemExit("preflight receipt lifetime is invalid")
    if check_time:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if issued_at > current + RECEIPT_FUTURE_SKEW:
            raise SystemExit("preflight receipt was issued in the future")
        if current >= expires_at:
            raise SystemExit("preflight receipt has expired; run cutover preflight again")

    receipt_project = _receipt_path(receipt.get("sitehub_project"), "sitehub_project")
    if _path_key(receipt_project) != _path_key(PROJECT_ROOT):
        raise SystemExit("preflight receipt belongs to a different SiteHub project")
    receipt_target = _receipt_path(receipt.get("target_database"), "target_database")
    if _path_key(receipt_target) != _path_key(target_db):
        raise SystemExit("preflight receipt target database does not match --target-db")

    sources = receipt.get("sources")
    if not isinstance(sources, dict):
        raise SystemExit("preflight receipt has no valid sources object")
    for key, supplied_path, label in (
        ("vshop", vshop_db, "VShop"),
        ("autocheckin", autocheckin_db, "AutoCheckin"),
    ):
        source = sources.get(key)
        if not isinstance(source, dict):
            raise SystemExit(f"preflight receipt has no valid {label} source")
        receipt_path = _receipt_path(source.get("database"), f"sources.{key}.database")
        if _path_key(receipt_path) != _path_key(supplied_path):
            raise SystemExit(
                f"preflight receipt {label} path does not match the supplied database"
            )
        _check_frozen_source(supplied_path, source.get("sha256"), label)

    receipt_profile_source = _receipt_path(
        receipt.get("profile_source"), "profile_source"
    )
    if profile_source is not None and _path_key(receipt_profile_source) != _path_key(profile_source):
        raise SystemExit("preflight receipt Profile source does not match --profile-source")
    if not receipt_profile_source.is_dir():
        raise SystemExit(f"preflight receipt Profile source is missing: {receipt_profile_source}")

    receipt_profile_target = _receipt_path(
        receipt.get("profile_target"), "profile_target"
    )
    if profile_target is not None and _path_key(receipt_profile_target) != _path_key(profile_target):
        raise SystemExit("preflight receipt Profile target does not match --profile-target")
    return receipt


def _load_preflight_receipt(
    path: Path,
    **validation: Any,
) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"preflight receipt is missing: {path}")
    if path.stat().st_size > _RECEIPT_MAX_BYTES:
        raise SystemExit("preflight receipt is unexpectedly large")
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit("preflight receipt is not valid UTF-8 JSON") from exc
    return _validate_receipt_document(document, **validation)


def _claim_new_target(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb"):
            pass
    except FileExistsError as exc:
        raise SystemExit(
            "apply requires a new target database path; move the existing target aside "
            "or use --profiles-only for the supported profile-copy resume phase"
        ) from exc


def _cleanup_owned_target(path: Path) -> list[str]:
    errors: list[str] = []
    for candidate in (Path(str(path) + "-wal"), Path(str(path) + "-shm"), path):
        try:
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
    return errors


def _store_receipt_binding(database: Database, receipt: dict[str, Any]) -> None:
    payload = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,datetime('now'))
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                   updated_at=excluded.updated_at""",
            (RECEIPT_BINDING_SETTING, payload),
        )


def _load_receipt_binding(database: Database) -> dict[str, Any]:
    try:
        with database.connect() as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            succeeded = int(connection.execute(
                "SELECT count(*) FROM migration_runs WHERE status='succeeded'"
            ).fetchone()[0])
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key=?", (RECEIPT_BINDING_SETTING,)
            ).fetchone()
    except Exception as exc:
        raise SystemExit("profiles-only target is not a valid imported SiteHub database") from exc
    if integrity != "ok" or succeeded < 1:
        raise SystemExit("profiles-only target has no successful, integral legacy migration")
    if row is None:
        raise SystemExit(
            "profiles-only target has no persisted cutover receipt binding; "
            "do not copy profiles into an unverified target"
        )
    try:
        document = json.loads(str(row["value_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit("profiles-only target has an invalid receipt binding") from exc
    if not isinstance(document, dict):
        raise SystemExit("profiles-only target has an invalid receipt binding")
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate frozen VShop and AutoCheckin SQLite snapshots into Site Hub."
    )
    parser.add_argument("mode", choices=("analyze", "dry-run", "apply"))
    parser.add_argument("--vshop-db", type=Path, required=True)
    parser.add_argument("--autocheckin-db", type=Path, required=True)
    parser.add_argument("--target-db", type=Path)
    parser.add_argument(
        "--preflight-receipt", type=Path,
        help="Short-lived receipt emitted by a zero-blocker cutover preflight (full apply only).",
    )
    parser.add_argument(
        "--allow-count-drift", action="store_true",
        help="Allow source counts different from the audited frozen baseline.",
    )
    parser.add_argument("--profile-source", type=Path)
    parser.add_argument("--profile-target", type=Path)
    parser.add_argument(
        "--snapshot-root", type=Path,
        help="Controlled parent for apply snapshots (default: target sibling staging).",
    )
    parser.add_argument(
        "--keep-snapshots", action="store_true",
        help="Keep encrypted-sensitive source snapshots for an explicitly approved audit.",
    )
    parser.add_argument(
        "--include-profile-inventory", action="store_true",
        help="Hash profile files during analyze/dry-run without copying them.",
    )
    parser.add_argument(
        "--copy-profiles", action="store_true",
        help="During apply, copy profiles and verify their SHA-256 manifests.",
    )
    parser.add_argument(
        "--profiles-only", action="store_true",
        help=(
            "Resume only the independently recoverable profile-copy phase on an "
            "already imported target (requires apply and --copy-profiles)."
        ),
    )
    return parser


def _print(value: object) -> None:
    # Reports intentionally contain counts/hashes only, never source rows or secrets.
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    expected = None if args.allow_count_drift else FROZEN_SNAPSHOT_COUNTS

    # Planning does not initialize, create, or connect to the target database.
    placeholder = Database(args.target_db or PROJECT_ROOT / ".unused-target.db")
    if args.mode == "analyze":
        # A protector is not constructed because analyze performs no writes at all.
        importer = LegacyImporter(placeholder, None)  # type: ignore[arg-type]
        plan = importer.plan(args.vshop_db, args.autocheckin_db, expected_counts=expected)
        output: dict[str, object] = {"mode": "analyze", "plan": plan.to_dict()}
        if args.include_profile_inventory:
            if args.profile_source is None:
                raise SystemExit("--profile-source is required for profile inventory")
            # A throwaway sibling name is used only for path validation; it is never created.
            inventory_target = args.profile_target or args.profile_source.parent / "site-hub-profiles"
            manifests = ProfileCopier(args.profile_source, inventory_target).inventory()
            output["profiles"] = {
                "profiles": len(manifests),
                "files": sum(item.file_count for item in manifests.values()),
                "bytes": sum(item.total_bytes for item in manifests.values()),
            }
        _print(output)
        return 0

    if args.mode == "dry-run":
        # A rehearsal is a real, complete import into a disposable database.  This
        # deliberately exercises schema migrations, DPAPI, foreign keys, mappings,
        # transaction rollback and snapshot cleanup without touching the requested
        # production target path.
        planning = LegacyImporter(placeholder, None)  # type: ignore[arg-type]
        plan = planning.plan(args.vshop_db, args.autocheckin_db, expected_counts=expected)
        with tempfile.TemporaryDirectory(prefix="sitehub-migration-rehearsal-") as temp_dir:
            rehearsal_db = Path(temp_dir) / "sitehub.rehearsal.db"
            database = Database(rehearsal_db)
            database.initialize()
            importer = LegacyImporter(database, SecretService(database))
            report = importer.execute(
                args.vshop_db,
                args.autocheckin_db,
                expected_counts=expected,
            )
            with database.connect() as connection:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                foreign_key_violations = len(
                    connection.execute("PRAGMA foreign_key_check").fetchall()
                )
                legacy_mappings = int(
                    connection.execute("SELECT count(*) FROM legacy_map").fetchone()[0]
                )
                plaintext_url_suffixes = int(connection.execute(
                    """SELECT count(*) FROM site_endpoints
                       WHERE instr(url_base,'?')>0 OR instr(url_base,'#')>0"""
                ).fetchone()[0])
            if integrity != "ok" or foreign_key_violations or plaintext_url_suffixes:
                raise RuntimeError("migration rehearsal verification failed")
            output = {
                "mode": "dry-run",
                "plan": plan.to_dict(),
                "report": report.to_dict(),
                "verification": {
                    "integrity": integrity,
                    "foreign_key_violations": foreign_key_violations,
                    "legacy_mappings": legacy_mappings,
                    "plaintext_url_suffixes": plaintext_url_suffixes,
                    "disposable_target_removed": True,
                },
            }
            if args.include_profile_inventory:
                if args.profile_source is None:
                    raise SystemExit("--profile-source is required for profile inventory")
                inventory_target = (
                    args.profile_target
                    or args.profile_source.parent / "site-hub-profiles"
                )
                manifests = ProfileCopier(
                    args.profile_source, inventory_target
                ).inventory()
                output["profiles"] = {
                    "profiles": len(manifests),
                    "files": sum(item.file_count for item in manifests.values()),
                    "bytes": sum(item.total_bytes for item in manifests.values()),
                }
            _print(output)
        return 0

    if args.target_db is None:
        raise SystemExit("--target-db is required for apply")
    if args.copy_profiles and (args.profile_source is None or args.profile_target is None):
        raise SystemExit("--copy-profiles requires --profile-source and --profile-target")
    if args.profiles_only and not args.copy_profiles:
        raise SystemExit("--profiles-only requires --copy-profiles")
    if args.profiles_only and args.preflight_receipt is not None:
        raise SystemExit(
            "--profiles-only uses the receipt binding persisted by the successful "
            "business import; omit --preflight-receipt"
        )

    target_resolved = args.target_db.resolve()
    source_paths = {args.vshop_db.resolve(), args.autocheckin_db.resolve()}
    if target_resolved in source_paths:
        raise SystemExit("target database must differ from both legacy source databases")
    if args.profile_source is not None:
        profile_source = args.profile_source.resolve()
        if target_resolved == profile_source or profile_source in target_resolved.parents:
            raise SystemExit("target database must not be inside the profile source")

    if args.profiles_only:
        if not args.target_db.is_file():
            raise SystemExit("--profiles-only requires an existing imported target database")
        database = Database(args.target_db)
        binding = _load_receipt_binding(database)
        _validate_receipt_document(
            binding,
            vshop_db=args.vshop_db.resolve(),
            autocheckin_db=args.autocheckin_db.resolve(),
            target_db=target_resolved,
            profile_source=args.profile_source.resolve(),
            profile_target=args.profile_target.resolve(),
            check_time=False,
        )
        importer = LegacyImporter(database, SecretService(database))
        counts = importer.copy_autocheckin_profiles(
            args.profile_source, args.profile_target
        )
        _print({"mode": "apply", "phase": "profiles", "profile_counts": counts})
        return 0

    if args.preflight_receipt is None:
        raise SystemExit(
            "full apply requires --preflight-receipt from a zero-blocker "
            "cutover-preflight.ps1 run"
        )

    # Validate the short-lived receipt and sources before the first target write.
    receipt = _load_preflight_receipt(
        args.preflight_receipt.resolve(),
        vshop_db=args.vshop_db.resolve(),
        autocheckin_db=args.autocheckin_db.resolve(),
        target_db=target_resolved,
        profile_source=args.profile_source.resolve() if args.profile_source else None,
        profile_target=args.profile_target.resolve() if args.profile_target else None,
        check_time=True,
    )
    planning = LegacyImporter(placeholder, None)  # type: ignore[arg-type]
    plan = planning.plan(args.vshop_db, args.autocheckin_db, expected_counts=expected)

    # Formal apply is intentionally a one-shot import into a fresh database.
    # Refuse an existing file even if it happens to contain no legacy_map rows;
    # this prevents a stale preflight or manual command from merging into live data.
    if args.target_db.exists():
        raise SystemExit(
            "apply requires a new target database path; move the existing target aside "
            "or use --profiles-only for the supported profile-copy resume phase"
        )

    database = Database(args.target_db)
    target_claimed = False
    recoverable_target = False
    try:
        # Exclusive creation closes the exists/open race.  From this point until
        # the receipt binding is persisted, every failure removes only the three
        # exact SQLite files owned by this invocation.
        _claim_new_target(args.target_db)
        target_claimed = True
        database.initialize()
        importer = LegacyImporter(database, SecretService(database))
        report = importer.execute(
            args.vshop_db,
            args.autocheckin_db,
            expected_counts=expected,
            snapshot_directory=(
                args.snapshot_root
                if args.snapshot_root is not None
                else args.target_db.parent / "snapshots" if args.keep_snapshots else None
            ),
            keep_snapshots=args.keep_snapshots,
        )

        # Detect a service restart or source mutation during snapshot/import.  A
        # changed source must not leave a seemingly accepted production target.
        _validate_receipt_document(
            receipt,
            vshop_db=args.vshop_db.resolve(),
            autocheckin_db=args.autocheckin_db.resolve(),
            target_db=target_resolved,
            profile_source=args.profile_source.resolve() if args.profile_source else None,
            profile_target=args.profile_target.resolve() if args.profile_target else None,
            check_time=True,
        )
        _store_receipt_binding(database, receipt)
        recoverable_target = True

        # Profile copy is deliberately a second, independently resumable phase.
        # Once the successful import and binding exist, a Profile error preserves
        # the target so --profiles-only can continue from the same frozen source.
        if args.copy_profiles:
            report.profile_counts = importer.copy_autocheckin_profiles(
                args.profile_source, args.profile_target
            )
    except BaseException as exc:
        if target_claimed and not recoverable_target:
            cleanup_errors = _cleanup_owned_target(args.target_db)
            if cleanup_errors and hasattr(exc, "add_note"):
                exc.add_note(
                    "target cleanup also failed: " + "; ".join(cleanup_errors)
                )
        raise
    _print({"mode": "apply", "plan": plan.to_dict(), "report": report.to_dict()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
