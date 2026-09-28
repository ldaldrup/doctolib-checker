import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.doctolib import DoctolibClient
from app.services.checks import CheckService
from app.settings import Settings
from app.storage.db import Database
from app.storage.repositories import Repository, iso, parse_time, utc_now


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
            return FixtureResponse({
                "total": 1,
                "next_slot": "2026-10-25T08:00:00+02:00",
                "availabilities": [{"date": "2026-10-25", "slots": [
                    {"start_time": "2026-10-25T08:00:00+02:00"}
                ]}],
            })
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
        return self.successful, None if self.successful else "telegram_timeout"


class RaisingNotifier:
    def __call__(self, settings, alert):
        raise RuntimeError("https://api.telegram.org/botprivate-token/sendMessage")


class RaisingDoctolib(DoctolibClient):
    """Use actual fixture checks while simulating one failing target request."""

    def check(self, booking_url, search, meta=None, now=None):
        if "source=fail" in booking_url:
            raise requests.Timeout()
        return super().check(booking_url, search, meta=meta, now=now)


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


def setup_backend(tmp_path, status="available"):
    database = Database(str(tmp_path / "checker.sqlite3"))
    database.initialize()
    repository = Repository(database)
    settings = Settings(database_path=str(tmp_path / "checker.sqlite3"),
                        telegram_bot_token="test-token-never-return-this",
                        telegram_chat_id="12345", telegram_enabled=True)
    doctolib = FixtureDoctolib(no_availability=status == "no_availability")
    app = create_app(settings=settings, repository=repository, doctolib=doctolib)
    return TestClient(app), repository, settings, doctolib


def create_job(client):
    response = client.post("/api/v1/jobs", json={
        "name": "My appointment search",
        "target_urls": [URL],
        "interval_seconds": 300,
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
    checker = CheckService(repository, doctolib, settings, notifier=notifier)
    outcomes = checker.run_due()
    assert outcomes == [{"successful_targets": 1, "failed_targets": 0}]
    assert len(notifier.sent) == 1

    check_rows = client.get("/api/v1/jobs/" + job["id"] + "/checks").json()
    alert_rows = client.get("/api/v1/alerts").json()
    assert check_rows[0]["status"] == "available"
    assert check_rows[0]["slot_count"] == 3
    assert check_rows[0]["earliest_slot"] == "2026-10-15T07:30:00+00:00"
    assert check_rows[0]["run_outcome"] == "completed"
    assert check_rows[0]["successful_targets"] == 1
    assert check_rows[0]["booking_url"] == URL
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


def test_job_update_rejects_null_values_instead_of_failing_in_storage(tmp_path):
    client, _repository, _settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)

    response = client.patch("/api/v1/jobs/" + job["id"], json={"name": None})

    assert response.status_code == 422
    assert "name cannot be null" in response.text
    assert client.get("/api/v1/jobs/" + job["id"]).json()["name"] == "My appointment search"
    assert len(doctolib.fixture_session.calls) == 1


def test_no_match_stays_active_and_does_not_alert(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path, status="no_availability")
    job = create_job(client)
    notifier = FakeNotifier()
    outcomes = CheckService(repository, doctolib, settings, notifier=notifier).run_due()
    assert outcomes == [{"successful_targets": 1, "failed_targets": 0}]
    assert client.get("/api/v1/jobs/" + job["id"] + "/checks").json()[0]["status"] == "no_availability"
    assert client.get("/api/v1/jobs").json()[0]["status"] == "active"
    assert client.get("/api/v1/alerts").json() == []
    assert notifier.sent == []


def test_pause_during_request_records_result_but_suppresses_alert(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    pausing = PausingDoctolib(repository, doctolib.fixture_session, job["id"])
    notifier = FakeNotifier()

    CheckService(repository, pausing, settings, notifier=notifier).run_due()

    result = client.get("/api/v1/jobs/" + job["id"] + "/checks").json()[0]
    assert result["status"] == "available"
    assert repository.get_job(job["id"])["status"] == "paused"
    assert client.get("/api/v1/alerts").json() == []
    assert notifier.sent == []


def test_edit_applies_to_next_target_request_in_same_run(tmp_path):
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

    CheckService(repository, updating, settings, notifier=FakeNotifier()).run_due()

    assert [search["insurance_sector"] for search in updating.searches] == ["public", "private"]
    assert [search["telehealth"] for search in updating.searches] == [False, True]


def test_pause_resume_and_check_now_obey_minimum_interval_after_restart(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    notifier = FakeNotifier()
    CheckService(repository, doctolib, settings, notifier=notifier).run_due()

    queued = client.post("/api/v1/jobs/" + job["id"] + "/check-now")
    assert queued.status_code == 200
    assert parse_time(queued.json()["next_check_at"]) >= utc_now() + timedelta(seconds=295)
    assert client.post("/api/v1/jobs/" + job["id"] + "/pause").json()["status"] == "paused"
    assert client.post("/api/v1/jobs/" + job["id"] + "/resume").json()["status"] == "active"

    reopened = Repository(Database(settings.database_path))
    assert reopened.get_job(job["id"])["last_finished_at"]
    assert reopened.get_job(job["id"])["status"] == "active"
    assert reopened.checks(job["id"])[0]["status"] == "available"
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
    assert client.post("/api/v1/jobs/" + job["id"] + "/check-now").status_code == 409

    repository.finish_run(run_id, job["id"], 0, 0, claimed_job["interval_seconds"])
    current = repository.get_job(job["id"])
    assert current["interval_seconds"] == 600
    assert parse_time(current["next_check_at"]) >= utc_now() + timedelta(seconds=595)


def test_delete_keeps_history_available_through_job_id(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path, status="no_availability")
    job = create_job(client)
    CheckService(repository, doctolib, settings, notifier=FakeNotifier()).run_due()
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

    CheckService(repository, doctolib, settings, notifier=notifier).run_due()

    result = client.get("/api/v1/jobs/" + job["id"] + "/checks").json()[0]
    alert = client.get("/api/v1/alerts").json()[0]
    assert result["status"] == "available"
    assert alert["status"] == "failed"
    assert alert["attempt_count"] == 1
    assert alert["error_summary"] == "telegram_timeout"

    with repository.database.connection() as conn:
        conn.execute("UPDATE alerts SET next_attempt_at=? WHERE id=?", (iso(utc_now()), alert["id"]))
    retry_notifier = FakeNotifier(successful=True)
    CheckService(repository, doctolib, settings, notifier=retry_notifier).dispatch_pending()
    retried = client.get("/api/v1/alerts").json()[0]
    assert retried["status"] == "sent"
    assert retried["attempt_count"] == 2
    assert len(retry_notifier.sent) == 1


def test_notifier_exception_is_sanitized_and_saved_for_retry(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)

    CheckService(repository, doctolib, settings, notifier=RaisingNotifier()).run_due()

    alert = client.get("/api/v1/alerts").json()[0]
    assert alert["status"] == "failed"
    assert alert["error_summary"] == "telegram_delivery_error"
    assert b"private-token" not in client.get("/api/v1/alerts").content
    assert client.get("/api/v1/jobs/" + job["id"] + "/checks").json()[0]["status"] == "available"


def test_repeated_identical_slot_does_not_create_another_alert(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    CheckService(repository, doctolib, settings, notifier=FakeNotifier()).run_due()

    target = repository.get_job(job["id"])["targets"][0]
    check = repository.checks(job["id"])[0]
    duplicate = repository.create_alert(
        job, target, check["id"], datetime.fromisoformat(check["earliest_slot"])
    )

    assert duplicate is None
    assert len(client.get("/api/v1/alerts").json()) == 1


def test_one_target_failure_does_not_discard_another_target_result(tmp_path):
    database = Database(str(tmp_path / "checker.sqlite3"))
    database.initialize()
    repository = Repository(database)
    settings = Settings(database_path=str(tmp_path / "checker.sqlite3"))
    doctolib = RaisingDoctolib(session=FixtureSession())
    client = TestClient(create_app(settings=settings, repository=repository, doctolib=doctolib))
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

    CheckService(repository, doctolib, settings, notifier=FakeNotifier()).run_due()
    history = client.get("/api/v1/jobs/" + job["id"] + "/checks")

    assert history.status_code == 200
    assert {row["status"] for row in history.json()} == {"available", "error"}


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
