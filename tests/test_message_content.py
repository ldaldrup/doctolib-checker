"""Structured content uses the same safe renderer in previews and adapters."""
from email import policy
from email.parser import BytesParser

import pytest

from app.notifications import CONTENT_FIELDS, _telegram_payload, normalize_content, render_notification
from app.services.email_delivery import email_message, email_preview
from app.settings import Settings
from app.webhooks import webhook_payload


ALERT = {'event_id': 'event-one', 'job_name': '<Job>\r\nBcc: ignored',
    'practitioner_name': '<Dr Ada>', 'practice_name': 'A & B',
    'earliest_slot': '2030-01-02T10:00:00Z', 'checked_at': '2030-01-01T12:00:00Z',
    'time_zone': 'Europe/Berlin', 'booking_url': 'https://www.doctolib.de/?a=1&b=2', 'slot_count': 2}


@pytest.mark.parametrize('invalid', [{'template': 'bad'}, {'preset': 'unknown'}, {'silent': 'yes'},
    {'preset': 'custom', 'fields': []}, {'preset': 'custom', 'fields': ['unknown']},
    {'preset': 'custom', 'fields': ['practice', 'practice']}, {'fields': [[]]}])
def test_only_allowlisted_structured_options(invalid):
    with pytest.raises(ValueError, match='invalid_message_content'):
        normalize_content(invalid)


def test_adapter_payloads_match_the_pure_preview():
    alert = ALERT | {'message_content': {'preset': 'custom', 'fields': list(CONTENT_FIELDS), 'silent': False}}
    telegram = render_notification('telegram', alert)
    payload = _telegram_payload(Settings(telegram_chat_id='fixture'), alert)
    assert payload['text'] == telegram['text'] and payload['disable_notification'] is False
    assert '&lt;Job&gt;' in payload['text'] and '2030-01-02 11:00 CET' in payload['text']
    assert '2030-01-01 13:00 CET' in payload['text'] and '\r' not in payload['text']
    ntfy = render_notification('ntfy', alert)
    payload = webhook_payload({'type': 'ntfy', 'endpoint': 'https://example.org/topic'}, alert)
    assert payload['message'] == ntfy['message'] and payload['click'] == ntfy['click']
    email = render_notification('email', alert)
    assert email_preview(alert) == {'html': email['html'], 'text': email['text']}
    parsed = BytesParser(policy=policy.default).parsebytes(email_message(
        {'sender_email': 'sender@example.org'}, {'recipient': 'to@example.org'}, alert))
    assert parsed.get_body(preferencelist=('plain',)).get_content().replace('\r\n', '\n').rstrip() == email['text']
    assert parsed.get_body(preferencelist=('html',)).get_content().replace('\r\n', '\n').rstrip() == email['html']
    assert parsed['Subject'] == email['subject']


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'https://user:pass@example.org/',
    'https://example.org/\r\nInjected: yes', 'https://example.org/' + 'x'*2000])
def test_unsafe_links_are_omitted(url):
    alert = ALERT | {'booking_url': url}
    for channel in ('telegram', 'ntfy', 'email'):
        preview = render_notification(channel, alert)
        assert url not in str(preview) and preview['warnings']


def test_bounds_whole_escaped_entities_missing_values_and_timezone_fallback():
    alert = {key: '&'*70000 for key in ('job_name', 'practitioner_name', 'practice_name')}
    alert.update(time_zone='Invalid/Zone', earliest_slot='not-a-date', checked_at='2030-01-01T12:00:00')
    options = {'preset': 'custom', 'fields': list(CONTENT_FIELDS)}
    rendered = render_notification('telegram', alert, options)
    assert len(rendered['text']) < 4096 and 'UTC' in rendered['text'] and 'Unavailable' in rendered['text']
    assert rendered['text'].count('&amp;') > 0 and rendered['warnings']
    assert render_notification('email', {}, options)['subject'] == 'Matching appointment available'


def test_silent_supported_channels_and_fixed_webhook_schema():
    silent = ALERT | {'message_content': {'preset': 'compact', 'silent': True}}
    assert _telegram_payload(Settings(), silent)['disable_notification'] is True
    assert webhook_payload({'type': 'ntfy', 'endpoint': 'https://example.org/topic', 'ntfy_priority': 5}, silent)['priority'] == 1
    for channel in ('email', 'webhook'):
        with pytest.raises(ValueError, match='unsupported_silent_option'):
            render_notification(channel, silent)
    old = webhook_payload({'type': 'webhook'}, ALERT)
    new = webhook_payload({'type': 'webhook'}, ALERT | {'message_content': {'preset': 'custom', 'fields': ['job_name']}})
    assert old == new and new['schema_version'] == 1


def test_ntfy_standard_formats_the_slot_in_its_labeled_timezone():
    preview = render_notification('ntfy', ALERT)
    assert 'Earliest: 2030-01-02 11:00 CET (Europe/Berlin)' in preview['message']
    assert '2030-01-02T10:00:00Z' not in preview['message']
    assert webhook_payload({'type': 'ntfy', 'endpoint': 'https://example.org/topic'}, ALERT)['message'] == preview['message']


def test_ntfy_retry_preserves_saved_priority_and_silent_overrides_it():
    channel = {'type': 'ntfy', 'endpoint': 'https://example.org/topic', 'ntfy_priority': 5}
    alert = ALERT | {'ntfy_priority': 2}
    assert webhook_payload(channel, alert)['priority'] == render_notification('ntfy', alert)['priority'] == 2
    assert webhook_payload(channel, ALERT)['priority'] == 5
    assert webhook_payload(channel, alert | {'message_content': {'silent': True}})['priority'] == 1
