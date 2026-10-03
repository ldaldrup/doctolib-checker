"""Durable creation outcomes and config versions survive offline recovery."""
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import admin
from app.api.app import create_app
from app.storage.db import Database
from app.storage.repositories import Repository
from test_backend_journey import setup_backend, URL


def test_restored_create_replays_without_metadata_and_preserves_settings_version(tmp_path):
    helper, repository, settings, doctolib = setup_backend(tmp_path)
    client = TestClient(helper.app)
    payload={'name':'Recoverable create','target_urls':[URL]}
    headers={'Idempotency-Key':'restore-operation'}
    first=client.post('/api/v1/jobs',json=payload,headers=headers)
    assert first.status_code==201
    config=client.get('/api/v1/settings').json()
    saved=client.put('/api/v1/settings',json={'expected_version':config['edit_version'],'default_interval_seconds':600}).json()
    archive=tmp_path/'create.zip'
    admin.backup(Path(settings.database_path),archive)
    work=tmp_path/'verify'
    assert admin.verify(archive,work)['restored_schema_version']==8
    restored=Repository(Database(str(work/'restored.sqlite3')))
    doctolib.resolve=lambda _url: pytest.fail('metadata on completed replay')
    with TestClient(create_app(replace(settings,database_path=restored.database.path),restored,doctolib)) as app:
        replay=app.post('/api/v1/jobs',json=payload,headers=headers)
        assert replay.status_code==200 and replay.json()['id']==first.json()['id']
        assert len(app.get('/api/v1/jobs').json())==1
        assert app.get('/api/v1/settings').json()['edit_version']==saved['edit_version']


def test_backup_rejects_schema7_without_create_lease_structure(tmp_path):
    _, _, settings, _ = setup_backend(tmp_path)
    with sqlite3.connect(settings.database_path) as conn:
        conn.execute('ALTER TABLE create_operations DROP COLUMN generation')
    with pytest.raises(admin.AdminError,match='incomplete_checker_schema'):
        admin.backup(Path(settings.database_path),tmp_path/'invalid.zip')
