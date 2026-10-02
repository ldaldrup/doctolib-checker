# 04 — Bounded manual override and fresh checks after search edits

**Outcome:** A job card can request the earliest possible check beyond the normal polling floor; a saved active search edit requests a fresh check. A paused job can run once and notify without becoming recurring. **Effort:** medium–large. **Depends on:** 02–03. **Why fourth:** user-facing speed on trustworthy results and independent delivery. Follow [shared execution rules](README.md).

## Source and precise behavior

Inspect check-now routes, `set_job_due/update_job/claim_due_jobs/finish_run`, current guard rules, UI client/controller/cards and 02 snapshots. User selected **one extra run/job/60 seconds**, shared between manual and search-edit requests. Ordinary recurring checks retain the server floor; no global request spacing, retry/backoff or lock bypass. “Immediate” means eligible promptly, not synchronous upstream completion or guaranteed queue priority.

## Implementation order

1. Persist one coalesced pending intent per job, requested revision, trigger/reason, eligible time, run link and last-extra-start time. Consuming intent and acquiring run ownership is atomic. Enforce the 60-second extra-run budget at claim/start, not just button/API time; creation/recurring runs do not reset it. Rapid edits/clicks update the pending intent, not stack many runs. A pending newer revision survives completion of an older run.
2. Manual endpoint accepts active or paused job and returns intent identity, queued/running/coalesced status, eligibility timestamp and requested revision. Deterministic running-click behavior: first click during a run requests one follow-up for the latest revision; subsequent clicks coalesce into that same pending intent. A completed intent is not reused for a new deliberate click. No implicit resume. A search edit requests the latest revision only for active jobs. Cosmetic/notification edits do not request upstream work.
3. Claim paused jobs only through a scoped manual intent created while the job was paused. Explicit pause cancels pending requests made while active and stops ordinary notification capability; it does not convert them into paused capabilities. A search edit while paused cancels an obsolete pending manual intent with visible feedback; a new explicit manual request is required for the changed search. Stamp the run/event with the capability to notify for that manual check; do not alter stored paused state or globally remove pause guards. Completion of a paused manual run leaves no recurring next due work.
4. Delivery eligibility accepts a still-current paused manual event even after its run completes, subject to freshness, slot expiry, channel opt-in and later policy. New requests do not reset dedupe; a matching previously sent episode stays silent. Deletion/search edit cancels obsolete events. Do not create recurring checks to refresh a paused event.
5. Wire API client/card action and saved-search feedback; show queued-until, checking, partial/error/complete, worker unavailable and historical evidence. Disable duplicate local submissions but enforce invariants server-side. Read completion from intent/run evidence, not timestamps alone. Keep refresh bounded with existing polling/history mechanisms.

## Direct acceptance checks

- Fake time: repeated manual/edit requests start at most one extra run/60 seconds across restart; edits coalesce latest revision and ordinary schedule stays floored.
- Edit/add target while a run is blocked; old finish cannot erase the follow-up, which covers all current targets. No-op/rename edit does not queue it.
- Manual paused check records fresh result and one eligible deduped alert while status stays paused; no later recurring claim. Manual completion does not count as resume.
- Pause/delete/edit in-flight cancels or supersedes work under explicit capability/revision rules; duplicate HTTP requests cannot create overlapping checks.
- Active manual queue → pause cancels it; paused manual queue → search edit invalidates it; a fresh manual request made while paused alone grants the one-run capability.
- UI journey: Check now, cooldown, completed result; paused Check once and saved active search feedback. Unavailable worker leaves durable intent and honest UI.

**Ship gate:** actual override plus post-edit flow works; wiring the old floor-respecting endpoint alone is insufficient. Handoff names budget/intent consumption semantics and manual paused capability so 06/10/12 preserve it.

**Major risks:** paused checks permanently bypassing pause; old finish deleting latest intent; edit storms evading server budget; implying guaranteed immediate start; re-alerting unchanged slots on manual checks.

## Local completion

Implemented and verified on 2 October 2026 using only `feat/improvements`, then consolidated into local `master` under the existing authorization. See the [part 04 completion handoff](04-completion-handoff.md) for schema-6 contracts, adversarial fixes, backend/native UI verification and bounded real-provider evidence. Parts 05–14 remain plans; remote publication and production deployment are separate.
