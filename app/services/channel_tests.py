"""One-shot, durably owned test sends, separate from availability events."""
from dataclasses import replace
import json

from app.notification_secrets import NotificationSecrets, SecretUnavailable
from app.notifications import DeliveryOutcome, send_telegram_alert


def synthetic_alert():
    return {'practitioner_name':'Example <Practitioner>', 'practice_name':'Example & Practice',
            'earliest_slot':'2030-01-02T10:00:00+00:00','time_zone':'UTC',
            'booking_url':'https://www.doctolib.de/','slot_count':1,
            'event_id':'synthetic-preview','job_id':'example-job','job_name':'Example job',
            'target_id':'example-target','triggered_by':'test','search_revision':1,
            'checked_at':'2030-01-02T09:00:00+00:00'}


def channel_settings(settings, channel, smtp_transport=None):
    secrets = NotificationSecrets(settings.notification_secret_key)
    if channel['type'] == 'email':
        if (not smtp_transport or not smtp_transport['enabled'] or
                not smtp_transport['configured']):
            raise SecretUnavailable('smtp_transport_unavailable')
        return {'type':'email', 'id':channel['id'],
            'recipient':secrets.decrypt(channel['email_recipient_ciphertext']),
            'transport':{
                'host':smtp_transport['host'], 'port':smtp_transport['port'],
                'tls_mode':smtp_transport['tls_mode'], 'sender_name':smtp_transport['sender_name'] or '',
                'sender_email':secrets.decrypt(smtp_transport['sender_email_ciphertext']),
                'username':secrets.decrypt(smtp_transport['username_ciphertext']) if smtp_transport['username_ciphertext'] else '',
                'password':secrets.decrypt(smtp_transport['password_ciphertext']) if smtp_transport['password_ciphertext'] else '',
            }}
    if channel['type'] != 'telegram':
        configured = {key:channel[key] for key in ('type','auth_type','ntfy_priority')}
        configured['endpoint'] = secrets.decrypt(channel['endpoint_ciphertext'])
        for field in ('auth_token','auth_username','auth_password'):
            configured[field] = (secrets.decrypt(channel[field+'_ciphertext'])
                                 if channel.get(field+'_ciphertext') else '')
        return configured
    return replace(settings,telegram_enabled=True,
        telegram_bot_token=secrets.decrypt(channel['token_ciphertext']),
        telegram_chat_id=secrets.decrypt(channel['chat_ciphertext']))


def send_configured(settings, configured, alert, before_send):
    if isinstance(configured, dict):
        if configured['type'] == 'email':
            from app.services.email_delivery import send_email_alert
            return send_email_alert(settings, configured, alert, before_send=before_send)
        from app.webhooks import send_webhook_alert
        return send_webhook_alert(settings, configured, alert, before_send=before_send)
    return send_telegram_alert(configured, alert, before_send=before_send)


def notification_preview(channel, content=None):
    from app.notifications import render_notification
    alert = synthetic_alert()
    if channel['type'] == 'ntfy':
        alert['ntfy_priority'] = channel.get('ntfy_priority', 3)
    return render_notification(channel['type'], alert, content)


def run_test_once(repository,settings,sender=None):
    repository.reconcile_channel_tests()
    test = repository.claim_channel_test()
    if test is None:
        return False
    started = False
    def before_send(remaining_seconds=None):
        nonlocal started
        started = repository.begin_channel_test_attempt(test['id'],test['owner_token'],remaining_seconds)
        return started
    try:
        channel = repository.get_channel(test['channel_config_id'],private=True)
        if (channel['destination_version'] != test['destination_version'] or
                channel['credential_version'] != test['credential_version']):
            repository.begin_channel_test_attempt(test['id'],test['owner_token'])
            return True
        smtp_transport = (repository.get_smtp_transport(private=True)
                          if channel['type'] == 'email' else None)
        configured = channel_settings(settings,channel,smtp_transport)
        alert = {**synthetic_alert(), 'event_id':test['id'], 'message_content':json.loads(test['message_content']) if test.get('message_content') else None}
        if test.get('ntfy_priority') is not None:
            alert['ntfy_priority'] = test['ntfy_priority']
        if sender is None:
            outcome = send_configured(settings,configured,alert,before_send)
        elif before_send():
            outcome = sender(configured,alert)
        else:
            return True
        if not isinstance(outcome,DeliveryOutcome):
            outcome = DeliveryOutcome('uncertain' if started else 'action_required','invalid_sender_outcome',attempted=started)
    except SecretUnavailable as exc:
        outcome = DeliveryOutcome('action_required',str(exc),attempted=False)
    except Exception:
        outcome = DeliveryOutcome('uncertain' if started else 'action_required','notification_test_error',attempted=started)
    repository.finish_channel_test(test['id'],test['owner_token'],outcome)
    return True
