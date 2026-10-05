"""Encrypted SMTP settings and named recipient API contracts."""
from dataclasses import replace
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.notification_secrets import NotificationSecrets
from app.settings import Settings
from app.storage.db import Database
from app.storage.repositories import Repository


def setup(tmp_path):
    database = Database(str(tmp_path / 'email.sqlite3'))
    database.initialize()
    repository = Repository(database)
    settings = Settings(database_path=database.path,
        notification_secret_key=Fernet.generate_key().decode())
    return TestClient(create_app(settings, repository=repository)), repository, settings


def smtp_body(version=1, **changes):
    body = {'expected_version':version,'enabled':True,'host':'smtp.example.com','port':587,
        'tls_mode':'starttls','sender_name':'Checker','sender_email_action':'replace',
        'sender_email':'sender@example.com','username_action':'replace','username':'mailer',
        'password_action':'replace','password':'smtp-fixture-password'}
    body.update(changes)
    return body


def preview_smtp(client, body):
    response = client.post('/api/v1/settings/smtp/impact',json=body)
    assert response.status_code == 200, response.text
    return response.json()


def save_smtp(client, body, impact=None):
    impact = impact or preview_smtp(client,body)
    return client.put('/api/v1/settings/smtp',json={**body,'expected_impact_token':impact['impact_token']})


def configure_smtp(client):
    response = save_smtp(client,smtp_body())
    assert response.status_code == 200, response.text
    return response.json()


def make_email(client, recipient='recipient@example.net'):
    return client.post('/api/v1/channels',headers={'Idempotency-Key':'email-create'},
        json={'type':'email','name':'Email1','enabled':True,'recipient':recipient})


def test_smtp_transport_masks_values_and_supports_keep_replace_clear(tmp_path):
    client, repository, settings = setup(tmp_path)
    initial = client.get('/api/v1/settings/smtp').json()
    assert not initial['enabled'] and initial['edit_version'] == 1
    assert 'sender_email' not in initial and 'username' not in initial and 'password' not in initial

    saved = configure_smtp(client)
    assert saved['usable'] and saved['host'] == 'smtp.example.com'
    assert saved['sender_email_set'] and saved['username_set'] and saved['password_set']
    assert 'sender@example.com' not in str(saved) and 'mailer' not in str(saved)
    raw = repository.get_smtp_transport(private=True)
    secrets = NotificationSecrets(settings.notification_secret_key)
    assert secrets.decrypt(raw['sender_email_ciphertext']) == 'sender@example.com'
    assert secrets.decrypt(raw['username_ciphertext']) == 'mailer'
    assert secrets.decrypt(raw['password_ciphertext']) == 'smtp-fixture-password'
    assert settings.notification_secret_key.encode() not in open(settings.database_path,'rb').read()

    saved = save_smtp(client,smtp_body(2,
        sender_email_action='keep',sender_email=None,username_action='replace',
        username='mailer-2',password_action='keep',password=None)).json()
    assert saved['credential_version'] == 3 and saved['destination_version'] == 2
    assert secrets.decrypt(repository.get_smtp_transport(private=True)['password_ciphertext']) == 'smtp-fixture-password'
    cleared = save_smtp(client,smtp_body(3,enabled=False,
        sender_email_action='clear',sender_email=None,username_action='clear',username=None,
        password_action='clear',password=None)).json()
    assert not cleared['sender_email_set'] and not cleared['username_set'] and not cleared['password_set']
    assert not cleared['usable']
    assert client.put('/api/v1/settings/smtp',json=smtp_body(3)).status_code == 409


def test_email_recipient_crud_never_returns_address(tmp_path):
    client, repository, settings = setup(tmp_path)
    configure_smtp(client)
    response = make_email(client)
    assert response.status_code == 201, response.text
    channel = response.json()
    assert channel['type'] == 'email' and channel['usable'] and channel['recipient_set']
    assert 'recipient@example.net' not in response.text
    raw = repository.get_channel(channel['id'],private=True)
    assert raw['email_recipient_ciphertext'] != 'recipient@example.net'
    assert NotificationSecrets(settings.notification_secret_key).decrypt(raw['email_recipient_ciphertext']) == 'recipient@example.net'

    saved = client.patch('/api/v1/channels/'+channel['id'],json={
        'expected_version':1,'recipient_action':'replace','recipient':'next@example.org'}).json()
    assert saved['destination_version'] == 2 and 'next@example.org' not in str(saved)
    invalid = client.patch('/api/v1/channels/'+channel['id'],json={
        'expected_version':2,'recipient_action':'replace','recipient':'next@example.org\r\nBcc:third@example.org'})
    assert invalid.status_code == 422 and 'third@example.org' not in invalid.text
    cleared = client.patch('/api/v1/channels/'+channel['id'],json={
        'expected_version':2,'recipient_action':'clear'}).json()
    assert not cleared['recipient_set'] and not cleared['usable']


def test_smtp_identity_change_cancels_unsent_email_and_saved_test(tmp_path):
    client, repository, _ = setup(tmp_path)
    configured = configure_smtp(client)
    channel = make_email(client).json()
    test = client.post('/api/v1/channels/'+channel['id']+'/tests',
        headers={'Idempotency-Key':'email-test'},json={'expected_version':1})
    assert test.status_code == 202, test.text
    with repository.database.connection() as conn:
        conn.execute("""INSERT INTO alerts(id,channel,event_type,dedupe_key,status,created_at,
            channel_config_id,destination_version,credential_version,channel_name)
            VALUES('pending-email','email','slot_found','email-dedupe','pending','2030-01-01T00:00:00+00:00',?,?,?,?)""",
            (channel['id'],channel['destination_version'],channel['credential_version'],channel['name']))
    impact = preview_smtp(client,smtp_body(configured['edit_version'],
        sender_email_action='replace',sender_email='new-sender@example.com',
        username_action='keep',username=None,password_action='keep',password=None))
    changed = save_smtp(client,smtp_body(configured['edit_version'],
        sender_email_action='replace',sender_email='new-sender@example.com',
        username_action='keep',username=None,password_action='keep',password=None),impact)
    assert changed.status_code == 200, changed.text
    assert changed.json()['destination_version'] == configured['destination_version'] + 1
    assert repository.get_channel_test(test.json()['id'])['status'] == 'cancelled'
    with repository.database.connection() as conn:
        row = conn.execute("SELECT status,error_summary FROM alerts WHERE id='pending-email'").fetchone()
    assert tuple(row) == ('cancelled','destination_changed')


def test_smtp_impact_reports_only_pending_deliveries_for_real_identity_changes(tmp_path):
    client, repository, _ = setup(tmp_path)
    configured = configure_smtp(client)
    channel = make_email(client).json()
    job = repository.create_job({
        'name':'SMTP impact route','interval_seconds':300,'date_mode':'first_available','horizon_days':15,
        'time_zone':'UTC','insurance_sector':'public','telehealth':False,'telegram_enabled':False,
        'notification_channel_ids':[channel['id']],
    },[{'booking_url':'https://www.doctolib.de/test/booking/availabilities?placeId=1&motiveIds%5B%5D=2',
        'country':'de','profile_slug':'test','practice_id':'1','motive_id':'2','agenda_ids_str':'3',
        'practice_name':'Practice','practitioner_name':'Doctor'}])
    changed_body = smtp_body(configured['edit_version'],host='smtp-new.example.com',
        sender_email_action='keep',sender_email=None,username_action='keep',username=None,
        password_action='keep',password=None)
    empty_impact = preview_smtp(client,changed_body)
    assert empty_impact['pending_email_deliveries'] == []
    with repository.database.connection() as conn:
        conn.execute("""INSERT INTO alerts(id,job_id,channel,event_type,dedupe_key,status,created_at,
            channel_config_id,destination_version,credential_version,channel_name)
            VALUES('pending-email',?,'email','slot_found','smtp-impact-pending','pending',?,?,?,?,?)""",
            (job['id'],'2030-01-01T00:00:00+00:00',channel['id'],channel['destination_version'],
             channel['credential_version'],channel['name']))

    same_identity = client.post('/api/v1/settings/smtp/impact',json=smtp_body(configured['edit_version'],
        sender_email='sender@example.com',username_action='keep',username=None,
        password_action='keep',password=None))
    assert same_identity.status_code == 200, same_identity.text
    assert same_identity.json()['pending_email_deliveries'] == []
    changed = client.post('/api/v1/settings/smtp/impact',json=changed_body)
    assert changed.status_code == 200, changed.text
    impact = changed.json()
    assert impact['pending_email_deliveries'] == [{'job_id':job['id'],'job_name':'SMTP impact route','count':1}]
    assert 'sender@example.com' not in changed.text and 'smtp-fixture-password' not in changed.text
    assert repository.get_smtp_transport()['edit_version'] == configured['edit_version']
    stale = client.put('/api/v1/settings/smtp',json={**changed_body,'expected_impact_token':empty_impact['impact_token']})
    assert stale.status_code == 409 and stale.json()['detail']['code'] == 'smtp_impact_changed'
    assert stale.json()['detail']['pending_email_deliveries'] == impact['pending_email_deliveries']
    with repository.database.connection() as conn:
        assert conn.execute("SELECT status FROM alerts WHERE id='pending-email'").fetchone()[0] == 'pending'
    saved = save_smtp(client,changed_body,impact)
    assert saved.status_code == 200, saved.text
    with repository.database.connection() as conn:
        assert conn.execute("SELECT status FROM alerts WHERE id='pending-email'").fetchone()[0] == 'cancelled'


def test_smtp_rejects_plaintext_and_header_injection(tmp_path):
    client, _, _ = setup(tmp_path)
    assert client.put('/api/v1/settings/smtp',json=smtp_body(
        tls_mode='implicit_tls')).status_code == 422
    assert client.put('/api/v1/settings/smtp',json=smtp_body(
        sender_name='Checker\r\nBcc: victim@example.net')).status_code == 422
    assert client.post('/api/v1/channels',headers={'Idempotency-Key':'email-bad-name'},json={
        'type':'email','name':'Email\r\nBcc: victim@example.net','recipient':'recipient@example.net'}).status_code == 422


def test_missing_key_can_disable_saved_smtp_without_exposing_addresses(tmp_path):
    client, repository, settings = setup(tmp_path)
    configure_smtp(client)
    channel = make_email(client).json()
    no_key = TestClient(create_app(Settings(database_path=settings.database_path),repository=repository))
    body = smtp_body(2,enabled=False,sender_email_action='keep',sender_email=None,
        username_action='keep',username=None,password_action='keep',password=None)
    response = no_key.put('/api/v1/settings/smtp',json=body)
    assert response.status_code == 200, response.text
    assert not response.json()['usable'] and not response.json()['enabled']
    assert 'sender@example.com' not in response.text and 'mailer' not in response.text
    assert not no_key.get('/api/v1/channels/'+channel['id']).json()['usable']


def test_removed_port_allowlist_still_allows_disabling_saved_smtp(tmp_path):
    client, repository, settings = setup(tmp_path)
    client.app.state.settings = replace(settings,
        webhook_allowlist=(('smtp.private.example',2525,'10.0.0.8'),))
    saved = save_smtp(client,smtp_body(
        host='smtp.private.example',port=2525))
    assert saved.status_code == 200, saved.text
    client.app.state.settings = settings
    assert not client.get('/api/v1/settings/smtp').json()['usable']
    disabled = client.put('/api/v1/settings/smtp',json=smtp_body(saved.json()['edit_version'],
        host='smtp.private.example',port=2525,enabled=False,
        sender_email_action='keep',sender_email=None,username_action='keep',username=None,
        password_action='keep',password=None))
    assert disabled.status_code == 200, disabled.text
    assert not disabled.json()['enabled'] and not disabled.json()['usable']
