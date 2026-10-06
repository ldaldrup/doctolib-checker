"""Exercise actual operator entrypoints on one final-model disposable database."""
import json
import os
from pathlib import Path
import subprocess
import sys

from cryptography.fernet import Fernet

from tests.test_recovery_final import recovery, all_rows


def test_operator_commands_complete_roundtrip_without_live_provider_work(recovery, tmp_path):
    item = recovery
    environment = dict(os.environ, NOTIFICATION_SECRET_KEY=item['settings'].notification_secret_key)
    def command(*arguments, env=environment):
        process = subprocess.run([sys.executable, '-m', 'app.admin', *map(str, arguments)],
            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=30)
        assert process.returncode == 0, process.stderr
        return json.loads(process.stdout)
    before = all_rows(item['path'])
    preview = command('retention-preview', '--source', item['path'], '--cutoff', '2000-01-01T00:00:00+00:00')
    assert preview['plan_id'] and preview['cutoff'] and preview['pinned_reasons']
    assert not preview['estimate_reclaims_file_bytes']
    applied = command('retention-apply', '--source', item['path'], '--cutoff', preview['cutoff'], '--plan-id', preview['plan_id'], '--batch-size', 1)
    assert applied['completed'] and not any(applied['deleted'].values())
    output = tmp_path / 'history.json'
    exported = command('export-history', '--source', item['path'], '--destination', output)
    assert not exported['restorable_backup'] and not exported['private_urls_included']
    assert 'ciphertext' not in output.read_text() and 'booking_url' not in output.read_text()
    archive = tmp_path / 'backup.zip'
    backed_up = command('backup', '--source', item['path'], '--destination', archive)
    assert backed_up['status'] == 'backed_up'
    for key_kind, key in (('available', environment['NOTIFICATION_SECRET_KEY']), ('missing', ''), ('unreadable', Fernet.generate_key().decode())):
        checked = command('verify', '--archive', archive, '--work-directory', tmp_path / key_kind,
                          env=dict(environment, NOTIFICATION_SECRET_KEY=key))
        assert checked['status'] == 'verified'
        assert checked['notification_configuration']['key_state'] == key_kind
        restored = all_rows(tmp_path / key_kind / 'restored.sqlite3')
        assert all(restored[table] == values for table, values in before.items() if table != 'retention_plans')
    after = all_rows(item['path'])
    assert all(after[table] == values for table, values in before.items() if table != 'retention_plans')


def test_export_cli_reports_safe_errors_without_traceback(tmp_path):
    process = subprocess.run([sys.executable, '-m', 'app.admin', 'export-history',
        '--source', str(tmp_path / 'missing.sqlite3'), '--destination', str(tmp_path / 'history.json')],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
    assert process.returncode == 1 and not process.stdout
    assert json.loads(process.stderr) == {'status':'error', 'code':'source_missing_or_not_a_file'}
    assert 'Traceback' not in process.stderr and str(tmp_path) not in process.stderr
