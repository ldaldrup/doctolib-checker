import { api, ApiError, createJobPayload, updateJobPayload, settingsPayload } from '/assets/js/api.js';
import { deriveJobView, historyKey, safeBookingUrl } from '/assets/js/job-view.js';

const lines = [];
const assert = (condition, message = 'Assertion failed') => { if (!condition) throw new Error(message); };
const equal = (actual, expected) => assert(JSON.stringify(actual) === JSON.stringify(expected), `${JSON.stringify(actual)} != ${JSON.stringify(expected)}`);
const t = number => `2026-10-01T10:00:${String(number).padStart(2, '0')}Z`;
const url = id => `https://www.doctolib.de/test-${id}/booking/availabilities?placeId=practice-${id}&motiveIds[]=1`;
const target = id => ({id, active: 1, booking_url: url(id), last_validated_at: t(0)});
const job = {id: 'j', targets: [target('a'), target('b')], updated_at: t(0), time_zone: 'Europe/Berlin', date_mode: 'first_available', horizon_days: 15, interval_seconds: 300, insurance_sector: 'public', telehealth: false, telegram_enabled: false, name: 'Contract'};
const result = (id, status, extras = {}) => ({target_id: id, status, checked_at: t(12), count_complete: true, booking_url: url(id), ...(status === 'available' ? {earliest_slot: '2026-10-03T09:00:00Z'} : {}), ...extras});
const run = (outcome, results, extras = {}) => ({id: 'r', job_id: 'j', started_at: t(10), outcome, results, ...extras});
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
await test('operational edit preserves detection with uncertainty, never proves a negative', () => {
  const edited = {...job, updated_at: t(15), status: 'paused', name: 'Renamed', interval_seconds: 600};
  const view = deriveJobView(edited, [run('completed', [result('a', 'available'), result('b', 'no_availability')])]);
  assert(view.detected && view.historical && view.warning.includes('earlier options'));
  assert(deriveJobView(edited, [run('completed', [result('a', 'no_availability'), result('b', 'no_availability')])]).state !== 'no_availability');
});
await test('search invalidation and validation timestamps require a fresh check; equality warns', () => {
  const runs = [run('completed', [result('a', 'available'), result('b', 'no_availability')])];
  equal(deriveJobView(job, runs, {invalidatedAt: t(15)}).state, 'awaiting');
  equal(deriveJobView({...job, targets: [{...target('a'), last_validated_at: t(15)}, target('b')]}, runs).state, 'awaiting');
  const equalTime = deriveJobView(job, runs, {invalidatedAt: t(10)}); assert(equalTime.historical && equalTime.warning && !equalTime.coverageComplete);
  assert(deriveJobView({...job, updated_at: t(10)}, [run('completed', [result('a', 'no_availability'), result('b', 'no_availability')])]).state !== 'no_availability');
});
await test('history signatures include search/target/run evidence but ignore list last_result', () => {
  equal(historyKey(job), historyKey({...job, last_result: result('a', 'available')}));
  for (const changed of [{...job, horizon_days: 30}, {...job, targets: [target('b')]}, {...job, last_started_at: t(20)}, {...job, targets: [{...target('a'), last_validated_at: t(3)}, target('b')]}]) assert(historyKey(job) !== historyKey(changed));
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
document.querySelector('#results').textContent = lines.join('\n');
document.documentElement.dataset.contracts = lines.some(line => line.startsWith('FAIL')) ? 'failed' : 'passed';
