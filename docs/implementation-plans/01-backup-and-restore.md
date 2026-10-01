# 01 — SQLite backup and disposable restore

**Outcome:** The operator can create a SQLite-consistent backup and prove it restores a working checker. **Effort:** small–medium. **Depends on:** none. **Why first:** every later schema change needs a recoverable starting point. Follow [shared execution rules](README.md).

## Scope and source entry points

Inspect `app/storage/db.py::initialize/connection`, `app/storage/repositories.py`, `README.md`, Docker entry commands and current tests. Add a small administrative command using SQLite's online backup facility, a restore verification command/procedure, and runtime documentation. No backup scheduler, cloud transfer, production invocation, deletion, retention policy or new settings page.

## Implementation order

1. Add an explicit CLI entry point for backup with source DB and new destination path. Source must already exist; avoid silently creating an empty DB. Open existing source safely, backup into a temporary destination, close it, check integrity/foreign keys, then atomically publish to a non-existing final path. Restrictive permissions and cleanup apply on failure. Refuse source=destination/overwrite, including aliases; never print row contents or secret paths.
2. Write a compact manifest with UTC creation time, schema version and application revision when known. Capture schema from the backed-up copy so manifest and data agree. Unknown revision is reported as unknown rather than invented. Do not include job URLs or notification credentials.
3. Define restore as an offline operator procedure: stop writers, preserve the current DB separately, restore to a new controlled location, start the compatible application version, inspect data/readiness, then switch intentionally. Provide a disposable verification mode that never points application processes at the real database.
4. Verify restored jobs, settings, targets, history and alert-state relationships using synthetic seeded data. Include old supported-schema backups: integrity verification is independent of migration, then a separate disposable copy exercises the current application migration. Do not mutate the archival backup during verification.

## Acceptance and boundaries

- Backup while a fake writer commits transactions in WAL mode, restore, and verify a consistent point-in-time snapshot with integrity and foreign keys. Do not assert it includes commits after the backup snapshot.
- Invalid/missing source, existing destination and an interrupted destination write leave the source and existing output unchanged.
- Restored synthetic jobs/settings/history retain IDs and delivery/episode state; application health reads succeed on the disposable migrated copy.

Use temporary directories and Python's SQLite API; no Docker/network harness is necessary. Run focused admin-command checks plus relevant existing migration tests. A production restore drill and backup schedule remain an operator rollout action.

**Ship gate:** commands are documented and runnable against disposable data; backup archives remain immutable during restore verification. Handoff includes exact entry command, scope of snapshot verification, supported schema handling and restore compatibility. Plan 02 consumes this procedure; later secret-aware additions extend it in 06/08/14.

**Major risks:** backing up only the main WAL database file; accidentally opening a wrong path as a new empty DB; overwriting a useful backup; claiming a live backup exists based on local rehearsal.
