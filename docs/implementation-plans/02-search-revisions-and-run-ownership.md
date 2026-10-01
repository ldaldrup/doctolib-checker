# 02 — Coherent search evidence and run ownership

**Outcome:** A search edited during checking cannot produce a current alert from obsolete criteria. UI/history identify the effective search used. **Effort:** large. **Depends on:** 01. **Why second:** confirmed P1 correctness bug; later triggers/channels must trust results. Follow [shared execution rules](README.md).

## Scope and source entry points

Inspect `Repository.claim_due_jobs/insert_result/create_alert/finish_run/renew_job_lock/interrupt_stale_runs`, `CheckService.run_claim`, `DoctolibClient.check/_get`, schemas and UI `job-view.js`. Add persisted search revisions and immutable run snapshots, lease publication fences, periodic stale-run reconciliation, and revision-aware card evidence. Do not add manual overrides, new channels, workload slicing or a general revision framework.

## Implementation order

1. Add a job `search_revision`, a separate `edit_version` counter for later conflict checks, and run search snapshot/coverage metadata. A normalized change to dates/mode/horizon/time zone/insurance/telehealth/target membership or effective target metadata increments search revision; rename/interval/pause/notification toggles do not. General config edits increment edit_version; schedule/heartbeat writes do not. Effective equality, not submitted field presence, determines change.
2. Atomically claim the job and persist its constraints, target membership and metadata in the same DB transaction. Freeze effective calendar dates evaluated at claim time in the job time zone, including first-available horizon, so midnight cannot change the run. Preserve stable target IDs and URL/metadata evidence. Use a compact serialized snapshot unless relational rows are needed by actual queries.
3. Run against that snapshot. Pause/delete may stop remaining work; do not start a second run. Issue a fresh claim owner token/generation distinct from the stable logical run ID; this allows 12 to resume the same run safely. Add token/lease checks before outbound work and around retries/pages via the existing request hook; check after waiting for a reserved request turn. Renewal, current-state/event publication and finalization require the same live, unexpired claim token. Periodically reconcile expired runs. Old-owner responses may be retained as historical attempt evidence but cannot publish current state or overwrite a completed terminal target result.
4. Fence `insert_result` current episode updates/cancellations and alert creation transactionally on current revision, target eligibility and valid owner. Do not rely on a later read alone. Stale revisions must neither create alerts nor cancel newer deliveries. Stamp alerts with revision eligibility; pending legacy rows lack proven snapshots and need fresh confirmation before delivery. When a matching fresh result safely revalidates eligible legacy work, update its revision evidence without generating a second event or reviving unrelated cancellations. Preserve sent episode dedupe across harmless revisions; do not make every revision change a new episode.
5. Expose run/result revision and whether a snapshot is known. Update the UI adapter to use explicit revisions for new records; migrated historical records remain conservatively unknown. Preserve historical detections without implying current availability. Migrate old records without fabricating snapshots.

## Direct acceptance checks

- Block a fake request, narrow the date range or change insurance/target metadata, then release it: old response is historical only, cannot update current episodes/cancel new deliveries/send an alert; current revision later checks correctly.
- Two-target run edited between targets remains one coherent original snapshot; rename and equivalent URL ordering changes follow the explicitly normalized equality rule.
- Simulated midnight freezes effective dates; a historical migrated run is never asserted current without evidence.
- Expire/reclaim a lease while a fake request is in flight; old owner cannot renew or publish. Periodic reconciliation marks abandoned runs interrupted without restart.
- UI shows partial/error/obsolete coverage honestly; same earliest slot confirmed under a new revision does not resend an already sent unchanged episode.

Update existing mixed-settings tests because that behavior intentionally changes. Reuse temporary DBs, fake time and barriers. Run the focused journey/UI checks, then existing offline suite once for this migration.

**Ship gate:** obsolete results are harmless, new UI evidence is revision-based, migration preserves history/dedupe. Normal scheduling remains usable; durable prompt post-edit follow-up comes in 04. Handoff records snapshot fields, version rules, old-row treatment and stale-owner policy.

**Major risks:** snapshot taken after claim from mutable state; updating episode state before checking revision; retroactively pretending old history has known criteria; lease renewal after expiry; deduplication resetting on every edit.
