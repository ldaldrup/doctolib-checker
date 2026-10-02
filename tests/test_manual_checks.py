"""Direct part 04 API, worker and independent-delivery acceptance journeys."""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.checks import CheckService
from app.services.delivery import DeliveryService
from app.storage.repositories import Repository, parse_time
from test_backend_journey import FakeNotifier, FixtureDoctolib, URL, create_job, setup_backend


@pytest.fixture
def clock(monkeypatch):
    current = [datetime(2026, 10, 2, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr('app.storage.repositories.utc_now', lambda: current[0])
    return current


def request(client, job):
    response = client.post('/api/v1/jobs/' + job['id'] + '/check-now')
    assert response.status_code == 200, response.text
    return response.json()


def run(repository, doctolib, settings):
    return CheckService(repository, doctolib, settings).run_due()


def test_extra_budget_is_persistent_shared_with_edits_and_not_regular_runs(tmp_path, clock):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    assert len(run(repository, doctolib, settings)) == 1  # Creation is ordinary work.
    first = request(client, job)
    assert first['status'] == 'queued'
    assert len(run(repository, doctolib, settings)) == 1  # Override the 300-second floor.
    assert repository.checks(job['id'])[0]['started_at'] == repository.checks(job['id'])[1]['started_at']
    next_request = request(client, job)
    assert next_request['id'] != first['id']
    assert parse_time(next_request['eligible_at']) == clock[0] + timedelta(seconds=60)
    for horizon in (16, 17, 18):
        # The effective date search here is custom; insurance changes instead.
        updated = client.patch('/api/v1/jobs/' + job['id'], json={'insurance_sector': 'private' if horizon % 2 == 0 else 'public'}).json()
        assert updated['check_intent']['id'] == next_request['id']
    assert updated['check_intent']['requested_revision'] == updated['search_revision']
    restarted = Repository(repository.database)
    assert run(restarted, doctolib, settings) == []
    clock[0] += timedelta(seconds=59)
    assert run(restarted, doctolib, settings) == []
    clock[0] += timedelta(seconds=1)
    assert len(run(restarted, doctolib, settings)) == 1
    assert run(restarted, doctolib, settings) == []


def test_edit_while_running_retains_latest_followup_and_all_new_targets(tmp_path, clock):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    run_id, old = repository.claim_due_jobs(limit=1)[0]
    first = request(client, job)
    assert request(client, job)['id'] == first['id']
    changed = client.patch('/api/v1/jobs/' + job['id'], json={
        'insurance_sector': 'private', 'target_urls': [URL, URL + '&source=second'],
    }).json()
    assert changed['check_intent']['id'] == first['id']
    assert changed['check_intent']['requested_revision'] == 2
    CheckService(repository, doctolib, settings).run_claim(run_id, old)
    historical = repository.checks(job['id'])[0]
    assert historical['search_revision'] == 1
    assert all(not row['published'] for row in historical['results'])
    assert repository.get_job(job['id'])['check_intent']['id'] == first['id']
    assert run(repository, doctolib, settings) == [{'successful_targets': 2, 'failed_targets': 0}]
    fresh = repository.checks(job['id'])[0]
    assert fresh['search_revision'] == 2 and len(fresh['results']) == 2
    assert repository.get_job(job['id'])['check_intent']['run_id'] == fresh['id']


def test_cosmetic_and_noop_edits_never_create_extra_intent(tmp_path, clock):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    run(repository, doctolib, settings)
    for values in ({'name': 'Renamed'}, {'telegram_enabled': False}, {'interval_seconds': 600}, {'insurance_sector': 'public'}):
        response = client.patch('/api/v1/jobs/' + job['id'], json=values)
        assert response.status_code == 200
        assert response.json().get('check_intent') is None
    assert run(repository, doctolib, settings) == []


def test_paused_manual_check_notifies_after_completion_and_stays_paused(tmp_path, clock):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    repository.set_status(job['id'], 'paused')
    intent = request(client, job)
    assert intent['status'] == 'queued'
    assert run(repository, doctolib, settings) == [{'successful_targets': 1, 'failed_targets': 0}]
    assert repository.get_job(job['id'])['status'] == 'paused'
    assert repository.checks(job['id'])[0]['outcome'] == 'completed'
    notifier = FakeNotifier()
    assert DeliveryService(repository, settings, sender=notifier).run_once()
    assert len(notifier.sent) == 1 and repository.alerts()[0]['status'] == 'sent'
    clock[0] += timedelta(seconds=60)
    request(client, job)
    assert len(run(repository, doctolib, settings)) == 1
    assert not DeliveryService(repository, settings, sender=notifier).run_once()
    assert len(notifier.sent) == 1
    clock[0] += timedelta(days=1)
    assert run(repository, doctolib, settings) == []
    assert repository.get_job(job['id'])['status'] == 'paused'


@pytest.mark.parametrize('mutation', ['pause', 'delete'])
def test_active_queued_request_cannot_convert_to_paused_capability(tmp_path, clock, mutation):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    intent = request(client, job)
    repository.set_status(job['id'], 'paused' if mutation == 'pause' else 'deleted')
    assert run(repository, doctolib, settings) == []
    saved = repository.get_job(job['id'], include_deleted=True)['check_intent']
    assert saved['id'] == intent['id'] and saved['status'] == 'cancelled'
    if mutation == 'pause':
        assert request(client, job)['id'] != intent['id']
        assert len(run(repository, doctolib, settings)) == 1
    else:
        assert client.post('/api/v1/jobs/' + job['id'] + '/check-now').status_code == 404


def test_paused_queue_search_edit_requires_new_explicit_manual_request(tmp_path, clock):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    repository.set_status(job['id'], 'paused')
    old = request(client, job)
    response = client.patch('/api/v1/jobs/' + job['id'], json={'insurance_sector': 'private'})
    assert response.status_code == 200
    assert response.json()['check_intent']['id'] == old['id']
    assert response.json()['check_intent']['status'] == 'cancelled'
    assert run(repository, doctolib, settings) == []
    fresh = request(client, job)
    assert fresh['id'] != old['id'] and fresh['requested_revision'] == 2
    assert len(run(repository, doctolib, settings)) == 1


@pytest.mark.parametrize('mutation', ['search', 'delete', 'pause'])
def test_paused_manual_inflight_change_stops_remaining_requests_and_notifications(tmp_path, clock, mutation):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    client.patch('/api/v1/jobs/' + job['id'], json={'target_urls': [URL, URL + '&source=second']})
    repository.set_status(job['id'], 'paused')
    request(client, job)

    class Mutating(FixtureDoctolib):
        calls = 0

        def check(self, booking_url, search, meta=None, now=None):
            self.calls += 1
            result = super().check(booking_url, search, meta=meta, now=now)
            if mutation == 'search':
                repository.update_job(job['id'], {'insurance_sector': 'private'})
            else:
                repository.set_status(job['id'], 'deleted' if mutation == 'delete' else 'paused')
            return result

    mutating = Mutating()
    run(repository, mutating, settings)
    assert mutating.calls == 1
    assert not repository.claim_alert()
    assert run(repository, doctolib, settings) == []
    assert repository.get_job(job['id'], include_deleted=True)['status'] != 'active'


def test_manual_intent_response_tracks_terminal_error_not_timestamp_guessing(tmp_path, clock):
    from test_backend_journey import StructuredErrorDoctolib
    client, repository, settings, _doctolib = setup_backend(tmp_path)
    job = create_job(client)
    request(client, job)
    run(repository, StructuredErrorDoctolib(), settings)
    evidence = client.get('/api/v1/jobs/' + job['id']).json()['check_intent']
    assert evidence['status'] == 'completed'
    assert evidence['run_outcome'] == 'error' and evidence['run_id']


def test_concurrent_clicks_coalesce_and_claim_has_only_one_owner(tmp_path, clock):
    import threading
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    run(repository, doctolib, settings)
    barrier = threading.Barrier(3)
    intents, errors = [], []

    def click():
        try:
            barrier.wait(timeout=3)
            intents.append(request(client, job))
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=click) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait(timeout=3)
    for worker in workers:
        worker.join(5)
    assert not errors and all(not worker.is_alive() for worker in workers)
    assert len({intent['id'] for intent in intents}) == 1
    first = repository.claim_due_jobs(limit=1)
    assert len(first) == 1
    assert Repository(repository.database).claim_due_jobs(limit=1) == []
    assert repository.get_job(job['id'])['check_intent']['run_id'] == first[0][0]


def test_paused_manual_capability_rechecked_after_shared_spacing_wait(tmp_path, clock):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    repository.set_status(job['id'], 'paused')
    request(client, job)
    doctolib.before_request = lambda: repository.set_status(job['id'], 'paused')
    before = len(doctolib.fixture_session.calls)
    run(repository, doctolib, settings)
    assert len(doctolib.fixture_session.calls) == before
    assert repository.checks(job['id'])[0]['outcome'] == 'interrupted'
    assert not repository.alerts()


@pytest.mark.parametrize('mutation', ['search', 'delete', 'telegram_optout', 'pause'])
def test_paused_manual_event_is_revalidated_after_its_run_completes(tmp_path, clock, mutation):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    repository.set_status(job['id'], 'paused')
    request(client, job)
    run(repository, doctolib, settings)
    assert repository.alerts()[0]['status'] == 'pending'
    claimed = repository.claim_alert()
    assert claimed  # Completed run retains its scoped delivery capability.
    if mutation == 'search':
        repository.update_job(job['id'], {'insurance_sector': 'private'})
    elif mutation == 'telegram_optout':
        repository.update_job(job['id'], {'telegram_enabled': False})
    else:
        repository.set_status(job['id'], 'deleted' if mutation == 'delete' else 'paused')
    assert repository.begin_alert_attempt(claimed['id'], claimed['owner_token']) is None
    assert repository.alerts()[0]['attempt_count'] == 0
    assert not repository.claim_alert()
    assert run(repository, doctolib, settings) == []


def test_active_override_keeps_followup_when_an_extra_run_is_inflight(tmp_path, clock):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    first = request(client, job)
    run_id, claimed = repository.claim_due_jobs(limit=1)[0]
    assert claimed['intent_id'] == first['id']
    second = request(client, job)
    assert second['id'] != first['id']
    assert second['eligible_at'] == request(client, job)['eligible_at']
    assert parse_time(second['eligible_at']) == clock[0] + timedelta(seconds=60)
    CheckService(repository, doctolib, settings).run_claim(run_id, claimed)
    evidence = repository.get_job(job['id'])['check_intent']
    assert evidence['id'] == second['id'] and evidence['status'] == 'queued'
    assert run(repository, doctolib, settings) == []
    clock[0] += timedelta(seconds=60)
    assert len(run(repository, doctolib, settings)) == 1
    assert repository.get_job(job['id'])['check_intent']['id'] == second['id']
    assert repository.get_job(job['id'])['check_intent']['status'] == 'completed'
