"""Raw channel mutation, privacy and one-shot test contracts."""
from dataclasses import replace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.notification_secrets import NotificationSecrets
from app.notifications import DeliveryOutcome
from app.services.channel_tests import run_test_once
from app.settings import Settings
from app.storage.db import Database
from app.storage.repositories import Repository

TOKEN='123456:fixture_secret_for_offline_checks_123'
CHAT='-100123456789'


@pytest.fixture
def setup(tmp_path):
    db=Database(str(tmp_path/'checker.db'));db.initialize()
    repo=Repository(db)
    settings=Settings(database_path=db.path,notification_secret_key=Fernet.generate_key().decode(),telegram_bot_token=TOKEN,telegram_chat_id=CHAT,telegram_enabled=True)
    return TestClient(create_app(settings,repository=repo)),repo,settings


def create(client,key='create-1',**changes):
    body={'name':'Telegram1','enabled':True,'bot_token':TOKEN,'chat_id':CHAT}
    body.update(changes)
    return client.post('/api/v1/channels',json=body,headers={'Idempotency-Key':key})


def test_channel_crud_ciphertext_masking_cas_and_identity(setup):
    client,repo,settings=setup
    response=create(client); assert response.status_code==201,response.text
    channel=response.json(); cid=channel['id']
    assert channel['usable'] and channel['credential_configured']
    assert TOKEN not in response.text and CHAT not in response.text
    raw=repo.get_channel(cid,private=True)
    assert raw['token_ciphertext']!=TOKEN and raw['chat_ciphertext']!=CHAT
    assert NotificationSecrets(settings.notification_secret_key).decrypt(raw['token_ciphertext'])==TOKEN
    assert create(client).json()['id']==cid
    assert create(client,name='Changed').status_code==409
    saved=client.patch('/api/v1/channels/'+cid,json={'expected_version':1,'name':'Renamed','token_action':'keep','chat_action':'keep'}).json()
    assert saved['edit_version']==2 and saved['destination_version']==1
    assert client.patch('/api/v1/channels/'+cid,json={'expected_version':1,'name':'stale'}).status_code==409
    repaired=client.patch('/api/v1/channels/'+cid,json={'expected_version':2,'token_action':'replace','bot_token':'123456:replacement_secret_for_offline_checks_456'}).json()
    assert repaired['credential_version']==2 and repaired['destination_version']==1
    moved=client.patch('/api/v1/channels/'+cid,json={'expected_version':3,'chat_action':'replace','chat_id':'-100987654321'}).json()
    assert moved['destination_version']==2
    cleared=client.patch('/api/v1/channels/'+cid,json={'expected_version':4,'token_action':'clear'}).json()
    assert not cleared['usable'] and not cleared['bot_token_set']
    assert client.request('DELETE','/api/v1/channels/'+cid,json={'expected_version':5}).status_code==422
    assert client.request('DELETE','/api/v1/channels/'+cid,json={'expected_version':5,'confirmed':True}).status_code==200
    assert client.get('/api/v1/channels').json()['total']==0


def test_keys_unavailable_wrong_key_no_secret_echo(setup):
    client,repo,settings=setup
    cid=create(client).json()['id']
    missing=TestClient(create_app(replace(settings,notification_secret_key=''),repository=repo))
    assert not missing.get('/api/v1/channels/'+cid).json()['usable']
    assert create(missing,key='missing').status_code==503
    assert missing.patch('/api/v1/channels/'+cid,json={'expected_version':1,'name':'Allowed'}).status_code==200
    wrong=TestClient(create_app(replace(settings,notification_secret_key=Fernet.generate_key().decode()),repository=repo))
    assert not wrong.get('/api/v1/channels/'+cid).json()['usable']
    result=wrong.patch('/api/v1/channels/'+cid,json={'expected_version':2,'token_action':'replace','bot_token':TOKEN})
    assert result.status_code==503
    invalid=create(client,key='invalid',bot_token='private-invalid-token')
    assert invalid.status_code==422 and 'private-invalid-token' not in invalid.text
    extra=create(client,key='extra',unknown=TOKEN)
    assert extra.status_code==422 and TOKEN not in extra.text
    with pytest.raises(ValueError,match='Fernet key'):
        create_app(replace(settings,notification_secret_key='broken'),repository=repo)


def test_import_idempotent_no_overwrite_and_preview(setup):
    client,repo,settings=setup
    assert client.post('/api/v1/channels/import-legacy',json={'unsupported':TOKEN},headers={'Idempotency-Key':'reject'}).status_code==422
    first=client.post('/api/v1/channels/import-legacy',headers={'Idempotency-Key':'import1'})
    assert first.status_code==201
    cid=first.json()['id']
    client.patch('/api/v1/channels/'+cid,json={'expected_version':1,'name':'My name'})
    replay=client.post('/api/v1/channels/import-legacy',headers={'Idempotency-Key':'import2'})
    assert replay.status_code==200 and replay.json()['name']=='My name'
    assert client.get('/api/v1/channels').json()['total']==1
    preview=client.get('/api/v1/channels/'+cid+'/preview').json()['html']
    assert '&lt;Practitioner&gt;' in preview and 'Example &amp; Practice' in preview
    assert TOKEN not in preview and CHAT not in preview


def test_test_replay_one_attempt_unknown_never_resends(setup):
    client,repo,settings=setup
    cid=create(client).json()['id']
    url='/api/v1/channels/'+cid+'/tests'
    headers={'Idempotency-Key':'test1'}
    first=client.post(url,json={'expected_version':1},headers=headers)
    assert first.status_code==202
    test=first.json()
    assert client.post(url,json={'expected_version':1},headers=headers).json()['id']==test['id']
    sent=[]
    def sender(config,alert):
        sent.append(config.telegram_chat_id)
        return DeliveryOutcome('uncertain','telegram_acknowledgement_timeout')
    assert run_test_once(repo,settings,sender)
    assert not run_test_once(repo,settings,sender)
    result=client.get('/api/v1/channel-tests/'+test['id']).json()
    assert result['status']=='unknown' and result['attempt_count']==1 and sent==[CHAT]
    assert client.post(url,json={'expected_version':1},headers=headers).json()['status']=='unknown'
    assert client.post(url,json={'expected_version':2},headers=headers).status_code==409
    reloaded = TestClient(create_app(settings, repository=repo))
    latest = reloaded.get('/api/v1/channels').json()['items'][0]['latest_test']
    assert latest == result and latest['status'] == 'unknown'
    assert 'owner_token' not in latest and TOKEN not in str(latest) and CHAT not in str(latest)


def test_test_claim_expiry_fences_late_result(setup):
    client,repo,settings=setup
    cid=create(client).json()['id']
    test=client.post('/api/v1/channels/'+cid+'/tests',json={'expected_version':1},headers={'Idempotency-Key':'test-expiry'}).json()
    owned=repo.claim_channel_test()
    assert repo.begin_channel_test_attempt(test['id'],owned['owner_token'])
    with repo.database.connection() as conn:
        conn.execute("UPDATE channel_tests SET claim_until='2000-01-01T00:00:00+00:00' WHERE id=?",(test['id'],))
    assert not repo.finish_channel_test(test['id'],owned['owner_token'],DeliveryOutcome('sent'))
    # Listing saved configuration after reload exposes the reconciled outcome.
    assert client.get('/api/v1/channels').json()['items'][0]['latest_test']['status'] == 'unknown'
    repo.reconcile_channel_tests()
    assert repo.get_channel_test(test['id'])['status']=='unknown'
    assert repo.claim_channel_test() is None


def test_test_disable_before_attempt_cancels(setup):
    client,repo,settings=setup
    cid=create(client).json()['id']
    test=client.post('/api/v1/channels/'+cid+'/tests',json={'expected_version':1},headers={'Idempotency-Key':'test-cancel'}).json()
    owned=repo.claim_channel_test()
    client.patch('/api/v1/channels/'+cid,json={'expected_version':1,'enabled':False})
    assert not repo.begin_channel_test_attempt(test['id'],owned['owner_token'])
    assert repo.get_channel_test(test['id'])['status']=='cancelled'
    assert client.post('/api/v1/channels/'+cid+'/tests',json={'expected_version':2},headers={'Idempotency-Key':'disabled'}).status_code==409


def test_import_opt_in_mapping_and_delete_detaches_versions(setup):
    client,repo,settings=setup
    values={'name':'Opted in','interval_seconds':300,'date_mode':'first_available','horizon_days':15,
            'time_zone':'UTC','insurance_sector':'public','telehealth':False,'telegram_enabled':True}
    opted=repo.create_job(values,[])
    opted_out=repo.create_job(dict(values,name='Opted out',telegram_enabled=False),[])
    imported=client.post('/api/v1/channels/import-legacy',json={},headers={'Idempotency-Key':'map'}).json()
    cid=imported['id']
    assert repo.get_job(opted['id'])['notification_channel_ids']==[cid]
    assert repo.get_job(opted_out['id'])['notification_channel_ids']==[]
    version=repo.get_job(opted['id'])['edit_version']
    assert client.request('DELETE','/api/v1/channels/'+cid,json={'expected_version':1,'confirmed':True}).status_code==200
    job=repo.get_job(opted['id'])
    assert job['notification_channel_ids']==[] and job['edit_version']==version+1
    assert repo.update_job(job['id'],{'name':'Still editable','notification_channel_ids':[]},expected_version=job['edit_version'])['name']=='Still editable'


def test_expired_create_key_evaluates_local_credentials_after_prune(setup):
    client,repo,settings=setup
    first=create(client).json()
    with repo.database.connection() as conn:
        conn.execute("UPDATE channel_mutations SET expires_at='2000-01-01T00:00:00+00:00'")
    called=[]
    def values():
        called.append(True)
        raw=repo.get_channel(first['id'],private=True)
        return {key:raw[key] for key in ('name','enabled','token_ciphertext','chat_ciphertext','destination_identity')}
    channel,created=repo.create_channel(values,'create-1','fresh')
    assert called==[True] and created and channel['id']!=first['id']


def test_expired_test_key_rechecks_key_and_sanitizes_sender_error(setup):
    client,repo,settings=setup
    cid=create(client).json()['id']
    url='/api/v1/channels/'+cid+'/tests'
    test=client.post(url,json={'expected_version':1},headers={'Idempotency-Key':'expire'}).json()
    assert run_test_once(repo,settings,lambda *_:DeliveryOutcome('uncertain','https://secret.example/private-token'))
    assert repo.get_channel_test(test['id'])['error_code']=='invalid_sender_error'
    with repo.database.connection() as conn:
        conn.execute("UPDATE channel_tests SET expires_at='2000-01-01T00:00:00+00:00'")
    missing=TestClient(create_app(replace(settings,notification_secret_key=''),repository=repo))
    assert missing.post(url,json={'expected_version':1},headers={'Idempotency-Key':'expire'}).status_code==409
    assert repo.get_channel_test(test['id']) is not None  # Failed transaction preserves old evidence.
