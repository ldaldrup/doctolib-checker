"""History export remains separate from backup and excludes secret storage."""
import json

import pytest

from app.admin import AdminError
from app.history_export import export_history
from app.storage.db import SCHEMA_VERSION
from tests.test_admin import seeded, rows


def test_default_export_preserves_history_and_redacts_urls_and_credentials(tmp_path):
    source, repository = seeded(tmp_path)
    before = rows(source)
    # Legacy error strings may contain credentials; they are not export fields.
    with repository.database.connection() as conn:
        conn.execute("UPDATE check_results SET error_message='secret-value',error_code='secret-value'")
        conn.execute("UPDATE alerts SET error_summary='secret-value',claim_owner_token='private-owner'")
    output = tmp_path / 'history.json'
    report = export_history(source, output)
    data = json.loads(output.read_text())
    assert data['schema_version'] == SCHEMA_VERSION and data['format_version'] == 1
    assert data['restorable_backup'] is False and not data['private_urls_included']
    assert len(data['jobs']) == report['counts']['jobs'] == 2
    assert data['check_runs'] and data['check_results'] and data['alerts']
    assert 'booking_url' not in output.read_text()
    assert 'ciphertext' not in output.read_text() and 'secret-value' not in output.read_text()
    assert 'private-owner' not in output.read_text()
    assert output.stat().st_mode & 0o777 == 0o600
    after = rows(source)
    assert before['jobs'] == after['jobs'] and before['targets'] == after['targets']


def test_explicit_private_export_requires_protected_destination_and_filters_jobs(tmp_path):
    source, repository = seeded(tmp_path)
    job = repository.list_jobs()[0]
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    output = private / 'history.json'
    export_history(source, output, job_ids=[job['id']], include_private_urls=True)
    data = json.loads(output.read_text())
    assert len(data['jobs']) == 1 and data['jobs'][0]['id'] == job['id']
    assert data['private_urls_included']
    assert data['targets'][0]['booking_url'] == job['targets'][0]['booking_url']
    assert all(row['job_id'] == job['id'] for table in ('targets','check_runs','check_results','alerts') for row in data[table])
    assert output.stat().st_mode & 0o777 == 0o600
    public = tmp_path / 'public'
    public.mkdir(mode=0o755)
    with pytest.raises(AdminError, match='private_export_requires_private_directory'):
        export_history(source, public / 'unsafe.json', include_private_urls=True)
    with pytest.raises(AdminError, match='destination_exists'):
        export_history(source, output)


def test_export_missing_job_or_existing_destination_leaves_source_and_output_unchanged(tmp_path):
    source, _ = seeded(tmp_path)
    before = rows(source)
    with pytest.raises(AdminError, match='export_job_not_found'):
        export_history(source, tmp_path / 'absent.json', job_ids=['missing'])
    assert not (tmp_path / 'absent.json').exists()
    with pytest.raises(AdminError, match='destination_exists'):
        export_history(source, source)
    assert before == rows(source)
