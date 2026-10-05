import { safeBookingUrl } from "./job-view.js";

const ROOT = "/api/v1";
const JOB_FIELDS = ["name", "target_urls", "interval_seconds", "date_mode", "horizon_days", "earliest_date", "latest_date", "time_zone", "insurance_sector", "telehealth", "telegram_enabled", "notification_channel_ids", "message_content", "quiet_hours_enabled", "quiet_hours_start", "quiet_hours_end"];
const SETTINGS_FIELDS = ["default_interval_seconds", "request_spacing_seconds", "message_content"];
const pick = (source, fields) => Object.fromEntries(fields.filter(key => Object.hasOwn(source, key) && source[key] !== undefined).map(key => [key, source[key]]));

export class ApiError extends Error {
  constructor(message, {kind = "http", status = null, fields = {}, ambiguous = false, detail = null} = {}) {
    super(message);
    this.name = "ApiError";
    Object.assign(this, {kind, status, fields, ambiguous, detail});
  }
}

function responseError(status, body) {
  const detail = body?.detail;
  const fields = {};
  if (Array.isArray(detail)) {
    for (const issue of detail) {
      const location = (issue.loc || []).filter(part => part !== "body").join(".");
      fields[location || "form"] = String(issue.msg || "Invalid value");
    }
  }
  const fallback = {404: "This record no longer exists.", 409: "The operation conflicts with the current server state.", 422: "Check the supplied values.", 502: "Doctolib could not be reached. Try again later."};
  const message = detail && typeof detail === "object" && !Array.isArray(detail) ? (detail.message || {version_conflict: "The saved version changed. Review the latest values.", smtp_impact_changed: "Pending email deliveries changed. Review the confirmation and try again.", idempotency_conflict: "This saved creation request conflicts with another request.", invalid_job: "The job configuration was rejected. Correct the draft and try again.", doctolib_unavailable: "Doctolib could not be reached. Retry the saved request later."}[detail.code] || fallback[status] || "The request could not be completed.") : typeof detail === "string" ? detail.replaceAll("_", " ")
    : Object.values(fields).join("; ") || fallback[status] || `The server returned an error (${status}).`;
  return new ApiError(message, {status, fields, detail, kind: status === 422 ? "validation" : status === 404 ? "missing" : status === 409 ? "conflict" : status === 502 ? "upstream" : "http"});
}

async function request(path, {method = "GET", body, signal, headers = {}, timeout = 15_000} = {}) {
  const controller = new AbortController();
  const mutation = !["GET", "HEAD"].includes(method) && path !== "/targets/validate";
  let timedOut = false;
  const abort = () => controller.abort();
  if (signal?.aborted) abort();
  else signal?.addEventListener("abort", abort, {once: true});
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
  try {
    const response = await fetch(`${ROOT}${path}`, {
      method, signal: controller.signal, credentials: "same-origin", redirect: "manual", cache: "no-store",
      headers: {Accept: "application/json", ...(body === undefined ? {} : {"Content-Type": "application/json"}), ...headers},
      ...(body === undefined ? {} : {body: JSON.stringify(body)})
    });
    if (response.type === "opaqueredirect" || [301, 302, 303, 307, 308, 401, 403].includes(response.status)) {
      throw new ApiError("Your session has expired. Sign in again to continue.", {kind: "auth", status: response.status, ambiguous: mutation});
    }
    const contentType = response.headers.get("content-type") || "";
    const json = /\bapplication\/(?:[\w.-]+\+)?json\b/i.test(contentType);
    if (!response.ok) {
      let data = null;
      if (json) { try { data = await response.json(); } catch { /* A malformed error body still has a useful HTTP status. */ } }
      const error = responseError(response.status, data);
      error.ambiguous = mutation && response.status >= 500;
      throw error;
    }
    if (!json) {
      const authPage = /\b(?:text\/html|application\/xhtml\+xml)\b/i.test(contentType);
      throw new ApiError(authPage ? "The server returned a sign-in page. Sign in again to continue."
        : "The server did not return API data. Reload or sign in again.",
      {kind: authPage ? "auth" : "protocol", status: response.status, ambiguous: mutation});
    }
    try { return await response.json(); }
    catch { throw new ApiError("The server returned unreadable API data.", {kind: "protocol", status: response.status, ambiguous: mutation}); }
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (controller.signal.aborted) throw new ApiError(timedOut
      ? mutation ? "The request timed out. Its outcome is uncertain; refresh before retrying." : "The request timed out. Try again."
      : "The request was cancelled.", {kind: timedOut ? "timeout" : "cancelled", ambiguous: mutation});
    throw new ApiError(mutation ? "The connection failed. Its outcome is uncertain; refresh before retrying." : "The API could not be reached. Check the connection and try again.", {kind: "network", ambiguous: mutation});
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

const identifier = value => encodeURIComponent(String(value));
const page = ({limit = 100, offset = 0, status} = {}) => {
  if (!Number.isInteger(limit) || limit < 1 || limit > 100 || !Number.isInteger(offset) || offset < 0) throw new ApiError("Invalid page bounds.", {kind: "validation"});
  const query = new URLSearchParams({limit: String(limit), offset: String(offset)});
  if (status) query.set("status", status);
  return query;
};

export const api = {
  notificationPreview: (channel_type, message_content) => request("/notification-preview", {method: "POST", body: {channel_type, message_content}}),
  getSmtp: () => request('/settings/smtp'),
  smtpImpact: (values, version) => request('/settings/smtp/impact', {method: 'POST', body: {...values, expected_version: version}}),
  updateSmtp: (values, version, impactToken) => request('/settings/smtp', {method: 'PUT', body: {...values, expected_version: version, ...(impactToken ? {expected_impact_token: impactToken} : {})}}),
  listChannels: options => request("/channels", options),
  createChannel: (values, key) => request("/channels", {method: "POST", body: values, headers: {"Idempotency-Key": key}}),
  updateChannel: (id, values, version) => request(`/channels/${identifier(id)}`, {method: "PATCH", body: {...values, expected_version: version}}),
  deleteChannel: (id, version) => request(`/channels/${identifier(id)}`, {method: "DELETE", body: {expected_version: version, confirmed: true}}),
  importLegacyChannel: key => request("/channels/import-legacy", {method: "POST", body: {}, headers: {"Idempotency-Key": key}}),
  channelPreview: id => request(`/channels/${identifier(id)}/preview`),
  testChannel: (id, version, key, contentVersion) => request(`/channels/${identifier(id)}/tests`, {method: "POST", body: {expected_version: version, ...(contentVersion === undefined ? {} : {expected_content_version: contentVersion})}, headers: {"Idempotency-Key": key}}),
  getChannelTest: id => request(`/channel-tests/${identifier(id)}`),
  getSettings: options => request("/settings", options),
  previewQuietHours: values => request("/quiet-hours/preview", {method: "POST", body: values}),
  getStatus: options => request("/status", options),
  listJobs: (options = {}) => request(`/jobs?${page(options)}`, {signal: options.signal}),
  getJob: (id, options) => request(`/jobs/${identifier(id)}`, options),
  getChecks: (id, options = {}) => request(`/jobs/${identifier(id)}/checks?${page({...options, limit: options.limit ?? 10})}`, {signal: options.signal}),
  getActivity: ({jobId, runId, limit = 25, offset = 0, beforeRunId} = {}) => {
    if (!Number.isInteger(limit) || limit < 1 || limit > 100 || !Number.isInteger(offset) || offset < 0) throw new ApiError("Invalid page bounds.", {kind: "validation"});
    const query = new URLSearchParams({limit: String(limit), offset: String(offset)});
    if (jobId) query.set("job_id", jobId);
    if (runId) query.set("run_id", runId);
    if (beforeRunId) query.set("before_run_id", beforeRunId);
    return request(`/activity?${query}`);
  },
  validateTarget: (booking_url, options = {}) => request("/targets/validate", {signal: options.signal, method: "POST", body: {booking_url}, timeout: 120_000}),
  createJob: (values, options = {}) => {
    if (!/^[A-Za-z0-9._:-]{1,128}$/.test(options.idempotencyKey || "")) throw new ApiError("A saved creation request key is required.", {kind: "validation"});
    return request("/jobs", {signal: options.signal, method: "POST", body: pick(values, JOB_FIELDS), headers: {"Idempotency-Key": options.idempotencyKey}, timeout: 120_000});
  },
  updateJob: (id, values, options = {}) => request(`/jobs/${identifier(id)}`, {signal: options.signal, method: "PATCH", body: {...pick(values, JOB_FIELDS), expected_version: options.expectedVersion}, timeout: Object.hasOwn(values, "target_urls") ? 120_000 : 15_000}),
  pauseJob: (id, options = {}) => request(`/jobs/${identifier(id)}/pause`, {signal: options.signal, method: "POST", body: {expected_version: options.expectedVersion}}),
  resumeJob: (id, options = {}) => request(`/jobs/${identifier(id)}/resume`, {signal: options.signal, method: "POST", body: {expected_version: options.expectedVersion}}),
  checkNowJob: (id, options = {}) => request(`/jobs/${identifier(id)}/check-now`, {signal: options.signal, method: "POST"}),
  deleteJob: (id, options = {}) => request(`/jobs/${identifier(id)}`, {signal: options.signal, method: "DELETE", body: {expected_version: options.expectedVersion}}),
  updateSettings: (values, options = {}) => request("/settings", {signal: options.signal, method: "PUT", body: {...pick(values, SETTINGS_FIELDS), expected_version: options.expectedVersion}})
};

function invalid(field, message) { throw new ApiError(message, {kind: "validation", fields: {[field]: message}}); }
function number(value, field, min, max, integer = true) {
  const result = typeof value === "string" && !value.trim() ? NaN : Number(value);
  if (!Number.isFinite(result) || (integer && !Number.isInteger(result)) || result < min || result > max) invalid(field, `${field.replaceAll("_", " ")} must be ${integer ? "a whole number " : ""}between ${min} and ${max}.`);
  return result;
}
function date(value, field) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "") || !Number.isFinite(Date.parse(`${value}T00:00:00Z`)) || new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) !== value) invalid(field, "Enter a valid calendar date.");
  return value;
}

export function createJobPayload(draft, settings) {
  const name = String(draft.name || "").trim();
  if (!name || name.length > 120) invalid("name", "Enter a job name of 1–120 characters.");
  if (!Array.isArray(draft.target_urls) || !draft.target_urls.length || draft.target_urls.length > 100) invalid("target_urls", "Add between 1 and 100 booking URLs.");
  const target_urls = draft.target_urls.map(value => String(value || "").trim());
  if (target_urls.some(value => !value || value.length > 4096)) invalid("target_urls", "Enter a complete booking URL for every target.");
  const canonical = target_urls.map(value => {
    const url = safeBookingUrl(value);
    if (!url) invalid("target_urls", "Enter a complete HTTPS Doctolib availability URL.");
    return url;
  });
  if (new Set(canonical).size !== canonical.length) invalid("target_urls", "Target URLs must be unique.");
  if (!["first_available", "custom"].includes(draft.date_mode)) invalid("date_mode", "Choose an appointment date mode.");
  if (!["public", "private"].includes(draft.insurance_sector)) invalid("insurance_sector", "Choose an insurance sector.");
  const payload = {
    name, target_urls,
    interval_seconds: number(draft.interval_seconds, "interval_seconds", Math.max(300, settings.minimum_poll_interval_seconds), 86400),
    date_mode: draft.date_mode,
    horizon_days: number(draft.horizon_days, "horizon_days", 1, 365),
    time_zone: String(draft.time_zone || settings.time_zone),
    insurance_sector: draft.insurance_sector, telehealth: Boolean(draft.telehealth), telegram_enabled: Boolean(draft.telegram_enabled)
  };
  const quietStart = String(draft.quiet_hours_start ?? "22:00"), quietEnd = String(draft.quiet_hours_end ?? "07:00");
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(quietStart)) invalid("quiet_hours_start", "Enter a quiet-hours start time.");
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(quietEnd)) invalid("quiet_hours_end", "Enter a quiet-hours end time.");
  if (draft.quiet_hours_enabled && quietStart === quietEnd) invalid("quiet_hours_end", "Quiet-hours start and end must differ.");
  payload.quiet_hours_enabled = Boolean(draft.quiet_hours_enabled);
  payload.quiet_hours_start = quietStart;
  payload.quiet_hours_end = quietEnd;
  try { new Intl.DateTimeFormat("en", {timeZone: payload.time_zone}); } catch { invalid("time_zone", "Choose a valid IANA time zone."); }
  if (Object.hasOwn(draft, "notification_channel_ids")) {
    if (!Array.isArray(draft.notification_channel_ids) || draft.notification_channel_ids.some(id => typeof id !== "string" || !id)) invalid("notification_channel_ids", "Choose saved notification channels.");
    payload.notification_channel_ids = [...new Set(draft.notification_channel_ids)];
  } else if (payload.telegram_enabled && !settings.telegram_configured) invalid("telegram_enabled", "No usable notification channel is configured.");
  if (Object.hasOwn(draft, "message_content")) payload.message_content = draft.message_content;
  if (payload.date_mode === "custom") {
    payload.earliest_date = date(draft.earliest_date, "earliest_date");
    payload.latest_date = date(draft.latest_date, "latest_date");
    const days = (Date.parse(payload.latest_date) - Date.parse(payload.earliest_date)) / 86_400_000 + 1;
    if (days < 1 || days > 366) invalid("latest_date", "The date range must contain 1–366 calendar dates in chronological order.");
  }
  return payload;
}

export function updateJobPayload(draft, original, settings) {
  // Preserve the saved time zone; the initial job editor has no time-zone control.
  const preserveTelegram = !Object.hasOwn(draft, "notification_channel_ids") && !settings.telegram_configured && Boolean(original.telegram_enabled);
  const clean = createJobPayload({...draft, time_zone: original.time_zone,
    ...(preserveTelegram ? {telegram_enabled: false} : {})}, settings);
  if (preserveTelegram) clean.telegram_enabled = Boolean(original.telegram_enabled);
  const previous = {...original, quiet_hours_enabled: false, quiet_hours_start: "22:00", quiet_hours_end: "07:00",
    target_urls: original.targets.map(target => target.booking_url)};
  const payload = Object.fromEntries(Object.entries(clean).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(previous[key])));
  if (clean.date_mode === "first_available" && original.date_mode === "custom") Object.assign(payload, {earliest_date: null, latest_date: null});
  return payload;
}

export function settingsPayload(draft, settings) {
  return {
    default_interval_seconds: number(draft.default_interval_seconds, "default_interval_seconds", Math.max(300, settings.minimum_poll_interval_seconds), 86400),
    request_spacing_seconds: number(draft.request_spacing_seconds, "request_spacing_seconds", 3, 120, false),
    ...(Object.hasOwn(draft, "message_content") ? {message_content: draft.message_content} : {})
  };
}
