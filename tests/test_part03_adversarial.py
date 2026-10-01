"""Independent legacy acceptance-ambiguity regression."""
from datetime import datetime

import pytest

from app.services.checks import CheckService
from app.services.delivery import DeliveryService
from app.storage.repositories import ConflictError
from test_backend_journey import FakeNotifier, create_job, drop_delivery_columns, record_result, setup_backend


@pytest.mark.parametrize('legacy_status,attempt_count', [('pending', 0), ('failed', 2)])
def test_unsent_inline_migration_never_automatically_replays_possible_acceptance(tmp_path, legacy_status, attempt_count):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    CheckService(repository, doctolib, settings).run_due()
    original = repository.alerts()[0]
    with repository.database.connection() as conn:
        drop_delivery_columns(conn)
        conn.execute('UPDATE schema_version SET version=4')
        conn.execute('UPDATE alerts SET status=?,attempt_count=? WHERE id=?',
                     (legacy_status, attempt_count, original['id']))
    # Old provider acceptance followed by process death leaves exactly this
    # pending/count0 state; a migration cannot prove it was never transmitted.
    repository.database.initialize()
    migrated = repository.alerts()[0]
    assert migrated['status'] == legacy_status
    assert migrated['delivery_state'] == 'uncertain'
    assert migrated['last_attempt_outcome'] == 'legacy_unknown'
    assert migrated['attempt_count'] == attempt_count
    sender = FakeNotifier()
    dispatcher = DeliveryService(repository, settings, sender=sender)
    assert not dispatcher.run_once() and not sender.sent
    slot = datetime.fromisoformat(original['earliest_slot'])
    target, result_id = record_result(repository, job, 'available', slot, 3)
    assert repository.create_alert(job, target, result_id, slot, owner_token=job['owner_token']) == original['id']
    assert not dispatcher.run_once() and not sender.sent  # Fresh evidence alone cannot resolve acceptance.
    with pytest.raises(ConflictError, match='acknowledgement'):
        repository.recover_alert(original['id'])
    assert repository.recover_alert(original['id'], acknowledge_duplicate_risk=True)
    assert dispatcher.run_once() and len(sender.sent) == 1
    assert repository.alerts()[0]['attempt_count'] == attempt_count + 1


def test_proven_unstarted_failure_release_does_not_increment_attempts(tmp_path):
    from app.storage.repositories import parse_time, utc_now
    client, repository, settings, doctolib = setup_backend(tmp_path)
    create_job(client)
    CheckService(repository, doctolib, settings).run_due()
    claim = repository.claim_alert()
    assert not repository.finish_unstarted_delivery(claim['id'], 'wrong_owner', 'retry')
    assert repository.finish_unstarted_delivery(claim['id'], claim['owner_token'], 'retry', 'telegram_transport_setup_failed')
    failed = repository.alerts()[0]
    assert failed['attempt_count'] == failed['delivery_epoch_attempts'] == 0
    assert failed['last_attempt_at'] is None
    assert failed['delivery_state'] == 'retry'
    assert 4 <= (parse_time(failed['next_attempt_at']) - utc_now()).total_seconds() <= 5
    assert repository.claim_alert() is None
    with repository.database.connection() as conn:
        conn.execute('UPDATE alerts SET next_attempt_at=NULL WHERE id=?', (claim['id'],))
    second = repository.claim_alert()
    assert repository.finish_unstarted_delivery(second['id'], second['owner_token'], 'action_required', 'telegram_not_configured')
    assert repository.recover_alert(claim['id'])
    final = repository.alerts()[0]
    assert final['attempt_count'] == final['delivery_epoch_attempts'] == 0
    assert final['delivery_state'] == 'ready'
    third = repository.claim_alert()
    assert repository.begin_alert_attempt(third['id'], third['owner_token'])
    assert not repository.finish_unstarted_delivery(third['id'], third['owner_token'], 'retry')
    assert repository.finish_delivery(third['id'], third['owner_token'], 'sent')
    assert repository.alerts()[0]['attempt_count'] == 1


def test_unstarted_finish_respects_lease_and_original_age_budget(tmp_path):
    from datetime import timedelta
    from app.storage.repositories import precise_iso, utc_now
    client, repository, _settings, doctolib = setup_backend(tmp_path)
    create_job(client)
    CheckService(repository, doctolib, _settings).run_due()
    claim = repository.claim_alert()
    with repository.database.connection() as conn:
        conn.execute('UPDATE alerts SET claim_until=? WHERE id=?',
                     (precise_iso(utc_now() - timedelta(seconds=1)), claim['id']))
    assert not repository.finish_unstarted_delivery(claim['id'], claim['owner_token'], 'retry')
    repository.reconcile_alert_claims()
    second = repository.claim_alert()
    with repository.database.connection() as conn:
        conn.execute('UPDATE alerts SET delivery_epoch_at=? WHERE id=?',
                     (precise_iso(utc_now() - timedelta(hours=25)), claim['id']))
    assert repository.finish_unstarted_delivery(second['id'], second['owner_token'], 'retry')
    row = repository.alerts()[0]
    assert row['delivery_state'] == 'exhausted' and row['next_attempt_at'] is None
    assert row['attempt_count'] == 0 and row['last_attempt_at'] is None


def test_delivery_guard_can_fail_immediately_under_writer_contention(tmp_path):
    import sqlite3
    import time
    client, repository, settings, doctolib = setup_backend(tmp_path)
    create_job(client)
    CheckService(repository, doctolib, settings).run_due()
    claim = repository.claim_alert()
    with repository.database.connection() as holder:
        holder.execute('BEGIN IMMEDIATE')
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            repository.begin_alert_attempt(claim['id'], claim['owner_token'], wait_seconds=0)
        assert time.monotonic() - started < 1
    row = repository.alerts()[0]
    assert row['attempt_count'] == 0 and row['attempt_started_at'] is None
    for invalid in (float('inf'), float('nan')):
        with pytest.raises(ValueError, match='finite'):
            repository.begin_alert_attempt(claim['id'], claim['owner_token'], wait_seconds=invalid)
    assert repository.begin_alert_attempt(claim['id'], claim['owner_token'], wait_seconds=0)['attempt_count'] == 1
