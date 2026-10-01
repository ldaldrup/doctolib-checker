const time = value => { const parsed = typeof value === "number" ? value : Date.parse(value || ""); return Number.isFinite(parsed) ? parsed : null; };
const activeTargets = job => (job.targets || []).filter(target => target.active !== 0 && target.active !== false);

export function safeBookingUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || !["doctolib.de", "www.doctolib.de", "doctolib.fr", "www.doctolib.fr"].includes(url.hostname)
      || url.username || url.password || (url.port && url.port !== "443")) return null;
    const parts = url.pathname.split("/").filter(Boolean);
    const booking = parts.indexOf("booking");
    if (booking < 1 || parts[booking + 1] !== "availabilities") return null;
    url.hash = "";
    return url.href;
  } catch { return null; }
}

// Includes search/target identity and scheduler evidence, never list.last_result.
export function historyKey(job) {
  return JSON.stringify([job.id, job.search_revision, job.updated_at, job.last_started_at, job.last_finished_at, job.last_outcome,
    job.date_mode, job.horizon_days, job.earliest_date, job.latest_date, job.time_zone,
    job.insurance_sector, job.telehealth,
    activeTargets(job).map(target => [target.id, target.booking_url, target.last_validated_at])]);
}

function resultsFor(run, targets) {
  const ids = new Set(targets.map(target => target.id));
  const byTarget = new Map();
  for (const result of run.results || []) {
    if (!ids.has(result.target_id)) continue;
    const previous = byTarget.get(result.target_id);
    // The history endpoint orders equal checked_at values by row insertion.
    if (!previous || (time(result.checked_at) ?? -Infinity) >= (time(previous.checked_at) ?? -Infinity)) byTarget.set(result.target_id, result);
  }
  return [...byTarget.values()];
}

export function deriveJobView(job, runs = [], {historyState = "loaded", historyError = null, invalidatedAt = null} = {}) {
  const targets = activeTargets(job);
  const view = {state: "unknown", detected: false, slot: null, warning: null, checkedAt: null,
    runId: null, running: false, stale: historyState === "stale", coverageComplete: false,
    countComplete: false, targetCount: targets.length, historical: false, error: historyError};
  if (!["loaded", "stale"].includes(historyState)) {
    view.warning = historyState === "error" ? "Check history could not be loaded." : null;
    return view;
  }
  const ordered = [...runs].filter(run => run.job_id === job.id).sort((a, b) => (time(b.started_at) ?? -Infinity) - (time(a.started_at) ?? -Infinity));
  if (!ordered.length) { view.state = "never"; view.coverageComplete = true; return view; }
  const latest = ordered[0];
  view.runId = latest.id;
  view.running = latest.outcome === "running";
  // Only persisted revision evidence proves which options a run used. Legacy
  // timestamps cannot establish this, and cosmetic edits do not invalidate it.
  const flag = value => value === true || value === 1;
  const revision = value => Number.isInteger(value) && value > 0;
  const fresh = run => revision(job.search_revision) && flag(run.snapshot_known)
    && run.search_revision === job.search_revision && !!run.search_snapshot;
  const uncertain = !fresh(latest);
  if (uncertain) view.historical = true;
  let snapshot = latest;
  const snapshotTargets = run => fresh(run) ? targets : (run.search_snapshot?.targets || (run.results || []).map(result => ({id: result.target_id})));
  let results = resultsFor(snapshot, snapshotTargets(snapshot));
  // A running/interrupted check with no current-target result has no snapshot.
  // Retain the nearest preceding evidence only as historical. Do not skip a
  // completed negative check and revive an older contradicted detection.
  if (!results.length && ["running", "interrupted"].includes(latest.outcome)) {
    const previous = ordered.slice(1).find(run => run.outcome !== "running" && resultsFor(run, snapshotTargets(run)).length);
    if (previous) {
      snapshot = previous;
      results = resultsFor(snapshot, snapshotTargets(snapshot));
      view.historical = true;
    }
  }
  const known = results.filter(result => ["available", "no_availability"].includes(result.status));
  const eligible = results.filter(result => flag(result.snapshot_known) && flag(result.published) && result.search_revision === job.search_revision);
  const errors = results.filter(result => result.status === "error");
  const missing = Math.max(0, targets.length - results.length);
  view.countComplete = known.length > 0 && known.every(result => result.count_complete === true || result.count_complete === 1);
  view.coverageComplete = targets.length > 0 && known.length === targets.length && snapshot.outcome === "completed" && snapshot === latest && view.countComplete && !uncertain && eligible.length === targets.length;
  const checkedTimes = results.map(result => time(result.checked_at)).filter(value => value !== null);
  view.checkedAt = checkedTimes.length ? new Date(Math.max(...checkedTimes)).toISOString() : null;
  const slots = known.filter(result => result.status === "available" && time(result.earliest_slot) !== null)
    .sort((a, b) => time(a.earliest_slot) - time(b.earliest_slot));
  if (slots.length) {
    const result = slots[0];
    const target = targets.find(item => item.id === result.target_id);
    // Never substitute a different target or construct a booking URL.
    const link = safeBookingUrl(result.booking_url);
    view.slot = {earliest_slot: result.earliest_slot, booking_url: link, target_id: result.target_id,
      practitioner_name: result.practitioner_name || target?.practitioner_name || null,
      practice_name: result.practice_name || target?.practice_name || null,
      checked_at: result.checked_at, run_id: snapshot.id};
    view.detected = true;
    if (!fresh(snapshot) || !eligible.includes(result)) view.historical = true;
  }
  const warnings = [];
  if (uncertain) warnings.push(latest.snapshot_known ? "These historical results use an earlier search revision. Awaiting a fresh check with the saved options." : "The search options used for this historical check are unknown. Awaiting a fresh check.");
  if (view.running) warnings.push(snapshot !== latest ? "Check in progress; showing the preceding check evidence." : "Check in progress; results may be incomplete.");
  if (latest.outcome === "interrupted") warnings.push("The latest check was interrupted.");
  if (["partial_error", "error"].includes(latest.outcome) || errors.length) warnings.push("Some targets could not be checked.");
  if (missing && !view.running) warnings.push("Results are missing for some current targets.");
  if (!uncertain && results.length && eligible.length !== results.length) warnings.push("Some results were not published for the current search; awaiting fresh evidence.");
  if (known.length && !view.countComplete) warnings.push("Availability counts are incomplete.");
  if (view.stale) warnings.push("Showing last loaded check history; refresh failed.");
  view.warning = warnings.length ? warnings.join(" ") : null;
  view.state = view.detected ? "available" : uncertain ? "awaiting" : view.running ? "running"
    : latest.outcome === "error" || (errors.length > 0 && !known.length) ? "error"
    : latest.outcome === "completed" && view.coverageComplete && known.every(result => result.status === "no_availability") ? "no_availability"
    : "partial";
  return view;
}
