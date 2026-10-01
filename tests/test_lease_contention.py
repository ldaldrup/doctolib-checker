"""Lease validity must be evaluated after acquiring SQLite's writer lock."""

import threading
from contextlib import contextmanager
from datetime import timedelta

from app.storage.repositories import precise_iso, utc_now
from test_backend_journey import create_job, setup_backend


def test_renewal_cannot_resurrect_lease_expired_while_waiting_for_writer(tmp_path, monkeypatch):
    client, repository, _settings, _doctolib = setup_backend(tmp_path)
    job = create_job(client)
    run_id, claim = repository.claim_due_jobs(limit=1)[0]
    clock = [utc_now()]
    monkeypatch.setattr("app.storage.repositories.utc_now", lambda: clock[0])
    with repository.database.connection() as conn:
        conn.execute("UPDATE jobs SET lock_until=? WHERE id=?",
                     (precise_iso(clock[0] + timedelta(seconds=1)), job["id"]))

    statement_started = threading.Event()
    original_connection = repository.database.connection
    main_thread = threading.current_thread()

    @contextmanager
    def traced_connection():
        with original_connection() as conn:
            if threading.current_thread() is not main_thread:
                def trace(statement):
                    if statement.startswith("BEGIN IMMEDIATE") or statement.lstrip().startswith("UPDATE jobs"):
                        statement_started.set()
                conn.set_trace_callback(trace)
            yield conn

    monkeypatch.setattr(repository.database, "connection", traced_connection)
    renewed = []
    errors = []

    def renew():
        try:
            renewed.append(repository.renew_job_lock(job["id"], run_id, claim["owner_token"]))
        except Exception as exc:
            errors.append(exc)

    with original_connection() as writer:
        writer.execute("BEGIN IMMEDIATE")
        worker = threading.Thread(target=renew)
        worker.start()
        assert statement_started.wait(timeout=2), "Renewal did not reach its blocked SQL statement"
        # Advance deterministically while the writer lock remains held.
        clock[0] += timedelta(seconds=2)
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors == []
    assert renewed == [False]
    with repository.database.connection() as conn:
        lock_until = conn.execute("SELECT lock_until FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
    assert lock_until == precise_iso(clock[0] - timedelta(seconds=1))
