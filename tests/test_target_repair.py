"""Saved-target repair preserves identity and fences obsolete work."""
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Event, Thread
from pathlib import Path
import time

import pytest
import requests

from app.models import AvailabilityResult
from app.services.checks import CheckService
from app.storage.db import Database, SCHEMA_VERSION
from app.storage.repositories import precise_iso, utc_now
from tests.test_safe_mutations import backend, create


def repair(client, job, **values):
    target = job['targets'][0]
    return client.post(f"/api/v1/jobs/{job['id']}/targets/{target['id']}/revalidate", json={
        'expected_version': job['edit_version'], 'booking_url': target['booking_url'], **values})


def changed_resolver(doctolib):
    original = doctolib.resolve
    doctolib.resolve = lambda url: replace(original(url), agenda_ids_str='999', motive_name='Updated motive')


def test_changed_repair_preserves_identity_history_and_cancels_old_eligibility(tmp_path):
    client, repository, settings, doctolib = backend(tmp_path)
    job = create(client).json()
    run_id, claim = repository.claim_due_jobs(limit=1)[0]
    target = claim['search_snapshot']['targets'][0]
    result = AvailabilityResult(status='available', slot_count=1, earliest_slot=datetime.fromisoformat('2026-10-12T10:00:00+02:00'), count_complete=True)
    repository.insert_result(run_id, claim, target, result, owner_token=claim['owner_token'])
    with repository.database.connection() as conn:
        before_results = [dict(row) for row in conn.execute('SELECT * FROM check_results')]
        # Independent delivery eligibility must be cancelled by the shared revision path.
        conn.execute("""INSERT INTO alerts(id,job_id,target_id,result_id,channel,event_type,dedupe_key,status,
            created_at,search_revision) VALUES('old',?,?,?,'telegram','slot_available','old','pending',?,1)""",
            (job['id'], target['id'], before_results[0]['id'], precise_iso()))
    changed_resolver(doctolib)
    response = repair(client, job)
    assert response.status_code == 200, response.text
    outcome = response.json()
    saved = outcome['job']
    assert outcome['changed'] and outcome['fresh_check_queued']
    assert saved['search_revision'] == 2 and saved['edit_version'] == 2
    assert saved['targets'][0]['id'] == target['id']
    assert saved['targets'][0]['agenda_ids'] == '999'
    assert saved['check_intent']['requested_revision'] == 2
    assert claim['search_snapshot']['targets'][0]['agenda_ids'] != '999'
    assert not repository.run_can_check(job['id'], run_id, claim['owner_token'])
    with repository.database.connection() as conn:
        assert [dict(row) for row in conn.execute('SELECT * FROM check_results')] == before_results
        assert conn.execute("SELECT status,error_summary FROM alerts WHERE id='old'").fetchone()[:] == ('cancelled', 'search_edited')
    # The old immutable claim remains historical; it cannot publish another result.
    repository.finish_run(run_id, job['id'], 1, 0, 300, owner_token=claim['owner_token'])
    assert repository.checks(job['id'])[0]['search_revision'] == 1
    assert CheckService(repository, doctolib, settings).run_due() == [{'successful_targets': 1, 'failed_targets': 0}]
    assert repository.checks(job['id'])[0]['results'][0]['target_id'] == target['id']


def test_unchanged_repair_refreshes_age_without_revision_intent_or_episode(tmp_path):
    client, repository, _, _ = backend(tmp_path)
    job = create(client).json()
    before = job['targets'][0]['last_validated_at']
    outcome = repair(client, job).json()
    assert outcome['validation_state'] == 'validated' and not outcome['changed']
    saved = outcome['job']
    assert saved['edit_version'] == job['edit_version'] and saved['search_revision'] == job['search_revision']
    assert saved['check_intent'] is None
    target = saved['targets'][0]
    assert target['last_validated_at'] != before
    assert target['metadata_checked_at'] == target['last_validated_at']
    with repository.database.connection() as conn:
        assert conn.execute('SELECT count(*) FROM target_alert_state').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM check_runs').fetchone()[0] == 0


@pytest.mark.parametrize('failure,expected', [
    (requests.Timeout('private provider detail'), ('unavailable', 'upstream_unavailable')),
    (None, ('invalid', 'invalid_metadata')),
])
def test_failed_or_incomplete_validation_preserves_saved_evidence(tmp_path, failure, expected):
    client, _, _, doctolib = backend(tmp_path)
    job = create(client).json()
    original = doctolib.resolve
    def fail(url):
        if failure:
            raise failure
        return replace(original(url), agenda_ids_str='')
    doctolib.resolve = fail
    response = repair(client, job)
    assert response.status_code == 200, response.text
    outcome = response.json()
    assert (outcome['validation_state'], outcome['validation_reason']) == expected
    assert 'private provider detail' not in response.text
    assert not outcome['changed']
    saved = outcome['job']['targets'][0]
    assert saved['validation_state'] == 'ready'
    assert saved['last_validated_at'] == job['targets'][0]['last_validated_at']
    assert saved['agenda_ids'] == job['targets'][0]['agenda_ids']


@pytest.mark.parametrize('mutation', ['edit', 'remove', 'delete'])
def test_concurrent_mutation_conflicts_without_holding_write_transaction(tmp_path, mutation):
    client, repository, _, doctolib = backend(tmp_path)
    job = create(client).json()
    started, release = Event(), Event()
    original = doctolib.resolve
    def blocked(url):
        started.set()
        assert release.wait(5)
        return replace(original(url), agenda_ids_str='999')
    doctolib.resolve = blocked
    responses = []
    thread = Thread(target=lambda: responses.append(repair(client, job)))
    thread.start()
    try:
        assert started.wait(5)
        if mutation == 'edit':
            repository.update_job(job['id'], {'name': 'Concurrent'}, expected_version=1)
        elif mutation == 'delete':
            repository.set_status(job['id'], 'deleted', expected_version=1)
        else:
            repository.update_job(job['id'], {}, targets=[], expected_version=1)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert responses[0].status_code in (404, 409)
    with repository.database.connection() as conn:
        target = conn.execute('SELECT * FROM targets WHERE id=?', (job['targets'][0]['id'],)).fetchone()
        assert target['agenda_ids'] == job['targets'][0]['agenda_ids']
        assert target['metadata_checked_at'] == job['targets'][0]['metadata_checked_at']


def test_paused_repair_preserves_pause_and_manual_uses_new_metadata(tmp_path):
    client, repository, settings, doctolib = backend(tmp_path)
    job = create(client).json()
    job = repository.set_status(job['id'], 'paused', expected_version=1)
    changed_resolver(doctolib)
    outcome = repair(client, job).json()
    job = outcome['job']
    assert outcome['changed'] and not outcome['fresh_check_queued']
    assert job['status'] == 'paused' and job['check_intent'] is None
    assert CheckService(repository, doctolib, settings).run_due() == []
    repository.request_check(job['id'])
    assert CheckService(repository, doctolib, settings).run_due() == [{'successful_targets': 1, 'failed_targets': 0}]
    run = repository.checks(job['id'])[0]
    assert run['paused_manual'] and run['search_revision'] == job['search_revision']
    assert repository.get_job(job['id'])['status'] == 'paused'
    assert any(kwargs.get('params', {}).get('agenda_ids') == '999' for _, kwargs in doctolib.fixture_session.calls)


def test_repair_gate_rejects_unaffordable_wait_without_advancing_gate(tmp_path):
    client, repository, settings, doctolib = backend(tmp_path)
    job = create(client).json()
    client.app.state.settings = replace(settings, target_budget_seconds=0.01)
    gate = precise_iso(utc_now() + timedelta(seconds=30))
    with repository.database.connection() as conn:
        conn.execute('INSERT INTO request_gate(singleton_id,next_allowed_at) VALUES(1,?)', (gate,))
    calls = len(doctolib.fixture_session.calls)
    started = time.monotonic()
    outcome = repair(client, job).json()
    assert time.monotonic() - started < 1
    assert outcome['validation_state'] == 'unavailable' and outcome['validation_reason'] == 'budget_exceeded'
    assert len(doctolib.fixture_session.calls) == calls
    with repository.database.connection() as conn:
        assert conn.execute('SELECT next_allowed_at FROM request_gate').fetchone()[0] == gate


def test_schema14_metadata_migration_preserves_validation_age_and_identity(tmp_path):
    client, repository, _, _ = backend(tmp_path)
    job = create(client).json()
    before = job['targets'][0]
    with repository.database.connection() as conn:
        for column in ('metadata_validation_state', 'metadata_validation_reason', 'metadata_checked_at'):
            conn.execute('ALTER TABLE targets DROP COLUMN ' + column)
        conn.execute('UPDATE schema_version SET version=14')
    from app import admin
    assert admin._inspect(Path(repository.database.path)) == 14
    Database(repository.database.path).initialize()
    assert admin._inspect(Path(repository.database.path)) == SCHEMA_VERSION
    target = repository.get_job(job['id'])['targets'][0]
    assert target['id'] == before['id'] and target['last_validated_at'] == before['last_validated_at']
    assert target['metadata_validation_state'] == 'validated'
    assert target['metadata_checked_at'] == before['last_validated_at']
    with repository.database.connection() as conn:
        assert conn.execute('SELECT version FROM schema_version').fetchone()[0] == SCHEMA_VERSION


def test_late_old_result_is_historical_and_cannot_update_episodes(tmp_path):
    client, repository, _, doctolib = backend(tmp_path)
    job = create(client).json()
    run_id, claim = repository.claim_due_jobs(limit=1)[0]
    changed_resolver(doctolib)
    assert repair(client, job).json()['changed']
    result = AvailabilityResult(status='available', slot_count=1,
        earliest_slot=datetime.fromisoformat('2026-10-12T10:00:00+02:00'), count_complete=True)
    repository.insert_result(run_id, claim, claim['search_snapshot']['targets'][0], result,
        owner_token=claim['owner_token'])
    with repository.database.connection() as conn:
        assert conn.execute('SELECT published FROM check_results WHERE run_id=?', (run_id,)).fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM target_alert_state').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM availability_events').fetchone()[0] == 0


def test_agenda_order_and_duplicates_are_not_an_effective_change(tmp_path):
    client, repository, _, doctolib = backend(tmp_path)
    job = create(client).json()
    with repository.database.connection() as conn:
        conn.execute("UPDATE targets SET agenda_ids='11-22' WHERE id=?", (job['targets'][0]['id'],))
    original = doctolib.resolve
    doctolib.resolve = lambda url: replace(original(url), agenda_ids_str='22-11-22')
    outcome = repair(client, job).json()
    assert not outcome['changed']
    assert outcome['job']['search_revision'] == job['search_revision']


@pytest.mark.parametrize('malformed', ['agenda_item', 'agenda_id', 'agenda_motives', 'motive_item', 'person_name', 'profile_name'])
def test_malformed_upstream_items_record_safe_invalid_metadata(tmp_path, malformed):
    from tests.test_backend_journey import FixtureResponse
    client, _, _, doctolib = backend(tmp_path)
    job = create(client).json()
    import json
    from tests.test_backend_journey import FIXTURES
    payload = json.loads((FIXTURES / 'info_de.json').read_text())
    data = payload['data']
    if malformed == 'agenda_item':
        data['agendas'] = [None]
    elif malformed == 'agenda_id':
        data['agendas'][0]['id'] = {'bad': 'shape'}
    elif malformed == 'agenda_motives':
        data['agendas'][0]['visit_motive_ids'] = '789'
    elif malformed == 'motive_item':
        data['visit_motives'] = [None]
    elif malformed == 'person_name':
        data['practitioners'] = [{'id': 456, 'first_name': 123}]
    else:
        data['profile']['name'] = ['invalid']
    doctolib.metadata_session.get = lambda *args, **kwargs: FixtureResponse(payload)
    outcome = repair(client, job).json()
    assert outcome['validation_state'] == 'invalid' and outcome['validation_reason'] == 'invalid_metadata'
    assert outcome['job']['targets'][0]['agenda_ids'] == job['targets'][0]['agenda_ids']


def test_metadata_repair_uses_total_timeout_adapter_without_mutating_shared_client(tmp_path, monkeypatch):
    client, _, _, doctolib = backend(tmp_path)
    job = create(client).json()
    original_session = requests.Session()
    doctolib.metadata_session = original_session
    transports = []
    monkeypatch.setattr('app.services.jobs.get_availability_session', lambda profile: (
        transports.append(profile) or doctolib.fixture_session))
    assert repair(client, job).json()['validation_state'] == 'validated'
    assert transports == ['safari2601']
    assert doctolib.metadata_session is original_session and doctolib.deadline is None
    assert doctolib.fixture_session.calls[-1][1]['timeout'] <= 10


def test_reinitialization_preserves_explicit_unknown_validation_state(tmp_path):
    client, repository, _, _ = backend(tmp_path)
    job = create(client).json()
    with repository.database.connection() as conn:
        conn.execute("UPDATE targets SET metadata_checked_at=NULL,metadata_validation_state='unknown' WHERE id=?", (job['targets'][0]['id'],))
    repository.database.initialize()
    target = repository.get_job(job['id'])['targets'][0]
    assert target['metadata_checked_at'] is None and target['metadata_validation_state'] == 'unknown'


def test_unsafe_provider_redirect_is_unavailable_and_bad_input_stays_invalid(tmp_path):
    from tests.test_backend_journey import FixtureResponse
    client, _, _, doctolib = backend(tmp_path)
    job = create(client).json()
    response = FixtureResponse({}, status_code=302)
    response.headers = {'Location': 'https://example.org/private-provider-data'}
    doctolib.metadata_session.get = lambda *args, **kwargs: response
    outcome = repair(client, job).json()
    assert outcome['validation_state'] == 'unavailable'
    assert outcome['validation_reason'] == 'upstream_unavailable'
    assert outcome['job']['targets'][0]['last_validated_at'] == job['targets'][0]['last_validated_at']
    assert 'private-provider-data' not in str(outcome)
    assert repair(client, job, booking_url='https://example.org/private-input').status_code == 422


@pytest.mark.parametrize('column', ['metadata_validation_state', 'metadata_validation_reason', 'metadata_checked_at'])
def test_admin_rejects_incomplete_current_metadata_schema(tmp_path, column):
    from app import admin
    _, repository, _, _ = backend(tmp_path)
    assert admin._inspect(Path(repository.database.path)) == SCHEMA_VERSION
    with repository.database.connection() as conn:
        conn.execute('ALTER TABLE targets DROP COLUMN ' + column)
    with pytest.raises(admin.AdminError, match='incomplete_checker_schema'):
        admin._inspect(Path(repository.database.path))
