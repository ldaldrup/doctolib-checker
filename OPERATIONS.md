# Retention, history export and recovery

These commands operate on an existing local SQLite database. Run them with the
same application version as the API, worker and dispatcher. Keep administrative
access restricted. They do not contact notification providers or restore into a
live deployment. The application does not load `.env` automatically.

## Preview and apply retention

Preview defaults to terminal history older than 90 days:

```bash
python -m app.admin retention-preview --source ./data/checker.sqlite3
```

The JSON report gives a `plan_id`, explicit timezone-aware `cutoff`, eligible
counts, a logical history-byte estimate and pinned reasons. Preview deletes no
history. Plans expire after 30 days; create a new preview after expiry. The estimate is not reclaimed filesystem space; SQLite keeps free pages
for reuse.

Review the report, then copy its exact cutoff and plan ID:

```bash
python -m app.admin retention-apply --source ./data/checker.sqlite3 \
  --cutoff '<cutoff from preview>' --plan-id '<plan_id from preview>'
```

Apply rechecks each bounded write transaction. Newly live or changed rows are
skipped. After interruption, rerun the same command; already removed rows are
not removed twice. Use `--batch-size` between 1 and 1000 to adjust transaction
size. No automatic purge or `VACUUM` runs. Make a protected backup before a
production purge; restore from that backup if deleted display history is needed.

Running/yielded checks, current evidence, pending manual work and live delivery
relationships can outlive the cutoff. This includes held, uncertain and
operator-action-required deliveries. Compact observed-event/original-routing
and sent-destination evidence remains so reconfirming an unchanged slot cannot
send it again. A genuine disappearance and reappearance can create a new event.
Older completed manual history can be removed when no live dependency needs it.
Delivery age uses the newest recorded creation/send/attempt time. Historic
cancellation times were not recorded; retention cannot infer their exact age.
Necessary dedupe and live records mean retention does not guarantee a fixed
maximum database size.

Creation/channel operation keys are separate from alert dedupe. Optional
`--cleanup-expired-keys` on preview permits expired terminal keys to be removed
after their seven-day guarantee. Pending operations remain protected. Replaying
an expired key is outside the duplicate-prevention guarantee.

## Export saved history

Export defaults to all jobs and their saved target/check/delivery history:

```bash
python -m app.admin export-history --source ./data/checker.sqlite3 \
  --destination ./history.json
```

Use `--job-id` to select a job; repeat it for multiple jobs. Export streams a
consistent read snapshot to a new file with mode `0600`; it refuses overwrite.
The JSON declares format and schema versions and `restorable_backup: false`.
Export is for reading history, not restoring the application. It omits secret
fields, ciphertext, request/lease tokens, raw error payloads and booking URLs.
Names, appointment times and saved search details remain personal data.

To include current and historical booking URLs, explicitly choose private-data
export and an owner-only destination directory:

```bash
mkdir -m 700 private-history
python -m app.admin export-history --source ./data/checker.sqlite3 \
  --destination ./private-history/history.json --include-private-urls
```

Soft deletion does not erase job/target identities, personal appointment details,
retained history or necessary event evidence. Retention removes eligible display
history, not accounts/configuration or all personal data.

## Whole-application backup and key custody

Load the deployment's existing `NOTIFICATION_SECRET_KEY` into the environment
through your protected secret-management mechanism. Never put it in command-line
arguments, shell history or the database. Then create a new backup:

```bash
python -m app.admin backup --source ./data/checker.sqlite3 \
  --destination ./backup.zip
```

SQLite's online backup captures a consistent database including committed WAL
content. The archive has mode `0600` and refuses overwrite. Its manifest gives
schema compatibility, database checksum, tool-checkout revision when known,
encrypted channel/SMTP configuration counts and external-key fingerprint when
validated. A checkout revision does not identify an independently deployed
writer's build. No key content is included. If the key is absent or wrong, the
manifest reports missing/unreadable readiness instead of inventing its identity.

Back up the external key separately in protected custody. Retain the matching
application release and the deployment's external settings, such as egress
exceptions and process configuration. A database archive cannot reconstruct
those external values or provider acceptance that happened after its snapshot.

## Disposable restore rehearsal

Use a new directory and the same separately protected key:

```bash
python -m app.admin verify --archive ./backup.zip \
  --work-directory ./restore-check
```

Verification checks checksum, SQLite integrity and foreign keys, then migrates a
separate disposable copy forward and reads application health. It never starts
workers, resumes copied claims, sends notifications or contacts Doctolib. The
archive remains immutable. `--integrity-only` skips runtime migration/health.

Inspect the aggregate `notification_configuration` result. `available` means
the supplied key decrypts stored secret configuration. `missing` or `unreadable`
requires the correct external key before secret edits or sends. Ciphertext stays
unchanged; a dispatcher encountering unusable secrets records action-required
work without contacting providers. Do not replace credentials to work around a
missing backup key.

Live owner tokens stay historical until their leases expire. Compatible runtime
reconciliation must fence old owners and reclaim/resume work with fresh ownership.
Do not manually rewrite copied tokens or assume a restored claim is safe to send.
A held paused-manual event still needs its original current capability and fresh
evidence. Unknown external acceptance must never be automatically resent.

## Production restore or upgrade

Stop API, worker and dispatcher writers. Preserve the current database and key
separately. Rehearse the archive with a compatible release, verify secret readiness
and relationships, then install the recovered database at a new protected
location. Intentionally switch compatible processes/UI together. Do not resurrect
soft-deleted jobs or bypass quiet hours, manual capability or delivery uncertainty.

Schema changes are forward-only. Rollback restores the pre-upgrade database,
matching key and matching application after stopping writers; no in-place
schema downgrade. A restore cannot reconstruct later provider acceptance, so
reconcile uncertain delivery state before enabling dispatch. No backup scheduler,
cloud upload, key rotation or automatic destructive cleanup is included.
