"""Raw API contracts: no legacy helper may fill keys or versions here."""
from datetime import timedelta
from dataclasses import replace
from threading import Event, Thread

import pytest
import requests
from fastapi.testclient import TestClient
from app.api.app import create_app

from app.storage.repositories import utc_now, precise_iso
from tests.test_backend_journey import setup_backend, URL


def backend(tmp_path):
    legacy, repository, settings, doctolib = setup_backend(tmp_path)
    return TestClient(legacy.app), repository, settings, doctolib


def draft(**changes):
    return {"name": "Safe save", "target_urls": [URL], **changes}


def create(client, key="draft-1", **changes):
    return client.post("/api/v1/jobs", json=draft(**changes), headers={"Idempotency-Key": key})


@pytest.mark.parametrize('action', ['create', 'update', 'validate'])
def test_metadata_deadline_rejects_gate_without_mutation_or_transport(tmp_path, monkeypatch, action):
    fixture_client, repository, settings, fixture = backend(tmp_path)
    saved = create(fixture_client).json()
    client = TestClient(create_app(replace(settings, target_budget_seconds=0.01), repository=repository))
    original = client.app.state.doctolib
    monkeypatch.setattr('app.services.jobs.get_availability_session', lambda profile: fixture.fixture_session)
    monkeypatch.setattr('app.storage.repositories.time.sleep', lambda seconds: pytest.fail('Unaffordable gate wait'))
    gate = precise_iso(utc_now() + timedelta(seconds=30))
    with repository.database.connection() as conn:
        conn.execute('INSERT OR REPLACE INTO request_gate(singleton_id,next_allowed_at) VALUES(1,?)', (gate,))
    calls = len(fixture.fixture_session.calls)
    if action == 'create':
        response = create(client, key='bounded-create')
    elif action == 'update':
        response = client.patch('/api/v1/jobs/' + saved['id'], json={
            'expected_version': saved['edit_version'], 'target_urls': [URL + '&source=edit']})
    else:
        response = client.post('/api/v1/targets/validate', json={'booking_url': URL})
    assert response.status_code == 502, response.text
    assert 'doctolib_unavailable' in response.text
    assert len(fixture.fixture_session.calls) == calls
    assert original.deadline is None and isinstance(original.metadata_session, requests.Session)
    assert repository.get_job(saved['id'])['edit_version'] == saved['edit_version']
    assert len(repository.list_jobs()) == 1
    with repository.database.connection() as conn:
        assert conn.execute('SELECT next_allowed_at FROM request_gate').fetchone()[0] == gate
    if action == 'create':
        with repository.database.connection() as conn:
            state, retryable = conn.execute(
                "SELECT state,retryable FROM create_operations WHERE key='bounded-create'").fetchone()
        assert state == 'failed' and retryable


def test_create_requires_key_replays_and_rejects_different_body(tmp_path):
    client, repository, _, doctolib = backend(tmp_path)
    assert client.post("/api/v1/jobs", json=draft()).status_code == 422
    first = create(client)
    assert first.status_code == 201, first.text
    call_count = len(doctolib.fixture_session.calls)
    replay = create(client)
    assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
    assert len(doctolib.fixture_session.calls) == call_count
    assert create(client, name="Other body").status_code == 409
    assert len(repository.list_jobs()) == 1


@pytest.mark.parametrize("body", [{"expected_version": None}, {"expected_version": True},
    {"expected_version": "1"}, {"expected_version": 0}, {"expected_version": 1, "mystery": 9},
    {"expected_version": 1, "request_spacing_seconds": None}])
def test_settings_strict_contract(tmp_path, body):
    client, *_ = backend(tmp_path)
    assert client.put("/api/v1/settings", json=body).status_code == 422


def test_versions_conflict_without_overwrite_and_status_writes_require_version(tmp_path):
    client, repository, *_ = backend(tmp_path)
    job = create(client).json()
    path = "/api/v1/jobs/" + job["id"]
    assert client.patch(path, json={"name": "Missing version"}).status_code == 422
    first = client.patch(path, json={"name": "First", "expected_version": job["edit_version"]})
    assert first.status_code == 200
    stale = client.patch(path, json={"name": "Second", "expected_version": job["edit_version"]})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_conflict"
    assert repository.get_job(job["id"])["name"] == "First"
    assert client.post(path + "/pause").status_code == 422
    assert client.request("DELETE", path).status_code == 422
    paused = client.post(path + "/pause", json={"expected_version": first.json()["edit_version"]})
    assert paused.status_code == 200
    assert client.post(path + "/resume", json={"expected_version": first.json()["edit_version"]}).status_code == 409
    # Requesting manual work changes runtime state, not the configuration version.
    check = client.post(path + "/check-now")
    assert check.status_code == 200
    final = client.patch(path, json={"name": "After intent", "expected_version": paused.json()["edit_version"]})
    assert final.status_code == 200
    settings = client.get("/api/v1/settings").json()
    changed = client.put("/api/v1/settings", json={"expected_version": settings["edit_version"], "default_interval_seconds": 600})
    assert changed.status_code == 200
    assert client.put("/api/v1/settings", json={"expected_version": settings["edit_version"], "request_spacing_seconds": 9}).status_code == 409
    assert client.get("/api/v1/settings").json()["request_spacing_seconds"] == settings["request_spacing_seconds"]


def test_concurrent_create_is_pending_and_expired_owner_returns_authoritative_job(tmp_path):
    client, repository, _, doctolib = backend(tmp_path)
    started, release = Event(), Event()
    original = doctolib.resolve
    calls = []

    def blocked(url):
        calls.append(url)
        if len(calls) == 1:
            started.set()
            assert release.wait(10)
        return original(url)

    doctolib.resolve = blocked
    replies = []
    thread = Thread(target=lambda: replies.append(create(client)))
    thread.start()
    try:
        assert started.wait(5)
        pending = create(client)
        assert pending.status_code == 202 and pending.json()["status"] == "in_progress"
        assert len(calls) == 1
        # Change defaults during the unresolved operation. Its first values survive.
        config = client.get("/api/v1/settings").json()
        assert client.put("/api/v1/settings", json={"expected_version": config["edit_version"], "default_interval_seconds": 900}).status_code == 200
        with repository.database.connection() as conn:
            conn.execute("UPDATE create_operations SET lease_until=?", (precise_iso(utc_now() - timedelta(seconds=1)),))
        replacement = create(client)
        assert replacement.status_code == 201, replacement.text
        assert replacement.json()["interval_seconds"] == 300
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert replies[0].status_code == 200
    assert replies[0].json()["id"] == replacement.json()["id"]
    assert len(repository.list_jobs()) == 1


def test_retryable_failure_releases_lease_and_keeps_frozen_defaults(tmp_path):
    client, _, _, doctolib = backend(tmp_path)
    original = doctolib.resolve
    doctolib.resolve = lambda url: (_ for _ in ()).throw(requests.Timeout("private exception"))
    failed = create(client)
    assert failed.status_code == 502 and "private" not in failed.text
    config = client.get("/api/v1/settings").json()
    client.put("/api/v1/settings", json={"expected_version": config["edit_version"], "default_interval_seconds": 1200})
    doctolib.resolve = original
    retried = create(client)
    assert retried.status_code == 201 and retried.json()["interval_seconds"] == 300


def test_invalid_metadata_result_is_replayed_without_resolving_again(tmp_path):
    client, _, _, doctolib = backend(tmp_path)
    calls = []
    def invalid(url):
        calls.append(url)
        raise ValueError("private provider detail")
    doctolib.resolve = invalid
    first, second = create(client), create(client)
    assert first.status_code == second.status_code == 422
    assert first.json() == second.json() and len(calls) == 1
    assert "private" not in first.text


def test_custom_date_null_transition_and_unknown_job_field(tmp_path):
    client, *_ = backend(tmp_path)
    job = create(client, date_mode="custom", earliest_date="2027-01-01", latest_date="2027-01-03").json()
    path = "/api/v1/jobs/" + job["id"]
    assert client.patch(path, json={"expected_version": job["edit_version"], "unknown": 1}).status_code == 422
    updated = client.patch(path, json={"expected_version": job["edit_version"], "date_mode": "first_available", "earliest_date": None, "latest_date": None})
    assert updated.status_code == 200
    assert updated.json()["earliest_date"] is None and updated.json()["latest_date"] is None


def test_worker_claim_and_finish_do_not_conflict_with_configuration_save(tmp_path):
    client, repository, *_ = backend(tmp_path)
    job = create(client).json()
    run_id, claim = repository.claim_due_jobs()[0]
    assert repository.finish_run(run_id, job["id"], 0, 1, 300, owner_token=claim["owner_token"])
    saved = client.patch("/api/v1/jobs/" + job["id"], json={"expected_version": job["edit_version"], "name": "Saved after worker"})
    assert saved.status_code == 200


def test_key_expiry_marks_end_of_replay_guarantee(tmp_path):
    client, repository, *_ = backend(tmp_path)
    initial = create(client).json()
    with repository.database.connection() as conn:
        conn.execute("UPDATE create_operations SET expires_at=?", (precise_iso(utc_now() - timedelta(seconds=1)),))
    fresh = create(client)
    assert fresh.status_code == 201 and fresh.json()["id"] != initial["id"]
    assert len(repository.list_jobs()) == 2


def test_stale_custom_date_editor_conflicts_before_merged_validation(tmp_path):
    client, *_ = backend(tmp_path)
    job = create(client, date_mode="custom", earliest_date="2027-01-01", latest_date="2027-01-03").json()
    path = "/api/v1/jobs/" + job["id"]
    switched = client.patch(path, json={"expected_version": job["edit_version"], "date_mode": "first_available", "earliest_date": None, "latest_date": None})
    assert switched.status_code == 200
    stale = client.patch(path, json={"expected_version": job["edit_version"], "latest_date": "2027-01-04"})
    assert stale.status_code == 409
    assert stale.json()["detail"] == {"code": "version_conflict", "current_version": switched.json()["edit_version"]}


def test_date_mode_change_between_api_and_service_reads_is_still_conflict(tmp_path):
    client, repository, *_ = backend(tmp_path)
    job = create(client, date_mode="custom", earliest_date="2027-01-01", latest_date="2027-01-03").json()
    original_get = repository.get_job
    reads = []
    def changing_get(job_id, **kwargs):
        reads.append(job_id)
        if len(reads) == 2:
            repository.update_job(job_id, {"date_mode": "first_available", "earliest_date": None, "latest_date": None}, expected_version=job["edit_version"])
        return original_get(job_id, **kwargs)
    repository.get_job = changing_get
    stale = client.patch("/api/v1/jobs/" + job["id"], json={"expected_version": job["edit_version"], "latest_date": "2027-01-04"})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "version_conflict"
