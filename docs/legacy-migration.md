# Legacy database migration

`tools/migrate_legacy.py` imports the audited VShop and AutoCheckin SQLite
databases into a new Site Hub database. It never edits either source database.

## Safety model

- `analyze` is completely target-write-free. `dry-run` performs the same full
  snapshot/import/DPAPI/foreign-key path as `apply`, but only inside a disposable
  temporary database that is removed before the command returns. It never opens
  or initializes the requested production target.
- `apply` first uses SQLite Backup API to create consistent snapshots under the
  target database's sibling `.migration-staging/` directory. It validates and
  imports only those snapshots, records their SHA-256 aggregate in
  `migration_runs`, and removes the snapshots plus WAL/SHM sidecars afterward.
- `--keep-snapshots` is opt-in because snapshots contain the complete legacy
  data. When enabled, the default destination is a target-sibling `snapshots/`
  directory; protect and delete it as sensitive data.
- Target paths equal to either source DB, or inside the browser-profile source,
  are rejected before target initialization.
- Normal `apply` requires a database path that does not yet exist, and the
  importer independently rejects any target containing business rows. Supported asset-only resumes against an existing imported database are
  `--profiles-only` and `--artifacts-only`; these flags are mutually exclusive.
- A full `apply` also requires `--preflight-receipt`. The receipt is emitted only
  when `cutover-preflight.ps1` has zero blockers, expires within 60 minutes, and
  binds the absolute source paths and SHA-256 values, SiteHub target database,
  Profile and screenshot source/target paths and manifests, and SiteHub project path. Apply verifies it immediately
  before its first target write and again after the database snapshot/import.
- The CLI claims a new target with exclusive file creation. If initialization or
  business import fails, it removes only the target DB and its exact `-wal`/`-shm`
  sidecars created by that invocation, so the same receipt can be retried while
  it remains valid. Once a successful business import and receipt binding have
  been committed, a Profile-copy failure preserves the database for the
  supported `--profiles-only` recovery phase.
- URL query strings and fragments, credentials, cookies, tokens, and webhook
  endpoints are encrypted. Production encryption is Windows DPAPI CurrentUser;
  there is no plaintext or non-Windows production fallback.
- Every source entity uses a deterministic UUID and an auditable `legacy_map`
  row. Same-domain legacy sites remain separate business entries.
- Imported sites are placed under `导入数据/VShop/<legacy kind>` and
  `导入数据/AutoCheckin`; the VShop `kind` value is preserved verbatim as the
  final folder name.
- Legacy site `kind` values are also mapped to deterministic controlled website
  types. Known `shop`, `checkin`, `tool`, and `bookmark` values use the built-in
  types; an unknown value receives a deterministic type UUID instead of being
  discarded.
- Each imported run/job stores its untouched legacy status alongside the mapped
  SiteHub status. No semantic meaning is inferred from legacy message text.

The frozen baseline is 29 VShop sites plus 9 AutoCheckin sites (38 total), 138
products, 37 product categories, 8 tags, 139 product sync runs, 95 check-in
runs, 12 scheduler batches, and 86 scheduler jobs. Count drift aborts unless
`--allow-count-drift` is explicitly supplied.

## Commands

```powershell
python tools\migrate_legacy.py analyze `
  --vshop-db C:\path\to\vshop.db `
  --autocheckin-db C:\path\to\checkin.db

python tools\migrate_legacy.py dry-run `
  --vshop-db C:\path\to\vshop.db `
  --autocheckin-db C:\path\to\checkin.db `
  --target-db C:\path\to\site-hub.db

python tools\migrate_legacy.py apply `
  --vshop-db C:\path\to\vshop.db `
  --autocheckin-db C:\path\to\checkin.db `
  --target-db C:\path\to\site-hub.db `
  --preflight-receipt C:\path\to\cutover-receipt.json
```

To inventory profiles during a non-writing run, add
`--include-profile-inventory --profile-source C:\path\to\browser-profiles`.
To copy and verify profiles during apply, use:

```powershell
python tools\migrate_legacy.py apply `
  --vshop-db C:\path\to\vshop.db `
  --autocheckin-db C:\path\to\checkin.db `
  --target-db C:\path\to\site-hub.db `
  --preflight-receipt C:\path\to\cutover-receipt.json `
  --copy-profiles `
  --profile-source C:\path\to\browser-profiles `
  --profile-target C:\path\to\site-hub-profiles
```

Profile copy excludes active Chromium singleton/DevTools lock files, copies to
an account-UUID directory through a staging directory, computes per-file and
aggregate SHA-256 manifests, verifies the destination, then atomically renames
it and records the manifest in `browser_profiles`.

Business data and profile copying are separate recoverable phases. If a profile
copy is interrupted after the database import, rerun only that phase without
re-importing business rows:

```powershell
python tools\migrate_legacy.py apply `
  --vshop-db C:\path\to\vshop.db `
  --autocheckin-db C:\path\to\checkin.db `
  --target-db C:\path\to\site-hub.db `
  --copy-profiles --profiles-only `
  --profile-source C:\path\to\browser-profiles `
  --profile-target C:\path\to\site-hub-profiles
```

`--profiles-only` intentionally does not accept a new short-lived receipt. It
loads the receipt binding persisted in the successfully imported target and
revalidates the current source database paths/hashes plus Profile source/target
paths against that binding. Receipt expiry is ignored only for this recovery
phase. The Profile directory set must exactly match all imported AutoCheckin
accounts; missing or unexpected `site-*` directories fail without reporting a
partial migration as successful.

## Verification after apply

The apply report contains counts and snapshot hashes only. It never prints
source rows or secret values. Verify the target with:

```sql
PRAGMA integrity_check;
PRAGMA foreign_key_check;
SELECT counts_json, source_db_sha256, status FROM migration_runs;
```

Expected results are `ok`, zero foreign-key rows, and a `succeeded` migration
run. Imported schedules are intentionally paused for operator verification.

## Screenshot migration and recovery

Business import creates screenshot references; files and verified metadata are
copied only with `--copy-artifacts`. Include these options in a full cutover:

```powershell
python tools\migrate_legacy.py apply `
  --vshop-db C:\path\to\vshop.db `
  --autocheckin-db C:\path\to\checkin.db `
  --target-db C:\path\to\site-hub.db `
  --preflight-receipt C:\path\to\cutover-receipt.json `
  --copy-profiles --profile-source C:\path\to\browser-profiles `
  --profile-target C:\path\to\site-hub\browser-profiles `
  --copy-artifacts --artifact-source C:\path\to\screenshots `
  --artifact-target C:\path\to\site-hub\artifacts
```

For screenshot-only recovery, use the same source/target arguments with
`--copy-artifacts --artifacts-only`, omit `--preflight-receipt` and the Profile
copy options. The imported database's persisted binding authorizes only those
frozen input hashes and paths. Existing copied files are verified before being
reused. Changed source files are rejected; do not replace the frozen source with
new data to force recovery to pass. Verify at least one historical screenshot
through the artifact API after the migration.
