# 12 — Fair progress with bounded checking work

**Outcome:** A large job yields between targets so other jobs progress; one pathological paginated target has a finite deadline, with incomplete checks labeled honestly. **Effort:** medium–large. **Depends on:** 02 run snapshots/fences, 04 intents, 11 progress. **Why here:** instrumentation and stable semantics exist before scheduler surgery. Follow [shared execution rules](README.md).

## Scope and source entry points

Inspect `run_due/run_claim`, scheduler, claim/finalize/reconciliation repository methods, `DoctolibClient._get/check` and request gate. Implement target-level continuations on the same logical run, finite transport budgets and fair continuation selection. No parallel availability workers, broker, page-level resumable transport or automatic request-rate increase. Existing date-window/target limits remain; an incomplete budget-limited check is an error, never a negative result.

## Implementation order

1. Use 11 metrics to capture current queue lag/run duration in synthetic workloads and confirm source bottleneck. Define operator-configured budgets with explicit safe defaults: 120 seconds per target and 180 seconds per slice, including gate waits/retries/network. Budgets are limits, not claims of guaranteed latency under unlimited load; document that large windows or congested egress can exceed them and require an operator adjustment. Do not silently truncate requested dates.
2. Persist run target cursor/continuation state and completed coverage against 02's immutable snapshot. Commit each terminal target result, any current episode/event publication, and completed coverage/cursor advancement together under the live claim fence. Enforce one terminal result per logical `(run_id,target_id)`; resume skips completed targets. A slice processes a bounded amount, yields only after its current request/target returns, and releases ownership through an explicit yielded state understood by periodic reconciliation. The next slice claims the same logical run with a fresh claim token/generation from 02, not a new alert episode or reused owner. New manual/search intents coalesce while it is running; superseded search work can stop its remaining old targets and yield to a latest-revision follow-up.
3. Check remaining monotonic deadline before gate reservation, after gate wait, retries/redirects and each page; bound individual connect/read timeouts and retry sleeps to remaining budget. A gate slot beyond the budget must not cause an unbounded sleep; choose safe cancellation/defer accounting without accelerating other reservations. When a target budget expires, discard partial availability and record incomplete/error with a safe reason; advance the cursor so one failing target cannot be retried forever in the same run.
4. Fairly interleave ready new jobs and yielded continuations using persistent last-served/ready ordering; bound priority boosts for manual work so it cannot starve ordinary jobs. Do not start a target with too little remaining slice budget; yield and permit it a fresh bounded slice. Renew/fence resumed ownership; late responses from an expired slice cannot publish current state. A logical run is complete only when all snapshot targets have terminal results, otherwise explicitly yielded/interrupted.
5. Extend existing status/card/detail evidence with queued continuation/progress/budget error. Preserve complete-negative rules; valid per-target positive detections may notify independently, but mixed coverage stays partial. Document budget tuning from observed lag rather than lowering provider protections.

## Direct acceptance checks

- Fake large multi-target job yields and a second due job checks before the first completes; cursor/revision/result identities survive restart.
- Crash at terminal result/cursor publication: restart cannot replay a completed target or reorder episode state. Expire/reclaim the same logical run with a new token, then release an old-slice response: old owner cannot publish or finalize.
- One endlessly slow/paginated fake target exceeds its deadline, records incomplete error, and other jobs progress; no partial data becomes a negative/complete count.
- Shared gate waits, retries and redirects consume the same budget; no outbound work starts after deadline/lease loss.
- Repeated manual requests cannot starve ordinary jobs; latest search edit cancels obsolete continuation and gets one fresh run.
- UI shows yielded progress and error while preserving useful historical evidence.

**Ship gate:** direct bounded-progress reproduction passes with finite runnable input; no universal SLA claim. Handoff describes cursor/yield state, gate budget accounting and observed latency. If actual client hooks cannot enforce deadlines, solve that adapter limitation rather than substituting a target-count-only budget that leaves paginated targets unbounded.

**Major risks:** stale-run sweeper treating yielded work as abandoned; duplicate target results/alerts on resume; deadline accounting excluding gate waits; preserving misleading partial totals; promising fairness under unbounded admission.

**Part 04 preservation contract:** read the [part 04 handoff](04-completion-handoff.md). Preserve durable intent identity/revision, one queued intent per job, atomic intent consumption plus owned run claim, and the persisted shared 60-second extra-start budget. Creation/ordinary polling do not reset that budget. Paused manual runs/events require their current status-version capability and current search revision even after completion; explicit pause/Stop check revokes it, and paused search edits require another deliberate request. Future policy or workload slices must not resume paused jobs or automatically refresh a stale paused event.
