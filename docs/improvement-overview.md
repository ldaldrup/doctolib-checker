# Improvement and extension overview

Planning snapshot: 1 October 2026. No implementation or deployment is authorized by this document.

## Scope and evidence

This overview combines independent notification, job/scheduler, and operations sweeps, followed by a separate review of the assembled priorities and feasibility. It targets a private, hobby-scale appointment monitor: improve trustworthy checks and notifications before adding infrastructure or broad customization.

Inspected backend: local `master` at `b5cee59`. Inspected UI: local `feat/ui-foundation` at `6f3f589`, read directly from Git without switching branches. At the initial inspection the checkout had no `app/web/` directory; those UI findings applied to the separate branch. The UI and completed parts 01/02 have since been consolidated into `master`. Older ignored UI planning documents are context, not proof of current behavior or deployment. No production system, credentials, real notification destination, or live Doctolib response was inspected.

Implementation status and future decisions are maintained in the [sequential plans](implementation-plans/README.md). All further work reuses the single `feat/improvements` branch from latest `master`; no extra per-part or agent branches. The findings below remain the initial planning snapshot, not a current claim that completed gaps are still unfixed.

Priority definitions:

- **P1 reliability:** can undermine timely checks, correctness of alerts, or confidence in results. Address before expanding delivery or concurrency.
- **P2 improvement:** useful product capability or operational safeguard, with no confirmed urgent incident. Some P2 work is a prerequisite for other features.
- Effort is relative to this repository: **small** = focused UI/API change; **medium** = coordinated UI/API behavior and tests; **large** = persisted state, migration, delivery semantics, and failure recovery. These are not calendar estimates.

## Biggest areas at a glance

| ID | Area | Priority | Effort | Main outcome |
| --- | --- | --- | --- | --- |
| A | Results and alerts across job edits | P1 reliability | Large | Every result belongs to identifiable search settings; outdated checks cannot notify as current |
| B | Notification delivery resilience | P1 reliability | Medium–large | Failed channels cannot stall checks; retries distinguish temporary and permanent failure |
| C | Named channels and job routing | P2 foundation | Large | Choose multiple independent destinations per job |
| D | Telegram configuration in Settings | P2 | Medium after C; delivery/tests use B | Add, test, preview, enable, and manage named Telegram targets |
| E | Webhook / ntfy notification channels | P2 | Medium after C/B | Multiple safe outbound HTTP destinations with explicit payload contracts |
| F | Email notifications | P2 | Medium–large after C/B | Multiple named email destinations with visible delivery status |
| G | Check now on job cards | P2 | Small for existing queue; medium for override | A clear manual-check action with honest cooldown/progress feedback |
| H | Fresh checks after editing | P2 behavior; depends on A | Medium after A | Search edits schedule one durable fresh check; unrelated edits do not waste requests |
| I | Notification content and policy | P2 | Medium–large | Control what, when, and how to notify without breaking freshness/deduplication |
| J | Activity, diagnostics, and monitoring | P2 | Medium | Understand partial checks, delayed jobs, and individual delivery failures |
| K | Backups, retention, and recovery | P2 | Medium | Recover configuration/history and bound database growth |
| L | Safer API mutations and settings | P2 | Medium | Resolve uncertain saves and conflicting edits; reject unsupported settings |
| M | Workload fairness and lease recovery | P2; concurrency prerequisite | Medium–large | Large jobs cannot indefinitely delay others; expired owners cannot publish current state |
| N | Target metadata repair | P2 | Medium | Recover from changed agendas/motives and explain invalid targets |

Recommended first slice: rehearse basic **K** backup/restore before the first schema migration, resolve **A/B**, then ship the existing queued **G** control and clear status feedback. Build **C/D**, add **E**, and add **F** when email is useful. Introduce **H/I** on top of trustworthy search revisions and deliveries. Start basic **J** diagnostics alongside the first slice; avoid making every later feature a prerequisite for a useful release. A queued G release is an intermediate improvement, not completion of the requested true immediate override.

## A. Results and alerts across job edits

**Current:** `CheckService.run_claim` snapshots the target list once but reads current job search settings before each target. An edit during a check can therefore produce one run with different search constraints, or omit newly added targets. Results store target labels and booking URL, but no immutable search revision/constraint snapshot. Before creating an alert, the service rechecks active status and the Telegram flag; it does not establish that the result matches the current search revision. **Confirmed offline:** a stub request returned a 15 October slot while its job was edited to accept only 19–20 October; the worker still sent one alert through a fake notifier. No live false alert was observed.

**Needs:** Introduce a monotonically increasing search revision, separate from name/status/settings timestamps. At claim time, freeze the run's effective constraints, target membership, and metadata revision/snapshot; persist this evidence with each run/result instead of attaching a revision label to mutable settings. Choose one explicit rule for in-flight edits: preferably finish the old revision as historical evidence and queue one coalesced run for the latest revision. Prevent superseded results from updating current availability/episode state or cancelling current deliveries, suppress stale-revision delivery, and recheck revision eligibility when claiming a delivery. Notification routing/policy should have its own version rules rather than treating every edit as a search change.

**Acceptance:** Edit dates, insurance, telehealth, time zone, or target set while a stub upstream request is blocked. Its response must remain labeled with the old revision and cannot generate a current alert. The follow-up checks every current target, survives restart, and does not duplicate runs after repeated edits. A rename preserves valid evidence. Define how unchanged-slot deduplication survives revisions so harmless edits do not create duplicate alerts.

**Evidence:** `app/services/checks.py::run_claim`; `app/storage/db.py` (`check_runs`, `check_results`); `app/storage/repositories.py::insert_result/create_alert/update_job`; UI branch `app/web/assets/js/job-view.js`. **Feasibility:** SQLite is sufficient; this is a cross-cutting schema/worker/UI change, not a card-only fix.

## B. Notification delivery resilience

**Current:** `run_due` dispatches notifications before claiming jobs; `run_claim` also dispatches after positive target results. Telegram delivery is synchronous, with three attempts and sleeps. One slow send can take roughly a minute in nominal timeout/backoff terms; multiple pending sends compound the delay. HTTP failures, including invalid credentials, take the same immediate retry path, followed by repository retries while the alert remains eligible. Reads/rechecks of pending alerts do not constitute an exclusive delivery claim.

**Needs:** Separate durable delivery work from availability checking using a small dispatcher backed by SQLite; no external broker is required. Start with one delivery process and add claims/leases before any concurrent dispatch. Classify permanent credential/destination failures, transient failures, rate limits, and uncertain outcomes; schedule backoff rather than sleeping inside the check loop. Bound attempts/age, expose terminal or action-required states, and preserve the current requirement for fresh confirming availability before retries. Use one bounded network attempt per dispatch turn and fair selection so a broken destination cannot monopolize the queue; a serial dispatcher can still delay another channel by one attempt, so define a latency budget rather than promising zero delay. Add bounded concurrency only if observed latency requires it.

After credentials are repaired for the same destination, allow explicit recovery of still-fresh eligible deliveries. Changing the destination identity must cancel/replan according to C, rather than silently redirecting old work or leaving recoverable deliveries permanently stranded.

**Acceptance:** Simulated slow/failing Telegram does not delay a due check; other eligible channels progress within the delivery budget; a permanent failure stops automatic retry and identifies the affected config; transient retries respect bounds/provider retry information. Test explicit same-destination recovery after repair. If concurrent dispatch is introduced, two dispatchers cannot claim the same delivery simultaneously. Pause, deletion, expired slots, and superseded revisions suppress eligible pending work. An accepted send followed by a crash is explicitly an uncertain outcome: do not promise exactly-once external delivery.

**Evidence:** `app/services/checks.py::dispatch_pending/run_due/run_claim`; `app/notifications.py::send_telegram_alert`; `app/storage/repositories.py::get_pending_alerts/finish_alert`. **Feasibility:** Medium–large. Serial delivery already causes blocking; duplicate sends from multiple workers are a conditional risk, not a confirmed current deployment issue.

## C. Named channels and per-job routing

**Current:** One process-start Telegram configuration and one `telegram_enabled` boolean per job. Alerts hard-code `channel='telegram'`; the unique dedupe key identifies target/slot/episode, without a destination identity. A second destination cannot safely be added just by calling another sender.

**Should:** Store named channel configs (`id`, name, type, enabled, non-secret options, credential reference/version), job-to-channel selections, a logical availability event, and one delivery record per selected channel config. For example, one job can select **Telegram1, Telegram2, Webhook1, Email1, Email2**. Stable IDs carry selections through renames; the display name is not a dedupe key. Channel success/failure/retry is independent.

Start with explicit selection per job. Optional default selections apply to newly created jobs only. Define disabled/deleted-channel behavior and show affected jobs before removal. A disabled channel must not silently reactivate pending deliveries when enabled again; reevaluate freshness and policy. Adding a destination should not automatically replay old alerts; provide an explicit test or separately defined catch-up action. Rotation must not silently send old queued work to a different recipient.

**Migration:** Preserve historical sent/cancelled episodes. Offer one default channel seeded from the existing environment configuration and map only opted-in jobs to it; make ownership/precedence explicit and avoid creating duplicates on every startup. Migrate pending/failed legacy deliveries with their attempt history and stable event identity, reevaluate freshness/status, and cancel ineligible work with a clear reason. Include sent, failed, paused, stale, removed-target, and in-flight cases in migration rehearsal. Resolve API/worker compatibility and rollback before applying a migration.

**Acceptance:** One event creates five independently tracked deliveries; one failure neither suppresses nor repeats the four successful ones. Rename preserves job selections. Disabling/removing/changing a destination has predictable pending-work behavior. Cleared/disabled configs reject new test/send claims even if a process has cached old settings; record any send already in flight honestly because it cannot be recalled. Existing jobs retain their notification opt-in and migration does not resend history.

**Evidence:** `app/settings.py`; `app/api/schemas.py`; `app/storage/db.py` (`jobs`, `alerts`); `app/storage/repositories.py::alert_dedupe_key/create_alert`. **Feasibility:** Large, but this shared foundation prevents three incompatible notification systems.

## D. Telegram configuration and testing in Settings

**Current:** Backend credentials come from environment variables, commonly supplied through `.env`; the UI only shows configured/not-configured and the job form offers one Telegram checkbox. The message is fixed HTML. Presence of credentials is not a verified connection.

**Should:** Settings should list named Telegram configs with add/edit/disable/delete, bot credentials, target chat, optional supported send options, connection-test status, and message preview. Separate configuration validation from **Send test message**, which has a real external side effect and must be clearly labeled. Show destination name, timestamp, sanitized result, and whether settings are saved or still a draft. Saved changes must be observed by the worker without requiring a process restart.

First slice: save before sending a test; use B's bounded delivery mechanism instead of a long synchronous Settings request. A browser timeout must not automatically resend; show an uncertain outcome when acceptance cannot be established. Tests remain distinct from real appointment episode history. Draft-only testing can be a later transient operation with no stored secret payload.

**Needs with editable secrets:** Write-only credential fields, masked reads, explicit keep/replace/clear semantics, no secrets in browser storage/logs/history, and protected mutation/test endpoints. If storing encrypted credentials, keep the encryption key outside the database and define backup/restore/key-loss behavior; encryption does not substitute for authentication or file permissions. Reuse the existing authenticated deployment boundary rather than adding an unnecessary second login system.

**Acceptance:** Add two configs, test each, select both on a job, reload, and receive isolated delivery states. Editing without entering a new token retains it; explicit clearing disables usable configuration. Tokens are absent from API reads, errors, telemetry, and exported general settings. Template preview escapes untrusted values.

**Evidence:** `app/settings.py::Settings.from_env`; `app/api/routes.py` settings endpoints; `app/notifications.py::format_slot_alert`; UI branch `pages/settings.js`, `pages/jobs.js`. **Feasibility:** Medium after C for configuration CRUD; reliable tests/delivery use B. Editable secrets require backend storage/API work as well as UI controls; advanced policy and concurrent dispatch are not prerequisites for these screens.

## E. Outbound webhook and ntfy channels

**Current:** No webhook config, sender, or per-job routing exists.

**Should:** Implement outbound webhook destinations in Settings; these are send targets, not inbound webhooks for controlling jobs. Start with a small versioned JSON event contract and a dedicated ntfy adapter/preset where its supported request shape differs. Each named config has destination, supported authorization, enabled state, preview, and test send. Allow multiple configs of the same type. Keep method/body/header choices bounded rather than exposing arbitrary scripts or a general HTTP client.

**Needs:** Explicit success criteria, timeout/body limits, retry classification, secret redaction, and an idempotency/event identifier where the receiver supports it. Generic success means accepted by the remote endpoint, not proven delivered to a device. Provider-specific behavior must be checked against official documentation during implementation.

Start with a documented ntfy preset/adapter if it meets the need, then expand to generic HTTP destinations. Mask secret-bearing endpoint paths/query parameters as well as authorization fields in API reads, logs, and exports.

User-entered endpoints introduce a new server-side request surface. Require HTTPS by default; block loopback, link-local, metadata, and unintended private destinations, including DNS and redirect changes. Deliberate self-hosted private destinations need an explicit operator allowlist/exception rather than a global bypass. Apply destination policy to test sends too, and avoid forwarding credentials across redirects.

**Acceptance:** Two named webhook configs deliver independently; mock ntfy and generic endpoints verify their distinct payloads. Invalid/private/redirected destinations are rejected according to policy; secrets never appear in response/history. Failure/retry of one endpoint does not stall checks or duplicate other deliveries.

**Feasibility:** Medium after C/B; secure arbitrary destination handling is the main added complexity. Implement ntfy first if it satisfies the use case, then generalize only as needed.

## F. Email notifications

**Current:** No backend email delivery or supported email settings.

**Could:** Add an SMTP-backed adapter first, with one operator-managed transport configuration and multiple named recipient configs; this supports Email1/Email2 without duplicating SMTP credentials. Start with one recipient per named config, making delivery outcome unambiguous; multiple recipients later require per-recipient accepted/rejected status. Allow separate transports later only if needed. Include TLS/certificate validation, bounded timeouts, sender validation, test send, plain-text content, and optional escaped HTML. Do not build mailbox polling, inbound mail, or a mail server.

**Needs:** Secret handling from D, independent delivery state from C/B, bounded retries, header-injection validation, and clear status wording. SMTP acceptance does not prove inbox receipt; spam filtering, sender/provider restrictions, and bounce visibility need operational validation. Decide whether the SMTP transport stays operator-managed or is editable through Settings.

**Acceptance:** Multiple named email configs on one job send once per event; rejection/authentication/timeout failures are distinguishable and do not block other channels. Verify both text and HTML rendering without real recipient data in fixtures. A controlled real-provider smoke test belongs to a later implementation/deployment step.

**Feasibility:** Medium–large. Prefer an existing mail provider; inbox deliverability is a separate concern from a passing adapter test.

## G. Check now on the job card

**Current:** `POST /api/v1/jobs/{id}/check-now` already queues a due time and returns `queued` plus `next_check_at`. Paused jobs and jobs with an active lease return conflict. The server minimum interval still applies, as does global Doctolib request spacing. UI branch API/client and job actions have no check-now control.

**Should first:** Wire that endpoint into the card. Show submitting, accepted/queued, cooldown-until, running, completed/failed, and worker-unavailable states using server evidence. A successful POST means queued, not checked. Keep polling/history refresh bounded and preserve the last historical result while waiting. Repeated clicks must coalesce rather than pile up requests.

**Product decision:** The requested immediate override goes beyond current behavior. Recommended baseline: **Check now = request the earliest allowed check**. The floor is anchored to the later of the last start/finish, so a just-completed job normally waits at least the server minimum from completion, even if its ordinary interval is longer. To implement the requested true manual floor override, define an explicit bounded exception with a server-side cooldown/budget and trigger audit; never bypass the shared request-spacing gate, provider backoff, active-run ownership, or job status checks. A running sequential worker and other queued work still prevent a wall-clock guarantee. Decide separately whether a paused job can run once while staying paused.

**Acceptance:** Card action exercises the existing endpoint; queued/cooldown feedback matches returned timestamps; running/paused conflicts are clear; repeated clicks do not overlap checks. Any later override has direct API tests for budget exhaustion and restart/concurrency, rather than trusting a disabled button.

**Evidence:** `app/api/routes.py::check_now`; `app/storage/repositories.py::_minimum_due/set_job_due`; `tests/test_backend_journey.py::test_pause_resume_and_check_now_obey_minimum_interval_after_restart`; UI branch `api.js`, `pages/jobs.js`. **Feasibility:** Small for wiring existing semantics, medium for a real override policy.

## H. Check after editing

**Current:** `Repository.update_job` already moves the next check to the earliest permitted time. It does not bypass the floor. During an active run it retains the lease; `finish_run` subsequently schedules finish + the current interval, overwriting the edit's due time. New targets were not in the original run's target snapshot. Thus “editing never schedules a check” would be inaccurate, but prompt, complete verification of the edited search is not guaranteed.

**Should:** Detect meaningful search changes and persist one follow-up intent for the latest revision; consume it after the current run finishes and when the earliest allowed time arrives. A rename or channel-label change should not cause an upstream check. Separate notification-selection changes from search changes. Record trigger reason (`manual`, `search_edit`, `schedule`) and show “Saved; fresh check queued for …” or “Saved; monitoring remains paused.” Do not silently resume paused jobs. Creation already starts due; preserve that behavior.

**Acceptance:** Idle edits respect the floor and check the new settings; in-flight edits retain a follow-up through finish/restart and include new targets; repeated edits coalesce; name-only edits do not consume upstream capacity; paused jobs stay paused. Old-revision results cannot satisfy the new-revision check intent.

**Evidence:** `app/storage/repositories.py::update_job/finish_run`; `app/services/checks.py::run_claim`; `tests/test_backend_journey.py::test_edit_during_run_keeps_claim_and_uses_new_interval`. **Feasibility:** Medium after A; use the same durable request mechanism as G.

## I. Notification content, timing, and event policy

**Current:** Backend policy already alerts on a new earliest-slot episode/reappearance; count-only growth with unchanged earliest slot does not alert. Pending retries require fresh confirmation. API/worker message formatting is fixed; legacy CLI has different configuration and notification behavior.

**Should first:** Preserve current policy as the default. Add safe content controls: include job name, practitioner/practice, earliest appointment, checked time, booking link, time zone, and channel-appropriate formatting. Preview with synthetic values; use an allowlisted placeholder vocabulary, not executable templates. Offer silent/loud options where supported, optional quiet hours with an explicit time zone, and clear per-job event toggles.

**Could later:** Per-channel/job overrides, digest/reminder modes, and persistent job-error notifications. These need explicit semantics for stale/expired appointments, disappearance, rate limits, and policy edits. Quiet-hour release requires a successful current-revision confirmation within a defined freshness window and a still-matching future slot. If confirmation is stale, queue a normal check respecting the floor/spacing and wait for its result; do not deliver an expired alert just because its timer elapsed. Define whether error events go to the same channels and how to avoid repeated-error noise.

**Acceptance:** Existing episode tests still pass; count-only growth remains silent by default. Preview and actual rendering agree; unknown placeholders are rejected; HTML is escaped; length limits are handled. Quiet hours work across midnight/daylight-saving changes and never release stale alerts. Re-enabling a policy has an explicit replay rule.

**Evidence:** `app/notifications.py::format_slot_alert/should_notify`; `app/storage/repositories.py::insert_result/create_alert/get_pending_alerts`; `app/config.py` for legacy configuration. **Feasibility:** Medium for content/silent controls; large for quiet hours/digests. Keep the legacy CLI explicitly compatible or document deliberate divergence instead of claiming one shared configuration.

## J. Activity, diagnostics, and monitoring

**Current:** Backend exposes paginated check and alert history. UI cards derive evidence conservatively from history, but there is no dedicated activity/alerts page in the inspected UI. `/healthz` checks database access, and `/status` treats recent worker heartbeat as liveness; neither proves a specific job is progressing. Generic check errors hide useful distinctions. Notification sends do not refresh the worker heartbeat, creating a possible false-stale status during long delivery work.

**Should:** Build an activity/detail view with per-target outcomes, incomplete coverage, run revision/trigger, request versus completion time, and per-channel delivery status. Add sanitized error categories for upstream rejection, throttling, transport failure, malformed response, and invalid metadata; include only bounded safe status/retry information. Expose overdue jobs, last meaningful check progress, delivery backlog/oldest age, and action-required channel configs. Avoid fabricated zero counts when coverage is unknown.

**Needs operationally:** Independent API/readiness, worker liveness/progress, and delivery health signals. Route critical service failure detection through an external monitor where possible; the dead worker cannot reliably report its own death. Monitor heartbeat around bounded work, without treating heartbeat alone as successful checking.

**Acceptance:** Partial success plus error is visible together; all-error is never “no availability.” Stub 403/429/timeout/schema failures produce useful sanitized categories without URLs, tokens, or raw response bodies. A stalled check and a failed channel are distinguishable. History fetches remain bounded with many jobs.

**Evidence:** `app/api/routes.py::healthz/status_view/checks/alerts`; `app/services/checks.py::_safe_error`; `app/storage/repositories.py::dashboard_status/checks`; UI branch `job-view.js`. **Feasibility:** Medium; basic diagnostics can precede the full activity page.

## K. Backups, retention, and recovery

**Current:** SQLite uses WAL; checks/results/alerts accumulate and jobs are soft-deleted. No backup/restore or retention procedure is implemented/documented in the inspected repository. This is a repository gap; deployed backup state was not verified.

**Should:** Document and automate SQLite-consistent backups using an online backup mechanism or a deliberate quiesced procedure, then restore into a disposable database and verify jobs/settings/history. A simple copy of the main database file while WAL writes are active is not a sufficient procedure. Include secrets/key recovery decisions from D, restrictive permissions, and retention for backup copies.

Set an explicit history retention period, optional export, and preview-before-purge behavior. Preserve active alert state, pending deliveries, required referenced rows, and the dedupe evidence needed to avoid resending. Soft deletion alone does not reclaim history or remove personal appointment data.

**Acceptance:** Restore a backup taken with writes active and verify integrity/foreign keys plus application behavior. Test interruption/restart and secret-key availability. Retention cannot erase queued delivery dependencies or turn a previously sent episode into a fresh alert. Document restore and compatibility checks before a production migration.

**Evidence:** `app/storage/db.py::initialize/connection`; `app/storage/repositories.py::set_status`; `README.md` backend section. **Feasibility:** Medium; a tested manual restore procedure is a useful first release before a scheduler or management UI.

## L. Safer mutations and settings

**Current:** Create requests have no idempotency key; a lost response leaves an uncertain outcome. Job edits have no expected-version conflict check. Request schemas use the default Pydantic handling of extra fields, so unsupported settings may be ignored instead of rejected. Only default interval and request spacing are writable through Settings; time zone/polling floor/Telegram credentials are runtime configuration.

**Should:** Add create idempotency with a persisted key/request fingerprint and explicit retention, optimistic edit conflicts, strict extra-field rejection, and stable settings revisions. Preserve drafts on conflict/failure and refetch authoritative state after an uncertain response. Make editable time-zone defaults a separate product choice; retain operator safety bounds for polling/egress rather than making every environment variable a UI preference.

Do not silently retry an ambiguous create until idempotency exists. Keep offset-pagination instability visible if adding larger lists; use cursor/snapshot pagination only when real usage justifies it. New secret/channel endpoints require the same authenticated deployment boundary; this inspection did not establish any current public authentication vulnerability.

**Acceptance:** Repeat the same create key after a lost response and obtain one job; reuse it with different content and get a conflict. Two concurrent editors cannot silently overwrite changes. Unsupported settings return validation errors. Reload preserves canonical values and distinguishes defaults for new jobs from existing job options.

**Evidence:** `app/api/schemas.py`; `app/api/routes.py::add_job/patch_job/put_settings`; `app/storage/repositories.py::create_job/update_job/update_settings`; UI branch `api.js`, `main.js`. **Feasibility:** Medium; versioning should align with A without conflating search revisions and general edit versions.

## M. Workload fairness and lease recovery

**Current:** The worker processes whole jobs sequentially. A job permits up to 100 targets and date windows up to 366 calendar dates, with paginated upstream requests. Large/slow jobs can delay every other due job, including manual requests. The ten-minute lease renews before each target, not every page/retry; stale-run reconciliation runs at worker startup. `finish_run` already checks lock ownership; the missing fences are at result insertion/current episode state/alert creation. Multiple workers or unusually long targets make expired ownership significant, but no live starvation or duplicate check was established.

**Should first:** Measure queue lag, per-job/target duration, and request/page progress; make overdue status visible. Add reasonable workload limits or a bounded per-pass budget before expanding concurrency. Renew and verify lease ownership around long outbound work and fence current-state/result publication against an expired owner. Reconcile stale runs periodically so abandoned checks do not remain visibly running until restart.

**Could later:** Fair target-level work slicing with a persisted cursor and explicit incomplete coverage, if actual workload needs it. Do not jump directly to many workers: all upstream traffic must still pass the shared request gate, and notification claims from B are required first.

**Acceptance:** A slow multi-target fixture exposes delay honestly and lets other eligible jobs make bounded progress under the chosen budget. Simulated lease expiry/reclaim prevents the old owner from publishing current availability or alerts; expired runs become interrupted without restarting. Restart does not lose pending manual/edit intents.

**Evidence:** `app/services/checks.py::run_due/run_claim`; `app/worker/scheduler.py::run_forever/create_check_service`; `app/storage/repositories.py::claim_due_jobs/renew_job_lock/interrupt_stale_runs`; `app/api/schemas.py`. **Feasibility:** Medium for observability/ownership recovery, large for resumable slicing. Coordinate fencing with A.

## N. Target metadata freshness and repair

**Current:** Metadata resolves on creation and target-URL edits; subsequent checks use the saved practice/motive/agenda metadata. There is no periodic revalidation or targeted repair action. A target marked `ready` proves metadata resolution, not successful availability access, and external agenda/motive changes may invalidate earlier metadata.

**Should:** Track metadata age and safe failure reason. Add a deliberate revalidate/repair action per target, and consider bounded automatic revalidation for selected errors or expiry. Preserve the old target/history identity while versioning changed metadata and scheduling a fresh check. Avoid treating a transient upstream rejection as proof the saved booking URL is invalid.

**Acceptance:** Changed metadata fixtures either repair the target and check fresh constraints or show actionable validation failure; retries are bounded and globally spaced. Repair cannot generate alerts from old metadata/search results. Partial/error outcomes remain distinct from no availability.

**Evidence:** `app/services/jobs.py::resolve_target/update_job`; `app/services/checks.py::_meta/run_claim`; `app/storage/repositories.py::update_job/get_target`. **Feasibility:** Medium; use A/J rather than another independent status mechanism.

## Dependency and feasibility decisions

1. **Keep the current runtime shape:** FastAPI, native JavaScript UI, SQLite, and small worker/dispatcher processes are adequate. No broker, frontend framework, new auth product, browser on the server, or provider-hosted dashboard is necessary for these proposals.
2. **Treat event identity and delivery identity separately:** a slot episode identifies what happened; a channel config identifies where it should go. Retry/failure state belongs to the delivery. Preserve freshness and cancellation checks through every adapter.
3. **Separate three versions:** search revision establishes result validity; job/settings edit version prevents lost updates; channel credential/destination version controls queued-delivery behavior. They solve different problems.
4. **Resolve manual override policy before implementing it:** earliest-allowed queue is feasible now; true immediate checking changes the existing protection policy and requires an explicit bounded design. Do not claim that wiring a button implements the latter.
5. **Sequence migrations deliberately:** choose compatibility between API/worker revisions, seed one legacy channel without history replay, back up and test restore, then migrate. Rehearse on disposable data. Updating backend and UI branches is distinct from deployment.
6. **Verify providers at implementation time:** this document proposes adapter behavior without asserting current Telegram/ntfy/SMTP service specifications. Review official provider documentation and perform controlled end-to-end tests when building each adapter.

## Review and verification record

Initial sweep: three independent agents covered notifications, jobs/scheduling, and operations/security/storage. A second round cross-reviewed the assembled document: the notification agent reviewed job correctness/priority; the job agent reviewed notification feasibility; the operations agent reviewed security, scope, and migration sequence. No unresolved planning blocker was identified. The review does not establish that proposed implementation works.

Incorporated review corrections: freeze the full search/target/metadata snapshot at claim; fence result/episode publication despite existing finish-run ownership checks; keep queued check-now distinct from the requested override; bound serial delivery delay and require fairness rather than claiming complete channel isolation; define recovery after credential repair and migration of unsent legacy work; handle uncertain test acceptance without automatic resends; use explicit freshness rules for delayed alerts; rehearse restore before any first schema migration. A single dispatcher, ntfy-first scope, one SMTP transport with named recipients, and the existing SQLite/native UI stack remain the recommended low-complexity starting points.

Local checks performed for this planning task:

- Read current backend source, schema, tests, README, and actual UI branch source. No branch switch or application edits.
- Ran the three focused offline tests for check-now/pause/resume, edit-during-run, and configured minimum scheduling: **3 passed**, with one upstream deprecation warning. They verify current scheduling behavior, not the proposed improvements.
- Notification sweep used a fake HTTP session with sleep patched out: a permanent HTTP 401 still took three immediate attempts. This exercised retry classification without external delivery.
- Job sweep independently ran six focused offline tests (`edit or check_now or lease or stale`): **6 passed**. Its separate temporary-database reproduction confirmed the stale-search alert in A and finish-based rescheduling in H; these tests overlap the three-test run above and are not nine unique tests.
- Live deployment, authentication, backup existence, Doctolib behavior, and actual Telegram/webhook/email delivery remain unverified. Proposed acceptance checks above describe future implementation validation.

Open choices before implementation: true manual override versus earliest-allowed checks; run-once behavior for paused jobs; secret storage and environment migration precedence; private webhook destination exceptions; SMTP transport ownership; channel/policy-edit replay semantics; quiet-hour urgency; history retention duration. These choices do not block use of this overview as a prioritized backlog.
