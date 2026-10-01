"""Adversarial retry checks for a freshly revalidated existing alert episode."""

from datetime import datetime

from app.services.checks import CheckService
from app.storage.repositories import iso, utc_now
from test_backend_journey import FakeNotifier, FixtureResponse, create_job, record_result, setup_backend


def test_retry_defers_payload_when_same_episode_is_refreshed(tmp_path, monkeypatch):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    CheckService(repository, doctolib, settings, notifier=FakeNotifier(False)).run_due()
    original = repository.alerts()[0]
    slot = datetime.fromisoformat(original["earliest_slot"])
    with repository.database.connection() as conn:
        conn.execute("UPDATE alerts SET next_attempt_at=? WHERE id=?", (iso(utc_now()), original["id"]))
    messages = []

    class Session:
        def post(self, endpoint, **kwargs):
            messages.append(kwargs["json"]["text"])
            return FixtureResponse({"ok": len(messages) > 1}, status_code=503 if len(messages) == 1 else 200)

    def refresh_during_backoff(_seconds):
        targets = []
        for target in repository.get_job(job["id"])["targets"]:
            target = dict(target)
            target["agenda_ids_str"] = target["agenda_ids"]
            target["practitioner_name"] = "New practitioner label"
            targets.append(target)
        repository.update_job(job["id"], {}, targets=targets)
        target, result_id = record_result(repository, job, "available", slot, 3)
        assert repository.create_alert(job, target, result_id, slot, owner_token=job["owner_token"]) == original["id"]

    monkeypatch.setattr("app.notifications.requests.Session", Session)
    monkeypatch.setattr("app.notifications.time.sleep", refresh_during_backoff)
    checker = CheckService(repository, doctolib, settings)
    checker.dispatch_pending()
    assert len(messages) == 1  # The old payload must never retry against newer evidence.
    assert repository.alerts()[0]["status"] == "failed"
    with repository.database.connection() as conn:
        conn.execute("UPDATE alerts SET next_attempt_at=? WHERE id=?", (iso(utc_now()), original["id"]))
    checker.dispatch_pending()
    assert len(messages) == 2 and "New practitioner label" in messages[1]
    assert repository.alerts()[0]["status"] == "sent"
    assert len(repository.alerts()) == 1
