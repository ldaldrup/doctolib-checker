"""Routing boundaries, recipient races and independent delivery ownership."""
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from app.models import AvailabilityResult
from app.notification_secrets import NotificationSecrets
from app.notifications import DeliveryOutcome
from app.services.delivery import DeliveryService
from app.settings import Settings
from app.storage.db import Database
from app.storage.repositories import Repository


@pytest.fixture
def routed(tmp_path):
    db = Database(str(tmp_path / 'checker.sqlite'))
    db.initialize()
    repo = Repository(db)
    key = Fernet.generate_key().decode()
    secrets = NotificationSecrets(key)
    settings = Settings(notification_secret_key=key)
    def channel(name, chat):
        return repo.create_channel(dict(name=name, enabled=True,
            token_ciphertext=secrets.encrypt('123:test-token'),
            chat_ciphertext=secrets.encrypt(chat), destination_identity=chat), name, name)[0]
    first, second = channel('Telegram1', '1'), channel('Telegram2', '2')
    values = dict(name='Job', interval_seconds=300, date_mode='first_available', horizon_days=15,
        time_zone='UTC', insurance_sector='public', telehealth=False, telegram_enabled=True,
        notification_channel_ids=[first['id'], second['id']])
    target = dict(booking_url='https://www.doctolib.de/test', country='de', profile_slug='test',
        practice_id='1', motive_id='2', agenda_ids_str='3', practice_name='Practice', practitioner_name='Doctor')
    job = repo.create_job(values, [target])
    slot = datetime.now(timezone.utc) + timedelta(days=1)
    def observe(status='available'):
        repo.set_job_due(job['id'], datetime.now(timezone.utc)-timedelta(seconds=1))
        # This test controls observation time, independently of polling-floor checks.
        with db.connection() as conn:
            conn.execute('UPDATE jobs SET last_started_at=NULL,last_finished_at=NULL,next_check_at=? WHERE id=?',((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),job['id']))
        run, claimed = repo.claim_due_jobs(limit=1)[0]
        result = AvailabilityResult(status=status,slot_count=int(status=='available'),
            earliest_slot=slot if status=='available' else None)
        result_id = repo.insert_result(run,claimed,repo.get_targets(job['id'])[0],result,owner_token=claimed['owner_token'])
        repo.finish_run(run,job['id'],1,0,300,owner_token=claimed['owner_token'])
        return result_id
    return db, repo, settings, secrets, job, first, second, observe


def test_independent_retry_does_not_repeat_success(routed):
    db, repo, settings, _, _, _, _, observe = routed
    observe()
    sent = []
    def sender(configured, alert):
        sent.append(configured.telegram_chat_id)
        return DeliveryOutcome('retry','telegram_connect_failed') if configured.telegram_chat_id=='1' else DeliveryOutcome('sent')
    service = DeliveryService(repo,settings,sender)
    assert service.run_once()
    assert service.run_once()
    with db.connection() as conn:
        conn.execute("UPDATE alerts SET next_attempt_at=created_at WHERE status='failed'")
    assert service.run_once()
    assert sent.count('1')==2 and sent.count('2')==1
    alerts=repo.alerts()
    assert sorted(a['attempt_count'] for a in alerts)==[1,2]
    with db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM availability_events').fetchone()[0]==1


@pytest.mark.parametrize('disabled', [True,False])
def test_observed_episode_without_routing_is_consumed(routed, disabled):
    db, repo, _, _, job, first, _, observe = routed
    repo.update_job(job['id'],{'telegram_enabled':not disabled,'notification_channel_ids':[]})
    observe()
    assert repo.alerts()==[]
    repo.update_job(job['id'],{'telegram_enabled':True,'notification_channel_ids':[first['id']]})
    observe()
    assert repo.alerts()==[]
    observe('no_availability')
    observe()
    assert len(repo.alerts())==1
    with db.connection() as conn:
        events=conn.execute('SELECT routing_snapshot FROM availability_events ORDER BY rowid').fetchall()
        assert len(events)==2 and events[0][0]=='[]'


def test_recipient_rotation_cancels_claim_but_records_inflight_honestly(routed):
    _, repo, _, secrets, _, first, _, observe = routed
    observe()
    claimed=repo.claim_alert()
    first=repo.get_channel(claimed['channel_config_id'])
    repo.update_channel(first['id'],{'chat_ciphertext':secrets.encrypt('replacement'),'destination_identity':'replacement'},first['edit_version'])
    assert repo.begin_alert_attempt(claimed['id'],claimed['owner_token']) is None
    first_alert=next(a for a in repo.alerts() if a['id']==claimed['id'])
    assert first_alert['status']=='cancelled' and first_alert['error_summary']=='destination_changed'
    other=repo.claim_alert()
    assert repo.begin_alert_attempt(other['id'],other['owner_token'])
    channel=repo.get_channel(other['channel_config_id'])
    repo.update_channel(channel['id'],{'enabled':False},channel['edit_version'])
    assert repo.finish_delivery(other['id'],other['owner_token'],'sent')
    after=next(a for a in repo.alerts() if a['id']==other['id'])
    assert after['status']=='cancelled' and after['sent_at'] and after['last_attempt_outcome']=='sent'
    assert after['error_summary']=='channel_disabled'
    observe()
    assert len(repo.alerts())==2
    assert repo.get_pending_alerts()==[]


def test_same_destination_credential_repair_requires_explicit_fresh_recovery(routed):
    _, repo, settings, secrets, _, first, _, observe = routed
    observe()
    service=DeliveryService(repo,settings,lambda s,a:DeliveryOutcome('action_required','telegram_rejected_401'))
    assert service.run_once()
    failed=next(a for a in repo.alerts() if a['status']=='failed')
    first=repo.get_channel(failed['channel_config_id'])
    updated=repo.update_channel(first['id'],{'token_ciphertext':secrets.encrypt('123:repaired')},first['edit_version'])
    assert next(a for a in repo.alerts() if a['id']==failed['id'])['delivery_state']=='action_required'
    repo.update_channel(first['id'],{'token_ciphertext':secrets.encrypt('123:repaired-again')},updated['edit_version'],recover_failed=True)
    recovered=next(a for a in repo.alerts() if a['id']==failed['id'])
    assert recovered['status']=='pending' and recovered['attempt_count']==1
    assert recovered['delivery_epoch_attempts']==0
    assert recovered['destination_version']==1


def test_cancelled_detached_recipient_is_not_reactivated_on_same_episode(routed):
    _,repo,_,_,job,first,second,observe=routed
    observe()
    repo.update_job(job['id'],{'notification_channel_ids':[second['id']]})
    repo.update_job(job['id'],{'notification_channel_ids':[first['id'],second['id']]})
    observe()
    row=next(a for a in repo.alerts() if a['channel_config_id']==first['id'])
    assert row['status']=='cancelled' and row['error_summary']=='channel_detached'
    assert len(repo.alerts())==2


def test_explicit_recovery_retains_original_destination_and_fresh_capability(routed):
    _,repo,_,secrets,_,first,second,observe=routed
    observe()
    first_alert=next(a for a in repo.alerts() if a['channel_config_id']==first['id'])
    repo.update_channel(first['id'],{'enabled':False},first['edit_version'])
    disabled=repo.get_channel(first['id'])
    repo.update_channel(first['id'],{'enabled':True},disabled['edit_version'])
    observe()
    assert next(a for a in repo.alerts() if a['id']==first_alert['id'])['status']=='cancelled'
    assert repo.recover_alert(first_alert['id'])
    assert next(a for a in repo.alerts() if a['id']==first_alert['id'])['status']=='pending'
    other=next(a for a in repo.alerts() if a['channel_config_id']==second['id'])
    repo.update_channel(second['id'],{'chat_ciphertext':secrets.encrypt('new'),'destination_identity':'new'},second['edit_version'])
    assert not repo.recover_alert(other['id'])


def test_credential_change_between_claim_and_permission_cannot_send_stale_secret(routed):
    _,repo,_,secrets,_,_,_,observe=routed
    observe()
    claim=repo.claim_alert()
    channel=repo.get_channel(claim['channel_config_id'])
    repo.update_channel(channel['id'],{'token_ciphertext':secrets.encrypt('123:new')},channel['edit_version'])
    assert repo.begin_alert_attempt(claim['id'],claim['owner_token']) is None
    fresh=repo.claim_alert()
    assert fresh['id']==claim['id'] and fresh['credential_version']==channel['credential_version']+1
    assert repo.begin_alert_attempt(fresh['id'],fresh['owner_token'])


@pytest.mark.parametrize("history", ["sent","cancelled","failed","fresh_pending","stale_pending","claimed"])
def test_migration_preserves_attempts_claims_and_consumes_unrouted_observation(routed, history):
    db,repo,_,_,job,_,_,observe=routed
    observe()
    claim=repo.claim_alert()
    if history == 'claimed':
        assert repo.begin_alert_attempt(claim['id'],claim['owner_token'])
    with db.connection() as conn:
        if history != 'claimed':
            status=history if history in ('sent','cancelled','failed') else 'pending'
            conn.execute("UPDATE alerts SET status=?,claim_owner_token=NULL,claim_until=NULL,claim_result_id=NULL,claim_search_revision=NULL,attempt_count=2,delivery_epoch_attempts=2,last_attempt_outcome=?,error_summary='historical_reason',sent_at=? WHERE id=?",(status,'sent' if status=='sent' else 'retry',datetime.now(timezone.utc).isoformat() if status=='sent' else None,claim['id']))
        if history == 'stale_pending':
            conn.execute("UPDATE check_results SET checked_at=? WHERE id=(SELECT result_id FROM alerts WHERE id=?)",((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),claim['id']))
        before=dict(conn.execute('SELECT * FROM alerts WHERE id=?',(claim['id'],)).fetchone())
        conn.execute('DELETE FROM alerts WHERE id!=?',(claim['id'],))
        conn.execute('UPDATE alerts SET dedupe_key=? WHERE id=?',(before['dedupe_key'].split(':channel:')[0],claim['id']))
        # Honest schema7 fixture: remove only schema8 structures/fields.
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('DROP INDEX idx_alerts_event_destination')
        for col in ('event_id','channel_config_id','destination_version','credential_version','channel_name'):
            conn.execute('ALTER TABLE alerts DROP COLUMN '+col)
        for table in ('availability_events','job_channels','channel_tests','channel_mutations','notification_onboarding','notification_channels'):
            conn.execute('DROP TABLE '+table)
        conn.execute('UPDATE schema_version SET version=7')
    db.initialize()
    with db.connection() as conn:
        migrated=dict(conn.execute('SELECT * FROM alerts WHERE id=?',(claim['id'],)).fetchone())
        for field in ('claim_owner_token','claim_until','claim_result_id','claim_search_revision','attempt_started_at','last_attempt_at','last_attempt_outcome','attempt_count','delivery_epoch_at','delivery_epoch_attempts'):
            assert migrated[field]==before[field]
        assert migrated['status']==(before['status'] if before['status'] in ('sent','cancelled') else 'cancelled')
        assert migrated['sent_at']==before['sent_at']
        assert migrated['error_summary']==(before['error_summary'] if before['status'] in ('sent','cancelled') else 'migration_legacy_requires_import')
        assert conn.execute('SELECT COUNT(*) FROM availability_events').fetchone()[0]==1


def test_migration_consumes_episode_observed_without_any_legacy_alert(routed):
    db,repo,_,secrets,job,_,_,observe=routed
    repo.update_job(job['id'],{'notification_channel_ids':[]})
    observe()
    assert repo.alerts()==[]
    with db.connection() as conn:
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('DROP INDEX idx_alerts_event_destination')
        for col in ('event_id','channel_config_id','destination_version','credential_version','channel_name'):
            conn.execute('ALTER TABLE alerts DROP COLUMN '+col)
        for table in ('availability_events','job_channels','channel_tests','channel_mutations','notification_onboarding','notification_channels'):
            conn.execute('DROP TABLE '+table)
        conn.execute('UPDATE schema_version SET version=7')
    db.initialize()
    imported,_=repo.create_channel(dict(name='Telegram1',enabled=True,token_ciphertext=secrets.encrypt('123:test'),
        chat_ciphertext=secrets.encrypt('1'),destination_identity='1'),'import','import',legacy=True)
    assert repo.get_job(job['id'])['notification_channel_ids']==[imported['id']]
    observe()
    assert repo.alerts()==[]
    observe('no_availability')
    observe()
    assert len(repo.alerts())==1
