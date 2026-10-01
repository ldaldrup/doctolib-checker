import { api, createJobPayload, updateJobPayload, settingsPayload } from "./api.js";
import { deriveJobView, historyKey, safeBookingUrl } from "./job-view.js";
import { emptyDraft, renderJobList, renderJobs, renderTargetMetadata } from "./pages/jobs.js";
import { renderSettings, settingWarning } from "./pages/settings.js";

const app = document.querySelector("#app"), toast = document.querySelector("#toast"), dialog = document.querySelector("#confirm-dialog");
const state = {
  jobs: null, settings: null, status: null,
  load: Object.fromEntries(["jobs", "settings", "status"].map(key => [key, {phase: "loading", error: null}])),
  views: new Map(), histories: new Map(), targetMetadata: new Map(),
  formDraft: null, editingId: null, original: null, pendingJobs: new Set(),
  jobSubmitting: false, settingsSubmitting: false, jobError: null, settingsError: null,
  settingsDraft: null, settingsDirty: false, remoteSettingsChanged: false,
  query: "", filter: "all", intervalFilter: "all", uncertainCreate: null
};
let toastTimer, refreshTimer, refreshPromise, refreshAgain = false, refreshSettingsAgain = false, failures = 0, observer, deleteFocus;
let historyActive = 0, settingsGeneration = 0, historyEpoch = 0;
const historyQueue = [], historyQueued = new Set(), searchSignatures = new Map(), invalidations = new Map();
const route = () => location.hash === "#settings" ? "settings" : "jobs";
const message = error => error?.message || "The request failed.";
const settingsValues = settings => ({default_interval_seconds: settings.default_interval_seconds, request_spacing_seconds: settings.request_spacing_seconds});

function announce(value) {
  clearTimeout(toastTimer); toast.textContent = value; toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 6500);
}
function controlFocus(element = document.activeElement) {
  return {id: element?.id, action: element?.dataset.action, job: element?.closest("[data-job-id]")?.dataset.jobId, filter: element?.dataset.filter, href: element?.getAttribute("href")};
}
function restoreControl(focus) {
  let element = focus.id ? document.getElementById(focus.id) : null;
  if (!element && focus.action) element = [...document.querySelectorAll("[data-action]")].find(candidate => candidate.dataset.action === focus.action
    && (!focus.job || candidate.closest("[data-job-id]")?.dataset.jobId === focus.job)
    && (!focus.filter || candidate.dataset.filter === focus.filter));
  if (!element && focus.href && focus.job) element = [...document.querySelectorAll("[data-job-id] a")].find(candidate => candidate.getAttribute("href") === focus.href && candidate.closest("[data-job-id]")?.dataset.jobId === focus.job);
  element?.focus({preventScroll: true});
  return Boolean(element);
}
function render() {
  const control = controlFocus();
  const focused = document.activeElement;
  const focusId = focused?.id, start = focused?.selectionStart, end = focused?.selectionEnd;
  const scroll = window.scrollY;
  app.innerHTML = route() === "jobs" ? renderJobs(state) : renderSettings(state);
  document.querySelectorAll("[data-nav]").forEach(link => {
    if (link.dataset.nav === route()) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  });
  document.title = `${route() === "jobs" ? "Jobs" : "Settings"} · DoctolibChecker`;
  workerStatus(); observeCards();
  if (focusId) {
    const next = document.getElementById(focusId); next?.focus({preventScroll: true});
    if (start !== null && start !== undefined && next?.setSelectionRange) { try { next.setSelectionRange(start, end); } catch {} }
  }
  if (!focusId) restoreControl(control);
  window.scrollTo({top: scroll, behavior: "instant"});
}
function workerStatus() {
  const element = document.querySelector("#worker-status"); if (!element) return;
  const load = state.load.status;
  const auth = Object.values(state.load).some(value => value.error?.kind === "auth") || state.jobError?.kind === "auth" || state.settingsError?.kind === "auth";
  const signIn = document.getElementById("sign-in"); if (signIn) signIn.hidden = !auth;
  element.classList.toggle("worker-unknown", load.phase !== "loaded" || !state.status?.worker_alive);
  element.textContent = load.phase === "loading" && !state.status ? "Worker status loading…"
    : load.phase === "error" ? "API unavailable · worker unknown"
    : load.phase === "stale" ? `Last observed: worker ${state.status?.worker_alive ? "online" : "unavailable"} · stale`
    : state.status?.worker_alive ? "Worker online" : "Worker unavailable";
}
function patchJobs() {
  workerStatus(); if (route() !== "jobs") return;
  const focus = controlFocus();
  const list = document.querySelector("#job-list");
  if (list) {
    const content = document.createElement("template"); content.innerHTML = renderJobList(state);
    const existing = new Map([...list.children].filter(node => node.dataset.jobId).map(node => [node.dataset.jobId, node]));
    let position = list.firstElementChild;
    for (const candidate of [...content.content.children]) {
      const previous = existing.get(candidate.dataset.jobId);
      const node = previous?.outerHTML === candidate.outerHTML ? previous : candidate;
      if (node !== position) list.insertBefore(node, position);
      position = node.nextElementSibling;
      existing.delete(candidate.dataset.jobId);
    }
    while (position) { const next = position.nextElementSibling; position.remove(); position = next; }
  }
  const template = document.createElement("template"); template.innerHTML = renderJobs(state);
  for (const id of ["job-counts", "jobs-load-state"]) {
    const old = document.getElementById(id), next = template.content.querySelector(`#${id}`);
    if (old && next && old.innerHTML !== next.innerHTML) old.innerHTML = next.innerHTML;
  }
  const interval = document.getElementById("interval-filter"), nextInterval = template.content.querySelector("#interval-filter");
  if (interval && nextInterval) {
    if (interval.innerHTML !== nextInterval.innerHTML) interval.innerHTML = nextInterval.innerHTML;
    // A saved filter remains selectable even if its last job was removed.
    if (state.intervalFilter !== "all" && ![...interval.options].some(option => option.value === state.intervalFilter)) {
      const option = document.createElement("option"); option.value = state.intervalFilter;
      option.textContent = `${state.intervalFilter} sec`; interval.append(option);
    }
    interval.value = state.intervalFilter;
  }
  const canCreate = Boolean(state.settings) && state.load.jobs.phase === "loaded";
  const newJob = document.querySelector('[data-action="new-job"]'); if (newJob) newJob.disabled = !canCreate || state.jobSubmitting;
  const submit = document.querySelector('#job-form [type="submit"]'); if (submit) submit.disabled = !canCreate || state.jobSubmitting || Boolean(state.uncertainCreate);
  observeCards(); restoreControl(focus);
}
function updateViews() {
  for (const job of state.jobs || []) {
    const signature = JSON.stringify([job.date_mode, job.horizon_days, job.earliest_date, job.latest_date, job.time_zone, job.insurance_sector, job.telehealth, job.targets?.map(target => [target.id, target.booking_url])]);
    if (searchSignatures.has(job.id) && searchSignatures.get(job.id) !== signature) invalidations.set(job.id, new Date().toISOString());
    searchSignatures.set(job.id, signature);
    const entry = state.histories.get(job.id);
    const matching = entry?.key === historyKey(job);
    state.views.set(job.id, deriveJobView(job, matching ? entry.runs : [], {historyState: matching ? entry.phase : "loading", historyError: entry?.error, invalidatedAt: invalidations.get(job.id)}));
  }
}
function queueHistory(job) {
  const key = historyKey(job), entry = state.histories.get(job.id);
  if (historyQueued.has(job.id)) return;
  if (entry?.key === key && (entry.epoch === historyEpoch || entry.phase === "loading" || (entry.phase === "loaded" && Date.now() - entry.loadedAt < 15000)
    || (["error", "stale"].includes(entry.phase) && !entry.retryEligible))) return;
  historyQueued.add(job.id); historyQueue.push({id: job.id, key}); pumpHistory();
}
function pumpHistory() {
  while (historyActive < 3 && historyQueue.length && !document.hidden && route() === "jobs") {
    const item = historyQueue.shift(), job = state.jobs?.find(value => value.id === item.id);
    if (!job || historyKey(job) !== item.key) { historyQueued.delete(item.id); continue; }
    const previous = state.histories.get(item.id);
    const priorRuns = previous?.key === item.key ? previous.runs : [];
    const epoch = historyEpoch;
    historyActive++; state.histories.set(item.id, {key: item.key, phase: previous?.phase === "loaded" && previous.key === item.key ? "loaded" : priorRuns?.length ? "stale" : "loading", runs: priorRuns || [], retryEligible: false, epoch});
    api.getChecks(item.id, {limit: 10}).then(runs => {
      if (historyKey(state.jobs?.find(value => value.id === item.id) || {}) !== item.key) return;
      state.histories.set(item.id, {key: item.key, phase: "loaded", runs, error: null, loadedAt: Date.now(), epoch});
    }).catch(error => {
      const current = state.jobs?.find(value => value.id === item.id);
      if (current && historyKey(current) === item.key) state.histories.set(item.id, {key: item.key, phase: priorRuns?.length ? "stale" : "error", runs: priorRuns || [], error, retryEligible: false, epoch});
    }).finally(() => {
      historyActive--; historyQueued.delete(item.id); updateViews(); patchJobs(); pumpHistory();
    });
  }
}
function observeCards() {
  observer?.disconnect(); if (route() !== "jobs") return;
  if (typeof IntersectionObserver === "function") {
    observer = new IntersectionObserver(entries => entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      const job = state.jobs?.find(value => value.id === entry.target.dataset.jobId); if (job) queueHistory(job);
      observer.unobserve(entry.target);
    }), {rootMargin: "100px"});
    document.querySelectorAll("[data-job-id]").forEach(card => observer.observe(card));
  } else (state.jobs || []).forEach(queueHistory);
  // The Slot count is displayed for all jobs, so it needs full coverage through the same bounded queue.
  if (state.load.jobs.phase === "loaded") (state.jobs || []).forEach(queueHistory);
}
async function loadJobs() {
  historyEpoch++;
  for (const entry of state.histories.values()) if (["error", "stale"].includes(entry.phase)) entry.retryEligible = true;
  if (state.jobs === null) { state.load.jobs = {phase: "loading", error: null}; patchJobs(); }
  const collected = new Map();
  try {
    for (let offset = 0; ; offset += 100) {
      const page = await api.listJobs({limit: 100, offset});
      for (const job of page) collected.set(job.id, job);
      if (page.length < 100) break;
    }
    state.jobs = [...collected.values()]; state.load.jobs = {phase: "loaded", error: null};
    updateViews(); reconcileCreate();
  } catch (error) { state.load.jobs = {phase: state.jobs ? "stale" : "error", error}; }
  patchJobs();
}
async function loadStatus() {
  try { state.status = await api.getStatus(); state.load.status = {phase: "loaded", error: null}; }
  catch (error) { state.load.status = {phase: state.status ? "stale" : "error", error}; }
  patchJobs();
}
async function loadSettings() {
  if (state.settingsSubmitting) return;
  const generation = ++settingsGeneration;
  try {
    const settings = await api.getSettings();
    if (generation !== settingsGeneration) return;
    if (state.settingsDirty && state.settings && JSON.stringify(settingsValues(settings)) !== JSON.stringify(settingsValues(state.settings))) state.remoteSettingsChanged = true;
    state.settings = settings; state.load.settings = {phase: "loaded", error: null};
    if (!state.settingsDirty) state.settingsDraft = settingsValues(settings);
    if (!state.formDraft) state.formDraft = emptyDraft(settings);
  } catch (error) { if (generation === settingsGeneration) state.load.settings = {phase: state.settings ? "stale" : "error", error}; }
}
function showSettingsRead() {
  if (route() === "jobs" && document.getElementById("job-form")) { patchJobs(); return; }
  if (route() !== "settings" || !document.getElementById("settings-form")) { render(); return; }
  // Keep the actual controls mounted while the user is editing a settings draft.
  const template = document.createElement("template"); template.innerHTML = renderSettings(state);
  for (const id of ["settings-load-state", "settings-remote-notice"]) {
    const current = document.getElementById(id), next = template.content.querySelector(`#${id}`);
    if (current && next) { if (current.innerHTML !== next.innerHTML) current.innerHTML = next.innerHTML; current.hidden = next.hidden; }
  }
  if (!state.settingsDirty) {
    const current = document.querySelector(".settings-sections"), next = template.content.querySelector(".settings-sections");
    if (current && next && settingsDiffer(readSettings(document.getElementById("settings-form")), state.settings)) { const focus = controlFocus(); current.innerHTML = next.innerHTML; restoreControl(focus); }
  }
  settingsFeedback(); workerStatus();
}
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  if (document.hidden) return;
  refreshTimer = setTimeout(() => refresh(), Math.min(120000, 15000 * 2 ** failures));
}
function refresh(includeSettings = false) {
  if (refreshPromise) { refreshAgain = true; refreshSettingsAgain ||= includeSettings; return refreshPromise; }
  refreshPromise = (async () => {
    let settings = includeSettings || route() === "settings";
    try {
      do {
        refreshAgain = false;
        await Promise.all([loadJobs(), loadStatus(), ...(settings ? [loadSettings()] : [])]);
        failures = [state.load.jobs, state.load.status].some(load => ["error", "stale"].includes(load.phase)) ? Math.min(failures + 1, 2) : 0;
        if (settings) showSettingsRead();
        settings = refreshSettingsAgain; refreshSettingsAgain = false;
      } while (refreshAgain);
    } finally { refreshPromise = null; scheduleRefresh(); }
  })();
  return refreshPromise;
}
function readDraft(form) {
  if (!form || !state.settings) return state.formDraft;
  const data = new FormData(form), interval = data.get("interval_choice");
  return {name: String(data.get("name") || ""), target_urls: [...form.querySelectorAll('[name="target_urls"]')].map(input => input.value),
    interval_seconds: interval === "custom" ? form.querySelector("#custom-job-interval")?.value : interval || data.get("interval_seconds"),
    date_mode: data.get("date_mode"), horizon_days: form.querySelector("#horizon-days")?.value || state.formDraft?.horizon_days || 15,
    earliest_date: form.querySelector("#earliest-date")?.value || "", latest_date: form.querySelector("#latest-date")?.value || "",
    time_zone: state.original?.time_zone || state.settings.time_zone, insurance_sector: data.get("insurance_sector"),
    telehealth: data.has("telehealth"), telegram_enabled: state.settings.telegram_configured && data.has("telegram_enabled")};
}
function readSettings(form) {
  const data = new FormData(form), interval = data.get("default_interval_choice");
  return {default_interval_seconds: interval === "custom" ? form.querySelector("#default-interval")?.value : interval, request_spacing_seconds: data.get("request_spacing_seconds")};
}
function showErrors(error, formId, errorId) {
  const form = document.getElementById(formId), output = document.getElementById(errorId); if (!form || !output) return;
  output.textContent = message(error); output.hidden = false;
  for (const [field] of Object.entries(error?.fields || {})) {
    const name = field.split(".")[0];
    const names = {interval_seconds: ["interval_choice", "custom_interval_seconds"], default_interval_seconds: ["default_interval_choice", "custom_default_interval_seconds"]}[name] || [name];
    [...form.elements].filter(input => names.includes(input.name)).forEach(input => { input.setAttribute("aria-invalid", "true"); input.setAttribute("aria-describedby", errorId); });
  }
  output.focus?.(); announce(message(error));
}
function resetEditor() { state.editingId = null; state.original = null; state.formDraft = state.settings ? emptyDraft(state.settings) : null; state.jobError = null; }
function openEditor() {
  if (route() !== "jobs") location.hash = "#jobs"; else render();
  requestAnimationFrame(() => document.getElementById("job-name")?.focus());
}
function canonicalJob(job) {
  if (!state.jobs) state.jobs = [];
  const index = state.jobs.findIndex(value => value.id === job.id);
  if (index < 0) state.jobs.unshift(job); else state.jobs[index] = job;
  updateViews();
}
function payloadMatches(job, payload) {
  const normalized = value => { try { const url = new URL(value); url.hash = ""; return url.href; } catch { return value; } };
  return Object.entries(payload).every(([key, value]) => key === "target_urls"
    ? JSON.stringify(job.targets.map(target => normalized(target.booking_url))) === JSON.stringify(value.map(normalized))
    : JSON.stringify(job[key]) === JSON.stringify(value));
}
function reconcileCreate() {
  const uncertain = state.uncertainCreate; if (!uncertain || !state.jobs) return;
  const matches = state.jobs.filter(job => !uncertain.ids.has(job.id) && payloadMatches(job, uncertain.payload));
  if (matches.length === 1) {
    state.uncertainCreate = null; resetEditor(); announce("The server contains the submitted job. Its creation was confirmed.");
    if (!state.jobSubmitting) render();
  }
}
async function submitJob(form) {
  if (state.jobSubmitting || state.uncertainCreate || !state.settings || !state.jobs || state.load.jobs.phase !== "loaded") return;
  state.formDraft = readDraft(form); state.jobError = null;
  let payload;
  try { payload = state.original ? updateJobPayload(state.formDraft, state.original, state.settings) : createJobPayload(state.formDraft, state.settings); }
  catch (error) { state.jobError = error; showErrors(error, "job-form", "job-form-error"); return; }
  const id = state.editingId, ids = new Set(state.jobs.map(job => job.id));
  state.jobSubmitting = true; if (id) state.pendingJobs.add(id); render();
  try {
    const job = id ? await api.updateJob(id, payload) : await api.createJob(payload);
    canonicalJob(job); resetEditor(); announce(id ? "Job saved." : "Job created."); await refresh();
  } catch (error) {
    state.jobError = error;
    if (!id && error.ambiguous) state.uncertainCreate = {payload, ids};
    if (error.ambiguous || error.status === 404) await refresh();
  } finally {
    state.jobSubmitting = false; if (id) state.pendingJobs.delete(id); render();
    if (state.jobError) showErrors(state.jobError, "job-form", "job-form-error");
  }
}
let settingsSaveTimer, settingsEditRevision = 0;
function settingsDiffer(draft, saved) {
  return Object.keys(settingsValues(saved)).some(key => draft[key] === "" || Number(draft[key]) !== Number(saved[key]));
}
function settingsFeedback() {
  document.getElementById("settings-form")?.setAttribute("aria-busy", String(state.settingsSubmitting));
  document.querySelectorAll('[data-setting-warning]').forEach(button => {
    const warning = settingWarning(state, button.dataset.settingWarning);
    button.hidden = !warning;
    button.setAttribute("aria-label", `${button.closest(".settings-row").querySelector("h3").textContent}: ${warning?.message || ""}`);
    button.querySelector(".settings-warning-message").textContent = warning?.message || "";
    button.classList.toggle("settings-warning-failed", Boolean(warning?.failed));
  });
}
function settingsEdited(immediate = false) {
  state.settingsDraft = readSettings(document.getElementById("settings-form"));
  state.settingsDirty = settingsDiffer(state.settingsDraft, state.settings);
  state.settingsError = null; settingsEditRevision++;
  clearTimeout(settingsSaveTimer); settingsFeedback();
  if (state.settingsDirty) settingsSaveTimer = setTimeout(() => submitSettings(), immediate ? 0 : 600);
}
async function submitSettings(form) {
  clearTimeout(settingsSaveTimer);
  if (form) state.settingsDraft = readSettings(form);
  if (state.settingsSubmitting || !state.settings || !state.settingsDraft) return;
  state.settingsDirty = settingsDiffer(state.settingsDraft, state.settings);
  if (!state.settingsDirty) { settingsFeedback(); return; }
  let payload; try { payload = settingsPayload(state.settingsDraft, state.settings); }
  catch (error) { state.settingsError = error; settingsFeedback(); return; }
  const revision = settingsEditRevision;
  ++settingsGeneration; state.settingsSubmitting = true; state.settingsError = null; settingsFeedback();
  try {
    state.settings = await api.updateSettings(payload);
    state.load.settings = {phase: "loaded", error: null};
    state.settingsDirty = settingsDiffer(state.settingsDraft, state.settings);
    state.remoteSettingsChanged = false;
  } catch (error) { state.settingsError = error; }
  finally {
    state.settingsSubmitting = false; settingsFeedback();
    if (settingsEditRevision !== revision && state.settingsDirty) settingsSaveTimer = setTimeout(() => submitSettings(), 600);
  }
}

async function mutateJob(job, action) {
  if (state.pendingJobs.has(job.id)) return;
  state.pendingJobs.add(job.id); patchJobs();
  try {
    const result = await api[`${action}Job`](job.id);
    if (action === "delete") {
      state.jobs = state.jobs.filter(value => value.id !== job.id); state.histories.delete(job.id);
      if (state.editingId === job.id) { resetEditor(); render(); }
    }
    else canonicalJob(result);
    announce(action === "delete" ? "Job deleted. Check history is retained." : `Job ${action === "pause" ? "paused" : "resumed"}.`);
    await refresh();
  } catch (error) { announce(message(error)); if (error.ambiguous || error.status === 404) await refresh(); }
  finally { state.pendingJobs.delete(job.id); patchJobs(); }
}
let searchRefreshTimer;
let targetValidationTimer, targetValidationActive = false, targetValidationNext = 0;
function updateTargetMetadata() {
  document.querySelectorAll('[name="target_urls"]').forEach(input => {
    const output = input.closest(".target-entry")?.querySelector(".target-meta");
    if (output) output.innerHTML = renderTargetMetadata(input.value.trim(), state);
  });
}
function scheduleTargetValidation() {
  clearTimeout(targetValidationTimer);
  targetValidationTimer = setTimeout(validateTargets, Math.max(600, targetValidationNext - Date.now()));
}
async function validateTargets() {
  if (targetValidationActive || route() !== "jobs") return;
  const urls = [...new Set([...document.querySelectorAll('[name="target_urls"]')].map(input => input.value.trim()).filter(Boolean))];
  const url = urls.find(value => {
    const cached = state.targetMetadata.get(value);
    return safeBookingUrl(value) && !cached;
  });
  if (!url) return;
  targetValidationActive = true;
  state.targetMetadata.set(url, {phase: "loading", data: null, error: null});
  updateTargetMetadata();
  try {
    const data = await api.validateTarget(url);
    state.targetMetadata.set(url, {phase: "loaded", data, error: null});
  } catch (error) {
    state.targetMetadata.set(url, {phase: "error", data: null, error, at: Date.now()});
  } finally {
    targetValidationActive = false;
    targetValidationNext = Date.now() + Math.max(3000, (state.settings?.request_spacing_seconds || 3) * 1000);
    updateTargetMetadata();
    scheduleTargetValidation();
  }
}

document.addEventListener("click", event => {
  const control = event.target.closest("[data-action]"); if (!control || control.disabled) return;
  const action = control.dataset.action, job = state.jobs?.find(value => value.id === control.closest("[data-job-id]")?.dataset.jobId);
  if (["retry", "refresh"].includes(action)) { refresh(true); return; }
  if (action === "sign-in") { location.assign(location.href); return; }
  if (action === "filter") { state.filter = control.dataset.filter; patchJobs(); refresh(); return; }
  if (["new-job", "cancel-edit"].includes(action)) { if (!state.jobSubmitting) { resetEditor(); openEditor(); refresh(); } return; }
  if (["add-target", "remove-target"].includes(action)) {
    if (state.jobSubmitting || !state.formDraft) return;
    state.formDraft = readDraft(document.getElementById("job-form"));
    if (action === "add-target" && state.formDraft.target_urls.length < 100) state.formDraft.target_urls.push("");
    if (action === "remove-target" && state.formDraft.target_urls.length > 1) state.formDraft.target_urls.splice(Number(control.dataset.index), 1);
    render(); document.getElementById(`target-${action === "add-target" ? state.formDraft.target_urls.length - 1 : Math.max(0, Number(control.dataset.index) - 1)}`)?.focus(); return;
  }
  if (action === "retry-settings") { submitSettings(); return; }
  if (action === "retry-create" && state.uncertainCreate) {
    deleteFocus = controlFocus(control); dialog.dataset.operation = "retry-create"; dialog.returnValue = "";
    dialog.querySelector('[value="confirm"]').textContent = "Allow retry";
    dialog.querySelector("#confirm-title").textContent = "Allow another creation attempt?";
    dialog.querySelector("#confirm-copy").textContent = "The previous request may still create a job. Trying again can create a duplicate. Confirm only after checking the refreshed jobs list; this confirmation enables the form, and does not submit it.";
    dialog.showModal(); return;
  }
  if (!job || state.pendingJobs.has(job.id)) return;
  if (["pause", "resume"].includes(action)) { mutateJob(job, action); return; }
  if (action === "edit" && !state.jobSubmitting) {
    state.editingId = job.id; state.original = structuredClone(job);
    state.formDraft = {...job, target_urls: job.targets.map(target => target.booking_url), earliest_date: job.earliest_date || "", latest_date: job.latest_date || ""};
    for (const target of job.targets) {
      if (state.targetMetadata.get(target.booking_url)?.phase !== "loading") state.targetMetadata.set(target.booking_url, {phase: "loaded", data: target, error: null, seeded: true});
    }
    state.jobError = null; openEditor(); refresh(); return;
  }
  if (action === "delete") {
    deleteFocus = controlFocus(control); dialog.dataset.operation = "delete"; dialog.dataset.jobId = job.id; dialog.returnValue = "";
    dialog.querySelector('[value="confirm"]').textContent = "Delete job";
    dialog.querySelector("#confirm-title").textContent = `Delete ${job.name}?`;
    dialog.querySelector("#confirm-copy").textContent = "The job will stop being scheduled and its check history will be retained. A check already in flight may still record a result. Deletion persists after reload.";
    dialog.showModal();
  }
});
document.addEventListener("input", event => {
  if (event.target.id === "job-search") { state.query = event.target.value; patchJobs(); clearTimeout(searchRefreshTimer); searchRefreshTimer = setTimeout(() => refresh(), 300); return; }
  if (event.target.closest("#job-form")) {
    state.formDraft = readDraft(document.getElementById("job-form"));
    event.target.removeAttribute("aria-invalid");
    if (event.target.name === "target_urls") {
      const index = [...document.querySelectorAll('[name="target_urls"]')].indexOf(event.target);
      const url = event.target.value.trim();
      const output = document.getElementById(`target-meta-${index}`); if (output) output.innerHTML = renderTargetMetadata(url, state);
      const cached = state.targetMetadata.get(url);
      if (cached?.phase === "error" && Date.now() - cached.at >= 30000) state.targetMetadata.delete(url);
      scheduleTargetValidation();
    }
  }
  if (event.target.closest("#settings-form")) settingsEdited(event.target.type === "radio");
});
document.addEventListener("change", event => {
  if (event.target.id === "interval-filter") { state.intervalFilter = event.target.value; patchJobs(); refresh(); return; }
  if (event.target.closest("#job-form")) state.formDraft = readDraft(document.getElementById("job-form"));
  if (event.target.closest("#settings-form")) settingsEdited(event.target.type === "radio");
  const toggle = (wrapperId, custom, inputId) => {
    const wrapper = document.getElementById(wrapperId), input = document.getElementById(inputId);
    if (wrapper) wrapper.hidden = !custom;
    if (input) { input.disabled = !custom; input.required = custom; }
  };
  if (event.target.name === "default_interval_choice") toggle("custom-default-interval", event.target.value === "custom", "default-interval");
  if (event.target.name === "interval_choice") toggle("custom-interval-fields", event.target.value === "custom", "custom-job-interval");
  if (event.target.name === "date_mode") {
    const custom = event.target.value === "custom";
    toggle("custom-date-fields", custom, "earliest-date");
    const latest = document.getElementById("latest-date"); if (latest) { latest.required = custom; latest.disabled = !custom; }
    const horizon = document.getElementById("horizon-fields"); if (horizon) horizon.hidden = custom;
    const horizonInput = document.getElementById("horizon-days"); if (horizonInput) { horizonInput.disabled = custom; horizonInput.required = !custom; }
  }
});
document.addEventListener("submit", event => {
  if (event.target.id === "job-form") { event.preventDefault(); submitJob(event.target); }
  if (event.target.id === "settings-form") { event.preventDefault(); submitSettings(event.target); }
});
dialog.addEventListener("close", () => {
  if (dialog.returnValue === "confirm") {
    if (dialog.dataset.operation === "retry-create") { state.uncertainCreate = null; state.jobError = null; render(); }
    if (dialog.dataset.operation === "delete") {
      const job = state.jobs?.find(value => value.id === dialog.dataset.jobId); if (job) mutateJob(job, "delete");
    }
  }
  if (!deleteFocus || !restoreControl(deleteFocus)) app.focus({preventScroll: true});
});
window.addEventListener("hashchange", async () => {
  clearTimeout(refreshTimer); render(); app.focus({preventScroll: true});
  if (route() === "settings") { await refresh(true); }
  else { pumpHistory(); refresh(); }
});
document.addEventListener("visibilitychange", () => {
  clearTimeout(refreshTimer);
  if (!document.hidden) { if (route() === "jobs") { pumpHistory(); refresh(); } else refresh(true); }
});
render();
refresh(true);
