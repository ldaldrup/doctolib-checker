"""Mixed routing and saved tests share the same guarded endpoint transport."""
import json
import socket

from app.notifications import DeliveryOutcome
from app.services.delivery import DeliveryService
from app.webhooks import send_webhook_alert
from test_named_channel_delivery import routed


def test_mixed_destinations_keep_independent_attempts_and_event_identity(routed, monkeypatch):
    db,repo,settings,secrets,job,telegram,_,observe = routed
    channels = []
    for kind,name in [('ntfy','topic_one'),('ntfy','topic_two'),('webhook','hook')]:
        channels.append(repo.create_channel(dict(type=kind,name=name,enabled=True,
            endpoint_ciphertext=secrets.encrypt('https://receiver.example/'+name),
            destination_identity=name),name,name)[0])
    repo.update_job(job['id'],{'notification_channel_ids':[telegram['id']]+[c['id'] for c in channels]})
    observe()
    sent = []
    attempts = []
    class Connection:
        def __init__(self,*args): pass
        def connect(self): pass
        def request(self,method,path,body,headers):
            self.body = json.loads(body)
            sent.append((path,self.body))
        def getresponse(self):
            self.status = 200
            if 'topic' not in self.body:
                attempts.append(1)
                self.status = 503 if len(attempts)==1 else 202
            return self
        def read(self,size):
            return json.dumps({'id':'remote','event':'message','topic':self.body.get('topic')}).encode()
        def getheader(self,name): return None
        def close(self): pass
    def webhook(configured,channel,alert,before_send):
        return send_webhook_alert(configured,channel,alert,before_send,
            resolver=lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('1.1.1.1',443))],
            connection_factory=Connection)
    def tg(configured,alert,before_send):
        assert before_send(remaining_seconds=15)
        sent.append(('telegram',alert))
        return DeliveryOutcome('sent')
    monkeypatch.setattr('app.webhooks.send_webhook_alert',webhook)
    monkeypatch.setattr('app.services.channel_tests.send_telegram_alert',tg)
    service = DeliveryService(repo,settings)
    for _ in range(4): assert service.run_once()
    rows = repo.alerts()
    assert len(rows)==4 and len({r['event_id'] for r in rows})==1
    assert all('booking_url' not in row for row in rows)
    assert sorted(r['status'] for r in rows)==['failed','sent','sent','sent']
    event = rows[0]['event_id']
    generic = next(body for path,body in sent if path=='/hook')
    assert generic['event_id']==event and generic['trigger']=='schedule'
    assert generic['job']['name']=='Job' and generic['target']['practice_name']=='Practice'
    assert {body['topic'] for path,body in sent if path=='/'}=={'topic_one','topic_two'}
    assert all(event in body['message'] for path,body in sent if path=='/')
    with db.connection() as conn:
        conn.execute("UPDATE alerts SET next_attempt_at=created_at WHERE status='failed'")
    assert service.run_once()
    assert len(sent)==5 and sent[-1][1]==generic
    assert sorted(r['attempt_count'] for r in repo.alerts())==[1,1,1,2]


def test_saved_test_and_event_reject_same_private_destination_without_send(routed, monkeypatch):
    _,repo,settings,secrets,job,_,_,observe = routed
    channel = repo.create_channel(dict(type='webhook',name='private',enabled=True,
        endpoint_ciphertext=secrets.encrypt('https://receiver.example/secret'),
        destination_identity='private'),'private','private')[0]
    repo.update_job(job['id'],{'notification_channel_ids':[channel['id']]})
    observe()
    test = repo.reserve_channel_test(channel['id'],channel['edit_version'],'test','test')
    def webhook(configured,channel,alert,before_send):
        return send_webhook_alert(configured,channel,alert,before_send,
            resolver=lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))],
            connection_factory=lambda *a: (_ for _ in ()).throw(AssertionError('private connection')))
    monkeypatch.setattr('app.webhooks.send_webhook_alert',webhook)
    service = DeliveryService(repo,settings)
    assert service.run_once()  # one-shot test was processed without a POST
    assert not service.run_once()  # availability policy failure did not consume an attempt
    saved = repo.get_channel_test(test['id'])
    alert = repo.alerts()[0]
    assert saved['status']=='failed' and saved['attempt_count']==0
    assert alert['delivery_state']=='action_required' and alert['attempt_count']==0
    assert saved['error_code']==alert['error_summary']=='webhook_address_blocked'


def test_ntfy_priority_is_saved_for_retries_and_saved_tests(routed):
    from app.services.channel_tests import run_test_once
    from app.webhooks import webhook_payload
    db,repo,settings,secrets,job,_,_,observe = routed
    channel,_ = repo.create_channel(dict(type='ntfy',name='Topic',enabled=True,
        endpoint_ciphertext=secrets.encrypt('https://receiver.example/topic'),
        destination_identity='topic',ntfy_priority=3),'priority','priority')
    repo.update_job(job['id'],{'notification_channel_ids':[channel['id']]})
    observe()
    priorities = []
    def sender(configured,alert):
        priorities.append(webhook_payload(configured,alert)['priority'])
        return DeliveryOutcome('retry','fixture_connect_error') if len(priorities)==1 else DeliveryOutcome('sent')
    dispatcher = DeliveryService(repo,settings,sender)
    assert dispatcher.run_once()
    repo.reserve_channel_test(channel['id'],channel['edit_version'],'priority-test','priority-test')
    repo.update_channel(channel['id'],{'ntfy_priority':5},channel['edit_version'])
    with db.connection() as conn:
        conn.execute("UPDATE alerts SET next_attempt_at=created_at WHERE status='failed'")
    assert run_test_once(repo,settings,sender)
    assert dispatcher.run_once()
    assert priorities == [3,3,3]
    assert repo.get_channel(channel['id'])['ntfy_priority'] == 5


def test_schema10_migration_freezes_ntfy_priority_and_content(routed):
    from app.services.channel_tests import run_test_once
    from app.webhooks import webhook_payload
    db,repo,settings,secrets,job,_,_,observe = routed
    channel,_ = repo.create_channel(dict(type='ntfy',name='Topic',enabled=True,
        endpoint_ciphertext=secrets.encrypt('https://receiver.example/topic'),
        destination_identity='migration-topic',ntfy_priority=4),'migration-priority','migration-priority')
    repo.update_job(job['id'],{'notification_channel_ids':[channel['id']]})
    observe()
    repo.reserve_channel_test(channel['id'],channel['edit_version'],'migration-test','migration-test')
    with db.connection() as conn:
        for table, columns in {'settings':['message_content','content_version'],
                'jobs':['message_content','content_version','policy_version'],
                'alerts':['message_content','event_snapshot','content_version','policy_version'],
                'channel_tests':['message_content','ntfy_priority']}.items():
            for column in columns:
                conn.execute('ALTER TABLE '+table+' DROP COLUMN '+column)
        conn.execute('UPDATE schema_version SET version=10')
    db.initialize()
    repo.update_channel(channel['id'],{'ntfy_priority':5},channel['edit_version'])
    repo.update_settings({'message_content':{'preset':'compact','silent':True}},300)
    snapshots = []
    def sender(configured,alert):
        snapshots.append((webhook_payload(configured,alert)['priority'],alert['message_content']))
        return DeliveryOutcome('sent')
    assert run_test_once(repo,settings,sender)
    assert DeliveryService(repo,settings,sender).run_once()
    assert [priority for priority,_ in snapshots] == [4,4]
    assert all(content['preset']=='standard' and not content['silent'] for _,content in snapshots)
