import json

import pytest
import requests

from app.notifications import format_slot_alert, send_telegram_alert
from app.settings import Settings


def test_slot_alert_escapes_booking_data_and_formats_configured_timezone():
    message = format_slot_alert({
        "practitioner_name": "<Dr. Ada>",
        "practice_name": "Praxis & Partner",
        "earliest_slot": "2026-10-15T07:30:00+00:00",
        "booking_url": "https://www.doctolib.de/booking?a=1&b=2",
        "slot_count": 3,
        "time_zone": "Europe/Berlin",
    })

    assert "&lt;Dr. Ada&gt;" in message
    assert "Praxis &amp; Partner" in message
    assert "2026-10-15 09:30 CEST" in message
    assert "a=1&amp;b=2" in message


@pytest.mark.parametrize('status,body,headers,category,code,retry_after', [
    (200, {'ok': True}, {}, 'sent', None, None),
    (401, {'ok': False, 'description': 'private-provider-content', 'error_code': 401}, {}, 'action_required', 'telegram_rejected_401', None),
    (403, {'ok': False, 'error_code': 403}, {}, 'action_required', 'telegram_rejected_403', None),
    (429, {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 37}}, {}, 'retry', 'telegram_rate_limited', 37),
    (503, {'ok': False, 'error_code': 503}, {'Retry-After': '2000'}, 'retry', 'telegram_temporary_rejection', 900),
    (200, {'ok': False, 'description': 'private-provider-content'}, {}, 'uncertain', 'telegram_unconfirmed_response', None),
    (302, {}, {'Location': 'https://private-provider-content.example'}, 'uncertain', 'telegram_unconfirmed_response', None),
])
def test_sender_makes_one_bounded_attempt_and_classifies_without_provider_data(status, body, headers, category, code, retry_after):
    calls = []

    class Response:
        status_code = status
        closed = False

        def iter_content(self, chunk_size):
            yield json.dumps(body).encode()

        def close(self):
            self.closed = True

    response = Response()
    response.headers = headers

    class Session:
        def post(self, endpoint, **kwargs):
            calls.append(kwargs)
            return response

    outcome = send_telegram_alert(Settings(telegram_enabled=True, telegram_bot_token='fixture-secret', telegram_chat_id='fixture-chat'), {}, session=Session())
    assert len(calls) == 1
    assert calls[0]['timeout'] == (3, 7)
    assert calls[0]['stream'] and calls[0]['allow_redirects'] is False
    assert response.closed
    assert outcome.category == category and outcome.error_code == code
    assert outcome.retry_after == retry_after
    assert 'private-provider-content' not in repr(outcome) and 'fixture-secret' not in repr(outcome)


@pytest.mark.parametrize('error,category,code', [
    (requests.exceptions.ConnectTimeout('https://private-token.example'), 'retry', 'telegram_connect_timeout'),
    (requests.exceptions.ReadTimeout('https://private-token.example'), 'uncertain', 'telegram_acknowledgement_timeout'),
    (requests.exceptions.ConnectionError('lost acknowledgement private-token'), 'uncertain', 'telegram_connection_lost'),
    (RuntimeError('private-token'), 'uncertain', 'telegram_delivery_error'),
])
def test_sender_distinguishes_presend_timeout_from_ambiguous_acceptance(error, category, code):
    calls = []

    class Session:
        def post(self, endpoint, **kwargs):
            calls.append(1)
            raise error

    outcome = send_telegram_alert(Settings(telegram_enabled=True, telegram_bot_token='fixture-secret', telegram_chat_id='fixture-chat'), {}, session=Session())
    assert len(calls) == 1
    assert outcome.category == category and outcome.error_code == code
    assert 'private-token' not in repr(outcome)
