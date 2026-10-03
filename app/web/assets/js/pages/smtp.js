import { api } from '../api.js';
import { escapeHtml as h, icon } from '../ui.js';

const secrets = ['sender_email', 'username', 'password'];
const draftFrom = saved => ({enabled: saved.enabled, host: saved.host || '', port: saved.port || 587,
  tls_mode: saved.tls_mode || 'starttls', sender_name: saved.sender_name || '',
  ...Object.fromEntries(secrets.flatMap(field => [[field, ''], [field + '_action', 'keep']])), recover_failed: false});
export function renderSmtp(state) {
  const ui = state.smtpUI || {}, saved = state.smtp, draft = ui.draft || (saved && draftFrom(saved));
  return `<section class="panel" id="smtp-settings" aria-labelledby="smtp-heading"><div class="panel-header">${icon('send')}<div><h2 id="smtp-heading">Email transport</h2><p>One SMTP connection serves all named email recipients.</p></div></div>
    ${ui.loadError ? `<p class="form-error" role="alert">${h(ui.loadError.message)}</p>` : ui.error ? `<p class="form-error" role="alert">${h(ui.error.message)}</p>` : ''}
    ${!draft ? '<p>Loading SMTP configuration…</p><button type="button" class="text-button" data-action="smtp-refresh">Retry</button>' : `<form id="smtp-form" class="channel-form" autocomplete="off"><fieldset ${ui.busy || ui.blocked ? 'disabled' : ''}>
      <label class="checkbox-line"><span>Enable email delivery</span><input name="enabled" type="checkbox" ${draft.enabled ? 'checked' : ''}></label>
      <div class="field"><label for="smtp-host">SMTP host</label><input class="input" id="smtp-host" name="host" maxlength="253" value="${h(draft.host)}"></div>
      <div class="smtp-connection"><div class="field"><label for="smtp-port">Port</label><input class="input" id="smtp-port" name="port" type="number" min="1" max="65535" value="${h(draft.port)}" required></div><div class="field"><label for="smtp-tls">TLS</label><select class="select" id="smtp-tls" name="tls_mode"><option value="starttls" ${draft.tls_mode === 'starttls' ? 'selected' : ''}>STARTTLS (usually 587)</option><option value="implicit_tls" ${draft.tls_mode === 'implicit_tls' ? 'selected' : ''}>Implicit TLS (usually 465)</option></select></div></div>
      <div class="field"><label for="smtp-sender_name">Sender name</label><input class="input" id="smtp-sender_name" name="sender_name" maxlength="120" value="${h(draft.sender_name)}"></div>
      ${secrets.map(field => `<div class="field"><label for="smtp-${field}-action">${h({sender_email:'Sender address',username:'Username',password:'Password'}[field])} (${saved[field + '_set'] ? 'saved' : 'not set'})</label><select class="select" id="smtp-${field}-action" name="${field}_action">${['keep','replace','clear'].map(action => `<option value="${action}" ${draft[field + '_action'] === action ? 'selected' : ''}>${action[0].toUpperCase() + action.slice(1)}</option>`).join('')}</select><input class="input" id="smtp-${field}" name="${field}" type="password" autocomplete="new-password" aria-label="Replacement ${h(field.replaceAll('_',' '))}" value="${h(draft[field])}" ${draft[field + '_action'] === 'replace' ? 'required' : 'disabled'}></div>`).join('')}
      <label class="checkbox-line"><span>Recover fresh failed email deliveries after repairing credentials for this same SMTP destination</span><input name="recover_failed" type="checkbox" ${draft.recover_failed ? 'checked' : ''}></label>
      <p class="muted">Use verified TLS. Clear username and password together for unauthenticated SMTP. Saving does not send; test a saved email channel below.</p></fieldset>
      ${ui.blocked ? `<div class="notice notice-warning"><div><p>Your draft is preserved. Review the saved SMTP configuration before applying it.</p>${ui.latest ? `<p>Saved host: ${h(ui.latest.host || 'not set')}, port: ${h(ui.latest.port)}, TLS: ${h(ui.latest.tls_mode)}, enabled: ${ui.latest.enabled ? 'yes' : 'no'}, sender name: ${h(ui.latest.sender_name || 'not set')}. Credentials remain masked.</p><label class="checkbox-line"><span>I reviewed the saved configuration and explicit credential operations.</span><input type="checkbox" id="smtp-review-confirm"></label><button type="button" class="text-button" data-action="smtp-apply">Apply draft to latest version</button><button type="button" class="text-button" data-action="smtp-use-saved">Use saved values</button>` : '<button type="button" class="text-button" data-action="smtp-refresh">Fetch saved values</button>'}</div></div>` : ''}
      <button class="button button-primary" type="submit" ${ui.busy || ui.blocked || (!state.settings?.notification_secret_configured && (draft.enabled || secrets.some(field => draft[field + '_action'] === 'replace'))) ? 'disabled' : ''}>${ui.busy ? 'Saving…' : 'Save SMTP transport'}</button><button type="button" class="button button-secondary" data-action="smtp-cancel" ${ui.busy ? 'disabled' : ''}>Discard draft</button>
    </form>`}</section>`;
}
export function smtpController(state, {render, announce, refreshJobs}) {
  state.smtp = null; state.smtpUI = {draft: null, busy: false, error: null, loadError: null};
  const ui = state.smtpUI;
  const read = form => { const data = new FormData(form); return {enabled: data.has('enabled'), host: String(data.get('host') || '').trim(), port: Number(data.get('port')), tls_mode: data.get('tls_mode'), sender_name: String(data.get('sender_name') || '').trim(), ...Object.fromEntries(secrets.flatMap(field => [[field, String(data.get(field) || '')], [field + '_action', data.get(field + '_action')]])), recover_failed: data.has('recover_failed')}; };
  async function load() {
    try { const saved = await api.getSmtp(); ui.loadError = null; if (ui.blocked) ui.latest = saved; else if (!ui.draft && !ui.busy) state.smtp = saved; }
    catch (error) { ui.loadError = error; }
    const node = document.getElementById('smtp-settings');
    if (node && !ui.draft && !ui.busy) node.outerHTML = renderSmtp(state);
  }
  function input(event) {
    if (!event.target.closest('#smtp-form')) return false;
    if (ui.busy || ui.blocked) return true;
    if (['username_action', 'password_action'].includes(event.target.name)) {
      const form = document.getElementById('smtp-form'), other = event.target.name === 'username_action' ? 'password_action' : 'username_action';
      const otherAction = form.elements[other];
      if (event.target.value === 'clear' || otherAction.value === 'clear') otherAction.value = event.target.value;
      for (const field of ['username', 'password']) {
        const action = form.elements[field + '_action'].value, control = form.elements[field];
        control.disabled = action !== 'replace'; control.required = !control.disabled;
        if (control.disabled) control.value = '';
      }
      ui.draft = read(form);
      return true;
    }
    ui.draft = read(document.getElementById('smtp-form'));
    if (event.target.name.endsWith('_action')) {
      const field = event.target.name.slice(0,-7), control = document.getElementById('smtp-' + field);
      control.disabled = event.target.value !== 'replace'; control.required = !control.disabled;
      if (control.disabled) { control.value = ''; ui.draft[field] = ''; }
    }
    document.querySelector('#smtp-form [type="submit"]').disabled = !state.settings?.notification_secret_configured && (ui.draft.enabled || secrets.some(field => ui.draft[field + '_action'] === 'replace'));
    return true;
  }
  async function submit(form) {
    if (ui.busy || ui.blocked || !state.smtp) return;
    ui.draft = read(form); const payload = {...ui.draft};
    for (const field of secrets) if (payload[field + '_action'] !== 'replace') delete payload[field];
    ui.busy = true; ui.error = null; render();
    try { state.smtp = await api.updateSmtp(payload,state.smtp.edit_version); ui.draft = null; announce('SMTP transport saved.'); await refreshJobs(); }
    catch (error) { ui.error = error; if (error.status === 409 || error.ambiguous) { ui.blocked = true; ui.latest = null; await load(); } }
    finally { ui.busy = false; render(); }
  }
  async function click(control) {
    const action = control.dataset.action;
    if (ui.busy) return;
    if (action === 'smtp-refresh') { await load(); render(); }
    if (action === 'smtp-cancel' || action === 'smtp-use-saved') { if (ui.latest) state.smtp = ui.latest; ui.draft = null; ui.blocked = false; ui.error = null; ui.latest = null; await load(); render(); }
    if (action === 'smtp-apply' && ui.latest) { if (!document.getElementById('smtp-review-confirm')?.checked) { announce('Confirm that you reviewed the saved configuration.'); return; } state.smtp = ui.latest; ui.latest = null; ui.blocked = false; ui.error = null; render(); }
  }
  return {load,input,submit,click};
}
