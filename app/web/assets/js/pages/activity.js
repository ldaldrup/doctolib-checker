import {badge, escapeHtml as h, formatSlot, icon} from "../ui.js";

const when = value => {
  const date = new Date(value || "");
  return Number.isFinite(date.getTime())
    ? `${new Intl.DateTimeFormat("en-GB", {dateStyle: "medium", timeStyle: "short", timeZone: "UTC"}).format(date)} UTC`
    : "Not recorded";
};
const runOutcomes = {running: "In progress", yielded: "Continuation queued", completed: "Completed", partial_error: "Partial errors", error: "Failed", interrupted: "Interrupted"};
const errorTypes = {budget_exceeded: "Target check time budget exceeded; partial availability was discarded", upstream_rejected: "Doctolib rejected the request", throttled: "Rate limited by Doctolib", upstream_error: "Doctolib returned an error", timeout: "Request timed out", connectivity: "Could not reach Doctolib", invalid_metadata: "Booking metadata could not be resolved", malformed_response: "Invalid availability response", incomplete_response: "Availability response was incomplete", unknown: "Check failed for an unknown reason"};
const reasonText = code => typeof code === "string" && /^[A-Za-z0-9_]{1,80}$/.test(code) ? code.replaceAll("_", " ") : "";
const runUrl = (jobId, runId) => `#activity?job=${encodeURIComponent(jobId || "")}&amp;run=${encodeURIComponent(runId || "")}`;

function health(state) {
  const status = state.status, ready = state.load?.status?.phase === "loaded" && status?.database_ready;
  if (state.load?.status?.error && !status) return `<section class="panel activity-health"><div class="panel-header"><h2>Operational signals</h2></div><div class="panel-content"><p class="notice notice-warning" role="status">Status unavailable. ${h(state.load.status.error.message || "Retry to load current signals.")} <button class="text-button" type="button" data-action="activity-retry">Retry</button></p></div></section>`;
  const worker = ready ? status.worker_alive ? "Heartbeat active" : "Heartbeat missing or stale" : "Unavailable";
  const dispatcher = ready ? status.dispatcher_alive ? "Heartbeat active" : "Heartbeat missing or stale" : "Unavailable";
  const lastRun = ready && status.last_completed_run;
  const backlog = ready && status.delivery_backlog || {};
  const count = key => Number.isInteger(backlog[key]) ? backlog[key] : "—";
  return `<section class="panel activity-health" aria-labelledby="activity-health-heading"><div class="panel-header"><h2 id="activity-health-heading">Operational signals</h2></div><div class="panel-content"><div class="activity-health-grid">
    <div><h3>API and database</h3><p>${ready ? "API responding · database ready" : status?.database_ready === false ? "API responding · database unavailable" : "Status is stale or unavailable"}</p></div>
    <div><h3>Checker worker</h3><p>${h(worker)}</p><small>Last completed check: ${lastRun ? `${h(lastRun.job_name)} · ${h(runOutcomes[lastRun.outcome] || "Ended")} · ${h(when(lastRun.finished_at))}` : ready ? "None recorded" : "Unavailable"}</small><small>Overdue active jobs: ${ready && Number.isInteger(status.overdue_jobs) ? status.overdue_jobs : "Unavailable"}</small></div>
    <div><h3>Notification dispatcher</h3><p>${h(dispatcher)}</p><small>Ready ${count("ready")} · retry ${count("retry")} · action required ${count("action_required")} · uncertain ${count("uncertain")}</small><small>Oldest ready delivery: ${ready ? status.oldest_ready_delivery_at ? h(when(status.oldest_ready_delivery_at)) : "None currently eligible" : "Unavailable"}</small></div>
  </div><p class="activity-note">Heartbeats show process liveness. They do not prove a successful check or device receipt.</p></div></section>`;
}

function deliveryStatus(delivery) {
  if (delivery.status === "sent") {
    const accepted = delivery.channel_type === "email" ? "Accepted by SMTP server" : delivery.channel_type === "telegram" ? "Accepted by Telegram API" : "Accepted by endpoint";
    return [accepted, "success"];
  }
  if (delivery.status === "cancelled" || delivery.quiet_state === "cancelled") return ["Cancelled", "paused"];
  if (delivery.delivery_state === "uncertain") return ["Acceptance unknown; automatic resend blocked", "warning"];
  if (delivery.quiet_state === "held") return ["Withheld until quiet hours end", "warning"];
  if (delivery.quiet_state === "needs_manual_check") return ["Action required: run a fresh manual check", "warning"];
  if (delivery.quiet_state === "waiting_for_fresh_check") return ["Waiting for a fresh check", "warning"];
  if (["action_required", "exhausted"].includes(delivery.delivery_state)) return ["Action required", "warning"];
  if (delivery.delivery_state === "retry") return ["Retry scheduled", "warning"];
  if (delivery.status === "pending") return ["Pending delivery", "running"];
  return ["Delivery state unavailable", "warning"];
}

function renderDelivery(delivery, run) {
  const [label, tone] = deliveryStatus(delivery), reason = reasonText(delivery.reason_code);
  const eligible = delivery.next_eligible_at ? ` · Next eligible ${when(delivery.next_eligible_at)}` : "";
  const origin = delivery.originating_run_id && delivery.originating_run_id !== run.id
    ? `<a href="${runUrl(run.job_id, delivery.originating_run_id)}">Observed in run ${h(delivery.originating_run_id.slice(0, 8))}</a>`
    : "Observed in this run";
  const confirmation = delivery.confirmation_run_id && delivery.confirmation_run_id !== delivery.originating_run_id
    ? ` · <a href="${runUrl(run.job_id, delivery.confirmation_run_id)}">Confirmed by run ${h(delivery.confirmation_run_id.slice(0, 8))}</a>` : "";
  const acceptedAt = delivery.accepted_at ? ` · Accepted ${when(delivery.accepted_at)}` : "";
  return `<li class="activity-delivery">${badge(label, tone)} <strong>${h(delivery.channel_name || "Notification destination")}</strong><span>${h(delivery.attempts)} attempt${delivery.attempts === 1 ? "" : "s"}${acceptedAt}${h(eligible)}</span>${reason ? `<small>Reason: ${h(reason)}</small>` : ""}<small>${origin}${confirmation}</small></li>`;
}

function completeNegative(run) {
  return run.outcome === "completed" && run.history_state === "current" && run.snapshot_known && run.coverage_complete &&
    run.target_total > 0 && run.results.length === run.target_total && run.results.every(result => result.status === "no_availability" && result.count_complete && result.published && result.search_revision === run.search_revision);
}

function renderResult(result, run) {
  const name = [result.practitioner_name, result.practice_name].filter(Boolean).join(" · ") || "Target identity unavailable";
  const historical = !result.published || run.history_state !== "current";
  let summary = "Result unavailable";
  if (result.status === "error") summary = errorTypes[result.error_category] || errorTypes.unknown;
  else if (result.status === "available" && result.count_complete) summary = `${result.slot_count} appointment${result.slot_count === 1 ? "" : "s"} found · earliest ${formatSlot(result.earliest_slot, run.time_zone || "Europe/Berlin")}`;
  else if (result.status === "available") summary = "Availability found; total count is incomplete";
  else if (result.status === "no_availability" && result.count_complete) summary = "No appointments in this target’s search window";
  else if (result.status === "no_availability") summary = "No complete availability count recorded";
  return `<li class="activity-target"><div class="activity-target-head"><div><h4>${h(name)}</h4><small>${h(when(result.checked_at))}${result.target_removed ? " · Removed from current job" : ""}</small></div>${badge(result.status === "error" ? "Check error" : result.status === "available" ? "Available" : "No availability", result.status === "error" ? "warning" : result.status === "available" ? "success" : "paused")}</div><p>${h(summary)}${result.upstream_status ? ` (HTTP ${h(result.upstream_status)})` : ""}</p>${historical ? '<small class="activity-historical">Historical evidence only; not current published availability.</small>' : ""}${result.retry_at ? `<small>Retry after ${h(when(result.retry_at))}</small>` : ""}${result.deliveries?.length ? `<ul class="activity-deliveries" aria-label="Notification destinations">${result.deliveries.map(item => renderDelivery(item, run)).join("")}</ul>` : ""}</li>`;
}

function renderMissingTarget(target, run) {
  const name = [target.practitioner_name, target.practice_name].filter(Boolean).join(" · ") || "Target identity unavailable";
  return `<li class="activity-target"><div class="activity-target-head"><div><h4>${h(name)}</h4></div>${badge(["running", "yielded"].includes(run.outcome) ? "Awaiting result" : "No result recorded", ["running", "yielded"].includes(run.outcome) ? "running" : "warning")}</div><p>${["running", "yielded"].includes(run.outcome) ? "This target is awaiting its result in the current check." : "No result was recorded for this target in this run."}</p></li>`;
}

function renderRun(run) {
  const outcome = runOutcomes[run.outcome] || "Outcome unavailable";
  const history = {current: "Current search revision", superseded: "Superseded search revision", historical: "Historical run"}[run.history_state] || "History state unknown";
  const coverage = !run.snapshot_known ? "Target coverage unknown; this run has no known target snapshot."
    : `${run.target_completed} of ${run.target_total ?? "unknown"} targets have results${run.coverage_complete ? " · coverage complete" : " · coverage incomplete"}.`;
  const summary = completeNegative(run) ? `No appointments found across all ${run.target_total} targets.` : coverage;
  const results = Array.isArray(run.results) ? run.results : [];
  const targets = run.snapshot_known && Array.isArray(run.targets) ? run.targets.filter(target => target && target.id) : [];
  const knownTargets = new Set(targets.map(target => target.id));
  const resultByTarget = new Map(results.map(result => [result.target_id, result]));
  const rows = targets.length
    ? [...targets.map(target => resultByTarget.has(target.id) ? renderResult(resultByTarget.get(target.id), run) : renderMissingTarget(target, run)),
      ...results.filter(result => !knownTargets.has(result.target_id)).map(result => renderResult(result, run))]
    : results.map(result => renderResult(result, run));
  return `<article class="panel activity-run" id="run-${h(run.id)}"><div class="panel-header activity-run-header"><div><p class="eyebrow">${h(run.triggered_by?.replaceAll("_", " ") || "Check")}${run.paused_manual ? " · one-time check while paused" : ""}</p><h2>${h(run.job_name || "Deleted job")}</h2></div><div>${badge(outcome, run.outcome === "completed" ? "success" : ["running", "yielded"].includes(run.outcome) ? "running" : "warning")}<small>${h(history)}</small></div></div><div class="panel-content"><dl class="activity-run-meta"><div><dt>Requested</dt><dd>${h(when(run.requested_at))}</dd></div><div><dt>Started</dt><dd>${h(when(run.started_at))}</dd></div><div><dt>Finished</dt><dd>${h(when(run.finished_at))}</dd></div><div><dt>Search revision</dt><dd>${run.search_revision == null ? "Unknown" : h(run.search_revision)}${run.current_search_revision == null ? "" : ` · current ${h(run.current_search_revision)}`}</dd></div></dl><p class="activity-coverage">${h(summary)}</p>${rows.length ? `<ul class="activity-targets">${rows.join("")}</ul>` : `<p class="notice" role="status">${["running", "yielded"].includes(run.outcome) ? "This run awaits target results; completed targets will be preserved between worker turns." : "No target results were saved for this run."}</p>`}</div></article>`;
}

export function renderActivity(state) {
  const activity = state.activity || {}, job = state.jobs?.find(item => item.id === activity.jobId);
  const title = activity.runId ? "Run details" : activity.jobId ? `${job?.name || "Job"} activity` : "Activity";
  const back = activity.runId ? `<a class="button button-secondary button-small" href="#activity${activity.jobId ? `?job=${encodeURIComponent(activity.jobId)}` : ""}">Back to activity</a>` : "";
  const refresh = '<button id="activity-refresh" class="button button-secondary button-small" type="button" data-action="activity-retry">Refresh activity</button>';
  const error = activity.error || state.load?.activity?.error;
  const notice = error ? `<div class="notice notice-warning" role="status"><span>${activity.items?.length ? "Showing previously loaded activity. " : "Activity could not be loaded. "}${h(error.message || "Check the connection and try again.")}</span><button id="activity-retry" class="text-button" type="button" data-action="activity-retry">Retry</button>${error.kind === "auth" ? '<button class="text-button" type="button" data-action="sign-in">Sign in</button>' : ""}</div>` : activity.phase === "loading" && !activity.items?.length ? '<p class="notice" role="status">Loading recent activity…</p>' : "";
  const empty = activity.phase === "loaded" && !activity.items?.length ? '<p class="notice" role="status">No check activity recorded yet.</p>' : "";
  const more = activity.hasMore ? `<div class="activity-more"><button id="activity-load-more" class="button button-secondary" type="button" data-action="activity-more" aria-disabled="${activity.loadingMore ? "true" : "false"}">${activity.loadingMore ? "Loading…" : "Load more"}</button></div>` : "";
  const allActivity = activity.jobId || activity.runId ? '<a class="button button-secondary button-small" href="#activity">All activity</a>' : "";
  return `<section class="activity-page"><div class="page-heading"><div><span class="eyebrow">Check runs and destination outcomes</span><h1 id="activity-heading" tabindex="-1">${h(title)}</h1><p>Shows saved evidence and provider acceptance; acceptance does not confirm device or inbox receipt.</p></div><div class="page-actions">${back}${refresh}${allActivity}</div></div>${health(state)}<div id="activity-load-state">${notice}</div>${empty}<div class="activity-list">${(activity.items || []).map(renderRun).join("")}</div>${more}</section>`;
}

export function shouldRefreshActivity(activity, force = false, routeChanged = false) {
  return force || routeChanged || (!activity?.loadingMore && (activity?.items?.length || 0) <= 25);
}
