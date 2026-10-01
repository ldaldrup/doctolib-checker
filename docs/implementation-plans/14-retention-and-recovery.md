# 14 — Previewed retention and complete recovery

**Outcome:** History can be bounded without losing current checks, queued delivery dependencies or sent-episode dedupe; backups restore the final app/configuration model. **Effort:** medium. **Depends on:** 01 and final data models through 13. **Why last:** retention knows all live references, keys, intents and continuation states. Follow [shared execution rules](README.md).

## Scope and source entry points

Inspect final schema/repository references, 01 backup command, channel/SMTP encrypted settings, idempotency leases, quiet-hour work, run continuations and history UI. Ship operator preview/apply retention and safe export/recovery documentation. No automatic production purge, cloud uploader, deleted-job resurrection, key rotation framework or UI backup marketplace.

## Implementation order

1. Inventory actual references before designing purge. Keep running/yielded runs, current evidence, active target episode state, pending/in-flight manual intents and event-capability dependencies of live deliveries, plus pending/claimed/withheld/uncertain/action-required deliveries and referenced records. Completed manual history is not pinned forever: terminal history is normally eligible after 90 days; exact retained dependency closure takes precedence. Keep compact observed-event/routing boundary and sent-episode/channel uniqueness evidence so removing display history cannot make a sent/previously unrouted unchanged slot new. New event identity after a genuine disappearance still remains possible.
2. Add a non-destructive preview command returning cutoff, safe aggregate counts/bytes estimate and pinned reasons. Do not print job URLs/credentials. Apply requires an explicit cutoff/plan identifier and rechecks eligibility in the write transaction. Recompute current dependencies to protect work changed since preview; report skipped differences rather than deleting newly live rows. Delete in foreign-key-safe bounded batches; interruption/retry is idempotent. Avoid automatic VACUUM while processes are active.
3. Export requested job/check/delivery history with explicit schema/version and safe default redaction. Secrets are never in general export; historical booking URLs require an explicit private-data export option and protected destination. An export is not a restorable whole-application backup. Record optional terminal-idempotency-key cleanup with its 7-day guarantee, separate from alert dedupe preservation.
4. Extend 01 backup manifest/procedure to include compatible application/schema, encrypted channel/SMTP configuration and required external-key identity (fingerprint only, never key content). Document separate protected key custody and restore rehearsal. A restore without the correct key cannot send/edit secrets, but must not corrupt data; restored settings become action-required safely.
5. Rehearse restore into a disposable runtime for the current schema: jobs/selected channels/SMTP content, episode state, withheld manual-paused events, resumable checks and operation keys retain relationships. Restored live claims are reconciled by expiry, not blindly resumed under copied owner tokens. Do not deliver real notifications during verification. Document migration/rollback limits and retained personal appointment data after soft deletion.

## Direct acceptance checks

- Synthetic old terminal history purges, while pending/withheld/claimed work and reference closure survive integrity/foreign-key checks.
- Completed old manual run/intent history purges; a still-held paused-manual event retains its capability/dependencies and safe release behavior.
- Preview then new claim/change before apply is protected; interrupted apply reruns without double deletion or current-state damage.
- Purge sent display history, reconfirm same slot, and verify no resend; genuine disappear/reappear still alerts.
- Restore final schema with correct key and with absent/wrong key using fake providers; no real sends and no rewritten ciphertext. Resumable run/manual intent remains coherent.
- Export defaults exclude secrets/private URLs; explicit private export uses restrictive permissions and is clearly separate from backup.

Use one synthetic relationship-rich DB and targeted retention/restore tests, not a new data generator platform. Run existing migration/episode suite once because deletion and restoration touch shared state.

**Ship gate:** non-destructive preview is default, explicit apply is safe and the operator can recover the complete configuration. Handoff explains pinned history that may outlive 90 days; bounded history does not guarantee a fixed DB size when necessary live/dedupe records remain.

**Major risks:** deleting dedupe evidence triggers repeats; removing a result still referenced by an unsent delivery; stale preview purges new work; backups exclude the encryption key recovery path; export reveals private appointment details; restored claims impersonate a live old owner.
