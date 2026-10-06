"""Retention keeps live capability closure and permanent episode boundaries."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.retention import apply_retention, preview_retention
from test_backend_journey import create_job, record_result, setup_backend

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=120)).isoformat()


def history(tmp_path, monkeypatch, *, sent=True, notifications=True):
    monkeypatch.setattr('app.storage.repositories.utc_now', lambda: NOW)
    client, repository, _, _ = setup_backend(tmp_path)
    job = create_job(client)
    if not notifications:
        repository.update_job(job['id'], {'telegram_enabled': False})
    target, result = record_result(repository, job, 'available', NOW + timedelta(days=10), 1)
    with repository.database.connection() as conn:
        run = conn.execute('SELECT run_id FROM check_results WHERE id=?', (result,)).fetchone()[0]
    repository.finish_run(run, job['id'], 1, 0, 300, owner_token=job['owner_token'])
    with repository.database.connection() as conn:
        conn.execute("UPDATE check_runs SET started_at=?,finished_at=?", (OLD, OLD))
        conn.execute('UPDATE check_results SET checked_at=?', (OLD,))
        conn.execute('UPDATE availability_events SET observed_at=?,routed_at=?', (OLD, OLD))
        if sent:
            conn.execute("UPDATE alerts SET status='sent',sent_at=?,created_at=?", (OLD, OLD))
        else:
            conn.execute('UPDATE alerts SET created_at=?', (OLD,))
    # New current evidence permits old result/run history to disappear.
    record_result(repository, job, 'available', NOW + timedelta(days=10), 1)
    return repository, job, target, result, run


def apply(database, plan, **kwargs):
    return apply_retention(database, cutoff=plan['cutoff'], plan_id=plan['plan_id'], now=NOW, **kwargs)


@pytest.mark.parametrize('notifications', [True, False])
def test_purge_display_history_never_replays_observed_episode(tmp_path, monkeypatch, notifications):
    repository, job, target, old_result, old_run = history(tmp_path, monkeypatch, notifications=notifications)
    plan = preview_retention(repository.database, now=NOW)
    assert plan['eligible']['check_runs'] == 1
    assert OLD not in json.dumps(plan)
    result = apply(repository.database, plan, batch_size=1)
    assert result['completed']
    with repository.database.connection() as conn:
        assert not conn.execute('SELECT 1 FROM check_results WHERE id=?', (old_result,)).fetchone()
        assert not conn.execute('SELECT 1 FROM check_runs WHERE id=?', (old_run,)).fetchone()
        event = conn.execute('SELECT * FROM availability_events').fetchone()
        assert event['result_id'] is None
        assert bool(json.loads(event['sent_destinations'])) == notifications
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    record_result(repository, job, 'available', NOW + timedelta(days=10), 1)
    assert repository.alerts() == []
    record_result(repository, job, 'no_availability')
    record_result(repository, job, 'available', NOW + timedelta(days=10), 1)
    with repository.database.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM availability_events').fetchone()[0] == 2
    assert len(repository.alerts()) == int(notifications)
    again = apply(repository.database, plan)
    assert not any(again['deleted'].values())


def test_changed_claim_pins_full_reference_closure_and_interruption_retries(tmp_path, monkeypatch):
    repository, _, _, old_result, old_run = history(tmp_path, monkeypatch)
    plan = preview_retention(repository.database, now=NOW)
    with repository.database.connection() as conn:
        conn.execute("UPDATE alerts SET status='failed',delivery_state='uncertain',claim_owner_token='new-owner',claim_result_id=?", (old_result,))
    result = apply(repository.database, plan, batch_size=1, max_batches=1)
    assert not result['completed'] and result['skipped_changed_or_pinned']['alerts'] == 1
    retry = apply(repository.database, plan, batch_size=1)
    assert retry['completed']
    with repository.database.connection() as conn:
        assert conn.execute('SELECT 1 FROM check_results WHERE id=?', (old_result,)).fetchone()
        assert conn.execute('SELECT 1 FROM check_runs WHERE id=?', (old_run,)).fetchone()
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_changed_run_protects_unchanged_results(tmp_path, monkeypatch):
    repository, _, _, old_result, old_run = history(tmp_path, monkeypatch)
    plan = preview_retention(repository.database, now=NOW)
    with repository.database.connection() as conn:
        conn.execute("UPDATE check_runs SET outcome='yielded',finished_at=NULL WHERE id=?", (old_run,))
    result = apply(repository.database, plan)
    assert result['skipped_changed_or_pinned']['check_results'] == 1
    with repository.database.connection() as conn:
        assert conn.execute('SELECT 1 FROM check_results WHERE id=?', (old_result,)).fetchone()


def test_optional_operation_keys_keep_seven_day_guarantee_and_explicit_plan(tmp_path, monkeypatch):
    repository, *_ = history(tmp_path, monkeypatch)
    with repository.database.connection() as conn:
        conn.execute('INSERT INTO channel_mutations VALUES(?,?,?,?,?)', ('old-key', 'hash', None, OLD, OLD))
    default = preview_retention(repository.database, now=NOW)
    assert default['eligible']['channel_mutations'] == 0
    explicit = preview_retention(repository.database, now=NOW, cleanup_keys=True)
    assert explicit['eligible']['channel_mutations'] == 1
    with pytest.raises(ValueError, match='must match'):
        apply_retention(repository.database, cutoff=NOW.isoformat(), plan_id=explicit['plan_id'], now=NOW)
    apply(repository.database, explicit)
    with repository.database.connection() as conn:
        assert not conn.execute("SELECT 1 FROM channel_mutations WHERE key='old-key'").fetchone()


@pytest.mark.parametrize('held', [True, False])
def test_old_paused_manual_history_is_eligible_but_held_capability_survives(tmp_path, monkeypatch, held):
    from app.services.checks import CheckService
    from app.services.delivery import DeliveryService
    from test_quiet_hours_delivery import Sender, make_job
    clock = [NOW]
    monkeypatch.setattr('app.storage.repositories.utc_now', lambda: clock[0])
    _, repository, settings, doctolib, job = make_job(tmp_path, enabled=held)
    repository.set_status(job['id'], 'paused')
    intent = repository.request_check(job['id'])
    checker = CheckService(repository, doctolib, settings)
    checker.run_due()
    sender = Sender()
    dispatcher = DeliveryService(repository, settings, sender=sender)
    if not held:
        assert dispatcher.run_once()
    with repository.database.connection() as conn:
        old_run = conn.execute('SELECT run_id FROM check_intents WHERE id=?', (intent['id'],)).fetchone()[0]
        conn.execute('UPDATE check_intents SET requested_at=?,eligible_at=? WHERE id=?', (OLD, OLD, intent['id']))
        conn.execute('UPDATE check_runs SET started_at=?,finished_at=? WHERE id=?', (OLD, OLD, old_run))
        conn.execute('UPDATE check_results SET checked_at=?', (OLD,))
        conn.execute('UPDATE availability_events SET observed_at=?,routed_at=?', (OLD, OLD))
        conn.execute('UPDATE alerts SET created_at=?', (OLD,))
        if not held:
            conn.execute('UPDATE alerts SET sent_at=?,last_attempt_at=?,attempt_started_at=?', (OLD, OLD, OLD))
    # A separate later manual run supplies the latest evidence.
    clock[0] += timedelta(seconds=61)
    repository.request_check(job['id'])
    checker.run_due()
    plan = preview_retention(repository.database, now=clock[0])
    apply_retention(repository.database, cutoff=plan['cutoff'], plan_id=plan['plan_id'], now=clock[0])
    with repository.database.connection() as conn:
        assert bool(conn.execute('SELECT 1 FROM check_runs WHERE id=?', (old_run,)).fetchone()) == held
        assert bool(conn.execute('SELECT 1 FROM check_intents WHERE id=?', (intent['id'],)).fetchone()) == held
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    if held:
        clock[0] = NOW.replace(hour=5)
        assert not dispatcher.run_once()
        assert repository.alerts()[0]['quiet_state'] == 'needs_manual_check'
        assert repository.get_job(job['id'])['check_intent']['status'] == 'completed'
        repository.request_check(job['id'])
        checker.run_due()
        # Old delivery epoch stays expired; only explicit fresh recovery may send.
        assert not dispatcher.run_once()
        assert repository.recover_alert(repository.alerts()[0]['id'])
        assert dispatcher.run_once()
        assert len(sender.sent) == 1
    assert repository.get_job(job['id'])['status'] == 'paused'


def test_cleanup_seven_days_and_cancelled_accepted_evidence(tmp_path, monkeypatch):
    repository, *_ = history(tmp_path, monkeypatch)
    with repository.database.connection() as conn:
        for key, days in [('within', 6), ('eligible', 8)]:
            created = (NOW - timedelta(days=days)).isoformat()
            conn.execute('INSERT INTO channel_mutations VALUES(?,?,?,?,?)', (key, 'hash', None, created, OLD))
        conn.execute("UPDATE alerts SET status='cancelled',last_attempt_outcome='sent'")
    plan = preview_retention(repository.database, now=NOW, cleanup_keys=True)
    assert plan['eligible']['channel_mutations'] == 1
    apply(repository.database, plan)
    with repository.database.connection() as conn:
        assert conn.execute("SELECT 1 FROM channel_mutations WHERE key='within'").fetchone()
        assert not conn.execute("SELECT 1 FROM channel_mutations WHERE key='eligible'").fetchone()
        assert json.loads(conn.execute('SELECT sent_destinations FROM availability_events').fetchone()[0])


def test_interrupted_after_committed_deletion_resumes_without_replay(tmp_path, monkeypatch):
    repository, job, _, old_result, old_run = history(tmp_path, monkeypatch)
    plan = preview_retention(repository.database, now=NOW)
    interrupted = apply(repository.database, plan, batch_size=1, max_batches=1)
    assert not interrupted['completed'] and interrupted['deleted']['alerts'] == 1
    resumed = apply(repository.database, plan, batch_size=1)
    assert resumed['completed'] and resumed['deleted']['alerts'] == 0
    with repository.database.connection() as conn:
        assert not conn.execute('SELECT 1 FROM check_results WHERE id=?', (old_result,)).fetchone()
        assert not conn.execute('SELECT 1 FROM check_runs WHERE id=?', (old_run,)).fetchone()
        assert json.loads(conn.execute('SELECT sent_destinations FROM availability_events').fetchone()[0])
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    record_result(repository, job, 'available', NOW + timedelta(days=10), 1)
    assert repository.alerts() == []
    with pytest.raises(ValueError, match='must match'):
        apply_retention(repository.database, cutoff=plan['cutoff'], plan_id=plan['plan_id'], now=NOW + timedelta(days=31))


@pytest.mark.parametrize('field', ['sent_at', 'last_attempt_at', 'attempt_started_at'])
def test_old_created_fresh_terminal_delivery_stays_ninety_days(tmp_path, monkeypatch, field):
    repository, _, _, old_result, old_run = history(tmp_path, monkeypatch)
    with repository.database.connection() as conn:
        conn.execute('UPDATE alerts SET ' + field + '=?', (NOW.isoformat(),))
    plan = preview_retention(repository.database, now=NOW)
    assert plan['eligible']['alerts'] == 0
    assert plan['eligible']['check_results'] == 0
    assert plan['eligible']['check_runs'] == 0
    apply(repository.database, plan)
    with repository.database.connection() as conn:
        assert conn.execute('SELECT 1 FROM check_results WHERE id=?', (old_result,)).fetchone()
        assert conn.execute('SELECT 1 FROM check_runs WHERE id=?', (old_run,)).fetchone()
        assert conn.execute('SELECT COUNT(*) FROM alerts').fetchone()[0] == 1
