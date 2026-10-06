"""Explicit, previewed retention of terminal display history.

Observed events and target episode state are permanent dedupe boundaries. A
preview stores row fingerprints, never private row content, for bounded CAS
transactions. Necessary live relationships take precedence over age.
"""
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from app.storage.db import SCHEMA_VERSION

TABLES = ('alerts', 'availability_events', 'check_results', 'check_runs', 'check_intents',
          'channel_tests', 'create_operations', 'channel_mutations')
TIME_COLUMNS = {'alerts':'created_at', 'availability_events':'observed_at',
                'check_results':'checked_at', 'check_runs':'finished_at',
                'check_intents':'requested_at', 'channel_tests':'updated_at',
                'create_operations':'created_at', 'channel_mutations':'created_at'}


def _time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        raise ValueError('Retention timestamps require a time zone')
    return parsed.astimezone(timezone.utc)


def _history_time(table, row):
    if table == 'alerts':
        # Cancellation has no timestamp in historic rows. Use only recorded facts.
        stamps = [row[field] for field in ('created_at', 'sent_at', 'last_attempt_at', 'attempt_started_at') if row[field]]
        return max(map(_time, stamps))
    stamp = row[TIME_COLUMNS[table]]
    return _time(stamp) if stamp else None


def _fingerprint(row):
    values = dict(row)
    # Append-only sent evidence does not alter purge eligibility or dependencies.
    values.pop('sent_destinations', None)
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _inventory(conn, cutoff, now, cleanup_keys):
    rows = {table: {row['key' if table in ('create_operations', 'channel_mutations') else 'id']: dict(row)
                    for row in conn.execute('SELECT * FROM ' + table)} for table in TABLES}
    pinned = {table: set() for table in TABLES}
    reasons = {}

    def pin(table, key, reason=None):
        if key in rows[table] and key not in pinned[table]:
            pinned[table].add(key)
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
            return True
        return False

    for table, records in rows.items():
        for key, row in records.items():
            stamp = _history_time(table, row)
            if table not in ('create_operations', 'channel_mutations') and (stamp is None or stamp >= cutoff):
                pin(table, key, 'within_retention_window')
            if table == 'alerts' and (row['status'] in ('pending', 'failed') or row['claim_owner_token']):
                pin(table, key, 'live_delivery')
            if table == 'check_runs' and row['outcome'] in ('running', 'yielded'):
                pin(table, key, 'resumable_run')
            if table == 'check_intents' and row['status'] in ('queued', 'running'):
                pin(table, key, 'live_intent')
            if table == 'channel_tests' and (row['status'] in ('queued', 'running') or row['owner_token']):
                pin(table, key, 'live_channel_test')
            if table in ('create_operations', 'channel_mutations'):
                if not cleanup_keys:
                    pin(table, key, 'operation_keys_cleanup_disabled')
                elif _time(row['expires_at']) > now or _time(row['created_at']) > now - timedelta(days=7):
                    pin(table, key, 'operation_key_guarantee')
                if table == 'create_operations' and row['state'] == 'pending':
                    pin(table, key, 'live_operation')
    for row in conn.execute("SELECT lock_run_id FROM jobs WHERE lock_run_id IS NOT NULL"):
        pin('check_runs', row[0], 'owned_run')
    # Match the repository's latest-published-evidence ordering exactly.
    for row in conn.execute("""SELECT r.id FROM targets t JOIN jobs j ON j.id=t.job_id
        JOIN check_results r ON r.id=(SELECT x.id FROM check_results x WHERE x.target_id=t.id
          AND x.search_revision=j.search_revision AND x.snapshot_known=1 AND x.published=1
          ORDER BY x.rowid DESC LIMIT 1) WHERE t.active=1 AND j.status!='deleted'"""):
        pin('check_results', row[0], 'current_evidence')
    while True:
        changed = False
        for key in tuple(pinned['alerts']):
            row = rows['alerts'][key]
            for table, field in (('availability_events', 'event_id'), ('check_results', 'result_id'),
                                 ('check_results', 'claim_result_id')):
                changed |= pin(table, row[field], 'live_reference_closure')
        for key in tuple(pinned['availability_events']):
            changed |= pin('check_results', rows['availability_events'][key]['result_id'], 'live_reference_closure')
        for key in tuple(pinned['check_results']):
            changed |= pin('check_runs', rows['check_results'][key]['run_id'], 'live_reference_closure')
        for key in tuple(pinned['check_runs']):
            changed |= pin('check_intents', rows['check_runs'][key]['intent_id'], 'live_reference_closure')
        for key in tuple(pinned['check_intents']):
            changed |= pin('check_runs', rows['check_intents'][key]['run_id'], 'live_reference_closure')
        for key, row in rows['check_results'].items():
            if row['run_id'] in pinned['check_runs']:
                changed |= pin('check_results', key, 'live_reference_closure')
        if not changed:
            break
    candidates = {table: {key: _fingerprint(row) for key, row in records.items()
                         if key not in pinned[table] and (table != 'availability_events' or row['result_id'] is not None)}
                  for table, records in rows.items()}
    sizes = {table: sum(len(json.dumps(rows[table][key]).encode()) for key in candidates[table]) for table in TABLES}
    return rows, candidates, sizes, reasons


def preview_retention(database, *, cutoff=None, now=None, cleanup_keys=False):
    now = _time(now or datetime.now(timezone.utc))
    cutoff = _time(cutoff) if cutoff is not None else now - timedelta(days=90)
    if cutoff > now:
        raise ValueError('Retention cutoff cannot be in the future')
    plan_id = str(uuid.uuid4())
    with database.connection() as conn:
        conn.execute('BEGIN')
        _, candidates, sizes, reasons = _inventory(conn, cutoff, now, cleanup_keys)
    with database.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM retention_plans WHERE created_at<?', ((now - timedelta(days=30)).isoformat(),))
        conn.execute('INSERT INTO retention_plans VALUES(?,?,?,?)',
                     (plan_id, cutoff.isoformat(), now.isoformat(), json.dumps({'cleanup_keys': cleanup_keys, 'rows': candidates})))
    return {'plan_id': plan_id, 'cutoff': cutoff.isoformat(), 'schema_version': SCHEMA_VERSION,
            'eligible': {table: len(items) for table, items in candidates.items()},
            'estimated_history_bytes': sum(sizes.values()), 'estimate_reclaims_file_bytes': False,
            'pinned_reasons': reasons, 'terminal_key_cleanup': cleanup_keys,
            'plan_valid_days': 30, 'estimate_basis': 'serialized row bytes, not SQLite file reclamation',
            'cancelled_history_age': 'newest recorded creation/send/attempt time; historic cancellation time is unknown',
            'event_action': 'compact permanent observed/routing/sent-destination boundaries'}


def _remember_sent(conn, event_id):
    event = conn.execute('SELECT * FROM availability_events WHERE id=?', (event_id,)).fetchone()
    if not event:
        return
    sent = {item['dedupe_key']: item for item in json.loads(event['sent_destinations'])}
    for row in conn.execute("SELECT channel_config_id,destination_version,dedupe_key FROM alerts WHERE event_id=? AND (status='sent' OR sent_at IS NOT NULL OR last_attempt_outcome='sent')", (event_id,)):
        sent[row['dedupe_key']] = dict(row)
    conn.execute('UPDATE availability_events SET sent_destinations=? WHERE id=?',
                 (json.dumps(list(sent.values()), sort_keys=True), event_id))


def _still_eligible(conn, table, row, cutoff, now, preview_rows):
    """Check direct dependencies under the writer lock; retained rows close the graph."""
    key = row.get('id', row.get('key'))
    if table not in ('create_operations', 'channel_mutations'):
        stamp = _history_time(table, row)
        if stamp is None or stamp >= cutoff:
            return False
    def exists(sql, parameters):
        return conn.execute(sql, parameters).fetchone() is not None
    def changed(table, record):
        expected = preview_rows[table].get(record['id'])
        return expected is not None and _fingerprint(record) != expected
    if table == 'alerts':
        return row['status'] in ('sent', 'cancelled') and not row['claim_owner_token']
    if table == 'availability_events':
        return not exists('SELECT 1 FROM alerts WHERE event_id=?', (key,))
    if table == 'check_results':
        if exists('SELECT 1 FROM alerts WHERE result_id=? OR claim_result_id=?', (key, key)) or exists(
                'SELECT 1 FROM availability_events WHERE result_id=?', (key,)):
            return False
        run = conn.execute('SELECT * FROM check_runs WHERE id=?', (row['run_id'],)).fetchone()
        if changed('check_runs', run) or run['outcome'] in ('running', 'yielded') or not run['finished_at'] or _time(run['finished_at']) >= cutoff:
            return False
        if exists("SELECT 1 FROM jobs WHERE lock_run_id=?", (run['id'],)) or exists(
                "SELECT 1 FROM check_intents WHERE run_id=? AND (status IN ('queued','running') OR requested_at>=?)",
                (run['id'], cutoff.isoformat())):
            return False
        for intent in conn.execute('SELECT * FROM check_intents WHERE run_id=?', (run['id'],)):
            if changed('check_intents', intent):
                return False
        for sibling in conn.execute('SELECT * FROM check_results WHERE run_id=?', (run['id'],)):
            if changed('check_results', sibling):
                return False
        if exists("""SELECT 1 FROM check_results r WHERE r.run_id=? AND
            (EXISTS(SELECT 1 FROM alerts a WHERE a.result_id=r.id OR a.claim_result_id=r.id)
             OR EXISTS(SELECT 1 FROM availability_events e WHERE e.result_id=r.id))""", (run['id'],)):
            return False
        return not exists("""SELECT 1 FROM targets t JOIN jobs j ON j.id=t.job_id
            JOIN check_results r ON r.id=(SELECT x.id FROM check_results x WHERE x.target_id=t.id
            AND x.search_revision=j.search_revision AND x.snapshot_known=1 AND x.published=1
            ORDER BY x.rowid DESC LIMIT 1) WHERE t.active=1 AND j.status!='deleted' AND r.run_id=?""", (run['id'],))
    if table == 'check_runs':
        run_id = key
        for intent in conn.execute('SELECT * FROM check_intents WHERE run_id=?', (run_id,)):
            if changed('check_intents', intent):
                return False
    if table == 'check_runs':
        return (row['outcome'] not in ('running', 'yielded')
                and not exists('SELECT 1 FROM check_results WHERE run_id=?', (key,))
                and not exists('SELECT 1 FROM jobs WHERE lock_run_id=?', (key,))
                and not exists("SELECT 1 FROM check_intents WHERE run_id=? AND (status IN ('queued','running') OR requested_at>=?)", (key, cutoff.isoformat())))
    if table == 'check_intents':
        return row['status'] not in ('queued', 'running') and not exists('SELECT 1 FROM check_runs WHERE intent_id=?', (key,))
    if table == 'channel_tests':
        return row['status'] not in ('queued', 'running') and not row['owner_token']
    if table == 'create_operations' and row['state'] == 'pending':
        return False
    return _time(row['expires_at']) <= now and _time(row['created_at']) <= now - timedelta(days=7)


def apply_retention(database, *, cutoff, plan_id, batch_size=100, now=None, max_batches=None):
    if not isinstance(batch_size, int) or not 1 <= batch_size <= 1000:
        raise ValueError('Retention batch size must be between 1 and 1000')
    frozen_now = _time(now) if now is not None else None
    now = frozen_now or datetime.now(timezone.utc)
    cutoff = _time(cutoff)
    deleted = {table: 0 for table in TABLES}
    skipped = {table: 0 for table in TABLES}
    batches = 0
    # The immutable preview remains available after interruption and retry.
    with database.connection() as conn:
        plan = conn.execute('SELECT * FROM retention_plans WHERE id=?', (plan_id,)).fetchone()
        if plan is None or plan['cutoff'] != cutoff.isoformat() or _time(plan['created_at']) < now - timedelta(days=30):
            raise ValueError('Retention plan and explicit cutoff must match')
        content = json.loads(plan['candidates'])
    for table in TABLES:
        keys = list(content['rows'][table])
        for offset in range(0, len(keys), batch_size):
            if max_batches is not None and batches >= max_batches:
                return {'deleted': deleted, 'skipped_changed_or_pinned': skipped, 'completed': False, 'batches': batches}
            with database.connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                now = frozen_now or datetime.now(timezone.utc)
                for key in keys[offset:offset + batch_size]:
                    field = 'key' if table in ('create_operations', 'channel_mutations') else 'id'
                    fetched = conn.execute('SELECT * FROM ' + table + ' WHERE ' + field + '=?', (key,)).fetchone()
                    row = dict(fetched) if fetched else None
                    if row is None or (table == 'availability_events' and row['result_id'] is None):
                        continue
                    expected = content['rows'][table][key]
                    current = _fingerprint(row)
                    if current != expected or not _still_eligible(conn, table, row, cutoff, now, content['rows']):
                        skipped[table] += 1
                        continue
                    if table == 'alerts':
                        _remember_sent(conn, row['event_id'])
                    if table == 'availability_events':
                        _remember_sent(conn, key)
                        routing = [{'id': item['id'], 'destination_version': item['destination_version']}
                                   for item in json.loads(row['routing_snapshot'])]
                        conn.execute('UPDATE availability_events SET result_id=NULL,routing_snapshot=? WHERE id=?', (json.dumps(routing), key))
                    else:
                        field = 'key' if table in ('create_operations', 'channel_mutations') else 'id'
                        conn.execute('DELETE FROM ' + table + ' WHERE ' + field + '=?', (key,))
                    deleted[table] += 1
            batches += 1
    return {'deleted': deleted, 'skipped_changed_or_pinned': skipped, 'completed': True, 'batches': batches,
            'availability_events_action': 'compacted, not deleted'}
