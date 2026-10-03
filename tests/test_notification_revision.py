"""Each durable retry refreshes its payload and uses current revision evidence."""

from datetime import datetime

from app.notifications import DeliveryOutcome
from app.services.checks import CheckService
from app.services.delivery import DeliveryService
from app.storage.repositories import iso, utc_now
from test_backend_journey import create_job, record_result, setup_backend


def test_retry_turn_uses_freshly_revalidated_payload_for_same_episode(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    job = create_job(client)
    CheckService(repository, doctolib, settings).run_due()
    original = repository.alerts()[0]
    slot = datetime.fromisoformat(original['earliest_slot'])
    messages = []

    def sender(_settings, alert):
        messages.append(alert['practitioner_name'])
        return DeliveryOutcome('retry', 'telegram_connect_error') if len(messages) == 1 else DeliveryOutcome('sent')

    dispatcher = DeliveryService(repository, settings, sender=sender)
    assert dispatcher.run_once()
    assert len(messages) == 1
    targets = []
    for target in repository.get_job(job['id'])['targets']:
        target = dict(target)
        target['agenda_ids_str'] = target['agenda_ids']
        target['practitioner_name'] = 'New practitioner label'
        targets.append(target)
    repository.update_job(job['id'], {}, targets=targets)
    with repository.database.connection() as conn:
        conn.execute('UPDATE alerts SET next_attempt_at=? WHERE id=?', (iso(utc_now()), original['id']))
    assert not dispatcher.run_once()  # Old evidence cannot dispatch after the edit.
    target, result_id = record_result(repository, job, 'available', slot, 3)
    assert repository.create_alert(job, target, result_id, slot, owner_token=job['owner_token']) is None
    assert not dispatcher.run_once()  # Cancellation is never revived implicitly.
    assert repository.recover_alert(original['id'])
    assert dispatcher.run_once()
    assert messages[-1] == 'New practitioner label' and len(messages) == 2
    assert repository.alerts()[0]['status'] == 'sent'
    assert len(repository.alerts()) == 1
