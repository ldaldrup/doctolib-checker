# 11 — Activity detail and actionable operational diagnostics

**Outcome:** The user can tell which targets were checked, whether evidence is complete/current, and which destination accepted, failed or is waiting for delivery. **Effort:** medium. **Depends on:** 02–10 data contracts; basic signals from 03. **Why here:** a stable view across all channels/policies; simple health signals already shipped in 03. Follow [shared execution rules](README.md).

## Scope and source entry points

Inspect check/alert history routes, status/heartbeat repositories, error handling in `app/doctolib.py` and services, and UI routing/controller/history adapter. Ship a bounded job-detail/Activity view and useful status diagnostics, using the native UI. No general telemetry platform, graphs, raw log viewer, auto-retry button for every failure or new notification-on-error policy.

## Implementation order

1. Define a safe bounded error taxonomy from typed transport/parse failures: upstream rejection, throttling, timeout/connectivity, invalid metadata, malformed/incomplete response and lease/revision interruption. Preserve safe numeric upstream status and retry-at where available; never copy exception strings, raw provider bodies or request URLs. Keep unknown failures unknown. `ready` metadata is not evidence of successful availability.
2. Extend existing history/status APIs additively. A run exposes requested/start/finish time, trigger/intent, revision/snapshot known state, target coverage and outcome. A delivery exposes channel display identity, event/run link, attempts, pending/withheld/action-required/accepted/uncertain/cancelled state, safe reason and next eligibility. Expose API DB readiness, worker and dispatcher liveness separately from progress, overdue jobs, oldest ready delivery and recent check completion. A heartbeat is not successful checking.
3. Add a job-detail view linking from cards and an Activity navigation entry backed by real data. Show current versus superseded/historical runs, per-target outcomes and per-destination deliveries. Partial success plus error may coexist; no complete-negative label unless every relevant target succeeded under the current revision. Remote acceptance is not device/inbox receipt. Deleted-target labels come from historical evidence, not invented current metadata.
4. Use existing pagination/request queue/cache mechanisms with a finite page size (default 25, maximum 100) and explicit load more. No full-history fetch on every dashboard poll. Filters operate within known coverage or query supported server fields; count unknowns honestly. Preserve loaded data with stale/error notices on failures, with keyboard focus and mobile layouts matching the existing UI.
5. Document an external monitoring checklist for API, worker progress and dispatcher backlog plus safe recovery instructions. Configure no live monitoring system in this application plan; production placement/authentication of health routes requires independent verification.

## Direct acceptance checks

- Fake 403/429/timeout/malformed response become distinguishable safe categories; assert representative token-bearing exception/URL content is absent from reads/logs.
- Mixed successful/error target results, old revisions, withheld paused-manual events and removed targets render truthful history/coverage.
- A heartbeat-only worker with overdue jobs differs from recent progress; a stopped dispatcher differs from a stopped checker.
- Load more/outage retains prior data and bounds fetches; expired-auth state offers recovery without treating login HTML as API JSON.

Reuse journey/UI fixtures; add only taxonomy tests and one connected detail/failure journey. No production load benchmark or global UI redesign.

**Ship gate:** Activity/detail has real backend evidence, useful distinctions and bounded reads. Handoff gives taxonomy/status fields and safe monitoring checklist consumed by 12/13. No claim of deployed external alerting.

**Major risks:** fabricated completeness/counts; showing acceptance as receipt; exposing private appointment URLs in diagnostic output; heartbeat mistaken for progress; fetching all history repeatedly.
