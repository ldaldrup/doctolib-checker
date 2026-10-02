# Part 05 completion handoff

Completed locally on 2 October 2026, grounded in the [part 04 handoff](04-completion-handoff.md), [part 05 plan](05-safe-api-mutations.md) and shared execution contract. All implementation and adversarial reviews reused **`feat/improvements`**, based on consolidated local `master` at `7c195d5`. Completed work was committed on that branch, fast-forwarded into local `master`, and the checkout returned to the same feature branch. No extra branch, remote push, production migration or deployment. Parts 06–14 remain plans.

## Delivered outcome and request contract

Lost create responses can be replayed without duplicate jobs. Concurrent configuration saves cannot silently overwrite another editor. Unsupported request fields and invalid null/version values are rejected. The matching UI preserves drafts and reconciles conflicts.

| Mutation | Required contract |
| --- | --- |
| `POST /api/v1/jobs` | `Idempotency-Key`: 1–128 ASCII letters/digits or `.`, `_`, `:`, `-`; unchanged validated request on replay |
| Job PATCH | Positive strict integer `expected_version`, alongside supported changed fields |
| Settings PUT | Positive strict integer `expected_version`, alongside supported changed fields |
| Pause/resume POST and job DELETE | JSON body containing positive strict integer `expected_version` |
| Check now | Existing durable manual-intent contract; no configuration version required |

Reads/writes expose `edit_version`. A stale version returns **409** with safe `version_conflict` and current version, before state-dependent validation and again inside the write transaction. Missing versions/keys, unsupported fields and invalid input return **422**. Error validation omits Pydantic raw input/context so private values are not reflected. Dates retain valid custom-to-first-available null transitions. Background scheduling/check intents/heartbeats do not spuriously advance configuration versions; actual status changes, including repeated pause/Stop check capability revocation, do.

New creation returns **201**. Completed replay returns **200** for the original job ID without metadata work. In-progress or lost-owner state returns **202**, `Retry-After: 2`, and `status: in_progress`; clients retain and explicitly retry the same operation. A different validated request for an existing key returns **409 idempotency_conflict**. Completed replay returns the current original resource, including subsequent edits/deletion, rather than an immutable initial JSON snapshot. It does not recreate deleted jobs.

## Persistence, ownership and constants

Schema **7** adds `settings.edit_version` and `create_operations`:

`key`, `fingerprint`, `canonical_values`, `state`, `owner_token`, `generation`, `lease_until`, `job_id`, `error_code`, `retryable`, `created_at`, `expires_at`.

The fingerprint hashes normalized, validated supplied fields; omitted server defaults remain omitted in the fingerprint. Canonical values, including defaults, are frozen once in the first reservation. Retries/reclaimed owners reuse those values even after defaults change. Keys/failure responses never expose ownership tokens or stored private input.

- **120-second reservation lease**; **30-second renewal heartbeat** while one target's metadata work is active.
- **300-second per-target ownership budget**. Deadline failure becomes retryable `metadata_unavailable` and releases ownership. Foreground work already waiting in a gate/transport is not forcibly cancelled; its next guard/final write is fenced. The heartbeat stops on completion, failure, ownership loss or deadline; exit joins it for at most one second, and a SQLite lock wait remains bounded by the existing database timeout.
- **7-day successful retention from completion**. Pending/failed operations expire seven days from first reservation. Expired keys are cleaned when reserving another operation; replay after expiry is outside duplicate-prevention guarantees.

`reserve_create`, `renew_create`, `fail_create` and `create_job(..., operation=...)` check owner token, generation, current lease and retention deadline under transactional ownership. Reservation precedes metadata work. Same-key concurrent requests receive pending/replay state. Final job insertion and operation completion share one transaction, with a fence both before and after insertion. Late resolver A cannot insert or change the outcome after resolver B reclaims/completes.

`MetadataLease` uses a private Doctolib client copy with request guards around the shared gate and after resolution; it does not mutate the shared client's callback. Global spacing remains enforced. Sanitized retryable upstream failures return **502**, release the lease and retain frozen defaults; deterministic invalid configuration returns persistent nonretryable **422**. A crash leaves only the renewable lease, which must expire before reclamation. No resume or notification-policy behavior from part 04 was relaxed.

## UI save and reconciliation behavior

A create draft attempt owns one UUID and immutable submitted payload in memory. Uncertain responses and 202 retain both for explicit saved-request replay; no similar-job heuristic or automatic new-key retry. Editor switching is blocked while creation is unresolved, and replay explicitly chooses POST independently of editor state. A definitive nonretryable rejection preserves the draft/error and permits a corrected operation with a fresh key. Leaving an unresolved create prompts the browser's unsaved-work warning. No local/session browser storage was introduced.

Job edits use the version read when opening the editor. A conflict preserves the draft; refetch failure remains blocked until Fetch latest succeeds. Three-way reconciliation adopts current values for untouched fields and requires choices for conflicting edits. Delete confirmation pins the displayed version rather than deleting a subsequently changed job with a newer incidental read.

Settings autosave remains debounced and serial. Older responses update server versions without replacing newer draft text. External conflicts stop the save loop, including when refetch fails. Fetch/reconcile offers explicit per-field choices before saving against the newly read version. New credential forms in part 06 use explicit Save and reuse these contracts.

## Verification and adversarial fixes

- **211 backend tests passed**, independently repeated by the final adversarial reviewer, with Python 3.12 and pinned `curl_cffi==0.16.1`; one dependency deprecation warning.
- **16 raw TestClient contract tests** exercise missing keys/versions, replay, changed-body conflict, same-key concurrency, blocked resolver expiry/reclaim and late completion, frozen defaults, retryable failure, strict extras/null/version handling, date transitions, worker completion without false conflicts and retention expiry. Raw tests deliberately bypass the legacy journey client that supplies current keys/versions for older behavioral tests.
- Four additional privacy/race/recovery tests prove stale metadata edits cannot alter targets or queue work, validation does not echo private inputs, completed operation replay survives restore without metadata, Settings versions survive restore, and backup rejects missing reservation-generation structure. Two ownership tests exercise renewal across a simulated request-gate wait longer than the original lease, and bounded deadline/reclamation without old-owner resurrection.
- Actual `python -m app.admin backup`/`verify` subprocesses passed **6→7** and **7→7** on disposable databases: integrity, foreign keys and API health all okay. Legacy fixtures honestly remove new schema fields; migration initializes historical Settings versions to 1 while preserving named configuration values independently of physical SQLite column order.
- **27 native in-app-browser contracts passed**, confirmed again by root after the final harness fix. Actual iframe journeys cover lost response → same-key/payload replay → one job, prevention of unrelated Edit during unresolved creation, pending → definitive rejection → corrected new key, external Settings conflict while newer draft text survives, refetch failure blocking, explicit per-field reconciliation, and job three-way merge. Prior manual-check/history/refresh contracts remain green. The deterministic worker reset uses `Database.connection()` and its response success is asserted.
- Bounded **real HTTP validation** used a loopback Uvicorn server, actual requests, the two previously supplied real Doctolib targets, supplied Telegram credentials and a disposable local database. Same-key replay returned each original ID with **zero additional metadata calls**, retained its first defaults after a Settings change, and rejected changed body/stale job and Settings writes with 409. Unsupported Settings input returned sanitized 422. Exactly **two jobs** existed. Their paused manual checks returned `no_availability` and `available` respectively; **one Telegram event was sent in one attempt**. Both remained paused, with no recurring work claimed. Six shared Doctolib request turns were reserved. No booking action occurred. This is point-in-time provider evidence, not proof of production deployment.
- Real credentials and identifying URL prefixes had **zero matches** in tracked/untracked application, test and documentation source. Live database, runner, browser tabs, fixture directories and loopback test servers were removed/stopped.

Adversarial findings fixed: unresolved create could become PATCH to another job; terminal rejected replay could lock the draft; stale date validation could return 422 instead of conflict; a long gate wait could expire otherwise valid metadata ownership; failed conflict refetch needed conservative save blocking; and a test-only worker reset used a nonexistent database method. Direct regressions and native journeys cover these corrections. Final independent review found no reproducible remaining P1/P2. Browser/live evidence was exercised by UI agent/root; the final read-only reviewer inspected coverage and independently ran backend tests without repeating external sends.

## Maintenance and next part

Schema 7 and the strict mutation contract require compatible API, worker, dispatcher and UI code. Before a separately authorized production transition, stop old writers, make/rehearse a protected pre-upgrade backup, install compatible processes together and reload browser tabs. Obsolete clients lacking keys/versions are rejected. Rollback restores the pre-upgrade DB and matching application version, not an in-place downgrade. Stored operations/settings versions are included in protected backups; provider acceptance since a backup cannot be reconstructed exactly.

Proceed with [part 06](06-named-telegram-channels.md) only on **`feat/improvements`**. Reuse `VersionedRequest`, transactional CAS and `api.js` expected-version options; creation forms require immutable keys, fenced durable operation state and explicit uncertain replay. Reuse field reconciliation instead of blind overwrite. Current polling Settings keep autosave; credentials use explicit Save. Telegram/webhook/SMTP configuration is not implemented in this part; the supplied ntfy/email preferences remain applicable to their later parts.

Final self-review checks every part 05 acceptance requirement against current source, test/browser/live outputs, migration compatibility, all reported fixes, privacy, documentation links, branch policy and resource cleanup. Whitespace checks pass. This handoff records local completion and bounded authorized live validation, without remote publication or deployment.
