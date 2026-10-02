# Part 03 completion handoff

Completed locally on 1 October 2026, grounded in the [part 02 handoff](02-completion-handoff.md), [part 03 plan](03-isolated-notification-delivery.md) and shared branch policy. All work remains uncommitted on the existing **`feat/improvements`** branch, based on consolidated local `master` at `dc3efea`. No additional branch, remote push, production database, real provider send or live infrastructure change was made. Parts 04–14 remain plans.

## Delivered

- Availability checks persist eligible alerts only. `CheckService.dispatch_pending` and both inline dispatch call sites are removed. The legacy CLI has its separate original notification loop; this change targets the API/worker/UI backend.
- `python -m app.dispatcher` runs a separate serial SQLite-backed dispatcher; `--once` runs at most one network attempt. `DeliveryService` reconciles expired delivery claims, updates its own heartbeat, claims fresh eligible work and sends independently from availability polling. Missing environment credentials produce a safe heartbeat error without consuming an attempt.
- Schema **5** adds alert delivery/attempt ownership and a separate `dispatcher_heartbeat`. Claims atomically bind `claim_owner_token`, `claim_until`, `claim_result_id` and `claim_search_revision`. `attempt_started_at`, `last_attempt_at`, `last_attempt_outcome`, `delivery_state`, `delivery_epoch_at` and `delivery_epoch_attempts` are additive; existing alert identity, dedupe, statuses and lifetime `attempt_count` are preserved.
- Claims require an active job/target, enabled Telegram, known published matching current revision, future slot, matching episode and confirming result no older than one job interval. A final guarded transaction rechecks this immediately before transport permission. Result refresh between claim and permission cannot silently replace the payload. Completion requires the same live unexpired owner; stale owners cannot overwrite another claim.
- Default transport prepares a short-lived child/session/payload, signals readiness, then waits for the parent to persist the attempt and permit its single POST. Preparation/startup failures do not increment network-attempt counters. The guard's SQLite wait is bounded by the remaining budget, and permission is not sent after the deadline. A crash after persisted permission remains conservatively uncertain, even if actual provider acceptance cannot be determined.
- One send uses connect/read timeouts **3/7 seconds**, a **15-second transport budget**, a **64 KiB** response cap and no redirects/retry sleeps. Parent deadline cleanup and an independent child watchdog stop trickling/stalled transport, including if the parent dies. Cleanup has bounded terminate/kill joins. SQLite claim/completion waits remain separately bounded by the DB connection timeout. Production uses `spawn`; offline tests also exercise the real spawn boundary.
- Credentials/recipient rejections (400/401/403) are action-required. Known connection-establishment failure, 429 and known temporary rejection retry with safe machine codes. Read timeout, lost acknowledgement, invalid/unknown response and started-attempt expiry become uncertain. Provider body/description, exception text, token and secret-bearing endpoint are never persisted as outcomes.
- Retry defaults: **5 network attempts per epoch**, **5-second minimum** exponential delay, **900-second cap** and **24-hour age limit**. Provider Retry-After is bounded to 5–900 seconds. Future backoff, in-flight, exhausted and uncertain work do not block another ready alert. Counts are recorded once at network permission; dispatcher cycles and proven preparation failures do not count.
- Unstarted expired claims recover automatically; started expired claims become uncertain. Cancellation history is retained even if an attempted send later acknowledges acceptance. Sent/uncertain cancellation cannot automatically reactivate.
- Explicit CLI recovery reevaluates all freshness/eligibility guards, preserves lifetime counts and starts a new epoch. `--recover ALERT_UUID` permits action-required/exhausted recovery; uncertain delivery additionally requires `--acknowledge-duplicate-risk`. It only requeues and never directly sends or replays sent work. UUID validation occurs before DB initialization/output.
- `/api/v1/status` exposes separate dispatcher heartbeat/aliveness/backlog. `/api/v1/alerts` includes safe delivery categories and attempt metadata while excluding claim credentials. Read-only Jobs/Settings notices distinguish unavailable dispatcher, queued work, action-required, exhausted and uncertain delivery from availability-worker health. Existing editor/settings controls survive refresh.

## Migration and recovery compatibility

Complete schemas 1/2/3/4 migrate transactionally to 5. Admin required-column validation supports 5 before initialization, and backup archive format remains 1. Supported legacy backup/rehearsal handling and original snapshot immutability are retained.

Legacy **pending and failed** inline alerts migrate to `delivery_state=uncertain`/`last_attempt_outcome=legacy_unknown`, retaining status/ID/count. An old worker could have sent successfully and crashed before storing acknowledgement, even with pending status and zero durable attempts. Fresh confirmation alone cannot prove non-acceptance; explicit duplicate-risk acknowledgement plus fresh eligibility is required. Old sent episode dedupe remains preserved. Recovery does not reset lifetime attempt history.

Plan 06 now explicitly carries all claim/binding/attempt/epoch fields and uncertainty semantics into named channels. There is no new channel/event schema or general notification configuration in this part.

## Verification

Full offline Python suite: **165 passed**, with the existing Starlette/AnyIO deprecation warning. An additional focused real-spawn regression passed after that suite, proving the actual production child sees persisted attempt metadata before its offline POST and exits without a remaining child. Native browser fixture harness: **21 contracts passed**, including new dispatcher/recovery notices and existing transport, revision, refresh and autosave contracts. Temporary browser tab and fixture server were closed.

Direct acceptance evidence:

| Requirement | Evidence |
| --- | --- |
| Slow delivery cannot block checks | A fake send is held open while an independent availability service completes a newly due job and queues its alert. Production CheckService has no dispatch method/calls. |
| One attempt and safe classification | Fake sessions cover success, 401/403, 429, 503, malformed acknowledgement, redirect, connect/read timeout and connection loss. No retry loop or raw provider data remains. |
| Atomic claim and fenced completion | Two concurrent SQLite owners yield one claim; wrong/expired owners cannot start/finalize. Beginning an attempt is idempotently blocked after its first start. |
| Expiry before/after sending | Unstarted expiry reclaims with count zero. Fake provider acceptance followed by simulated process death becomes uncertain on restart and performs no second automatic send. |
| Freshness and explicit recovery | Pause/delete/search edit/slot expiry before permission prevent send/count increment. Stale confirmation prevents recovery; fresh confirmation enables explicit repair, uncertain recovery requires acknowledgement, sent recovery is refused. |
| Fair bounded retries | A future-backoff row does not block another ready job; fifth attempt exhausts; provider delay caps at 900 seconds; age over 24 hours exhausts; recovery retains lifetime count. |
| Process/work bounds | Offline real child hangs hit parent and independent child deadlines; no surviving child. Spawn/setup failures count zero. Guard contention honors remaining wait; expired permission budget suppresses POST. |
| Independent visibility/privacy | API and availability-worker health stay alive with stale dispatcher heartbeat and retained backlog. Claim token is absent from public history/status; UI notices escape content and do not assert stale health. |
| Legacy/recovery tooling | Seeded schema 1–4 backup migrations preserve IDs/history/status/dedupe. Legacy pending/count-zero and failed acceptance remain uncertain. UUID CLI recovery only requeues without sending. |

Actual subprocess smoke used disposable schema 4 data: backup -> migration rehearsal to 5 with integrity/foreign keys/real ASGI health -> schema 5 backup -> schema 5 rehearsal, all successful. `python -m app.dispatcher --once` exited cleanly against the disposable migrated DB with Telegram explicitly disabled. No Doctolib or Telegram network request was made; transports were offline fakes. Local tests do not prove production authentication, real acceptance or inbox receipt.

## Adversarial review and fixes

Separate storage/ownership, sender/runtime, test-gap and independent final acceptance passes reviewed the implementation. Reported issues were resolved with regressions:

1. Cancellation could be overwritten by claim reconciliation: preserve cancelled status, and block sent/uncertain automatic reactivation.
2. Legacy pending alerts could have already reached Telegram: migrate pending as well as failed work to uncertain and require acknowledged recovery.
3. Recovery CLI accepted arbitrary text/echoed it: validate a canonical UUID before DB initialization or output.
4. Child could outlive a killed dispatcher parent: add an independent child watchdog with direct offline verification.
5. Transport startup/preparation failures counted as actual attempts: prepare first, then perform the persisted permission handshake; add count-zero and persisted-before-POST regressions.
6. SQLite permission wait could overrun the remaining send budget: bound the guard wait and recheck deadline after permission; suppress POST and retain uncertainty if that boundary expires.

All reported P1/P2 issues are fixed. Final independent acceptance review found no additional reproducible P1/P2. Final self-review checks the full source/test/doc diff against the plan, schema/admin compatibility, branch preservation, runtime commands, no-inline-send invariant, claim/recovery safety, private outcomes and evidence limits. `git diff --check` passes.

## Runtime transition and next boundary

Use compatible code/image and the same persistent writable DB for:

```bash
python -m app.api.main
python -m app.worker.main
python -m app.dispatcher
```

These are separate supervised processes/containers, not sequential commands in one terminal. Dispatcher needs no ingress port. The Dockerfile API default is unchanged; override service commands for worker/dispatcher and share `/data` with UID/GID 10001 access. Environment credentials must be consistent across all three; the dispatcher performs sends. Restart each process independently.

For an authorized live transition, stop all old writers/inline senders, make and rehearse a protected schema-4 backup, migrate/start schema-5-compatible API and availability worker, start one dispatcher, and inspect independent health/backlog/history. Review legacy uncertain rows before explicit acknowledged recovery. Never mix old inline sending with the dispatcher. Stop all three for restore; rollback restores the pre-migration DB and compatible earlier application, not an in-place downgrade. Provider acceptance since a backup cannot be reconstructed exactly.

Part 04 can now add manual/post-edit triggers without coupling checks to delivery. Continue only on **`feat/improvements`**; no extra branches, remote push or production action. This completion does not implement part 04 or named Telegram/webhook/email settings.

## Consolidation checkpoint — 2 October 2026

The original uncommitted-state statements above are historical. Part 03 was committed as `ba6a04a` and is now included in local `master` by a fast-forward merge authorized by the user. Only `master` and `feat/improvements` remain locally; there are no remote feature branches or open pull requests to close. No remote push or production action occurred. Further implementation and adversarial review must reuse the same `feat/improvements` branch, advanced from the consolidated `master`; no additional branches. Interrupted part 04 edits are preserved separately as unverified work on that branch and do not change this part 03 completion claim.
