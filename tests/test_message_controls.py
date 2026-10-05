"""Content inheritance, immutable delivery contracts and prospective routing."""
from app.notifications import DeliveryOutcome, render_notification
from app.services.channel_tests import run_test_once, synthetic_alert
from app.services.checks import CheckService
from test_backend_journey import setup_backend, create_job


def test_content_edits_preserve_search_and_queued_rendering(tmp_path):
    client, repo, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    assert job['message_content'] is None
    assert job['effective_message_content']['preset'] == 'standard'
    CheckService(repo, doctolib, settings).run_due()
    queued = repo.get_pending_alerts()[0]
    original = render_notification('telegram', queued)
    saved = client.patch('/api/v1/jobs/'+job['id'], json={'name':'Renamed', 'message_content':{'preset':'compact','silent':True}})
    assert saved.status_code == 200, saved.text
    result = saved.json()
    assert result['search_revision'] == job['search_revision']
    assert result['content_version'] == 2 and result['policy_version'] == 1
    assert result['check_intent'] is None
    assert result['effective_message_content']['silent']
    queued_after = repo.get_pending_alerts()[0]
    assert render_notification('telegram', queued_after) == original
    assert queued_after['job_name'] == queued['job_name']
    inherited = client.patch('/api/v1/jobs/'+job['id'], json={'message_content':None})
    assert inherited.status_code == 200
    assert inherited.json()['message_content'] is None
    assert inherited.json()['effective_message_content']['preset'] == 'standard'


def test_saved_test_snapshot_and_content_version_conflict(tmp_path):
    client, repo, settings, _ = setup_backend(tmp_path)
    channel = repo.list_channels()['items'][0]
    saved = client.get('/api/v1/settings').json()
    old_preview = client.get('/api/v1/channels/'+channel['id']+'/preview').json()
    request = {'expected_version':channel['edit_version'],'expected_content_version':saved['content_version']}
    url = '/api/v1/channels/'+channel['id']+'/tests'
    test = client.post(url,json=request,headers={'Idempotency-Key':'snapshot-test'})
    assert test.status_code == 202, test.text
    update = client.put('/api/v1/settings',json={'expected_version':saved['edit_version'],'message_content':{'preset':'compact','silent':True}})
    assert update.status_code == 200, update.text
    stale = client.post(url,json=request,headers={'Idempotency-Key':'stale-content'})
    assert stale.status_code == 409 and stale.json()['detail']['code'] == 'content_version_conflict'
    sent = []
    assert run_test_once(repo,settings,sender=lambda configured,alert: sent.append(alert) or DeliveryOutcome('sent'))
    assert render_notification('telegram',sent[0]) == old_preview
    assert client.post(url,json=request,headers={'Idempotency-Key':'snapshot-test'}).json()['id'] == test.json()['id']


def test_preview_rejects_unknown_content_and_unsupported_silent(tmp_path):
    client, _, _, _ = setup_backend(tmp_path)
    for content in ({'preset':'custom','fields':['arbitrary']}, {'preset':'standard','extra':True}, {'preset':'custom','fields':[]}, {'silent':'false'}, {'silent':1}):
        assert client.post('/api/v1/notification-preview',json={'channel_type':'telegram','message_content':content}).status_code == 422
    assert client.post('/api/v1/notification-preview',json={'channel_type':'email','message_content':{'silent':True}}).status_code == 422
    for kind in ('telegram','ntfy','email','webhook'):
        result = client.post('/api/v1/notification-preview',json={'channel_type':kind})
        assert result.status_code == 200
        assert result.json() == render_notification(kind,synthetic_alert())


def test_schema10_upgrade_freezes_existing_delivery_before_edits(tmp_path):
    client, repo, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    CheckService(repo,doctolib,settings).run_due()
    before = render_notification('telegram',repo.get_pending_alerts()[0])
    with repo.database.connection() as conn:
        for table, columns in {'settings':['message_content','content_version'],
                'jobs':['message_content','content_version','policy_version'],
                'alerts':['message_content','event_snapshot','content_version','policy_version'],
                'channel_tests':['message_content','ntfy_priority']}.items():
            for column in columns:
                conn.execute('ALTER TABLE '+table+' DROP COLUMN '+column)
        conn.execute('UPDATE schema_version SET version=10')
    repo.database.initialize()
    repo.update_job(job['id'],{'name':'Changed after migration','message_content':{'preset':'compact'}})
    assert render_notification('telegram',repo.get_pending_alerts()[0]) == before


def test_silent_settings_and_job_reject_email_webhook_selection(tmp_path):
    import pytest
    client, repo, _, _ = setup_backend(tmp_path)
    job = create_job(client)
    webhook, _ = repo.create_channel({'name':'Webhook','type':'webhook','enabled':True,
            'endpoint_ciphertext':'fixture-encrypted-endpoint','destination_identity':'fixture'},'hook-create','hook-create')
    with pytest.raises(ValueError,match='Silent delivery'):
        repo.update_job(job['id'],{'notification_channel_ids':[webhook['id']],
                'message_content':{'preset':'standard','silent':True}})
    unchanged = repo.get_job(job['id'])
    assert unchanged['message_content'] is None and webhook['id'] not in unchanged['notification_channel_ids']
    repo.update_job(job['id'],{'notification_channel_ids':[webhook['id']]})
    saved = client.get('/api/v1/settings').json()
    result = client.put('/api/v1/settings',json={'expected_version':saved['edit_version'],
            'message_content':{'preset':'standard','silent':True}})
    assert result.status_code == 422
    assert client.get('/api/v1/settings').json()['content_version'] == saved['content_version']


def test_schema10_saved_test_replays_legacy_fingerprint(tmp_path):
    from app.api.routes import fingerprint
    client,repo,_,_ = setup_backend(tmp_path)
    channel = repo.list_channels()['items'][0]
    body = {'expected_version':channel['edit_version']}
    created = repo.reserve_channel_test(channel['id'],channel['edit_version'],'legacy-test',
            fingerprint(['test',channel['id'],channel['edit_version']]))
    with repo.database.connection() as conn:
        for table, columns in {'settings':['message_content','content_version'],
                'jobs':['message_content','content_version','policy_version'],
                'alerts':['message_content','event_snapshot','content_version','policy_version'],
                'channel_tests':['message_content','ntfy_priority']}.items():
            for column in columns:
                conn.execute('ALTER TABLE '+table+' DROP COLUMN '+column)
        conn.execute('UPDATE schema_version SET version=10')
    repo.database.initialize()
    replay = client.post('/api/v1/channels/'+channel['id']+'/tests',json=body,
            headers={'Idempotency-Key':'legacy-test'})
    assert replay.status_code == 202, replay.text
    assert replay.json()['id'] == created['id']
    with repo.database.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM channel_tests').fetchone()[0] == 1
