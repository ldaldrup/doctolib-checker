# 03 — Telegram delivery independent of availability checking

**Outcome:** A slow/broken Telegram send cannot block due availability checks. Retry and recovery are durable and visible through API status/history. **Effort:** medium–large. **Depends on:** 01–02. **Why third:** remaining P1 reliability issue, before expanding channels. Follow [shared execution rules](README.md).

## Scope and source entry points

Inspect `CheckService.dispatch_pending/run_due/run_claim`, `send_telegram_alert`, pending/finish-alert repository methods, heartbeat/status and worker entry points. Add one SQLite-backed serial dispatcher process for the existing environment Telegram target. Reuse current alert identity; do not build named configs or a second event schema yet. API/availability worker/dispatcher must share the DB and compatible image. Prepare runtime command/operator handoff; no live infrastructure edits.

## Implementation order

1. Remove network notification dispatch from the availability loop. Availability work persists eligible alerts only. Add an explicit dispatcher entry command and separate dispatcher heartbeat/backlog status; one bounded send attempt per dispatch turn, scheduled retries rather than in-request sleeps.
2. Add atomic alert claims with owner token, lease, attempt metadata and safe outcome categories. Preserve current pending/failed/sent/cancelled history; action-required/exhausted/uncertain categories can be additive fields, avoiding a parallel history model. Claim selects eligible fresh current-revision work; recheck immediately before sending. Persist attempt-started before the network call. A claim expiring before dispatch may recover automatically; one that might have reached the provider becomes uncertain, not pending.
3. Classify permanent credentials/recipient rejection as action-required. Retry known pre-send failures/known temporary rejection with bounded exponential backoff and bounded provider Retry-After when supported. A read timeout, lost acknowledgement or crash after attempted dispatch may mean acceptance: uncertain work is excluded from automatic retries. Default: at most 5 dispatcher attempts, minimum 5-second retry delay, capped 15-minute backoff and 24-hour age; freshness/slot expiry can end eligibility sooner. Counts mean actual network attempts, not dispatch cycles. One attempt has connect/read timeouts and an overall work budget; no unbounded sleep in the sender.
4. Fairly select ready work, excluding exhausted/uncertain/future-backoff rows. Keep checking independent; a serial send can still delay another by one bounded attempt. Add an explicit recovery operation for action-required/exhausted rows after credentials are repaired; reevaluate active job, revision, slot and fresh confirmation. Uncertain resend requires an explicit duplicate-risk acknowledgement, not a general retry button. Never retry sent work as recovery. Provider acknowledgement plus crash cannot be made exactly-once.
5. Extend status/read-only UI notices enough to show dispatcher availability/action-required delivery and prepare maintenance instructions: stop old inline worker before starting the new dispatcher, no incompatible simultaneous writers. Migration claim/attempt fields must be carried into plan 06's channel model.

## Direct acceptance checks

- Hold a fake send open while an independent fake availability worker completes a due check; verify no inline send remains.
- Fake 401, 429, timeout and success produce terminal/rate-limited/backoff/sent states without raw provider data; no immediate three-attempt sender loop.
- Two dispatcher instances claim one row at most once; expiry/restart recovers a lease, fenced completion cannot overwrite another owner.
- Compare expiry before dispatch with provider-accepted → crash → lease expiry: only the first automatically retries; the second remains uncertain without another fake send.
- Pause/delete/search edit/slot expiry before claim prevents delivery; same-destination recovery requires fresh confirmation and cannot replay sent alerts.
- Dispatcher failure does not mark API/availability worker dead; backlog retains unsent work.

Use fake sessions/time plus SQLite transactions, not real Telegram. Focused delivery tests and existing episode/migration suite establish local behavior. No repeated broad performance benchmark.

**Ship gate:** existing single Telegram remains functional via the documented dispatcher command; no new channel screens needed. Handoff includes retry constants, state meanings, runtime transition and lease fields that 06 must preserve.

**Major risks:** accidentally retaining inline dispatch; long send blocking other deliveries despite isolation; attempts counted twice; losing freshness or pause guards; starting both old and new send paths; claiming exactly-once delivery.
