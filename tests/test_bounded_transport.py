import threading
import time
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from curl_cffi import requests as curl_requests

from app.doctolib import DoctolibClient, TargetBudgetExceeded
from app.settings import Settings
from app.storage.db import Database
from app.storage.repositories import LeaseLostError, Repository
from tests.test_doctolib import BOOKING_URL, FakeSession, Response, _test_meta


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_budget_settings_reject_nonfinite_nonpositive_values(value):
    with pytest.raises(ValueError):
        Settings(target_budget_seconds=value)
    with pytest.raises(ValueError):
        Settings(slice_budget_seconds=value)


def test_slice_allows_full_target_and_environment_overrides(monkeypatch):
    with pytest.raises(ValueError, match="full target"):
        Settings(target_budget_seconds=200)
    monkeypatch.setenv("TARGET_BUDGET_SECONDS", "90")
    monkeypatch.setenv("SLICE_BUDGET_SECONDS", "150")
    settings = Settings.from_env()
    assert (settings.target_budget_seconds, settings.slice_budget_seconds) == (90, 150)


@pytest.mark.parametrize("response", [Response({}, 302, {"Location": "/next"}), Response({}, 503)])
def test_redirects_and_retry_waits_consume_same_deadline(monkeypatch, response):
    clock = [0.0]
    monkeypatch.setattr("app.doctolib.time_module.monotonic", lambda: clock[0])
    monkeypatch.setattr("app.doctolib.time_module.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    session = FakeSession([response, Response({})])

    def gate():
        clock[0] += 0.4

    client = DoctolibClient(session=session, before_request=gate)
    client.deadline = 0.6
    with pytest.raises(TargetBudgetExceeded):
        client._get("https://www.doctolib.de/test")
    assert len(session.calls) == 1
    assert session.calls[0][1]["timeout"] == pytest.approx(0.2)


def test_lease_loss_during_gate_stops_outbound_request():
    session = FakeSession([Response({})])
    owned = [True]
    client = DoctolibClient(session=session, before_request=lambda: owned.__setitem__(0, False))

    def guard():
        if not owned[0]:
            raise LeaseLostError("Lost ownership")

    client.check_permission = guard
    with pytest.raises(LeaseLostError):
        client._get("https://www.doctolib.de/test")
    assert session.calls == []


def test_paginated_deadline_discards_previous_positive_page(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("app.doctolib.time_module.monotonic", lambda: clock[0])
    payload = {"total": 1, "availabilities": [{"date": "2026-10-01", "slots": ["2026-10-01T10:00:00+02:00"]}]}
    session = FakeSession([payload, {"total": 0, "availabilities": []}])
    original_get = session.get

    def get(url, **kwargs):
        response = original_get(url, **kwargs)
        clock[0] += 0.4
        return response

    session.get = get
    client = DoctolibClient(session=session, page_days=1)
    client.deadline = 0.6
    with pytest.raises(TargetBudgetExceeded):
        client.check(BOOKING_URL, {"date_mode": "custom", "earliest_date": "2026-10-01",
                                 "latest_date": "2026-10-02", "time_zone": "Europe/Berlin"},
                     meta=_test_meta(), now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert len(session.calls) == 2


def test_gate_rejects_unaffordable_slot_without_moving_other_reservations(tmp_path):
    database = Database(str(tmp_path / "gate.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.reserve_request_turn(3)
    with database.connection() as conn:
        before = conn.execute("SELECT next_allowed_at FROM request_gate").fetchone()[0]
    started = time.monotonic()
    with pytest.raises(TargetBudgetExceeded):
        repository.reserve_request_turn(3, deadline=started + 0.1)
    assert time.monotonic() - started < 1
    with database.connection() as conn:
        assert conn.execute("SELECT next_allowed_at FROM request_gate").fetchone()[0] == before


def test_gate_database_lock_wait_is_bounded(tmp_path):
    database = Database(str(tmp_path / "busy-gate.sqlite3"))
    database.initialize()
    repository = Repository(database)
    with database.connection() as locked:
        locked.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(TargetBudgetExceeded):
            repository.reserve_request_turn(3, deadline=started + 0.1)
        assert time.monotonic() - started < 1


def test_gate_busy_before_deadline_is_not_misclassified_as_budget(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "busy-error.sqlite3"))
    database.initialize()
    original_connection = database.connection

    @contextmanager
    def connection():
        with original_connection() as conn:
            class BusyConnection:
                def execute(self, sql):
                    if sql == "BEGIN IMMEDIATE":
                        error = sqlite3.OperationalError("database is locked")
                        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                        raise error
                    return conn.execute(sql)
            yield BusyConnection()

    monkeypatch.setattr(database, "connection", connection)
    with pytest.raises(sqlite3.OperationalError):
        Repository(database).reserve_request_turn(3, deadline=time.monotonic() + 120)


def test_actual_curl_dripping_response_obeys_total_wall_deadline():
    class Drip(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with curl_requests.Session(impersonate="safari2601") as session:
            client = DoctolibClient(availability_session=session)
            started = time.monotonic()
            client.deadline = started + 0.2
            with pytest.raises(TargetBudgetExceeded):
                client._get(f"http://127.0.0.1:{server.server_port}/", availability=True)
            elapsed = time.monotonic() - started
            assert 0.15 <= elapsed < 1.0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
