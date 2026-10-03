"""Named destination mutations and one-shot saved-configuration tests."""
import json
import math
import uuid
from datetime import datetime, timedelta, timezone

CHANNEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_channels (
 id TEXT PRIMARY KEY,type TEXT NOT NULL DEFAULT 'telegram',name TEXT NOT NULL,
 enabled INTEGER NOT NULL,deleted INTEGER NOT NULL DEFAULT 0,edit_version INTEGER NOT NULL DEFAULT 1,
 destination_version INTEGER NOT NULL DEFAULT 1,credential_version INTEGER NOT NULL DEFAULT 1,
 token_ciphertext TEXT,chat_ciphertext TEXT,destination_identity TEXT,
 endpoint_ciphertext TEXT,auth_type TEXT NOT NULL DEFAULT 'none',
 auth_token_ciphertext TEXT,auth_username_ciphertext TEXT,auth_password_ciphertext TEXT,
 ntfy_priority INTEGER NOT NULL DEFAULT 3,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS channel_mutations (
 key TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,channel_id TEXT REFERENCES notification_channels(id),
 created_at TEXT NOT NULL,expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notification_onboarding (
 singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),channel_id TEXT REFERENCES notification_channels(id));
CREATE TABLE IF NOT EXISTS channel_tests (
 id TEXT PRIMARY KEY,key TEXT NOT NULL UNIQUE,fingerprint TEXT NOT NULL,
 channel_config_id TEXT NOT NULL REFERENCES notification_channels(id),destination_version INTEGER NOT NULL,
 credential_version INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'queued',owner_token TEXT,claim_until TEXT,
 attempt_started_at TEXT,attempt_count INTEGER NOT NULL DEFAULT 0,error_code TEXT,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,expires_at TEXT NOT NULL);
"""

SECRET_COLUMNS = ('token_ciphertext','chat_ciphertext','endpoint_ciphertext','auth_token_ciphertext','auth_username_ciphertext','auth_password_ciphertext')
COMPLETE_SQL = """((c.type='telegram' AND c.token_ciphertext IS NOT NULL AND c.chat_ciphertext IS NOT NULL)
 OR (c.type IN ('ntfy','webhook') AND c.endpoint_ciphertext IS NOT NULL AND
 (c.auth_type='none' OR (c.auth_type='bearer' AND c.auth_token_ciphertext IS NOT NULL)
 OR (c.auth_type='basic' AND c.auth_username_ciphertext IS NOT NULL AND c.auth_password_ciphertext IS NOT NULL))))"""


def channel_secret_columns(row):
    if row['type'] == 'telegram':
        return ('token_ciphertext','chat_ciphertext')
    return ('endpoint_ciphertext',) + {'none': (), 'bearer': ('auth_token_ciphertext',),
        'basic': ('auth_username_ciphertext','auth_password_ciphertext')}[row['auth_type']]


def channel_complete(row):
    return all(row[column] for column in channel_secret_columns(row))


def now_text():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def expiry(days=7):
    return (datetime.now(timezone.utc)+timedelta(days=days)).isoformat(timespec='microseconds')


class ChannelOperations:
    def _channel(self, conn, row, private=False):
        if row is None:
            return None
        item = dict(row)
        item['enabled'] = bool(item['enabled'])
        item['deleted'] = bool(item['deleted'])
        item['bot_token_set'] = bool(item['token_ciphertext'])
        item['chat_id_set'] = bool(item['chat_ciphertext'])
        item['endpoint_set'] = bool(item['endpoint_ciphertext'])
        item['auth_configured'] = item['type'] != 'telegram' and item['auth_type'] != 'none' and all(
            item[column] for column in channel_secret_columns(item) if column != 'endpoint_ciphertext')
        item['credential_configured'] = channel_complete(item)
        item['usable'] = item['enabled'] and not item['deleted'] and item['credential_configured']
        item['bot_token_masked'] = '••••' if item['bot_token_set'] else None
        item['chat_id_masked'] = '••••' if item['chat_id_set'] else None
        latest = conn.execute("SELECT * FROM channel_tests WHERE channel_config_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (item['id'],)).fetchone()
        item['latest_test'] = self._test(latest)
        item['affected_jobs'] = [dict(job) for job in conn.execute(
            "SELECT j.id,j.name FROM jobs j JOIN job_channels jc ON jc.job_id=j.id WHERE jc.channel_config_id=? AND j.status!='deleted'",(item['id'],))]
        if not private:
            for key in (*SECRET_COLUMNS,'destination_identity'):
                item.pop(key)
        return item

    def list_channels(self):
        with self.database.connection() as conn:
            items = [self._channel(conn,row) for row in conn.execute("SELECT * FROM notification_channels WHERE deleted=0 ORDER BY created_at,id")]
            return {'items':items,'total':len(items)}

    def get_channel(self, channel_id, private=False):
        with self.database.connection() as conn:
            return self._channel(conn,conn.execute('SELECT * FROM notification_channels WHERE id=?',(channel_id,)).fetchone(),private)

    def legacy_imported(self):
        with self.database.connection() as conn:
            return bool(conn.execute('SELECT 1 FROM notification_onboarding').fetchone())

    def create_channel(self, values, key, fingerprint, legacy=False):
        from app.storage.repositories import ConflictError
        with self.database.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            now = now_text()
            conn.execute('DELETE FROM channel_mutations WHERE expires_at<=?',(now,))
            prior = conn.execute('SELECT * FROM channel_mutations WHERE key=?',(key,)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ConflictError('idempotency_conflict')
                return self._channel(conn,conn.execute('SELECT * FROM notification_channels WHERE id=?',(prior['channel_id'],)).fetchone()),False
            if legacy:
                imported = conn.execute('SELECT channel_id FROM notification_onboarding').fetchone()
                if imported:
                    channel_id = imported['channel_id']
                    conn.execute('INSERT INTO channel_mutations VALUES(?,?,?,?,?)',(key,fingerprint,channel_id,now,expiry()))
                    return self._channel(conn,conn.execute('SELECT * FROM notification_channels WHERE id=?',(channel_id,)).fetchone()),False
            if callable(values):
                values = values()
            if not values:
                raise ConflictError('idempotency_operation_expired')
            channel_id = str(uuid.uuid4())
            columns = ('type','name','enabled',*SECRET_COLUMNS,'destination_identity','auth_type','ntfy_priority')
            defaults = {'type':'telegram','auth_type':'none','ntfy_priority':3}
            stored = [values.get(column,defaults.get(column)) for column in columns]
            conn.execute('INSERT INTO notification_channels(id,'+','.join(columns)+',created_at,updated_at) VALUES('+','.join('?' for _ in range(len(columns)+3))+')',(channel_id,*stored,now,now))
            conn.execute('INSERT INTO channel_mutations VALUES(?,?,?,?,?)',(key,fingerprint,channel_id,now,expiry()))
            if legacy:
                conn.execute('INSERT INTO notification_onboarding VALUES(1,?)',(channel_id,))
                conn.execute("INSERT OR IGNORE INTO job_channels(job_id,channel_config_id) SELECT id,? FROM jobs WHERE telegram_enabled=1 AND status!='deleted'",(channel_id,))
                conn.execute("UPDATE jobs SET edit_version=edit_version+1 WHERE telegram_enabled=1 AND status!='deleted'")
            return self._channel(conn,conn.execute('SELECT * FROM notification_channels WHERE id=?',(channel_id,)).fetchone()),True

    def update_channel(self, channel_id, values, expected_version, recover_failed=False, delete=False):
        from app.storage.repositories import NotFoundError, VersionConflictError
        with self.database.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            before = conn.execute('SELECT * FROM notification_channels WHERE id=? AND deleted=0',(channel_id,)).fetchone()
            if before is None:
                raise NotFoundError('channel_not_found')
            if before['edit_version'] != expected_version:
                raise VersionConflictError(before['edit_version'])
            values = dict(values)
            if delete:
                values.update(deleted=1,enabled=0)
            destination_changed = 'destination_identity' in values and values['destination_identity'] != before['destination_identity']
            credentials_changed = any(key in values and values[key] != before[key] for key in (*SECRET_COLUMNS,'auth_type','ntfy_priority'))
            changes = {key:value for key,value in values.items() if value != before[key]}
            if changes:
                changes.update(edit_version=before['edit_version']+1,updated_at=now_text())
                if destination_changed:
                    changes['destination_version']=before['destination_version']+1
                if credentials_changed:
                    changes['credential_version']=before['credential_version']+1
                conn.execute('UPDATE notification_channels SET '+','.join(key+'=?' for key in changes)+' WHERE id=?',(*changes.values(),channel_id))
            after = conn.execute('SELECT * FROM notification_channels WHERE id=?',(channel_id,)).fetchone()
            self._channel_changed(conn,dict(before),dict(after),recover_failed=recover_failed)
            if destination_changed or not after['enabled'] or after['deleted'] or not channel_complete(after):
                conn.execute("UPDATE channel_tests SET status='cancelled',error_code='channel_changed',updated_at=? WHERE channel_config_id=? AND status IN ('queued','running') AND attempt_started_at IS NULL",(now_text(),channel_id))
            public = self._channel(conn,after)
            if delete:
                conn.execute('UPDATE jobs SET edit_version=edit_version+1,updated_at=? WHERE id IN (SELECT job_id FROM job_channels WHERE channel_config_id=?)',(now_text(),channel_id))
                conn.execute('DELETE FROM job_channels WHERE channel_config_id=?',(channel_id,))
            return public

    @staticmethod
    def _test(row):
        if row is None:
            return None
        return {key:row[key] for key in ('id','channel_config_id','destination_version','credential_version','status','attempt_count','error_code','created_at','updated_at')}

    def reserve_channel_test(self, channel_id, expected_version, key, fingerprint, validate_usable=None):
        from app.storage.repositories import ConflictError, NotFoundError, VersionConflictError
        with self.database.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            now = now_text()
            conn.execute('DELETE FROM channel_tests WHERE expires_at<=? AND status NOT IN (\'queued\',\'running\')',(now,))
            prior = conn.execute('SELECT * FROM channel_tests WHERE key=?',(key,)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ConflictError('idempotency_conflict')
                return self._test(prior)
            channel = conn.execute('SELECT * FROM notification_channels WHERE id=? AND deleted=0',(channel_id,)).fetchone()
            if channel is None:
                raise NotFoundError('channel_not_found')
            if channel['edit_version'] != expected_version:
                raise VersionConflictError(channel['edit_version'])
            if not channel['enabled'] or not channel_complete(channel):
                raise ConflictError('channel_unusable')
            if validate_usable is not None:
                validate_usable(dict(channel))
            test_id = str(uuid.uuid4())
            conn.execute('''INSERT INTO channel_tests(id,key,fingerprint,channel_config_id,destination_version,credential_version,created_at,updated_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?)''',(test_id,key,fingerprint,channel_id,channel['destination_version'],channel['credential_version'],now,now,expiry()))
            return self._test(conn.execute('SELECT * FROM channel_tests WHERE id=?',(test_id,)).fetchone())

    def get_channel_test(self, test_id):
        with self.database.connection() as conn:
            return self._test(conn.execute('SELECT * FROM channel_tests WHERE id=?',(test_id,)).fetchone())

    def reconcile_channel_tests(self):
        with self.database.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            now = now_text()
            conn.execute("UPDATE channel_tests SET status='cancelled',error_code='test_expired',updated_at=? WHERE status='queued' AND expires_at<=?",(now,now))
            conn.execute("UPDATE channel_tests SET status=CASE WHEN attempt_started_at IS NULL THEN 'queued' ELSE 'unknown' END,error_code=CASE WHEN attempt_started_at IS NULL THEN NULL ELSE 'test_acknowledgement_lost' END,owner_token=NULL,claim_until=NULL,updated_at=? WHERE status='running' AND claim_until<=?",(now,now))

    def claim_channel_test(self):
        with self.database.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM channel_tests WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            token = str(uuid.uuid4())
            deadline = (datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat(timespec='microseconds')
            conn.execute("UPDATE channel_tests SET status='running',owner_token=?,claim_until=?,updated_at=? WHERE id=?",(token,deadline,now_text(),row['id']))
            return dict(conn.execute('SELECT * FROM channel_tests WHERE id=?',(row['id'],)).fetchone())

    def begin_channel_test_attempt(self, test_id, owner_token, remaining_seconds=None):
        wait = 10 if remaining_seconds is None else remaining_seconds
        if not math.isfinite(wait):
            raise ValueError('Test guard wait must be finite')
        with self.database.connection() as conn:
            conn.execute(f'PRAGMA busy_timeout={int(min(10,max(0,wait))*1000)}')
            conn.execute('BEGIN IMMEDIATE')
            now = now_text()
            row = conn.execute("SELECT t.*,c.enabled,c.deleted,"+COMPLETE_SQL+" AS complete,c.destination_version AS current_destination,c.credential_version AS current_credential FROM channel_tests t JOIN notification_channels c ON c.id=t.channel_config_id WHERE t.id=?",(test_id,)).fetchone()
            if row is None or row['status']!='running' or row['owner_token']!=owner_token or row['claim_until']<=now:
                return False
            if not row['enabled'] or row['deleted'] or not row['complete'] or row['destination_version']!=row['current_destination'] or row['credential_version']!=row['current_credential']:
                conn.execute("UPDATE channel_tests SET status='cancelled',error_code='channel_changed',updated_at=? WHERE id=?",(now,test_id))
                return False
            conn.execute('UPDATE channel_tests SET attempt_started_at=?,attempt_count=attempt_count+1,updated_at=? WHERE id=? AND attempt_started_at IS NULL',(now,now,test_id))
            return row['attempt_started_at'] is None

    def finish_channel_test(self, test_id, token, outcome):
        with self.database.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM channel_tests WHERE id=?',(test_id,)).fetchone()
            if row is None or row['status']!='running' or row['owner_token']!=token or row['claim_until']<=now_text():
                return False
            code = outcome.error_code
            if code and (not isinstance(code,str) or len(code)>80 or not all(c.isalnum() or c=='_' for c in code)):
                code = 'invalid_sender_error'
            status = {'sent':'sent','uncertain':'unknown','retry':'failed','action_required':'failed'}.get(outcome.category,'unknown')
            conn.execute('UPDATE channel_tests SET status=?,error_code=?,owner_token=NULL,claim_until=NULL,updated_at=? WHERE id=?',(status,code,now_text(),test_id))
            return True
