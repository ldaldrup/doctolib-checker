"""Final-schema recovery rehearsals use fake providers and durable relationships."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from app import admin
from app.api.app import create_app
from app.notification_secrets import NotificationSecrets
from app.services.checks import CheckService
from app.services.delivery import DeliveryService
from app.services.channel_tests import channel_settings
from app.storage.db import Database, SCHEMA_VERSION
from app.storage.repositories import Repository, LeaseLostError
from test_backend_journey import URL, create_job, setup_backend, FakeNotifier
from test_bounded_workload_storage import complete_target


def all_rows(path):
    with sqlite3.connect(path) as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {table: conn.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall() for table in tables}


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    now = [datetime(2026, 10, 5, 20, 30, tzinfo=timezone.utc)]
    monkeypatch.setattr('app.storage.repositories.utc_now', lambda: now[0])
    client, repository, settings, doctolib = setup_backend(tmp_path)
    secrets = NotificationSecrets(settings.notification_secret_key)
    repository.update_smtp_transport({'enabled':True, 'host':'smtp.example.com', 'port':587,
        'tls_mode':'starttls','sender_name':'Recovered Checker',
        'sender_email_ciphertext':secrets.encrypt('sender@example.com'),
        'username_ciphertext':secrets.encrypt('mailer'), 'password_ciphertext':secrets.encrypt('fake-password'),
        'destination_identity':secrets.identity('sender@example.com')}, 1)
    email, _ = repository.create_channel({'type':'email','name':'Recovery email','enabled':True,
        'email_recipient_ciphertext':secrets.encrypt('recipient@example.net'),
        'destination_identity':secrets.identity('recipient@example.net')}, 'email-key', 'email-fingerprint')
    channels = [channel['id'] for channel in repository.list_channels()['items']]
    repository.update_settings({'message_content':{'preset':'compact','silent':False}},300)

    # A paused manual event stays held, with immutable routing and selected channels.
    held = create_job(client)
    held = client.patch('/api/v1/jobs/'+held['id'],json={'notification_channel_ids':channels,
        'quiet_hours_enabled':True,'quiet_hours_start':'22:00','quiet_hours_end':'07:00',
        'message_content':{'preset':'compact','silent':False}}).json()
    repository.set_status(held['id'],'paused')
    manual = repository.request_check(held['id'])
    assert CheckService(repository,doctolib,settings).run_due() == [{'successful_targets':1,'failed_targets':0}]
    assert not DeliveryService(repository,settings,sender=FakeNotifier()).run_once()
    assert all(alert['quiet_state']=='held' for alert in repository.alerts())

    # Independent ready delivery has a live, unstarted claim at the snapshot.
    ready = create_job(client, interval_seconds=86400)
    ready = client.patch('/api/v1/jobs/'+ready['id'],json={'notification_channel_ids':[channels[0]]}).json()
    assert CheckService(repository,doctolib,settings).run_due() == [{'successful_targets':1,'failed_targets':0}]
    claim = repository.claim_alert(lease_seconds=60)
    assert claim is not None

    # A live manual continuation owns one completed target and its operation intent.
    continuation = create_job(client)
    continuation = client.patch('/api/v1/jobs/'+continuation['id'],json={'target_urls':[URL,URL+'&source=second']}).json()
    repository.set_status(continuation['id'],'paused')
    intent = repository.request_check(continuation['id'])
    run_id, claimed = repository.claim_due_jobs(limit=1)[0]
    result_id = complete_target(repository,run_id,claimed,0)
    return {'path':Path(settings.database_path),'repository':repository,'settings':settings,
        'doctolib':doctolib,'clock':now,'held':held,'manual':manual,'ready':ready,'claim':claim,
        'continuation':continuation,'intent':intent,'run_id':run_id,'claimed':claimed,'result_id':result_id,
        'email':email}


@pytest.mark.parametrize('key_kind',['correct','missing','wrong'])
def test_complete_configuration_restore_is_offline_and_ciphertext_immutable(recovery,tmp_path,monkeypatch,key_kind):
    item = recovery
    key = item['settings'].notification_secret_key
    archive = tmp_path/'final.zip'
    expected = all_rows(item['path'])
    admin.backup(item['path'],archive,notification_secret_key=key)
    archival_bytes = archive.read_bytes()
    selected_key = key if key_kind=='correct' else '' if key_kind=='missing' else Fernet.generate_key().decode()
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**k:pytest.fail('network during verification'))
    monkeypatch.setattr('app.services.channel_tests.send_configured',lambda *a,**k:pytest.fail('real send'))
    monkeypatch.setattr('app.services.delivery.send_configured',lambda *a,**k:pytest.fail('real send'))
    work = tmp_path/'rehearsal'
    report = admin.verify(archive,work,notification_secret_key=selected_key)
    assert report['restored_schema_version']==SCHEMA_VERSION
    assert report['notification_configuration']['key_state']=={'correct':'available','missing':'missing','wrong':'unreadable'}[key_kind]
    assert report['notification_configuration']['action_required']==(key_kind!='correct')
    assert all_rows(work/'restored.sqlite3')==expected
    assert all_rows(item['path'])==expected
    assert archive.read_bytes()==archival_bytes
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read(admin.MANIFEST_MEMBER))
    configuration = manifest['notification_configuration']
    assert configuration['external_key_fingerprint']==hashlib.sha256(NotificationSecrets(key).key).hexdigest()
    assert configuration['included']=={'notification_channels':2,'smtp_transport':1}
    assert manifest['external_key_included'] is False
    assert key not in json.dumps(manifest) and 'fake-password' not in json.dumps(manifest)
    assert manifest['application_compatibility']['supported_schema_max']==SCHEMA_VERSION

    database = Database(str(work/'restored.sqlite3'))
    repository = Repository(database)
    settings = replace(item['settings'],database_path=database.path,notification_secret_key=selected_key)
    app = create_app(settings,repository=repository,doctolib=item['doctolib'])
    client = TestClient(app)
    channels = client.get('/api/v1/channels').json()['items']
    assert all(channel['usable']==(key_kind=='correct') for channel in channels)
    assert client.get('/api/v1/settings/smtp').json()['usable']==(key_kind=='correct')
    assert repository.get_job(item['held']['id'])['notification_channel_ids']==item['held']['notification_channel_ids']
    assert repository.get_job(item['held']['id'])['status']=='paused'
    assert repository.get_job(item['held']['id'])['check_intent']['id']==item['manual']['id']
    assert repository.get_job(item['continuation']['id'])['check_intent']['id']==item['intent']['id']
    with database.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM create_operations').fetchone()[0]>=3
        assert conn.execute('SELECT COUNT(*) FROM channel_mutations').fetchone()[0]>=2
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
    # Reading settings with absent/wrong keys never rewrites ciphertext.
    assert all_rows(work/'restored.sqlite3')==expected
    if key_kind!='correct':
        telegram = next(channel for channel in channels if channel['type']=='telegram')
        response = client.patch('/api/v1/channels/'+telegram['id'],json={
            'expected_version':telegram['edit_version'],'token_action':'replace',
            'bot_token':'123456:replacement_fixture_secret_for_offline_checks'})
        assert response.status_code==503
        assert all_rows(work/'restored.sqlite3')==expected

    repository.configure_notification_routing(settings)
    sender = FakeNotifier()
    delivery = DeliveryService(repository,settings,sender=sender)
    assert not delivery.run_once()  # Copied live owner cannot be adopted by a new dispatcher.
    assert sender.sent==[]
    item['clock'][0]+=timedelta(seconds=61)
    assert delivery.run_once()==(key_kind=='correct')
    assert len(sender.sent)==int(key_kind=='correct')
    ready_alert = next(alert for alert in repository.alerts() if alert['job_id']==item['ready']['id'])
    assert ready_alert['delivery_state']==('sent' if key_kind=='correct' else 'action_required')
    assert all(alert['quiet_state']=='held' for alert in repository.alerts() if alert['job_id']==item['held']['id'])
    assert repository.get_channel(item['email']['id'],private=True)['email_recipient_ciphertext']==item['repository'].get_channel(item['email']['id'],private=True)['email_recipient_ciphertext']
    if key_kind=='correct':
        configured = channel_settings(settings,repository.get_channel(item['email']['id'],private=True),
                                      repository.get_smtp_transport(private=True))
        assert configured['recipient']=='recipient@example.net'
        assert configured['transport']['sender_email']=='sender@example.com'
        assert configured['transport']['username']=='mailer'
        assert configured['transport']['password']=='fake-password'


def test_restored_live_manual_continuation_waits_for_expiry_then_uses_new_owner(recovery,tmp_path):
    item = recovery
    archive = tmp_path/'continuation.zip'
    admin.backup(item['path'],archive,notification_secret_key=item['settings'].notification_secret_key)
    work = tmp_path/'continuation-restore'
    admin.verify(archive,work,notification_secret_key=item['settings'].notification_secret_key)
    repository = Repository(Database(str(work/'restored.sqlite3')))
    assert repository.claim_due_jobs()==[]
    item['clock'][0]+=timedelta(minutes=11)
    repository.interrupt_stale_runs()
    resumed_id,resumed = repository.claim_due_jobs(limit=1)[0]
    assert resumed_id==item['run_id'] and resumed['intent_id']==item['intent']['id']
    assert resumed['target_cursor']==1 and resumed['generation']==2
    assert resumed['owner_token']!=item['claimed']['owner_token']
    with pytest.raises(LeaseLostError):
        complete_target(repository,resumed_id,item['claimed'],1)
    complete_target(repository,resumed_id,resumed,1)
    assert repository.finish_run(resumed_id,resumed['id'],0,0,300,owner_token=resumed['owner_token'])
    run = repository.checks(resumed['id'])[0]
    assert run['outcome']=='completed' and run['successful_targets']==2
    assert item['result_id'] in {result['id'] for result in run['results']}
    assert repository.get_job(resumed['id'])['status']=='paused'
    assert repository.get_job(resumed['id'])['last_extra_started_at']==item['repository'].get_job(resumed['id'])['last_extra_started_at']

    # Once quiet hours end, copied paused-manual evidence cannot release stale work.
    settings = replace(item['settings'],database_path=repository.database.path)
    repository.configure_notification_routing(settings)
    sender = FakeNotifier()
    delivery = DeliveryService(repository,settings,sender=sender)
    item['clock'][0] = datetime(2026,10,6,6,0,tzinfo=timezone.utc)
    delivery.run_once()  # Unrelated ready job may dispatch through the fake provider.
    delivery.run_once()
    held_alerts = [alert for alert in repository.alerts() if alert['job_id']==item['held']['id']]
    assert all(alert['quiet_state']=='needs_manual_check' for alert in held_alerts)
    assert not any(alert['job_id']==item['held']['id'] for alert in sender.sent)
    assert repository.get_job(item['held']['id'])['status']=='paused'
    repository.request_check(item['held']['id'])
    assert CheckService(repository,item['doctolib'],settings).run_due()==[{'successful_targets':1,'failed_targets':0}]
    assert delivery.run_once()
    assert delivery.run_once()
    assert sum(alert['job_id']==item['held']['id'] for alert in sender.sent)==2
    assert repository.get_job(item['held']['id'])['status']=='paused'
    with repository.database.connection() as conn:
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_schema15_recovery_migrates_only_copy_and_preserves_relationships(recovery,tmp_path):
    item = recovery
    with item['repository'].database.connection() as conn:
        conn.execute('DROP TABLE retention_plans')
        conn.execute('ALTER TABLE availability_events DROP COLUMN sent_destinations')
        conn.execute('UPDATE schema_version SET version=15')
    expected = all_rows(item['path'])
    archive = tmp_path/'schema15.zip'
    admin.backup(item['path'],archive,notification_secret_key=item['settings'].notification_secret_key)
    original = archive.read_bytes()
    work = tmp_path/'schema15-restore'
    report = admin.verify(archive,work,notification_secret_key=item['settings'].notification_secret_key)
    assert report['backup_schema_version']==15 and report['restored_schema_version']==SCHEMA_VERSION
    assert archive.read_bytes()==original and all_rows(item['path'])==expected
    restored = all_rows(work/'restored.sqlite3')
    for table in ('jobs','targets','check_runs','check_results','check_intents','alerts',
                  'job_channels','notification_channels','smtp_transport','target_alert_state',
                  'create_operations','channel_mutations','settings'):
        assert restored[table]==expected[table]
    with sqlite3.connect(work/'restored.sqlite3') as conn:
        assert 'sent_destinations' in {row[1] for row in conn.execute('PRAGMA table_info(availability_events)')}
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert restored['retention_plans']==[]


@pytest.mark.parametrize('missing',['retention_plans','sent_destinations'])
def test_schema16_incomplete_retention_structure_is_rejected(tmp_path,missing):
    source = tmp_path/'incomplete.sqlite3'
    database = Database(str(source))
    database.initialize()
    with database.connection() as conn:
        if missing=='retention_plans':
            conn.execute('DROP TABLE retention_plans')
        else:
            conn.execute('ALTER TABLE availability_events DROP COLUMN sent_destinations')
    with pytest.raises(admin.AdminError,match='incomplete_checker_schema'):
        admin.backup(source,tmp_path/'invalid.zip')
