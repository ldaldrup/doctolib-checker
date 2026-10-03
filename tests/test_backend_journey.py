import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.doctolib import DoctolibClient
from app.models import AvailabilityResult
from app.services.checks import CheckService
from app.services.delivery import DeliveryService
from app.notifications import DeliveryOutcome
from app.settings import Settings
from app.storage.db import Database
from app.storage.repositories import Repository, LeaseLostError, ConflictError, iso, parse_time, utc_now


URL = (
    "https://www.doctolib.de/praxis/berlin/beispiel/booking/availabilities"
    "?placeId=practice-123&motiveIds%5B%5D=789&practitionerId=456"
)


FIXTURES = Path(__file__).parent / "fixtures"


class FixtureResponse:
    status_code = 200

    def __init__(self, value, status_code=200):
        self.value = value
        self.status_code = status_code
        self.headers = {}
        self.is_redirect = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("fixture HTTP error")

    def json(self):
        return self.value


class FixtureSession:
    def __init__(self, no_availability=False):
        self.no_availability = no_availability
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("info.json"):
            value = json.loads((FIXTURES / "info_de.json").read_text())
            return FixtureResponse(value)
        if self.no_availability:
            value = json.loads((FIXTURES / "no_availability.json").read_text())
            return FixtureResponse(value)
        value = json.loads((FIXTURES / "availability_window.json").read_text())
        return FixtureResponse(value)


class FixtureDoctolib(DoctolibClient):
    def __init__(self, no_availability=False):
        self.fixture_session = FixtureSession(no_availability=no_availability)
        super().__init__(session=self.fixture_session)


class FakeNotifier:
    def __init__(self, successful=True):
        self.sent = []
        self.successful = successful

    def __call__(self, settings, alert):
        self.sent.append(dict(alert))
        return DeliveryOutcome("sent" if self.successful else "retry", None if self.successful else "telegram_connect_error")


class Journey:
    """Test-only orchestration: availability and delivery are explicit services."""

    def __init__(self, repository, doctolib, settings, notifier=None):
        self.checker = CheckService(repository, doctolib, settings)
        self.delivery = DeliveryService(repository, settings, sender=notifier) if notifier else DeliveryService(repository, settings)

    def run_due(self, **kwargs):
        outcomes = self.checker.run_due(**kwargs)
        self.dispatch_pending()
        return outcomes

    def dispatch_pending(self):
        # Each turn can send only one ready alert. The bounded loop exists only
        # in legacy journey tests; the production worker never dispatches.
        for _ in range(20):
            if not self.delivery.run_once():
                break


class RaisingNotifier:
    def __call__(self, settings, alert):
        raise RuntimeError("https://api.telegram.org/botprivate-token/sendMessage")


class RaisingDoctolib(DoctolibClient):
    """Use actual fixture checks while simulating one failing target request."""

    def check(self, booking_url, search, meta=None, now=None):
        if "source=fail" in booking_url:
            raise requests.Timeout()
        return super().check(booking_url, search, meta=meta, now=now)


class StructuredErrorDoctolib(DoctolibClient):
    def check(self, booking_url, search, meta=None, now=None):
        return AvailabilityResult(
            status="error", slot_count=0, earliest_slot=None, count_complete=False,
            error_code="incomplete_availability_response",
            error_message="Doctolib returned a partial response",
        )


class PausingDoctolib(DoctolibClient):
    def __init__(self, repository, session, job_id):
        super().__init__(session=session)
        self.repository = repository
        self.job_id = job_id

    def check(self, booking_url, search, meta=None, now=None):
        result = super().check(booking_url, search, meta=meta, now=now)
        self.repository.set_status(self.job_id, "paused")
        return result


class UpdatingDoctolib(DoctolibClient):
    def __init__(self, client, session, job_id):
        super().__init__(session=session)
        self.client = client
        self.job_id = job_id
        self.searches = []

    def check(self, booking_url, search, meta=None, now=None):
        self.searches.append(dict(search))
        if len(self.searches) == 1:
            response = self.client.patch("/api/v1/jobs/" + self.job_id, json={
                "insurance_sector": "private", "telehealth": True,
            })
            assert response.status_code == 200, response.text
        return super().check(booking_url, search, meta=meta, now=now)


class VersionedJourneyClient(TestClient):
    """Existing behavioral journeys send the current mutation contract.

    Contract tests use plain TestClient to exercise omitted/stale versions and
    keys. Explicit expectations are never replaced by this test-only helper.
    """
    def request(self, method, url, **kwargs):
        from urllib.parse import urlsplit
        from uuid import uuid4
        path = urlsplit(str(url)).path
        method = method.upper()
        if method == 'POST' and path == '/api/v1/jobs':
            headers = dict(kwargs.get('headers') or {})
            if not any(key.lower() == 'idempotency-key' for key in headers):
                headers['Idempotency-Key'] = str(uuid4())
            kwargs['headers'] = headers
            # Old journeys opt into their explicitly saved fixture destination.
            # Raw channel/API contract tests bypass this helper.
            body = dict(kwargs.get('json') or {})
            if 'notification_channel_ids' not in body and body.get('telegram_enabled') is not False:
                channels = self.app.state.repository.list_channels()['items']
                body['notification_channel_ids'] = [item['id'] for item in channels if item['usable']]
            kwargs['json'] = body
        config_job = ((method == 'PATCH' and path.startswith('/api/v1/jobs/')) or
                      (method == 'DELETE' and path.startswith('/api/v1/jobs/')) or
                      (method == 'POST' and path.endswith(('/pause', '/resume'))))
        config_settings = method == 'PUT' and path == '/api/v1/settings'
        if config_job or config_settings:
            body = dict(kwargs.get('json') or {})
            if 'expected_version' not in body:
                read_path = '/api/v1/settings' if config_settings else '/api/v1/jobs/' + path.split('/')[4]
                current = super().request('GET', read_path)
                body['expected_version'] = current.json().get('edit_version', 1) if current.status_code == 200 else 1
            kwargs['json'] = body
        return super().request(method, url, **kwargs)


def setup_backend(tmp_path, status="available", minimum_interval=300):
    database = Database(str(tmp_path / "checker.sqlite3"))
    database.initialize()
    repository = Repository(database)
    from cryptography.fernet import Fernet
    from app.notification_secrets import NotificationSecrets
    secret_key = Fernet.generate_key().decode()
    settings = Settings(database_path=str(tmp_path / "checker.sqlite3"),
                        notification_secret_key=secret_key,
                        telegram_bot_token="test-token-never-return-this",
                        telegram_chat_id="12345", telegram_enabled=True,
                        minimum_poll_interval_seconds=minimum_interval)
    secrets = NotificationSecrets(secret_key)
    token = "123456:fixture_secret_for_offline_checks_123"
    chat = "12345"
    repository.create_channel({"name": "Fixture Telegram", "enabled": True,
        "token_ciphertext": secrets.encrypt(token), "chat_ciphertext": secrets.encrypt(chat),
        "destination_identity": secrets.identity("123456:" + chat)},
        "fixture-channel", "fixture-channel")
    doctolib = FixtureDoctolib(no_availability=status == "no_availability")
    app = create_app(settings=settings, repository=repository, doctolib=doctolib)
    return VersionedJourneyClient(app), repository, settings, doctolib


def create_job(client, interval_seconds=300):
    response = client.post("/api/v1/jobs", json={
        "name": "My appointment search",
        "target_urls": [URL],
        "interval_seconds": interval_seconds,
        "date_mode": "custom",
        "earliest_date": "2026-10-10",
        "latest_date": "2026-10-20",
        "time_zone": "Europe/Berlin",
        "insurance_sector": "public",
        "telehealth": False,
        "telegram_enabled": True,
    })
    assert response.status_code == 201, response.text
    return response.json()


def latest_result(client, job_id):
    runs = client.get("/api/v1/jobs/" + job_id + "/checks").json()
    return runs[0]["results"][-1]


def lock_owner(repository, job_id):
    with repository.database.connection() as conn:
        return conn.execute("SELECT lock_run_id FROM jobs WHERE id=?", (job_id,)).fetchone()[0]


def claim_for_result(repository, job):
    # Each simulated response belongs to a real immutable, live claim.
    with repository.database.connection() as conn:
        previous = conn.execute("SELECT lock_run_id,lock_owner_token FROM jobs WHERE id=?", (job["id"],)).fetchone()
    if previous[0]:
        repository.finish_run(previous[0], job["id"], 0, 0, job["interval_seconds"], owner_token=previous[1])
    with repository.database.connection() as conn:
        conn.execute("UPDATE jobs SET next_check_at=? WHERE id=?", (iso(utc_now()), job["id"]))
    run_id, claimed = repository.claim_due_jobs(limit=1)[0]
    assert claimed["id"] == job["id"]
    job["owner_token"] = claimed["owner_token"]
    return run_id, claimed


def record_result(repository, job, status, earliest_slot=None, slot_count=0):
    run_id, claimed = claim_for_result(repository, job)
    target = claimed["search_snapshot"]["targets"][0]
    result = AvailabilityResult(status=status, slot_count=slot_count, earliest_slot=earliest_slot, count_complete=True)
    result_id = repository.insert_result(run_id, claimed, target, result, owner_token=claimed["owner_token"])
    return target, result_id


def test_primary_user_journey_validate_create_check_alert_and_read(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    validation = client.post("/api/v1/targets/validate", json={"booking_url": URL})
    assert validation.status_code == 200
    assert validation.json()["country"] == "de"
    assert validation.json()["practice_name"] == "Praxis Beispiel"
    assert validation.json()["practitioner_name"] == "Dr. Ada Beispiel"
    assert validation.json()["motive_name"] == "Erstuntersuchung"
    assert client.get("/api/v1/jobs").json() == []

    job = create_job(client)
    assert job["earliest_date"] == "2026-10-10"
    assert job["latest_date"] == "2026-10-20"
    assert job["next_check_at"]

    notifier = FakeNotifier()
    checker = Journey(repository, doctolib, settings, notifier=notifier)
    outcomes = checker.run_due()
    assert outcomes == [{"successful_targets": 1, "failed_targets": 0}]
    assert len(notifier.sent) == 1

    check_rows = client.get("/api/v1/jobs/" + job["id"] + "/checks").json()
    alert_rows = client.get("/api/v1/alerts").json()
    assert len(check_rows) == 1
    assert check_rows[0]["outcome"] == "completed"
    assert check_rows[0]["successful_targets"] == 1
    assert check_rows[0]["results"][0]["status"] == "available"
    assert check_rows[0]["results"][0]["slot_count"] == 3
    assert check_rows[0]["results"][0]["earliest_slot"] == "2026-10-15T07:30:00+00:00"
    assert check_rows[0]["results"][0]["booking_url"] == URL
    assert len(alert_rows) == 1
    assert alert_rows[0]["practice_name"] == "Praxis Beispiel"
    assert alert_rows[0]["time_zone"] == "Europe/Berlin"
    assert client.get("/api/v1/jobs/" + job["id"]).json()["last_result"]["status"] == "available"
    assert b"test-token-never-return-this" not in client.get("/api/v1/settings").content
    assert len(doctolib.fixture_session.calls) == 3
    status = client.get("/api/v1/status").json()
    assert status["api_alive"] is True
    assert status["api_checked_at"]
    assert status["worker_alive"] is True


def test_invalid_target_and_interval_are_rejected_without_network_access(tmp_path):
    client, _repository, _settings, doctolib = setup_backend(tmp_path)
    response = client.post("/api/v1/targets/validate", json={
        "booking_url": "https://example.org/praxis/berlin/beispiel/booking/availabilities?placeId=1&motiveIds=2"
    })
    assert response.status_code == 422
    assert doctolib.fixture_session.calls == []

    response = client.post("/api/v1/jobs", json={
        "name": "Reversed dates",
        "target_urls": [URL],
        "interval_seconds": 300,
        "date_mode": "custom",
        "earliest_date": "2026-10-21",
        "latest_date": "2026-10-20",
    })
    assert response.status_code == 422
    assert doctolib.fixture_session.calls == []

    response = client.post("/api/v1/jobs", json={
        "name": "Too frequent",
        "target_urls": [URL],
        "interval_seconds": 60,
    })
    assert response.status_code == 422
    assert doctolib.fixture_session.calls == []

    response = client.post("/api/v1/jobs", json={
        "name": "Window too large",
        "target_urls": [URL],
        "interval_seconds": 300,
        "date_mode": "custom",
        "earliest_date": "2026-01-01",
        "latest_date": "2027-01-02",
    })
    assert response.status_code == 422
    assert doctolib.fixture_session.calls == []


def test_job_update_rejects_custom_window_over_366_dates(tmp_path):
    client, _repository, _settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)

    response = client.patch("/api/v1/jobs/" + job["id"], json={
        "latest_date": "2027-11-01",
    })

    assert response.status_code == 422
    assert "must not exceed 366" in response.text
    assert len(doctolib.fixture_session.calls) == 1


def test_job_update_rejects_null_values_instead_of_failing_in_storage(tmp_path):
    client, _repository, _settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)

    response = client.patch("/api/v1/jobs/" + job["id"], json={"name": None})

    assert response.status_code == 422
    assert "name cannot be null" in response.text
    assert client.get("/api/v1/jobs/" + job["id"]).json()["name"] == "My appointment search"
    assert len(doctolib.fixture_session.calls) == 1


def test_job_update_rejects_date_range_when_effective_mode_is_first_available(tmp_path):
    client, _repository, _settings, _doctolib = setup_backend(tmp_path)
    response = client.post("/api/v1/jobs", json={
        "name": "First available",
        "target_urls": [URL],
        "interval_seconds": 300,
        "date_mode": "first_available",
        "horizon_days": 15,
        "telegram_enabled": False,
    })
    assert response.status_code == 201, response.text
    job = response.json()

    patch = client.patch("/api/v1/jobs/" + job["id"], json={"earliest_date": "2026-10-10"})
    assert patch.status_code == 422
    assert "only valid with custom date mode" in patch.text

    transition = client.patch("/api/v1/jobs/" + job["id"], json={
        "date_mode": "first_available", "earliest_date": "2026-10-10",
    })
    assert transition.status_code == 422
    current = client.get("/api/v1/jobs/" + job["id"]).json()
    assert current["date_mode"] == "first_available"
    assert current["earliest_date"] is None


def test_create_job_rejects_duplicate_urls_after_normalization(tmp_path):
    client, _repository, _settings, doctolib = setup_backend(tmp_path)

    response = client.post("/api/v1/jobs", json={
        "name": "Duplicate normalized targets",
        "target_urls": [URL, URL + "#ignored-fragment"],
        "interval_seconds": 300,
    })

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_job"
    assert doctolib.fixture_session.calls == []
    assert client.get("/api/v1/jobs").json() == []


def test_no_match_stays_active_and_does_not_alert(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path, status="no_availability")
    job = create_job(client)
    notifier = FakeNotifier()
    outcomes = Journey(repository, doctolib, settings, notifier=notifier).run_due()
    assert outcomes == [{"successful_targets": 1, "failed_targets": 0}]
    assert latest_result(client, job["id"])["status"] == "no_availability"
    assert client.get("/api/v1/jobs").json()[0]["status"] == "active"
    assert client.get("/api/v1/alerts").json() == []
    assert notifier.sent == []


def test_structured_incomplete_availability_result_counts_as_failed_target(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    checker = Journey(repository, StructuredErrorDoctolib(), settings, notifier=FakeNotifier())

    outcomes = checker.run_due()

    assert outcomes == [{"successful_targets": 0, "failed_targets": 1}]
    run = client.get("/api/v1/jobs/" + job["id"] + "/checks").json()[0]
    assert run["outcome"] == "error"
    assert run["failed_targets"] == 1
    assert run["results"][0]["status"] == "error"
    assert run["results"][0]["error_code"] == "incomplete_availability_response"


def test_run_history_paginates_runs_and_keeps_all_target_results_together(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path, status="no_availability")
    response = client.post("/api/v1/jobs", json={
        "name": "Two targets across runs",
        "target_urls": [URL, URL + "&source=second"],
        "interval_seconds": 300,
        "telegram_enabled": False,
    })
    assert response.status_code == 201, response.text
    job = response.json()
    checker = Journey(repository, doctolib, settings, notifier=FakeNotifier())

    assert len(checker.run_due()) == 1
    with repository.database.connection() as conn:
        conn.execute("UPDATE jobs SET next_check_at=? WHERE id=?", (iso(utc_now()), job["id"]))
    assert len(checker.run_due()) == 1

    newest = client.get("/api/v1/jobs/" + job["id"] + "/checks?limit=1&offset=0").json()
    older = client.get("/api/v1/jobs/" + job["id"] + "/checks?limit=1&offset=1").json()
    assert len(newest) == len(older) == 1
    assert len(newest[0]["results"]) == len(older[0]["results"]) == 2
    assert newest[0]["id"] != older[0]["id"]


def test_run_with_no_results_is_visible_in_history(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path, status="no_availability")
    job = create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier()).run_due()
    with repository.database.connection() as conn:
        conn.execute("UPDATE jobs SET next_check_at=? WHERE id=?", (iso(utc_now()), job["id"]))
    run_id, _claimed_job = repository.claim_due_jobs(limit=1)[0]

    history = client.get("/api/v1/jobs/" + job["id"] + "/checks").json()

    assert len(history) == 2
    assert history[0]["id"] == run_id
    assert history[0]["outcome"] == "running"
    assert history[0]["results"] == []
    assert client.get("/api/v1/jobs/" + job["id"]).json()["last_result"]["status"] == "no_availability"


def test_pause_during_request_records_result_but_suppresses_alert(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    pausing = PausingDoctolib(repository, doctolib.fixture_session, job["id"])
    notifier = FakeNotifier()

    Journey(repository, pausing, settings, notifier=notifier).run_due()

    result = latest_result(client, job["id"])
    assert result["status"] == "available"
    assert repository.get_job(job["id"])["status"] == "paused"
    assert client.get("/api/v1/alerts").json() == []
    assert notifier.sent == []


def test_edit_preserves_original_options_across_targets_in_same_run(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    response = client.post("/api/v1/jobs", json={
        "name": "Two targets with changed settings",
        "target_urls": [URL, URL + "&source=second"],
        "interval_seconds": 300,
        "date_mode": "custom",
        "earliest_date": "2026-10-10",
        "latest_date": "2026-10-20",
        "telegram_enabled": False,
    })
    assert response.status_code == 201, response.text
    job = response.json()
    updating = UpdatingDoctolib(client, doctolib.fixture_session, job["id"])

    Journey(repository, updating, settings, notifier=FakeNotifier()).run_due(limit=1)

    assert [search["insurance_sector"] for search in updating.searches] == ["public", "public"]
    assert [search["telehealth"] for search in updating.searches] == [False, False]


def test_manual_intent_bypasses_floor_while_resume_preserves_it_after_restart(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    notifier = FakeNotifier()
    Journey(repository, doctolib, settings, notifier=notifier).run_due()

    queued = client.post("/api/v1/jobs/" + job["id"] + "/check-now")
    assert queued.status_code == 200
    assert queued.json()["status"] == "queued"
    assert parse_time(queued.json()["eligible_at"]) <= utc_now()
    assert parse_time(repository.get_job(job["id"])["next_check_at"]) >= utc_now() + timedelta(seconds=295)
    assert client.post("/api/v1/jobs/" + job["id"] + "/pause").json()["status"] == "paused"
    assert client.post("/api/v1/jobs/" + job["id"] + "/resume").json()["status"] == "active"

    reopened = Repository(Database(settings.database_path))
    assert reopened.get_job(job["id"])["last_finished_at"]
    assert reopened.get_job(job["id"])["status"] == "active"
    assert reopened.checks(job["id"])[0]["results"][0]["status"] == "available"
    assert reopened.alerts()[0]["status"] == "sent"


def test_edit_during_run_keeps_claim_and_uses_new_interval(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    claimed = repository.claim_due_jobs(limit=1)
    run_id, claimed_job = claimed[0]
    lock_until = repository.get_job(job["id"])["lock_until"]

    edited = client.patch("/api/v1/jobs/" + job["id"], json={"interval_seconds": 600})
    assert edited.status_code == 200
    assert repository.get_job(job["id"])["lock_until"] == lock_until
    assert repository.claim_due_jobs(limit=1) == []
    assert client.post("/api/v1/jobs/" + job["id"] + "/check-now").status_code == 200

    repository.finish_run(run_id, job["id"], 0, 0, claimed_job["interval_seconds"], owner_token=claimed_job["owner_token"])
    current = repository.get_job(job["id"])
    assert current["interval_seconds"] == 600
    assert current["check_intent"]["status"] == "queued"
    assert parse_time(current["next_check_at"]) >= utc_now() + timedelta(seconds=595)


def test_worker_claims_one_job_at_a_time_and_renews_owned_lease(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path, status="no_availability")
    first = create_job(client)
    second_response = client.post("/api/v1/jobs", json={
        "name": "Second due job",
        "target_urls": [URL + "&source=second"],
        "interval_seconds": 300,
        "telegram_enabled": False,
    })
    assert second_response.status_code == 201, second_response.text
    second = second_response.json()

    class InspectingDoctolib(DoctolibClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def check(self, booking_url, search, meta=None, now=None):
            self.calls += 1
            if self.calls == 1:
                assert repository.get_job(second["id"])["lock_until"] is None
            return doctolib.check(booking_url, search, meta=meta, now=now)

    checker = Journey(repository, InspectingDoctolib(), settings, notifier=FakeNotifier())
    assert len(checker.run_due(limit=2)) == 2
    assert lock_owner(repository, first["id"]) is None
    assert lock_owner(repository, second["id"]) is None

    with repository.database.connection() as conn:
        conn.execute("UPDATE jobs SET next_check_at=? WHERE id=?", (iso(utc_now()), first["id"]))
    run_id, _job = repository.claim_due_jobs(limit=1)[0]
    with repository.database.connection() as conn:
        conn.execute("UPDATE jobs SET lock_until=? WHERE id=?", (iso(utc_now() - timedelta(seconds=1)), first["id"]))
    assert not repository.renew_job_lock(first["id"], run_id, _job["owner_token"])
    assert not repository.renew_job_lock(first["id"], "another-run", _job["owner_token"])
    assert repository.get_job(first["id"])["lock_until"] < iso(utc_now())
    assert not repository.finish_run(run_id, first["id"], 0, 0, 300, owner_token=_job["owner_token"])


def test_stale_run_is_interrupted_after_its_lease_expires(tmp_path):
    client, repository, _settings, _doctolib = setup_backend(tmp_path)
    job = create_job(client)
    run_id, _claimed_job = repository.claim_due_jobs(limit=1)[0]
    with repository.database.connection() as conn:
        conn.execute("UPDATE jobs SET lock_until=? WHERE id=?", (iso(utc_now() - timedelta(seconds=1)), job["id"]))

    repository.interrupt_stale_runs()

    run = repository.checks(job["id"])[0]
    assert run["id"] == run_id
    assert run["outcome"] == "interrupted"
    assert lock_owner(repository, job["id"]) is None


def test_database_v1_upgrade_interrupts_unprovable_running_lease(tmp_path):
    client, repository, _settings, _doctolib = setup_backend(tmp_path)
    job = create_job(client)
    run_id, _claimed = repository.claim_due_jobs(limit=1)[0]
    with repository.database.connection() as conn:
        drop_revision_columns(conn)
        conn.execute("DROP TABLE target_alert_state")
        conn.execute("ALTER TABLE jobs DROP COLUMN lock_run_id")
        conn.execute("UPDATE schema_version SET version=1")
    repository.database.initialize()
    assert lock_owner(repository, job["id"]) is None
    run = repository.checks(job["id"])[0]
    assert run["id"] == run_id and run["outcome"] == "interrupted"
    assert not run["snapshot_known"] and run["search_snapshot"] is None
    with repository.database.connection() as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 8


def test_delete_keeps_history_available_through_job_id(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path, status="no_availability")
    job = create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier()).run_due()
    deleted = client.delete("/api/v1/jobs/" + job["id"])
    assert deleted.status_code == 200
    assert client.get("/api/v1/jobs").json() == []
    history = client.get("/api/v1/jobs/" + job["id"] + "/checks")
    assert history.status_code == 200
    assert isinstance(history.json(), list)
    assert len(history.json()) == 1
    assert client.get("/api/v1/jobs/" + job["id"]).status_code == 404


def test_failed_telegram_delivery_preserves_available_result_and_history(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    notifier = FakeNotifier(successful=False)

    Journey(repository, doctolib, settings, notifier=notifier).run_due()

    result = latest_result(client, job["id"])
    alert = client.get("/api/v1/alerts").json()[0]
    assert result["status"] == "available"
    assert alert["status"] == "failed"
    assert alert["attempt_count"] == 1
    assert alert["error_summary"] == "telegram_connect_error"

    with repository.database.connection() as conn:
        conn.execute("UPDATE alerts SET next_attempt_at=? WHERE id=?", (iso(utc_now()), alert["id"]))
    retry_notifier = FakeNotifier(successful=True)
    Journey(repository, doctolib, settings, notifier=retry_notifier).dispatch_pending()
    retried = client.get("/api/v1/alerts").json()[0]
    assert retried["status"] == "sent"
    assert retried["attempt_count"] == 2
    assert len(retry_notifier.sent) == 1


def test_pause_revokes_old_alert_and_fresh_run_requires_telegram_enabled(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier(successful=False)).run_due()
    alert = client.get("/api/v1/alerts").json()[0]
    with repository.database.connection() as conn:
        conn.execute("UPDATE alerts SET next_attempt_at=? WHERE id=?", (iso(utc_now()), alert["id"]))

    retry_notifier = FakeNotifier()
    checker = Journey(repository, doctolib, settings, notifier=retry_notifier)
    assert client.post("/api/v1/jobs/" + job["id"] + "/pause").status_code == 200
    checker.dispatch_pending()
    assert retry_notifier.sent == []

    assert client.post("/api/v1/jobs/" + job["id"] + "/resume").status_code == 200
    assert client.patch("/api/v1/jobs/" + job["id"], json={"telegram_enabled": False}).status_code == 200
    checker.dispatch_pending()
    assert retry_notifier.sent == []

    assert client.patch("/api/v1/jobs/" + job["id"], json={"telegram_enabled": True}).status_code == 200
    checker.dispatch_pending()
    assert retry_notifier.sent == []  # Resume cannot revive the pre-pause capability.
    assert repository.alerts()[0]['status'] == 'cancelled'
    repository.request_check(job['id'])
    checker.run_due()
    assert retry_notifier.sent == []  # Reconfirmation does not revive cancelled routing.
    record_result(repository, job, 'no_availability')
    slot = datetime.fromisoformat(alert['earliest_slot'])
    record_result(repository, job, 'available', slot, 3)
    checker.dispatch_pending()
    assert len(retry_notifier.sent) == 1


def test_notifier_exception_is_sanitized_and_marked_uncertain(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)

    Journey(repository, doctolib, settings, notifier=RaisingNotifier()).run_due()

    alert = client.get("/api/v1/alerts").json()[0]
    assert alert["delivery_state"] == "uncertain"
    assert alert["error_summary"] == "telegram_delivery_error"
    assert b"private-token" not in client.get("/api/v1/alerts").content
    assert latest_result(client, job["id"])["status"] == "available"


def test_repeated_identical_slot_does_not_create_another_alert(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier()).run_due()

    target = repository.get_job(job["id"])["targets"][0]
    check = repository.checks(job["id"])[0]["results"][0]
    duplicate = repository.create_alert(
        job, target, check["id"], datetime.fromisoformat(check["earliest_slot"])
    )

    assert duplicate is None
    assert len(client.get("/api/v1/alerts").json()) == 1


def test_failed_alert_waits_for_fresh_confirmation_then_retries(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier(successful=False)).run_due()
    original = client.get("/api/v1/alerts").json()[0]
    slot = datetime.fromisoformat(original["earliest_slot"])
    with repository.database.connection() as conn:
        conn.execute("UPDATE check_results SET checked_at=? WHERE id=?",
                     (iso(utc_now() - timedelta(seconds=301)), original["result_id"]))
        conn.execute("UPDATE alerts SET next_attempt_at=? WHERE id=?", (iso(utc_now()), original["id"]))

    notifier = FakeNotifier()
    checker = Journey(repository, doctolib, settings, notifier=notifier)
    checker.dispatch_pending()
    assert notifier.sent == []
    target, result_id = record_result(repository, job, "available", slot, 3)
    assert result_id != original["result_id"]
    assert repository.create_alert(job, target, result_id, slot, owner_token=job["owner_token"]) == original["id"]
    checker.dispatch_pending()
    assert len(notifier.sent) == 1
    assert client.get("/api/v1/alerts").json()[0]["status"] == "sent"


def test_pause_and_error_do_not_make_an_old_alert_send_without_confirmation(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier(successful=False)).run_due()
    alert = client.get("/api/v1/alerts").json()[0]
    slot = datetime.fromisoformat(alert["earliest_slot"])
    with repository.database.connection() as conn:
        conn.execute("UPDATE alerts SET next_attempt_at=? WHERE id=?", (iso(utc_now()), alert["id"]))

    notifier = FakeNotifier()
    checker = Journey(repository, doctolib, settings, notifier=notifier)
    assert client.post("/api/v1/jobs/" + job["id"] + "/pause").status_code == 200
    checker.dispatch_pending()
    assert notifier.sent == []
    with repository.database.connection() as conn:
        conn.execute("UPDATE check_results SET checked_at=? WHERE id=?",
                     (iso(utc_now() - timedelta(seconds=301)), alert["result_id"]))
    assert client.post("/api/v1/jobs/" + job["id"] + "/resume").status_code == 200
    run_id, claimed = claim_for_result(repository, job)
    target = claimed["search_snapshot"]["targets"][0]
    repository.insert_error_result(run_id, claimed, target, "doctolib_request_error", "retry later", owner_token=claimed["owner_token"])
    assert repository.alerts()[0]["status"] == "cancelled"
    checker.dispatch_pending()
    assert notifier.sent == []

    target, result_id = record_result(repository, job, "available", slot, 3)
    repository.create_alert(job, target, result_id, slot, owner_token=job['owner_token'])
    checker.dispatch_pending()
    assert notifier.sent == []
    record_result(repository, job, 'no_availability')
    record_result(repository, job, 'available', slot, 3)
    checker.dispatch_pending()
    assert len(notifier.sent) == 1


@pytest.mark.parametrize("new_status,new_slot", [
    ("no_availability", None),
    ("available", datetime(2026, 10, 16, 7, 30, tzinfo=timezone.utc)),
])
def test_new_confirmed_result_cancels_contradicted_alert(tmp_path, new_status, new_slot):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier(successful=False)).run_due()
    old_id = repository.alerts()[0]["id"]
    record_result(repository, job, new_status, new_slot, int(new_slot is not None))

    notifier = FakeNotifier()
    Journey(repository, doctolib, settings, notifier=notifier).dispatch_pending()
    assert len(notifier.sent) == int(new_slot is not None)
    if new_slot:
        assert notifier.sent[0]["earliest_slot"] == iso(new_slot)
    assert next(item for item in repository.alerts() if item["id"] == old_id)["status"] == "cancelled"


def test_expired_slot_and_removed_target_cancel_pending_alerts(tmp_path, monkeypatch):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier(successful=False)).run_due()
    slot = datetime.fromisoformat(client.get("/api/v1/alerts").json()[0]["earliest_slot"])
    monkeypatch.setattr("app.storage.repositories.utc_now", lambda: slot + timedelta(seconds=1))
    assert repository.get_pending_alerts() == []
    assert client.get("/api/v1/alerts").json()[0]["error_summary"] == "slot_expired"

    monkeypatch.undo()
    second_client, second_repository, second_settings, second_doctolib = setup_backend(tmp_path / "second")
    second_job = create_job(second_client)
    Journey(second_repository, second_doctolib, second_settings,
                 notifier=FakeNotifier(successful=False)).run_due()
    replacement_url = URL + "&source=other"
    assert second_client.patch("/api/v1/jobs/" + second_job["id"],
                               json={"target_urls": [replacement_url]}).status_code == 200
    assert second_client.get("/api/v1/alerts").json()[0]["status"] == "cancelled"


def test_reappearance_alerts_again_but_count_change_does_not(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier()).run_due()
    first = client.get("/api/v1/alerts").json()[0]
    slot = datetime.fromisoformat(first["earliest_slot"])

    record_result(repository, job, "no_availability")
    target, result_id = record_result(repository, job, "available", slot, 3)
    second_id = repository.create_alert(job, target, result_id, slot, owner_token=job["owner_token"])
    assert second_id and second_id != first["id"]
    Journey(repository, doctolib, settings, notifier=FakeNotifier()).dispatch_pending()

    target, result_id = record_result(repository, job, "available", slot, 4)
    assert repository.create_alert(job, target, result_id, slot, owner_token=job["owner_token"]) is None
    alerts = client.get("/api/v1/alerts").json()
    assert len(alerts) == 2
    assert all(alert["status"] == "sent" for alert in alerts)


def test_configured_minimum_applies_to_recurring_but_manual_intent_overrides(tmp_path):
    client, repository, _settings, _doctolib = setup_backend(tmp_path, minimum_interval=600)
    too_fast = client.post("/api/v1/jobs", json={
        "name": "Too fast", "target_urls": [URL], "interval_seconds": 300,
    })
    assert too_fast.status_code == 422
    job = create_job(client, interval_seconds=600)
    assert client.patch("/api/v1/jobs/" + job["id"],
                        json={"interval_seconds": 300}).status_code == 422
    assert client.put("/api/v1/settings",
                      json={"default_interval_seconds": 300}).status_code == 422

    run_id, claimed = repository.claim_due_jobs(limit=1)[0]
    repository.finish_run(run_id, job["id"], 1, 0, claimed["interval_seconds"], owner_token=claimed["owner_token"])
    last_finished = parse_time(repository.get_job(job["id"])["last_finished_at"])
    floor = last_finished + timedelta(seconds=600)
    assert parse_time(repository.get_job(job["id"])["next_check_at"]) >= floor

    edited = client.patch("/api/v1/jobs/" + job["id"], json={"name": "Edited"})
    assert edited.status_code == 200
    assert parse_time(edited.json()["next_check_at"]) >= floor
    assert client.post("/api/v1/jobs/" + job["id"] + "/pause").status_code == 200
    resumed = client.post("/api/v1/jobs/" + job["id"] + "/resume")
    assert parse_time(resumed.json()["next_check_at"]) >= floor
    check_now = client.post("/api/v1/jobs/" + job["id"] + "/check-now")
    assert check_now.status_code == 200
    assert parse_time(check_now.json()["eligible_at"]) <= utc_now()
    assert parse_time(repository.get_job(job["id"])["next_check_at"]) >= floor


def test_raising_server_minimum_normalizes_existing_job_and_default(tmp_path):
    client, repository, _settings, _doctolib = setup_backend(tmp_path)
    job = create_job(client)
    repository.settings(300, 3)
    repository.configure_minimum(600)

    assert repository.get_job(job["id"])["interval_seconds"] == 600
    assert repository.settings(600, 3)["default_interval_seconds"] == 600


def test_request_spacing_keeps_fractional_seconds_across_repository_instances(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "spacing.sqlite3"))
    database.initialize()
    first = Repository(database)
    second = Repository(database)
    fixed = datetime(2026, 10, 1, 0, 0, 0, 900000, tzinfo=timezone.utc)
    waits = []
    monkeypatch.setattr("app.storage.repositories.utc_now", lambda: fixed)
    monkeypatch.setattr("app.storage.repositories.time.sleep", waits.append)

    first.reserve_request_turn(3)
    second.reserve_request_turn(3)

    assert waits == [3.0]
    with database.connection() as conn:
        stored = conn.execute("SELECT next_allowed_at FROM request_gate").fetchone()[0]
    assert stored == "2026-10-01T00:00:06.900000+00:00"


def test_v2_migration_preserves_sent_and_failed_alerts_without_resending(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    sent_job = create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier()).run_due()
    failed_job = create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier(successful=False)).run_due()
    before = {alert["job_id"]: alert for alert in client.get("/api/v1/alerts").json()}
    assert before[sent_job["id"]]["status"] == "sent"
    assert before[failed_job["id"]]["status"] == "failed"

    # Recreate the v2 alert constraint and remove v3 episode state.
    with repository.database.connection() as conn:
        conn.execute("""CREATE TABLE alerts_v2 (
            id TEXT PRIMARY KEY,job_id TEXT REFERENCES jobs(id),target_id TEXT REFERENCES targets(id),
            result_id TEXT REFERENCES check_results(id),channel TEXT NOT NULL,event_type TEXT NOT NULL,
            dedupe_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK (status IN ('pending','sent','failed')),
            attempt_count INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,sent_at TEXT,
            error_summary TEXT,next_attempt_at TEXT
        )""")
        conn.execute("INSERT INTO alerts_v2 SELECT id,job_id,target_id,result_id,channel,event_type,dedupe_key,status,attempt_count,created_at,sent_at,error_summary,next_attempt_at FROM alerts")
        conn.execute("DROP TABLE alerts")
        conn.execute("ALTER TABLE alerts_v2 RENAME TO alerts")
        conn.execute("CREATE INDEX idx_alerts_created ON alerts(created_at DESC)")
        conn.execute("DROP TABLE target_alert_state")
        drop_revision_columns(conn, alerts=False)
        conn.execute("UPDATE schema_version SET version=2")

    repository.database.initialize()
    with repository.database.connection() as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM target_alert_state").fetchone()[0] == 2
    after = {alert["job_id"]: alert for alert in repository.alerts()}
    assert {key: value["status"] for key, value in after.items()} == {
        sent_job["id"]: "sent", failed_job["id"]: "cancelled",
    }

    for job in (sent_job, failed_job):
        old_alert = before[job["id"]]
        slot = datetime.fromisoformat(old_alert["earliest_slot"])
        target, result_id = record_result(repository, job, "available", slot, 3)
        duplicate = repository.create_alert(job, target, result_id, slot, owner_token=job["owner_token"])
        assert duplicate is None
    assert len(repository.alerts()) == 2
    legacy_failed = next(alert for alert in repository.alerts() if alert["job_id"] == failed_job["id"])
    assert legacy_failed["delivery_state"] == "uncertain"
    sender = FakeNotifier()
    dispatcher = DeliveryService(repository, settings, sender=sender)
    assert not dispatcher.run_once() and sender.sent == []
    with pytest.raises(ConflictError, match="acknowledgement"):
        repository.recover_alert(legacy_failed["id"])
    assert not repository.recover_alert(legacy_failed["id"], acknowledge_duplicate_risk=True)
    assert not dispatcher.run_once() and sender.sent == []
    assert len(repository.alerts()) == 2


def test_one_target_failure_does_not_discard_another_target_result(tmp_path):
    database = Database(str(tmp_path / "checker.sqlite3"))
    database.initialize()
    repository = Repository(database)
    settings = Settings(database_path=str(tmp_path / "checker.sqlite3"))
    doctolib = RaisingDoctolib(session=FixtureSession())
    client = VersionedJourneyClient(create_app(settings=settings, repository=repository, doctolib=doctolib))
    good_url = URL
    bad_url = URL + "&source=fail"
    response = client.post("/api/v1/jobs", json={
        "name": "Two targets",
        "target_urls": [good_url, bad_url],
        "interval_seconds": 300,
        "date_mode": "custom",
        "earliest_date": "2026-10-10",
        "latest_date": "2026-10-20",
    })
    assert response.status_code == 201, response.text
    job = response.json()

    Journey(repository, doctolib, settings, notifier=FakeNotifier()).run_due()
    history = client.get("/api/v1/jobs/" + job["id"] + "/checks")

    assert history.status_code == 200
    assert {row["status"] for row in history.json()[0]["results"]} == {"available", "error"}


def test_shared_request_gate_reservations_survive_repository_reopen(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "checker.sqlite3"))
    database.initialize()
    first_repository = Repository(database)
    waits = []
    monkeypatch.setattr("app.storage.repositories.time.sleep", lambda seconds: waits.append(seconds))
    fixed_now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    monkeypatch.setattr("app.storage.repositories.utc_now", lambda: fixed_now)

    first_repository.reserve_request_turn(3)
    second_repository = Repository(Database(str(tmp_path / "checker.sqlite3")))
    second_repository.reserve_request_turn(3)

    with database.connection() as conn:
        next_allowed = parse_time(conn.execute(
            "SELECT next_allowed_at FROM request_gate WHERE singleton_id=1"
        ).fetchone()[0])
    assert waits == [3.0]
    assert next_allowed == fixed_now + timedelta(seconds=6)


def drop_channel_columns(conn):
    # Current fixtures contain one destination per legacy episode. Remove its
    # schema-8 delivery suffix to recreate the historical event-only identity.
    for row in conn.execute("SELECT id,dedupe_key FROM alerts").fetchall():
        conn.execute("UPDATE alerts SET dedupe_key=? WHERE id=?", (row[1].split(':channel:')[0], row[0]))
    conn.execute("DROP INDEX IF EXISTS idx_alerts_event_destination")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
    for column in ("event_id", "channel_config_id", "destination_version", "credential_version", "channel_name"):
        if column in columns:
            conn.execute(f"ALTER TABLE alerts DROP COLUMN {column}")
    for table in ("channel_tests", "channel_mutations", "notification_onboarding", "job_channels", "availability_events", "notification_channels"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def drop_mutation_columns(conn):
    drop_channel_columns(conn)
    conn.execute("DROP TABLE IF EXISTS create_operations")
    conn.execute("ALTER TABLE settings DROP COLUMN edit_version")


def drop_intent_columns(conn):
    drop_mutation_columns(conn)
    conn.execute("DROP TABLE IF EXISTS check_intents")
    for table, columns in {
        "jobs": ("status_version", "last_extra_started_at"),
        "check_runs": ("intent_id", "paused_manual", "status_version"),
    }.items():
        for column in columns:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def drop_delivery_columns(conn, *, alerts=True):
    drop_intent_columns(conn)
    conn.execute("DROP TABLE IF EXISTS dispatcher_heartbeat")
    if alerts:
        for column in ("delivery_state", "claim_owner_token", "claim_until", "claim_result_id", "claim_search_revision", "attempt_started_at", "last_attempt_at", "last_attempt_outcome", "delivery_epoch_at", "delivery_epoch_attempts"):
            conn.execute(f"ALTER TABLE alerts DROP COLUMN {column}")


def drop_revision_columns(conn, *, alerts=True):
    drop_delivery_columns(conn, alerts=alerts)
    conn.execute("DROP INDEX IF EXISTS idx_results_known_run_target")
    for table, columns in {
        "jobs": ("lock_owner_token", "search_revision", "edit_version"),
        "check_runs": ("search_revision", "search_snapshot", "snapshot_known", "owner_token"),
        "check_results": ("search_revision", "snapshot_known", "published"),
        "alerts": ("search_revision",) if alerts else (),
    }.items():
        for column in columns:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def test_effective_revision_equality_and_scheduler_versions(tmp_path):
    client, repository, _settings, _doctolib = setup_backend(tmp_path)
    job = create_job(client)
    original = (job['search_revision'], job['edit_version'])
    unchanged = client.patch('/api/v1/jobs/' + job['id'], json={'name': job['name'], 'target_urls': [URL], 'horizon_days': 40}).json()
    assert unchanged['search_revision'] == original[0]  # Horizon is ineffective in custom mode.
    renamed = client.patch('/api/v1/jobs/' + job['id'], json={'name': 'Renamed', 'interval_seconds': 600, 'telegram_enabled': False}).json()
    assert renamed['search_revision'] == original[0]
    assert renamed['edit_version'] == unchanged['edit_version'] + 1
    revision = client.patch('/api/v1/jobs/' + job['id'], json={'insurance_sector': 'private'}).json()
    assert revision['search_revision'] == original[0] + 1
    version = revision['edit_version']
    run_id, claimed = repository.claim_due_jobs(limit=1)[0]
    repository.renew_job_lock(job['id'], run_id, claimed['owner_token'])
    repository.finish_run(run_id, job['id'], 0, 0, 600, owner_token=claimed['owner_token'])
    assert repository.get_job(job['id'])['edit_version'] == version


def test_claim_freezes_dates_and_target_metadata_and_hides_owner(tmp_path, monkeypatch):
    client, repository, _settings, _doctolib = setup_backend(tmp_path)
    fixed = datetime(2026, 10, 1, 21, 59, tzinfo=timezone.utc)
    monkeypatch.setattr('app.storage.repositories.utc_now', lambda: fixed)
    job = create_job(client)
    client.patch('/api/v1/jobs/' + job['id'], json={'date_mode': 'first_available', 'horizon_days': 15})
    run_id, claimed = repository.claim_due_jobs(limit=1)[0]
    snapshot = claimed['search_snapshot']
    assert snapshot['search']['effective_earliest_date'] == '2026-10-01'
    assert snapshot['search']['effective_latest_date'] == '2026-10-15'
    assert claimed['owner_token'] != run_id
    with repository.database.connection() as conn:
        conn.execute("UPDATE targets SET practitioner_name='Changed after claim' WHERE job_id=?", (job['id'],))
    assert snapshot['targets'][0]['practitioner_name'] == 'Dr. Ada Beispiel'
    payload = client.get('/api/v1/jobs/' + job['id'] + '/checks').json()
    assert payload[0]['snapshot_known']
    assert isinstance(payload[0]['search_snapshot'], dict)
    assert claimed['owner_token'] not in json.dumps(payload)
    assert claimed['owner_token'] not in client.get('/api/v1/jobs/' + job['id']).text


def test_obsolete_response_is_history_only_after_edit_cancels_pending(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier(False)).run_due()
    pending = repository.alerts()[0]
    run_id, claimed = claim_for_result(repository, job)
    target = claimed['search_snapshot']['targets'][0]
    client.patch('/api/v1/jobs/' + job['id'], json={'insurance_sector': 'private'})
    result = AvailabilityResult(status='no_availability', slot_count=0, earliest_slot=None, count_complete=True)
    result_id = repository.insert_result(run_id, claimed, target, result, owner_token=claimed['owner_token'])
    history = repository.checks(job['id'])[0]['results'][0]
    assert history['id'] == result_id and not history['published']
    assert history['search_revision'] == claimed['search_revision']
    assert repository.alerts()[0]['status'] == 'cancelled'
    assert repository.alerts()[0]['error_summary'] == 'search_edited'
    assert repository.get_pending_alerts() == []
    with repository.database.connection() as conn:
        assert conn.execute('SELECT last_status FROM target_alert_state WHERE target_id=?', (target['id'],)).fetchone()[0] == 'available'


def test_expired_owner_cannot_publish_renew_or_finalize_after_reclaim(tmp_path):
    client, repository, _settings, _doctolib = setup_backend(tmp_path)
    job = create_job(client)
    old_run, old = repository.claim_due_jobs(limit=1)[0]
    with repository.database.connection() as conn:
        conn.execute('UPDATE jobs SET lock_until=? WHERE id=?', (iso(utc_now() - timedelta(seconds=1)), job['id']))
    assert not repository.renew_job_lock(job['id'], old_run, old['owner_token'])
    repository.interrupt_stale_runs()
    new_run, new = repository.claim_due_jobs(limit=1)[0]
    assert new['owner_token'] != old['owner_token']
    result = AvailabilityResult(status='no_availability', slot_count=0, earliest_slot=None, count_complete=True)
    with pytest.raises(LeaseLostError):
        repository.insert_result(old_run, old, old['search_snapshot']['targets'][0], result, owner_token=old['owner_token'])
    assert not repository.finish_run(old_run, job['id'], 1, 0, 300, owner_token=old['owner_token'])
    assert lock_owner(repository, job['id']) == new_run
    assert repository.checks(job['id'])[0]['outcome'] == 'running'


def test_partial_snapshot_coverage_cannot_be_completed_by_claimed_counters(tmp_path):
    client, repository, _settings, _doctolib = setup_backend(tmp_path)
    job = create_job(client)
    run_id, claimed = repository.claim_due_jobs(limit=1)[0]
    assert repository.finish_run(run_id, job['id'], 1, 0, 300, owner_token=claimed['owner_token'])
    run = repository.checks(job['id'])[0]
    assert run['outcome'] == 'interrupted'
    assert run['successful_targets'] == 0


def test_unknown_schema_fails_without_creating_checker_tables(tmp_path):
    path = tmp_path / 'future.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.executescript('CREATE TABLE schema_version(version INTEGER); INSERT INTO schema_version VALUES(99); CREATE TABLE private_future(value TEXT);')
    with pytest.raises(RuntimeError, match='Unsupported'):
        Database(str(path)).initialize()
    with sqlite3.connect(path) as conn:
        assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {'schema_version', 'private_future'}
        assert conn.execute('SELECT version FROM schema_version').fetchone()[0] == 99


def test_blocked_inflight_response_after_narrowing_cannot_notify(tmp_path):
    import threading
    client, repository, settings, _doctolib = setup_backend(tmp_path)
    job = create_job(client)
    entered, release = threading.Event(), threading.Event()
    errors = []

    class BlockedDoctolib(DoctolibClient):
        def check(self, booking_url, search, meta=None, now=None):
            entered.set()
            assert release.wait(5)
            return AvailabilityResult(status='available', slot_count=1,
                                      earliest_slot=datetime(2026, 10, 15, 7, 30, tzinfo=timezone.utc),
                                      count_complete=True)

    notifier = FakeNotifier()
    checker = Journey(repository, BlockedDoctolib(), settings, notifier=notifier)
    def run():
        try:
            checker.run_due(limit=1)
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        edited = client.patch('/api/v1/jobs/' + job['id'], json={'latest_date': '2026-10-12'})
        assert edited.status_code == 200
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    assert notifier.sent == [] and repository.alerts() == []
    historical = repository.checks(job['id'])[0]['results'][0]
    assert historical['status'] == 'available' and not historical['published']
    assert historical['search_revision'] != repository.get_job(job['id'])['search_revision']
    with repository.database.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM target_alert_state').fetchone()[0] == 0
        conn.execute('UPDATE jobs SET next_check_at=? WHERE id=?', (iso(utc_now()), job['id']))
    Journey(repository, FixtureDoctolib(no_availability=True), settings, notifier=notifier).run_due()
    fresh = repository.checks(job['id'])[0]['results'][0]
    assert fresh['status'] == 'no_availability' and fresh['published']
    assert fresh['search_revision'] == edited.json()['search_revision']


def test_equivalent_target_order_is_noop_but_effective_metadata_changes_revision(tmp_path):
    client, repository, _settings, _doctolib = setup_backend(tmp_path)
    job = create_job(client)
    second_url = URL + '&source=second'
    job = client.patch('/api/v1/jobs/' + job['id'], json={'target_urls': [URL, second_url]}).json()
    reordered = client.patch('/api/v1/jobs/' + job['id'], json={'target_urls': [second_url, URL]}).json()
    assert (reordered['search_revision'], reordered['edit_version']) == (job['search_revision'], job['edit_version'])
    targets = []
    for target in reordered['targets']:
        target = dict(target)
        target['agenda_ids_str'] = target['agenda_ids']
        target['practitioner_name'] = 'Updated effective label'
        targets.append(target)
    changed = repository.update_job(job['id'], {}, targets=targets)
    assert changed['search_revision'] == job['search_revision'] + 1


def test_legacy_pending_needs_reconfirmation_while_sent_dedupe_survives_revision(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier(False)).run_due()
    original = repository.alerts()[0]
    with repository.database.connection() as conn:
        conn.execute('UPDATE alerts SET search_revision=NULL,next_attempt_at=? WHERE id=?', (iso(utc_now()), original['id']))
        conn.execute('UPDATE check_results SET search_revision=NULL,snapshot_known=0,published=0 WHERE id=?', (original['result_id'],))
    notifier = FakeNotifier()
    checker = Journey(repository, doctolib, settings, notifier=notifier)
    checker.dispatch_pending()
    assert notifier.sent == []
    slot = datetime.fromisoformat(original['earliest_slot'])
    target, result_id = record_result(repository, job, 'available', slot, 3)
    assert repository.create_alert(job, target, result_id, slot, owner_token=job['owner_token']) is None
    assert repository.recover_alert(original['id'])
    checker.dispatch_pending()
    assert len(notifier.sent) == 1
    client.patch('/api/v1/jobs/' + job['id'], json={'insurance_sector': 'private'})
    target, result_id = record_result(repository, job, 'available', slot, 3)
    assert repository.create_alert(job, target, result_id, slot, owner_token=job['owner_token']) is None
    checker.dispatch_pending()
    assert len(notifier.sent) == 1 and len(repository.alerts()) == 1


def test_request_after_midnight_uses_claim_calendar_and_live_slot_clock(tmp_path, monkeypatch):
    client, repository, _settings, doctolib = setup_backend(tmp_path)
    before_midnight = datetime(2026, 10, 1, 21, 59, tzinfo=timezone.utc)
    monkeypatch.setattr('app.storage.repositories.utc_now', lambda: before_midnight)
    job = create_job(client)
    client.patch('/api/v1/jobs/' + job['id'], json={'date_mode': 'first_available', 'horizon_days': 15})
    after_midnight = datetime(2026, 10, 1, 22, 1, tzinfo=timezone.utc)
    monkeypatch.setattr('app.storage.repositories.utc_now', lambda: before_midnight)
    _run_id, claimed = repository.claim_due_jobs(limit=1)[0]
    search = claimed['search_snapshot']['search']
    meta = doctolib.resolve(URL)
    doctolib.fixture_session.calls.clear()
    result = doctolib.check(URL, search, meta=meta, now=after_midnight)
    assert result.status == 'available'
    assert doctolib.fixture_session.calls[0][1]['params']['start_date'] == '2026-10-01'
    assert DoctolibClient._window(search, after_midnight)[2].isoformat() == '2026-10-15'


@pytest.mark.parametrize('boundary', ['gate_wait', 'retry'])
def test_lease_is_rechecked_after_gate_wait_and_before_retry(tmp_path, monkeypatch, boundary):
    client, repository, settings, _doctolib = setup_backend(tmp_path)
    job = create_job(client)
    fixed = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    clock = [fixed]
    monkeypatch.setattr('app.storage.repositories.utc_now', lambda: clock[0])
    with repository.database.connection() as conn:
        conn.execute("UPDATE jobs SET next_check_at=? WHERE id=?", (iso(fixed), job["id"]))
    calls = []
    class ExpiringSession:
        def get(self, url, **kwargs):
            calls.append(url)
            clock[0] += timedelta(minutes=11)
            return FixtureResponse({}, status_code=503)
    def waiting_hook():
        clock[0] += timedelta(minutes=11)
    doctor = DoctolibClient(session=ExpiringSession(), before_request=waiting_hook if boundary == 'gate_wait' else None)
    monkeypatch.setattr('app.doctolib.time_module.sleep', lambda seconds: None)
    checker = Journey(repository, doctor, settings, notifier=FakeNotifier())
    checker.run_due(limit=1)
    assert len(calls) == (0 if boundary == 'gate_wait' else 1)
    assert repository.checks(job['id'])[0]['results'] == []
    repository.interrupt_stale_runs()
    assert repository.checks(job['id'])[0]['outcome'] == 'interrupted'


def test_scheduled_retry_rechecks_revision_before_another_attempt(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    CheckService(repository, doctolib, settings).run_due()
    notifier = FakeNotifier(False)
    dispatcher = DeliveryService(repository, settings, sender=notifier)
    assert dispatcher.run_once()
    assert len(notifier.sent) == 1
    alert = repository.alerts()[0]
    assert alert["attempt_count"] == 1
    assert parse_time(alert["next_attempt_at"]) >= utc_now() + timedelta(seconds=4)
    assert client.patch("/api/v1/jobs/" + job["id"], json={"insurance_sector": "private"}).status_code == 200
    with repository.database.connection() as conn:
        conn.execute("UPDATE alerts SET next_attempt_at=? WHERE id=?", (iso(utc_now()), alert["id"]))
    assert not dispatcher.run_once()
    assert len(notifier.sent) == 1
    assert repository.alerts()[0]["attempt_count"] == 1


def test_periodic_stale_reconciliation_preserves_actual_terminal_counts(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    response = client.patch("/api/v1/jobs/" + job["id"], json={
        "target_urls": [URL, URL + "&source=second", URL + "&source=third"],
        "telegram_enabled": False,
    })
    assert response.status_code == 200
    run_id, claimed = repository.claim_due_jobs(limit=1)[0]
    targets = claimed["search_snapshot"]["targets"]
    repository.insert_result(run_id, claimed, targets[0],
                             AvailabilityResult(status="no_availability", slot_count=0,
                                                earliest_slot=None, count_complete=True),
                             owner_token=claimed["owner_token"])
    repository.insert_error_result(run_id, claimed, targets[1], "doctolib_request_error", "retry later",
                                   owner_token=claimed["owner_token"])
    with repository.database.connection() as conn:
        conn.execute("UPDATE jobs SET lock_until=?,next_check_at=? WHERE id=?",
                     (iso(utc_now() - timedelta(seconds=1)), iso(utc_now() + timedelta(hours=1)), job["id"]))
    # A normal tick must reconcile without restart or another claim.
    assert Journey(repository, doctolib, settings, notifier=FakeNotifier()).run_due() == []
    run = repository.checks(job["id"])[0]
    assert run["id"] == run_id and run["outcome"] == "interrupted"
    assert (run["successful_targets"], run["failed_targets"]) == (1, 1)
    assert len(run["results"]) == 2
    assert lock_owner(repository, job["id"]) is None


def test_worker_uses_snapshot_metadata_for_each_target_after_midrun_edit(tmp_path):
    client, repository, settings, _doctolib = setup_backend(tmp_path)
    job = create_job(client)
    response = client.patch("/api/v1/jobs/" + job["id"], json={
        "target_urls": [URL, URL + "&source=second"], "telegram_enabled": False,
    })
    assert response.status_code == 200
    observed = []

    class MetadataEditingDoctolib(DoctolibClient):
        def check(self, booking_url, search, meta=None, now=None):
            observed.append((meta.practitioner_name, meta.agenda_ids_str))
            if len(observed) == 1:
                targets = repository.get_job(job["id"])["targets"]
                for target in targets:
                    target["practitioner_name"] = "Edited practitioner"
                    target["agenda_ids_str"] = "9999"
                repository.update_job(job["id"], {}, targets=targets)
            return AvailabilityResult(status="no_availability", slot_count=0,
                                      earliest_slot=None, count_complete=True)

    Journey(repository, MetadataEditingDoctolib(), settings, notifier=FakeNotifier()).run_due()
    assert observed == [("Dr. Ada Beispiel", "1234"), ("Dr. Ada Beispiel", "1234")]
    run = repository.checks(job["id"])[0]
    assert len(run["results"]) == 2
    assert all(result["practitioner_name"] == "Dr. Ada Beispiel" and not result["published"]
               for result in run["results"])


def test_same_claim_cannot_overwrite_terminal_target_evidence(tmp_path):
    client, repository, _settings, _doctolib = setup_backend(tmp_path)
    job = create_job(client)
    run_id, claimed = repository.claim_due_jobs(limit=1)[0]
    target = claimed["search_snapshot"]["targets"][0]
    first = repository.insert_result(run_id, claimed, target,
                                    AvailabilityResult(status="no_availability", slot_count=0,
                                                       earliest_slot=None, count_complete=True),
                                    owner_token=claimed["owner_token"])
    repeated = repository.insert_result(run_id, claimed, target,
                                       AvailabilityResult(status="available", slot_count=1,
                                                          earliest_slot=datetime(2026, 10, 15, tzinfo=timezone.utc),
                                                          count_complete=True),
                                       owner_token=claimed["owner_token"])
    assert repeated == first
    results = repository.checks(job["id"])[0]["results"]
    assert len(results) == 1 and results[0]["status"] == "no_availability"
