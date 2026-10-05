"""Encrypted HTTP destination mutations, test fencing and schema recovery."""
from pathlib import Path
from dataclasses import replace

import pytest
from cryptography.fernet import Fernet

from app import admin
from app.notification_secrets import NotificationSecrets
from app.storage.db import Database
from app.storage.repositories import Repository
from test_channel_api import setup


@pytest.mark.parametrize('kind',['ntfy','webhook'])
def test_endpoint_auth_privacy_versions_and_pending_test_fence(setup,kind):
    client,repo,settings = setup
    endpoint = 'https://notify.example/private_topic'
    body = {'type':kind,'name':'Office','endpoint':endpoint,'auth_type':'bearer','auth_action':'replace','auth_token':'private_bearer'}
    response = client.post('/api/v1/channels',json=body,headers={'Idempotency-Key':'http-create'})
    assert response.status_code == 201,response.text
    channel = response.json(); cid = channel['id']
    assert channel['usable'] and channel['endpoint_host'] == 'notify.example'
    assert endpoint not in response.text and 'private_bearer' not in response.text
    raw = repo.get_channel(cid,private=True)
    secrets = NotificationSecrets(settings.notification_secret_key)
    assert secrets.decrypt(raw['endpoint_ciphertext']) == endpoint
    assert secrets.decrypt(raw['auth_token_ciphertext']) == 'private_bearer'
    assert client.post('/api/v1/channels',json=body,headers={'Idempotency-Key':'http-create'}).json()['id'] == cid
    for bad_key in ('',Fernet.generate_key().decode()):
        client.app.state.notification_secrets = NotificationSecrets(bad_key)
        assert not client.get(f'/api/v1/channels/{cid}').json()['usable']
        assert client.post('/api/v1/channels',json=body,headers={'Idempotency-Key':'http-create'}).json()['id'] == cid
        assert client.post(f'/api/v1/channels/{cid}/tests',json={'expected_version':1},headers={'Idempotency-Key':'bad-key-test'}).status_code == 409
        assert repo.get_channel(cid,private=True)['endpoint_ciphertext'] == raw['endpoint_ciphertext']
    client.app.state.notification_secrets = secrets
    test = client.post(f'/api/v1/channels/{cid}/tests',json={'expected_version':1},headers={'Idempotency-Key':'http-test'}).json()
    claimed = repo.claim_channel_test()
    replacement = client.patch(f'/api/v1/channels/{cid}',json={'expected_version':1,'auth_action':'replace','auth_type':'basic','auth_username':'alice','auth_password':'private_password'}).json()
    assert replacement['destination_version'] == 1 and replacement['credential_version'] == 2
    assert not repo.begin_channel_test_attempt(test['id'],claimed['owner_token'])
    assert repo.get_channel_test(test['id'])['status'] == 'cancelled'
    moved = client.patch(f'/api/v1/channels/{cid}',json={'expected_version':2,'endpoint_action':'replace','endpoint':'https://notify.example/other_topic'}).json()
    assert moved['destination_version'] == 2
    assert client.patch(f'/api/v1/channels/{cid}',json={'expected_version':3,'auth_type':'none'}).status_code == 422
    cleared = client.patch(f'/api/v1/channels/{cid}',json={'expected_version':3,'auth_action':'clear','auth_type':'none'}).json()
    assert cleared['usable'] and not cleared['auth_configured'] and cleared['credential_version'] == 4
    incomplete = client.patch(f'/api/v1/channels/{cid}',json={'expected_version':4,'endpoint_action':'clear'}).json()
    assert not incomplete['usable'] and not incomplete['endpoint_set']
    assert client.post(f'/api/v1/channels/{cid}/tests',json={'expected_version':5},headers={'Idempotency-Key':'unusable-test'}).status_code == 409


@pytest.mark.parametrize('changes',[
    {'endpoint':'http://notify.example/private'},
    {'endpoint':'https://user:password@notify.example/private'},
    {'auth_type':'bearer','auth_action':'replace','auth_token':'bad\r\nheader'},
    {'auth_type':'bearer','auth_action':'replace','auth_token':'contains space'},
    {'auth_type':'basic','auth_action':'replace','auth_username':'a:b','auth_password':'secret'},
    {'auth_type':'none','auth_action':'replace','auth_token':'ignored-secret'},
    {'endpoint_action':'clear','endpoint':'https://notify.example/private'},
])
def test_invalid_http_config_has_safe_errors(setup,changes):
    client,_,_ = setup
    body = {'type':'webhook','name':'HTTP','endpoint':'https://notify.example/private'} | changes
    response = client.post('/api/v1/channels',json=body,headers={'Idempotency-Key':'invalid-http'})
    assert response.status_code == 422
    assert 'notify.example/private' not in response.text and 'secret' not in response.text


def test_ntfy_priority_keeps_claimed_test_snapshot_and_telegram_rejects_http_options(setup):
    client,repo,_ = setup
    channel = client.post('/api/v1/channels',json={'type':'ntfy','name':'Priority','endpoint':'https://notify.example/topic'},headers={'Idempotency-Key':'priority'}).json()
    cid = channel['id']
    test = client.post(f'/api/v1/channels/{cid}/tests',json={'expected_version':1},headers={'Idempotency-Key':'priority-test'}).json()
    claimed = repo.claim_channel_test()
    updated = client.patch(f'/api/v1/channels/{cid}',json={'expected_version':1,'ntfy_priority':5}).json()
    assert updated['credential_version'] == 1 and updated['destination_version'] == 1
    assert claimed['ntfy_priority'] == 3
    assert repo.begin_channel_test_attempt(test['id'],claimed['owner_token'])
    body = {'name':'Telegram','bot_token':'123456:fixture_secret_for_offline_checks_123','chat_id':'-100123456789','ntfy_priority':5}
    assert client.post('/api/v1/channels',json=body,headers={'Idempotency-Key':'wrong-options'}).status_code == 422


def test_schema8_backup_migrates_http_columns_without_changing_telegram(setup,tmp_path):
    client,repo,settings = setup
    channel = client.post('/api/v1/channels',json={'name':'Telegram','bot_token':'123456:fixture_secret_for_offline_checks_123','chat_id':'-100123456789'},headers={'Idempotency-Key':'telegram'}).json()
    original = repo.get_channel(channel['id'],private=True)
    with repo.database.connection() as conn:
        for column in ('endpoint_ciphertext','auth_type','auth_token_ciphertext','auth_username_ciphertext','auth_password_ciphertext','ntfy_priority'):
            conn.execute('ALTER TABLE notification_channels DROP COLUMN '+column)
        conn.execute('UPDATE schema_version SET version=8')
    archive = tmp_path/'schema8.zip'
    assert admin.backup(Path(settings.database_path),archive)['schema_version'] == 8
    work = tmp_path/'restored'
    assert admin.verify(archive,work)['restored_schema_version'] == 13
    restored = Repository(Database(str(work/'restored.sqlite3'))).get_channel(channel['id'],private=True)
    assert restored == original


def test_removed_operator_port_exception_has_consistent_channel_usability(setup):
    client,repo,settings = setup
    client.app.state.settings = replace(settings,webhook_allowlist=(('notify.example',8443,'10.0.0.2'),))
    channel = client.post('/api/v1/channels',json={'type':'webhook','name':'Private',
        'endpoint':'https://notify.example:8443/topic'},headers={'Idempotency-Key':'private-port'}).json()
    job = repo.create_job(dict(name='Job',interval_seconds=300,date_mode='first_available',
        horizon_days=15,time_zone='UTC',insurance_sector='public',telehealth=False,
        telegram_enabled=True,notification_channel_ids=[channel['id']]),[])
    assert client.get('/api/v1/jobs/'+job['id']).json()['notification_channels'][0]['usable']
    client.app.state.settings = settings
    assert not client.get('/api/v1/channels/'+channel['id']).json()['usable']
    assert not client.get('/api/v1/jobs/'+job['id']).json()['notification_channels'][0]['usable']
