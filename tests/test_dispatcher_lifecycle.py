"""Acceptance and restart boundaries through the actual dispatcher service/CLI."""

import json

import pytest

from app import dispatcher
from app.services.delivery import DeliveryService
from test_delivery import pending, expire_claim


def test_provider_acceptance_then_crash_never_automatically_resends(tmp_path):
    _client, repository, settings, _doctolib, _job = pending(tmp_path)
    accepted = []

    def provider(_settings, alert):
        accepted.append(alert['id'])
        raise SystemExit('simulated process exit after acceptance')

    with pytest.raises(SystemExit):
        DeliveryService(repository, settings, sender=provider).run_once()
    alert = repository.alerts()[0]
    assert alert['attempt_count'] == 1 and alert['attempt_started_at']
    expire_claim(repository, alert['id'])
    assert not DeliveryService(repository, settings, sender=provider).run_once()
    assert accepted == [alert['id']]
    assert repository.alerts()[0]['delivery_state'] == 'uncertain'


def test_cli_uuid_recovery_only_requeues_without_sending(tmp_path, monkeypatch, capsys):
    _client, repository, settings, _doctolib, _job = pending(tmp_path)
    alert = repository.claim_alert()
    repository.begin_alert_attempt(alert['id'], alert['owner_token'])
    repository.finish_delivery(alert['id'], alert['owner_token'], 'action_required', 'telegram_rejected_401')
    monkeypatch.setattr(dispatcher.Settings, 'from_env', lambda: settings)
    assert dispatcher.main(['--recover', alert['id']]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {'alert_id': alert['id'], 'status': 'requeued'}
    current = repository.alerts()[0]
    assert current['status'] == 'pending' and current['attempt_count'] == 1
    assert current['delivery_epoch_attempts'] == 0
