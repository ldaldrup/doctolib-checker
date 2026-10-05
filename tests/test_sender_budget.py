"""Real offline subprocess lifecycle tests for the send deadline."""

import multiprocessing
import time

import pytest

from app import notifications
from app.settings import Settings


def _hang_without_network(connection, settings, alert):
    connection.send("ready")
    assert connection.recv() is True
    while True:
        time.sleep(0.01)


def test_hard_deadline_terminates_attempt_child(monkeypatch):
    # Fork starts our offline replacement directly, exercising the real parent
    # deadline/cleanup path without any provider connection or credential.
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("requires offline fork transport replacement")
    context = multiprocessing.get_context("fork")
    monkeypatch.setattr(multiprocessing, "get_context", lambda method: context)
    monkeypatch.setattr(notifications, "_telegram_child", _hang_without_network)
    monkeypatch.setattr(notifications, "SEND_BUDGET_SECONDS", 0.15)
    before = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()
    outcome = notifications.send_telegram_alert(
        Settings(telegram_enabled=True, telegram_bot_token="synthetic", telegram_chat_id="synthetic"), {})
    elapsed = time.monotonic() - started
    assert outcome.category == "uncertain"
    assert outcome.error_code == "telegram_attempt_deadline"
    assert 0.1 <= elapsed < 2
    assert {child.pid for child in multiprocessing.active_children()} == before


def test_missing_configuration_never_spawns_transport(monkeypatch):
    monkeypatch.setattr(multiprocessing, "get_context", lambda method: pytest.fail("transport spawned"))
    result = notifications.send_telegram_alert(Settings(), {})
    assert result.category == "action_required"
    assert result.error_code == "telegram_not_configured"


class _HangingSession:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def post(self, *args, **kwargs):
        while True:
            time.sleep(0.01)


def test_child_own_watchdog_ends_transport_without_parent_deadline(monkeypatch):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("requires offline fork transport replacement")
    context = multiprocessing.get_context("fork")
    monkeypatch.setattr(notifications.requests, "Session", _HangingSession)
    monkeypatch.setattr(notifications, "SEND_BUDGET_SECONDS", 0.15)
    receiving, sending = context.Pipe(duplex=True)
    settings = Settings(telegram_enabled=True, telegram_bot_token="synthetic", telegram_chat_id="synthetic")
    process = context.Process(target=notifications._telegram_child, args=(sending, settings, {}))
    try:
        process.start()
        sending.close()
        assert receiving.recv() == "ready"
        receiving.send(True)
        # There is no send_telegram_alert parent watchdog in this test.
        process.join(timeout=2)
        assert not process.is_alive()
        assert process.exitcode == 70
    finally:
        receiving.close()
        sending.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
        process.close()


@pytest.mark.parametrize('identifier', ['http://synthetic.invalid/private', '12', 'synthetic\\nvalue'])
def test_recovery_cli_rejects_non_uuid_before_database_open(monkeypatch, identifier):
    from app import dispatcher
    monkeypatch.setattr(dispatcher, 'create_delivery_service', lambda *args: pytest.fail('database opened'))
    with pytest.raises(SystemExit) as error:
        dispatcher.main(['--recover', identifier])
    assert error.value.code == 2


class _SpawnFailure:
    pid = None

    def start(self):
        raise OSError('synthetic startup failure')


class _SpawnFailureContext:
    def __init__(self, context):
        self.context = context

    def Pipe(self, **kwargs):
        return self.context.Pipe(**kwargs)

    def Process(self, **kwargs):
        return _SpawnFailure()


def test_default_dispatch_spawn_failure_never_counts_attempt(monkeypatch, tmp_path):
    from test_delivery import pending
    from app.services.delivery import DeliveryService
    _client, repository, settings, _doctolib, _job = pending(tmp_path)
    context = _SpawnFailureContext(multiprocessing.get_context('spawn'))
    monkeypatch.setattr(multiprocessing, 'get_context', lambda method: context)
    assert not DeliveryService(repository, settings).run_once()
    alert = repository.alerts()[0]
    assert alert['attempt_count'] == 0
    assert alert['delivery_epoch_attempts'] == 0
    assert alert['delivery_state'] == 'retry'
    assert alert['claim_until'] is None


def test_malformed_optional_event_value_has_bounded_fallback_before_permission():
    permitted = []
    settings = Settings(telegram_enabled=True, telegram_bot_token='synthetic', telegram_chat_id='synthetic')
    alert = {'slot_count': 'bad', 'practitioner_name': '<' * 70000}
    payload = notifications._telegram_payload(settings, alert)
    assert payload['text'].startswith('<b>1 matching appointment slot(s)</b>')
    assert len(payload['text']) < 4096 and '&lt;' in payload['text']
    def deny(**kwargs):
        permitted.append(kwargs['remaining_seconds'])
        return False
    outcome = notifications.send_telegram_alert(settings, alert, session=object(), before_send=deny)
    assert outcome.category == 'retry' and outcome.error_code == 'alert_no_longer_eligible'
    assert not outcome.attempted and permitted == [notifications.SEND_BUDGET_SECONDS]


def test_invalid_content_does_not_request_network_permission():
    permitted = []
    settings = Settings(telegram_enabled=True, telegram_bot_token='synthetic', telegram_chat_id='synthetic')
    outcome = notifications.send_telegram_alert(settings, {'message_content': {'template': 'unsupported'}},
        session=object(), before_send=lambda **kwargs: permitted.append(True))
    assert outcome.category == 'action_required' and not outcome.attempted and not permitted


def test_default_dispatch_child_format_failure_never_counts_attempt(monkeypatch, tmp_path):
    from test_delivery import pending
    from app.services.delivery import DeliveryService
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip('requires offline fork transport replacement')
    _client, repository, settings, _doctolib, _job = pending(tmp_path)
    context = multiprocessing.get_context('fork')
    monkeypatch.setattr(multiprocessing, 'get_context', lambda method: context)
    def invalid_payload(*args):
        raise ValueError('synthetic invalid payload')
    monkeypatch.setattr(notifications, '_telegram_payload', invalid_payload)
    assert not DeliveryService(repository, settings).run_once()
    alert = repository.alerts()[0]
    assert alert['attempt_count'] == 0
    assert alert['delivery_epoch_attempts'] == 0
    assert alert['delivery_state'] == 'action_required'
    assert alert['claim_until'] is None


class _OfflineResponse:
    status_code = 200
    headers = {}

    def iter_content(self, **kwargs):
        yield b'{"ok":true}'

    def close(self):
        pass


_POST_EVENT = None
_SEND_DATABASE_PATH = None


class _RecordingSession(_HangingSession):
    def post(self, *args, **kwargs):
        import sqlite3
        with sqlite3.connect(_SEND_DATABASE_PATH) as conn:
            count, started = conn.execute('SELECT attempt_count,attempt_started_at FROM alerts').fetchone()
        assert count == 1 and started is not None
        _POST_EVENT.set()
        return _OfflineResponse()


@pytest.mark.parametrize('pause_before_permission', [False, True])
def test_child_ready_permission_fences_and_persists_before_post(monkeypatch, tmp_path, pause_before_permission):
    global _POST_EVENT, _SEND_DATABASE_PATH
    from test_delivery import pending
    from app.services.delivery import DeliveryService
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip('requires offline fork transport replacement')
    _client, repository, settings, _doctolib, job = pending(tmp_path)
    context = multiprocessing.get_context('fork')
    _POST_EVENT = context.Event()
    _SEND_DATABASE_PATH = repository.database.path
    monkeypatch.setattr(multiprocessing, 'get_context', lambda method: context)
    monkeypatch.setattr(notifications.requests, 'Session', _RecordingSession)
    begin = repository.begin_alert_attempt
    if pause_before_permission:
        def pause_then_begin(*args, **kwargs):
            repository.set_status(job['id'], 'paused')
            return begin(*args, **kwargs)
        monkeypatch.setattr(repository, 'begin_alert_attempt', pause_then_begin)
    assert DeliveryService(repository, settings).run_once() is not pause_before_permission
    alert = repository.alerts()[0]
    assert alert['attempt_count'] == int(not pause_before_permission)
    assert _POST_EVENT.is_set() is not pause_before_permission
    assert alert['status'] == ('cancelled' if pause_before_permission else 'sent')


def test_expired_budget_after_persisted_permission_never_starts_post(monkeypatch, tmp_path):
    global _POST_EVENT, _SEND_DATABASE_PATH
    from test_delivery import pending
    from app.services.delivery import DeliveryService
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip('requires offline fork transport replacement')
    _client, repository, settings, _doctolib, _job = pending(tmp_path)
    context = multiprocessing.get_context('fork')
    _POST_EVENT = context.Event()
    _SEND_DATABASE_PATH = repository.database.path
    monkeypatch.setattr(multiprocessing, 'get_context', lambda method: context)
    monkeypatch.setattr(notifications.requests, 'Session', _RecordingSession)
    real_monotonic = time.monotonic
    offset = [0]
    monkeypatch.setattr(time, 'monotonic', lambda: real_monotonic() + offset[0])
    original_begin = repository.begin_alert_attempt
    waits = []
    def delayed_begin(*args, **kwargs):
        waits.append(kwargs['wait_seconds'])
        result = original_begin(*args, **kwargs)
        offset[0] = 20  # Deterministically exhaust the network budget in the guard.
        return result
    monkeypatch.setattr(repository, 'begin_alert_attempt', delayed_begin)
    assert DeliveryService(repository, settings).run_once()
    assert waits and 0 < waits[0] <= notifications.SEND_BUDGET_SECONDS
    alert = repository.alerts()[0]
    assert alert['attempt_count'] == 1
    assert alert['delivery_state'] == 'uncertain'
    assert alert['error_summary'] == 'telegram_attempt_deadline'
    assert not _POST_EVENT.is_set()
