from datetime import datetime, timedelta, timezone

import pytest

from app.notifications import DeliveryOutcome
from app.notification_secrets import NotificationSecrets
from app.services.checks import CheckService
from app.services.delivery import DeliveryService
from app.storage.repositories import Repository
from test_backend_journey import create_job, record_result, setup_backend


@pytest.fixture
def clock(monkeypatch):
    current = [datetime(2026, 10, 5, 20, 30, tzinfo=timezone.utc)]
    monkeypatch.setattr("app.storage.repositories.utc_now", lambda: current[0])
    return current


class Sender:
    def __init__(self):
        self.sent = []

    def __call__(self, _settings, alert):
        self.sent.append(dict(alert))
        return DeliveryOutcome("sent")


def make_job(tmp_path, *, enabled=True):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    channel_id = repository.list_channels()["items"][0]["id"]
    job = client.patch("/api/v1/jobs/" + job["id"], json={
        "notification_channel_ids": [channel_id],
        "quiet_hours_enabled": enabled,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "07:00",
    }).json()
    return client, repository, settings, doctolib, job


def add_channel(repository, settings, name, token, chat):
    secrets = NotificationSecrets(settings.notification_secret_key)
    channel, _created = repository.create_channel({
        "name": name,
        "enabled": True,
        "token_ciphertext": secrets.encrypt(token),
        "chat_ciphertext": secrets.encrypt(chat),
        "destination_identity": secrets.identity(token.split(":", 1)[0] + ":" + chat),
    }, name, name)
    return channel["id"]


def test_confirmation_must_match_slot_and_be_fresh_not_future_dated():
    now = datetime(2026, 10, 5, 20, 30, tzinfo=timezone.utc)
    evidence = {"latest_result_status": "available", "latest_earliest_slot": "2026-10-10T08:00:00+00:00",
                "earliest_slot": "2026-10-10T08:00:00+00:00", "latest_checked_at": now.isoformat(),
                "interval_seconds": 300}
    assert Repository._delivery_confirmed(evidence, now)
    assert not Repository._delivery_confirmed({**evidence, "latest_checked_at": (now + timedelta(seconds=1)).isoformat()}, now)
    assert not Repository._delivery_confirmed({**evidence, "latest_earliest_slot": "2026-10-11T08:00:00+00:00"}, now)
    assert not Repository._delivery_confirmed({**evidence, "latest_checked_at": (now - timedelta(seconds=301)).isoformat()}, now)


def test_policy_edit_preserves_search_and_does_not_queue_a_check(tmp_path):
    client, repository, _settings, _doctolib, job = make_job(tmp_path, enabled=False)
    before = (job["search_revision"], job["policy_version"], job["edit_version"])
    saved = client.patch("/api/v1/jobs/" + job["id"], json={"quiet_hours_enabled": True}).json()
    assert saved["search_revision"] == before[0]
    assert saved["policy_version"] == before[1] + 1
    assert saved["edit_version"] == before[2] + 1
    assert saved["check_intent"] is None
    invalid = client.patch("/api/v1/jobs/" + job["id"], json={"quiet_hours_start": "07:00", "quiet_hours_end": "07:00"})
    assert invalid.status_code == 422
    preview = client.post("/api/v1/quiet-hours/preview", json={
        "enabled": True, "start": "22:00", "end": "07:00", "time_zone": "Europe/Berlin",
    })
    assert preview.status_code == 200
    assert "fresh confirmation" in preview.json()["preview"]
    assert "Monitoring continues" in preview.json()["preview"]
    assert "No message sent" in preview.json()["preview"]
    disabled = client.post("/api/v1/quiet-hours/preview", json={
        "enabled": False, "start": "22:00", "end": "07:00", "time_zone": "Europe/Berlin",
    })
    assert "Times use Europe/Berlin" in disabled.json()["preview"]
    assert "Monitoring continues" in disabled.json()["preview"]


def test_held_alert_waits_for_one_normal_refresh_then_releases(tmp_path, clock):
    client, repository, settings, doctolib, job = make_job(tmp_path)
    assert CheckService(repository, doctolib, settings).run_due() == [{"successful_targets": 1, "failed_targets": 0}]
    sender = Sender()
    delivery = DeliveryService(repository, settings, sender=sender)
    assert delivery.run_once() is False
    alert = repository.alerts()[0]
    assert alert["status"] == "pending"
    assert alert["quiet_state"] == "held"
    assert alert["quiet_until"] == "2026-10-06T05:00:00.000000+00:00"
    assert sender.sent == []

    clock[0] = datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)
    assert delivery.run_once() is False
    waiting = repository.alerts()[0]
    assert waiting["quiet_state"] == "waiting_for_fresh_check"
    intent = repository.get_job(job["id"])["check_intent"]
    assert intent["status"] == "queued" and intent["triggered_by"] == "quiet_hours"
    assert delivery.run_once() is False
    assert repository.get_job(job["id"])["check_intent"]["id"] == intent["id"]

    assert CheckService(repository, doctolib, settings).run_due() == [{"successful_targets": 1, "failed_targets": 0}]
    assert delivery.run_once() is True
    assert len(sender.sent) == 1
    sent = repository.alerts()[0]
    assert sent["status"] == "sent" and sent["quiet_state"] == "released"
    api_alert = client.get("/api/v1/alerts").json()[0]
    assert api_alert["quiet_state"] == "released"
    assert api_alert["quiet_until"] is None


def test_manual_check_promotes_queued_quiet_refresh_and_keeps_extra_run_floor(tmp_path, clock):
    client, repository, settings, doctolib, job = make_job(tmp_path)
    CheckService(repository, doctolib, settings).run_due()
    delivery = DeliveryService(repository, settings, sender=Sender())
    assert delivery.run_once() is False
    clock[0] = datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)
    assert delivery.run_once() is False
    quiet_intent = repository.get_job(job["id"])["check_intent"]
    assert quiet_intent["status"] == "queued" and quiet_intent["triggered_by"] == "quiet_hours"

    with repository.database.connection() as conn:
        conn.execute("UPDATE jobs SET last_extra_started_at=? WHERE id=?",
                     ((clock[0] - timedelta(seconds=10)).isoformat(), job["id"]))
        conn.execute("UPDATE check_intents SET eligible_at=? WHERE id=?",
                     ((clock[0] + timedelta(hours=1)).isoformat(), quiet_intent["id"]))
    promoted = client.post("/api/v1/jobs/" + job["id"] + "/check-now").json()
    assert promoted["id"] == quiet_intent["id"] and promoted["coalesced"]
    assert promoted["triggered_by"] == "manual"
    assert promoted["eligible_at"] == "2026-10-06T05:00:50.000000+00:00"


def test_quiet_episodes_coalesce_per_selected_destination(tmp_path, clock):
    client, repository, settings, doctolib, job = make_job(tmp_path)
    first_channel = job["notification_channel_ids"][0]
    second_channel = add_channel(repository, settings, "Second Telegram", "987654:second_fixture", "67890")
    job = client.patch("/api/v1/jobs/" + job["id"], json={
        "notification_channel_ids": [first_channel, second_channel],
    }).json()
    assert CheckService(repository, doctolib, settings).run_due() == [{"successful_targets": 1, "failed_targets": 0}]

    for day in (18, 19):
        record_result(repository, job, "available", datetime(2026, 10, day, 8, tzinfo=timezone.utc), 2)

    alerts = repository.alerts()
    assert len(alerts) == 6
    assert sum(alert["status"] == "cancelled" for alert in alerts) == 4
    pending = [alert for alert in alerts if alert["status"] == "pending"]
    assert {alert["channel_config_id"] for alert in pending} == {first_channel, second_channel}
    assert all(alert["quiet_state"] == "held" for alert in pending)


def test_successful_destination_is_not_replayed_after_policy_edit(tmp_path, clock):
    client, repository, settings, doctolib, job = make_job(tmp_path, enabled=False)
    assert CheckService(repository, doctolib, settings).run_due() == [{"successful_targets": 1, "failed_targets": 0}]
    sender = Sender()
    delivery = DeliveryService(repository, settings, sender=sender)
    assert delivery.run_once() is True
    assert len(sender.sent) == 1

    client.patch("/api/v1/jobs/" + job["id"], json={
        "quiet_hours_enabled": True,
        "quiet_hours_start": "00:00",
        "quiet_hours_end": "23:59",
    })
    assert delivery.run_once() is False
    assert len(sender.sent) == 1
    assert repository.alerts()[0]["status"] == "sent"


def test_quiet_release_preserves_delivery_retry_backoff(tmp_path, clock):
    client, repository, settings, doctolib, job = make_job(tmp_path)
    job = client.patch("/api/v1/jobs/" + job["id"], json={
        "interval_seconds": 7200,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "22:35",
    }).json()
    assert CheckService(repository, doctolib, settings).run_due() == [{"successful_targets": 1, "failed_targets": 0}]
    sender = Sender()
    delivery = DeliveryService(repository, settings, sender=sender)
    assert delivery.run_once() is False

    retry_at = datetime(2026, 10, 5, 21, 40, tzinfo=timezone.utc)
    with repository.database.connection() as conn:
        conn.execute("""UPDATE alerts SET status='failed',delivery_state='retry',attempt_count=1,
            delivery_epoch_attempts=1,next_attempt_at=?""", (retry_at.isoformat(),))
    clock[0] = datetime(2026, 10, 5, 21, 35, tzinfo=timezone.utc)
    assert delivery.run_once() is False
    alert = repository.alerts()[0]
    assert alert["quiet_state"] == "released"
    assert alert["next_attempt_at"] == retry_at.isoformat()
    assert sender.sent == []

    clock[0] = retry_at
    assert delivery.run_once() is True
    assert len(sender.sent) == 1


def test_job_channel_status_keeps_older_held_target_visible(tmp_path, clock):
    client, repository, settings, doctolib, job = make_job(tmp_path)
    job = client.patch("/api/v1/jobs/" + job["id"], json={
        "target_urls": [job["targets"][0]["booking_url"], job["targets"][0]["booking_url"] + "&source=second"],
    }).json()
    assert CheckService(repository, doctolib, settings).run_due() == [{"successful_targets": 2, "failed_targets": 0}]
    assert len(repository.alerts()) == 2

    with repository.database.connection() as conn:
        newest = conn.execute("SELECT id FROM alerts WHERE job_id=? ORDER BY rowid DESC LIMIT 1", (job["id"],)).fetchone()[0]
        conn.execute("UPDATE alerts SET status='sent',quiet_state='released',quiet_until=NULL WHERE id=?", (newest,))

    channel = repository.get_job(job["id"])["notification_channels"][0]
    assert channel["delivery_status"] == "held"
    assert channel["quiet_until"] == "2026-10-06T05:00:00.000000+00:00"


def test_paused_manual_alert_needs_another_manual_check_when_stale(tmp_path, clock):
    client, repository, settings, doctolib, job = make_job(tmp_path)
    repository.set_status(job["id"], "paused", expected_version=job["edit_version"])
    first = client.post("/api/v1/jobs/" + job["id"] + "/check-now").json()
    assert first["paused_manual"] == 1
    assert CheckService(repository, doctolib, settings).run_due() == [{"successful_targets": 1, "failed_targets": 0}]
    sender = Sender()
    delivery = DeliveryService(repository, settings, sender=sender)
    assert delivery.run_once() is False
    assert repository.alerts()[0]["quiet_state"] == "held"

    clock[0] = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)
    assert delivery.run_once() is False
    assert repository.alerts()[0]["quiet_state"] == "needs_manual_check"
    assert repository.get_job(job["id"])["status"] == "paused"
    assert repository.get_job(job["id"])["check_intent"]["status"] == "completed"

    second = client.post("/api/v1/jobs/" + job["id"] + "/check-now").json()
    assert second["status"] == "queued" and second["paused_manual"] == 1
    assert CheckService(repository, doctolib, settings).run_due() == [{"successful_targets": 1, "failed_targets": 0}]
    assert delivery.run_once() is True
    assert len(sender.sent) == 1
    assert repository.get_job(job["id"])["status"] == "paused"


def test_disappearance_cancels_held_alert_without_release(tmp_path, clock):
    _client, repository, settings, doctolib, job = make_job(tmp_path)
    CheckService(repository, doctolib, settings).run_due()
    sender = Sender()
    delivery = DeliveryService(repository, settings, sender=sender)
    assert delivery.run_once() is False
    doctolib.fixture_session.no_availability = True
    clock[0] += timedelta(seconds=300)
    assert CheckService(repository, doctolib, settings).run_due() == [{"successful_targets": 1, "failed_targets": 0}]
    assert delivery.run_once() is False
    alert = repository.alerts()[0]
    assert alert["status"] == "cancelled"
    assert alert["quiet_state"] == "cancelled" and alert["quiet_until"] is None
    assert alert["error_summary"] == "availability_disappeared"
    assert sender.sent == []
