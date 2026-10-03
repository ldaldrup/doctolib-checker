"""Email channels join ordinary event routing and keep SMTP edits fenced."""
import json
from dataclasses import replace

from app.notifications import DeliveryOutcome
from app.services.delivery import DeliveryService
from test_named_channel_delivery import routed


def _smtp(repo, secrets, port=587):
    identity = secrets.identity(json.dumps(
        ['smtp.example.org', port, 'starttls', 'Checker', 'sender@example.org'],
        separators=(',', ':')))
    return repo.update_smtp_transport({
        'enabled': 1,
        'host': 'smtp.example.org',
        'port': port,
        'tls_mode': 'starttls',
        'sender_name': 'Checker',
        'sender_email_ciphertext': secrets.encrypt('sender@example.org'),
        'username_ciphertext': secrets.encrypt('smtp-user'),
        'password_ciphertext': secrets.encrypt('smtp-password'),
        'destination_identity': identity,
    }, expected_version=1)


def _email(repo, secrets, name, recipient):
    return repo.create_channel({
        'type': 'email', 'name': name, 'enabled': True,
        'email_recipient_ciphertext': secrets.encrypt(recipient),
        'destination_identity': secrets.identity('email:' + recipient),
    }, 'create-' + name, name)[0]


def test_two_email_recipients_and_telegram_deliver_independently(routed, monkeypatch):
    _, repo, settings, secrets, job, telegram, _, observe = routed
    _smtp(repo, secrets)
    first = _email(repo, secrets, 'Email1', 'one@example.org')
    second = _email(repo, secrets, 'Email2', 'two@example.org')
    repo.update_job(job['id'], {'notification_channel_ids': [telegram['id'], first['id'], second['id']]})
    observe()

    sent = []

    def email_sender(_settings, configured, alert, before_send):
        assert before_send(remaining_seconds=15)
        sent.append(('email', configured['recipient'], alert['event_id']))
        return DeliveryOutcome('sent')

    def telegram_sender(configured, alert, before_send):
        assert before_send(remaining_seconds=15)
        sent.append(('telegram', configured.telegram_chat_id, alert['event_id']))
        return DeliveryOutcome('sent')

    monkeypatch.setattr('app.services.email_delivery.send_email_alert', email_sender)
    monkeypatch.setattr('app.services.channel_tests.send_telegram_alert', telegram_sender)
    delivery = DeliveryService(repo, settings)

    assert delivery.run_once()
    assert delivery.run_once()
    assert delivery.run_once()

    assert {(kind, recipient) for kind, recipient, _ in sent} == {
        ('email', 'one@example.org'), ('email', 'two@example.org'), ('telegram', '1')}
    assert len({event_id for _, _, event_id in sent}) == 1
    assert all(alert['status'] == 'sent' and alert['attempt_count'] == 1 for alert in repo.alerts())


def test_smtp_identity_change_fences_claim_before_data_permission(routed):
    _, repo, _, secrets, job, _, _, observe = routed
    _smtp(repo, secrets)
    email = _email(repo, secrets, 'Email1', 'one@example.org')
    repo.update_job(job['id'], {'notification_channel_ids': [email['id']]})
    observe()
    claimed = repo.claim_alert()
    assert claimed and claimed['channel'] == 'email'

    current = repo.get_smtp_transport(private=True)
    repo.update_smtp_transport({
        'host': 'replacement.example.org',
        'destination_identity': secrets.identity('replacement-smtp-destination'),
    }, expected_version=current['edit_version'])

    assert repo.begin_alert_attempt(claimed['id'], claimed['owner_token']) is None
    alert = next(item for item in repo.alerts() if item['id'] == claimed['id'])
    assert alert['status'] == 'cancelled'
    assert alert['error_summary'] == 'destination_changed'


def test_email_events_are_not_routed_when_runtime_allowlist_no_longer_allows_smtp(routed):
    _, repo, settings, secrets, job, _, _, observe = routed
    allowed = replace(settings,webhook_allowlist=(('smtp.example.org',2525,'10.0.0.8'),))
    repo.configure_notification_routing(allowed)
    _smtp(repo,secrets,port=2525)
    email = _email(repo,secrets,'Email1','one@example.org')
    repo.update_job(job['id'],{'notification_channel_ids':[email['id']]})
    repo.configure_notification_routing(settings)

    observe()

    assert repo.alerts() == []
    with repo.database.connection() as conn:
        event = conn.execute('SELECT routing_snapshot FROM availability_events').fetchone()
    assert json.loads(event['routing_snapshot']) == []


def test_email_events_are_not_routed_when_runtime_encryption_key_is_unavailable(routed):
    _, repo, settings, secrets, job, _, _, observe = routed
    _smtp(repo,secrets)
    email = _email(repo,secrets,'Email1','one@example.org')
    repo.update_job(job['id'],{'notification_channel_ids':[email['id']]})
    repo.configure_notification_routing(replace(settings,notification_secret_key=''))

    observe()

    assert repo.alerts() == []
    with repo.database.connection() as conn:
        event = conn.execute('SELECT routing_snapshot FROM availability_events').fetchone()
    assert json.loads(event['routing_snapshot']) == []
