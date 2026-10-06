"""Backup rehearsal retains encrypted channels and delivery history."""
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import admin
from app.api.app import create_app
from app.notification_secrets import NotificationSecrets, SecretUnavailable
from app.storage.db import Database, SCHEMA_VERSION
from app.storage.repositories import Repository
from test_backend_journey import setup_backend, create_job, Journey, FakeNotifier, drop_channel_columns


def test_schema8_restore_preserves_encrypted_destination_and_event_routing(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier()).run_due()
    channel = repository.list_channels()['items'][0]
    original = repository.get_channel(channel['id'], private=True)
    archive = tmp_path / 'channels.zip'
    admin.backup(Path(settings.database_path), archive)
    work = tmp_path / 'restore'
    report = admin.verify(archive, work)
    assert report['backup_schema_version'] == report['restored_schema_version'] == SCHEMA_VERSION
    restored = Repository(Database(str(work / 'restored.sqlite3')))
    copied = restored.get_channel(channel['id'], private=True)
    assert copied == original
    assert restored.list_jobs()[0]['notification_channel_ids'] == [channel['id']]
    assert restored.alerts()[0]['status'] == 'sent'
    secrets = NotificationSecrets(settings.notification_secret_key)
    assert secrets.decrypt(copied['token_ciphertext']).startswith('123456:')
    for key in ('', Fernet.generate_key().decode()):
        with pytest.raises(SecretUnavailable):
            NotificationSecrets(key).decrypt(copied['token_ciphertext'])
        app = create_app(replace(settings, notification_secret_key=key), restored, doctolib)
        public = TestClient(app).get('/api/v1/channels').json()['items'][0]
        assert public['usable'] is False
        assert 'token_ciphertext' not in public and 'chat_ciphertext' not in public
    assert settings.notification_secret_key.encode() not in archive.read_bytes()


def test_schema7_backup_migrates_without_replaying_current_episode(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier(False)).run_due()
    original = repository.alerts()[0]
    with repository.database.connection() as conn:
        drop_channel_columns(conn)
        conn.execute('UPDATE schema_version SET version=7')
    archive = tmp_path / 'legacy.zip'
    assert admin.backup(Path(settings.database_path), archive)['schema_version'] == 7
    work = tmp_path / 'migrated'
    assert admin.verify(archive, work)['restored_schema_version'] == SCHEMA_VERSION
    restored = Repository(Database(str(work / 'restored.sqlite3')))
    historical = restored.alerts()[0]
    assert historical['id'] == original['id']
    assert historical['attempt_count'] == original['attempt_count']
    assert historical['status'] == 'cancelled'
    assert historical['error_summary'] == 'migration_legacy_requires_import'
    with restored.database.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM availability_events').fetchone()[0] >= 1
        assert conn.execute('SELECT COUNT(*) FROM job_channels').fetchone()[0] == 0


def test_schema10_backup_refuses_missing_test_attempt_fence(tmp_path):
    _, repository, settings, _ = setup_backend(tmp_path)
    with sqlite3.connect(settings.database_path) as conn:
        conn.execute('ALTER TABLE channel_tests DROP COLUMN claim_until')
    with pytest.raises(admin.AdminError, match='incomplete_checker_schema'):
        admin.backup(Path(settings.database_path), tmp_path / 'invalid.zip')


def test_schema9_restore_adds_email_recipient_and_empty_smtp_transport(tmp_path):
    source = Database(str(tmp_path / 'schema9.sqlite3'))
    source.initialize()
    with source.connection() as conn:
        conn.execute('ALTER TABLE notification_channels DROP COLUMN email_recipient_ciphertext')
        conn.execute('DROP TABLE smtp_transport')
        conn.execute('UPDATE schema_version SET version=9')
    archive = tmp_path / 'schema9.zip'
    assert admin.backup(Path(source.path), archive)['schema_version'] == 9
    work = tmp_path / 'schema9-restore'
    assert admin.verify(archive, work)['restored_schema_version'] == SCHEMA_VERSION
    with sqlite3.connect(work / 'restored.sqlite3') as conn:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(notification_channels)')}
        assert 'email_recipient_ciphertext' in columns
        transport = conn.execute('SELECT enabled,port,tls_mode FROM smtp_transport WHERE singleton_id=1').fetchone()
        assert transport == (0,587,'starttls')
