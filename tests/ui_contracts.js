import { contentControls, CONTENT_PRESETS, readContent, previewText } from '/assets/js/message-content.js';
import { renderChannels } from "/assets/js/pages/channels.js";
import { deliveryNotice } from "/assets/js/delivery-view.js";
import { api, ApiError, createJobPayload, updateJobPayload, settingsPayload } from '/assets/js/api.js';
import { deriveJobView, historyKey, safeBookingUrl } from '/assets/js/job-view.js';

import { settingWarning } from '/assets/js/pages/settings.js';
import { jobStatus, checkedAgo, renderJobList, compactDuration, nextCheck, nextCheckTitle, checkIntentFeedback, savedJobFeedback } from '/assets/js/pages/jobs.js';
import {renderActivity, shouldRefreshActivity} from '/assets/js/pages/activity.js';

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
  catch (error) { lines.push(`FAIL ${name}: ${error.stack || error.message}`); }
}
async function rejects(fn, kind) {
  try { await fn(); throw new Error('Expected rejection'); }
  catch (error) { assert(error instanceof ApiError && error.kind === kind, `Expected ${kind}, got ${error.message}`); return error; }
}
await test('durable check intent shows cooldown, worker health and run outcome without timestamp inference', () => {
  const now = Date.parse(t(0));
  const state = {load: {status: {phase: 'loaded'}, jobs: {phase: 'loaded'}}, status: {worker_alive: true}, jobs: [], filter: 'all', intervalFilter: 'all', views: new Map()};
  const queued = {...job, status: 'active', last_finished_at: t(59), check_intent: {id: 'intent', status: 'queued', search_revision: 1, eligible_at: t(30), triggered_by: 'search_edit'}};
  assert(checkIntentFeedback(queued, state, now).includes('cooldown ends in 30s'));
  assert(checkIntentFeedback(queued, state, now).includes('fresh check requested'));
  state.status.worker_alive = false; assert(checkIntentFeedback(queued, state, now).includes('Worker unavailable'));
  state.jobs = [queued]; assert(/data-action="check-now"[^>]*disabled/.test(renderJobList(state)));
  const quietRefresh = {...queued, check_intent: {...queued.check_intent, triggered_by: 'quiet_hours'}};
  state.jobs = [quietRefresh]; assert(!/data-action="check-now"[^>]*disabled/.test(renderJobList(state)));
  state.checkErrors = new Map([[job.id, 'Uncertain request <script>']]);
  assert(renderJobList(state).includes('Check queued') && renderJobList(state).includes('Uncertain request &lt;script&gt;'));
  state.checkErrors.clear();
  const paused = {...queued, status: 'paused', check_intent: {...queued.check_intent, status: 'running', run_id: 'run'}};
  assert(checkIntentFeedback(paused, state, now).includes('remains paused'));
  assert(checkIntentFeedback(paused, state, now).includes('Worker unavailable'));
  state.jobs = [paused]; assert(renderJobList(state).includes('Check once'));
  assert(renderJobList(state).includes('Stop check'));
  state.load.status.phase = 'stale'; assert(checkIntentFeedback(paused, state, now).includes('Worker status is unknown'));
  state.load.status.phase = 'loaded';
  for (const [outcome, text] of [['completed', 'completed.'], ['partial_error', 'some target errors'], ['error', 'failed'], ['interrupted', 'interrupted']]) {
    assert(checkIntentFeedback({...paused, check_intent: {...paused.check_intent, status: 'completed', outcome}}, state).includes(text));
  }
  assert(checkIntentFeedback({...paused, check_intent: {...paused.check_intent, status: 'completed', run_id: null}}, state).includes('awaiting run evidence'));
  assert(checkIntentFeedback({...paused, search_revision: 2}, state).includes('earlier search revision'));
  assert(checkIntentFeedback({...paused, check_intent: {...paused.check_intent, status: 'cancelled', cancel_reason: 'search_edited'}}, state).includes('select Check once'));
  equal(checkIntentFeedback(job, state), null);
  assert(savedJobFeedback(job, {...job, search_revision: 2, status: 'active'}).includes('fresh check was requested'));
  assert(savedJobFeedback(job, {...job, search_revision: 2, status: 'paused'}).includes('select Check once'));
  equal(savedJobFeedback(job, {...job, name: 'Renamed', edit_version: 2}), 'Job saved.');
});
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
await test('yielded checks preserve progress, useful evidence and incomplete budget errors', () => {
  const yielded = run('yielded', [result('a', 'no_availability')], {target_cursor: 1});
  const view = deriveJobView(job, [yielded]);
  equal(view.state, 'yielded'); assert(view.yielded && !view.running && !view.coverageComplete);
  assert(view.warning.includes('continuation') && !view.warning.includes('Results are missing'));
  const current = {...job, current_run: {id: 'r', outcome: 'yielded', search_revision: 1,
    target_completed: 1, target_total: 2, target_cursor: 1}};
  assert(historyKey(current) !== historyKey({...current, current_run: {...current.current_run, target_cursor: 2}}));
  const state = {jobs: [current], filter: 'all', intervalFilter: 'all',
    load: {status: {phase: 'loaded'}, jobs: {phase: 'loaded'}}, status: {worker_alive: true}, views: new Map([[job.id, view]])};
  equal(jobStatus(current, state).label, 'Continuation queued');
  equal(nextCheck(current, state), 'continuation queued');
  assert(renderJobList(state).includes('1 of 2 targets have results'));
  state.status.worker_alive = false;
  assert(checkIntentFeedback({...current, status: 'paused'}, state).includes('keeps the job paused'));
  assert(checkIntentFeedback(current, state).includes('Worker unavailable'));
  equal(jobStatus(current, state).label, 'Blocked');
  const empty = deriveJobView(job, [run('yielded', [], {id: 'new', started_at: t(20)}),
    run('completed', [result('a', 'available'), result('b', 'no_availability')])]);
  assert(empty.detected && empty.historical && !empty.coverageComplete && empty.slot.run_id === 'r');
  const budget = deriveJobView(job, [run('partial_error', [result('a', 'error', {error_category: 'budget_exceeded', count_complete: false}), result('b', 'no_availability')])]);
  assert(budget.warning.includes('time budget') && !budget.coverageComplete && budget.state !== 'no_availability');
  state.activity = {items: [{...yielded, history_state: 'current', job_name: job.name, target_total: 2,
    target_completed: 1, targets: [{id: 'a'}, {id: 'b'}], results: [result('a', 'error', {error_category: 'budget_exceeded'})]}], phase: 'loaded'};
  const html = renderActivity(state);
  assert(html.includes('Continuation queued') && html.includes('1 of 2 targets have results'));
  assert(html.includes('time budget exceeded') && html.includes('awaiting its result'));
  assert(!html.includes('No appointments found across all'));
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
await test('Activity distinguishes complete negative evidence, partial errors and destination acceptance safely', () => {
  assert(shouldRefreshActivity({items: []}) && shouldRefreshActivity({items: Array(25)}));
  assert(!shouldRefreshActivity({items: Array(25), loadingMore: true}) && shouldRefreshActivity({items: Array(25), loadingMore: true}, true));
  assert(shouldRefreshActivity({items: Array(26)}, false, true));
  assert(!shouldRefreshActivity({items: Array(26)}) && shouldRefreshActivity({items: Array(26)}, true));
  const run = {id: 'run-1', job_id: 'j', job_name: '<Clinic>', time_zone: 'Europe/Berlin', outcome: 'completed', triggered_by: 'schedule',
    requested_at: t(1), started_at: t(2), finished_at: t(3), search_revision: 4, current_search_revision: 4,
    history_state: 'current', snapshot_known: true, target_total: 1, target_completed: 1, coverage_complete: true,
    results: [{id: 'result-1', target_id: 'a', practitioner_name: 'Dr A', practice_name: 'Clinic', status: 'available',
      slot_count: 2, earliest_slot: '2026-10-03T09:00:00Z', count_complete: true, published: true, search_revision: 4,
      booking_url: 'https://private.example/path?token=raw', deliveries: [{channel_name: 'Telegram', channel_type: 'telegram',
        status: 'sent', delivery_state: 'ready', attempts: 1, accepted_at: t(4), originating_run_id: 'run-1',
        confirmation_run_id: 'run-2'}]}]};
  const state = {jobs: [job], activity: {items: [run], jobId: 'j', phase: 'loaded', hasMore: true},
    load: {status: {phase: 'loaded'}, activity: {phase: 'loaded'}}, status: {database_ready: true, worker_alive: true,
      dispatcher_alive: false, last_completed_run: null, overdue_jobs: 0, delivery_backlog: {ready: 0, retry: 0, action_required: 0, uncertain: 1}, oldest_ready_delivery_at: null}};
  let html = renderActivity(state);
  assert(html.includes('Dr A') && html.includes('Accepted by Telegram API') && html.includes('Last completed check: None recorded'));
  assert(html.includes('data-action="activity-retry"') && html.includes('Refresh activity'));
  assert(html.includes('Confirmed by run run-2') && html.includes('&lt;Clinic&gt;'));
  assert(!html.includes('private.example') && !html.includes('token=raw') && html.includes('Load more'));
  assert(!html.includes('No appointments found across all'));
  state.activity.error = {kind: 'auth', message: 'Sign in again'};
  html = renderActivity(state);
  assert(html.includes('data-action="sign-in"'));
  state.activity.error = null;
  const interrupted = {...run, outcome: 'interrupted', snapshot_known: true,
    targets: [{id: 'a', practitioner_name: 'Dr A'}, {id: 'b', practitioner_name: 'Dr B'}], results: []};
  state.activity.items = [interrupted]; html = renderActivity(state);
  assert(html.includes('Dr A') && html.includes('Dr B') && html.includes('No result was recorded for this target'));
  state.activity.items = [run]; state.activity.hasMore = true; state.activity.loadingMore = true; html = renderActivity(state);
  assert(html.includes('id="activity-load-more"') && html.includes('aria-disabled="true"') && !html.includes('id="activity-load-more" class="button button-secondary" type="button" data-action="activity-more" disabled'));
  const negative = {...run, results: [{...run.results[0], status: 'no_availability', earliest_slot: null, deliveries: []}]};
  state.activity.items = [negative]; html = renderActivity(state);
  assert(html.includes('No appointments found across all 1 targets'));
  negative.history_state = 'superseded'; html = renderActivity(state);
  assert(!html.includes('No appointments found across all'));
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
  equal([first.quiet_hours_enabled, first.quiet_hours_start, first.quiet_hours_end], [false, '22:00', '07:00']);
  assert(!Object.keys(updateJobPayload(draft, {...job, targets: job.targets}, settings)).some(key => key.startsWith('quiet_hours_')));
  const quiet = createJobPayload({...draft, quiet_hours_enabled: true, quiet_hours_start: '21:30', quiet_hours_end: '06:30'}, settings);
  equal([quiet.quiet_hours_enabled, quiet.quiet_hours_start, quiet.quiet_hours_end], [true, '21:30', '06:30']);
  await rejects(() => createJobPayload({...draft, quiet_hours_start: '', quiet_hours_end: ''}, settings), 'validation');
  await rejects(() => createJobPayload({...draft, quiet_hours_enabled: true, quiet_hours_start: '07:00', quiet_hours_end: '07:00'}, settings), 'validation');
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
const create = values => api.createJob(values, {idempotencyKey: crypto.randomUUID()});
const remove = async id => api.deleteJob(id, {expectedVersion: (await api.getJob(id)).edit_version});
const setStatus = async (id, action) => api[`${action}Job`](id, {expectedVersion: (await api.getJob(id)).edit_version});
const saveSettings = async values => api.updateSettings(values, {expectedVersion: (await api.getSettings()).edit_version});
await test('native transport headers, relative paths, allowlists and no automatic mutation retry', async () => {
  const calls = [];
  globalThis.fetch = async (path, options) => { calls.push({path, options}); return new Response('{}', {headers: {'Content-Type': 'application/json'}}); };
  await api.getSettings(); await api.createJob({...draft, unsupported: true}, {idempotencyKey: "contract-key"}); await api.updateSettings({default_interval_seconds: 600, unsupported: true}, {expectedVersion: 7});
  for (const {path, options} of calls) { assert(path.startsWith('/api/v1/')); equal(options.credentials, 'same-origin'); equal(options.redirect, 'manual'); equal(options.cache, 'no-store'); equal(options.headers.Accept, 'application/json'); assert(options.signal instanceof AbortSignal); }
  equal(calls[1].options.headers['Idempotency-Key'], 'contract-key'); equal(JSON.parse(calls[2].options.body).expected_version, 7); equal(calls[1].options.headers['Content-Type'], 'application/json'); assert(!JSON.parse(calls[1].options.body).unsupported); assert(!JSON.parse(calls[2].options.body).unsupported);
  await api.updateJob('j', {name:'Updated'}, {expectedVersion:7});
  await api.pauseJob('j', {expectedVersion:7}); await api.resumeJob('j', {expectedVersion:7}); await api.deleteJob('j', {expectedVersion:7});
  for (const call of calls.slice(3)) equal(JSON.parse(call.options.body).expected_version, 7);
  let count = 0; globalThis.fetch = async () => { count++; throw new TypeError('Test connection failed'); };
  const error = await rejects(() => api.createJob(draft, {idempotencyKey: "transport-key"}), 'network'); assert(error.ambiguous); equal(count, 1);
});
await test('redirect/auth, HTTP errors, JSON validation fields and protocol failures', async () => {
  for (const [status, kind] of [[302, 'auth'], [401, 'auth'], [403, 'auth'], [404, 'missing'], [409, 'conflict'], [422, 'validation'], [502, 'upstream']]) {
    globalThis.fetch = async () => new Response(JSON.stringify({detail: [{loc: ['body', 'name'], msg: 'Name required'}]}), {status, headers: {'Content-Type': 'application/json'}});
    const error = await rejects(() => api.getJob('a'), kind); equal(error.status, status); if (status === 422) equal(error.fields.name, 'Name required');
  }
  globalThis.fetch = async () => new Response('<html>Unavailable</html>', {status:502, headers:{'Content-Type':'text/html'}});
  await rejects(() => api.getStatus(), 'upstream');
  globalThis.fetch = async () => ({type: 'opaqueredirect', status: 0}); await rejects(() => api.getStatus(), 'auth');
  globalThis.fetch = async () => new Response('<html>Sign in</html>', {headers: {'Content-Type': 'text/html'}}); await rejects(() => api.getStatus(), 'auth');
  globalThis.fetch = async () => new Response('{broken', {headers: {'Content-Type': 'application/json'}}); await rejects(() => api.getStatus(), 'protocol');
});
await test('Activity pagination sends a stable run cursor', async () => {
  let requested;
  globalThis.fetch = async input => {
    requested = new URL(String(input), 'https://checker.example');
    return new Response(JSON.stringify({items: [], has_more: false}), {headers: {'Content-Type': 'application/json'}});
  };
  await api.getActivity({jobId: 'job-1', limit: 25, beforeRunId: 'run-1'});
  equal(requested.searchParams.get('job_id'), 'job-1');
  equal(requested.searchParams.get('before_run_id'), 'run-1');
  equal(requested.searchParams.get('limit'), '25');
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
    const timeout = await rejects(() => api.createJob(draft, {idempotencyKey: "transport-key"}), 'timeout'); assert(timeout.ambiguous); equal(calls, 1);
    globalThis.setTimeout = nativeTimer;
    const controller = new AbortController(); controller.abort();
    const cancelled = await rejects(() => api.getStatus({signal: controller.signal}), 'cancelled'); assert(!cancelled.ambiguous); equal(calls, 2);
  } finally { globalThis.setTimeout = nativeTimer; }
});
globalThis.fetch = nativeFetch;
await test('15-second refresh retains unchanged cards and unsaved editor controls', async () => {
  const reset = await nativeFetch('/__test/reset-worker', {method:'POST'}); assert(reset.ok, 'Disposable worker reset failed');
  const fixtureUrl = 'https://www.doctolib.de/praxis/berlin/beispiel/booking/availabilities?placeId=practice-123&motiveIds%5B%5D=789&practitionerId=456';
  const saved = await create({...draft, name: 'Refresh regression', target_urls: [fixtureUrl], telegram_enabled: false});
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
  } finally { frame.remove(); await remove(saved.id); }
});
await test('card check requests survive unavailable worker, complete once while paused, and preserve unsaved editor', async () => {
  const fixtureUrl = 'https://www.doctolib.de/praxis/berlin/beispiel/booking/availabilities?placeId=practice-123&motiveIds%5B%5D=789&practitionerId=456';
  const saved = await create({...draft, name: 'Manual check journey', target_urls: [fixtureUrl], telegram_enabled: false});
  await setStatus(saved.id, "pause");
  let activeSaved = null;
  const frame = document.createElement('iframe'); frame.src = '/#jobs'; document.body.append(frame);
  const waitUntil = async predicate => {
    const deadline = Date.now() + 7000;
    while (!predicate()) { if (Date.now() > deadline) throw new Error('Manual check UI journey timed out'); await new Promise(resolve => setTimeout(resolve, 50)); }
  };
  try {
    await waitUntil(() => frame.contentDocument?.querySelector(`[data-job-id="${saved.id}"] [data-action="check-now"]`));
    const doc = frame.contentDocument, win = frame.contentWindow;
    const card = () => doc.querySelector(`[data-job-id="${saved.id}"]`);
    card().querySelector('[data-action="edit"]').click();
    await waitUntil(() => doc.querySelector('#job-name')?.value === saved.name);
    const name = doc.querySelector('#job-name'); name.value = 'Unsaved manual draft'; name.dispatchEvent(new win.Event('input', {bubbles: true})); name.focus();
    const fetch = win.fetch.bind(win); let calls = 0;
    win.fetch = async (url, options) => {
      if (String(url).endsWith('/check-now')) { calls++; await new Promise(resolve => setTimeout(resolve, 150)); }
      return fetch(url, options);
    };
    const button = card().querySelector('[data-action="check-now"]'); assert(button.textContent.includes('Check once'));
    button.click(); button.click();
    await waitUntil(() => card().querySelector('.job-check-feedback')?.textContent.includes('Check queued'));
    equal(calls, 1); assert(card().querySelector('[data-action="check-now"]').disabled);
    assert(card().querySelector('.job-check-feedback').textContent.includes('request remains saved'));
    assert(doc.querySelector('#job-name') === name && name.value === 'Unsaved manual draft', 'Check refresh replaced unsaved editor');
    const queued = await api.getJob(saved.id); equal(queued.status, 'paused'); equal(queued.check_intent.status, 'queued');
    await nativeFetch('/__test/checks', {method: 'POST'});
    doc.querySelector('[data-action="filter"][data-filter="all"]').click();
    await waitUntil(() => card().querySelector('.job-check-feedback')?.textContent.includes('Requested check completed.'));
    const finished = await api.getJob(saved.id); equal(finished.status, 'paused'); equal(finished.check_intent.status, 'completed');
    card().querySelector('[data-action="check-now"]').click();
    await waitUntil(() => card().querySelector('.job-check-feedback')?.textContent.includes('cooldown ends'));
    equal(calls, 2); equal((await api.getJob(saved.id)).status, 'paused');
    await waitUntil(() => !card().querySelector('[data-action="pause"]')?.disabled);
    assert(card().querySelector('[data-action="pause"]').textContent.includes('Stop check'));
    card().querySelector('[data-action="pause"]').click();
    await waitUntil(() => card().querySelector('.job-check-feedback')?.textContent.includes('request was cancelled'));
    equal((await api.getJob(saved.id)).status, 'paused');
    await waitUntil(() => !card().querySelector('[data-action="check-now"]').disabled);
    card().querySelector('[data-action="check-now"]').click();
    await waitUntil(() => card().querySelector('.job-check-feedback')?.textContent.includes('cooldown ends'));
    doc.querySelector('[data-action="cancel-edit"]').click(); card().querySelector('[data-action="edit"]').click();
    await waitUntil(() => doc.querySelector('#job-name')?.value === saved.name);
    editControl(doc, win, '#job-name', 'Unsaved manual draft');
    const horizon = doc.querySelector('#horizon-days'); horizon.value = '16'; horizon.dispatchEvent(new win.Event('input', {bubbles: true}));
    doc.querySelector('#job-form').dispatchEvent(new win.Event('submit', {bubbles: true, cancelable: true}));
    await waitUntil(() => card().querySelector('.job-check-feedback')?.textContent.includes('previous check request was cancelled'));
    assert(doc.querySelector('#toast').textContent.includes('select Check once'));
    await setStatus(saved.id, "resume"); doc.querySelector('[data-action="filter"][data-filter="all"]').click();
    await waitUntil(() => card().querySelector('[data-action="check-now"]')?.textContent.includes('Check now'));
    card().querySelector('[data-action="edit"]').click();
    await waitUntil(() => doc.querySelector('#job-name')?.value === 'Unsaved manual draft');
    const activeHorizon = doc.querySelector('#horizon-days'); activeHorizon.value = '17'; activeHorizon.dispatchEvent(new win.Event('input', {bubbles: true}));
    doc.querySelector('#job-form').dispatchEvent(new win.Event('submit', {bubbles: true, cancelable: true}));
    await waitUntil(() => card().querySelector('.job-check-feedback')?.textContent.includes('fresh check requested'));
    assert(doc.querySelector('#toast').textContent.includes('fresh check was requested'));
    activeSaved = await create({...draft, name: 'Active manual check journey', target_urls: [fixtureUrl], telegram_enabled: false});
    doc.querySelector('[data-action="filter"][data-filter="all"]').click();
    const activeCard = () => doc.querySelector(`[data-job-id="${activeSaved.id}"]`);
    await waitUntil(() => activeCard()?.querySelector('[data-action="check-now"]'));
    assert(activeCard().querySelector('[data-action="check-now"]').textContent.includes('Check now'));
    activeCard().querySelector('[data-action="check-now"]').click();
    await waitUntil(() => activeCard().querySelector('.job-check-feedback')?.textContent.includes('Check queued'));
    const activeIntent = (await api.getJob(activeSaved.id)).check_intent;
    assert(activeIntent.id && activeIntent.status === 'queued' && activeIntent.triggered_by === 'manual');
    await nativeFetch('/__test/checks', {method: 'POST'});
    doc.querySelector('[data-action="filter"][data-filter="all"]').click();
    await waitUntil(() => activeCard().querySelector('.job-check-feedback')?.textContent.includes('Requested check completed.'));
    const activeCompleted = await api.getJob(activeSaved.id);
    equal(activeCompleted.status, 'active'); equal(activeCompleted.check_intent.id, activeIntent.id);
    equal(activeCompleted.check_intent.status, 'completed'); assert(activeCompleted.check_intent.run_id);
  } finally { frame.remove(); await remove(saved.id); if (activeSaved) await remove(activeSaved.id); }
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
    assert(!doc.querySelector('[data-action="discard-settings"]') && !doc.querySelector('#settings-form [type="submit"]'), 'Manual settings buttons remain');
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
  } finally { frame.remove(); await saveSettings({default_interval_seconds: original.default_interval_seconds, request_spacing_seconds: original.request_spacing_seconds}); }
});

const waitUI = async predicate => {
  const deadline = Date.now() + 10000;
  while (!predicate()) { if (Date.now() > deadline) throw new Error('Mutation UI journey timed out'); await new Promise(resolve => setTimeout(resolve, 50)); }
};
const fixtureBooking = 'https://www.doctolib.de/praxis/berlin/beispiel/booking/availabilities?placeId=practice-123&motiveIds%5B%5D=789&practitionerId=456';
function editControl(doc, win, selector, value) {
  const input = doc.querySelector(selector); input.value = value; input.dispatchEvent(new win.Event('input', {bubbles: true})); return input;
}
await test('lost create response replays immutable key without heuristic matching or editing another job', async () => {
  const unrelated = await create({...draft, name: 'Unrelated job', target_urls: [fixtureBooking], telegram_enabled: false});
  const frame = document.createElement('iframe'); frame.src = '/#jobs'; document.body.append(frame);
  let created;
  try {
    await waitUI(() => frame.contentDocument?.querySelector('#job-form [type="submit"]')?.disabled === false);
    const doc = frame.contentDocument, win = frame.contentWindow, fetch = win.fetch.bind(win), calls = [];
    win.fetch = async (path, options) => {
      if (path === '/api/v1/jobs' && options?.method === 'POST') {
        calls.push({key: options.headers['Idempotency-Key'], body: options.body});
        const response = await fetch(path, options);
        if (calls.length === 1) { created = await response.clone().json(); return new win.Response('{}', {status: 503, headers: {'Content-Type': 'application/json'}}); }
        return response;
      }
      assert(options?.method !== 'PATCH', 'Unresolved create became a PATCH'); return fetch(path, options);
    };
    editControl(doc, win, '#job-name', 'Lost response job'); editControl(doc, win, '#target-0', fixtureBooking);
    doc.querySelector('#job-form').dispatchEvent(new win.Event('submit', {bubbles: true, cancelable: true}));
    await waitUI(() => doc.querySelector('[data-action="retry-create"]'));
    assert(doc.querySelector('#job-name').matches(':disabled'), 'Uncertain draft remains editable');
    doc.querySelector(`[data-job-id="${unrelated.id}"] [data-action="edit"]`).click();
    equal(doc.querySelector('#job-name').value, 'Lost response job');
    doc.querySelector('[data-action="retry-create"]').click();
    await waitUI(() => !doc.querySelector('[data-action="retry-create"]'));
    equal(calls.length, 2); equal(calls[0], calls[1]); assert(calls[0].key);
    equal((await api.getJob(unrelated.id)).name, 'Unrelated job');
    equal((await api.listJobs()).filter(item => item.name === 'Lost response job').length, 1);
  } finally { frame.remove(); await remove(unrelated.id); if (created?.id) await remove(created.id); }
});
await test('in-progress create retains key and terminal validation permits corrected operation with new key', async () => {
  const frame = document.createElement('iframe'); frame.src = '/#jobs'; document.body.append(frame);
  let saved;
  try {
    await waitUI(() => frame.contentDocument?.querySelector('#job-form [type="submit"]')?.disabled === false);
    const doc = frame.contentDocument, win = frame.contentWindow, fetch = win.fetch.bind(win), calls = [];
    win.fetch = async (path, options) => {
      if (path === '/api/v1/jobs' && options?.method === 'POST') {
        calls.push(options.headers['Idempotency-Key']);
        if (calls.length === 1) return new win.Response(JSON.stringify({status:'in_progress', retry_after_seconds:2}), {status:202, headers:{'Content-Type':'application/json'}});
        if (calls.length === 2) return new win.Response(JSON.stringify({detail:{code:'invalid_job', retryable:false}}), {status:422, headers:{'Content-Type':'application/json'}});
        const response = await fetch(path, options); saved = await response.clone().json(); return response;
      }
      return fetch(path, options);
    };
    editControl(doc, win, '#job-name', 'Pending validation job'); editControl(doc, win, '#target-0', fixtureBooking);
    doc.querySelector('#job-form').dispatchEvent(new win.Event('submit', {bubbles:true, cancelable:true}));
    await waitUI(() => doc.querySelector('[data-action="retry-create"]'));
    doc.querySelector('[data-action="retry-create"]').click();
    await waitUI(() => !doc.querySelector('[data-action="retry-create"]') && !doc.querySelector('#job-name').matches(':disabled'));
    equal(doc.querySelector('#job-name').value, 'Pending validation job'); equal(calls[0], calls[1]);
    editControl(doc, win, '#job-name', 'Corrected validation job');
    doc.querySelector('#job-form').dispatchEvent(new win.Event('submit', {bubbles:true, cancelable:true}));
    await waitUI(() => saved?.id && doc.querySelector(`#job-list [data-job-id="${saved.id}"]`));
    assert(calls[2] !== calls[1], 'Corrected operation reused terminal key');
  } finally { frame.remove(); if (saved?.id) await remove(saved.id); }
});
await test('external settings conflict preserves newer draft and stops retries until explicit per-field reconciliation', async () => {
  const original = await api.getSettings();
  const frame = document.createElement('iframe'); frame.src = '/#settings'; document.body.append(frame);
  try {
    await waitUI(() => frame.contentDocument?.querySelector('#request-spacing'));
    const doc = frame.contentDocument, win = frame.contentWindow, fetch = win.fetch.bind(win); let writes = 0, failedConflictRead = false;
    win.fetch = async (path, options) => {
      if (path === '/api/v1/settings' && (!options?.method || options.method === 'GET') && writes === 1 && !failedConflictRead) { failedConflictRead = true; return new win.Response('{}', {status:503, headers:{'Content-Type':'application/json'}}); }
      if (options?.method === 'PUT') {
        writes++;
        if (writes === 1) {
          await saveSettings({default_interval_seconds: 900, request_spacing_seconds: 6});
          editControl(doc, win, '#request-spacing', '5');
          await new Promise(resolve => setTimeout(resolve, 100));
        }
      }
      return fetch(path, options);
    };
    const input = editControl(doc, win, '#request-spacing', '4');
    await waitUI(() => doc.querySelector('#settings-remote-notice [data-action="refresh"]'));
    await new Promise(resolve => setTimeout(resolve, 800)); equal(writes, 1);
    doc.querySelector('#settings-remote-notice [data-action="refresh"]').click();
    await waitUI(() => doc.querySelector('[data-action="reconcile-settings"]'));
    equal(input.value, '5'); assert(doc.querySelector('#request-spacing') === input);
    await new Promise(resolve => setTimeout(resolve, 1000)); equal(writes, 1);
    equal((await api.getSettings()).request_spacing_seconds, 6);
    assert(doc.querySelector('#settings-remote-notice').textContent.includes('Server: 6'));
    doc.querySelector('[name="reconcile-default_interval_seconds"][value="server"]').click();
    doc.querySelector('[name="reconcile-request_spacing_seconds"][value="draft"]').click();
    doc.querySelector('[name="reconcile-message_content"][value="server"]').click();
    doc.querySelector('[data-action="reconcile-settings"]').click();
    await waitUI(() => writes === 2 && doc.querySelector('[data-setting-warning="request_spacing_seconds"]')?.hidden);
    const reconciled = await api.getSettings(); equal(reconciled.request_spacing_seconds, 5); equal(reconciled.default_interval_seconds, 900);
  } finally { frame.remove(); await saveSettings({default_interval_seconds:original.default_interval_seconds, request_spacing_seconds:original.request_spacing_seconds}); }
});

await test('job conflict preserves draft and merges unchanged fields from latest server version', async () => {
  const saved = await create({...draft, name:'Job conflict original', target_urls:[fixtureBooking], telegram_enabled:false});
  const frame = document.createElement('iframe'); frame.src = '/#jobs'; document.body.append(frame);
  try {
    await waitUI(() => frame.contentDocument?.querySelector(`[data-job-id="${saved.id}"] [data-action="edit"]`));
    const doc = frame.contentDocument, win = frame.contentWindow;
    doc.querySelector(`[data-job-id="${saved.id}"] [data-action="edit"]`).click();
    editControl(doc, win, '#job-name', 'Job conflict draft');
    await api.updateJob(saved.id, {name:'Job conflict server', interval_seconds:600}, {expectedVersion:saved.edit_version});
    doc.querySelector('#job-form').dispatchEvent(new win.Event('submit', {bubbles:true, cancelable:true}));
    await waitUI(() => doc.querySelector('[data-action="reconcile-job"]'));
    equal(doc.querySelector('#job-name').value, 'Job conflict draft');
    equal((await api.getJob(saved.id)).name, 'Job conflict server');
    assert(doc.querySelector('[name="job-reconcile-name"][value="draft"]'));
    assert(!doc.querySelector('[name="job-reconcile-interval_seconds"]'), 'Unchanged numeric draft incorrectly marked conflicting');
    doc.querySelector('[name="job-reconcile-name"][value="draft"]').click();
    doc.querySelector('[data-action="reconcile-job"]').click();
    equal(doc.querySelector('[name="interval_choice"]:checked').value, '600');
    equal(doc.querySelector('#job-name').value, 'Job conflict draft');
    doc.querySelector('#job-form').dispatchEvent(new win.Event('submit', {bubbles:true, cancelable:true}));
    await waitUI(() => doc.querySelector(`#job-list [data-job-id="${saved.id}"] h2`)?.textContent === 'Job conflict draft');
    equal((await api.getJob(saved.id)).interval_seconds, 600);
  } finally { frame.remove(); await remove(saved.id); }
});
await test('named channels escape labels and preserve independent destination status', () => {
  const state = {settings:{notification_secret_configured:true}, channels:[{id:'c', name:'<img src=x onerror=alert(1)>', enabled:false, usable:false, affected_jobs:[]}], channelUI:{tests:{c:{status:'unknown'}}}};
  const html = renderChannels(state);
  assert(html.includes('&lt;img') && !html.includes('<img'));
  assert(/data-action="test-channel"[^>]*disabled/.test(html));
  assert(html.includes('No automatic resend'));
  const jobsState = {jobs:[{...job,notification_channels:[{id:'a',name:'Telegram1',delivery_status:'sent'},{id:'b',name:'Telegram2',delivery_status:'failed',error_code:'telegram_forbidden'}]}],filter:'all',intervalFilter:'all',views:new Map(),load:{jobs:{phase:'loaded'},status:{phase:'loaded'}},status:{worker_alive:true}};
  const cards = renderJobList(jobsState); assert(cards.includes('Telegram1: sent') && cards.includes('Telegram2: failed'));
});
await test('channel create lost response replays original credentials and key, then explicit edits and version conflict preserve draft', async () => {
  const frame = document.createElement('iframe'); frame.src='/#settings'; document.body.append(frame);
  let created;
  try {
    await waitUI(() => frame.contentDocument?.querySelector('[data-action="new-channel"]')?.disabled === false);
    let doc=frame.contentDocument;const win=frame.contentWindow,fetch=win.fetch.bind(win),calls=[];let blockChannelReads=false;
    win.confirm=()=>true;
    win.fetch=async(path,options)=>{
      if(path==='/api/v1/channels' && (!options?.method || options.method==='GET') && blockChannelReads)return new win.Response('{}',{status:503,headers:{'Content-Type':'application/json'}});
      if(path==='/api/v1/channels' && options?.method==='POST') {
        calls.push({key:options.headers['Idempotency-Key'],body:options.body});
        const response=await fetch(path,options);
        if(calls.length===1){created=await response.clone().json();return new win.Response('{}',{status:503,headers:{'Content-Type':'application/json'}});}
        return response;
      }
      return fetch(path,options);
    };
    doc.querySelector('[data-action="new-channel"]').click();
    editControl(doc,win,'#channel-name','Native Telegram channel');
    editControl(doc,win,'#channel-bot_token','23456:synthetic_native_channel_token_only');
    editControl(doc,win,'#channel-chat_id','-100000002');
    doc.querySelector('#channel-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>doc.querySelector('[data-action="retry-channel-create"]'));
    assert(doc.querySelector('#channel-name').matches(':disabled'));
    doc.querySelector('[data-action="retry-channel-create"]').click();
    await waitUI(()=>!doc.querySelector('#channel-form') && doc.querySelector(`[data-channel-id="${created.id}"]`));
    equal(calls.length,2);equal(calls[0],calls[1]);
    equal((await api.listChannels()).items.filter(item=>item.name==='Native Telegram channel').length,1);
    const publicJson=JSON.stringify((await api.listChannels()).items);
    assert(!publicJson.includes('synthetic_native_channel_token_only') && !publicJson.includes('-100000002'));
    assert(!doc.body.textContent.includes('synthetic_native_channel_token_only'));
    equal(win.localStorage.length,0);equal(win.sessionStorage.length,0);
    doc.querySelector(`[data-channel-id="${created.id}"] [data-action="edit-channel"]`).click();
    equal(doc.querySelector('#channel-token_action').value,'keep');equal(doc.querySelector('#channel-chat_action').value,'keep');
    equal(doc.querySelector('#channel-bot_token').value,'');assert(doc.querySelector('#channel-bot_token').disabled);
    editControl(doc,win,'#channel-name','My preserved channel draft');
    const current=(await api.listChannels()).items.find(item=>item.id===created.id);
    await api.updateChannel(created.id,{name:'Other editor channel',token_action:'keep',chat_action:'keep'},current.edit_version);
    blockChannelReads=true;
    doc.querySelector('#channel-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>doc.querySelector('[data-action="refetch-channel"]'));
    assert(!doc.querySelector('[data-action="reconcile-channel"]'),'Cached channel falsely offered as latest after failed refetch');
    doc.querySelector('[data-action="refetch-channel"]').click();await new Promise(resolve=>setTimeout(resolve,100));
    assert(!doc.querySelector('[data-action="reconcile-channel"]'));blockChannelReads=false;
    doc.querySelector('[data-action="refetch-channel"]').click();
    await waitUI(()=>doc.querySelector('[data-action="reconcile-channel"]'));
    equal(doc.querySelector('#channel-name').value,'My preserved channel draft');
    assert(doc.querySelector('#channel-name').matches(':disabled'));
    doc.querySelector('#channel-conflict-confirm').click();doc.querySelector('[data-action="reconcile-channel"]').click();
    doc.querySelector('#channel-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>!doc.querySelector('#channel-form'));
    equal((await api.listChannels()).items.find(item=>item.id===created.id).name,'My preserved channel draft');
    doc.querySelector(`[data-channel-id="${created.id}"] [data-action="preview-channel"]`).click();
    await waitUI(()=>doc.querySelector('.channel-preview'));
    assert(doc.querySelector('.channel-preview').textContent.includes('Example <Practitioner>'));
    assert(!doc.querySelector('.channel-preview img'));
    const testCalls=[];
    const beforeTestFetch=win.fetch;
    win.fetch=async(path,options)=>{
      const response=await beforeTestFetch(path,options);
      if(path===`/api/v1/channels/${created.id}/tests` && options?.method==='POST'){
        testCalls.push({key:options.headers['Idempotency-Key'],body:options.body});
        if(testCalls.length===1)return new win.Response('{}',{status:503,headers:{'Content-Type':'application/json'}});
      }
      return response;
    };
    doc.querySelector(`[data-channel-id="${created.id}"] [data-action="test-channel"]`).click();
    await waitUI(()=>doc.querySelector(`[data-channel-id="${created.id}"]`).textContent.includes('Test request outcome uncertain'));
    doc.querySelector(`[data-channel-id="${created.id}"] [data-action="channel-test-status"]`).click();
    await waitUI(()=>doc.querySelector(`[data-channel-id="${created.id}"]`).textContent.includes('Test queued'));
    equal(testCalls.length,2);equal(testCalls[0],testCalls[1]);
    await nativeFetch('/__test/deliver',{method:'POST'});
    doc.querySelector(`[data-channel-id="${created.id}"] [data-action="channel-test-status"]`).click();
    await waitUI(()=>doc.querySelector(`[data-channel-id="${created.id}"]`).textContent.includes('Test delivered'));
    doc.querySelector(`[data-channel-id="${created.id}"] [data-action="edit-channel"]`).click();
    editControl(doc,win,'#channel-token_action','clear');
    doc.querySelector('#channel-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>!doc.querySelector('#channel-form'));
    assert(doc.querySelector(`[data-channel-id="${created.id}"] [data-action="test-channel"]`).disabled);
    doc.querySelector(`[data-channel-id="${created.id}"] [data-action="edit-channel"]`).click();
    editControl(doc,win,'#channel-token_action','replace');editControl(doc,win,'#channel-bot_token','23456:synthetic_native_repaired_token_only');
    doc.querySelector('#channel-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>!doc.querySelector('#channel-form'));
    const channel=(await api.listChannels()).items.find(item=>item.id===created.id);assert(channel.usable);
    doc.querySelector(`[data-channel-id="${created.id}"] [data-action="test-channel"]`).click();
    await waitUI(()=>doc.querySelector(`[data-channel-id="${created.id}"]`).textContent.includes('Test queued'));
    await nativeFetch('/__test/deliver?outcome=uncertain',{method:'POST'});
    doc.querySelector(`[data-channel-id="${created.id}"] [data-action="channel-test-status"]`).click();
    await waitUI(()=>doc.querySelector(`[data-channel-id="${created.id}"]`).textContent.includes('Test outcome unknown'));
    const oldDoc=doc;win.location.reload();
    await waitUI(()=>frame.contentDocument!==oldDoc && frame.contentDocument?.querySelector(`[data-channel-id="${created.id}"]`)?.textContent.includes('Test outcome unknown'));
    doc=frame.contentDocument;frame.contentWindow.confirm=()=>true;
    assert(doc.querySelector(`[data-channel-id="${created.id}"]`).textContent.includes('No automatic resend'));
    equal((await api.listChannels()).items.find(item=>item.id===created.id).latest_test.attempt_count,1);
    doc.querySelector(`[data-channel-id="${created.id}"] [data-action="delete-channel"]`).click();
    await waitUI(()=>!doc.querySelector(`[data-channel-id="${created.id}"]`));created=null;
  } finally {frame.remove();if(created?.id){const channel=(await api.listChannels()).items.find(item=>item.id===created.id);if(channel)await api.deleteChannel(channel.id,channel.edit_version);}}
});
await test('Settings creates ntfy and webhook channels, tests saved destinations and preserves explicit endpoint/auth edits',async()=>{
  const frame=document.createElement('iframe');frame.src='/#settings';document.body.append(frame);const created=[];
  try{
    await waitUI(()=>frame.contentDocument?.querySelector('[data-action="new-channel"]')?.disabled===false);
    const doc=frame.contentDocument,win=frame.contentWindow;win.confirm=()=>true;
    for(const [type,name,auth] of [['ntfy','Native ntfy','bearer'],['webhook','Native webhook','basic']]){
      doc.querySelector('[data-action="new-channel"]').click();editControl(doc,win,'#channel-type',type);
      editControl(doc,win,'#channel-name',name);editControl(doc,win,'#channel-endpoint',`https://notify.example/${type}-private-topic`);
      editControl(doc,win,'#channel-auth_type',auth);
      if(auth==='bearer')editControl(doc,win,'#channel-auth_token','synthetic_bearer_secret');
      else {editControl(doc,win,'#channel-auth_username','synthetic_user');editControl(doc,win,'#channel-auth_password','synthetic_password');}
      if(type==='ntfy')editControl(doc,win,'#channel-ntfy_priority','4');
      doc.querySelector('#channel-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
      await waitUI(()=>!doc.querySelector('#channel-form'));
      const saved=(await api.listChannels()).items.find(item=>item.name===name);assert(saved?.usable);created.push(saved);
      const json=JSON.stringify(saved);assert(!json.includes('private-topic')&&!json.includes('synthetic_bearer_secret')&&!json.includes('synthetic_password'));
      doc.querySelector(`[data-channel-id="${saved.id}"] [data-action="edit-channel"]`).click();editControl(doc,win,'#channel-name',`${name} unsaved`);
      const previewFetch=win.fetch;let releasePreview;
      const previewGate=new Promise(resolve=>{releasePreview=resolve;});
      win.fetch=async(path,options)=>{if(String(path).endsWith(`/${saved.id}/preview`))await previewGate;return previewFetch(path,options);};
      doc.querySelector(`[data-channel-id="${saved.id}"] [data-action="preview-channel"]`).click();
      await waitUI(()=>doc.querySelector('#channel-name').matches(':disabled'));equal(doc.querySelector('#channel-name').value,`${name} unsaved`);
      releasePreview();
      await waitUI(()=>doc.querySelector('.channel-preview'));assert(doc.querySelector('.channel-preview').textContent.includes('Example'));
      win.fetch=previewFetch;equal(doc.querySelector('#channel-name').value,`${name} unsaved`);assert(!doc.querySelector('#channel-name').matches(':disabled'));
      assert(!doc.querySelector('.channel-preview').textContent.includes('private-topic'));
      doc.querySelector('[data-action="cancel-channel"]').click();
      doc.querySelector('[data-action="close-channel-preview"]').click();
      doc.querySelector(`[data-channel-id="${saved.id}"] [data-action="test-channel"]`).click();
      await waitUI(()=>doc.querySelector(`[data-channel-id="${saved.id}"]`).textContent.includes('Test queued'));
      await nativeFetch('/__test/deliver',{method:'POST'});doc.querySelector(`[data-channel-id="${saved.id}"] [data-action="channel-test-status"]`).click();
      await waitUI(()=>doc.querySelector(`[data-channel-id="${saved.id}"]`).textContent.includes('Test accepted by endpoint'));
      doc.querySelector(`[data-channel-id="${saved.id}"] [data-action="edit-channel"]`).click();
      assert(!doc.querySelector('#channel-type'));equal(doc.querySelector('#channel-endpoint').value,'');assert(doc.querySelector('#channel-endpoint').disabled);
      equal(doc.querySelector('#channel-auth_action').value,'keep');
      editControl(doc,win,'#channel-name',`${name} renamed`);
      await api.updateChannel(saved.id,{auth_type:'none',auth_action:'clear'},saved.edit_version);
      doc.querySelector('#channel-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));await waitUI(()=>doc.querySelector('[data-action="reconcile-channel"]'));
      assert(doc.querySelector('#channel-form').textContent.includes('Authentication: none.'));
      doc.querySelector('#channel-conflict-confirm').click();doc.querySelector('[data-action="reconcile-channel"]').click();
      doc.querySelector('#channel-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));await waitUI(()=>!doc.querySelector('#channel-form'));
      equal((await api.listChannels()).items.find(item=>item.id===saved.id).auth_type,'none');
      equal((await api.listChannels()).items.find(item=>item.id===saved.id).destination_version,saved.destination_version);
      doc.querySelector(`[data-channel-id="${saved.id}"] [data-action="edit-channel"]`).click();editControl(doc,win,'#channel-endpoint_action','clear');
      doc.querySelector('#channel-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));await waitUI(()=>!doc.querySelector('#channel-form'));
      assert(doc.querySelector(`[data-channel-id="${saved.id}"] [data-action="test-channel"]`).disabled);
      doc.querySelector(`[data-channel-id="${saved.id}"] [data-action="edit-channel"]`).click();editControl(doc,win,'#channel-endpoint_action','replace');
      editControl(doc,win,'#channel-endpoint',`https://notify.example/${type}-replacement-topic`);editControl(doc,win,'#channel-auth_type','bearer');editControl(doc,win,'#channel-auth_action','clear');
      equal(doc.querySelector('#channel-auth_type').value,'none');
      doc.querySelector('#channel-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));await waitUI(()=>!doc.querySelector('#channel-form'));
      const repaired=(await api.listChannels()).items.find(item=>item.id===saved.id);assert(repaired.usable&&!repaired.auth_configured);assert(repaired.destination_version>saved.destination_version);
    }
  }finally{frame.remove();for(const saved of created){const current=(await api.listChannels()).items.find(item=>item.id===saved.id);if(current)await api.deleteChannel(current.id,current.edit_version);}}
});
await test('explicit legacy import and mixed job selection preserve independent endpoint acceptance and saved rename',async()=>{
  const frame=document.createElement('iframe');frame.src='/#settings';document.body.append(frame);let saved;
  try{
    await waitUI(()=>frame.contentDocument?.querySelector('[data-action="import-telegram"]'));
    const doc=frame.contentDocument,win=frame.contentWindow;doc.querySelector('[data-action="import-telegram"]').click();
    await waitUI(()=>!doc.querySelector('[data-action="import-telegram"]'));
    const first=(await api.listChannels()).items.find(item=>item.name==='Telegram1');assert(first);
    const replay=await api.importLegacyChannel(crypto.randomUUID());equal(replay.id,first.id);
    const second=await api.createChannel({name:'Parallel Telegram',enabled:true,token_action:'replace',chat_action:'replace',bot_token:'34567:synthetic_parallel_channel_token_only',chat_id:'-100000003'},crypto.randomUUID());
    const extra=[];
    for(const [type,name] of [['ntfy','ntfy One'],['ntfy','ntfy Two'],['webhook','Generic hook']])extra.push(await api.createChannel({type,name,endpoint_action:'replace',endpoint:`https://notify.example/${name.replaceAll(' ','-')}`,auth_type:'none',auth_action:'clear'},crypto.randomUUID()));
    win.location.hash='#jobs';await waitUI(()=>doc.querySelector(`[name="notification_channel_ids"][value="${second.id}"]`));
    editControl(doc,win,'#job-name','Native routed job');editControl(doc,win,'#target-0',fixtureBooking);
    doc.querySelector('[name="telegram_enabled"]').click();doc.querySelector(`[name="notification_channel_ids"][value="${first.id}"]`).click();doc.querySelector(`[name="notification_channel_ids"][value="${second.id}"]`).click();
    for(const channel of extra)doc.querySelector(`[name="notification_channel_ids"][value="${channel.id}"]`).click();
    doc.querySelector('#job-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>[...doc.querySelectorAll('#job-list h2')].some(node=>node.textContent==='Native routed job'));
    saved=(await api.listJobs()).find(item=>item.name==='Native routed job');equal([...saved.notification_channel_ids].sort(),[first.id,second.id,...extra.map(channel=>channel.id)].sort());
    await nativeFetch('/__test/checks',{method:'POST'});
    for(let index=0;index<5;index++)await nativeFetch('/__test/deliver',{method:'POST'});
    doc.dispatchEvent(new win.Event('visibilitychange'));
    await waitUI(()=>{const text=doc.querySelector(`[data-job-id="${saved.id}"]`)?.textContent;return text?.includes('Telegram1: sent') && text.includes('Parallel Telegram: sent') && extra.every(channel=>text.includes(`${channel.name}: Accepted by endpoint`));});
    await api.updateChannel(second.id,{name:'Renamed parallel',token_action:'keep',chat_action:'keep'},second.edit_version);
    doc.dispatchEvent(new win.Event('visibilitychange'));await waitUI(()=>doc.querySelector(`[data-job-id="${saved.id}"]`).textContent.includes('Renamed parallel'));
    doc.querySelector(`[data-job-id="${saved.id}"] [data-action="edit"]`).click();
    assert(doc.querySelector(`[name="notification_channel_ids"][value="${second.id}"]`).checked);
    await api.deleteChannel(second.id,(await api.listChannels()).items.find(item=>item.id===second.id).edit_version);
    for(const channel of extra)await api.deleteChannel(channel.id,channel.edit_version);
  }finally{frame.remove();if(saved?.id)await remove(saved.id);}
});
await test('Settings saves masked SMTP transport and email recipient, previews tests and keeps conflict drafts',async()=>{
  const frame=document.createElement('iframe');frame.src='/#settings';document.body.append(frame);let email,createdJob;
  try {
    await waitUI(()=>frame.contentDocument?.querySelector('#smtp-form'));
    const doc=frame.contentDocument,win=frame.contentWindow,confirmations=[];let approveSmtpChange=true,smtpWrites=0;
    win.confirm=message=>{confirmations.push({message,writes:smtpWrites});return approveSmtpChange;};
    editControl(doc,win,'#smtp-host','smtp.example');
    editControl(doc,win,'#smtp-sender_email-action','replace');editControl(doc,win,'#smtp-sender_email','sender@example.org');
    editControl(doc,win,'#smtp-username-action','replace');editControl(doc,win,'#smtp-username','synthetic_user');
    editControl(doc,win,'#smtp-password-action','replace');editControl(doc,win,'#smtp-password','synthetic_password');
    doc.querySelector('#smtp-form [name="enabled"]').click();
    doc.querySelector('#smtp-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>doc.querySelector('#smtp-sender_email-action')?.value==='keep' && !doc.querySelector('#smtp-form button[type="submit"]').disabled);
    const saved=await api.getSmtp();assert(saved.usable && saved.sender_email_set && saved.username_set && saved.password_set);
    assert(!JSON.stringify(saved).includes('sender@example.org'));equal(doc.querySelector('#smtp-sender_email').value,'');
    const panels=[doc.querySelector('#settings-form'),doc.querySelector('#smtp-settings'),doc.querySelector('#channel-settings')].map(node=>node.getBoundingClientRect());
    assert(panels[1].top-panels[0].bottom>=20 && panels[2].top-panels[1].bottom>=20,'Settings sections lack spacing');
    assert(doc.querySelector('#smtp-sender_email-action').getBoundingClientRect().width<doc.querySelector('#smtp-form').getBoundingClientRect().width,'Secret select stretches across form');
    editControl(doc,win,'#smtp-sender_name','Draft sender');
    await api.updateSmtp({enabled:true,host:saved.host,port:saved.port,tls_mode:saved.tls_mode,sender_name:'Remote sender',sender_email_action:'keep',username_action:'keep',password_action:'keep'},saved.edit_version);
    doc.querySelector('#smtp-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>doc.querySelector('[data-action="smtp-apply"]'));equal(doc.querySelector('#smtp-sender_name').value,'Draft sender');
    doc.querySelector('#smtp-review-confirm').click();doc.querySelector('[data-action="smtp-apply"]').click();
    doc.querySelector('#smtp-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>!doc.querySelector('[data-action="smtp-apply"]') && !doc.querySelector('#smtp-form button[type="submit"]').disabled);
    editControl(doc,win,'#smtp-username-action','clear');equal(doc.querySelector('#smtp-password-action').value,'clear','clearing SMTP username should clear password with it');
    doc.querySelector('#smtp-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>doc.querySelector('#smtp-username-action')?.value==='keep' && !doc.querySelector('#smtp-form button[type="submit"]').disabled);
    assert(!(await api.getSmtp()).username_set && !(await api.getSmtp()).password_set);
    doc.querySelector('[data-action="new-channel"]').click();editControl(doc,win,'#channel-type','email');
    editControl(doc,win,'#channel-name','Native email');editControl(doc,win,'#channel-recipient','recipient@example.org');
    doc.querySelector('#channel-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>!doc.querySelector('#channel-form'));
    email=(await api.listChannels()).items.find(item=>item.name==='Native email');assert(email?.usable && email.recipient_set);
    assert(!JSON.stringify(email).includes('recipient@example.org'));
    doc.querySelector(`[data-channel-id="${email.id}"] [data-action="preview-channel"]`).click();
    await waitUI(()=>doc.querySelector('.channel-preview')?.textContent.includes('Example <Practitioner>'));
    const originalFetch=win.fetch.bind(win);let loseTestResponse=true;const testKeys=[];
    win.fetch=async(input,init)=>{const url=String(input);if(url.endsWith(`/channels/${email.id}/tests`))testKeys.push(new win.Headers(init.headers).get('Idempotency-Key'));const response=await originalFetch(input,init);if(loseTestResponse&&url.endsWith(`/channels/${email.id}/tests`)){loseTestResponse=false;throw new TypeError('Test-owned lost response');}return response;};
    doc.querySelector(`[data-channel-id="${email.id}"] [data-action="test-channel"]`).click();
    await waitUI(()=>doc.querySelector(`[data-channel-id="${email.id}"] [data-action="channel-test-status"]`)?.textContent.includes('Retry same test request'));
    equal(testKeys.length,1,'lost test response should leave one reserved operation');
    doc.querySelector(`[data-channel-id="${email.id}"] [data-action="channel-test-status"]`).click();
    await waitUI(()=>doc.querySelector(`[data-channel-id="${email.id}"]`)?.textContent.includes('Test queued'));
    equal(testKeys.length,2);equal(testKeys[0],testKeys[1],'retry must reuse the same idempotency key');win.fetch=originalFetch;
    await nativeFetch('/__test/deliver',{method:'POST'});doc.querySelector(`[data-channel-id="${email.id}"] [data-action="channel-test-status"]`).click();
    await waitUI(()=>doc.querySelector(`[data-channel-id="${email.id}"]`)?.textContent.includes('accepted by SMTP server'));
    win.location.hash='#jobs';await waitUI(()=>doc.querySelector(`[name="notification_channel_ids"][value="${email.id}"]`));
    assert(doc.querySelector(`[name="notification_channel_ids"][value="${email.id}"]`).closest('label').textContent.includes('Email'));
    editControl(doc,win,'#job-name','Email route');editControl(doc,win,'#target-0',fixtureBooking);
    doc.querySelector('[name="telegram_enabled"]').click();doc.querySelector(`[name="notification_channel_ids"][value="${email.id}"]`).click();
    doc.querySelector('#job-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>[...doc.querySelectorAll('#job-list h2')].some(node=>node.textContent==='Email route'));
    createdJob=(await api.listJobs()).find(item=>item.name==='Email route');equal(createdJob.notification_channel_ids,[email.id]);
    await nativeFetch('/__test/checks',{method:'POST'});
    const queued=(await (await nativeFetch('/api/v1/alerts?limit=100')).json()).find(alert=>alert.job_name==='Email route' && alert.status==='pending');
    assert(queued,'fixture check should queue an email delivery for the route');
    win.location.hash='#settings';await waitUI(()=>doc.querySelector(`[data-channel-id="${email.id}"] [data-action="edit-channel"]`));
    confirmations.length=0;
    editControl(doc,win,'#smtp-host','smtp-renamed.example');

    win.fetch=async(input,init)=>{if(String(input).endsWith('/api/v1/settings/smtp') && init?.method==='PUT')smtpWrites++;return originalFetch(input,init);};
    approveSmtpChange=false;
    doc.querySelector('#smtp-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>confirmations.length===1);
    assert(confirmations[0].message.includes('1 unsent email delivery') && confirmations[0].message.includes('Email route (1)'),`SMTP identity change should name its pending delivery: ${confirmations[0].message}`);
    equal(confirmations[0].writes,0,'confirmation must precede the settings write');equal(smtpWrites,0,'declining must not write SMTP settings');
    equal((await api.getSmtp()).host,saved.host,'declining must preserve the saved SMTP host');
    confirmations.length=0;
    approveSmtpChange=true;
    let injectImpactRace=true;
    win.fetch=async(input,init)=>{
      if(String(input).endsWith('/api/v1/settings/smtp') && init?.method==='PUT'){
        smtpWrites++;
        if(injectImpactRace){
          injectImpactRace=false;
          const body=JSON.parse(init.body);delete body.expected_impact_token;
          const response=await originalFetch('/api/v1/settings/smtp/impact',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
          const impact=await response.json();
          return new win.Response(JSON.stringify({detail:{code:'smtp_impact_changed',...impact}}),{status:409,headers:{'Content-Type':'application/json'}});
        }
      }
      return originalFetch(input,init);
    };
    doc.querySelector('#smtp-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>smtpWrites===2 && doc.querySelector('#smtp-form button[type="submit"]')?.textContent==='Save SMTP transport');
    equal(confirmations.length,2);equal(confirmations[1].writes,1,'changed impact must be reconfirmed after the rejected write');equal(smtpWrites,2);
    equal((await api.getSmtp()).host,'smtp-renamed.example');
    win.fetch=originalFetch;
    doc.querySelector(`[data-channel-id="${email.id}"] [data-action="edit-channel"]`).click();equal(doc.querySelector('#channel-recipient').value,'');
    editControl(doc,win,'#channel-recipient_action','clear');
    doc.querySelector('#channel-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>!doc.querySelector('#channel-form'));email=(await api.listChannels()).items.find(item=>item.id===email.id);
    assert(!email.usable && !email.recipient_set);
  } finally {frame.remove();if(createdJob)await remove(createdJob.id);if(email)await api.deleteChannel(email.id,email.edit_version);}
});
await test('structured content controls keep presets, custom fields and explicit job inheritance', () => {
  equal(previewText({schema_version:1,event_id:'example'}),JSON.stringify({schema_version:1,event_id:'example'},null,2));
  equal(previewText({text:'<b>Example &lt;Practitioner&gt;</b>',parse_mode:'HTML',warnings:['Shortened.']}),'Example <Practitioner>\n\nRendering notes: Shortened.');
  const form=document.createElement('form');
  form.innerHTML=contentControls({preset:'compact',fields:CONTENT_PRESETS.compact,silent:true},'test-content');
  equal(readContent(form,'test-content'),{preset:'compact',fields:CONTENT_PRESETS.compact,silent:true});
  form.querySelector('[name="test-content-preset"]').value='custom';
  equal(readContent(form,'test-content').fields,CONTENT_PRESETS.compact);
  form.innerHTML=contentControls({preset:'custom',fields:['practice','booking_link'],silent:false},'test-content');
  equal(readContent(form,'test-content'),{preset:'custom',fields:['practice','booking_link'],silent:false});
  const defaults={...settings,message_content:{preset:'compact',fields:CONTENT_PRESETS.compact,silent:false}};
  equal(createJobPayload({...draft,message_content:null},defaults).message_content,null);
  const override={preset:'custom',fields:['earliest_appointment'],silent:false};
  equal(updateJobPayload({...draft,message_content:override},{...job,message_content:null},defaults).message_content,override);
  equal(updateJobPayload({...draft,message_content:null},{...job,message_content:override},defaults).message_content,null);
  equal(settingsPayload({...defaults,default_interval_seconds:300,request_spacing_seconds:3,message_content:override},defaults).message_content,override);
});
await test('content previews render draft without sending and job override persists after reload',async()=>{
  const original=await api.getSettings(),frame=document.createElement('iframe');frame.src='/#settings';document.body.append(frame);let saved;
  try{
    await waitUI(()=>frame.contentDocument?.querySelector('#settings-content-preset'));
    const doc=frame.contentDocument,win=frame.contentWindow;let sends=0;
    const fetch=win.fetch.bind(win);win.fetch=async(input,init)=>{if(String(input).endsWith('/tests'))sends++;return fetch(input,init);};
    editControl(doc,win,'#settings-content-preset','compact');
    await waitUI(()=>doc.querySelector('[data-setting-warning="message_content"]').hidden);
    equal((await api.getSettings()).message_content.preset,'compact');
    for(const type of ['telegram','ntfy','email','webhook']){
      editControl(doc,win,'#message-preview-type',type);doc.querySelector('[data-action="preview-message-content"]').click();
      await waitUI(()=>doc.querySelector('.message-preview-controls .channel-preview'));
      assert(doc.querySelector('.message-preview-controls').textContent.includes('Synthetic example only'));equal(sends,0);
    }
    win.location.hash='#jobs';await waitUI(()=>doc.querySelector('[name="content-inherit"]'));
    assert(doc.querySelector('[name="content-inherit"]').checked);
    doc.querySelector('[name="content-inherit"]').click();
    equal(doc.querySelector('#job-content-preset').value,'compact');
    editControl(doc,win,'#job-content-preset','custom');
    editControl(doc,win,'#job-name','Content override');editControl(doc,win,'#target-0',fixtureBooking);
    doc.querySelector('#job-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>[...doc.querySelectorAll('#job-list h2')].some(node=>node.textContent==='Content override'));
    saved=(await api.listJobs()).find(item=>item.name==='Content override');equal(saved.message_content.preset,'custom');
    doc.querySelector(`[data-job-id="${saved.id}"] [data-action="edit"]`).click();
    assert(!doc.querySelector('[name="content-inherit"]').checked);equal(doc.querySelector('#job-content-preset').value,'custom');
    doc.querySelector('[name="content-inherit"]').click();
    doc.querySelector('#job-form').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));
    await waitUI(()=>!doc.querySelector('[data-action="cancel-edit"]'));
    equal((await api.getJob(saved.id)).message_content,null);
  }finally{frame.remove();if(saved)await remove(saved.id);const latest=await api.getSettings();await api.updateSettings({default_interval_seconds:original.default_interval_seconds,request_spacing_seconds:original.request_spacing_seconds,message_content:original.message_content},{expectedVersion:latest.edit_version});}
});
await test('saved target repair updates identity, stays paused, reconciles conflict and lost response without retry', async () => {
  const fixtureUrl = 'https://www.doctolib.de/praxis/berlin/beispiel/booking/availabilities?placeId=practice-123&motiveIds%5B%5D=789&practitionerId=456';
  const saved = await create({...draft, name: 'Metadata repair journey', target_urls: [fixtureUrl], telegram_enabled: false});
  await setStatus(saved.id, 'pause');
  const frame = document.createElement('iframe'); frame.src = '/#jobs'; document.body.append(frame);
  const wait = async predicate => {
    const end = Date.now() + 8000;
    while (!predicate()) { if (Date.now() > end) throw new Error('Repair UI journey timed out'); await new Promise(resolve => setTimeout(resolve, 50)); }
  };
  const provider = async query => assert((await nativeFetch(`/__test/metadata?${query}`, {method: 'POST'})).ok);
  try {
    await wait(() => frame.contentDocument?.querySelector(`[data-job-id="${saved.id}"] .target-details`));
    const doc = frame.contentDocument, win = frame.contentWindow;
    const card = () => doc.querySelector(`[data-job-id="${saved.id}"]`);
    card().querySelector('summary').click();
    card().querySelector('[data-action="edit"]').click();
    await wait(() => doc.querySelector('#job-name')?.value === saved.name);
    const name = doc.querySelector('#job-name'); name.value = 'Unsaved repair draft'; name.dispatchEvent(new win.Event('input', {bubbles: true}));
    const fetch = win.fetch.bind(win); let calls = 0, mode = 'normal';
    win.fetch = async (url, options) => {
      if (String(url).endsWith('/revalidate')) {
        calls++;
        if (mode === 'conflict') { await api.updateJob(saved.id, {name: 'Concurrent saved edit'}, {expectedVersion: (await api.getJob(saved.id)).edit_version}); }
        const response = await fetch(url, options);
        if (mode === 'lost') throw new TypeError('Synthetic lost response');
        return response;
      }
      return fetch(url, options);
    };
    await provider('agenda_id=5678');
    card().querySelector('[data-action="repair-target"]').click();
    await wait(() => card().textContent.includes('Metadata repaired.') && !card().querySelector('[data-action="repair-target"]').disabled);
    const repaired = await api.getJob(saved.id);
    equal(repaired.targets[0].id, saved.targets[0].id); equal(repaired.search_revision, saved.search_revision + 1);
    equal(repaired.status, 'paused'); equal(repaired.targets[0].agenda_ids, '5678'); equal(calls, 1);
    assert(card().textContent.includes('Job remains paused') && card().textContent.includes('Before:') && card().textContent.includes('After:'));
    assert(card().querySelector('details').open, 'Repair closed target details');
    equal(doc.querySelector('#job-name'), name); equal(name.value, 'Unsaved repair draft');
    mode = 'conflict'; card().querySelector('[data-action="repair-target"]').click();
    await wait(() => card().textContent.includes('Repair conflicted with a saved edit') && !card().querySelector('[data-action="repair-target"]').disabled);
    equal(calls, 2); equal((await api.getJob(saved.id)).search_revision, repaired.search_revision);
    mode = 'lost'; card().querySelector('[data-action="repair-target"]').click();
    await wait(() => card().textContent.includes('Request outcome unknown') && !card().querySelector('[data-action="repair-target"]').disabled);
    equal(calls, 3); equal((await api.getJob(saved.id)).search_revision, repaired.search_revision);
    assert(card().textContent.includes('Showing current saved metadata'));
    const beforeFailure = await api.getJob(saved.id);
    mode = 'normal'; await provider('failure=true'); card().querySelector('[data-action="repair-target"]').click();
    await wait(() => card().textContent.includes('Validation failed.') && !card().querySelector('[data-action="repair-target"]').disabled);
    const failed = await api.getJob(saved.id);
    equal(failed.targets[0].agenda_ids, '5678'); equal(failed.targets[0].metadata_validation_state, 'unavailable');
    equal(failed.targets[0].last_validated_at, beforeFailure.targets[0].last_validated_at);
    assert(card().textContent.includes('separate from an availability check'));
    equal(calls, 4);
  } finally { frame.remove(); await provider('agenda_id=1234'); await remove(saved.id); }
});
document.querySelector('#results').textContent = lines.join('\n');
document.documentElement.dataset.contracts = lines.some(line => line.startsWith('FAIL')) ? 'failed' : 'passed';
