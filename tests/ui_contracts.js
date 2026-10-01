import { deliveryNotice } from "/assets/js/delivery-view.js";
import { api, ApiError, createJobPayload, updateJobPayload, settingsPayload } from '/assets/js/api.js';
import { deriveJobView, historyKey, safeBookingUrl } from '/assets/js/job-view.js';

import { settingWarning } from '/assets/js/pages/settings.js';
import { jobStatus, checkedAgo, renderJobList, compactDuration, nextCheck, nextCheckTitle } from '/assets/js/pages/jobs.js';

const lines = [];
const assert = (condition, message = 'Assertion failed') => { if (!condition) throw new Error(message); };
const equal = (actual, expected) => assert(JSON.stringify(actual) === JSON.stringify(expected), `${JSON.stringify(actual)} != ${JSON.stringify(expected)}`);
const t = number => `2026-10-01T10:00:${String(number).padStart(2, '0')}Z`;
const url = id => `https://www.doctolib.de/test-${id}/booking/availabilities?placeId=practice-${id}&motiveIds[]=1`;
const target = id => ({id, active: 1, booking_url: url(id), last_validated_at: t(0)});
const job = {id: 'j', search_revision: 1, edit_version: 1, targets: [target('a'), target('b')], updated_at: t(0), time_zone: 'Europe/Berlin', date_mode: 'first_available', horizon_days: 15, interval_seconds: 300, insurance_sector: 'public', telehealth: false, telegram_enabled: false, name: 'Contract'};
const result = (id, status, extras = {}) => ({target_id: id, search_revision: 1, snapshot_known: 1, published: 1, status, checked_at: t(12), count_complete: true, booking_url: url(id), ...(status === 'available' ? {earliest_slot: '2026-10-03T09:00:00Z'} : {}), ...extras});
const run = (outcome, results, extras = {}) => ({id: 'r', job_id: 'j', search_revision: 1, snapshot_known: 1, search_snapshot: {targets: job.targets}, started_at: t(10), outcome, results, ...extras});
const settings = {minimum_poll_interval_seconds: 300, time_zone: 'Europe/Berlin', telegram_configured: true};
const draft = {...job, target_urls: job.targets.map(item => item.booking_url)};
async function test(name, fn) {
  try { await fn(); lines.push(`PASS ${name}`); }
  catch (error) { lines.push(`FAIL ${name}: ${error.message}`); }
}
async function rejects(fn, kind) {
  try { await fn(); throw new Error('Expected rejection'); }
  catch (error) { assert(error instanceof ApiError && error.kind === kind, `Expected ${kind}, got ${error.message}`); return error; }
}
await test('delivery notices separate dispatcher failure and recovery from checking', () => {
  const state = {load: {status: {phase: 'loaded'}}, status: {worker_alive: true, telegram_configured: true, dispatcher_alive: false,
    delivery_backlog: {queued: 2, action_required: 1, exhausted: 1, uncertain: 3}}};
  const notice = deliveryNotice(state);
  assert(notice.includes('dispatcher unavailable') && notice.includes('Availability checking runs independently'));
  assert(notice.includes('2 alert(s) queued') && notice.includes('credential or recipient repair'));
  assert(notice.includes('retry limit') && notice.includes('3 alert(s) may already have been delivered'));
  state.status.dispatcher_alive = true; assert(deliveryNotice(state).includes('dispatcher online'));
  state.load.status.phase = 'stale'; assert(deliveryNotice(state).includes('unknown'));
});
await test('next-check tooltip shows scheduled datetime and job timezone', () => {
  const scheduled = {...job, next_check_at: '2026-10-01T10:05:01Z'};
  equal(nextCheckTitle(scheduled), 'Scheduled next check: 01.10.2026, 12:05:01 (Europe/Berlin)');
  equal(nextCheckTitle({...scheduled, time_zone: 'UTC'}), 'Scheduled next check: 01.10.2026, 10:05:01 (UTC)');
  equal(nextCheckTitle({...scheduled, status: 'paused'}), 'No check scheduled while paused.');
  equal(nextCheckTitle({...scheduled, next_check_at: 'invalid'}), 'Next check time unavailable.');
  const state = {jobs: [scheduled], filter: 'all', intervalFilter: 'all', load: {jobs: {phase: 'loaded'}, status: {phase: 'loaded'}}, status: {worker_alive: true}, views: new Map()};
  assert(renderJobList(state).includes(`<span title="${nextCheckTitle(scheduled)}">(next:`));
});
await test('compact next-check countdown uses two adjacent units and reports scheduler blockers', () => {
  const now = Date.parse('2026-10-01T10:00:00Z');
  for (const [seconds, text] of [[301, '5m1s'], [3723, '1h2m'], [10800, '3h'], [86461, '1d'], [86400 + 7200, '1d2h'], [59, '59s']]) equal(compactDuration(seconds), text);
  const state = {load: {status: {phase: 'loaded'}}, status: {worker_alive: true}, views: new Map()};
  const scheduled = {...job, next_check_at: new Date(now + 301000).toISOString()};
  equal(nextCheck(scheduled, state, now), '5m1s'); equal(nextCheck(scheduled, state, now + 302000), 'due');
  equal(nextCheck({...scheduled, status: 'paused'}, state, now), 'paused');
  state.status.worker_alive = false; equal(nextCheck(scheduled, state, now), 'blocked');
  state.status.worker_alive = true; state.views.set(job.id, {running: true}); equal(nextCheck(scheduled, state, now), 'checking');
});
await test('relative check times appear beneath monitoring, with failures retaining their explanation', () => {
  const now = Date.parse('2026-10-01T10:00:00Z');
  for (const [seconds, text] of [[0, '0s'], [1, '1s'], [59, '59s'], [60, '1m'], [3600, '1h'], [432000, '5d'], [31536000, '1y'], [63072000, '2y']]) {
    equal(checkedAgo(new Date(now - seconds * 1000).toISOString(), now), `Checked ${text} ago`);
  }
  equal(checkedAgo('invalid', now), null); equal(checkedAgo(new Date(now + 1000).toISOString(), now), 'Checked 0s ago');
  const state = {jobs: [job], load: {status: {phase: 'loaded'}, jobs: {phase: 'loaded'}}, status: {worker_alive: true}, views: new Map([[job.id, {state: 'no_availability', checkedAt: new Date(now - 1000).toISOString()}]]), filter: 'all', intervalFilter: 'all'};
  equal(jobStatus(job, state, now).detail, 'Checked 1s ago');
  const html = renderJobList(state); assert(html.includes('<small>Checked ') && !html.includes('No availability reported') && !html.includes('class="job-evidence"'));
  state.status.worker_alive = false; equal(jobStatus(job, state, now).detail, 'Worker unavailable');
  state.status.worker_alive = true; state.views.get(job.id).state = 'error'; equal(jobStatus(job, state, now).detail, 'Targets could not be checked');
});
await test('setting warnings apply only to unsaved fields and clear after persistence', () => {
  const saved = {default_interval_seconds: 300, request_spacing_seconds: 3};
  const state = {settings: saved, settingsDraft: {...saved, request_spacing_seconds: 4}};
  equal(settingWarning(state, 'default_interval_seconds'), null);
  assert(settingWarning(state, 'request_spacing_seconds') && !settingWarning(state, 'request_spacing_seconds').failed);
  state.settingsError = new ApiError('Rejected', {fields: {request_spacing_seconds: 'Invalid'}});
  assert(settingWarning(state, 'request_spacing_seconds').failed);
  state.settingsDraft.request_spacing_seconds = '3'; equal(settingWarning(state, 'request_spacing_seconds'), null);
});
await test('job status distinguishes enabled, blocked, paused, failed and unknown health', () => {
  const state = {load: {status: {phase: 'loaded'}}, status: {worker_alive: false}, views: new Map()};
  equal(jobStatus(job, state).label, 'Blocked'); equal(jobStatus(job, state).detail, 'Worker unavailable');
  equal(jobStatus({...job, status: 'paused'}, state).label, 'Paused');
  state.status.worker_alive = true; equal(jobStatus(job, state).label, 'Monitoring');
  state.load.status.phase = 'stale'; equal(jobStatus(job, state).label, 'Status unknown');
  state.load.status.phase = 'loaded'; state.views.set(job.id, {state: 'error'}); equal(jobStatus(job, state).label, 'Check failed');
  state.views.set(job.id, {state: 'partial'}); equal(jobStatus(job, state).label, 'Check incomplete');
  state.views.set(job.id, {state: 'unknown', error: new Error('failed')}); equal(jobStatus(job, state).detail, 'Check history unavailable');
});
await test('multi-target earliest slot and link stay together, alongside errors', () => {
  const view = deriveJobView(job, [run('partial_error', [result('a', 'error'), result('b', 'available')])]);
  assert(view.detected && view.warning.includes('could not')); equal(view.slot.target_id, 'b'); equal(view.slot.booking_url, url('b')); assert(!view.coverageComplete);
  const earliest = deriveJobView(job, [run('completed', [result('a', 'available'), result('b', 'available', {earliest_slot: '2026-10-02T08:00:00Z'})])]);
  equal(earliest.slot.target_id, 'b'); equal(earliest.slot.booking_url, url('b'));
});
await test('removed targets do not contribute slots or errors', () => {
  const view = deriveJobView({...job, targets: [target('a')]}, [run('completed', [result('a', 'no_availability'), result('b', 'available')])]);
  equal(view.state, 'no_availability'); assert(!view.detected);
});
await test('negative evidence requires complete successful coverage', () => {
  equal(deriveJobView(job, [run('completed', [result('a', 'no_availability'), result('b', 'no_availability')])]).state, 'no_availability');
  for (const results of [[result('a', 'no_availability')], [result('a', 'no_availability', {count_complete: false}), result('b', 'no_availability')], [result('a', 'error'), result('b', 'error')]]) {
    const view = deriveJobView(job, [run('completed', results)]); assert(view.state !== 'no_availability'); assert(!view.coverageComplete);
  }
});
await test('empty running/interrupted checks retain preceding historical detection', () => {
  for (const outcome of ['running', 'interrupted']) {
    const view = deriveJobView(job, [run(outcome, [], {id: 'new', started_at: t(20)}), run('completed', [result('a', 'available'), result('b', 'no_availability')])]);
    assert(view.detected && view.historical && view.warning && !view.coverageComplete); equal(view.slot.run_id, 'r');
  }
  equal(deriveJobView(job, [run('running', [])]).state, 'running');
  assert(deriveJobView(job, [run('interrupted', [])]).state !== 'no_availability');
});
await test('empty running or interrupted checks do not revive contradicted older detections', () => {
  for (const outcome of ['running', 'interrupted']) {
    const view = deriveJobView(job, [
      run(outcome, [], {id: 'new', started_at: t(30)}),
      run('completed', [result('a', 'no_availability'), result('b', 'no_availability')], {id: 'negative', started_at: t(20)}),
      run('completed', [result('a', 'available'), result('b', 'no_availability')]),
    ]);
    assert(!view.detected && !view.slot && !view.coverageComplete);
    assert(view.state !== 'no_availability');
  }
});
await test('cosmetic edits retain proven revision evidence', () => {
  const edited = {...job, updated_at: t(15), edit_version: 2, status: 'paused', name: 'Renamed', interval_seconds: 600};
  const view = deriveJobView(edited, [run('completed', [result('a', 'available'), result('b', 'no_availability')])]);
  assert(view.detected && !view.historical && !view.warning && view.coverageComplete);
  equal(deriveJobView(edited, [run('completed', [result('a', 'no_availability'), result('b', 'no_availability')])]).state, 'no_availability');
});
await test('obsolete revisions retain historical positives and cannot prove current negatives', () => {
  const edited = {...job, search_revision: 2};
  const view = deriveJobView(edited, [run('completed', [result('a', 'available'), result('b', 'no_availability')])]);
  assert(view.detected && view.historical && view.warning.includes('earlier search revision') && !view.coverageComplete);
  equal(deriveJobView(edited, [run('completed', [result('a', 'no_availability'), result('b', 'no_availability')])]).state, 'awaiting');
  // Explicit matching revisions supersede local and validation timestamps.
  equal(deriveJobView(job, [run('completed', [result('a', 'no_availability'), result('b', 'no_availability')])], {invalidatedAt: t(15)}).state, 'no_availability');
});
await test('migrated unknown snapshots never assert current negative evidence', () => {
  const legacy = extras => run('completed', [result('a', 'no_availability'), result('b', 'no_availability')], {snapshot_known: 0, search_revision: null, search_snapshot: null, ...extras});
  equal(deriveJobView(job, [legacy()]).state, 'awaiting');
  const view = deriveJobView(job, [legacy({results: [result('a', 'available')]})]);
  assert(view.detected && view.historical && view.warning.includes('unknown') && !view.coverageComplete);
  const unpublished = deriveJobView(job, [run('completed', [result('a', 'no_availability', {published: 0}), result('b', 'no_availability')])]);
  assert(!unpublished.coverageComplete && unpublished.state !== 'no_availability');
});
await test('history signatures include search/target/run evidence but ignore list last_result', () => {
  equal(historyKey(job), historyKey({...job, last_result: result('a', 'available')}));
  for (const changed of [{...job, search_revision: 2}, {...job, horizon_days: 30}, {...job, targets: [target('b')]}, {...job, last_started_at: t(20)}, {...job, targets: [{...target('a'), last_validated_at: t(3)}, target('b')]}]) assert(historyKey(job) !== historyKey(changed));
});
await test('payload dates, URL safety, bounds, allowlisting, and Telegram preservation', async () => {
  const first = createJobPayload({...draft, earliest_date: '', latest_date: '', unsupported: true}, settings); assert(!Object.hasOwn(first, 'earliest_date') && !Object.hasOwn(first, 'unsupported'));
  const custom = createJobPayload({...draft, date_mode: 'custom', earliest_date: '2026-10-01', latest_date: '2026-10-31'}, settings); equal(custom.earliest_date, '2026-10-01');
  const patch = updateJobPayload(draft, {...job, date_mode: 'custom', earliest_date: '2026-10-01', latest_date: '2026-10-31'}, settings); equal(patch.earliest_date, null); equal(patch.latest_date, null); assert(!Object.hasOwn(patch, 'target_urls'));
  for (const unsafe of ['http://www.doctolib.de/a/booking/availabilities', 'https://evil.example/a/booking/availabilities', 'https://u:p@www.doctolib.de/a/booking/availabilities', 'https://www.doctolib.de:444/a/booking/availabilities', 'https://www.doctolib.de/a']) {
    equal(safeBookingUrl(unsafe), null); await rejects(() => createJobPayload({...draft, target_urls: [unsafe]}, settings), 'validation');
  }
  await rejects(() => createJobPayload({...draft, target_urls: [url('a'), `${url('a')}#duplicate`]}, settings), 'validation');
  await rejects(() => createJobPayload({...draft, date_mode: 'custom', earliest_date: '2026-02-30', latest_date: '2026-03-01'}, settings), 'validation');
  await rejects(() => createJobPayload({...draft, interval_seconds: 300}, {...settings, minimum_poll_interval_seconds: 600}), 'validation');
  const telegram = updateJobPayload({...draft, name: 'Changed', telegram_enabled: false}, {...job, telegram_enabled: true}, {...settings, telegram_configured: false}); assert(!Object.hasOwn(telegram, 'telegram_enabled')); equal(telegram.name, 'Changed');
  equal(settingsPayload({default_interval_seconds: '600', request_spacing_seconds: '3.5', ignored: true}, settings), {default_interval_seconds: 600, request_spacing_seconds: 3.5});
});
const nativeFetch = globalThis.fetch;
await test('native transport headers, relative paths, allowlists and no automatic mutation retry', async () => {
  const calls = [];
  globalThis.fetch = async (path, options) => { calls.push({path, options}); return new Response('{}', {headers: {'Content-Type': 'application/json'}}); };
  await api.getSettings(); await api.createJob({...draft, unsupported: true}); await api.updateSettings({default_interval_seconds: 600, unsupported: true});
  for (const {path, options} of calls) { assert(path.startsWith('/api/v1/')); equal(options.credentials, 'same-origin'); equal(options.redirect, 'manual'); equal(options.cache, 'no-store'); equal(options.headers.Accept, 'application/json'); assert(options.signal instanceof AbortSignal); }
  equal(calls[1].options.headers['Content-Type'], 'application/json'); assert(!JSON.parse(calls[1].options.body).unsupported); assert(!JSON.parse(calls[2].options.body).unsupported);
  let count = 0; globalThis.fetch = async () => { count++; throw new TypeError('Test connection failed'); };
  const error = await rejects(() => api.createJob(draft), 'network'); assert(error.ambiguous); equal(count, 1);
});
await test('redirect/auth, HTTP errors, JSON validation fields and protocol failures', async () => {
  for (const [status, kind] of [[302, 'auth'], [401, 'auth'], [403, 'auth'], [404, 'missing'], [409, 'conflict'], [422, 'validation'], [502, 'upstream']]) {
    globalThis.fetch = async () => new Response(JSON.stringify({detail: [{loc: ['body', 'name'], msg: 'Name required'}]}), {status, headers: {'Content-Type': 'application/json'}});
    const error = await rejects(() => api.getJob('a'), kind); equal(error.status, status); if (status === 422) equal(error.fields.name, 'Name required');
  }
  globalThis.fetch = async () => ({type: 'opaqueredirect', status: 0}); await rejects(() => api.getStatus(), 'auth');
  globalThis.fetch = async () => new Response('<html>Sign in</html>', {headers: {'Content-Type': 'text/html'}}); await rejects(() => api.getStatus(), 'protocol');
  globalThis.fetch = async () => new Response('{broken', {headers: {'Content-Type': 'application/json'}}); await rejects(() => api.getStatus(), 'protocol');
});
await test('timeout and cancellation are distinct, with uncertain mutation outcome and no retry', async () => {
  const nativeTimer = globalThis.setTimeout;
  let calls = 0;
  globalThis.fetch = async (path, options) => {
    calls++;
    return new Promise((resolve, reject) => {
      if (options.signal.aborted) reject(new DOMException('Cancelled', 'AbortError'));
      else options.signal.addEventListener('abort', () => reject(new DOMException('Cancelled', 'AbortError')), {once: true});
    });
  };
  try {
    globalThis.setTimeout = callback => nativeTimer(callback, 0);
    const timeout = await rejects(() => api.createJob(draft), 'timeout'); assert(timeout.ambiguous); equal(calls, 1);
    globalThis.setTimeout = nativeTimer;
    const controller = new AbortController(); controller.abort();
    const cancelled = await rejects(() => api.getStatus({signal: controller.signal}), 'cancelled'); assert(!cancelled.ambiguous); equal(calls, 2);
  } finally { globalThis.setTimeout = nativeTimer; }
});
globalThis.fetch = nativeFetch;
await test('15-second refresh retains unchanged cards and unsaved editor controls', async () => {
  const fixtureUrl = 'https://www.doctolib.de/praxis/berlin/beispiel/booking/availabilities?placeId=practice-123&motiveIds%5B%5D=789&practitionerId=456';
  const saved = await api.createJob({...draft, name: 'Refresh regression', target_urls: [fixtureUrl], telegram_enabled: false});
  const frame = document.createElement('iframe'); frame.src = '/#jobs'; document.body.append(frame);
  const waitUntil = async predicate => {
    const deadline = Date.now() + 22000;
    while (!predicate()) { if (Date.now() > deadline) throw new Error('Timed out waiting for UI refresh'); await new Promise(resolve => setTimeout(resolve, 100)); }
  };
  try {
    await waitUntil(() => frame.contentDocument?.querySelector(`[data-job-id="${saved.id}"] .job-state small`)?.textContent === 'Worker unavailable');
    const doc = frame.contentDocument;
    doc.querySelector(`[data-job-id="${saved.id}"] [data-action="edit"]`).click();
    await waitUntil(() => doc.querySelector('#job-name')?.value === 'Refresh regression');
    const name = doc.querySelector('#job-name'); name.value = 'Unsaved draft'; name.dispatchEvent(new Event('input', {bubbles: true})); name.focus();
    await new Promise(resolve => setTimeout(resolve, 500));
    const card = doc.querySelector(`[data-job-id="${saved.id}"]`);
    const countReads = () => frame.contentWindow.performance.getEntriesByType('resource').filter(entry => entry.name.endsWith('/api/v1/status')).length;
    const reads = countReads(), started = Date.now();
    const notices = []; const observer = new MutationObserver(() => notices.push(doc.querySelector('#jobs-load-state')?.textContent));
    observer.observe(doc.querySelector('#jobs-load-state'), {childList: true, subtree: true});
    await waitUntil(() => countReads() > reads);
    await new Promise(resolve => setTimeout(resolve, 300)); observer.disconnect();
    assert(Date.now() - started < 18000, 'Refresh did not occur within 15-second cadence');
    assert(doc.querySelector(`[data-job-id="${saved.id}"]`) === card, 'Unchanged card was replaced');
    assert(doc.querySelector('#job-name') === name, 'Refresh replaced the editor');
    equal(name.value, 'Unsaved draft');
    assert(doc.activeElement === name, 'Refresh moved focus away from editor');
    assert(!notices.some(text => text?.includes('Loading jobs')), 'Refresh showed transient loading notice');
    assert(!doc.querySelector('.page-actions [data-action="refresh"]'), 'Toolbar Refresh remains');
  } finally { frame.remove(); await api.deleteJob(saved.id); }
});
await test('settings autosave preserves edits during writes, skips invalid values and exposes retry', async () => {
  const original = await api.getSettings();
  const frame = document.createElement('iframe'); frame.src = '/#settings'; document.body.append(frame);
  const waitUntil = async predicate => {
    const deadline = Date.now() + 6000;
    while (!predicate()) { if (Date.now() > deadline) throw new Error('Autosave timed out'); await new Promise(resolve => setTimeout(resolve, 50)); }
  };
  try {
    await waitUntil(() => frame.contentDocument?.querySelector('#request-spacing'));
    const doc = frame.contentDocument, win = frame.contentWindow, input = doc.querySelector('#request-spacing');
    assert(!doc.querySelector('#settings-save-status'), 'Persistent save label remains');
    assert(!doc.querySelector('[data-action="discard-settings"]') && !doc.querySelector('[type="submit"]'), 'Manual settings buttons remain');
    const fetch = win.fetch.bind(win); let writes = 0, fail = false;
    win.fetch = async (url, options) => {
      if (options?.method === 'PUT') {
        writes++;
        if (fail) return new win.Response(JSON.stringify({detail: 'Test save rejected'}), {status: 422, headers: {'Content-Type': 'application/json'}});
        await new Promise(resolve => setTimeout(resolve, 250));
      }
      return fetch(url, options);
    };
    const width = doc.querySelector('.panel').getBoundingClientRect().width, controlX = input.getBoundingClientRect().x;
    const edit = value => { input.value = value; input.dispatchEvent(new win.Event('input', {bubbles: true})); input.focus(); };
    edit(String(original.request_spacing_seconds)); await new Promise(resolve => setTimeout(resolve, 700)); equal(writes, 0);
    edit('4'); await waitUntil(() => doc.querySelector('#settings-form').getAttribute('aria-busy') === 'true');
    edit('5'); await waitUntil(() => doc.querySelector('[data-setting-warning="request_spacing_seconds"]').hidden);
    await waitUntil(() => writes === 2); await waitUntil(() => doc.querySelector('[data-setting-warning="request_spacing_seconds"]').hidden);
    equal((await api.getSettings()).request_spacing_seconds, 5);
    assert(doc.querySelector('#request-spacing') === input && input.value === '5', 'Autosave replaced or reset input');
    edit(''); assert(doc.querySelector('[data-setting-warning="default_interval_seconds"]').hidden, 'Warning appeared on unchanged setting');
    equal(doc.querySelector('.panel').getBoundingClientRect().width, width); equal(input.getBoundingClientRect().x, controlX);
    await waitUntil(() => doc.querySelector('[data-setting-warning="request_spacing_seconds"]').classList.contains('settings-warning-failed')); equal(writes, 2);
    fail = true; edit('6'); await waitUntil(() => writes === 3 && doc.querySelector('[data-setting-warning="request_spacing_seconds"]').classList.contains('settings-warning-failed'));
    assert(!doc.querySelector('[data-setting-warning="request_spacing_seconds"]').hidden && input.value === '6', 'Failed edit or retry lost');
    fail = false; doc.querySelector('[data-setting-warning="request_spacing_seconds"]').click();
    await waitUntil(() => doc.querySelector('[data-setting-warning="request_spacing_seconds"]').hidden);
    equal((await api.getSettings()).request_spacing_seconds, 6);
    const choice = doc.querySelector('[name="default_interval_choice"][value="600"]'); choice.checked = true; choice.dispatchEvent(new win.Event('change', {bubbles: true}));
    await waitUntil(() => doc.querySelector('[data-setting-warning="default_interval_seconds"]').hidden && writes >= 5);
    equal((await api.getSettings()).default_interval_seconds, 600);
  } finally { frame.remove(); await api.updateSettings({default_interval_seconds: original.default_interval_seconds, request_spacing_seconds: original.request_spacing_seconds}); }
});
document.querySelector('#results').textContent = lines.join('\n');
document.documentElement.dataset.contracts = lines.some(line => line.startsWith('FAIL')) ? 'failed' : 'passed';
