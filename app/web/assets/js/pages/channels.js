import { previewText } from "../message-content.js";
import { api } from "../api.js";
import { escapeHtml as h, icon } from "../ui.js";

const blank = () => ({type: "telegram", name: "", enabled: true, token_action: "replace", bot_token: "", chat_action: "replace", chat_id: "", recipient_action: 'replace', recipient: '', endpoint_action: "replace", endpoint: "", auth_type: "none", auth_action: "clear", auth_token: "", auth_username: "", auth_password: "", ntfy_priority: 3, recover_failed: false});
const typeLabel = type => ({telegram: "Telegram", ntfy: "ntfy", webhook: "HTTPS webhook", email: 'Email'}[type] || "Telegram");
const secretActions = {token_action: ["bot_token"], chat_action: ["chat_id"], recipient_action: ['recipient'], endpoint_action: ["endpoint"], auth_action: ["auth_token", "auth_username", "auth_password"]};
const credentialsChanged = draft => (draft.type === 'telegram' ? [draft.token_action,draft.chat_action] : draft.type === 'email' ? [draft.recipient_action] : [draft.endpoint_action,draft.auth_action]).some(action => action !== 'keep');
const destinationSummary = channel => channel.type === 'email' ? channel.recipient_set ? 'Recipient saved' : 'Recipient not set' : channel.type === 'telegram' || !channel.type ? channel.chat_id_masked || 'Recipient not set' : channel.endpoint_host || 'Endpoint not set';
const errorText = error => error?.message || "The request failed.";
const terminal = status => ["sent", "failed", "unknown", "cancelled"].includes(status);
export function renderChannels(state) {
  const ui = state.channelUI || {}, channels = state.channels || [], draft = ui.draft;
  const locked = Boolean(ui.busy || ui.createAttempt || ui.conflictBlocked);
  const unavailable = !state.settings?.notification_secret_configured;
  return `<section class="panel" id="channel-settings" aria-labelledby="notification-heading"><div class="panel-header">${icon("bellActive")}<div><h2 id="notification-heading">Notification channels</h2><p>Save named destinations, then choose them on each job.</p></div></div>
    ${unavailable ? '<p class="notice notice-warning">The server notification encryption key is unavailable. An operator must configure NOTIFICATION_SECRET_KEY before saving credentials.</p>' : ""}
    ${state.settings?.legacy_telegram_available && !state.settings?.legacy_telegram_imported ? `<p class="notice"><span>Legacy environment Telegram settings are available. Import once as Telegram1 and map existing opted-in jobs.</span><button type="button" class="text-button" data-action="import-telegram" ${ui.busy || unavailable ? "disabled" : ""}>Import Telegram1</button></p>` : ""}
    ${ui.error ? `<p class="form-error" role="alert">${h(errorText(ui.error))}</p>` : ""}
    ${state.channels === null ? '<p>Loading saved channels…</p>' : channels.length ? channels.map(channel => `<div class="settings-row" data-channel-id="${h(channel.id)}"><div><h3>${h(channel.name)}</h3><p>${h(typeLabel(channel.type))} · ${channel.enabled ? channel.usable ? "Ready" : "Incomplete" : "Disabled"} · ${h(destinationSummary(channel))}</p>${ui.tests?.[channel.id] ? `<p role="status">${h(testFeedback(ui.tests[channel.id], channel.type))}</p><button type="button" class="text-button" data-action="channel-test-status">${ui.tests[channel.id].uncertain ? "Retry same test request" : "Check test status"}</button>` : ""}</div><div class="settings-control"><button class="text-button" type="button" data-action="edit-channel" ${locked || draft ? "disabled" : ""}>Edit</button><button class="text-button" type="button" data-action="preview-channel" ${ui.busy ? "disabled" : ""}>Preview</button><button class="text-button" type="button" data-action="test-channel" ${ui.busy || !channel.enabled || !channel.usable || ui.testAttempts?.[channel.id] ? "disabled" : ""}>Send test</button><button class="text-button" type="button" data-action="delete-channel" ${locked || draft ? "disabled" : ""}>Delete</button></div></div>`).join("") : '<p class="muted">No saved notification channels. Jobs can still check with notifications off.</p>'}
    ${ui.preview ? `<div class="notice"><div><h3>Synthetic notification preview</h3><p>Saved Settings content. No appointment data or message is sent.</p><pre class="channel-preview">${h(ui.preview)}</pre><button class="text-button" type="button" data-action="close-channel-preview">Close preview</button></div></div>` : ""}
    ${draft ? `<form id="channel-form" class="channel-form" autocomplete="off" aria-busy="${Boolean(ui.busy)}"><h3>${ui.original ? `Edit ${h(ui.original.name)}` : "New notification channel"}</h3><fieldset ${locked ? "disabled" : ""}><div class="field"><label for="channel-name">Name</label><input class="input" id="channel-name" name="name" maxlength="120" value="${h(draft.name)}" required></div><label class="checkbox-line"><span>Enabled</span><input name="enabled" type="checkbox" ${draft.enabled ? "checked" : ""}></label>
      ${ui.original ? `<p>${h(typeLabel(draft.type))}. To change type, create another channel.</p>` : `<div class="field"><label for="channel-type">Type</label><select class="select" id="channel-type" name="type">${["telegram", "ntfy", "webhook", 'email'].map(type => `<option value="${type}" ${draft.type === type ? "selected" : ""}>${typeLabel(type)}</option>`).join("")}</select></div>`}
      ${draft.type === "telegram" ? `${credentialField("bot_token", "Bot token", "token_action", draft, ui.original?.bot_token_set)}${credentialField("chat_id", "Recipient chat ID", "chat_action", draft, ui.original?.chat_id_set)}` : draft.type === 'email' ? `${credentialField('recipient','Recipient email address','recipient_action',draft,ui.original?.recipient_set)}<p class="muted">One recipient per channel. Configure and enable the SMTP transport above before sending a test.</p>` : endpointFields(draft, ui.original)}
      ${ui.original ? `<label class="checkbox-line"><span>Recover fresh failed deliveries after repairing credentials for this same destination</span><input name="recover_failed" type="checkbox" ${draft.recover_failed ? "checked" : ""}></label>` : ""}<p class="muted">${['ntfy','webhook'].includes(draft.type) ? "HTTPS only; private hosts need an operator allowlist. " : ''}Destination changes cancel unsent deliveries. Saving does not send.</p></fieldset>
      ${ui.conflictBlocked ? `<div class="notice notice-warning"><div><p>This channel changed. Your draft is preserved. Fetch and review the saved version before applying it.</p>${ui.latest ? `<p>Saved name: ${h(ui.latest.name)}. Saved enabled: ${ui.latest.enabled ? "yes" : "no"}.${['ntfy','webhook'].includes(ui.latest.type) ? ` Host: ${h(ui.latest.endpoint_host || "not set")}. Authentication: ${h(ui.latest.auth_type)}.${ui.latest.type === "ntfy" ? ` Priority: ${h(ui.latest.ntfy_priority)}.` : ""}` : ""} Credentials remain masked.</p><label class="checkbox-line"><span>I reviewed the saved configuration and want to apply my draft, including its explicit credential operations.</span><input type="checkbox" id="channel-conflict-confirm"></label><button type="button" class="text-button" data-action="reconcile-channel">Apply draft to latest version</button><button type="button" class="text-button" data-action="use-server-channel">Use saved values</button>` : '<button type="button" class="text-button" data-action="refetch-channel">Fetch latest version</button>'}</div></div>` : ""}
      ${ui.createAttempt ? '<p class="notice notice-warning">The saved create outcome is uncertain. Keep this page open. Retry reuses its original credentials, payload and key.</p><button type="button" class="button button-primary" data-action="retry-channel-create">Retry saved create</button>' : `<button class="button button-primary" type="submit" ${locked || (unavailable && (!ui.original || credentialsChanged(draft))) ? "disabled" : ""}>${ui.busy ? "Saving…" : "Save channel"}</button>`}<button class="button button-secondary" type="button" data-action="cancel-channel" ${ui.busy || ui.createAttempt ? "disabled" : ""}>Cancel</button></form>` : `<button class="button button-secondary" type="button" data-action="new-channel" ${locked || unavailable ? "disabled" : ""}>Add notification channel</button>`}
  </section>`;
}
function credentialField(field, label, actionField, draft, present) {
  return `<div class="field"><label for="channel-${actionField}">${label} (${present ? "saved" : "not set"})</label><select class="select" id="channel-${actionField}" name="${actionField}">${(present === undefined ? ["replace", "clear"] : ["keep", "replace", "clear"]).map(action => `<option value="${action}" ${draft[actionField] === action ? "selected" : ""}>${action[0].toUpperCase() + action.slice(1)}</option>`).join("")}</select><input class="input" id="channel-${field}" name="${field}" aria-label="Replacement ${label.toLowerCase()}" type="password" autocomplete="new-password" value="${h(draft[field] || "")}" placeholder="Enter replacement ${label.toLowerCase()}" ${draft[actionField] === "replace" ? "required" : "disabled"}></div>`;
}
function endpointFields(draft, original) {
  return `${credentialField("endpoint", draft.type === "ntfy" ? "HTTPS topic URL" : "HTTPS endpoint URL", "endpoint_action", draft, original?.endpoint_set)}
    <div class="field"><label for="channel-auth_type">Authentication</label><select class="select" id="channel-auth_type" name="auth_type">${["none", "bearer", "basic"].map(type => `<option value="${type}" ${draft.auth_type === type ? "selected" : ""}>${{none: "None", bearer: "Bearer token", basic: "Username and password"}[type]}</option>`).join("")}</select></div>
    ${draft.auth_type !== "none" ? credentialField(draft.auth_type === "bearer" ? "auth_token" : "auth_password", draft.auth_type === "bearer" ? "Bearer token" : "Password", "auth_action", draft, original?.auth_configured) : ""}
    ${draft.auth_type === "basic" ? `<div class="field"><label for="channel-auth_username">Username</label><input class="input" id="channel-auth_username" name="auth_username" type="password" autocomplete="new-password" value="${h(draft.auth_username)}" ${draft.auth_action === "replace" ? "required" : "disabled"}></div>` : ""}
    ${draft.type === "ntfy" ? `<div class="field"><label for="channel-ntfy_priority">Priority</label><select class="select" id="channel-ntfy_priority" name="ntfy_priority">${[1, 2, 3, 4, 5].map(value => `<option value="${value}" ${Number(draft.ntfy_priority) === value ? "selected" : ""}>${["", "Minimum", "Low", "Default", "High", "Maximum"][value]}</option>`).join("")}</select></div>` : ""}
    `;
}
function testFeedback(test, type = "telegram") {
  if (test.uncertain) return "Test request outcome uncertain. Retry the same saved test request; no new send is authorized.";
  const status = {queued: "Test queued", running: "Test send in progress", sent: type === "telegram" ? "Test delivered" : type === 'email' ? 'Test accepted by SMTP server; inbox delivery is not confirmed' : "Test accepted by endpoint", failed: "Test failed", unknown: "Test outcome unknown; it may have been delivered. No automatic resend.", cancelled: "Test cancelled"}[test.status] || "Test status unavailable";
  return `${status}${test.error_code ? ` (${test.error_code})` : ""}${test.pollError ? "; status lookup failed, check again." : ""}${test.pollStopped ? "; automatic status polling stopped, check again." : ""}`;
}
export function channelController(state, {render, announce, refreshJobs, onLoad}) {
  state.channels = null;
  state.channelUI = {draft: null, original: null, busy: false, error: null, tests: {}, testAttempts: {}};
  const ui = state.channelUI;
  const readDraft = form => {
    const data = new FormData(form);
    return {...ui.draft, name: String(data.get("name") || ""), enabled: data.has("enabled"), type: data.get("type") || ui.original?.type || ui.draft.type,
      ...Object.fromEntries(Object.keys(secretActions).map(field => [field, data.get(field) || ui.draft[field]])),
      ...Object.fromEntries(Object.values(secretActions).flat().map(field => [field, String(data.get(field) || "")])),
      auth_type: data.get("auth_type") || ui.draft.auth_type, ntfy_priority: Number(data.get("ntfy_priority") || ui.draft.ntfy_priority), recover_failed: data.has("recover_failed")};
  };
  const mount = () => {
    const node = document.getElementById("channel-settings");
    if (!node) return;
    // Normal refreshes preserve mounted credential controls and their in-memory draft.
    if (ui.draft && document.getElementById("channel-form")) return;
    node.outerHTML = renderChannels(state);
  };
  async function load() {
    try {
      const saved = await api.listChannels(); state.channels = Array.isArray(saved) ? saved : saved.items;
      for (const channel of state.channels) if (channel.latest_test && !ui.testAttempts[channel.id]) ui.tests[channel.id] = channel.latest_test;
    }
    catch (error) { ui.error = error; mount(); return false; }
    mount(); onLoad?.(); return true;
  }
  function edit(channel) {
    ui.original = channel ? structuredClone(channel) : null;
    ui.draft = channel ? {...blank(), type: channel.type || "telegram", name: channel.name, enabled: channel.enabled, auth_type: channel.auth_type || "none", ntfy_priority: channel.ntfy_priority || 3, token_action: "keep", chat_action: "keep", recipient_action: 'keep', endpoint_action: "keep", auth_action: "keep"} : blank();
    ui.error = null; ui.latest = null; ui.conflictBlocked = false; render();
  }
  async function submit(form, replay = false) {
    if (ui.busy || ui.conflictBlocked || (ui.createAttempt && !replay)) return;
    if (!replay) ui.draft = readDraft(form);
    let payload = replay ? ui.createAttempt.payload : {...ui.draft};
    if (!replay) {
      payload = {...payload, name: payload.name.trim()};
      for (const [action, fields] of Object.entries(secretActions)) if (payload[action] !== "replace") for (const field of fields) delete payload[field];
      if (payload.auth_action === "keep") delete payload.auth_type;
      for (const field of payload.type === "telegram" ? ["endpoint_action", "endpoint", "auth_type", "auth_action", "auth_token", "auth_username", "auth_password", "ntfy_priority"] : ["token_action", "bot_token", "chat_action", "chat_id"]) delete payload[field];
      if (payload.type !== "ntfy") delete payload.ntfy_priority;
      if (payload.type !== 'email') { delete payload.recipient; delete payload.recipient_action; }
      else for (const field of ['endpoint_action','endpoint','auth_type','auth_action','auth_token','auth_username','auth_password']) delete payload[field];
      if (payload.type !== "telegram") for (const field of payload.auth_type === "bearer" ? ["auth_username", "auth_password"] : payload.auth_type === "basic" ? ["auth_token"] : ["auth_token", "auth_username", "auth_password"]) delete payload[field];
      if (ui.original) delete payload.type;
      if (!ui.original) delete payload.recover_failed;
    }
    const original = ui.original;
    const attempt = ui.createAttempt || (!original ? {key: crypto.randomUUID(), payload: structuredClone(payload)} : null);
    ui.busy = true; ui.error = null; render();
    try {
      const saved = original && !replay ? await api.updateChannel(original.id, payload, original.edit_version) : await api.createChannel(attempt.payload, attempt.key);
      if (!saved.id) { ui.createAttempt = attempt; ui.error = new Error("Creation remains in progress."); return; }
      ui.createAttempt = null; ui.draft = null; ui.original = null;
      announce("Channel saved."); await load(); await refreshJobs();
    } catch (error) {
      ui.error = error;
      if (!original && error.ambiguous) ui.createAttempt = attempt;
      if (!original && !error.ambiguous) ui.createAttempt = null;
      if (original && error.status === 409) {
        ui.conflictBlocked = true;
        ui.latest = null;
        if (await load()) ui.latest = state.channels.find(item => item.id === original.id);
      }
    } finally { ui.busy = false; render(); }
  }
  async function poll(channelId, operationId, turn = 0) {
    try {
      const saved = await api.getChannelTest(operationId);
      if (ui.tests[channelId]?.id && ui.tests[channelId].id !== operationId) return;
      ui.tests[channelId] = saved;
      if (!terminal(saved.status) && turn < 29 && !document.hidden) setTimeout(() => poll(channelId, operationId, turn + 1), 2000);
      else if (!terminal(saved.status)) ui.tests[channelId].pollStopped = true;
    } catch { ui.tests[channelId] = {...ui.tests[channelId], pollError: true}; }
    mount();
  }
  async function click(control) {
    const action = control.dataset.action;
    if (!["new-channel", "cancel-channel", "edit-channel", "delete-channel", "preview-channel", "close-channel-preview", "test-channel", "channel-test-status", "import-telegram", "retry-channel-create", "refetch-channel", "reconcile-channel", "use-server-channel"].includes(action)) return false;
    const channelId = control.closest("[data-channel-id]")?.dataset.channelId;
    const channel = state.channels?.find(item => item.id === channelId);
    if (action === "close-channel-preview") { ui.preview = null; render(); return true; }
    if (action === "new-channel") { if (!ui.busy && !ui.createAttempt) edit(); return true; }
    if (action === "edit-channel") { if (!ui.busy && !ui.createAttempt && channel) edit(channel); return true; }
    if (action === "cancel-channel") { if (!ui.busy && !ui.createAttempt) { ui.draft = null; ui.original = null; ui.conflictBlocked = false; ui.error = null; render(); } return true; }
    if (action === "retry-channel-create") { await submit(null, true); return true; }
    if (action === "refetch-channel") { ui.latest = null; if (await load()) ui.latest = state.channels?.find(item => item.id === ui.original?.id); render(); return true; }
    if (action === "use-server-channel") { edit(ui.latest); return true; }
    if (action === "reconcile-channel") {
      if (!document.getElementById("channel-conflict-confirm")?.checked) { announce("Confirm that you reviewed the saved configuration."); return true; }
      ui.original = structuredClone(ui.latest); ui.latest = null; ui.conflictBlocked = false; ui.error = null; render(); return true;
    }
    if (ui.busy) return true;
    ui.busy = true; ui.error = null; render();
    try {
      if (action === "import-telegram") {
        ui.importKey ||= crypto.randomUUID(); await api.importLegacyChannel(ui.importKey); announce("Legacy Telegram1 imported."); await load(); await refreshJobs();
      } else if (channel && action === "preview-channel") {
        const preview = await api.channelPreview(channel.id);
        ui.preview = previewText(preview);
      } else if (channel && action === "delete-channel") {
        const jobs = (channel.affected_jobs || []).map(job => job.name).join(", ");
        if (window.confirm(`Delete ${channel.name}? Affected jobs: ${jobs || "none"}. Unsent deliveries are cancelled. History is retained.`)) { await api.deleteChannel(channel.id, channel.edit_version); await load(); await refreshJobs(); }
      } else if (channel && ["test-channel", "channel-test-status"].includes(action)) {
        const prior = ui.tests[channel.id];
        if (action === "channel-test-status" && prior?.id) { await poll(channel.id, prior.id); return true; }
        const attempt = ui.testAttempts[channel.id] || {key: crypto.randomUUID(), version: channel.edit_version, contentVersion: state.settings?.content_version};
        if (!ui.testAttempts[channel.id] && !window.confirm(`Send a synthetic ${typeLabel(channel.type)} test to ${channel.name}?`)) return true;
        ui.testAttempts[channel.id] = attempt;
        try {
          const saved = await api.testChannel(channel.id, attempt.version, attempt.key, attempt.contentVersion); ui.tests[channel.id] = saved;
          delete ui.testAttempts[channel.id]; if (!terminal(saved.status)) poll(channel.id, saved.id);
        } catch (error) {
          if (error.ambiguous) ui.tests[channel.id] = {uncertain: true}; else delete ui.testAttempts[channel.id];
          throw error;
        }
      }
    } catch (error) { ui.error = error; }
    finally { ui.busy = false; render(); }
    return true;
  }
  function input(event) {
    if (!event.target.closest("#channel-form")) return false;
    if (ui.busy || ui.createAttempt || ui.conflictBlocked) return true;
    ui.draft = readDraft(document.getElementById("channel-form"));
    if (["type", "auth_type"].includes(event.target.name)) {
      if (event.target.name === "type") ui.draft = {...blank(), name: ui.draft.name, enabled: ui.draft.enabled, type: event.target.value};
      else { ui.draft.auth_action = event.target.value === "none" ? "clear" : "replace"; for (const field of secretActions.auth_action) ui.draft[field] = ""; }
      render();
    } else if (secretActions[event.target.name]) {
      if (event.target.name === "auth_action" && event.target.value === "clear") { ui.draft.auth_type = "none"; render(); return true; }
      for (const field of secretActions[event.target.name]) {
        const input = document.getElementById(`channel-${field}`); if (!input) continue;
        input.disabled = event.target.value !== "replace"; input.required = !input.disabled;
        if (input.disabled) { input.value = ""; ui.draft[field] = ""; }
      }
    }
    return true;
  }
  return {load, click, input, submit};
}
