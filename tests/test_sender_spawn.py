"""Exercise the production spawn boundary with the real child and an offline session."""

import multiprocessing
import sqlite3

from app import notifications
from test_delivery import pending


def _offline_spawn_child(connection, settings, alert):
    # Spawn imports this function in a clean interpreter. Install the fake
    # inside that interpreter, then execute the actual production child.
    class Response:
        status_code = 200
        headers = {}

        def iter_content(self, **kwargs):
            yield b'{"ok":true}'

        def close(self):
            pass

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, endpoint, **kwargs):
            with sqlite3.connect(alert['_fixture_db_path']) as conn:
                count, started = conn.execute('SELECT attempt_count,attempt_started_at FROM alerts').fetchone()
            assert count == 1 and started is not None
            return Response()

    notifications.requests.Session = Session
    notifications._telegram_child(connection, settings, alert)


def test_production_spawn_prepares_and_persists_before_offline_post(tmp_path, monkeypatch):
    _client, repository, settings, _doctolib, _job = pending(tmp_path)
    claimed = repository.claim_alert()
    before = {child.pid for child in multiprocessing.active_children()}
    # The top-level test helper is pickleable; production still selects spawn.
    monkeypatch.setattr(notifications, '_telegram_child', _offline_spawn_child)
    # The helper must call the unpatched child in the clean spawned interpreter.
    outcome = notifications.send_telegram_alert(
        settings, {**claimed, '_fixture_db_path': repository.database.path},
        before_send=lambda remaining_seconds: repository.begin_alert_attempt(
            claimed['id'], claimed['owner_token'], wait_seconds=remaining_seconds) is not None)
    assert outcome.category == 'sent' and outcome.attempted
    assert repository.finish_delivery(claimed['id'], claimed['owner_token'], outcome.category)
    assert repository.alerts()[0]['status'] == 'sent'
    assert {child.pid for child in multiprocessing.active_children()} == before
