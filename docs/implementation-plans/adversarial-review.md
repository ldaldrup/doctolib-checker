# Adversarial review of implementation plans

Planning review completed: 1 October 2026. Three agents independently challenged the actual files, then performed a targeted confirmation of corrections. This file records actionable plan corrections, not a claim that future implementation passed acceptance tests.

Review scope: actual plans 01–14 and shared index; shippable vertical outcomes, dependency/contract consistency, migration/failure behavior, scope restraint, and lightweight agent handoff/drift control.

## Passes

- Job/repository reviewer: 01–05 and 11–14, plus paused-manual and quiet-hour interfaces.
- Notification/security reviewer: 03/04/06–10/14; routing, credential/endpoint privacy, provider ambiguity and migration transitions.
- Sequence/feasibility reviewer: cross-reviewed the complete index and plans, focusing on chunks not authored by that agent, A–N coverage, future dependencies, operational transition and unnecessary machinery.

## Findings and applied corrections

| Priority | Files | Defect challenged | Applied contract / direct future verification |
| --- | --- | --- | --- |
| P1 | 02, 12 | Resuming the same logical run could accept a late old owner's response | Fresh per-claim token distinct from run ID; renewal/publication/finalization fenced. Test expiry → same-run reclaim → old response |
| P1 | 03, 06–08 | Automatic retry after timeout/expired claim conflicts with uncertain acceptance | Persist attempt-started; possibly accepted work is uncertain, excluded from automatic retries. Contrast pre-send failure with accepted-send/crash |
| P1 | 05 | Reclaimed create resolver can finish late and create a duplicate | Reservation owner/live lease checked in atomic job+operation publication. Block A → reclaim/complete B → A returns → one job |
| P1 | 12 | Crash between result publication and cursor advance replays a target | Unique terminal `(run,target)` result, atomic result/episode/event/coverage commit and skip completed targets on resume |
| P1 | 06, 09 | Enable/attach can revive cancelled same-episode work despite promised no replay | Observed episode consumed even when off/unrouted; original routing boundary; no automatic cancelled-row revival. Test same-slot silence then genuine reappearance |
| P2 | 04 | Running-click and paused transitions leave product choices to the agent | One follow-up then coalescing; explicit pause cancels active-origin queue; paused search edit invalidates obsolete manual queue |
| P2 | 14 | Every historical manual intent pinned indefinitely | Pin only live intent/capability dependencies; old completed manual history follows cutoff |
| P2 | Index, 10 | Separate quiet zone conflicts with job-zone default | Inherit job time zone only; explicit gap/fold boundary behavior; use existing UI before Activity ships |
| P2 | 13 | Repair reconciliation implies a framework not shipped by 05 | Refetch target validation/metadata and job version, retain unknown outcome when evidence is insufficient |
| P2 | 07 | Privacy acceptance forbids legitimate authenticated booking links | Secrets/endpoints hidden in channel status/diagnostics; intended authenticated booking views and selected outbound payloads remain supported |
| P2 | Index, 08 | SMTP effort understated | Medium–large consistently, accounting for editable transport/TLS/connection policy |

## Outcome and remaining implementation risks

All three targeted confirmation passes found their corrections present in the actual files and no new critical contradiction in those changed contracts. The coverage check maps A–N to numbered releases; no requested notification type or manual/edit behavior is replaced by a narrower queued-only feature. Each plan has a usable outcome, explicit boundaries, source starting points, direct acceptance checks and completion handoff. Plans 06 and 12 remain the largest effort/risk concentrations, but their scope is explicit rather than an open-ended foundation.

No general orchestration framework, broker, repeated full sweep at each step or custom testing platform is prescribed. Snapshot/claim/idempotency races justify narrow fake-time/barrier tests; UI checks exercise changed flows, not arbitrary test quotas. Provider connection binding, SMTP acceptance ambiguity, destructive retention and migration compatibility still require actual implementation verification. They are documented risks, not currently proven features.

Plan-file integrity was checked for numbered cardinality/order, internal links, dependencies pointing backward, required outcome/scope/verification/handoff content and absence of tracked application changes. Planning-only source branches were revalidated; no production/server/provider state was read or changed in this task.

This review is complete for the present planning snapshot. A later material design change should revise only affected plans and their consumers, rather than triggering permanent repeated review cycles.
