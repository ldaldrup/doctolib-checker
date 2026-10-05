import { contentControls, contentSummary } from "../message-content.js";
import { renderChannels } from "./channels.js";
import { renderSmtp } from './smtp.js';
import { renderDeliveryNotice } from "../delivery-view.js";
import { escapeHtml as h, icon } from "../ui.js";
const pollInterval = seconds => seconds % 3600 === 0 ? `${seconds / 3600} hr` : seconds % 60 === 0 ? `${seconds / 60} min` : `${seconds} sec`;

const message = error => error?.message || String(error || "Settings could not be loaded.");
export function settingWarning(state, field) {
  const draft = state.settingsDraft || state.settings;
  if (!draft || (field === "message_content" ? JSON.stringify(draft[field]) === JSON.stringify(state.settings?.[field]) : draft[field] !== "" && Number(draft[field]) === Number(state.settings?.[field]))) return null;
  if (state.settingsConflict) return {failed: true, message: "Settings changed on the server. Reconcile the choices above before saving."};
  const error = state.settingsError;
  const fields = Object.keys(error?.fields || {});
  const failed = Boolean(error && (!fields.length || fields.includes(field)));
  return {failed, message: failed ? `${message(error)} Click to retry saving.` : "Unsaved change. Waiting to save."};
}
function row(title, description, control, field, state) {
  const warning = field ? settingWarning(state, field) : null;
  return `<div class="settings-row"><div><h3>${title}</h3><p>${description}</p></div><div class="settings-control">${control}</div>${field ? `<button class="settings-warning" type="button" data-action="retry-settings" data-setting-warning="${field}" aria-label="${h(title + ': ' + (warning?.message || ''))}" ${warning ? "" : "hidden"}>${icon("warning")}<span class="settings-warning-message" role="tooltip">${h(warning?.message || "")}</span></button>` : ""}</div>`;
}
export function renderSettings(state) {
  const settings = state.settings;
  if (!settings) return `<div class="page-heading"><div><h1>Settings</h1><p>Polling defaults and server configuration.</p></div></div><div class="notice ${state.load?.settings?.error ? "notice-warning" : ""}" role="status"><span>${state.load?.settings?.error ? h(message(state.load.settings.error)) : "Loading saved settings…"}</span><button class="text-button" type="button" data-action="refresh">Retry</button></div>`;
  const draft = state.settingsDraft || settings;
  const intervals = [300, 600, 900, 1800].filter(value => value >= settings.minimum_poll_interval_seconds);
  const preset = intervals.includes(Number(draft.default_interval_seconds));
  const pending = Boolean(state.settingsSubmitting);
  return `<div class="page-heading"><div><h1>Settings</h1><p>Set defaults for new jobs and outbound requests.</p></div></div>
    <div id="delivery-status">${renderDeliveryNotice(state)}</div><div id="settings-load-state">${state.load?.settings?.error ? `<div class="notice notice-warning" role="status"><span>Showing last loaded settings. ${h(message(state.load.settings.error))}</span><button class="text-button" type="button" data-action="refresh">Retry</button></div>` : ""}</div><div class="settings-panels">
    <form id="settings-form" class="settings-form" aria-busy="${pending}"><div id="settings-remote-notice" class="notice notice-warning" ${state.remoteSettingsChanged ? "" : "hidden"}>${state.settingsConflict ? `<div><p>Settings changed on the server. Your pending edits are preserved. Choose which values to save.</p>${["default_interval_seconds", "request_spacing_seconds", "message_content"].map(field => `<fieldset><legend>${h(field.replaceAll("_", " "))}</legend><label><input type="radio" name="reconcile-${field}" value="server"> Server: ${h(field === "message_content" ? contentSummary(state.settingsConflict[field]) : state.settingsConflict[field])}</label><label><input type="radio" name="reconcile-${field}" value="draft"> My draft: ${h(field === "message_content" ? contentSummary(draft[field]) : draft[field])}</label></fieldset>`).join("")}<button class="text-button" type="button" data-action="reconcile-settings">Save selected values</button><button class="text-button" type="button" data-action="use-server-settings">Use all server values</button></div>` : 'Settings changed on the server. Your pending edits have been preserved. <button class="text-button" type="button" data-action="refresh">Fetch latest settings</button>'}</div><fieldset class="form-fields settings-sections">
    <section class="panel" aria-labelledby="engine-heading"><div class="panel-header">${icon("sliders")}<div><h2 id="engine-heading">General &amp; Engine Configuration</h2><p>Defaults for new jobs and outbound requests.</p></div></div>
      ${row("Default polling interval", "Applied to newly created jobs; existing jobs keep their own interval.", `<fieldset class="settings-interval"><legend class="visually-hidden">Default polling interval</legend><div class="choice-row">${intervals.map(seconds => `<label class="choice"><input type="radio" name="default_interval_choice" value="${seconds}" ${Number(draft.default_interval_seconds) === seconds ? "checked" : ""}><span>${h(pollInterval(seconds))}</span></label>`).join("")}<label class="choice"><input type="radio" name="default_interval_choice" value="custom" ${preset ? "" : "checked"}><span>Custom</span></label></div><div id="custom-default-interval" class="settings-custom-interval" ${preset ? "hidden" : ""}><label for="default-interval">Seconds</label><input class="input numeric" id="default-interval" name="custom_default_interval_seconds" type="number" min="${h(settings.minimum_poll_interval_seconds)}" max="86400" step="1" value="${h(draft.default_interval_seconds)}" ${preset ? "disabled" : "required"}></div></fieldset>`, "default_interval_seconds", state)}
      ${row("Outbound request spacing", "Minimum delay between Doctolib requests across jobs.", `<label class="visually-hidden" for="request-spacing">Request spacing in seconds</label><input class="input numeric" id="request-spacing" name="request_spacing_seconds" type="number" min="3" max="120" step="any" value="${h(draft.request_spacing_seconds)}" required><span class="unit">sec</span>`, "request_spacing_seconds", state)}
      ${row("Server polling floor", "Configured on the server; minimum interval allowed for every job.", `<span class="numeric">${h(settings.minimum_poll_interval_seconds)} seconds</span>`)}
      ${row("Default time zone", "Configured on the server; used for new jobs.", `<span>${h(settings.time_zone || "Unknown")}</span>`)}
    </section><section class="panel" aria-labelledby="message-content-heading"><div class="panel-header"><div><h2 id="message-content-heading">Appointment message content</h2><p>Defaults for jobs that inherit Settings. Changes affect future events; queued messages keep their saved content.</p></div></div>
      ${row("Message defaults", "Jobs may explicitly override these defaults. Changes save automatically.", contentControls(draft.message_content, 'settings-content'), "message_content", state)}
      <div class="message-preview-controls"><label for="message-preview-type">Synthetic preview channel</label><select class="select" id="message-preview-type">${[['telegram', 'Telegram'], ['ntfy', 'ntfy'], ['email', 'Email'], ['webhook', 'HTTPS webhook JSON']].map(([type, label]) => `<option value="${type}" ${state.contentPreviewType === type ? 'selected' : ''}>${label}</option>`).join('')}</select><button class="button button-secondary" type="button" data-action="preview-message-content" ${state.contentPreviewBusy ? 'disabled' : ''}>${state.contentPreviewBusy ? 'Rendering…' : 'Preview draft'}</button><p class="muted">Synthetic example only. Preview sends no notification. Channel test sends use saved Settings.</p>${state.contentPreview ? `<pre class="channel-preview" role="status">${h(state.contentPreview)}</pre>` : ''}${state.contentPreviewError ? `<p class="form-error" role="alert">${h(state.contentPreviewError)}</p>` : ''}</div>
    </section></fieldset>
    </form>${renderSmtp(state)}${renderChannels(state)}</div>`;
}
