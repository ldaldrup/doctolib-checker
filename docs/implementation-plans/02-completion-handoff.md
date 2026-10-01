# Part 02 completion handoff

Completed locally on 1 October 2026. Grounded in the [part 01 completion handoff](01-completion-handoff.md) and [part 02 plan](02-search-revisions-and-run-ownership.md). Source checkout: `feat/search-revisions`, based on UI foundation `6f3f589`; the part 01 admin implementation and README work were preserved and integrated. This implementation was subsequently committed and consolidated into `master` with explicit user authorization. No production database, server, credentials or deployment was modified. Parts 03–14 remain unimplemented.

## Delivered

- Schema **4**, migrated transactionally from complete schemas 1/2/3. Jobs have `search_revision` and `edit_version`; claims have a distinct `lock_owner_token`; runs have revision, serialized search snapshot, snapshot-known flag and owner token; results have revision, snapshot-known and publication flags; alerts have revision eligibility. Unsupported versions fail before checker DDL. A partial unique index prevents duplicate known terminal results for a run/target.
- Effective search equality controls revisions: date mode, active date/horizon fields, time zone, insurance, telehealth, target membership and effective target metadata. URL ordering and agenda-ID ordering/duplicates are normalized. Target labels are included so result/notification evidence is coherent. Rename, interval, notification and pause edits change only `edit_version`; scheduler, heartbeat and lease writes change neither counter. An identical save changes neither counter; irrelevant horizon/date fields do not bump search revision.
- Claim transaction captures search constraints, active target IDs/URLs/metadata, evaluation time and effective start/end calendar dates in the job time zone. First-available dates stay fixed across midnight while past-slot filtering continues to use the actual check clock. All targets in a run use this snapshot, including after edits.
- Renewal, result publication and finalization require the same unexpired token. Request guards run before and after the shared spacing wait and before every retry/redirect/page through the existing hook. Renewal samples time only after obtaining SQLite's writer lock. Expired-owner responses are discarded; live-owner obsolete-search responses are retained as historical results with `published=0`. Pause stops further requests; deletion revokes the claim. Coverage/counters derive from persisted terminal results; incomplete runs cannot be reported completed. Stale reconciliation runs every worker tick and preserves actual coverage.
- Episode changes, pending cancellations and alert creation are transactionally fenced by revision, target eligibility and ownership. Old pending rows stay dormant until matching fresh confirmation; sent same-slot episodes remain deduplicated across revisions. Unrelated cancellations are not revived. Telegram retries recheck eligibility after backoff and require the original result/revision identity, so freshly updated evidence is dispatched with a refreshed payload on a later pass.
- History API exposes parsed snapshots and run/result revision evidence without claim credentials. Cards use explicit revisions; legacy snapshots stay unknown, obsolete positives are historical, and negatives require complete current published coverage. Empty running/interrupted checks cannot revive a positive superseded by a closer negative check.
- Part 01 admin validation/rehearsal now supports schema 4 while preserving schema 1/2/3 support and the existing archive format 1. README version guidance is updated.

## Files and decisions

Application changes: `app/storage/db.py`, `app/storage/repositories.py`, `app/services/checks.py`, `app/doctolib.py`, `app/notifications.py`, `app/web/assets/js/job-view.js`, and schema support in `app/admin.py`. Regressions: backend journey/admin/web/UI contracts plus `tests/test_lease_contention.py` and `tests/test_notification_revision.py`.

Snapshots are compact JSON rather than additional relational tables. Legacy run/result revisions are null, snapshots are absent/unknown, and old results are unpublished; no historical criteria are invented. Legacy running claims are interrupted and locks cleared during migration. Existing IDs, history, alert statuses and episode dedupe are preserved. Expired-owner attempt history is deliberately discarded, as permitted by the plan. Retained live-owner obsolete results record their original labels/URLs and revision.

The worker continues its existing sequential scheduling and inline delivery. Durable post-edit follow-up/manual overrides belong to part 04; separate notification dispatch, delivery claims and acceptance ambiguity belong to part 03. A final eligibility read cannot recall a network send already accepted by a provider.

## Verification

Final full offline Python suite: **119 passed**, with the existing Starlette/AnyIO deprecation warning. Final native browser fixture harness: **20 contracts passed**, including revision evidence, obsolete/unknown negatives, contradicted historical positives, transport behavior, 15-second refresh and preservation of unsaved editor/settings controls. Browser tab and fixture server were closed after testing.

Direct regressions exercise a blocked in-flight request released after narrowing the search; coherent two-target metadata despite an intervening edit; frozen request parameters across midnight; lease expiry during request-gate waits and retries; expiry/reclaim publication/finalization fences; deterministic SQLite lock contention; duplicate terminal-result protection; periodic reconciliation coverage; legacy fresh confirmation and sent-event dedupe; Telegram edit during backoff and refreshed same-episode payload; API token exclusion; supported migration/history preservation and unsupported-version rejection.

Actual subprocess admin CLI smoke used disposable data: schema 3 backup -> migration rehearsal to 4 with integrity/foreign keys/ASGI health -> schema 4 backup -> schema 4 rehearsal, all successful. Seeded legacy recovery tests additionally preserve IDs/history/alerts across schemas 1/2/3. Source and archive immutability are checked. No real upstream appointment request or Telegram send was made; all providers were fake/offline.

## Adversarial review and final self-review

Separate worker/lease/migration, notification/UI/API, test-gap and fresh independent reviews were used. Four reproducible findings were fixed and confirmed:

1. P1 lease resurrection after waiting for a SQLite writer lock: acquire transaction before sampling renewal time; deterministic contention regression passes and fails against the old method.
2. P2 stale reconciliation lost terminal coverage counters: derive success/error counts from persisted results; periodic worker regression passes.
3. P2 empty running/interrupted UI fallback skipped a newer negative and revived an old positive: use nearest preceding terminal evidence; running/interrupted browser regression passes.
4. P2 freshly reconfirmed same-episode alert could retry an old payload: require exact result/revision identity on retry; independent regression rejects old retry and later sends the refreshed label under the same event ID.

All reported P1/P2 findings are resolved. Final self-review checked the complete source/test diff, plan scope, revision/token exposure, clock sampling under lock, migration/recovery compatibility, evidence counters, notification fences, UI fallback and working-tree preservation. `git diff --check` passes. Verification remains local and offline; it does not establish a production backup, successful rollout, live authentication or real inbox delivery.

## Operational transition and next boundary

Before any authorized live migration, stop all old API/worker writers, create and rehearse a protected schema 3 backup with the part 01 commands, and start only schema-4-compatible application code. Do not mix old/new writers. Recheck health/history/UI and worker behavior in the deployed environment separately. Rollback restores the pre-migration database and compatible earlier code after stopping writers; never downgrade schema 4 in place. Changes after the backup can be lost, and provider acceptance after a snapshot remains uncertain.

Part 03 can now rely on revision-eligible alert evidence. Do not implement it as part of this handoff. The saved README integration stash was preserved rather than deleted; no user stash was removed. The overview and numbered plan/handoff Markdown files are now versioned alongside implementation work.

## Branch consolidation and future work

The user explicitly authorized committing, merging and retiring old branches on 1 October 2026. Completed backend/UI and parts 01/02 work is consolidated into `master`. Old `feat/backend-api`, `feat/ui-foundation` and `feat/search-revisions` branches are retired after ancestry verification; the saved README integration stash remains preserved. Further implementation and adversarial review must reuse **one `feat/improvements` branch** based on the latest `master`. No additional per-part, agent, review, worktree or auxiliary branches are permitted. After an authorized completed-part merge, advance this same branch before starting the next part. Production rollout remains a separate operation.

Consolidation verification: local `master` and `feat/improvements` share the integrated history; the three old local feature branches were deleted only after ancestor checks. The working tree is clean and the integration stash is preserved. Remote `master` already contained the UI and had no remaining feature branches or open pull requests. Publishing the completed parts and creating remote `feat/improvements` is pending explicit push authorization: automatic approval review rejected the push as outside its interpretation of the local commit/merge permission. No remote source publication or deployment occurred during this consolidation.
