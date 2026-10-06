"""Transactional application operations over the SQLite schema."""

import time
import uuid
import json
import math
from datetime import datetime, timedelta, timezone

from app.doctolib import DoctolibClient
from app.quiet_hours import quiet_window, validate_quiet_hours
from app.storage.channel_operations import ChannelOperations, channel_complete, COMPLETE_SQL


def utc_now():
    return datetime.now(timezone.utc)


def iso(value=None, *, timespec="seconds"):
    value = value or utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec=timespec)


def precise_iso(value=None):
    return iso(value, timespec="microseconds")


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def new_id():
    return str(uuid.uuid4())


def alert_dedupe_key(target_id, earliest_slot, episode):
    base = target_id + ":slot:" + earliest_slot
    return base if episode == 0 else base + ":episode:" + str(episode)


class NotFoundError(LookupError):
    pass


class ConflictError(ValueError):
    pass


class VersionConflictError(ConflictError):
    def __init__(self, current_version):
        super().__init__("edit_version_conflict")
        self.current_version = current_version


class CreateReservationLostError(ConflictError):
    def __init__(self, operation):
        super().__init__("create_reservation_lost")
        self.operation = operation


CREATE_LEASE_SECONDS = 120
CREATE_RETENTION_DAYS = 7


class LeaseLostError(RuntimeError):
    pass


SEARCH_FIELDS = ("date_mode", "horizon_days", "earliest_date", "latest_date", "time_zone", "insurance_sector", "telehealth")
TARGET_FIELDS = ("booking_url", "country", "profile_slug", "practice_id", "motive_id", "practitioner_id",
                 "agenda_ids", "practice_name", "practitioner_name", "motive_name")


def search_signature(job, targets):
    search = {key: job[key] for key in SEARCH_FIELDS}
    search["telehealth"] = bool(search["telehealth"])
    if search["date_mode"] == "custom":
        search["horizon_days"] = None
    else:
        search["earliest_date"] = search["latest_date"] = None
    metadata = []
    for target in targets:
        values = {key: target.get(key) for key in TARGET_FIELDS}
        values["agenda_ids"] = sorted(set(str(values["agenda_ids"]).split("-")))
        metadata.append(values)
    return json.dumps([search, sorted(metadata, key=lambda item: item["booking_url"])], sort_keys=True)


class Repository(ChannelOperations):
    def __init__(self, database, minimum_poll_interval_seconds=300):
        self.database = database
        self.minimum_poll_interval_seconds = minimum_poll_interval_seconds
        self._notification_secrets = None
        self._notification_allowlist = ()

    def configure_notification_routing(self, settings):
        """Apply this worker's key and SMTP egress policy to event routing."""
        from app.notification_secrets import NotificationSecrets
        self._notification_secrets = NotificationSecrets(settings.notification_secret_key)
        self._notification_allowlist = settings.webhook_allowlist

    def _email_channel_usable(self, conn, channel):
        if self._notification_secrets is None:
            return False
        from app.services.email_delivery import mailbox, smtp_transport_usable
        from app.notification_secrets import SecretUnavailable
        transport = self._smtp(conn.execute(
            'SELECT * FROM smtp_transport WHERE singleton_id=1').fetchone(),private=True)
        if not smtp_transport_usable(transport,self._notification_secrets,self._notification_allowlist):
            return False
        try:
            mailbox(self._notification_secrets.decrypt(channel['email_recipient_ciphertext']))
        except (SecretUnavailable,ValueError,TypeError):
            return False
        return True

    def configure_minimum(self, minimum_poll_interval_seconds):
        """Apply a raised server floor to persisted settings and existing jobs."""
        self.minimum_poll_interval_seconds = minimum_poll_interval_seconds
        now = utc_now()
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE settings SET default_interval_seconds=?,updated_at=?,edit_version=edit_version+1
                WHERE default_interval_seconds<?""",
                (minimum_poll_interval_seconds, iso(now), minimum_poll_interval_seconds),
            )
            rows = conn.execute(
                """SELECT id,next_check_at,last_started_at,last_finished_at FROM jobs
                WHERE status!='deleted' AND interval_seconds<?""",
                (minimum_poll_interval_seconds,),
            ).fetchall()
            for row in rows:
                due = max(parse_time(row["next_check_at"]), self._minimum_due(row, now))
                conn.execute(
                    """UPDATE jobs SET interval_seconds=?,next_check_at=?,updated_at=?,edit_version=edit_version+1 WHERE id=?""",
                    (minimum_poll_interval_seconds, precise_iso(due), iso(now), row["id"]),
                )

    def _minimum_due(self, job, requested_at):
        anchor = max(
            (parse_time(job[key]) for key in ("last_started_at", "last_finished_at") if job[key]),
            default=None,
        )
        allowed = anchor + timedelta(seconds=self.minimum_poll_interval_seconds) if anchor else requested_at
        return max(requested_at, allowed)

    def _job(self, row, conn=None):
        if row is None:
            return None
        item = dict(row)
        item['message_content'] = json.loads(item['message_content']) if item.get('message_content') else None
        if conn is not None:
            item['effective_message_content'] = self._effective_content(conn, item)
            setting = conn.execute('SELECT content_version FROM settings WHERE singleton_id=1').fetchone()
            item['effective_content_version'] = item['content_version'] if item['message_content'] is not None else (setting['content_version'] if setting else 1)
        item.pop("lock_run_id", None)
        item.pop("lock_owner_token", None)
        if conn is not None:
            item["check_intent"] = self._intent_evidence(conn, item["id"])
            item["current_run"] = self._current_run(conn, item["id"])
        if conn is not None:
            item["targets"] = [dict(target) for target in conn.execute(
                "SELECT * FROM targets WHERE job_id=? AND active=1 ORDER BY rowid", (item["id"],)
            ).fetchall()]
        if conn is not None:
            item["notification_channel_ids"] = [channel[0] for channel in conn.execute(
                "SELECT channel_config_id FROM job_channels WHERE job_id=? ORDER BY channel_config_id", (item["id"],))]
        if conn is not None:
            item['notification_channels'] = []
            for channel in conn.execute("""SELECT c.* FROM notification_channels c JOIN job_channels jc
                    ON jc.channel_config_id=c.id WHERE jc.job_id=? ORDER BY c.name,c.id""", (item['id'],)):
                latest = conn.execute("SELECT status,delivery_state,error_summary,claim_owner_token,quiet_state,quiet_until FROM alerts WHERE job_id=? AND channel_config_id=? ORDER BY rowid DESC LIMIT 1", (item['id'],channel['id'])).fetchone()
                quiet_work = conn.execute("""SELECT status,delivery_state,error_summary,claim_owner_token,quiet_state,quiet_until
                    FROM alerts WHERE job_id=? AND channel_config_id=? AND status IN ('pending','failed')
                    AND quiet_state IN ('held','waiting_for_fresh_check','needs_manual_check')
                    ORDER BY CASE quiet_state WHEN 'needs_manual_check' THEN 0
                    WHEN 'waiting_for_fresh_check' THEN 1 ELSE 2 END,rowid DESC LIMIT 1""",
                    (item['id'],channel['id'])).fetchone()
                delivery = quiet_work or latest
                delivery_status = delivery['status'] if delivery else None
                if delivery and delivery['status'] in ('pending', 'failed'):
                    delivery_status = {'held': 'held', 'waiting_for_fresh_check': 'waiting_for_fresh_check',
                                       'needs_manual_check': 'needs_manual_check'}.get(delivery['quiet_state'],
                                       'in_flight' if delivery['claim_owner_token'] else delivery['status'])
                quiet_until = delivery['quiet_until'] if delivery and delivery['status'] in ('pending', 'failed') and delivery['quiet_state'] == 'held' else None
                item['notification_channels'].append({'id':channel['id'],'name':channel['name'],'type':channel['type'],
                    'enabled':bool(channel['enabled']) and not channel['deleted'],
                    'usable':bool(channel['enabled'] and not channel['deleted'] and channel_complete(channel)),
                    'delivery_status':delivery_status,
                    'quiet_until':quiet_until,
                    'error_code':delivery['error_summary'] if delivery else None})
        for key in ("telehealth", "telegram_enabled", "quiet_hours_enabled"):
            item[key] = bool(item[key])
        return item

    @staticmethod
    def _current_run(conn, job_id):
        row = conn.execute("""SELECT r.id,r.outcome,r.search_revision,r.triggered_by,r.intent_id,r.paused_manual,
            r.target_cursor,r.successful_targets,r.failed_targets,r.search_snapshot
            FROM check_runs r JOIN jobs j ON j.lock_run_id=r.id WHERE j.id=?
            AND (r.outcome='yielded' OR (r.outcome='running' AND j.lock_until>?))""", (job_id, precise_iso())).fetchone()
        if not row:
            return None
        result = dict(row)
        snapshot = json.loads(result.pop('search_snapshot'))
        result.update(target_total=len(snapshot['targets']),
                      target_completed=result['successful_targets'] + result['failed_targets'])
        return result

    @staticmethod
    def _intent_evidence(conn, job_id, intent_id=None):
        row = conn.execute("""SELECT i.*,r.outcome FROM check_intents i LEFT JOIN check_runs r ON r.id=i.run_id
            WHERE i.job_id=? AND (? IS NULL OR i.id=?)
            ORDER BY CASE WHEN i.status='queued' THEN 0 ELSE 1 END,
            i.rowid DESC LIMIT 1""", (job_id, intent_id, intent_id)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result.update(requested_revision=row['search_revision'],run_outcome=row['outcome'],
                      reason=row['triggered_by'],cancellation_reason=row['cancel_reason'])
        return result

    @staticmethod
    def _queue_intent(conn, job, trigger, now):
        queued = conn.execute("SELECT * FROM check_intents WHERE job_id=? AND status='queued'", (job['id'],)).fetchone()
        eligible = max(now, parse_time(job['last_extra_started_at']) + timedelta(seconds=60)) if job['last_extra_started_at'] else now
        intent_id = queued['id'] if queued else new_id()
        if queued:
            conn.execute("""UPDATE check_intents SET search_revision=?,triggered_by=?,requested_at=?,eligible_at=?,
                paused_manual=?,status_version=? WHERE id=?""",
                (job['search_revision'],trigger,precise_iso(now),precise_iso(eligible),int(job['status']=='paused'),job['status_version'],intent_id))
        else:
            conn.execute("""INSERT INTO check_intents(id,job_id,search_revision,triggered_by,requested_at,eligible_at,
                status,paused_manual,status_version) VALUES(?,?,?,?,?,?,'queued',?,?)""",
                (intent_id,job['id'],job['search_revision'],trigger,precise_iso(now),precise_iso(eligible),int(job['status']=='paused'),job['status_version']))
        evidence = Repository._intent_evidence(conn, job['id'], intent_id)
        evidence['coalesced'] = queued is not None
        return evidence

    def request_check(self, job_id):
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("SELECT * FROM jobs WHERE id=? AND status!='deleted'", (job_id,)).fetchone()
            if job is None:
                raise NotFoundError("Job not found")
            return self._queue_intent(conn, job, 'manual', utc_now())

    def _create_operation(self, conn, row, owned=False):
        result = {key: row[key] for key in ("key", "state", "generation", "lease_until", "job_id",
                                          "error_code", "retryable", "created_at", "expires_at")}
        result["retryable"] = bool(result["retryable"])
        if owned:
            result.update(state="owned", owner_token=row["owner_token"],
                          canonical_values=json.loads(row["canonical_values"]))
        elif row["state"] == "completed":
            result["job"] = self._job(conn.execute("SELECT * FROM jobs WHERE id=?", (row["job_id"],)).fetchone(), conn)
        return result

    def reserve_create(self, key, fingerprint, canonical_values):
        """Reserve metadata resolution before doing any network work.

        Retries keep the first operation's defaults. Expired owners can never
        finalize, even before a replacement claims the operation.
        """
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            conn.execute("DELETE FROM create_operations WHERE expires_at<=?", (precise_iso(now),))
            row = conn.execute("SELECT * FROM create_operations WHERE key=?", (key,)).fetchone()
            if row is not None and row["fingerprint"] != fingerprint:
                raise ConflictError("idempotency_key_conflict")
            if row is not None and (row["state"] == "completed" or
                    (row["state"] == "failed" and not row["retryable"]) or
                    (row["state"] == "pending" and parse_time(row["lease_until"]) > now)):
                return self._create_operation(conn, row)
            owner = new_id()
            lease = precise_iso(now + timedelta(seconds=CREATE_LEASE_SECONDS))
            if row is None:
                conn.execute("""INSERT INTO create_operations(key,fingerprint,canonical_values,state,owner_token,
                    lease_until,created_at,expires_at) VALUES(?,?,?,'pending',?,?,?,?)""",
                    (key, fingerprint, json.dumps(canonical_values, sort_keys=True), owner, lease,
                     precise_iso(now), precise_iso(now + timedelta(days=CREATE_RETENTION_DAYS))))
            else:
                conn.execute("""UPDATE create_operations SET state='pending',owner_token=?,generation=generation+1,
                    lease_until=?,error_code=NULL,retryable=0 WHERE key=?""", (owner, lease, key))
            return self._create_operation(conn, conn.execute("SELECT * FROM create_operations WHERE key=?", (key,)).fetchone(), True)

    def _owned_create_operation(self, conn, operation, now=None):
        row = conn.execute("SELECT * FROM create_operations WHERE key=?", (operation["key"],)).fetchone()
        now = now or utc_now()
        if (row is None or row["state"] != "pending" or row["owner_token"] != operation.get("owner_token") or
                row["generation"] != operation.get("generation") or parse_time(row["lease_until"]) <= now or
                parse_time(row["expires_at"]) <= now):
            authoritative = self._create_operation(conn, row) if row else {"key": operation["key"], "state": "expired"}
            raise CreateReservationLostError(authoritative)
        return row

    def fail_create(self, operation, error_code, retryable=True):
        # Never persist provider messages, URLs, or transport exception strings.
        if error_code not in ("doctolib_unavailable", "invalid_job", "invalid_target", "metadata_unavailable", "create_failed"):
            error_code = "create_failed"
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._owned_create_operation(conn, operation)
            conn.execute("""UPDATE create_operations SET state='failed',owner_token=NULL,lease_until=NULL,
                error_code=?,retryable=? WHERE key=?""", (error_code, int(retryable), operation["key"]))
            return self._create_operation(conn, conn.execute("SELECT * FROM create_operations WHERE key=?", (operation["key"],)).fetchone())

    def renew_create(self, operation):
        """Renew before each bounded metadata request, never after expiry."""
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            self._owned_create_operation(conn, operation, now)
            conn.execute("UPDATE create_operations SET lease_until=? WHERE key=?",
                         (precise_iso(now + timedelta(seconds=CREATE_LEASE_SECONDS)), operation["key"]))
            return self._create_operation(conn, conn.execute("SELECT * FROM create_operations WHERE key=?", (operation["key"],)).fetchone(), True)

    @staticmethod
    def _effective_content(conn, job=None):
        from app.notifications import normalize_content
        content = job['message_content'] if job is not None else None
        if content is None:
            row = conn.execute('SELECT message_content FROM settings WHERE singleton_id=1').fetchone()
            content = row['message_content'] if row else None
        if isinstance(content, str):
            content = json.loads(content)
        return normalize_content(content)

    def _validate_content_channels(self, conn, content, channel_ids):
        if content['silent']:
            for channel_id in channel_ids:
                row = conn.execute('SELECT type FROM notification_channels WHERE id=?', (channel_id,)).fetchone()
                if row and row['type'] not in ('telegram','ntfy'):
                    raise ValueError('Silent delivery is supported only by Telegram and ntfy')

    def create_job(self, values, targets, operation=None):
        if values["interval_seconds"] < self.minimum_poll_interval_seconds:
            raise ValueError("interval_seconds is below the server minimum")
        quiet_enabled = values.get("quiet_hours_enabled", False)
        quiet_start, quiet_end = values.get("quiet_hours_start", "22:00"), values.get("quiet_hours_end", "07:00")
        validate_quiet_hours(quiet_enabled, quiet_start, quiet_end)
        job_id = new_id()
        now = iso()
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if operation is not None:
                self._owned_create_operation(conn, operation)
            conn.execute(
                """INSERT INTO jobs
                (id,name,status,interval_seconds,date_mode,horizon_days,earliest_date,latest_date,
                 time_zone,insurance_sector,telehealth,telegram_enabled,created_at,updated_at,next_check_at,
                 quiet_hours_enabled,quiet_hours_start,quiet_hours_end)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, values["name"], "active", values["interval_seconds"], values["date_mode"],
                 values.get("horizon_days"), values.get("earliest_date"), values.get("latest_date"),
                 values["time_zone"], values["insurance_sector"], int(values["telehealth"]),
                 int(values["telegram_enabled"]), now, now, now, int(quiet_enabled), quiet_start, quiet_end),
            )
            from app.notifications import normalize_content
            content = normalize_content(values['message_content']) if values.get('message_content') is not None else None
            conn.execute('UPDATE jobs SET message_content=? WHERE id=?', (json.dumps(content) if content is not None else None, job_id))
            self._validate_content_channels(conn, self._effective_content(conn, {'message_content':content}), values.get('notification_channel_ids', []))
            self._set_job_channels(conn, job_id, values.get("notification_channel_ids", []))
            for target in targets:
                conn.execute(
                    """INSERT INTO targets
                    (id,job_id,booking_url,country,profile_slug,practice_id,motive_id,practitioner_id,
                     agenda_ids,practice_name,practitioner_name,motive_name,validation_state,last_validated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id(), job_id, target["booking_url"], target["country"], target["profile_slug"],
                     target["practice_id"], target["motive_id"], target.get("practitioner_id"),
                     target["agenda_ids_str"], target["practice_name"], target["practitioner_name"],
                     target.get("motive_name"), "ready", now),
                )
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if operation is not None:
                # Recheck the deadline after inserts, while the same transaction
                # still owns the database write lock. Expiry rolls all inserts back.
                self._owned_create_operation(conn, operation)
                conn.execute("""UPDATE create_operations SET state='completed',job_id=?,owner_token=NULL,
                    lease_until=NULL,error_code=NULL,retryable=0,expires_at=? WHERE key=?""",
                    (job_id, precise_iso(utc_now() + timedelta(days=CREATE_RETENTION_DAYS)), operation["key"]))
            return self._job(row, conn)

    def get_job(self, job_id, include_deleted=False):
        with self.database.connection() as conn:
            sql = "SELECT * FROM jobs WHERE id=?"
            params = [job_id]
            if not include_deleted:
                sql += " AND status != 'deleted'"
            return self._job(conn.execute(sql, params).fetchone(), conn)

    def list_jobs(self, status=None, limit=100, offset=0):
        with self.database.connection() as conn:
            where = "status != 'deleted'"
            params = []
            if status in ("active", "paused"):
                where += " AND status=?"
                params.append(status)
            rows = conn.execute(
                "SELECT * FROM jobs WHERE " + where + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            result = []
            for row in rows:
                job = self._job(row, conn)
                latest = conn.execute(
                    """SELECT status,slot_count,earliest_slot,checked_at,search_revision,snapshot_known,published FROM check_results
                    WHERE job_id=? ORDER BY checked_at DESC,rowid DESC LIMIT 1""", (job["id"],)
                ).fetchone()
                job["last_result"] = dict(latest) if latest else None
                result.append(job)
            return result

    def update_job(self, job_id, values, targets=None, expected_version=None):
        if values.get("interval_seconds", self.minimum_poll_interval_seconds) < self.minimum_poll_interval_seconds:
            raise ValueError("interval_seconds is below the server minimum")
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id=? AND status != 'deleted'", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError("Job not found")
            if expected_version is not None and row["edit_version"] != expected_version:
                raise VersionConflictError(row["edit_version"])
            previous_targets = [dict(target) for target in conn.execute("SELECT * FROM targets WHERE job_id=? AND active=1", (job_id,))]
            previous_search = search_signature(dict(row), previous_targets)
            previous_channels = [r[0] for r in conn.execute("SELECT channel_config_id FROM job_channels WHERE job_id=? ORDER BY channel_config_id", (job_id,))]
            if "notification_channel_ids" in values:
                self._set_job_channels(conn, job_id, values["notification_channel_ids"])
                removed = set(previous_channels) - set(values["notification_channel_ids"])
                for channel_id in removed:
                    conn.execute("""UPDATE alerts SET status='cancelled',next_attempt_at=NULL,error_summary='channel_detached'
                        WHERE job_id=? AND channel_config_id=? AND status IN ('pending','failed')""", (job_id,channel_id))
            if 'message_content' in values:
                from app.notifications import normalize_content
                content = normalize_content(values['message_content']) if values['message_content'] is not None else None
                conn.execute('UPDATE jobs SET message_content=? WHERE id=?', (json.dumps(content) if content is not None else None, job_id))
            effective_content = self._effective_content(conn, dict(conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()))
            self._validate_content_channels(conn, effective_content, values.get('notification_channel_ids', previous_channels))
            quiet_values = {
                "quiet_hours_enabled": values.get("quiet_hours_enabled", bool(row["quiet_hours_enabled"])),
                "quiet_hours_start": values.get("quiet_hours_start", row["quiet_hours_start"]),
                "quiet_hours_end": values.get("quiet_hours_end", row["quiet_hours_end"]),
            }
            validate_quiet_hours(quiet_values["quiet_hours_enabled"], quiet_values["quiet_hours_start"], quiet_values["quiet_hours_end"])
            fields = ["name", "interval_seconds", "date_mode", "horizon_days", "earliest_date", "latest_date",
                      "time_zone", "insurance_sector", "telehealth", "telegram_enabled", "quiet_hours_enabled",
                      "quiet_hours_start", "quiet_hours_end"]
            update = {key: values[key] for key in fields if key in values}
            if update:
                assignments = ",".join(key + "=?" for key in update)
                encoded = [int(value) if key in ("telehealth", "telegram_enabled", "quiet_hours_enabled") else value
                           for key, value in update.items()]
                conn.execute("UPDATE jobs SET " + assignments + " WHERE id=?", encoded + [job_id])
            if targets is not None:
                conn.execute("UPDATE targets SET active=0 WHERE job_id=?", (job_id,))
                for target in targets:
                    saved = conn.execute(
                        "SELECT id FROM targets WHERE job_id=? AND booking_url=?",
                        (job_id, target["booking_url"]),
                    ).fetchone()
                    if saved:
                        conn.execute(
                            """UPDATE targets SET country=?,profile_slug=?,practice_id=?,motive_id=?,
                            practitioner_id=?,agenda_ids=?,practice_name=?,practitioner_name=?,motive_name=?,
                            active=1,validation_state='ready',last_validated_at=? WHERE id=?""",
                            (target["country"], target["profile_slug"], target["practice_id"], target["motive_id"],
                             target.get("practitioner_id"), target["agenda_ids_str"], target["practice_name"],
                             target["practitioner_name"], target.get("motive_name"), iso(), saved["id"]),
                        )
                    else:
                        conn.execute(
                            """INSERT INTO targets
                            (id,job_id,booking_url,country,profile_slug,practice_id,motive_id,practitioner_id,
                             agenda_ids,practice_name,practitioner_name,motive_name,validation_state,last_validated_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (new_id(), job_id, target["booking_url"], target["country"], target["profile_slug"],
                             target["practice_id"], target["motive_id"], target.get("practitioner_id"),
                             target["agenda_ids_str"], target["practice_name"], target["practitioner_name"],
                             target.get("motive_name"), "ready", iso()),
                        )
                conn.execute(
                    """UPDATE alerts SET status='cancelled',next_attempt_at=NULL,error_summary='target_removed'
                    WHERE job_id=? AND status IN ('pending','failed') AND target_id IN
                    (SELECT id FROM targets WHERE job_id=? AND active=0)""",
                    (job_id, job_id),
                )
            current = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            current_targets = [dict(target) for target in conn.execute("SELECT * FROM targets WHERE job_id=? AND active=1", (job_id,))]
            search_changed = search_signature(current, current_targets) != previous_search
            channels_changed = "notification_channel_ids" in values and sorted(values["notification_channel_ids"]) != previous_channels
            content_changed = current['message_content'] != row['message_content']
            quiet_changed = any(current[key] != row[key] for key in ("quiet_hours_enabled", "quiet_hours_start", "quiet_hours_end"))
            policy_changed = channels_changed or current['telegram_enabled'] != row['telegram_enabled'] or quiet_changed
            changed = search_changed or content_changed or policy_changed or any(current[key] != row[key] for key in fields)
            if not current['telegram_enabled'] and row['telegram_enabled']:
                conn.execute("""UPDATE alerts SET status='cancelled',next_attempt_at=NULL,error_summary='notifications_disabled'
                    WHERE job_id=? AND status IN ('pending','failed')""", (job_id,))
            if changed:
                conn.execute("UPDATE jobs SET search_revision=search_revision+?,content_version=content_version+?,policy_version=policy_version+?,edit_version=edit_version+1,updated_at=? WHERE id=?",
                             (int(search_changed), int(content_changed), int(policy_changed), iso(), job_id))
            if search_changed:
                current = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if current['status'] == 'active':
                    self._queue_intent(conn, current, 'search_edit', utc_now())
                else:
                    conn.execute("UPDATE check_intents SET status='cancelled',cancel_reason='search_edited' WHERE job_id=? AND status='queued'", (job_id,))
                conn.execute("""UPDATE alerts SET status='cancelled',next_attempt_at=NULL,error_summary='search_edited'
                    WHERE job_id=? AND status IN ('pending','failed') AND search_revision IS NOT ?""", (job_id,current['search_revision']))
            return self._job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(), conn)

    def set_status(self, job_id, status, expected_version=None):
        if status not in ("active", "paused", "deleted"):
            raise ValueError("Invalid job status")
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id=? AND status != 'deleted'", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError("Job not found")
            if expected_version is not None and row["edit_version"] != expected_version:
                raise VersionConflictError(row["edit_version"])
            if status != row['status'] or status == 'paused':
                conn.execute("UPDATE jobs SET status_version=status_version+1 WHERE id=?", (job_id,))
                conn.execute("UPDATE check_intents SET status='cancelled',cancel_reason='status_changed' WHERE job_id=? AND status='queued'", (job_id,))
                if status != 'active':
                    conn.execute("UPDATE alerts SET status='cancelled',next_attempt_at=NULL,error_summary='job_paused' WHERE job_id=? AND status IN ('pending','failed')", (job_id,))
            due = row["next_check_at"]
            lock = row["lock_until"]
            if status == "active":
                due = precise_iso(self._minimum_due(row, utc_now()))
            if status == "deleted":
                due = iso(utc_now() + timedelta(days=36500))
                lock = None
                conn.execute(
                    """UPDATE alerts SET status='cancelled',next_attempt_at=NULL,error_summary='job_deleted'
                    WHERE job_id=? AND status IN ('pending','failed')""", (job_id,),
                )
            lock_run_id = row["lock_run_id"] if status != "deleted" else None
            conn.execute("""UPDATE jobs SET status=?,next_check_at=?,lock_until=?,lock_run_id=?,
                         lock_owner_token=CASE WHEN ?='deleted' THEN NULL ELSE lock_owner_token END,
                         edit_version=edit_version+?,updated_at=? WHERE id=?""",
                         (status, due, lock, lock_run_id, status, int(status != row["status"] or status == "paused"), iso(), job_id))
            return self._job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(), conn)

    def set_job_due(self, job_id, due_at):
        with self.database.connection() as conn:
            row = conn.execute("SELECT status,lock_until,last_started_at,last_finished_at FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] == "deleted":
                raise NotFoundError("Job not found")
            if row["status"] == "paused":
                raise ConflictError("Paused jobs cannot be run; resume the job first")
            if row["lock_until"] and parse_time(row["lock_until"]) > utc_now():
                raise ConflictError("This job already has a check in progress")
            due = self._minimum_due(row, due_at)
            conn.execute("UPDATE jobs SET next_check_at=?,updated_at=? WHERE id=?",
                         (precise_iso(due), iso(), job_id))
            return due

    def claim_due_jobs(self, limit=10):
        claimed = []
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            now_text = precise_iso(now)
            self._reconcile_runs(conn, now)
            rows = conn.execute("""SELECT j.* FROM jobs j LEFT JOIN check_intents i ON i.job_id=j.id AND i.status='queued'
                LEFT JOIN check_runs continuation ON continuation.id=j.lock_run_id AND continuation.outcome='yielded'
                WHERE j.status!='deleted' AND (j.lock_until IS NULL OR j.lock_until<=?)
                AND (continuation.id IS NOT NULL OR (i.id IS NOT NULL AND i.eligible_at<=? AND i.search_revision=j.search_revision
                  AND i.status_version=j.status_version AND (j.status='active' OR i.paused_manual=1)
                  AND (i.triggered_by='quiet_hours' OR j.last_extra_started_at IS NULL OR j.last_extra_started_at<=?))
                  OR (j.status='active' AND j.next_check_at<=?))
                ORDER BY COALESCE(j.last_served_at,''),
                CASE WHEN i.id IS NOT NULL AND i.eligible_at<=? THEN i.eligible_at ELSE j.next_check_at END,j.rowid LIMIT ?""",
                (now_text,now_text,precise_iso(now-timedelta(seconds=60)),now_text,now_text,limit)).fetchall()
            for row in rows:
                last_served = conn.execute("SELECT MAX(last_served_at) FROM jobs").fetchone()[0]
                served_at = max(now, parse_time(last_served)+timedelta(microseconds=1)) if last_served else now
                conn.execute("UPDATE jobs SET last_served_at=? WHERE id=?", (precise_iso(served_at), row['id']))
                continuation = conn.execute("SELECT * FROM check_runs WHERE id=? AND outcome='yielded'", (row['lock_run_id'],)).fetchone()
                if continuation:
                    owner_token = new_id()
                    conn.execute("UPDATE check_runs SET outcome='running',owner_token=?,generation=generation+1 WHERE id=?",
                                 (owner_token,continuation['id']))
                    conn.execute("UPDATE jobs SET lock_until=?,lock_owner_token=? WHERE id=?",
                                 (precise_iso(now+timedelta(minutes=10)),owner_token,row['id']))
                    job = dict(row)
                    job.update(owner_token=owner_token,search_snapshot=json.loads(continuation['search_snapshot']),
                               search_revision=continuation['search_revision'],intent_id=continuation['intent_id'],
                               triggered_by=continuation['triggered_by'],paused_manual=continuation['paused_manual'],
                               target_cursor=continuation['target_cursor'],generation=continuation['generation']+1,
                               successful_targets=continuation['successful_targets'],failed_targets=continuation['failed_targets'])
                    claimed.append((continuation['id'],job))
                    continue
                intent = conn.execute("SELECT * FROM check_intents WHERE job_id=? AND status='queued'", (row['id'],)).fetchone()
                extra_run = intent is not None and intent['triggered_by'] != 'quiet_hours'
                extra_ready = (intent is not None and parse_time(intent['eligible_at'])<=now
                    and intent['search_revision']==row['search_revision'] and intent['status_version']==row['status_version']
                    and (row['status']=='active' or intent['paused_manual'])
                    and (not extra_run or not row['last_extra_started_at']
                         or parse_time(row['last_extra_started_at'])<=now-timedelta(seconds=60)))
                if not extra_ready:
                    intent = None
                run_id, owner_token = new_id(), new_id()
                targets = [dict(target) for target in conn.execute("SELECT * FROM targets WHERE job_id=? AND active=1 ORDER BY rowid", (row['id'],))]
                search = {key: row[key] for key in SEARCH_FIELDS}
                search['telehealth'] = bool(search['telehealth'])
                _zone, earliest, latest = DoctolibClient._window(search, now)
                search.update(effective_earliest_date=earliest.isoformat(),effective_latest_date=latest.isoformat())
                snapshot = {'search':search,'targets':targets,'evaluated_at':now_text}
                conn.execute("""UPDATE jobs SET lock_until=?,lock_run_id=?,lock_owner_token=?,last_started_at=?,
                    last_extra_started_at=CASE WHEN ? THEN ? ELSE last_extra_started_at END WHERE id=?""",
                    (precise_iso(now+timedelta(minutes=10)),run_id,owner_token,now_text,
                     int(intent is not None and intent['triggered_by'] != 'quiet_hours'),now_text,row['id']))
                trigger = intent['triggered_by'] if intent else 'schedule'
                paused_manual = int(bool(intent and intent['paused_manual']))
                intent_id = intent['id'] if intent else None
                conn.execute("""INSERT INTO check_runs(id,job_id,job_name,requested_at,started_at,outcome,search_revision,
                    search_snapshot,snapshot_known,owner_token,triggered_by,intent_id,paused_manual,status_version)
                    VALUES(?,?,?,?,?,'running',?,?,1,?,?,?,?,?)""",
                    (run_id,row['id'],row['name'],intent['requested_at'] if intent else row['next_check_at'] or now_text,
                     now_text,row['search_revision'],json.dumps(snapshot,sort_keys=True),
                     owner_token,trigger,intent_id,paused_manual,row['status_version']))
                if intent:
                    conn.execute("UPDATE check_intents SET status='running',run_id=? WHERE id=? AND status='queued'", (run_id,intent_id))
                job = dict(row)
                job.update(owner_token=owner_token,search_snapshot=snapshot,intent_id=intent_id,triggered_by=trigger,
                           paused_manual=paused_manual,target_cursor=0,generation=1,successful_targets=0,failed_targets=0)
                claimed.append((run_id,job))
            conn.execute("""INSERT INTO worker_heartbeat(singleton_id,started_at,last_seen_at) VALUES(1,?,?)
                ON CONFLICT(singleton_id) DO UPDATE SET last_seen_at=excluded.last_seen_at""", (now_text,now_text))
        return claimed

    @staticmethod
    def _run_capable(job, run):
        return (job['status_version'] == run['status_version'] and
                (job['status']=='active' or (job['status']=='paused' and run['paused_manual'] and
                    job['search_revision']==run['search_revision'])))

    def run_can_check(self, job_id, run_id, owner_token):
        with self.database.connection() as conn:
            run = self._owned(conn,job_id,run_id,owner_token)
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return bool(run and job and self._run_capable(job,run) and job['search_revision']==run['search_revision'])

    def renew_job_lock(self, job_id, run_id, owner_token=None, lease_minutes=10, *, lease_seconds=None):
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = self._owned(conn,job_id,run_id,owner_token)
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not run or not job or not self._run_capable(job,run):
                return False
            conn.execute("UPDATE jobs SET lock_until=? WHERE id=? AND lock_run_id=? AND lock_owner_token=?",
                (precise_iso(utc_now()+timedelta(seconds=lease_seconds if lease_seconds is not None else lease_minutes*60)),job_id,run_id,owner_token))
            return True

    def _owned(self, conn, job_id, run_id, owner_token):
        if not owner_token:
            return None
        return conn.execute(
            """SELECT r.* FROM check_runs r JOIN jobs j ON j.id=r.job_id WHERE r.id=? AND r.job_id=?
            AND r.outcome='running' AND r.owner_token=? AND j.lock_owner_token=?
            AND j.lock_run_id=r.id AND j.lock_until>?""",
            (run_id, job_id, owner_token, owner_token, precise_iso()),
        ).fetchone()

    def owns_run(self, job_id, run_id, owner_token):
        with self.database.connection() as conn:
            return self._owned(conn, job_id, run_id, owner_token) is not None

    def get_targets(self, job_id):
        with self.database.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM targets WHERE job_id=? AND active=1 ORDER BY rowid", (job_id,)
            ).fetchall()]

    def get_target(self, job_id, target_id):
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM targets WHERE job_id=? AND id=? AND active=1",
                (job_id, target_id),
            ).fetchone()
            return dict(row) if row else None

    def insert_result(self, run_id, job, target, result, *, owner_token=None):
        result_id = new_id()
        earliest_slot = iso(result.earliest_slot) if result.earliest_slot else None
        if result.status == "available" and earliest_slot is None:
            raise ValueError("An available result requires an earliest slot")
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = self._owned(conn, job["id"], run_id, owner_token)
            if run is None:
                raise LeaseLostError("Run no longer owns a live lease")
            snapshot = json.loads(run["search_snapshot"])
            saved_target = next((item for item in snapshot["targets"] if item["id"] == target["id"]), None)
            if saved_target is None:
                raise ValueError("Target is not in the run snapshot")
            target = saved_target
            previous = conn.execute("SELECT id FROM check_results WHERE run_id=? AND target_id=?", (run_id, target["id"])).fetchone()
            if previous:
                return previous["id"]
            if run['target_cursor'] >= len(snapshot['targets']) or snapshot['targets'][run['target_cursor']]['id'] != target['id']:
                raise ValueError("Target is not the next uncompleted snapshot target")
            current = conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
            active = conn.execute("SELECT active FROM targets WHERE id=? AND job_id=?", (target["id"], job["id"])).fetchone()
            published = (self._run_capable(current, run) and current["search_revision"] == run["search_revision"]
                         and active is not None and active["active"])
            conn.execute(
                """INSERT INTO check_results
                (id,run_id,job_id,target_id,practitioner_name,practice_name,booking_url,checked_at,status,
                 slot_count,earliest_slot,count_complete,error_code,error_message,error_category,upstream_status,
                 retry_at,search_revision,snapshot_known,published)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (result_id, run_id, job["id"], target["id"], target["practitioner_name"],
                 target["practice_name"], target["booking_url"], iso(), result.status, result.slot_count,
                 earliest_slot, int(result.count_complete),
                 result.error_code, result.error_message, result.error_category, result.upstream_status,
                 iso(result.retry_at) if result.retry_at else None,
                 run["search_revision"], 1, int(published)),
            )
            completed = {row[0] for row in conn.execute("SELECT target_id FROM check_results WHERE run_id=?", (run_id,))}
            cursor = run['target_cursor']
            while cursor < len(snapshot['targets']) and snapshot['targets'][cursor]['id'] in completed:
                cursor += 1
            conn.execute("""UPDATE check_runs SET target_cursor=?,
                successful_targets=successful_targets+?,failed_targets=failed_targets+? WHERE id=?""",
                (cursor,int(result.status!='error'),int(result.status=='error'),run_id))
            if published and result.status in ("available", "no_availability"):
                state = conn.execute(
                    "SELECT last_status,last_earliest_slot,episode FROM target_alert_state WHERE target_id=?",
                    (target["id"],),
                ).fetchone()
                episode = state["episode"] if state else 0
                if result.status == "available" and state and state["last_status"] is not None:
                    if state["last_status"] != "available" or state["last_earliest_slot"] != earliest_slot:
                        episode += 1
                conn.execute(
                    """INSERT INTO target_alert_state(target_id,last_status,last_earliest_slot,episode)
                    VALUES(?,?,?,?) ON CONFLICT(target_id) DO UPDATE SET
                    last_status=excluded.last_status,last_earliest_slot=excluded.last_earliest_slot,
                    episode=excluded.episode""",
                    (target["id"], result.status, earliest_slot, episode),
                )
                if result.status == "no_availability":
                    conn.execute(
                        """UPDATE alerts SET status='cancelled',next_attempt_at=NULL,
                        error_summary='availability_disappeared'
                        WHERE target_id=? AND status IN ('pending','failed')""", (target["id"],),
                    )
                else:
                    current_key = alert_dedupe_key(target["id"], earliest_slot, episode)
                    conn.execute(
                        """UPDATE alerts SET status='cancelled',next_attempt_at=NULL,
                        error_summary='earliest_slot_changed'
                        WHERE target_id=? AND status IN ('pending','failed') AND event_id NOT IN (SELECT id FROM availability_events WHERE dedupe_key=?)""",
                        (target["id"], current_key),
                    )
                    conn.execute(
                        """UPDATE alerts SET result_id=?,search_revision=? WHERE target_id=?
                        AND event_id IN (SELECT id FROM availability_events WHERE dedupe_key=?)
                        AND status IN ('pending','failed') AND claim_owner_token IS NULL""",
                        (result_id, run["search_revision"], target["id"], current_key),
                    )
                    self._observe_event(conn, current, target, result_id, current_key, run['search_revision'])
        return result_id

    def insert_error_result(self, run_id, job, target, error_code, error_message, *,
                            error_category="unknown", upstream_status=None, retry_at=None, owner_token=None):
        class ErrorResult:
            status = "error"
            slot_count = 0
            earliest_slot = None
            count_complete = False

        result = ErrorResult()
        result.error_code = error_code
        result.error_message = error_message
        result.error_category = error_category
        result.upstream_status = upstream_status
        result.retry_at = retry_at
        return self.insert_result(run_id, job, target, result, owner_token=owner_token)

    def result_id(self, run_id, target_id):
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM check_results WHERE run_id=? AND target_id=? ORDER BY checked_at DESC LIMIT 1",
                (run_id, target_id),
            ).fetchone()
            return row["id"] if row else None

    @staticmethod
    def _set_job_channels(conn, job_id, channel_ids):
        if len(set(channel_ids)) != len(channel_ids):
            raise ValueError("Channel selections must be unique")
        for channel_id in channel_ids:
            if not conn.execute("SELECT 1 FROM notification_channels WHERE id=? AND deleted=0", (channel_id,)).fetchone():
                raise ValueError("Selected channel is unavailable")
        conn.execute("DELETE FROM job_channels WHERE job_id=?", (job_id,))
        conn.executemany("INSERT INTO job_channels(job_id,channel_config_id) VALUES(?,?)",
                         [(job_id, channel_id) for channel_id in channel_ids])

    def _observe_event(self, conn, job, target, result_id, dedupe, revision):
        if conn.execute("SELECT 1 FROM availability_events WHERE dedupe_key=?", (dedupe,)).fetchone():
            return
        channels = [dict(row) for row in conn.execute("""SELECT c.*
            FROM job_channels jc JOIN notification_channels c ON c.id=jc.channel_config_id
            WHERE jc.job_id=? AND c.enabled=1 AND c.deleted=0
            AND """+COMPLETE_SQL, (job['id'],))] if job['telegram_enabled'] else []
        channels = [channel for channel in channels if channel['type'] != 'email' or
                    self._email_channel_usable(conn,channel)]
        event_id, now = new_id(), precise_iso()
        quiet_until = (quiet_window(parse_time(now), job['time_zone'], job['quiet_hours_start'], job['quiet_hours_end'])
                       if job['quiet_hours_enabled'] else None)
        quiet_state = 'held' if quiet_until else 'released'
        conn.execute("""INSERT INTO availability_events
            (id,job_id,target_id,result_id,dedupe_key,search_revision,observed_at,routed_at,routing_snapshot)
            VALUES(?,?,?,?,?,?,?,?,?)""", (event_id,job['id'],target['id'],result_id,dedupe,revision,now,now,json.dumps(channels)))
        content = self._effective_content(conn, job)
        setting = conn.execute('SELECT content_version FROM settings WHERE singleton_id=1').fetchone()
        content_version = job['content_version'] if job['message_content'] is not None else (setting['content_version'] if setting else 1)
        result = dict(conn.execute('SELECT * FROM check_results WHERE id=?',(result_id,)).fetchone())
        run = conn.execute('SELECT * FROM check_runs WHERE id=?',(result['run_id'],)).fetchone()
        event_snapshot = {key:result[key] for key in ('practitioner_name','practice_name','earliest_slot','booking_url','checked_at','slot_count')}
        event_snapshot.update(time_zone=job['time_zone'],job_name=job['name'],triggered_by=run['triggered_by'])
        for channel in channels:
            conn.execute("""INSERT INTO alerts
                (id,job_id,target_id,result_id,channel,event_type,dedupe_key,status,created_at,next_attempt_at,
                 search_revision,event_id,channel_config_id,destination_version,credential_version,channel_name,message_content,event_snapshot,content_version,policy_version,quiet_state,quiet_until)
                VALUES(?,?,?,?,?,'slot_found',?,'pending',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id(),job['id'],target['id'],result_id,channel['type'],dedupe+':channel:'+channel['id']+':destination:'+str(channel['destination_version']),
                 now,precise_iso(quiet_until) if quiet_until else now,revision,event_id,channel['id'],channel['destination_version'],channel['credential_version'],channel['name'],json.dumps(content),json.dumps({**event_snapshot, **({'ntfy_priority':channel['ntfy_priority']} if channel['type']=='ntfy' else {})}),content_version,job['policy_version'],quiet_state,precise_iso(quiet_until) if quiet_until else None))

    def create_alert(self, job, target, result_id, earliest_slot, *, owner_token=None):
        # Events and their original routing are committed with observation.
        # Reconfirming an episode never attaches recipients or revives cancellation.
        with self.database.connection() as conn:
            result = conn.execute("SELECT * FROM check_results WHERE id=?", (result_id,)).fetchone()
            if result is None or self._owned(conn, job['id'], result['run_id'], owner_token) is None:
                return None
            row = conn.execute("""SELECT id FROM alerts WHERE result_id=? AND status IN ('pending','failed')
                ORDER BY rowid LIMIT 1""", (result_id,)).fetchone()
            return row[0] if row else None

    def _channel_changed(self, conn, before, after, recover_failed=False):
        changed_destination = before['destination_version'] != after['destination_version']
        disabled = not after['enabled'] or after['deleted']
        incomplete = not channel_complete(after)
        if changed_destination or disabled or incomplete:
            reason = 'destination_changed' if changed_destination else ('channel_deleted' if after['deleted'] else ('channel_disabled' if disabled else 'channel_incomplete'))
            conn.execute("""UPDATE alerts SET status='cancelled',next_attempt_at=NULL,error_summary=?
                WHERE channel_config_id=? AND status IN ('pending','failed')""", (reason,after['id']))
        elif recover_failed and before['credential_version'] != after['credential_version']:
            now = utc_now()
            for row in self._delivery_rows(conn):
                if (row['channel_config_id'] == after['id'] and row['destination_version'] == after['destination_version']
                        and row['status'] == 'failed' and row['delivery_state'] in ('action_required','exhausted')
                        and not row['claim_owner_token'] and self._delivery_eligible(row, now)):
                    self._release_delivery(conn,row['id'],'ready')
                    conn.execute("""UPDATE alerts SET status='pending',credential_version=?,next_attempt_at=?,
                        delivery_epoch_at=?,delivery_epoch_attempts=0,error_summary=NULL WHERE id=?""",
                        (after['credential_version'],precise_iso(now),precise_iso(now),row['id']))

    @staticmethod
    def _delivery_rows(conn, alert_id=None):
        return conn.execute(
            """SELECT a.*,a.rowid AS alert_order,r.earliest_slot,r.slot_count,r.practitioner_name,r.practice_name,
            r.booking_url,r.checked_at,r.status AS result_status,r.search_revision AS result_revision,r.snapshot_known,r.published,
            latest.id AS latest_result_id,latest.status AS latest_result_status,
            latest.earliest_slot AS latest_earliest_slot,latest.checked_at AS latest_checked_at,
            j.search_revision AS current_revision,j.time_zone,j.status AS job_status,j.telegram_enabled,
            j.interval_seconds,j.next_check_at,j.last_started_at,j.last_finished_at,j.lock_until,
            j.quiet_hours_enabled,j.quiet_hours_start,j.quiet_hours_end,
            j.status_version AS current_status_version, cr.status_version AS run_status_version,
            cr.paused_manual,cr.intent_id,cr.job_name,cr.triggered_by,c.type AS channel_type,t.active AS target_active,s.last_status,s.last_earliest_slot,
            c.enabled AS channel_enabled,c.deleted AS channel_deleted,c.destination_version AS current_destination_version,
            c.credential_version AS current_credential_version,
            """+COMPLETE_SQL+""" AS channel_complete,
            EXISTS(SELECT 1 FROM job_channels jc WHERE jc.job_id=a.job_id AND jc.channel_config_id=a.channel_config_id) AS channel_selected
            FROM alerts a JOIN check_results r ON r.id=a.result_id JOIN check_runs cr ON cr.id=r.run_id
            JOIN jobs j ON j.id=a.job_id LEFT JOIN targets t ON t.id=a.target_id
            LEFT JOIN check_results latest ON latest.id=(SELECT x.id FROM check_results x
                WHERE x.target_id=a.target_id AND x.search_revision=j.search_revision
                AND x.snapshot_known=1 AND x.published=1 ORDER BY x.rowid DESC LIMIT 1)
            LEFT JOIN target_alert_state s ON s.target_id=a.target_id
            LEFT JOIN notification_channels c ON c.id=a.channel_config_id
            WHERE (? IS NULL OR a.id=?) ORDER BY COALESCE(a.next_attempt_at,a.created_at),a.created_at,a.rowid""",
            (alert_id, alert_id),
        ).fetchall()

    @staticmethod
    def _delivery_route_valid(alert, now):
        return (alert['status'] in ('pending', 'failed') and (alert['job_status'] == 'active' or (alert['job_status'] == 'paused' and alert['paused_manual']))
                and alert['current_status_version'] == alert['run_status_version']
                and alert['telegram_enabled'] and alert['target_active']
                and alert['channel_enabled'] and not alert['channel_deleted'] and alert['channel_complete']
                and alert['channel_selected'] and alert['destination_version'] == alert['current_destination_version']
                and alert['snapshot_known'] and alert['published']
                and alert['search_revision'] == alert['current_revision'] == alert['result_revision']
                and alert['earliest_slot'] and parse_time(alert['earliest_slot']) > now
                and alert['last_status'] == 'available' and alert['last_earliest_slot'] == alert['earliest_slot'])

    @staticmethod
    def _delivery_confirmed(alert, now):
        age = now - parse_time(alert['latest_checked_at']) if alert['latest_checked_at'] else None
        return (alert['latest_result_status'] == 'available'
                and alert['latest_earliest_slot'] == alert['earliest_slot']
                and age is not None and timedelta(0) <= age <= timedelta(seconds=alert['interval_seconds']))

    @staticmethod
    def _delivery_eligible(alert, now):
        return (Repository._delivery_route_valid(alert, now)
                and Repository._delivery_confirmed(alert, now)
                and alert['quiet_state'] == 'released'
                and (not alert['quiet_hours_enabled'] or quiet_window(
                    now, alert['time_zone'], alert['quiet_hours_start'], alert['quiet_hours_end']) is None))

    @staticmethod
    def _normal_check_due(job, now):
        due = max(now, parse_time(job['next_check_at']))
        for field in ('last_started_at', 'last_finished_at'):
            if job[field]:
                due = max(due, parse_time(job[field]) + timedelta(seconds=job['interval_seconds']))
        return due

    @staticmethod
    def _queue_quiet_refresh(conn, alert, now):
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (alert['job_id'],)).fetchone()
        if job is None or job['status'] != 'active':
            return None
        due = Repository._normal_check_due(job, now)
        if job['lock_until'] and parse_time(job['lock_until']) > now:
            return max(due, parse_time(job['lock_until']))
        intent = conn.execute("""SELECT * FROM check_intents WHERE job_id=? AND status IN ('queued','running')
            ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END,rowid DESC LIMIT 1""", (job['id'],)).fetchone()
        if intent:
            if intent['status'] == 'running':
                return due
            if intent['search_revision'] == job['search_revision'] and intent['status_version'] == job['status_version']:
                return max(due, parse_time(intent['eligible_at']))
            conn.execute("UPDATE check_intents SET status='cancelled',cancel_reason='search_edited' WHERE id=?", (intent['id'],))
        conn.execute("""INSERT INTO check_intents(id,job_id,search_revision,triggered_by,requested_at,eligible_at,
            status,paused_manual,status_version) VALUES(?,?,?,'quiet_hours',?,?,'queued',0,?)""",
            (new_id(), job['id'], job['search_revision'], precise_iso(now), precise_iso(due), job['status_version']))
        return due

    @staticmethod
    def _refresh_quiet_delivery(conn, alert, now):
        if (alert['status'] not in ('pending', 'failed') or
                (alert['claim_owner_token'] and alert['attempt_started_at']) or
                not Repository._delivery_route_valid(alert, now)):
            return
        fresh = Repository._delivery_confirmed(alert, now)
        if alert['job_status'] == 'paused' and alert['paused_manual'] and not fresh:
            next_attempt = alert['next_attempt_at'] if alert['delivery_state'] == 'retry' else None
            conn.execute("UPDATE alerts SET quiet_state='needs_manual_check',quiet_until=NULL,next_attempt_at=? WHERE id=?",
                         (next_attempt, alert['id']))
            return
        quiet_until = None
        if alert['quiet_hours_enabled']:
            quiet_until = quiet_window(now, alert['time_zone'], alert['quiet_hours_start'], alert['quiet_hours_end'])
        if quiet_until:
            conn.execute("UPDATE alerts SET quiet_state='held',quiet_until=? WHERE id=?",
                         (precise_iso(quiet_until), alert['id']))
            return
        if fresh:
            next_attempt = alert['next_attempt_at'] if alert['delivery_state'] == 'retry' else precise_iso(now)
            conn.execute("UPDATE alerts SET quiet_state='released',quiet_until=NULL,next_attempt_at=? WHERE id=?",
                         (next_attempt, alert['id']))
            return
        if alert['job_status'] == 'active':
            Repository._queue_quiet_refresh(conn, alert, now)
            conn.execute("UPDATE alerts SET quiet_state='waiting_for_fresh_check',quiet_until=NULL WHERE id=?",
                         (alert['id'],))

    @staticmethod
    def _delivery_budget(alert, now):
        return (alert['delivery_epoch_attempts'] < 5 and
                now - parse_time(alert['delivery_epoch_at'] or alert['created_at']) < timedelta(hours=24))

    @staticmethod
    def _public_alert(alert, presentation=True):
        alert = dict(alert)
        if alert.get('status') == 'cancelled':
            alert['quiet_state'] = 'cancelled'
            alert['quiet_until'] = None
        elif alert.get('status') == 'sent':
            alert['quiet_state'] = 'released'
            alert['quiet_until'] = None
        if presentation and alert.get('event_snapshot'):
            alert.update(json.loads(alert['event_snapshot']))
        if alert.get('message_content'):
            alert['message_content'] = json.loads(alert['message_content'])
        alert.pop('event_snapshot', None)
        for key in ('claim_owner_token', 'claim_result_id', 'claim_search_revision'):
            alert.pop(key, None)
        return alert

    @staticmethod
    def _cancel_invalid_deliveries(conn, rows, now):
        for row in rows:
            if row['status'] not in ('pending', 'failed') or row['attempt_started_at']:
                continue
            reason = None
            if row['job_status'] == 'deleted':
                reason = 'job_deleted'
            elif row['current_status_version'] != row['run_status_version']:
                reason = 'status_changed'
            elif row['search_revision'] != row['current_revision']:
                reason = 'search_edited'
            elif not row['target_active']:
                reason = 'target_removed'
            elif not row['channel_selected']:
                reason = 'channel_detached'
            elif row['channel_deleted'] or row['channel_deleted'] is None:
                reason = 'channel_deleted'
            elif not row['channel_enabled']:
                reason = 'channel_disabled'
            elif row['destination_version'] != row['current_destination_version']:
                reason = 'destination_changed'
            elif not row['channel_complete']:
                reason = 'channel_incomplete'
            elif (row['snapshot_known'] and row['published'] and
                  row['search_revision'] == row['current_revision'] == row['result_revision']):
                if not row['earliest_slot'] or parse_time(row['earliest_slot']) <= now:
                    reason = 'slot_expired'
                elif row['last_status'] != 'available' or row['last_earliest_slot'] != row['earliest_slot']:
                    reason = 'availability_changed'
            if reason:
                conn.execute("UPDATE alerts SET status='cancelled',next_attempt_at=NULL,error_summary=? WHERE id=?",
                             (reason, row['id']))

    def get_pending_alerts(self, limit=20, alert_id=None):
        """Eligible preview with invalid-event cleanup; never authorizes delivery."""
        with self.database.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            now = utc_now()
            self._cancel_invalid_deliveries(conn, self._delivery_rows(conn, alert_id), now)
            return [self._public_alert(row) for row in self._delivery_rows(conn, alert_id)
                    if row['delivery_state'] in ('ready', 'retry') and not row['claim_owner_token']
                    and self._delivery_eligible(row, now) and self._delivery_budget(row, now)
                    and (not row['next_attempt_at'] or parse_time(row['next_attempt_at']) <= now)][:limit]

    @staticmethod
    def _release_delivery(conn, alert_id, state, outcome=None):
        conn.execute("""UPDATE alerts SET delivery_state=?,claim_owner_token=NULL,claim_until=NULL,
                     claim_result_id=NULL,claim_search_revision=NULL,attempt_started_at=NULL,
                     last_attempt_outcome=COALESCE(?,last_attempt_outcome) WHERE id=?""",
                     (state, outcome, alert_id))

    def reconcile_alert_claims(self):
        with self.database.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            now = utc_now()
            rows = conn.execute("SELECT * FROM alerts WHERE claim_owner_token IS NOT NULL AND claim_until<=?",
                                (precise_iso(now),)).fetchall()
            for row in rows:
                attempted = bool(row['attempt_started_at'])
                self._release_delivery(conn, row['id'], 'uncertain' if attempted else
                                       ('retry' if row['attempt_count'] else 'ready'),
                                       'uncertain' if attempted else None)
                if attempted:
                    conn.execute("""UPDATE alerts SET status=CASE WHEN status IN ('sent','cancelled') THEN status ELSE 'failed' END,
                                 next_attempt_at=NULL,error_summary='delivery_acknowledgement_unknown' WHERE id=?""", (row['id'],))
            return len(rows)

    def claim_alert(self, lease_seconds=120):
        if not 30 <= lease_seconds <= 300:
            raise ValueError('Delivery lease must be between 30 and 300 seconds')
        with self.database.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            now = utc_now()
            self._cancel_invalid_deliveries(conn, self._delivery_rows(conn), now)
            rows = self._delivery_rows(conn)
            for row in rows:
                self._refresh_quiet_delivery(conn, row, now)
            rows = self._delivery_rows(conn)
            newest_unsent = {}
            held_states = ('held', 'waiting_for_fresh_check', 'needs_manual_check')
            for row in rows:
                if row['status'] not in ('pending', 'failed') or not self._delivery_route_valid(row, now):
                    continue
                key = (row['job_id'], row['target_id'], row['channel_config_id'], row['destination_version'])
                prior = newest_unsent.get(key)
                if prior is None or (row['created_at'], row['alert_order']) > (prior['created_at'], prior['alert_order']):
                    newest_unsent[key] = row
            for row in rows:
                if row['quiet_state'] not in held_states or row['status'] not in ('pending', 'failed'):
                    continue
                key = (row['job_id'], row['target_id'], row['channel_config_id'], row['destination_version'])
                newest = newest_unsent.get(key)
                if newest and row['event_id'] != newest['event_id']:
                    conn.execute("""UPDATE alerts SET status='cancelled',quiet_state='cancelled',quiet_until=NULL,
                        next_attempt_at=NULL,error_summary='quiet_superseded' WHERE id=? AND status IN ('pending','failed')""",
                        (row['id'],))
            for row in self._delivery_rows(conn):
                if row['status'] not in ('pending', 'failed') or row['claim_owner_token']:
                    continue
                if row['delivery_state'] not in ('ready', 'retry'):
                    continue
                if not self._delivery_budget(row, now):
                    conn.execute("UPDATE alerts SET delivery_state='exhausted',next_attempt_at=NULL WHERE id=?", (row['id'],))
                    continue
                if not self._delivery_eligible(row, now):
                    # Invalid evidence is dormant: a current fresh result may
                    # confirm the same episode without creating another event.
                    continue
                if row['next_attempt_at'] and parse_time(row['next_attempt_at']) > now:
                    continue
                token = new_id()
                conn.execute("""UPDATE alerts SET claim_owner_token=?,claim_until=?,claim_result_id=result_id,
                             claim_search_revision=search_revision,credential_version=?,attempt_started_at=NULL WHERE id=?""",
                             (token, precise_iso(now + timedelta(seconds=lease_seconds)), row['current_credential_version'],row['id']))
                alert = self._public_alert(row)
                alert['owner_token'] = token
                alert['credential_version'] = row['current_credential_version']
                alert['claim_until'] = precise_iso(now + timedelta(seconds=lease_seconds))
                return alert
        return None

    def begin_alert_attempt(self, alert_id, owner_token, wait_seconds=10):
        wait_seconds = float(wait_seconds)
        if not math.isfinite(wait_seconds):
            raise ValueError('Delivery guard wait must be finite')
        wait_ms = int(min(10, max(0, wait_seconds)) * 1000)
        with self.database.connection() as conn:
            # Set the bounded guard wait before obtaining the writer lock.
            # The transport passes its remaining total work budget here.
            conn.execute(f'PRAGMA busy_timeout={wait_ms}')
            conn.execute('BEGIN IMMEDIATE')
            now = utc_now()
            rows = self._delivery_rows(conn, alert_id)
            if not rows:
                return None
            row = rows[0]
            if (not owner_token or row['claim_owner_token'] != owner_token or not row['claim_until']
                    or parse_time(row['claim_until']) <= now or row['attempt_started_at']):
                return None
            if (row['claim_result_id'] != row['result_id'] or row['claim_search_revision'] != row['search_revision']
                    or row['credential_version'] != row['current_credential_version']
                    or not self._delivery_budget(row, now)):
                self._release_delivery(conn, alert_id, 'exhausted' if not self._delivery_budget(row, now)
                                       else ('retry' if row['attempt_count'] else 'ready'))
                return None
            self._refresh_quiet_delivery(conn, row, now)
            row = self._delivery_rows(conn, alert_id)[0]
            if not self._delivery_eligible(row, now):
                self._release_delivery(conn, alert_id, 'retry' if row['attempt_count'] else 'ready')
                return None
            conn.execute("""UPDATE alerts SET attempt_count=attempt_count+1,delivery_epoch_attempts=delivery_epoch_attempts+1,attempt_started_at=?,last_attempt_at=?,
                         last_attempt_outcome='started',delivery_epoch_at=COALESCE(delivery_epoch_at,created_at)
                         WHERE id=?""", (precise_iso(now), precise_iso(now), alert_id))
            alert = self._public_alert(row)
            alert['attempt_count'] += 1
            alert['delivery_epoch_attempts'] += 1
            alert['owner_token'] = owner_token
            return alert

    def finish_unstarted_delivery(self, alert_id, owner_token, outcome, error_code=None, retry_after=None):
        """Release a proven pre-network failure without counting an attempt."""
        if outcome not in ('retry', 'action_required'):
            raise ValueError('Unstarted delivery must be retryable or action-required')
        if error_code and (len(error_code) > 80 or not all(c.isalnum() or c == '_' for c in error_code)):
            raise ValueError('Delivery error must be a safe machine code')
        with self.database.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            now = utc_now()
            row = conn.execute('SELECT * FROM alerts WHERE id=?', (alert_id,)).fetchone()
            if (row is None or not owner_token or row['claim_owner_token'] != owner_token
                    or row['attempt_started_at'] or not row['claim_until']
                    or parse_time(row['claim_until']) <= now):
                return False
            state = outcome
            next_attempt = None
            if outcome == 'retry':
                state = 'retry' if self._delivery_budget(row, now) else 'exhausted'
                delay = 5
                if retry_after is not None:
                    delay = min(900, max(5, float(retry_after)))
                if state == 'retry':
                    next_attempt = precise_iso(now + timedelta(seconds=delay))
            self._release_delivery(conn, alert_id, state)
            conn.execute("""UPDATE alerts SET status=CASE WHEN status='cancelled' THEN status ELSE 'failed' END,
                         next_attempt_at=?,error_summary=CASE WHEN status='cancelled' THEN error_summary ELSE ? END WHERE id=?""", (next_attempt, error_code, alert_id))
            return True

    def finish_delivery(self, alert_id, owner_token, outcome, error_code=None, retry_after=None):
        if outcome not in ('sent', 'retry', 'action_required', 'uncertain'):
            raise ValueError('Unknown delivery outcome')
        # Store only a bounded machine code; provider response bodies and URLs
        # are never delivery history.
        if error_code and (len(error_code) > 80 or not all(c.isalnum() or c == '_' for c in error_code)):
            raise ValueError('Delivery error must be a safe machine code')
        with self.database.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            now = utc_now()
            row = conn.execute('SELECT * FROM alerts WHERE id=?', (alert_id,)).fetchone()
            if (row is None or not owner_token or row['claim_owner_token'] != owner_token
                    or not row['attempt_started_at'] or parse_time(row['claim_until']) <= now):
                return False
            state = outcome
            next_attempt = None
            if outcome == 'retry':
                state = 'retry' if self._delivery_budget(row, now) else 'exhausted'
                delay = min(900, 5 * 2 ** max(0, row['delivery_epoch_attempts'] - 1))
                if retry_after is not None:
                    delay = max(delay, min(900, max(5, float(retry_after))))
                if state == 'retry':
                    next_attempt = precise_iso(now + timedelta(seconds=delay))
            self._release_delivery(conn, alert_id, state, outcome)
            conn.execute("""UPDATE alerts SET status=?,sent_at=?,next_attempt_at=?,error_summary=CASE WHEN status='cancelled' THEN error_summary ELSE ? END WHERE id=?""",
                         ('cancelled' if row['status'] == 'cancelled' else ('sent' if outcome == 'sent' else 'failed'),
                          precise_iso(now) if outcome == 'sent' else None, next_attempt, error_code, alert_id))
            return True

    def recover_alert(self, alert_id, acknowledge_duplicate_risk=False):
        with self.database.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            now = utc_now()
            rows = self._delivery_rows(conn, alert_id)
            if not rows:
                raise NotFoundError('Alert not found')
            row = dict(rows[0])
            cancelled = row['status'] == 'cancelled'
            if row['status'] == 'sent' or row['sent_at'] or (not cancelled and row['delivery_state'] not in ('action_required', 'exhausted', 'uncertain')):
                raise ConflictError('Alert is not recoverable')
            if row['last_attempt_outcome'] == 'sent':
                raise ConflictError('A delivered alert is not recoverable')
            if row['delivery_state'] == 'uncertain' and not acknowledge_duplicate_risk:
                raise ConflictError('Uncertain delivery requires duplicate-risk acknowledgement')
            if row['claim_owner_token']:
                raise ConflictError('Alert is still claimed')
            if cancelled:
                event = conn.execute('SELECT * FROM availability_events WHERE id=?', (row['event_id'],)).fetchone()
                authorized = event and any(c['id'] == row['channel_config_id'] and c['destination_version'] == row['destination_version']
                    for c in json.loads(event['routing_snapshot']))
                current_state = conn.execute('SELECT * FROM target_alert_state WHERE target_id=?', (row['target_id'],)).fetchone()
                same_episode = current_state and current_state['last_status'] == 'available' and event and event['dedupe_key'] == alert_dedupe_key(row['target_id'],current_state['last_earliest_slot'],current_state['episode'])
                if not authorized or not same_episode:
                    return False
                # Explicit same-recipient recovery may use a newer owned
                # confirmation. It never redirects an old event to a new recipient.
                result = conn.execute("""SELECT id,search_revision FROM check_results WHERE target_id=?
                    AND status='available' AND earliest_slot=? AND published=1 AND search_revision=?
                    ORDER BY rowid DESC LIMIT 1""", (row['target_id'],row['earliest_slot'],row['current_revision'])).fetchone()
                if result is None:
                    return False
                conn.execute('SAVEPOINT recovery')
                conn.execute("UPDATE alerts SET status='pending',result_id=?,search_revision=? WHERE id=?",
                    (result['id'],result['search_revision'],alert_id))
                row = dict(self._delivery_rows(conn, alert_id)[0])
                if not self._delivery_eligible(row, now):
                    conn.execute('ROLLBACK TO recovery')
                    return False
                conn.execute('RELEASE recovery')
            elif not self._delivery_eligible(row, now):
                return False
            self._release_delivery(conn, alert_id, 'ready')
            conn.execute("""UPDATE alerts SET status='pending',credential_version=?,delivery_epoch_attempts=0,
                delivery_epoch_at=?,next_attempt_at=?,error_summary=NULL WHERE id=?""",
                (row['current_credential_version'],precise_iso(now),precise_iso(now),alert_id))
            return True

    def touch_dispatcher(self, last_error=None):
        if last_error and (len(last_error) > 80 or not all(c.isalnum() or c == '_' for c in last_error)):
            raise ValueError('Dispatcher error must be a safe machine code')
        now = iso()
        with self.database.connection() as conn:
            conn.execute("""INSERT INTO dispatcher_heartbeat(singleton_id,started_at,last_seen_at,last_error)
                         VALUES(1,?,?,?) ON CONFLICT(singleton_id) DO UPDATE SET
                         last_seen_at=excluded.last_seen_at,last_error=excluded.last_error""", (now, now, last_error))

    def dispatcher_status(self):
        with self.database.connection() as conn:
            heartbeat = conn.execute('SELECT * FROM dispatcher_heartbeat WHERE singleton_id=1').fetchone()
            rows = conn.execute("""SELECT CASE WHEN claim_owner_token IS NOT NULL THEN 'in_flight' ELSE delivery_state END AS delivery_state,COUNT(*) AS count FROM alerts
                                WHERE status IN ('pending','failed') GROUP BY 1""").fetchall()
            return {'heartbeat': dict(heartbeat) if heartbeat else None,
                    'backlog': {**dict.fromkeys(('ready', 'retry', 'in_flight', 'action_required', 'exhausted', 'uncertain'), 0), **{row['delivery_state']: row['count'] for row in rows}}}

    def finish_run(self, run_id, job_id, successful, failed, interval_seconds, error=None, *, owner_token=None):
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            run = self._owned(conn, job_id, run_id, owner_token)
            if run is None:
                return False
            snapshot = json.loads(run["search_snapshot"])
            target_ids = {target["id"] for target in snapshot["targets"]}
            results = conn.execute("SELECT target_id,status FROM check_results WHERE run_id=?", (run_id,)).fetchall()
            covered = {result["target_id"] for result in results}
            successful = sum(result["status"] != "error" for result in results)
            failed = sum(result["status"] == "error" for result in results)
            outcome = ("interrupted" if covered != target_ids else
                       "error" if failed and not successful else "partial_error" if failed else "completed")
            job_row = conn.execute("SELECT interval_seconds FROM jobs WHERE id=?", (job_id,)).fetchone()
            effective_interval = max(self.minimum_poll_interval_seconds, job_row["interval_seconds"])
            conn.execute(
                "UPDATE check_runs SET finished_at=?,outcome=?,successful_targets=?,failed_targets=? WHERE id=?",
                (precise_iso(now), outcome, successful, failed, run_id),
            )
            conn.execute(
                """UPDATE jobs SET next_check_at=CASE WHEN status='active' THEN ? ELSE next_check_at END,
                last_finished_at=?,last_outcome=?,lock_until=NULL,lock_run_id=NULL,lock_owner_token=NULL
                WHERE id=? AND lock_run_id=? AND lock_owner_token=?""",
                (precise_iso(now + timedelta(seconds=effective_interval)), precise_iso(now), outcome,
                 job_id, run_id, owner_token),
            )
            if run['intent_id']:
                conn.execute("UPDATE check_intents SET status='completed' WHERE id=? AND status='running'", (run['intent_id'],))
            conn.execute(
                """INSERT INTO worker_heartbeat(singleton_id,started_at,last_seen_at,last_completed_run_at,last_error)
                VALUES(1,?,?,?,?) ON CONFLICT(singleton_id) DO UPDATE SET last_seen_at=excluded.last_seen_at,
                last_completed_run_at=excluded.last_completed_run_at,last_error=excluded.last_error""",
                (iso(now), iso(now), iso(now), error[:240] if error else None),
            )
            return True

    def yield_run(self, run_id, job_id, *, owner_token=None):
        """Release a live slice while retaining its logical run and intent."""
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = self._owned(conn, job_id, run_id, owner_token)
            if run is None:
                return False
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not self._run_capable(job,run) or job['search_revision'] != run['search_revision']:
                self._interrupt_run(conn,run,precise_iso())
                return False
            conn.execute("UPDATE check_runs SET outcome='yielded',owner_token=NULL WHERE id=?", (run_id,))
            conn.execute("UPDATE jobs SET lock_until=NULL,lock_owner_token=NULL WHERE id=? AND lock_run_id=?",
                         (job_id,run_id))
            return True

    @staticmethod
    def _interrupt_run(conn, run, now):
        conn.execute("UPDATE check_intents SET status='completed' WHERE run_id=? AND status='running'", (run['id'],))
        conn.execute("""UPDATE check_runs SET outcome='interrupted',finished_at=?,owner_token=NULL,
            successful_targets=(SELECT COUNT(*) FROM check_results WHERE run_id=check_runs.id AND status!='error'),
            failed_targets=(SELECT COUNT(*) FROM check_results WHERE run_id=check_runs.id AND status='error')
            WHERE id=?""", (now,run['id']))
        conn.execute("""UPDATE jobs SET lock_until=NULL,lock_run_id=NULL,lock_owner_token=NULL
            WHERE id=? AND lock_run_id=?""", (run['job_id'],run['id']))

    def _reconcile_runs(self, conn, now):
        now_text = precise_iso(now)
        runs = conn.execute("SELECT * FROM check_runs WHERE outcome IN ('running','yielded')").fetchall()
        for run in runs:
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (run['job_id'],)).fetchone()
            if (run['outcome']=='running' and job is not None and run['owner_token']
                    and job['lock_run_id']==run['id'] and job['lock_owner_token']==run['owner_token']
                    and job['lock_until'] and parse_time(job['lock_until'])>now):
                continue
            capable = (job is not None and run['snapshot_known'] and run['search_snapshot']
                       and self._run_capable(job,run) and job['search_revision']==run['search_revision']
                       and job['lock_run_id']==run['id'])
            if not capable:
                self._interrupt_run(conn,run,now_text)
            elif run['outcome']=='running' and (not job['lock_until'] or parse_time(job['lock_until'])<=now
                                                or job['lock_owner_token']!=run['owner_token']):
                conn.execute("UPDATE check_runs SET outcome='yielded',owner_token=NULL WHERE id=?", (run['id'],))
                conn.execute("UPDATE jobs SET lock_until=NULL,lock_owner_token=NULL WHERE id=?", (job['id'],))

    def interrupt_stale_runs(self):
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._reconcile_runs(conn, utc_now())

    def worker_status(self):
        with self.database.connection() as conn:
            row = conn.execute("SELECT * FROM worker_heartbeat WHERE singleton_id=1").fetchone()
            return dict(row) if row else None

    def touch_worker(self):
        now = iso()
        with self.database.connection() as conn:
            conn.execute(
                """INSERT INTO worker_heartbeat(singleton_id,started_at,last_seen_at)
                VALUES(1,?,?) ON CONFLICT(singleton_id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                (now, now),
            )

    def dashboard_status(self):
        with self.database.connection() as conn:
            current = utc_now()
            now = precise_iso(current)
            counts = conn.execute(
                "SELECT SUM(status='active') active_jobs,SUM(status='paused') paused_jobs FROM jobs WHERE status!='deleted'"
            ).fetchone()
            last_run = conn.execute(
                "SELECT id,job_id,job_name,finished_at,outcome FROM check_runs WHERE finished_at IS NOT NULL ORDER BY finished_at DESC,rowid DESC LIMIT 1"
            ).fetchone()
            next_check = conn.execute(
                "SELECT MIN(next_check_at) FROM jobs WHERE status='active'"
            ).fetchone()[0]
            overdue_jobs = conn.execute("""SELECT COUNT(*) FROM jobs WHERE status='active' AND next_check_at<=?
                AND (lock_until IS NULL OR lock_until<=?)""", (now, now)).fetchone()[0]
            candidates = conn.execute("""SELECT a.created_at,a.delivery_epoch_at,a.delivery_epoch_attempts,
                j.time_zone,j.quiet_hours_enabled,
                j.quiet_hours_start,j.quiet_hours_end FROM alerts a
                JOIN jobs j ON j.id=a.job_id
                JOIN check_results r ON r.id=a.result_id
                JOIN check_runs cr ON cr.id=r.run_id
                JOIN targets t ON t.id=a.target_id
                JOIN target_alert_state s ON s.target_id=a.target_id
                JOIN notification_channels c ON c.id=a.channel_config_id
                JOIN job_channels jc ON jc.job_id=a.job_id AND jc.channel_config_id=a.channel_config_id
                LEFT JOIN check_results latest ON latest.id=(SELECT x.id FROM check_results x
                    WHERE x.target_id=a.target_id AND x.search_revision=j.search_revision
                    AND x.snapshot_known=1 AND x.published=1 ORDER BY x.rowid DESC LIMIT 1)
                WHERE a.status IN ('pending','failed') AND a.delivery_state IN ('ready','retry')
                    AND a.quiet_state='released'
                    AND (a.next_attempt_at IS NULL OR a.next_attempt_at<=?)
                    AND (a.claim_owner_token IS NULL OR a.claim_until<=?)
                    AND a.attempt_started_at IS NULL
                    AND (j.status='active' OR (j.status='paused' AND cr.paused_manual=1))
                    AND j.status_version=cr.status_version AND j.telegram_enabled=1 AND t.active=1
                    AND c.enabled=1 AND c.deleted=0 AND """+COMPLETE_SQL+"""
                    AND a.destination_version=c.destination_version
                    AND r.snapshot_known=1 AND r.published=1
                    AND a.search_revision=j.search_revision AND r.search_revision=j.search_revision
                    AND r.earliest_slot IS NOT NULL AND r.earliest_slot>?
                    AND s.last_status='available' AND s.last_earliest_slot=r.earliest_slot
                    AND latest.status='available' AND latest.earliest_slot=r.earliest_slot
                    AND julianday(?)>=julianday(latest.checked_at)
                    AND (julianday(?) - julianday(latest.checked_at))*86400<=j.interval_seconds
                ORDER BY a.created_at,a.rowid""", (now, now, now, now, now)).fetchall()
            oldest_ready = None
            for row in candidates:
                if not self._delivery_budget(row, current):
                    continue
                if not row['quiet_hours_enabled']:
                    oldest_ready = row['created_at']
                    break
                try:
                    in_quiet_hours = quiet_window(
                        current, row['time_zone'], row['quiet_hours_start'], row['quiet_hours_end']) is not None
                except (KeyError, TypeError, ValueError):
                    in_quiet_hours = True
                if not in_quiet_hours:
                    oldest_ready = row['created_at']
                    break
            dispatcher = self.dispatcher_status()
            return {"active_jobs": counts["active_jobs"] or 0, "paused_jobs": counts["paused_jobs"] or 0,
                    "worker": self.worker_status(), "dispatcher": dispatcher["heartbeat"],
                    "delivery_backlog": dispatcher["backlog"], "overdue_jobs": overdue_jobs,
                    "oldest_ready_delivery_at": oldest_ready,
                    "last_completed_run": dict(last_run) if last_run else None,
                    "next_check_at": next_check}

    def checks(self, job_id, limit=50, offset=0):
        with self.database.connection() as conn:
            runs = [dict(row) for row in conn.execute(
                """SELECT id,job_id,job_name,started_at,finished_at,outcome,successful_targets,
                failed_targets,triggered_by,search_revision,search_snapshot,snapshot_known,intent_id,paused_manual,status_version,
                target_cursor FROM check_runs WHERE job_id=?
                ORDER BY started_at DESC,rowid DESC LIMIT ? OFFSET ?""",
                (job_id, limit, offset),
            ).fetchall()]
            if not runs:
                return []
            run_ids = [run["id"] for run in runs]
            placeholders = ",".join("?" for _run_id in run_ids)
            results = conn.execute(
                f"""SELECT * FROM check_results WHERE run_id IN ({placeholders})
                ORDER BY checked_at,rowid""", run_ids,
            ).fetchall()
            by_run = {run_id: [] for run_id in run_ids}
            for result in results:
                by_run[result["run_id"]].append(dict(result))
            for run in runs:
                run["search_snapshot"] = json.loads(run["search_snapshot"]) if run["search_snapshot"] else None
                run["results"] = by_run[run["id"]]
            return runs

    def activity(self, *, job_id=None, run_id=None, limit=25, offset=0, before_run_id=None):
        with self.database.connection() as conn:
            conn.execute("BEGIN")
            filters, args = [], []
            before = None
            if before_run_id:
                before = conn.execute(
                    "SELECT rowid,started_at FROM check_runs WHERE id=?", (before_run_id,)
                ).fetchone()
                if before is None:
                    return {"items": [], "has_more": False, "next_offset": None,
                            "next_before_run_id": None}
            if job_id:
                filters.append("r.job_id=?")
                args.append(job_id)
            if run_id:
                filters.append("r.id=?")
                args.append(run_id)
            if before:
                filters.append("(r.started_at<? OR (r.started_at=? AND r.rowid<?))")
                args.extend((before["started_at"], before["started_at"], before["rowid"]))
            where = "WHERE " + " AND ".join(filters) if filters else ""
            args.extend((limit + 1, 0 if before else offset))
            rows = conn.execute(f"""SELECT r.id,r.job_id,r.job_name,r.requested_at,r.started_at,r.finished_at,
                r.outcome,r.successful_targets,r.failed_targets,r.triggered_by,r.search_revision,r.search_snapshot,
                r.snapshot_known,r.intent_id,r.paused_manual,r.target_cursor,ci.requested_at AS intent_requested_at,
                ci.eligible_at,j.search_revision AS current_search_revision,j.status AS job_status,
                j.time_zone AS time_zone
                FROM check_runs r LEFT JOIN check_intents ci ON ci.id=r.intent_id
                LEFT JOIN jobs j ON j.id=r.job_id {where}
                ORDER BY r.started_at DESC,r.rowid DESC LIMIT ? OFFSET ?""", args).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            if not rows:
                return {"items": [], "has_more": False, "next_offset": None,
                        "next_before_run_id": None}

            run_ids = [row["id"] for row in rows]
            job_ids = list({row["job_id"] for row in rows if row["job_id"]})
            run_marks = ",".join("?" for _ in run_ids)
            result_rows = conn.execute(f"""SELECT id,run_id,job_id,target_id,practitioner_name,practice_name,
                checked_at,status,slot_count,earliest_slot,count_complete,error_code,error_category,upstream_status,
                retry_at,search_revision,published FROM check_results WHERE run_id IN ({run_marks})
                ORDER BY checked_at,rowid""", run_ids).fetchall()
            active_targets = set()
            if job_ids:
                job_marks = ",".join("?" for _ in job_ids)
                active_targets = {row["id"] for row in conn.execute(
                    f"SELECT id FROM targets WHERE active=1 AND job_id IN ({job_marks})", job_ids).fetchall()}
            deliveries = conn.execute(f"""SELECT a.id,a.event_id,a.target_id,a.channel_name,a.channel,a.status,
                a.attempt_count,a.created_at,a.sent_at,a.error_summary,a.next_attempt_at,a.delivery_state,
                a.quiet_state,a.quiet_until,origin.id AS origin_result_id,origin.run_id AS originating_run_id,
                confirmation.id AS confirmation_result_id,confirmation.run_id AS confirmation_run_id
                FROM alerts a LEFT JOIN availability_events e ON e.id=a.event_id
                LEFT JOIN check_results origin ON origin.id=e.result_id
                LEFT JOIN check_results confirmation ON confirmation.id=a.result_id
                WHERE origin.run_id IN ({run_marks})
                ORDER BY a.created_at,a.rowid""", run_ids).fetchall()
            deliveries_by_result = {}
            for row in deliveries:
                result_id = row["origin_result_id"]
                code = row["error_summary"]
                if (not isinstance(code, str) or len(code) > 80 or not code.isascii()
                        or not all(char.isalnum() or char == "_" for char in code)):
                    code = None
                delivery = {"id": row["id"], "event_id": row["event_id"],
                            "channel_name": row["channel_name"] or row["channel"],
                            "channel_type": row["channel"], "status": row["status"],
                            "attempts": row["attempt_count"], "created_at": row["created_at"],
                            "accepted_at": row["sent_at"], "reason_code": code,
                            "next_eligible_at": (row["quiet_until"] if row["quiet_state"] == "held" else
                                None if row["quiet_state"] in ("waiting_for_fresh_check", "needs_manual_check", "cancelled") else
                                row["next_attempt_at"]),
                            "delivery_state": row["delivery_state"], "quiet_state": row["quiet_state"],
                            "quiet_until": row["quiet_until"], "originating_run_id": row["originating_run_id"],
                            "confirmation_run_id": row["confirmation_run_id"]}
                deliveries_by_result.setdefault(result_id, []).append(delivery)

            results_by_run = {run_id: [] for run_id in run_ids}
            job_status_by_run = {run["id"]: run["job_status"] for run in rows}
            for row in result_rows:
                target_id = row["target_id"]
                job_status = job_status_by_run[row["run_id"]]
                category = row["error_category"]
                if category not in {"upstream_rejected", "throttled", "upstream_error", "timeout", "connectivity",
                                    "invalid_metadata", "malformed_response", "incomplete_response", "budget_exceeded", "unknown"}:
                    category = "unknown" if row["status"] == "error" else None
                code = row["error_code"]
                if (not isinstance(code, str) or len(code) > 80 or not code.isascii()
                        or not all(char.isalnum() or char == "_" for char in code)):
                    code = None
                upstream_status = row["upstream_status"]
                if not isinstance(upstream_status, int) or not 100 <= upstream_status <= 599:
                    upstream_status = None
                results_by_run[row["run_id"]].append({
                    "id": row["id"], "target_id": target_id,
                    "practitioner_name": row["practitioner_name"], "practice_name": row["practice_name"],
                    "checked_at": row["checked_at"], "status": row["status"],
                    "slot_count": row["slot_count"], "earliest_slot": row["earliest_slot"],
                    "count_complete": bool(row["count_complete"]), "error_code": code,
                    "error_category": category,
                    "upstream_status": upstream_status, "retry_at": row["retry_at"],
                    "search_revision": row["search_revision"], "published": bool(row["published"]),
                    "target_removed": bool(target_id and job_status != "deleted" and target_id not in active_targets),
                    "deliveries": deliveries_by_result.get(row["id"], []),
                })

            items = []
            for row in rows:
                snapshot = json.loads(row["search_snapshot"]) if row["search_snapshot"] else None
                targets = snapshot.get("targets", []) if isinstance(snapshot, dict) else []
                targets = targets if isinstance(targets, list) else []
                safe_targets = [{key: target.get(key) for key in ("id", "practitioner_name", "practice_name", "motive_name")}
                                for target in targets if isinstance(target, dict)]
                results = results_by_run[row["id"]]
                known = bool(row["snapshot_known"] and isinstance(snapshot, dict) and isinstance(snapshot.get("targets"), list))
                expected_ids = {target["id"] for target in safe_targets if target.get("id")}
                covered_ids = {result["target_id"] for result in results if result["target_id"]}
                same_revision = (row["current_search_revision"] is not None and
                                 row["current_search_revision"] == row["search_revision"])
                history_state = ("historical" if row["job_status"] == "deleted" or row["search_revision"] is None
                                 else "current" if same_revision else "superseded")
                items.append({
                    "id": row["id"], "job_id": row["job_id"], "job_name": row["job_name"],
                    "job_status": row["job_status"], "time_zone": row["time_zone"],
                    "requested_at": row["requested_at"] or row["intent_requested_at"],
                    "started_at": row["started_at"], "finished_at": row["finished_at"],
                    "outcome": row["outcome"], "triggered_by": row["triggered_by"],
                    "intent_id": row["intent_id"], "intent_eligible_at": row["eligible_at"],
                    "search_revision": row["search_revision"], "current_search_revision": row["current_search_revision"],
                    "history_state": history_state, "snapshot_known": known,
                    "target_total": len(expected_ids) if known else None, "target_completed": len(results),
                    "target_cursor": row['target_cursor'],
                    "target_successful": row["successful_targets"], "target_failed": row["failed_targets"],
                    "coverage_complete": bool(known and expected_ids == covered_ids),
                    "paused_manual": bool(row["paused_manual"]), "targets": safe_targets, "results": results,
                })
            return {"items": items, "has_more": has_more,
                    "next_offset": offset + len(items) if has_more else None,
                    "next_before_run_id": items[-1]["id"] if has_more else None}

    def latest_result(self, job_id):
        with self.database.connection() as conn:
            row = conn.execute(
                """SELECT r.*,c.started_at AS run_started_at,c.finished_at AS run_finished_at,
                c.outcome AS run_outcome,c.successful_targets,c.failed_targets,c.triggered_by
                FROM check_results r JOIN check_runs c ON c.id=r.run_id
                WHERE r.job_id=? ORDER BY r.checked_at DESC,r.rowid DESC LIMIT 1""", (job_id,)
            ).fetchone()
            return dict(row) if row else None

    def alerts(self, limit=50, offset=0):
        with self.database.connection() as conn:
            return [self._public_alert(row, presentation=False) for row in conn.execute(
                """SELECT a.*,r.earliest_slot,r.slot_count,r.practitioner_name,r.practice_name,
                j.name AS job_name,j.time_zone
                FROM alerts a
                LEFT JOIN check_results r ON r.id=a.result_id
                LEFT JOIN jobs j ON j.id=a.job_id
                ORDER BY a.created_at DESC LIMIT ? OFFSET ?""", (limit, offset)
            ).fetchall()]

    def settings(self, default_interval, spacing):
        now = iso()
        with self.database.connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO settings(singleton_id,default_interval_seconds,request_spacing_seconds,updated_at) VALUES(1,?,?,?)",
                (default_interval, spacing, now),
            )
            row = conn.execute("SELECT * FROM settings WHERE singleton_id=1").fetchone()
            item = dict(row)
            item['message_content'] = self._effective_content(conn)
            return item

    def update_settings(self, values, minimum_interval, expected_version=None):
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""INSERT OR IGNORE INTO settings(singleton_id,default_interval_seconds,
                request_spacing_seconds,updated_at) VALUES(1,?,3.0,?)""", (minimum_interval, iso()))
            current = conn.execute("SELECT * FROM settings WHERE singleton_id=1").fetchone()
            if expected_version is not None and current["edit_version"] != expected_version:
                raise VersionConflictError(current["edit_version"])
            default_interval = int(values.get("default_interval_seconds", current["default_interval_seconds"]))
            spacing = float(values.get("request_spacing_seconds", current["request_spacing_seconds"]))
            if default_interval < minimum_interval:
                raise ValueError(f"default_interval_seconds must be at least {minimum_interval}")
            if not math.isfinite(spacing) or spacing < 3:
                raise ValueError("request_spacing_seconds must be finite and at least 3")
            content = values.get('message_content', self._effective_content(conn))
            from app.notifications import normalize_content
            content = normalize_content(content)
            inherited_channels = [row[0] for row in conn.execute('SELECT DISTINCT jc.channel_config_id FROM job_channels jc JOIN jobs j ON j.id=jc.job_id WHERE j.message_content IS NULL AND j.status!=\'deleted\'')]
            self._validate_content_channels(conn, content, inherited_channels)
            content_changed = content != self._effective_content(conn)
            changed = content_changed or default_interval != current["default_interval_seconds"] or spacing != current["request_spacing_seconds"]
            conn.execute(
                """UPDATE settings SET default_interval_seconds=?,request_spacing_seconds=?,updated_at=?,
                edit_version=edit_version+?,message_content=?,content_version=content_version+? WHERE singleton_id=1""",
                (default_interval, spacing, iso() if changed else current["updated_at"], int(changed), json.dumps(content), int(content_changed)),
            )
            item = dict(conn.execute("SELECT * FROM settings WHERE singleton_id=1").fetchone())
            item['message_content'] = content
            return item

    def reserve_request_turn(self, spacing_seconds, *, deadline=None):
        import sqlite3
        from app.doctolib import TargetBudgetExceeded

        with self.database.connection() as conn:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining < 0.001:
                    raise TargetBudgetExceeded("The target checking budget was exceeded.")
                conn.execute(f"PRAGMA busy_timeout={min(10000, int(remaining * 1000))}")
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if (deadline is not None and deadline - time.monotonic() <= 0.002
                        and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY):
                    raise TargetBudgetExceeded("The target checking budget was exceeded.") from None
                raise
            now = utc_now()
            row = conn.execute("SELECT next_allowed_at FROM request_gate WHERE singleton_id=1").fetchone()
            scheduled = max(now, parse_time(row[0])) if row else now
            wait_seconds = max(0.0, (scheduled - utc_now()).total_seconds())
            if deadline is not None and wait_seconds >= deadline - time.monotonic():
                # Reject before reservation: existing callers retain their slots,
                # and cancelled work cannot move the shared gate backwards.
                raise TargetBudgetExceeded("The target checking budget was exceeded.")
            following = scheduled + timedelta(seconds=spacing_seconds)
            conn.execute(
                """INSERT INTO request_gate(singleton_id,next_allowed_at) VALUES(1,?)
                ON CONFLICT(singleton_id) DO UPDATE SET next_allowed_at=excluded.next_allowed_at""",
                (precise_iso(following),),
            )
        if wait_seconds:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TargetBudgetExceeded("The target checking budget was exceeded.")
            time.sleep(wait_seconds if remaining is None else min(wait_seconds, remaining))
        if deadline is not None and time.monotonic() >= deadline:
            raise TargetBudgetExceeded("The target checking budget was exceeded.")
