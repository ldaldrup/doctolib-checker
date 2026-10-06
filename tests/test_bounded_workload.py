"""Bounded logical-run journeys with deterministic work and finite admission."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.models import AvailabilityResult
from app.services.checks import CheckService
from app.storage.repositories import Repository
from test_backend_journey import URL, create_job, setup_backend


@pytest.fixture
def workload(tmp_path, monkeypatch):
    wall = datetime(2026, 10, 6, 10, tzinfo=timezone.utc)
    elapsed = [0.0]
    monkeypatch.setattr('app.storage.repositories.utc_now', lambda: wall + timedelta(seconds=elapsed[0]))
    monkeypatch.setattr('app.services.checks.time.monotonic', lambda: elapsed[0])
    client, repository, settings, doctolib = setup_backend(tmp_path)
    large = create_job(client)
    client.patch('/api/v1/jobs/' + large['id'], json={
        'target_urls': [URL + '&source=' + str(i) for i in range(4)]})
    small = create_job(client)
    with repository.database.connection() as conn:
        conn.execute('DELETE FROM check_intents')
        conn.execute("UPDATE jobs SET next_check_at=CASE WHEN id=? THEN '2026-10-06T09:00:00+00:00' ELSE '2026-10-06T09:01:00+00:00' END", (large['id'],))
    calls = []
    def check(url, search, meta=None):
        calls.append((url, elapsed[0]))
        elapsed[0] += 70
        return AvailabilityResult(status='no_availability', slot_count=0,
                                  earliest_slot=None, count_complete=True)
    doctolib.check = check
    return client, repository, settings, doctolib, large, small, elapsed, calls


def test_large_run_yields_and_second_job_progresses_then_restart_skips_results(workload):
    client, repository, settings, doctor, large, small, elapsed, calls = workload
    service = CheckService(repository, doctor, settings)
    hooks = (doctor.before_request, doctor.deadline, doctor.check_permission)
    service.run_due(limit=1)
    assert (doctor.before_request, doctor.deadline, doctor.check_permission) == hooks
    first = repository.checks(large['id'])[0]
    assert first['outcome'] == 'yielded'
    assert len(first['results']) == 1
    service.run_due(limit=1)
    assert calls[1] == (URL, 70)
    assert repository.checks(small['id'])[0]['outcome'] == 'completed'
    restarted = Repository(repository.database)
    CheckService(restarted, doctor, settings).run_due(limit=3)
    complete = restarted.checks(large['id'])[0]
    assert complete['id'] == first['id'] and complete['outcome'] == 'completed'
    assert complete['results'][0]['id'] == first['results'][0]['id']
    assert len(complete['results']) == 4
    assert len({url for url, _ in calls}) == len(calls) == 5
    assert elapsed[0] == 350


def test_budget_discards_partial_positive_and_does_not_retry_target(workload):
    client, repository, settings, doctor, large, small, elapsed, calls = workload
    def slow_check(url, search, meta=None):
        calls.append((url, elapsed[0]))
        elapsed[0] += 121
        return AvailabilityResult(status='available', slot_count=99,
                                  earliest_slot=datetime(2026, 10, 10, tzinfo=timezone.utc), count_complete=True)
    doctor.check = slow_check
    CheckService(repository, doctor, settings).run_due(limit=2)
    assert len(calls) == 2 and calls[1][0] == URL
    result = repository.checks(large['id'])[0]['results'][0]
    assert result['status'] == 'error' and result['error_code'] == 'target_budget_exceeded'
    assert result['slot_count'] == 0 and not result['count_complete']
    assert repository.alerts() == []
    doctor.check = lambda *a, **k: AvailabilityResult(status='no_availability', slot_count=0,
                                                    earliest_slot=None, count_complete=True)
    CheckService(repository, doctor, settings).run_due(limit=1)
    assert repository.checks(large['id'])[0]['outcome'] == 'partial_error'
    assert len(repository.checks(large['id'])[0]['results']) == 4


def test_equal_slice_and_target_budgets_can_start_and_complete(workload):
    client, repository, settings, doctor, large, small, elapsed, calls = workload
    # Even startup bookkeeping must not cause endless empty yields.
    settings = replace(settings, slice_budget_seconds=120)
    CheckService(repository, doctor, settings).run_due(limit=1)
    assert len(calls) == 1
    assert repository.checks(large['id'])[0]['outcome'] == 'yielded'
