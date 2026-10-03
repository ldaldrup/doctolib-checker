"""One-shot, durably owned test sends, separate from availability events."""
from dataclasses import replace

from app.notification_secrets import NotificationSecrets, SecretUnavailable
from app.notifications import DeliveryOutcome, send_telegram_alert


def synthetic_alert():
    return {'practitioner_name':'Example <Practitioner>', 'practice_name':'Example & Practice',
            'earliest_slot':'2030-01-02T10:00:00+00:00','time_zone':'UTC',
            'booking_url':'https://www.doctolib.de/','slot_count':1}


def channel_settings(settings, channel):
    secrets = NotificationSecrets(settings.notification_secret_key)
    return replace(settings,telegram_enabled=True,
        telegram_bot_token=secrets.decrypt(channel['token_ciphertext']),
        telegram_chat_id=secrets.decrypt(channel['chat_ciphertext']))


def run_test_once(repository,settings,sender=send_telegram_alert):
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
        configured = channel_settings(settings,channel)
        if sender is send_telegram_alert:
            outcome = sender(configured,synthetic_alert(),before_send=before_send)
        elif before_send():
            outcome = sender(configured,synthetic_alert())
        else:
            return True
        if not isinstance(outcome,DeliveryOutcome):
            outcome = DeliveryOutcome('uncertain' if started else 'action_required','invalid_sender_outcome',attempted=started)
    except SecretUnavailable as exc:
        outcome = DeliveryOutcome('action_required',str(exc),attempted=False)
    except Exception:
        outcome = DeliveryOutcome('uncertain' if started else 'action_required','telegram_test_error',attempted=started)
    repository.finish_channel_test(test['id'],test['owner_token'],outcome)
    return True
