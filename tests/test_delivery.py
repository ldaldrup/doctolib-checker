"""Durable delivery ownership, uncertainty and availability isolation."""

import threading
from datetime import datetime, timedelta

import pytest

from app.notifications import DeliveryOutcome
from app.services.checks import CheckService
from app.services.delivery import DeliveryService
from app.storage.repositories import ConflictError, Repository, iso, parse_time, utc_now
from test_backend_journey import FakeNotifier, create_job, record_result, setup_backend, drop_delivery_columns


def pending(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    CheckService(repository, doctolib, settings).run_due()
    assert repository.alerts()[0]['status'] == 'pending'
    assert repository.alerts()[0]['attempt_count'] == 0
    return client, repository, settings, doctolib, job


def expire_claim(repository, alert_id):
    with repository.database.connection() as conn:
        conn.execute('UPDATE alerts SET claim_until=? WHERE id=?', (iso(utc_now() - timedelta(seconds=1)), alert_id))


def test_blocked_dispatcher_does_not_block_independent_due_availability(tmp_path):
    client, repository, settings, doctolib, _job = pending(tmp_path)
    entered, release = threading.Event(), threading.Event()
    errors = []

    def sender(_settings, _alert):
        entered.set()
        assert release.wait(5)
        return DeliveryOutcome('sent')

    def dispatch():
        try:
            DeliveryService(repository, settings, sender=sender).run_once()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=dispatch)
    worker.start()
    try:
        assert entered.wait(3)
        second = create_job(client)
        outcomes = CheckService(Repository(repository.database), doctolib, settings).run_due()
        assert outcomes == [{'successful_targets': 1, 'failed_targets': 0}]
        assert repository.checks(second['id'])[0]['outcome'] == 'completed'
        assert any(row['job_id'] == second['id'] and row['status'] == 'pending' and row['attempt_count'] == 0 for row in repository.alerts())
        assert worker.is_alive()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors


def test_two_dispatchers_atomically_claim_only_one_owner(tmp_path):
    _client, repository, _settings, _doctolib, _job = pending(tmp_path)
    barrier = threading.Barrier(3)
    claims, errors = [], []

    def claim():
        try:
            barrier.wait(timeout=3)
            claims.append(Repository(repository.database).claim_alert())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=3)
    for thread in threads:
        thread.join(5)
    assert not errors and all(not thread.is_alive() for thread in threads)
    owners = [claim for claim in claims if claim]
    assert len(owners) == 1
    first = owners[0]
    assert repository.begin_alert_attempt(first['id'], 'wrong-owner') is None
    assert repository.begin_alert_attempt(first['id'], first['owner_token'])['attempt_count'] == 1
    assert repository.begin_alert_attempt(first['id'], first['owner_token']) is None
    assert not repository.finish_delivery(first['id'], 'wrong-owner', 'sent')
    assert repository.finish_delivery(first['id'], first['owner_token'], 'sent')
    assert not repository.finish_delivery(first['id'], first['owner_token'], 'retry')
    assert repository.alerts()[0]['status'] == 'sent'


@pytest.mark.parametrize('attempted', [False, True])
def test_expiry_before_dispatch_recovers_but_attempted_crash_is_uncertain(tmp_path, attempted):
    _client, repository, _settings, _doctolib, _job = pending(tmp_path)
    first = repository.claim_alert()
    if attempted:
        assert repository.begin_alert_attempt(first['id'], first['owner_token'])
    expire_claim(repository, first['id'])
    assert repository.reconcile_alert_claims() == 1
    assert not repository.finish_delivery(first['id'], first['owner_token'], 'sent')
    alert = repository.alerts()[0]
    assert alert['attempt_count'] == int(attempted)
    if attempted:
        assert alert['delivery_state'] == 'uncertain'
        assert repository.claim_alert() is None
        with pytest.raises(ConflictError, match='acknowledgement'):
            repository.recover_alert(first['id'])
        assert repository.recover_alert(first['id'], acknowledge_duplicate_risk=True)
    second = repository.claim_alert()
    assert second and second['owner_token'] != first['owner_token']
    assert repository.begin_alert_attempt(first['id'], first['owner_token']) is None


def test_retry_attempt_limit_backoff_and_fair_ready_selection(tmp_path):
    client, repository, settings, doctolib, _job = pending(tmp_path)
    first = repository.alerts()[0]
    second_job = create_job(client)
    CheckService(repository, doctolib, settings).run_due()
    sender = FakeNotifier(False)
    dispatcher = DeliveryService(repository, settings, sender=sender)
    assert dispatcher.run_once()
    failed = next(row for row in repository.alerts() if row['id'] == first['id'])
    assert failed['attempt_count'] == 1
    assert 4 <= (parse_time(failed['next_attempt_at']) - utc_now()).total_seconds() <= 5
    assert dispatcher.run_once()  # Oldest row is backing off; the other job proceeds.
    assert sender.sent[-1]['job_id'] == second_job['id']
    for attempt in range(2, 6):
        with repository.database.connection() as conn:
            conn.execute('UPDATE alerts SET next_attempt_at=? WHERE id=?', (iso(utc_now()), first['id']))
        assert dispatcher.run_once()
        row = next(row for row in repository.alerts() if row['id'] == first['id'])
        assert row['attempt_count'] == attempt
    assert row['delivery_state'] == 'exhausted' and row['next_attempt_at'] is None
    assert repository.claim_alert() is None


def test_recovery_requires_fresh_confirmation_and_never_replays_sent(tmp_path):
    _client, repository, settings, _doctolib, job = pending(tmp_path)
    first = repository.claim_alert()
    repository.begin_alert_attempt(first['id'], first['owner_token'])
    assert repository.finish_delivery(first['id'], first['owner_token'], 'action_required', 'telegram_http_401')
    with repository.database.connection() as conn:
        conn.execute('UPDATE check_results SET checked_at=? WHERE id=?', (iso(utc_now() - timedelta(seconds=301)), first['result_id']))
    assert not repository.recover_alert(first['id'])
    old = repository.alerts()[0]
    target, result_id = record_result(repository, job, 'available', datetime.fromisoformat(old['earliest_slot']), 3)
    assert repository.create_alert(job, target, result_id, datetime.fromisoformat(old['earliest_slot']), owner_token=job['owner_token']) == first['id']
    assert repository.recover_alert(first['id'])
    assert repository.alerts()[0]['attempt_count'] == 1  # Lifetime attempts survive approved recovery.
    assert DeliveryService(repository, settings, sender=FakeNotifier()).run_once()
    with pytest.raises(ConflictError, match='recoverable'):
        repository.recover_alert(first['id'], acknowledge_duplicate_risk=True)
    assert repository.alerts()[0]['status'] == 'sent'
    assert repository.alerts()[0]['attempt_count'] == 2


@pytest.mark.parametrize('mutation', ['pause', 'delete', 'search', 'expired_slot'])
def test_immediate_predispatch_recheck_stops_claim_after_eligibility_changes(tmp_path, mutation):
    client, repository, _settings, _doctolib, job = pending(tmp_path)
    first = repository.claim_alert()
    if mutation == 'pause':
        repository.set_status(job['id'], 'paused')
    elif mutation == 'delete':
        repository.set_status(job['id'], 'deleted')
    elif mutation == 'search':
        assert client.patch('/api/v1/jobs/' + job['id'], json={'insurance_sector': 'private'}).status_code == 200
    else:
        with repository.database.connection() as conn:
            conn.execute('UPDATE check_results SET earliest_slot=? WHERE id=?', (iso(utc_now() - timedelta(seconds=1)), first['result_id']))
    assert repository.begin_alert_attempt(first['id'], first['owner_token']) is None
    assert repository.alerts()[0]['attempt_count'] == 0


def test_read_only_delivery_api_exposes_backlog_without_claim_credentials(tmp_path):
    client, repository, _settings, _doctolib, _job = pending(tmp_path)
    claimed = repository.claim_alert()
    repository.touch_worker()
    repository.touch_dispatcher()
    for path in ('/api/v1/alerts', '/api/v1/status'):
        response = client.get(path)
        assert response.status_code == 200
        assert claimed['owner_token'] not in response.text
        assert 'claim_owner_token' not in response.text
    status = client.get('/api/v1/status').json()
    assert status['api_alive'] and status['worker_alive'] and status['dispatcher_alive']
    with repository.database.connection() as conn:
        conn.execute('UPDATE dispatcher_heartbeat SET last_seen_at=?', (iso(utc_now() - timedelta(seconds=91)),))
    stale = client.get('/api/v1/status').json()
    assert stale['api_alive'] and stale['worker_alive'] and not stale['dispatcher_alive']
    assert sum(stale['delivery_backlog'].values()) == 1
    assert repository.alerts()[0]['status'] == 'pending'


def test_provider_retry_after_is_capped_and_twenty_four_hour_age_exhausts(tmp_path):
    _client, repository, _settings, _doctolib, _job = pending(tmp_path)
    claim = repository.claim_alert()
    repository.begin_alert_attempt(claim['id'], claim['owner_token'])
    assert repository.finish_delivery(claim['id'], claim['owner_token'], 'retry', 'telegram_rate_limited', retry_after=100000)
    alert = repository.alerts()[0]
    assert 899 <= (parse_time(alert['next_attempt_at']) - utc_now()).total_seconds() <= 900
    with repository.database.connection() as conn:
        conn.execute('UPDATE alerts SET delivery_epoch_at=?,next_attempt_at=? WHERE id=?', (iso(utc_now() - timedelta(hours=25)), iso(utc_now()), claim['id']))
    assert repository.claim_alert() is None
    assert repository.alerts()[0]['delivery_state'] == 'exhausted'


def test_v4_zero_attempt_pending_preserves_uncertain_acceptance_until_acknowledged(tmp_path):
    _client, repository, settings, _doctolib, job = pending(tmp_path)
    original = repository.alerts()[0]
    with repository.database.connection() as conn:
        drop_delivery_columns(conn)
        conn.execute('UPDATE schema_version SET version=4')
    repository.database.initialize()
    alert = repository.alerts()[0]
    assert alert['id'] == original['id'] and alert['status'] == 'pending'
    assert alert['attempt_count'] == 0 and alert['delivery_state'] == 'uncertain'
    sender = FakeNotifier()
    dispatcher = DeliveryService(repository, settings, sender=sender)
    assert not dispatcher.run_once() and sender.sent == []
    with pytest.raises(ConflictError, match='acknowledgement'):
        repository.recover_alert(alert['id'])
    with repository.database.connection() as conn:
        conn.execute('UPDATE check_results SET checked_at=? WHERE id=?', (iso(utc_now() - timedelta(seconds=301)), alert['result_id']))
    assert not repository.recover_alert(alert['id'], acknowledge_duplicate_risk=True)
    slot = datetime.fromisoformat(alert['earliest_slot'])
    target, result_id = record_result(repository, job, 'available', slot, 3)
    assert repository.create_alert(job, target, result_id, slot, owner_token=job['owner_token']) == alert['id']
    assert repository.recover_alert(alert['id'], acknowledge_duplicate_risk=True)
    assert dispatcher.run_once() and len(sender.sent) == 1
    assert repository.alerts()[0]['status'] == 'sent'


def test_attempted_delivery_cancelled_during_send_stays_cancelled_after_crash(tmp_path):
    _client, repository, _settings, _doctolib, job = pending(tmp_path)
    claim = repository.claim_alert()
    assert repository.begin_alert_attempt(claim['id'], claim['owner_token'])
    # Provider acceptance may have occurred, but no acknowledgement survived.
    # A separate check closes the episode before the dispatcher lease expires.
    record_result(repository, job, 'no_availability')
    assert repository.alerts()[0]['status'] == 'cancelled'
    expire_claim(repository, claim['id'])
    assert repository.reconcile_alert_claims() == 1
    old = repository.alerts()[0]
    assert old['status'] == 'cancelled' and old['delivery_state'] == 'uncertain'
    assert not repository.finish_delivery(claim['id'], claim['owner_token'], 'sent')
    assert not repository.recover_alert(claim['id'], acknowledge_duplicate_risk=True)
    assert repository.claim_alert() is None
