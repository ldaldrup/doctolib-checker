"""A dead worker's intent ends without discarding a newer paused request."""
from datetime import datetime, timedelta, timezone

from app.services.checks import CheckService
from app.storage.repositories import Repository
from test_backend_journey import create_job, setup_backend


def test_expired_manual_run_reconciles_without_erasing_followup(tmp_path, monkeypatch):
    now = [datetime(2026, 10, 2, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr('app.storage.repositories.utc_now', lambda: now[0])
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    repository.set_status(job['id'], 'paused')
    first = repository.request_check(job['id'])
    run_id, abandoned = repository.claim_due_jobs(limit=1)[0]
    followup = repository.request_check(job['id'])
    assert first['id'] != followup['id']
    now[0] += timedelta(minutes=11)
    restarted = Repository(repository.database)
    restarted.interrupt_stale_runs()
    assert restarted.checks(job['id'])[0]['outcome'] == 'interrupted'
    with restarted.database.connection() as conn:
        assert conn.execute('SELECT status FROM check_intents WHERE id=?', (first['id'],)).fetchone()[0] == 'completed'
    assert restarted.get_job(job['id'])['check_intent']['id'] == followup['id']
    assert restarted.get_job(job['id'])['check_intent']['status'] == 'queued'
    assert not restarted.finish_run(run_id, job['id'], 0, 0, 300, owner_token=abandoned['owner_token'])
    next_run, current = restarted.claim_due_jobs(limit=1)[0]
    assert current['intent_id'] == followup['id']
    CheckService(restarted, doctolib, settings).run_claim(next_run, current)
    assert restarted.get_job(job['id'])['check_intent']['run_outcome'] == 'completed'
    assert restarted.get_job(job['id'])['status'] == 'paused'
    assert restarted.claim_due_jobs() == []
