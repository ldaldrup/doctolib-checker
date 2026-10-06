"""Durable target coverage, continuation ownership and fair slice selection."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import AvailabilityResult
from app.services.checks import CheckService
from app.storage.db import Database
from app.storage.repositories import LeaseLostError, Repository
from test_backend_journey import URL, create_job, setup_backend


@pytest.fixture
def clock(monkeypatch):
    now = [datetime(2026, 10, 6, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr('app.storage.repositories.utc_now', lambda: now[0])
    return now


def complete_target(repository, run_id, claimed, position):
    return repository.insert_result(run_id, claimed, claimed['search_snapshot']['targets'][position],
        AvailabilityResult(status='no_availability', slot_count=0, earliest_slot=None, count_complete=True),
        owner_token=claimed['owner_token'])


def test_yield_survives_restart_and_other_ready_job_goes_first(tmp_path, clock):
    client, repository, _, _ = setup_backend(tmp_path)
    first = create_job(client)
    client.patch('/api/v1/jobs/'+first['id'], json={'target_urls':[URL, URL+'&source=second']})
    run_id, claimed = repository.claim_due_jobs(limit=1)[0]
    result_id = complete_target(repository, run_id, claimed, 0)
    assert repository.yield_run(run_id, first['id'], owner_token=claimed['owner_token'])
    second = create_job(client)
    database = Database(repository.database.path)
    database.initialize()
    restarted = Repository(database)
    evidence = restarted.get_job(first['id'])['current_run']
    assert (evidence['outcome'],evidence['target_cursor'],evidence['target_total']) == ('yielded',1,2)
    second_run, second_claim = restarted.claim_due_jobs(limit=1)[0]
    assert second_claim['id'] == second['id']
    complete_target(restarted,second_run,second_claim,0)
    restarted.finish_run(second_run,second['id'],1,0,300,owner_token=second_claim['owner_token'])
    resumed_id, resumed = restarted.claim_due_jobs(limit=1)[0]
    assert resumed_id == run_id and resumed['target_cursor']==1 and resumed['generation']==2
    assert resumed['owner_token'] != claimed['owner_token']
    with pytest.raises(LeaseLostError):
        complete_target(restarted,run_id,claimed,1)
    assert not restarted.finish_run(run_id,first['id'],1,0,300,owner_token=claimed['owner_token'])
    complete_target(restarted,run_id,resumed,1)
    assert restarted.finish_run(run_id,first['id'],0,0,300,owner_token=resumed['owner_token'])
    run = restarted.checks(first['id'])[0]
    assert run['outcome']=='completed' and run['successful_targets']==2
    assert len(run['results'])==2 and result_id in {result['id'] for result in run['results']}


def test_result_episode_and_cursor_rollback_together(tmp_path, clock, monkeypatch):
    client, repository, _, _ = setup_backend(tmp_path)
    job = create_job(client)
    run_id, claimed = repository.claim_due_jobs(limit=1)[0]
    original = repository._observe_event
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('crash before commit')
    monkeypatch.setattr(repository,'_observe_event',crash)
    result = AvailabilityResult(status='available',slot_count=1,
        earliest_slot=clock[0]+timedelta(days=5),count_complete=True)
    with pytest.raises(RuntimeError,match='crash before commit'):
        repository.insert_result(run_id,claimed,claimed['search_snapshot']['targets'][0],result,
                                 owner_token=claimed['owner_token'])
    with repository.database.connection() as conn:
        assert conn.execute('SELECT target_cursor FROM check_runs WHERE id=?',(run_id,)).fetchone()[0]==0
        for table in ('check_results','target_alert_state','availability_events','alerts'):
            assert conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
    monkeypatch.setattr(repository,'_observe_event',original)
    first = repository.insert_result(run_id,claimed,claimed['search_snapshot']['targets'][0],result,
                                    owner_token=claimed['owner_token'])
    assert repository.insert_result(run_id,claimed,claimed['search_snapshot']['targets'][0],result,
                                    owner_token=claimed['owner_token']) == first
    assert repository.get_job(job['id'])['current_run']['target_cursor']==1


def test_expired_owner_resumes_same_run_without_debiting_manual_budget(tmp_path, clock):
    client, repository, _, _ = setup_backend(tmp_path)
    job = create_job(client)
    repository.set_status(job['id'],'paused')
    intent = repository.request_check(job['id'])
    run_id, claimed = repository.claim_due_jobs(limit=1)[0]
    original_debit = repository.get_job(job['id'])['last_extra_started_at']
    clock[0] += timedelta(minutes=11)
    repository.interrupt_stale_runs()
    resumed_id, resumed = repository.claim_due_jobs(limit=1)[0]
    assert resumed_id==run_id and resumed['intent_id']==intent['id'] and resumed['generation']==2
    assert repository.get_job(job['id'])['last_extra_started_at']==original_debit
    assert not repository.run_can_check(job['id'],run_id,claimed['owner_token'])
    assert repository.run_can_check(job['id'],run_id,resumed['owner_token'])
    repository.set_status(job['id'],'paused')
    repository.interrupt_stale_runs()
    assert not repository.run_can_check(job['id'],run_id,resumed['owner_token'])
    assert repository.yield_run(run_id,job['id'],owner_token=resumed['owner_token']) is False
    assert repository.checks(job['id'])[0]['outcome']=='interrupted'
    assert repository.claim_due_jobs()==[]


def test_latest_edit_interrupts_obsolete_yield_and_keeps_one_followup(tmp_path, clock):
    client, repository, _, _ = setup_backend(tmp_path)
    job = create_job(client)
    run_id, claimed = repository.claim_due_jobs(limit=1)[0]
    assert repository.yield_run(run_id,job['id'],owner_token=claimed['owner_token'])
    first = client.patch('/api/v1/jobs/'+job['id'],json={'insurance_sector':'private'}).json()
    second = client.patch('/api/v1/jobs/'+job['id'],json={'telehealth':True}).json()
    assert first['check_intent']['id']==second['check_intent']['id']
    fresh_run, fresh = repository.claim_due_jobs(limit=1)[0]
    assert fresh_run!=run_id and fresh['search_revision']==second['search_revision']
    assert fresh['search_snapshot']['search']['insurance_sector']=='private'
    assert fresh['search_snapshot']['search']['telehealth'] is True
    old = next(run for run in repository.checks(job['id']) if run['id']==run_id)
    assert old['outcome']=='interrupted' and old['results']==[]


def test_served_manual_job_cannot_starve_due_ordinary_job(tmp_path, clock):
    client, repository, _, _ = setup_backend(tmp_path)
    manual = create_job(client)
    ordinary = create_job(client)
    run_id, claim = repository.claim_due_jobs(limit=1)[0]
    assert claim['id']==manual['id']
    complete_target(repository,run_id,claim,0)
    repository.finish_run(run_id,manual['id'],1,0,300,owner_token=claim['owner_token'])
    for _ in range(5):
        repository.request_check(manual['id'])
    next_run, next_claim = repository.claim_due_jobs(limit=1)[0]
    assert next_claim['id']==ordinary['id']


def test_schema13_out_of_order_partial_resumes_only_uncompleted_targets(tmp_path, clock):
    client, repository, settings, _ = setup_backend(tmp_path)
    job = create_job(client)
    client.patch('/api/v1/jobs/'+job['id'],json={'target_urls':[URL,URL+'&source=second',URL+'&source=third']})
    run_id, claimed = repository.claim_due_jobs(limit=1)[0]
    targets = claimed['search_snapshot']['targets']
    saved_id = repository.insert_error_result(run_id,claimed,targets[0], 'availability_timeout','timeout',
                                             owner_token=claimed['owner_token'])
    # Schema 13 permitted terminal targets in any order. Rebuild its real
    # outcome constraint and omit the continuation fields before migration.
    with repository.database.connection() as conn:
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('UPDATE check_results SET target_id=? WHERE id=?',(targets[1]['id'],saved_id))
        conn.execute('ALTER TABLE check_runs DROP COLUMN target_cursor')
        conn.execute('ALTER TABLE check_runs DROP COLUMN generation')
        definition = conn.execute("SELECT sql FROM sqlite_master WHERE name='check_runs'").fetchone()[0]
        definition = definition.replace('check_runs','check_runs_old',1).replace("'running', 'yielded',","'running',")
        conn.execute(definition)
        conn.execute('INSERT INTO check_runs_old SELECT * FROM check_runs')
        conn.execute('DROP TABLE check_runs')
        conn.execute('ALTER TABLE check_runs_old RENAME TO check_runs')
        conn.execute('CREATE INDEX idx_runs_job_time ON check_runs(job_id,started_at DESC)')
        conn.execute('ALTER TABLE jobs DROP COLUMN last_served_at')
        conn.execute('UPDATE schema_version SET version=13')
    clock[0] += timedelta(minutes=11)
    repository.database.initialize()
    restarted = Repository(repository.database)
    resumed_id, resumed = restarted.claim_due_jobs(limit=1)[0]
    assert resumed_id==run_id and resumed['target_cursor']==0 and resumed['failed_targets']==1
    assert resumed['owner_token']!=claimed['owner_token']
    calls = []
    class Provider:
        def check(self, url, search, meta=None):
            calls.append(url)
            return AvailabilityResult(status='no_availability',slot_count=0,earliest_slot=None,count_complete=True)
    CheckService(restarted,Provider(),settings).run_claim(resumed_id,resumed)
    assert calls==[URL,URL+'&source=third']
    run = restarted.checks(job['id'])[0]
    assert run['id']==run_id and run['outcome']=='partial_error' and run['target_cursor']==3
    assert len(run['results'])==3 and saved_id in {result['id'] for result in run['results']}
    assert (run['successful_targets'],run['failed_targets'])==(2,1)
    with restarted.database.connection() as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchone() is None
