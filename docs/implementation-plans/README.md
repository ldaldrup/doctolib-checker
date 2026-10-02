# Sequential implementation plans

Planning only, 1 October 2026. Start here, then execute one numbered plan at a time. These files are a specification for future implementation, not evidence of implemented or deployed behavior. Source overview: [improvement overview](../improvement-overview.md).

Execution status: **parts 01–05 completed locally**; see the [part 01 handoff](01-completion-handoff.md) and [part 02 handoff](02-completion-handoff.md) and [part 03 handoff](03-completion-handoff.md) for implementation, verification, adversarial fixes and next-step boundaries. Parts 01–05 are committed and consolidated into local `master`. Part 05 was implemented and verified on the same `feat/improvements` branch; see the [part 05 handoff](05-completion-handoff.md). Parts 06–14 remain plans. All further implementation and adversarial reviews reuse only `feat/improvements`; no additional branches are permitted. The current instruction prohibits remote pushes; repository changes do not imply a production rollout.

## Sequence and coverage

Each release is usable by itself with the earlier releases installed. The sequence is the recommended execution order; dependencies named in each file identify what the change actually relies on. Do not implement later plans opportunistically within an earlier plan.

| # | Plan / shipped outcome | Overview coverage | Effort | Why here |
| --- | --- | --- | --- | --- |
| 01 | [Backup and disposable restore](01-backup-and-restore.md) | K, first slice | Small–medium | Recovery before any migration |
| 02 | [Coherent searches and stale-result suppression](02-search-revisions-and-run-ownership.md) | A; M ownership | Large | Fix confirmed stale-search alerts |
| 03 | [Delivery independent of checking](03-isolated-notification-delivery.md) | B | Medium–large | Remove check-loop blocking before more channels |
| 04 | [Manual and post-edit checks](04-manual-and-post-edit-checks.md) | G/H | Medium–large | Ship actual bounded override on trustworthy runs |
| 05 | [Safe saves and conflict recovery](05-safe-api-mutations.md) | L | Medium | Establish mutation contracts before more Settings forms |
| 06 | [Named Telegram channels in Settings](06-named-telegram-channels.md) | C/D | Large | Deliver usable routing and editable credentials together |
| 07 | [ntfy and generic webhook channels](07-webhook-channels.md) | E | Medium–large | Reuse the existing config/delivery lifecycle |
| 08 | [Email configuration and delivery](08-email-channels.md) | F | Medium–large | Add one SMTP transport and independent recipients |
| 09 | [Message content and event controls](09-message-content-and-event-controls.md) | I, content/basic policy | Medium | Controls can cover all supported channels |
| 10 | [Quiet hours with fresh release](10-quiet-hours.md) | I, time policy | Medium–large | Time deferral needs stable routing and event policy |
| 11 | [Activity and actionable diagnostics](11-activity-and-diagnostics.md) | J | Medium | One coherent view of checks and all delivery types |
| 12 | [Bounded workload and fair progress](12-bounded-workload.md) | M, remaining | Medium–large | Use measured progress; preserve revision/intent semantics |
| 13 | [Target metadata repair](13-target-metadata-repair.md) | N | Medium | Repair reuses safe saves, diagnostics and check budgets |
| 14 | [Retention and complete recovery](14-retention-and-recovery.md) | K, remaining | Medium | Final history/dependency model is known before purge |

Large means coordinated schema/API/worker/UI behavior, not necessarily many lines of code. Effort is relative; there are no implied day estimates. Plan 06 is the largest feature release; its implementation steps are ordered to keep migration, routing and UI understandable, but all must land before calling it complete.

## Decisions and working defaults

User-confirmed:

- Manual and search-edit requests may bypass the ordinary polling floor, limited to **one extra run per job per 60 seconds**. Shared Doctolib spacing, provider backoff and run ownership always apply. This is earliest possible execution, not a wall-clock promise.
- A manual check may run a paused job once and notify its selected channels under the normal dedupe/policy rules. It remains paused; no recurring run is enabled. Editing a paused job does not itself start a run.
- Plan 07 includes **ntfy and generic outbound HTTPS webhooks**, with bounded options and a fixed JSON event contract.
- One **SMTP transport editable in Settings**, plus multiple named recipient configs; SMTP credentials are not left in `.env` as the product setup path.

Implementation defaults chosen for implementability:

- API/worker/native JavaScript UI/SQLite remain. One availability worker and one serial delivery dispatcher initially; no broker, framework replacement or server browser.
- Store editable notification secrets with a vetted authenticated-encryption library and one operator-managed `NOTIFICATION_SECRET_KEY` outside SQLite. This key is infrastructure, not an editable notification preference. Missing/unusable key disables secret-dependent writes/sends with a safe action-required error; it does not delete or rewrite encrypted data. Restrict DB/key/backup access; restore requires both data and key. Add only the necessary dependency; no custom crypto or vault project.
- Stable IDs for channels; separate config edit version, destination version and credential version. Same-destination credential repair may explicitly recover eligible work; changed recipient/URL cancels old pending work. No automatic historical replay when attaching/enabling a channel.
- An observed slot episode consumes its logical event identity even with notifications off/no selected channels. Routing/policy enablement applies only to future episodes; continued confirmation of the same episode cannot manufacture a new delivery. Uncertain external acceptance is never automatically resent; explicit recovery must acknowledge duplicate risk and reevaluate freshness.
- Environment Telegram is imported once through explicit idempotent onboarding. Never overwrite stored user settings at startup. Keep the existing legacy CLI/config path compatible; these plans target API/worker/UI.
- Job quiet hours inherit the job time zone (no separate zone field), defer/coalesce, and release only with a matching current-revision successful confirmation no older than the job interval. A paused manual event can be withheld/released while still fresh but cannot silently create recurring refreshes.
- Plan 14 defaults to 90 days of unpinned terminal history, with explicit preview/apply purge; no automatic destructive purge in its first release. Necessary dedupe/live references can outlive that period.
- Generic outbound destinations are HTTPS with no redirects. Private targets require exact operator exceptions; no global “disable SSRF checks.” SMTP uses verified TLS and approved ports. Destination validation must protect the actual connection, not just parse-time DNS.

These choices can be changed deliberately before the relevant implementation starts. Update the decision here and affected plan together rather than letting agents improvise different semantics.

## Starting point and drift control

Initial inspection used backend `master` at `b5cee59` and UI `feat/ui-foundation` at `6f3f589`. The UI subsequently merged into remote `master` (`af83189` before parts 01/02 consolidation). Completed parts 01–03, the UI and these planning Markdown files are consolidated into local `master`.

**Branch policy (explicit user instruction, 1 October 2026):** all future parts use the single shared `feat/improvements` branch, based on the latest `master`. Reuse it across parts and agent reviews; never create per-part, agent, review, worktree or auxiliary branches. After each authorized merge into `master`, update the same `feat/improvements` branch before continuing. Retire older feature branches only after verifying their commits are preserved in `master`. Inspect status and ancestry before changes and preserve user work. This policy applies to every numbered plan and supersedes the historical separate-UI-branch guidance.

Commit IDs and file/function references locate evidence; they are not requirements to pin execution to obsolete revisions. If a planned function has moved or a capability already exists, map the requirement to current code and record the small difference in the completion handoff. If a product contract/dependency changes materially, revise the affected plan before dependent work. No repeated full repository sweep is needed at every step.

## Common execution rules

1. Read this index and the single target plan, then inspect its named sources and the immediate prerequisite's completion handoff. Keep one current step. Preserve existing patterns and user changes.
2. Implement the smallest complete vertical outcome specified. A migration or API with no usable UI does not complete a UI feature. Avoid speculative abstractions for future adapters; add only contracts used now.
3. Keep schema changes transactional and monotonic; derive the next version from actual code, not this snapshot. Reject unsupported/newer DB versions before mutation. Do not run old and new writers together across incompatible migrations. Prepare maintenance/startup instructions; local backup/migration/restore tests use disposable data.
4. Protect the invariants: current results/episodes require search revision and live owner; channels have independent delivery states; manual paused capability is scoped to its requested run/event; negative availability requires complete successful coverage; never log URLs/tokens/raw provider errors or expose secrets in API reads/browser storage.
5. Run the direct checks in the plan with fake providers/temporary DBs. Reuse `tests/test_backend_journey.py`, existing transport fakes and integrated UI `tests/ui_contracts.js`/`tests/ui_harness.py`. Do not invent a general harness. Use simulated time/barriers rather than long real sleeps. Run the existing complete offline Python suite once for a release that changes schema/shared repository code, after focused checks pass; repeat only for relevant changes/failures. UI journeys cover the changed flow and one relevant failure, not a ritual rerun of every visual viewport.
6. Stop at local reviewable work. Production backup/migration/restart, real provider test sends and deployment are separate authorized operations. Each plan's handoff describes what must be checked at rollout; local tests cannot prove production authentication, inbox receipt or actual upstream availability. Prepare runtime/service command documentation when adding the dispatcher, without changing unrelated infrastructure.
7. Finish with a short handoff: outcome, files/schema changed, decisions/differences, direct verification and limits, operational transition/rollback, remaining work. For long execution, update that same handoff at a real checkpoint; no duplicate diaries, heartbeat loops or custom completion framework.

Rollback means restore the pre-migration database **and** compatible application version after stopping writers; never downgrade a new schema in place. Newer completed work/data may be lost by restore, so explain that boundary before any live operation. The improvement overview and `docs/implementation-plans/*.md` are versioned with implementation work; local UI notes/screenshots remain ignored. Keep each new completion handoff in that tracked plan directory.

## Coverage limits and adversarial review

All fourteen overview areas map above. Optional digests/reminders, arbitrary executable templates, multi-transport email, automatic destructive retention, multiworker scaling and page-level resumable checking remain separate future choices. They are not silently required by these plans. Manual repair is implemented in 13; automatic expiry/error-triggered metadata refresh can be added later once the repair path is proven.

The requested adversarial review is recorded in [review findings](adversarial-review.md), with corrections applied to the actual plan files. Acceptance checks specify future implementation verification; creating these documents does not mean those features passed tests.

## Consolidation checkpoint — 2 October 2026

The user renewed explicit permission to commit, merge and retire old branches before continuing. Local `master` was fast-forwarded to `ba6a04a`, preserving completed parts 01–03. Only `master` and the shared `feat/improvements` branch remain locally; the remote has only `master` and no open pull requests. No branch deletion was needed. Documentation updates are committed on `master`, then the same `feat/improvements` branch is advanced to that commit. Remote publication remains prohibited by the existing instruction.

Interrupted part 04 edits are preserved on `feat/improvements` and are not a completed release: schema-6 recovery compatibility, behavioral verification, adversarial fixes and the completion handoff remain required. Resume that work on the same branch after consolidation. The earlier README integration stash remains preserved.

Part 04 continuation completed locally after the consolidation checkpoint. Its [completion handoff](04-completion-handoff.md) supersedes the interrupted-work status above; all changes remain on the same `feat/improvements` branch.
