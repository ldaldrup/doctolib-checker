# 13 — Versioned target revalidation and repair

**Outcome:** A saved target whose agendas/motive metadata changed can be deliberately repaired without losing job/history identity or sending alerts from obsolete metadata. **Effort:** medium. **Depends on:** 02/04/05/11/12. **Why here:** reuse coherent revisions, safe edits, direct diagnostics and transport limits. Follow [shared execution rules](README.md).

## Scope and source entry points

Inspect `resolve_target/update_job`, metadata resolution in `app/doctolib.py`, target repository methods, 02 snapshots and job-detail UI. Add metadata age/validation result and a per-target revalidate/repair action. No periodic every-target refresh, browser scraping, silent booking URL replacement or automatic repair on every 403/timeout.

## Implementation order

1. Expose last successful metadata validation time and safe current validation state/reason. Existing `ready` means metadata resolved; distinguish it from last successful availability check. Preserve old evidence when upstream validation is temporarily unavailable.
2. Add a bounded revalidate operation for a saved target ID, expected job edit_version and unchanged canonical URL. Resolve metadata through the existing validated-host client and shared request gate; apply 12's finite deadlines. Do no outbound call while holding the write transaction. A timeout yields failure/unknown, not automatic target deletion.
3. After resolution, transactionally recheck job/target existence, URL, expected version and allowed fields. If another user edited it while waiting, return conflict and do not apply the candidate. Repair metadata on the existing stable target ID; distinguish effective metadata changes from renewed validation time. Meaningful changes bump 02 search revision and job edit_version, invalidate current evidence and cancel old delivery eligibility. Unchanged metadata updates validation age without forcing another search revision.
4. On actual metadata change queue one fresh latest-revision intent under 04 for an active job; paused jobs remain paused. The user may separately choose manual Check once to notify while paused. Failed validation records a safe reason without replacing usable saved metadata with partial guesses or treating every upstream error as proof the URL is invalid.
5. Add a job-detail target action with pending/result/conflict feedback, metadata identity before/after and “fresh check queued” or “remains paused” wording. No automatic retry after ambiguous mutation; refetch target validation time/metadata and job edit_version to reconcile, showing unknown if evidence is insufficient. Plan 05 supplies conflict rules, not a generic repair-operation framework. Explain repair versus editing the supplied booking URL.

## Direct acceptance checks

- Mock changed agenda/motive metadata: stable target/history IDs remain, revision increments, old result cannot update current episodes/notify, fresh active intent uses new metadata.
- Unchanged resolution refreshes validation time without duplicate episode/search; invalid/incomplete metadata does not overwrite known data.
- Concurrent URL/target edit or deletion during blocked resolution conflicts safely; no long-held DB write lock.
- Paused repair stays paused; explicit manual run uses new metadata and selected channels without enabling recurrence.
- Temporary upstream failure is distinguished from malformed target, budget/gate constraints apply, and errors contain no raw URL/provider payload.

Reuse current fixture metadata and blocked fake-resolution hooks plus one connected repair/conflict UI journey. No live Doctolib request is needed for the implementation acceptance above; a later controlled upstream smoke test has a separate scope.

**Ship gate:** the user has a usable repair path with truthful metadata status and safe subsequent checking. Handoff records changed-metadata equality fields and revision behavior. Optional automatic revalidation on selected errors/expiry remains future work until evidence justifies it.

**Major risks:** metadata repair secretly changing target identity; old in-flight results using repaired live metadata; all transport failures labeled invalid URL; overwriting a concurrent edit; unnecessary refresh request storms.
