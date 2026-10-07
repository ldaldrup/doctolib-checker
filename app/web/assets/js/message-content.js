import { escapeHtml as h } from './ui.js';

export const CONTENT_FIELDS = {job_name: 'Job name', practitioner: 'Practitioner', practice: 'Practice', earliest_appointment: 'Earliest appointment', check_time: 'Check time', time_zone: 'Time zone', booking_link: 'Booking link'};
export const CONTENT_PRESETS = {standard: ['practitioner', 'practice', 'earliest_appointment', 'booking_link'], compact: ['practitioner', 'earliest_appointment', 'booking_link']};
export const defaultContent = () => ({preset: 'standard', fields: [...CONTENT_PRESETS.standard], silent: false});
export function readContent(form, prefix) {
  const data = new FormData(form), preset = data.get(`${prefix}-preset`) || 'standard';
  return {preset, fields: CONTENT_PRESETS[preset] ? [...CONTENT_PRESETS[preset]] : [...form.querySelectorAll(`[name="${prefix}-fields"]:checked`)].map(input => input.value), silent: data.has(`${prefix}-silent`)};
}
export function contentSummary(content) {
  if (content == null) return 'Inherit Settings';
  const fields = CONTENT_PRESETS[content.preset] || content.fields || [];
  return `${content.preset[0].toUpperCase() + content.preset.slice(1)}: ${fields.map(field => CONTENT_FIELDS[field]).join(', ')}; ${content.silent ? 'silent delivery' : 'normal delivery'}`;
}
export function contentControls(content, prefix) {
  const value = content || defaultContent();
  return `<div class="message-content-controls"><div class="field"><label for="${prefix}-preset">Message preset</label><select class="select" id="${prefix}-preset" name="${prefix}-preset">${['standard', 'compact', 'custom'].map(preset => `<option value="${preset}" ${value.preset === preset ? 'selected' : ''}>${preset[0].toUpperCase() + preset.slice(1)}</option>`).join('')}</select></div><fieldset class="message-field-options"><legend>Message fields</legend>${Object.entries(CONTENT_FIELDS).map(([field, label]) => `<label class="checkbox-line"><span>${h(label)}</span><input type="checkbox" id="${prefix}-field-${field}" name="${prefix}-fields" value="${field}" ${(CONTENT_PRESETS[value.preset] || value.fields).includes(field) ? 'checked' : ''} ${value.preset !== 'custom' ? 'disabled' : ''}></label>`).join('')}</fieldset><label class="checkbox-line"><span>Silent delivery</span><input type="checkbox" id="${prefix}-silent" name="${prefix}-silent" ${value.silent ? 'checked' : ''}></label><p class="muted">Silent delivery supports Telegram and ntfy only. Telegram disables notification sound; ntfy uses low priority. Email and webhooks reject silent delivery.</p><p class="muted">Missing details use Unavailable or a target-name fallback. Long values may be shortened. Booking links appear when available. Webhooks keep their fixed event JSON; these field choices affect Telegram, ntfy and email.</p></div>`;
}
export function previewText(preview) {
  let text;
  if (preview.subject) text = `${preview.subject}\n\n${preview.text}`;
  else if (preview.html || preview.parse_mode === 'HTML') text = new DOMParser().parseFromString(preview.html || preview.text, 'text/html').body.textContent;
  else if (preview.message) text = `${preview.title || ''}\n\n${preview.message}${preview.click ? '\n' + preview.click : ''}`;
  else text = preview.text || JSON.stringify(preview.json || preview, null, 2);
  return text + (preview.warnings?.length ? '\n\nRendering notes: ' + preview.warnings.join(' ') : '');
}
