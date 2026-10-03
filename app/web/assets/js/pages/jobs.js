import { renderDeliveryNotice } from "../delivery-view.js";
import { badge, escapeHtml as h, formatGermanDate, formatSlot, icon } from "../ui.js";
const pollInterval = seconds => seconds % 3600 === 0 ? `${seconds / 3600} hr` : seconds % 60 === 0 ? `${seconds / 60} min` : `${seconds} sec`;

export function emptyDraft(settings) {
  return { name: "", target_urls: [""], interval_seconds: settings.default_interval_seconds,
    date_mode: "first_available", horizon_days: 15, earliest_date: "", latest_date: "",
    time_zone: settings.time_zone, insurance_sector: "public", telehealth: false,
    telegram_enabled: false, notification_channel_ids: [] };
}
const message = error => error?.message || String(error || "Request failed. Try again.");
const targets = job => (job.targets || []).filter(target => target.active !== false && target.active !== 0);
const viewFor = (state, job) => state.views?.get(job.id);
const complete = state => state.jobs !== null && state.load?.jobs?.phase === "loaded";

export function filteredJobs(state) {
  const query = (state.query || "").trim().toLocaleLowerCase();
  return (state.jobs || []).filter(job => {
    if (state.filter === "slot" ? !viewFor(state, job)?.detected
      : state.filter !== "all" && job.status !== state.filter) return false;
    if (state.intervalFilter !== "all" && job.interval_seconds !== Number(state.intervalFilter)) return false;
    return !query || [job.name, ...targets(job).flatMap(target => [target.practitioner_name, target.practice_name, target.motive_name])]
      .some(term => String(term || "").toLocaleLowerCase().includes(query));
  });
}

export function renderJobsLoadState(state) {
  const load = state.load?.jobs || {};
  if (load.error) return `<div class="notice notice-warning" role="status"><span>${state.jobs !== null ? "Showing previously loaded jobs. " : "Jobs could not be loaded. "}${h(message(load.error))}</span><button class="text-button" type="button" data-action="refresh">Retry</button>${load.error.kind === "auth" ? '<button class="text-button" type="button" data-action="sign-in">Sign in</button>' : ""}</div>`;
  if (!complete(state)) return `<div class="notice" role="status">${icon("info")}<span>Loading jobs${state.jobs?.length ? ` (${state.jobs.length} received)` : ""}. Counts and filters use an incomplete snapshot until loading finishes.</span></div>`;
  return "";
}

export function renderJobCounts(state) {
  const jobs = state.jobs || [];
  const count = predicate => complete(state) ? jobs.filter(predicate).length : "—";
  const viewsResolved = complete(state) && jobs.every(job => {
    const view = viewFor(state, job);
    return view && !view.stale && !view.error && view.state !== "unknown";
  });
  return [["all", "All", complete(state) ? jobs.length : "—"], ["active", "Enabled", count(job => job.status === "active")],
    ["paused", "Paused", count(job => job.status === "paused")], ["slot", "Slot", viewsResolved ? jobs.filter(job => viewFor(state, job)?.detected).length : "—"]]
    .map(([key, label, total]) => `<button type="button" data-action="filter" data-filter="${key}" aria-pressed="${state.filter === key}" class="${key === "slot" ? "filter-slot" : ""}">${key === "slot" ? '<span class="filter-slot-dot" aria-hidden="true"></span>' : ""}${label} (${total})</button>`).join("");
}

export function compactDuration(seconds) {
  const total = Math.max(0, Math.ceil(seconds));
  const units = [[86400, "d"], [3600, "h"], [60, "m"], [1, "s"]];
  const index = units.findIndex(([size]) => total >= size);
  if (index < 0) return "0s";
  return units.slice(index, index + 2).map(([size, suffix], offset) => {
    const amount = Math.floor((offset ? total % units[index][0] : total) / size);
    return amount ? `${amount}${suffix}` : "";
  }).join("");
}

export function nextCheck(job, state, now = Date.now()) {
  if (job.status === "paused") return "paused";
  if (state.load?.status?.phase !== "loaded") return "unknown";
  if (!state.status?.worker_alive) return "blocked";
  if (viewFor(state, job)?.running) return "checking";
  const due = Date.parse(job.next_check_at || "");
  return !Number.isFinite(due) ? "unknown" : due <= now ? "due" : compactDuration((due - now) / 1000);
}

export function nextCheckTitle(job) {
  const due = Date.parse(job.next_check_at || "");
  if (job.status === "paused") return "No check scheduled while paused.";
  if (!Number.isFinite(due)) return "Next check time unavailable.";
  const zone = job.time_zone || "Europe/Berlin";
  try {
    const formatted = new Intl.DateTimeFormat("de-DE", {timeZone: zone, day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false}).format(new Date(due));
    return `Scheduled next check: ${formatted} (${zone})`;
  } catch { return `Scheduled next check: ${new Date(due).toISOString()} (UTC)`; }
}

export function checkedAgo(value, now = Date.now()) {
  const checked = Date.parse(value || "");
  if (!Number.isFinite(checked)) return null;
  const seconds = Math.max(0, Math.floor((now - checked) / 1000));
  const units = [[31536000, "y"], [86400, "d"], [3600, "h"], [60, "m"], [1, "s"]];
  const [size, suffix] = units.find(([size]) => seconds >= size) || units[units.length - 1];
  return `Checked ${Math.floor(seconds / size)}${suffix} ago`;
}

// Intent/run identity proves progress. A recent last_finished_at alone does not
// prove that a user's requested revision was checked.
export function checkIntentFeedback(job, state, now = Date.now()) {
  if (state.pendingJobs?.has(job.id) && state.checkSubmitting?.has(job.id)) return "Requesting a check…";
  const intent = job.check_intent;
  if (!intent) return null;
  const revision = intent.search_revision;
  if (intent.status === "cancelled") return intent.cancel_reason === "search_edited"
    ? "The saved search changed. The previous check request was cancelled; select Check once for the new search."
    : "The check request was cancelled.";
  if (revision !== job.search_revision) return "The requested check belongs to an earlier search revision; awaiting current evidence.";
  if (intent.status === "queued") {
    const eligible = Date.parse(intent.eligible_at || "");
    const queue = Number.isFinite(eligible) && eligible > now
      ? `Check queued; cooldown ends in ${compactDuration((eligible - now) / 1000)} (${formatSlot(intent.eligible_at, job.time_zone)}).`
      : "Check queued for the earliest available worker turn.";
    return `${intent.triggered_by === "search_edit" ? "Saved search: fresh check requested. " : ""}${queue}${state.load?.status?.phase !== "loaded" ? " Worker status is unknown; the request remains saved." : !state.status?.worker_alive ? " Worker unavailable; the request remains saved." : ""}`;
  }
  if (intent.status === "running" && intent.run_id) {
    const progress = job.status === "paused" ? "Checking once; the job remains paused. Matching slots can notify the selected channel." : "Requested check in progress.";
    return `${progress}${state.load?.status?.phase !== "loaded" ? " Worker status is unknown; this is the last saved running claim." : !state.status?.worker_alive ? " Worker unavailable; this is the last saved running claim and may need reconciliation." : ""}`;
  }
  if (intent.status === "completed" && intent.run_id) {
    const labels = {completed: "Requested check completed.", partial_error: "Requested check completed with some target errors.", error: "Requested check failed.", interrupted: "Requested check was interrupted."};
    return `${labels[intent.outcome] || "Requested check ended; load check history for its outcome."}${job.status === "paused" ? " The job remains paused." : ""}`;
  }
  return "Check request saved; awaiting run evidence.";
}

export function savedJobFeedback(previous, saved) {
  if (previous && saved.search_revision !== previous.search_revision) return saved.status === "paused"
    ? "Job saved. Monitoring remains paused; select Check once to check the changed search. Any obsolete pending check request was cancelled."
    : "Job saved. A fresh check was requested for the saved search; the worker and shared request spacing determine when it starts.";
  return "Job saved.";
}

export function jobStatus(job, state, now = Date.now()) {
  const view = viewFor(state, job);
  if (job.status === "paused") return {label: "Paused", tone: "paused", detail: "Checks are paused"};
  if (state.load?.status?.phase !== "loaded") return {label: "Status unknown", tone: "warning", detail: state.load?.status?.error ? "Worker status unavailable" : "Checking worker status…"};
  if (!state.status?.worker_alive) return {label: "Blocked", tone: "warning", detail: "Worker unavailable"};
  if (view?.error) return {label: "Status unknown", tone: "warning", detail: "Check history unavailable"};
  if (view?.state === "error") return {label: "Check failed", tone: "warning", detail: "Targets could not be checked"};
  if (view?.state === "partial") return {label: "Check incomplete", tone: "warning", detail: "Some target results are missing or failed"};
  const labels = {unknown: "Loading check history…", never: "Awaiting first check", awaiting: "Awaiting a fresh check", running: "Check in progress", no_availability: "Awaiting a check", available: "Historical slot detection"};
  return {label: "Monitoring", tone: "running", detail: checkedAgo(view?.checkedAt, now) || labels[view?.state || "unknown"]};
}

function card(job, state) {
  const view = viewFor(state, job);
  const list = targets(job);
  const detected = view?.detected && view.slot;
  const pending = state.pendingJobs?.has(job.id);
  const status = jobStatus(job, state);
  const checkFeedback = checkIntentFeedback(job, state);
  const range = job.date_mode === "custom" ? `${h(formatGermanDate(job.earliest_date))} – ${h(formatGermanDate(job.latest_date))}` : `Next ${h(job.horizon_days)} days`;
  return `<article class="panel job-card ${detected ? "job-card-last-detected" : ""}" data-job-id="${h(job.id)}" aria-busy="${Boolean(pending)}">
    ${detected ? `<div class="job-hero"><span class="job-hero-label">${icon("bellActive")} Historical slot detection</span><div class="job-hero-actions"><span class="slot-pill">${icon("calendar")} ${h(formatSlot(view.slot.earliest_slot, job.time_zone))}</span>${view.slot.booking_url ? `<a class="button button-primary button-small" href="${h(view.slot.booking_url)}" target="_blank" rel="noopener noreferrer" aria-label="Open detected target on Doctolib"><span>Open on Doctolib</span>${icon("external")}</a>` : ""}</div></div>` : ""}
    <div class="job-body"><div class="job-top"><div class="job-summary"><div class="job-title"><h2>${h(job.name)}</h2><span class="job-count">${list.length} target${list.length === 1 ? "" : "s"}</span></div>
      <div class="target-chips">${list.map(target => `<span class="target-chip ${target.id === view?.slot?.target_id ? "target-chip-found" : ""}"><span><strong>${h(target.practice_name || "Practice unavailable")}</strong><span>${h([target.practitioner_name, target.motive_name].filter(Boolean).join(" · ") || "Target identity unavailable")}</span></span></span>`).join("")}</div></div>
      <div class="job-state">${badge(status.label, status.tone)}<small>${h(status.detail)}</small></div></div>
      ${detected ? `<p class="job-evidence">${h(view.slot.practitioner_name || view.slot.practice_name || "Detected target")}. Availability may have changed; opening the link does not reserve a slot.</p>` : ""}
      ${job.status === "paused" && detected ? '<p class="job-evidence">Monitoring is paused; this detection is from a previous check.</p>' : ""}
      ${view?.warning ? `<p class="notice notice-warning">${icon("warning")}<span>${h(view.warning)}</span></p>` : ""}
      ${checkFeedback ? `<p class="job-check-feedback" role="status">${h(checkFeedback)}</p>` : ""}
      ${state.checkErrors?.has(job.id) ? `<p class="notice notice-warning" role="status">${h(state.checkErrors.get(job.id))}</p>` : ""}
      ${(job.notification_channels || []).length ? `<ul class="channel-deliveries" aria-label="Notification destination status">${job.notification_channels.map(channel => `<li>${h(channel.name)}: ${h(channel.delivery_status === "sent" && channel.type === 'email' ? 'accepted by SMTP server' : channel.delivery_status === "sent" && ["ntfy", "webhook"].includes(channel.type) ? "accepted by endpoint" : channel.delivery_status || (channel.enabled ? channel.usable ? "ready" : "incomplete" : "disabled"))}${channel.error_code ? ` (${h(channel.error_code)})` : ""}</li>`).join("")}</ul>` : ""}
      <div class="job-meta"><span>Range: <strong>${range}</strong></span><span class="meta-dot">•</span><span>Interval: <strong>${h(compactDuration(job.interval_seconds))}</strong> <span title="${h(nextCheckTitle(job))}">(next: ${h(nextCheck(job, state))})</span></span><span class="meta-dot">•</span><span>Alert: <strong>${job.telegram_enabled ? h((job.notification_channels || []).map(channel => channel.name).join(", ") || "No selected channel") : "Off"}</strong></span></div>
      <div class="job-actions">${[["check-now", "bolt", job.status === "paused" ? "Check once" : "Check now"], ...(job.status === "paused" && ["queued", "running"].includes(job.check_intent?.status) ? [["pause", "pause", "Stop check"]] : []), [job.status === "paused" ? "resume" : "pause", job.status === "paused" ? "play" : "pause", job.status === "paused" ? "Resume" : "Pause"], ["edit", "edit", "Edit"], ["delete", "trash", "Delete"]].map(([action, symbol, label]) => `<button class="button button-quiet" type="button" data-action="${action}" aria-label="${label} ${h(job.name)}" ${pending || (action === "edit" && state.uncertainCreate) || (action === "check-now" && job.check_intent?.status === "queued") ? "disabled" : ""}>${icon(symbol)}<span class="action-label">${label}</span></button>`).join("")}</div>
    </div></article>`;
}

export function renderJobList(state) {
  if (state.jobs === null) return `<div class="empty-state"><h2>${state.load?.jobs?.error ? "Jobs unavailable" : "Loading jobs"}</h2><p>${state.load?.jobs?.error ? "Refresh to try loading saved jobs again." : "Retrieving saved jobs from the server."}</p></div>`;
  const jobs = filteredJobs(state);
  if (!jobs.length) return `<div class="empty-state">${icon("search")}<h2>${!complete(state) ? "Waiting for jobs" : state.jobs.length ? "No matching jobs" : "No jobs yet"}</h2><p>${!complete(state) ? "The collection is not fully loaded." : state.filter === "slot" && state.jobs.some(job => !viewFor(state, job) || viewFor(state, job).state === "unknown") ? "Check histories are loading; slot matches are not yet known." : state.jobs.length ? "Try a different search or filter." : "Create a job with a complete Doctolib booking URL to start monitoring."}</p></div>`;
  return jobs.map(job => card(job, state)).join("");
}

export function renderTargetMetadata(url, state) {
  const entry = state.targetMetadata?.get(url.trim());
  if (entry?.phase === "loading") return '<span role="status">Validating target…</span>';
  if (entry?.error) return `<span class="form-error" role="status">${h(message(entry.error))}</span>`;
  if (!entry?.data) return "";
  return `<span>${h([entry.data.practice_name, entry.data.practitioner_name].filter(Boolean).join(" · ") || "Target identity unavailable")}</span>${entry.data.motive_name ? `<span>${h(entry.data.motive_name)}</span>` : ""}`;
}

function targetRows(draft, state) {
  return draft.target_urls.map((url, index) => `<div class="target-entry"><div class="target-row"><label class="visually-hidden" for="target-${index}">Target URL ${index + 1}</label><div class="target-input-wrap">${icon("link")}<input class="input" id="target-${index}" name="target_urls" type="url" inputmode="url" value="${h(url)}" placeholder="Paste a complete booking URL" aria-describedby="target-meta-${index}" required></div><button class="icon-button" type="button" data-action="remove-target" data-index="${index}" aria-label="Remove target ${index + 1}" ${draft.target_urls.length === 1 ? "disabled" : ""}>${icon("close")}</button></div><div class="target-meta" id="target-meta-${index}">${renderTargetMetadata(url, state)}</div></div>`).join("");
}

export function sameDraftValue(key, left, right) {
  if (["interval_seconds", "horizon_days"].includes(key)) return Number(left) === Number(right);
  if (["earliest_date", "latest_date"].includes(key)) return (left || "") === (right || "");
  return JSON.stringify(left) === JSON.stringify(right);
}
export function updateConflictFields(state) {
  if (!state.jobConflict || !state.original) return {};
  const prior = {...state.original, target_urls: state.original.targets.map(target => target.booking_url)};
  const server = {...state.jobConflict, target_urls: state.jobConflict.targets.map(target => target.booking_url)};
  return Object.fromEntries(Object.entries(state.formDraft).filter(([key, draft]) => Object.hasOwn(prior, key) && !sameDraftValue(key, draft, prior[key]) && !sameDraftValue(key, server[key], prior[key]) && !sameDraftValue(key, draft, server[key])).map(([key, draft]) => [key, {draft, server: server[key]}]));
}

function form(state) {
  if (!state.settings || !state.formDraft) return `<section class="panel setup-panel" id="job-setup"><div class="panel-content"><h2>New Job</h2><p>Server defaults must load before creating a job.</p>${state.load?.settings?.error ? `<p class="form-error">${h(message(state.load.settings.error))}</p><button class="button button-secondary" data-action="refresh" type="button">Retry</button>` : '<p role="status">Loading defaults…</p>'}</div></section>`;
  const draft = state.formDraft, editing = Boolean(state.editingId), min = state.settings.minimum_poll_interval_seconds;
  const presets = [300, 600, 900, 1800].filter(value => value >= min);
  const custom = !presets.includes(Number(draft.interval_seconds));
  return `<section class="panel setup-panel" id="job-setup" aria-labelledby="setup-heading"><div class="panel-header"><div class="setup-title">${icon("bolt")}<h2 id="setup-heading">${editing ? "Edit Job" : "Quick Setup Job"}</h2></div></div><div class="panel-content"><form id="job-form" class="setup-form" aria-describedby="job-form-error" aria-busy="${Boolean(state.jobSubmitting)}"><fieldset class="form-fields" ${state.jobSubmitting || state.uncertainCreate ? "disabled" : ""}>
    <div class="field"><label for="job-name">Job name</label><input class="input" id="job-name" name="name" maxlength="120" value="${h(draft.name)}" placeholder="Job name" required></div>
    <div class="form-section"><div class="target-heading"><span class="field-label">Practitioners &amp; practice links</span></div><div class="target-list" id="target-list">${targetRows(draft, state)}</div><button class="button add-target" type="button" data-action="add-target" ${draft.target_urls.length >= 100 ? "disabled" : ""}>${icon("plus")} Add another URL</button></div>
    <fieldset class="field date-mode"><legend>When do you need the appointment?</legend><div class="date-mode-options">${[["first_available", "First available"], ["custom", "Custom date range"]].map(([value, label]) => `<label><input type="radio" name="date_mode" value="${value}" ${draft.date_mode === value ? "checked" : ""}> ${label}</label>`).join("")}</div></fieldset>
    <div class="field" id="horizon-fields" ${draft.date_mode === "custom" ? "hidden" : ""}><label for="horizon-days">Search window in days</label><input class="input" id="horizon-days" name="horizon_days" type="number" min="1" max="365" step="1" value="${h(draft.horizon_days)}" ${draft.date_mode === "first_available" ? "required" : "disabled"}></div>
    <div class="field-row" id="custom-date-fields" ${draft.date_mode !== "custom" ? "hidden" : ""}>${[["earliest", "Earliest date"], ["latest", "Latest date"]].map(([key, label]) => `<div class="field"><label for="${key}-date">${label}</label><input class="input" id="${key}-date" name="${key}_date" type="date" value="${h(draft[`${key}_date`])}" ${draft.date_mode === "custom" ? "required" : "disabled"}></div>`).join("")}</div>
    <fieldset class="field"><legend>Polling interval</legend><div class="choice-row interval-choices">${presets.map(seconds => `<label class="choice"><input type="radio" name="interval_choice" value="${seconds}" ${Number(draft.interval_seconds) === seconds ? "checked" : ""}><span>${h(pollInterval(seconds))}</span></label>`).join("")}<label class="choice"><input type="radio" name="interval_choice" value="custom" ${custom ? "checked" : ""}><span>Custom</span></label></div><div class="field" id="custom-interval-fields" ${custom ? "" : "hidden"}><label for="custom-job-interval">Seconds</label><input class="input" id="custom-job-interval" name="custom_interval_seconds" type="number" min="${h(min)}" max="86400" step="1" value="${h(draft.interval_seconds)}" ${custom ? "required" : "disabled"}></div></fieldset>
    <div class="field"><label for="insurance-sector">Insurance sector</label><select class="select" id="insurance-sector" name="insurance_sector">${[["public", "Public"], ["private", "Private"]].map(([value, label]) => `<option value="${value}" ${draft.insurance_sector === value ? "selected" : ""}>${label}</option>`).join("")}</select></div>
    <div class="form-section"><span class="field-label">Appointment options</span><label class="checkbox-line"><span class="checkbox-copy">${icon("video")} Telehealth appointments</span><input name="telehealth" type="checkbox" ${draft.telehealth ? "checked" : ""}></label></div>
    <div class="form-section"><span class="field-label">Notifications</span><label class="checkbox-line"><span class="checkbox-copy">${icon("send")} Send matching appointment alerts</span><input name="telegram_enabled" type="checkbox" ${draft.telegram_enabled ? "checked" : ""}></label><fieldset id="job-channel-options"><legend>Selected notification channels</legend>${(state.channels || []).map(channel => `<label class="checkbox-line"><span>${h(channel.name)} · ${h({telegram: "Telegram", ntfy: "ntfy", webhook: "HTTPS webhook", email:'Email'}[channel.type] || "Telegram")}${channel.enabled && channel.usable ? "" : channel.enabled ? " (incomplete)" : " (disabled)"}</span><input name="notification_channel_ids" value="${h(channel.id)}" type="checkbox" ${(draft.notification_channel_ids || []).includes(channel.id) ? "checked" : ""}></label>`).join("")}${state.channels === null ? '<p>Loading notification channels…</p>' : !(state.channels || []).length ? '<p class="muted">Add channels in Settings. An empty selection sends no alerts.</p>' : ""}</fieldset><p class="muted">Routing changes apply to newly observed appointment episodes.</p></div>
    </fieldset><p id="job-form-error" class="form-error" role="alert" ${state.jobError ? "" : "hidden"}>${state.jobError ? h(message(state.jobError)) : ""}</p>
    ${state.uncertainCreate ? '<div class="notice notice-warning"><span>The saved create request is unresolved. Retry uses the same request and key; keep this page open until its result is confirmed.</span><button class="text-button" type="button" data-action="refresh">Refresh</button><button class="text-button" type="button" data-action="retry-create">Retry saved request</button></div>' : ""}
    ${state.jobConflictBlocked && !state.jobConflict ? '<div class="notice notice-warning"><span>The job changed on the server. Your draft is preserved; fetch its latest version to reconcile.</span><button class="text-button" type="button" data-action="refetch-job-conflict">Fetch latest version</button></div>' : ""}
    ${state.jobConflict ? `<div class="notice notice-warning" role="status"><div><p>This job changed on the server. Your draft is preserved. Choose a value for each conflicting field, then review the merged form before saving.</p>${Object.entries(updateConflictFields(state)).map(([key, value]) => `<fieldset><legend>${h(key.replaceAll("_", " "))}</legend><label><input type="radio" name="job-reconcile-${h(key)}" value="server"> Server: ${h(JSON.stringify(value.server))}</label><label><input type="radio" name="job-reconcile-${h(key)}" value="draft"> My draft: ${h(JSON.stringify(value.draft))}</label></fieldset>`).join("")}<button class="text-button" type="button" data-action="reconcile-job">Review merged draft</button></div></div>` : ""}
    <button class="button button-primary" type="submit" ${state.jobSubmitting || state.uncertainCreate || state.jobConflictBlocked || (!editing && !complete(state)) ? "disabled" : ""}>${icon(editing ? "check" : "plus")} ${state.jobSubmitting ? "Saving…" : editing ? "Save Changes" : "Create Job"}</button>${editing ? '<button class="button button-secondary" type="button" data-action="cancel-edit">Cancel edit</button>' : ""}</form></div></section>`;
}

export function renderJobs(state) {
  const intervals = [...new Set((state.jobs || []).map(job => job.interval_seconds))].sort((a, b) => a - b);
  return `<div class="page-heading"><div><h1>Jobs</h1><p>Monitor appointment availability from saved Doctolib booking links.</p></div><div class="page-actions"><button class="button button-primary" type="button" data-action="new-job" ${state.settings && complete(state) && !state.uncertainCreate ? "" : "disabled"}>${icon("plus")} New Job</button></div></div>
    <div id="delivery-status">${renderDeliveryNotice(state)}</div><div id="jobs-load-state">${renderJobsLoadState(state)}</div><div class="panel filter-toolbar"><div class="search-wrap">${icon("search")}<label class="visually-hidden" for="job-search">Filter jobs</label><input class="input" id="job-search" type="search" value="${h(state.query)}" placeholder="Filter by job, practitioner, practice, or motive"></div><div class="filter-controls"><div id="job-counts" class="filter-tabs" role="group" aria-label="Filter by job status">${renderJobCounts(state)}</div><label class="visually-hidden" for="interval-filter">Filter by polling interval</label><select class="select interval-filter" id="interval-filter"><option value="all">All intervals</option>${intervals.map(seconds => `<option value="${seconds}" ${state.intervalFilter === String(seconds) ? "selected" : ""}>${h(pollInterval(seconds))}</option>`).join("")}</select></div></div><div class="jobs-layout"><div class="jobs-list" id="job-list">${renderJobList(state)}</div>${form(state)}</div>`;
}
