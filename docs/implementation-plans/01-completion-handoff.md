# Part 01 completion handoff

Completed locally on 1 October 2026. Part 01 only; parts 02–14 remain unimplemented. Source checkout: `master`, based on `b5cee59`, with uncommitted changes. No production database, credentials, server, deployment, commit or publication was modified.

## Delivered

- `app/admin.py`: `python -m app.admin backup` uses SQLite online backup, validates the copied snapshot, and publishes one private ZIP with database/manifest by atomic no-overwrite hard link. Existing files/symlinks/hard links are refused. Copy deadline, failure cleanup, integrity/foreign-key/structure checks, snapshot schema, checksum and clean-checkout revision metadata are included.
- `python -m app.admin verify`: extracts to a newly created private directory, checks the archive, preserves the extracted original, migrates only a second disposable copy, exercises representative repository reads and actual ASGI health, with upstream/Telegram disabled regardless of environment. `--integrity-only` omits migration/health. Supported complete schemas are 1/2/3; newer schemas require compatible code.
- README: command usage, filesystem/privacy limitations, actual offline cutover and rollback procedure. Backups contain private data and do not contain environment secrets or legacy CLI state.
- `tests/test_admin.py`: 21 focused cases including WAL writes, archive immutability, IDs/history/alert-state preservation, schema 1/2 migration, aliases/publication race, interruption cleanup, unsupported-schema handling, incomplete-schema rejection and connection cleanup.

## Verified

Final complete offline suite: **81 passed**, one existing upstream Starlette/AnyIO deprecation warning. Focused admin suite: **21 passed**. Actual subprocess CLI smoke covered backup, normal verify and integrity-only verify using a disposable fresh database. No server/socket/worker or external provider was started. The tests/rehearsal do not establish a production backup exists, live auth works, or a real notification was delivered.

Three independent adversarial reviewers checked filesystem safety, SQLite/restore correctness and plan/operational coverage. All reproduced an incomplete-schema false positive: a schema-version-only DB could be initialized into an empty checker and certified. Fixed with version-aware required table/column checks before migration and repository reads afterward; five direct rejection regressions cover source and checksum-valid archive inputs. The restore reviewer also found source-connection cleanup depended on GC if destination open failed; nested closing contexts and a direct regression fixed it. All three targeted confirmations found their reported issues resolved; no remaining reported P1/P2 issue.

## Commands for part 02

Use the actual Python environment with runtime dependencies installed, a deliberately selected source DB and protected parent directory. These illustrative commands do not authorize production invocation:

```bash
python -m app.admin backup --source /selected/checker.sqlite3 --destination /protected/checker-before-part02.zip
python -m app.admin verify --archive /protected/checker-before-part02.zip --work-directory /private/tmp/checker-part02-rehearsal
```

Source must already exist; destination/work directory must not. Rehearsal leaves `checker.sqlite3` (original extracted snapshot) and `restored.sqlite3` (migrated disposable copy). Never point a real worker at the rehearsal. Archives use format 1, fixed members `checker.sqlite3`/`manifest.json`, uncompressed ZIP, schema and SHA-256 agreement; they are not signed/encrypted. Local hard-link support is required. A recorded revision identifies the clean backup-tool checkout, not the source database writer; modified/untracked application code or unavailable Git yields `null`.

## Readiness and next boundary

Part 02 can start using the completed recovery gate. Revalidate checkout/changes and its plan before editing. The UI currently exists on `feat/ui-foundation` (`6f3f589`), which contains the current backend base; this CLI-only part did not switch branches or copy UI files. Integrate these part 01 changes into an implementation lineage containing the current UI before doing part 02's UI work, preserving newer user work. Do not reset a checkout or treat local completion as publication.

When part 02 introduces its next schema, extend `app/admin.py::SUPPORTED_SCHEMAS` and the version-specific required-column validation for that new format. Keep legacy 1/2/3 archive verification supported and unknown newer formats non-migrating. Rehearse the new migration using a backup of the old schema, then verify a new-format backup; do not let `Database.initialize()` fabricate missing mandatory structure before validation. Backup archive format does not need to change merely because the DB schema changes.

Restore/rollback still stops every writer and pairs database with compatible code. Pending or restored in-flight notifications need operator review because acceptance after the snapshot cannot be reconstructed exactly. Parts 02's revision/lease changes, delivery restructuring, retention, encrypted notification configuration and automated backup scheduling have not been started.

## Subsequent consolidation

On 1 October 2026 the user authorized committing and merging all completed work into `master` and retiring the old feature branches. Part 01 was preserved through UI integration and the part 02 schema-4 extension; see the [part 02 handoff](02-completion-handoff.md). Historical checkout/uncommitted-state statements above describe the original completion only. All future numbered parts and adversarial reviews reuse the single `feat/improvements` branch from latest `master`; no extra feature, agent, review or auxiliary branches. The plan/handoff Markdown files are versioned. Repository consolidation does not deploy the application.
