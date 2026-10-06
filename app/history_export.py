"""Consistent, redacted history export; never a whole-application backup."""

from contextlib import closing
from datetime import datetime, timezone
import json
import os
import sqlite3
from pathlib import Path
import tempfile

from app.admin import AdminError, _inspect, _readonly
from app.storage.db import SCHEMA_VERSION


# Explicit fields keep credentials, ciphertext, lease tokens, request bodies and
# raw diagnostic payloads out of general exports, including future columns.
EXPORT_FIELDS = {
    'jobs': 'id name status interval_seconds date_mode horizon_days earliest_date latest_date time_zone insurance_sector telehealth telegram_enabled search_revision edit_version created_at updated_at quiet_hours_enabled quiet_hours_start quiet_hours_end',
    'targets': 'id job_id country practice_id motive_id practitioner_id agenda_ids practice_name practitioner_name motive_name active validation_state last_validated_at metadata_validation_state metadata_validation_reason metadata_checked_at',
    'check_runs': 'id job_id job_name started_at finished_at outcome successful_targets failed_targets triggered_by search_revision snapshot_known intent_id paused_manual status_version target_cursor generation requested_at',
    'check_results': 'id run_id job_id target_id practitioner_name practice_name checked_at status slot_count earliest_slot count_complete search_revision snapshot_known published error_category upstream_status retry_at',
    'alerts': 'id job_id target_id result_id channel event_type status attempt_count created_at sent_at next_attempt_at search_revision delivery_state event_id channel_config_id destination_version credential_version channel_name last_attempt_at last_attempt_outcome quiet_state quiet_until',
}


def export_history(source, destination, *, job_ids=None, include_private_urls=False):
    """Write requested saved history to a new private file without mutating SQLite."""
    source, destination = Path(source), Path(destination)
    if os.path.lexists(destination):
        raise AdminError('destination_exists')
    if not destination.parent.is_dir():
        raise AdminError('destination_parent_missing')
    if include_private_urls:
        info = destination.parent.stat()
        if os.name == 'posix' and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
            raise AdminError('private_export_requires_private_directory')
    selected = list(dict.fromkeys(job_ids or []))
    if len(selected) > 100 or any(not isinstance(value, str) or not value or len(value) > 128 for value in selected):
        raise AdminError('invalid_export_selection')
    if _inspect(source) != SCHEMA_VERSION:
        raise AdminError('export_requires_current_schema')
    counts = {}
    with tempfile.TemporaryDirectory(prefix='.checker-export-', dir=destination.parent) as temporary:
        output = Path(temporary) / 'history.json'
        with closing(_readonly(source)) as conn, output.open('x', encoding='utf-8') as stream:
            os.chmod(output, 0o600)
            conn.row_factory = sqlite3.Row
            conn.execute('BEGIN')
            if conn.execute('SELECT version FROM schema_version').fetchone()[0] != SCHEMA_VERSION:
                raise AdminError('export_requires_current_schema')
            if selected:
                found = {row[0] for row in conn.execute('SELECT id FROM jobs WHERE id IN (' + ','.join('?' for _ in selected) + ')', selected)}
                if found != set(selected):
                    raise AdminError('export_job_not_found')
            header = {'format': 'doctolib-checker-history', 'format_version': 1,
                      'schema_version': SCHEMA_VERSION, 'restorable_backup': False,
                      'exported_at': datetime.now(timezone.utc).isoformat(),
                      'private_urls_included': include_private_urls}
            stream.write(json.dumps(header, sort_keys=True)[:-1])
            for table, fields in EXPORT_FIELDS.items():
                columns = fields.split()
                if include_private_urls and table in ('targets', 'check_results'):
                    columns.append('booking_url')
                where = ' WHERE ' + ('id' if table == 'jobs' else 'job_id') + ' IN (' + ','.join('?' for _ in selected) + ')' if selected else ''
                stream.write(',\n' + json.dumps(table) + ':[')
                count = 0
                for row in conn.execute('SELECT ' + ','.join(columns) + ' FROM ' + table + where + ' ORDER BY id', selected):
                    if count:
                        stream.write(',\n')
                    json.dump(dict(row), stream, sort_keys=True)
                    count += 1
                stream.write(']')
                counts[table] = count
            stream.write('}\n')
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(output, destination)
        except FileExistsError:
            raise AdminError('destination_exists') from None
        if os.name == 'posix':
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    return {'status': 'exported', 'format_version': 1, 'schema_version': SCHEMA_VERSION,
            'restorable_backup': False, 'private_urls_included': include_private_urls, 'counts': counts}
