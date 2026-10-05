"""Recovery must preserve the durable manual budget and queued capability."""
import sqlite3
from pathlib import Path

import pytest

from app import admin
from app.storage.db import Database
from app.storage.repositories import Repository
from test_backend_journey import create_job, setup_backend


def test_restore_preserves_extra_budget_and_paused_pending_intent(tmp_path):
    client, repository, settings, _doctolib = setup_backend(tmp_path)
    job = create_job(client)
    repository.set_status(job['id'], 'paused')
    first = repository.request_check(job['id'])
    run_id, claimed = repository.claim_due_jobs(limit=1)[0]
    repository.finish_run(run_id, job['id'], 0, 0, 300, owner_token=claimed['owner_token'])
    followup = repository.request_check(job['id'])
    assert followup['id'] != first['id']
    assert repository.claim_due_jobs() == []
    archive = tmp_path / 'manual.zip'
    admin.backup(Path(settings.database_path), archive)
    work = tmp_path / 'restore'
    report = admin.verify(archive, work)
    assert report['restored_schema_version'] == 12
    restored = Repository(Database(str(work / 'restored.sqlite3')))
    assert restored.claim_due_jobs() == []
    old = repository.get_job(job['id'])
    new = restored.get_job(job['id'])
    assert new['status'] == 'paused'
    assert new['last_extra_started_at'] == old['last_extra_started_at']
    assert new['check_intent'] == old['check_intent']
    assert new['check_intent']['paused_manual'] == 1


def test_schema6_backup_rejects_missing_capability_column(tmp_path):
    _client, _repository, settings, _doctolib = setup_backend(tmp_path)
    with sqlite3.connect(settings.database_path) as conn:
        conn.execute('ALTER TABLE check_runs DROP COLUMN paused_manual')
    with pytest.raises(admin.AdminError, match='incomplete_checker_schema'):
        admin.backup(Path(settings.database_path), tmp_path / 'invalid.zip')
