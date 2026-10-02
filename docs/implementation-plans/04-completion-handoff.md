# Part 04 completion handoff

Completed locally on 2 October 2026, grounded in the [part 03 handoff](03-completion-handoff.md), [part 04 plan](04-manual-and-post-edit-checks.md) and shared execution contract. Implementation and reviews used only **`feat/improvements`**, advanced from consolidated local `master` at `c943edd`. No extra branch, remote push or production database/deployment change. Parts 05–14 remain plans. The authorized live validation below used a disposable local database and the user-provided destinations; credentials and identifying URLs are excluded from source and this handoff.

## Delivered behavior

- Active **Check now** and paused **Check once** request a durable intent through `POST /api/v1/jobs/{id}/check-now`; the endpoint does not synchronously contact Doctolib or alter recurring scheduling. Responses expose identity, requested revision, trigger, eligibility time, status and `coalesced`.
- One extra run per job per 60 seconds is enforced atomically at claim/start using persisted `last_extra_started_at`, shared across manual and effective active search edits. Creation and ordinary scheduled runs do not reset it. Global request spacing, retries, immutable run snapshots and live owner fences remain enforced.
- At most one queued intent per job. Repeated requests coalesce to the latest revision. A deliberate click during a run creates one follow-up; further clicks coalesce. Older completion terminates only its own intent and leaves newer queued work intact. A completed intent is never reused for a new request.
- Only effective active search changes enqueue fresh work. Rename, notification settings, interval-only changes and equivalent search saves do not. Paused search edits cancel obsolete queued manual work and require another explicit request.
- Paused checks are scoped to manual intent, search revision and status version. Completion leaves the job paused and schedules no recurring work. Matching fresh results may notify Telegram after the run completes, subject to current revision/status capability, active target, opt-in, future slot and freshness. Repeated confirmation preserves sent-episode dedupe.
- Explicit pause, including **Stop check** on an already paused manual job, revokes prior capability and cancels queued work and unsent events. Resume cannot revive old capability. Delete/search edits suppress obsolete publication/delivery; in-flight responses remain historical evidence where ownership still permits recording. A request already permitted to the provider cannot be recalled.
- Cards use durable `check_intent`, `current_run` and linked run outcome, rather than timestamp guesses. Queued/cooldown, checking, completed, partial/error/interrupted, worker unavailable/unknown, cancellation and saved-search feedback are visible. Local duplicate submissions are disabled. Request errors remain separate from server-confirmed progress. Bounded refresh preserves unsaved editor controls.

## Schema and downstream contract

Schema **6** adds:

| Record | Fields |
| --- | --- |
| `jobs` | `status_version`, `last_extra_started_at` |
| `check_runs` | `intent_id`, `paused_manual`, `status_version` |
| `check_intents` | `id`, `job_id`, `search_revision`, `triggered_by`, `requested_at`, `eligible_at`, `status`, `paused_manual`, `status_version`, `run_id`, `cancel_reason` |

A partial unique index enforces one queued intent per job. Intent consumption and run ownership acquisition share a `BEGIN IMMEDIATE` transaction. Recovery supports schemas 1–6 and checks all new structural requirements. Schema-5 migration preserves existing history, initializes status versions and gives legacy runs no paused-manual capability; no automatic check or notification replay is seeded.

`request_check` owns request creation/coalescing; `claim_due_jobs` owns eligibility, cooldown debit and snapshot creation; `run_can_check`/renewal enforce transport permission; result publication, `create_alert` and dispatcher attempt permission revalidate capability. Delivery obtains paused authorization through the alert's result/run link even after completion. Public evidence excludes owner tokens. Expired run reconciliation completes the linked intent with interrupted run evidence.

Parts **06, 10 and 12** must preserve this contract. Named-channel routing must propagate the same paused event capability to selected deliveries. Quiet hours must request another deliberate manual check for stale paused evidence, never recurring refresh. Workload continuation slices must retain logical intent/run identity and must not debit another extra run budget for each slice. Sent and uncertain delivery semantics from part 03 remain intact. See the preservation notes in those plans.

## Verification and adversarial review

- **188 backend tests passed** with Python 3.12 and the pinned `curl_cffi==0.16.1`, via `python -m pytest -q`. One dependency deprecation warning; no failures.
- **18 direct manual-flow acceptance tests** cover persistent/shared cooldown across repository restart, creation/recurring budget separation, rapid coalescing, blocked-run edits and new targets, unchanged saves, paused publication/notification/dedupe, explicit pause/resume/delete/search invalidation, concurrent requests and terminal/error evidence. A separate direct reconciliation test proves that a dead worker terminates its abandoned manual intent, cannot finish with its stale owner, and cannot erase a newer queued paused follow-up.
- Two focused recovery tests prove backup/restore preserves cooldown plus queued paused capability and rejects a schema-6 database missing capability structure. Honest legacy fixtures now remove schema-6 fields before rehearsing schemas 1–5.
- Actual `python -m app.admin backup` / `verify` subprocesses passed on disposable schema-5 and schema-6 databases: **5→6** and **6→6**, integrity/foreign keys/API health all okay. Archives and source data were temporary and removed.
- **23 native Safari contracts passed**, including actual active Check now → linked completion, paused Check once → completion while paused, duplicate-click prevention, worker-unavailable durable queue, cooldown, Stop check cancellation/re-request, paused-search cancellation and active-search saved feedback. Synthetic UI fixtures performed no provider calls.
- Bounded live acceptance used the two supplied real Doctolib targets, shared 3-second request spacing, a 30-day horizon and a disposable local database. The first target returned `no_availability`; the second returned `available`. Both manual runs completed while paused. **One Telegram event was sent with one attempt**. Duplicate queued requests coalesced, cooldown prevented immediate claims, and another check after cooldown preserved **one event/one attempt**. Neither job became recurring. Ten Doctolib request turns were reserved in total; no booking action occurred. Temporary data and runner were removed. Provider availability is a point-in-time observation, not a guarantee of future results or production deployment.
- Independent storage, flow and UI adversarial passes found no reproducible P1/P2 ownership, capability or migration flaw. Reported UI issues were fixed: running requests now show unavailable/unknown worker state, paused manual work has Stop check, and ambiguous request errors no longer hide durable progress. Final coverage review added an actual active-card check journey alongside the paused journey.
- Older backend assertions were updated for intentionally replaced floor/conflict/pause behavior. Single-run historical snapshot tests use a one-run bound so their fixture cannot fabricate a new current-revision match; separate acceptance tests exercise the fresh follow-up. Calendar tests establish fake time before job creation. These changes preserve stale-response, request-spacing and sender-permission safety assertions.

## Operations and next step

Schema 6 requires compatible API, availability worker and dispatcher code. Before an authorized production transition, stop old writers, create and rehearse a protected pre-upgrade backup, then start compatible processes against the same database. Do not mix older workers that do not understand scoped manual capabilities. Rollback restores the pre-upgrade database and matching application version; do not downgrade a live schema in place. Provider acceptance since a backup remains outside SQLite recovery guarantees.

Proceed with [part 05](05-safe-api-mutations.md) on the same **`feat/improvements`** branch. No named Telegram settings, ntfy/generic HTTPS adapter or SMTP/email implementation was added here. The user's real ntfy/email destination preferences apply when parts 07/08 are reached; they are not stored in repository configuration.

The final independent acceptance reviewer found no P1/P2 and independently passed the full 187-test suite before the added reconciliation test, then passed that new test separately. Its README clarification about explicit refresh of stale paused evidence was applied. Root subsequently passed the complete 188-test suite. Browser/live evidence was directly exercised by the UI agent/root respectively; the read-only final reviewer inspected coverage without repeating those external runs.

Under the existing commit/merge authorization, this completed work was committed on `feat/improvements` and fast-forwarded into local `master`, then the checkout returned to the same feature branch. Both branches preserve the reviewed implementation and this handoff; remote publication remains prohibited.

Final self-review checks the plan's outcome, every acceptance boundary, actual test/browser/live evidence, migration/recovery compatibility, all reported fixes, secret-free changes, documentation links, branch policy and temporary-resource cleanup. `git diff --check` passes. This is local implementation plus bounded live validation, not a production rollout.
