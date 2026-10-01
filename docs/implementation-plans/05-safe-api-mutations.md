# 05 — Idempotent creates and conflict-safe settings/jobs

**Outcome:** Lost create responses do not duplicate jobs, concurrent edits do not silently overwrite, and unsupported fields are rejected. **Effort:** medium. **Depends on:** 02/04. **Why fifth:** establish reliable save contracts before channel/SMTP Settings expand. Follow [shared execution rules](README.md).

## Scope and source entry points

Inspect schemas/routes/job services/repositories and UI `api.js/main.js` including current debounced Settings autosave. Add strict extra-field validation, expected edit versions for updates, and persisted create idempotency. Preserve the current autosave UX for existing polling settings; later credential forms use explicit Save. No general API rewrite, cursor pagination, automatic retry of every mutation or new auth product.

## Implementation order

1. Reject unsupported request fields with normal validation errors. Use 02's edit_version for config mutations; add settings edit version. Return version in reads/writes and require expected_version on updates. Validate version again within the transaction that applies the write. Background scheduling/heartbeat must not spuriously change configuration versions.
2. Give create requests a bounded `Idempotency-Key` header, normalized request fingerprint and durable operation state. Atomically reserve the key with owner token/generation and lease before metadata work; concurrent same-key requests return in-progress/replay semantics instead of resolving/creating twice. In the transaction inserting the final job and completing the operation, verify the current unexpired reservation owner; a late/replaced resolver cannot insert a job or overwrite the outcome and returns authoritative operation state. Same key/different fingerprint conflicts; lost response/same key returns the existing result. Reserve and recover abandoned in-progress operations without exposing credentials. Successful key retention default 7 days; replay after retention is explicitly outside the guarantee.
3. Canonicalize omitted defaults once for a reserved operation so a later default change cannot reinterpret a retry. Unsupported fields/null handling/date normalization participate consistently in validation. Retryable upstream failure must not leave an unbounded locked operation; bounded reservation lease and safe failure/retry handling are explicit.
4. UI generates one key per create draft attempt, retains it for uncertain replay, and creates a new key only for a genuinely new operation. Never automatically resubmit an unknown create with a new key. Version conflicts retain the user's draft and offer refetch/reconcile; no blind overwrite button by default.
5. Update Settings autosave to serialize versions while preserving newer local edits during an older response. External conflict shows a useful notice and requires reconciliation, not a retry loop. New request contracts need the matching UI release; document maintenance compatibility rather than silently accepting obsolete destructive writes.

## Direct acceptance checks

- Simulated lost create response/replayed same key produces one job; concurrent same-key reservation does not create two; different body conflicts.
- Change server defaults between reservation/replay: canonical result stays the same. Recover an abandoned reservation with fake time; bounded key expiry is documented.
- Block original resolver A → expire/reclaim by B → B completes → release A: exactly one job, and A cannot overwrite B's stored result.
- Two editors with one version: first save succeeds, second conflicts and does not overwrite; worker scheduling does not cause false edit conflicts.
- Unknown Settings field returns validation error; existing valid fields/date-null transitions still work.
- Browser flow covers uncertain create and an autosave conflict while newer draft text remains intact. No secret-bearing browser persistence.

Use current backend fakes/disposable UI harness. Run focused concurrency/save flows, then offline suite once because shared schemas/repository behavior changed. No bespoke distributed test framework.

**Ship gate:** end-to-end client and API use the same keys/version semantics. Handoff includes expiry/recovery constants and save helpers for 06 onward; callers must not rely on an ignored expected_version field.

**Major risks:** reserving idempotency only after expensive work; keys lost after timeout; replay recomputing changing defaults; autosave overwriting a newer draft; confusing edit_version with search_revision.
