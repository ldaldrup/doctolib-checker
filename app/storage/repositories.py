"""Transactional application operations over the SQLite schema."""

import time
import uuid
import json
import math
from datetime import datetime, timedelta, timezone

from app.doctolib import DoctolibClient


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


class Repository:
    def __init__(self, database, minimum_poll_interval_seconds=300):
        self.database = database
        self.minimum_poll_interval_seconds = minimum_poll_interval_seconds

    def configure_minimum(self, minimum_poll_interval_seconds):
        """Apply a raised server floor to persisted settings and existing jobs."""
        self.minimum_poll_interval_seconds = minimum_poll_interval_seconds
        now = utc_now()
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE settings SET default_interval_seconds=?,updated_at=?
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
        item.pop("lock_run_id", None)
        item.pop("lock_owner_token", None)
        if conn is not None:
            item["check_intent"] = self._intent_evidence(conn, item["id"])
            item["current_run"] = self._current_run(conn, item["id"])
        if conn is not None:
            item["targets"] = [dict(target) for target in conn.execute(
                "SELECT * FROM targets WHERE job_id=? AND active=1 ORDER BY rowid", (item["id"],)
            ).fetchall()]
        for key in ("telehealth", "telegram_enabled"):
            item[key] = bool(item[key])
        return item

    @staticmethod
    def _current_run(conn, job_id):
        row = conn.execute("""SELECT r.id,r.outcome,r.search_revision,r.triggered_by,r.intent_id,r.paused_manual
            FROM check_runs r JOIN jobs j ON j.lock_run_id=r.id WHERE j.id=? AND r.outcome='running'
            AND j.lock_until>?""", (job_id, precise_iso())).fetchone()
        return dict(row) if row else None

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

    def create_job(self, values, targets):
        if values["interval_seconds"] < self.minimum_poll_interval_seconds:
            raise ValueError("interval_seconds is below the server minimum")
        job_id = new_id()
        now = iso()
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO jobs
                (id,name,status,interval_seconds,date_mode,horizon_days,earliest_date,latest_date,
                 time_zone,insurance_sector,telehealth,telegram_enabled,created_at,updated_at,next_check_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, values["name"], "active", values["interval_seconds"], values["date_mode"],
                 values.get("horizon_days"), values.get("earliest_date"), values.get("latest_date"),
                 values["time_zone"], values["insurance_sector"], int(values["telehealth"]),
                 int(values["telegram_enabled"]), now, now, now),
            )
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

    def update_job(self, job_id, values, targets=None):
        if values.get("interval_seconds", self.minimum_poll_interval_seconds) < self.minimum_poll_interval_seconds:
            raise ValueError("interval_seconds is below the server minimum")
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id=? AND status != 'deleted'", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError("Job not found")
            previous_targets = [dict(target) for target in conn.execute("SELECT * FROM targets WHERE job_id=? AND active=1", (job_id,))]
            previous_search = search_signature(dict(row), previous_targets)
            fields = ["name", "interval_seconds", "date_mode", "horizon_days", "earliest_date", "latest_date",
                      "time_zone", "insurance_sector", "telehealth", "telegram_enabled"]
            update = {key: values[key] for key in fields if key in values}
            if update:
                assignments = ",".join(key + "=?" for key in update)
                encoded = [int(value) if key in ("telehealth", "telegram_enabled") else value
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
            changed = search_changed or any(current[key] != row[key] for key in fields)
            if changed:
                conn.execute("UPDATE jobs SET search_revision=search_revision+?,edit_version=edit_version+1,updated_at=? WHERE id=?",
                             (int(search_changed), iso(), job_id))
            if search_changed:
                current = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if current['status'] == 'active':
                    self._queue_intent(conn, current, 'search_edit', utc_now())
                else:
                    conn.execute("UPDATE check_intents SET status='cancelled',cancel_reason='search_edited' WHERE job_id=? AND status='queued'", (job_id,))
                conn.execute("""UPDATE alerts SET status='cancelled',next_attempt_at=NULL,error_summary='search_edited'
                    WHERE job_id=? AND status IN ('pending','failed') AND search_revision IS NOT ?""", (job_id,current['search_revision']))
            return self._job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(), conn)

    def set_status(self, job_id, status):
        if status not in ("active", "paused", "deleted"):
            raise ValueError("Invalid job status")
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id=? AND status != 'deleted'", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError("Job not found")
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
                         (status, due, lock, lock_run_id, status, int(status != row["status"]), iso(), job_id))
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
            rows = conn.execute("""SELECT j.* FROM jobs j LEFT JOIN check_intents i ON i.job_id=j.id AND i.status='queued'
                WHERE j.status!='deleted' AND (j.lock_until IS NULL OR j.lock_until<=?)
                AND ((i.id IS NOT NULL AND i.eligible_at<=? AND i.search_revision=j.search_revision
                  AND i.status_version=j.status_version AND (j.status='active' OR i.paused_manual=1)
                  AND (j.last_extra_started_at IS NULL OR j.last_extra_started_at<=?))
                  OR (j.status='active' AND j.next_check_at<=?))
                ORDER BY CASE WHEN i.id IS NOT NULL AND i.eligible_at<=? THEN i.eligible_at ELSE j.next_check_at END LIMIT ?""",
                (now_text,now_text,precise_iso(now-timedelta(seconds=60)),now_text,now_text,limit)).fetchall()
            for row in rows:
                intent = conn.execute("SELECT * FROM check_intents WHERE job_id=? AND status='queued'", (row['id'],)).fetchone()
                extra_ready = (intent is not None and parse_time(intent['eligible_at'])<=now
                    and intent['search_revision']==row['search_revision'] and intent['status_version']==row['status_version']
                    and (row['status']=='active' or intent['paused_manual'])
                    and (not row['last_extra_started_at'] or parse_time(row['last_extra_started_at'])<=now-timedelta(seconds=60)))
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
                    (precise_iso(now+timedelta(minutes=10)),run_id,owner_token,now_text,int(intent is not None),now_text,row['id']))
                trigger = intent['triggered_by'] if intent else 'schedule'
                paused_manual = int(bool(intent and intent['paused_manual']))
                intent_id = intent['id'] if intent else None
                conn.execute("""INSERT INTO check_runs(id,job_id,job_name,started_at,outcome,search_revision,
                    search_snapshot,snapshot_known,owner_token,triggered_by,intent_id,paused_manual,status_version)
                    VALUES(?,?,?,?,'running',?,?,1,?,?,?,?,?)""",
                    (run_id,row['id'],row['name'],now_text,row['search_revision'],json.dumps(snapshot,sort_keys=True),
                     owner_token,trigger,intent_id,paused_manual,row['status_version']))
                if intent:
                    conn.execute("UPDATE check_intents SET status='running',run_id=? WHERE id=? AND status='queued'", (run_id,intent_id))
                job = dict(row)
                job.update(owner_token=owner_token,search_snapshot=snapshot,intent_id=intent_id,triggered_by=trigger,paused_manual=paused_manual)
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
            return bool(run and job and self._run_capable(job,run))

    def renew_job_lock(self, job_id, run_id, owner_token=None, lease_minutes=10):
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = self._owned(conn,job_id,run_id,owner_token)
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not run or not job or not self._run_capable(job,run):
                return False
            conn.execute("UPDATE jobs SET lock_until=? WHERE id=? AND lock_run_id=? AND lock_owner_token=?",
                (precise_iso(utc_now()+timedelta(minutes=lease_minutes)),job_id,run_id,owner_token))
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
            current = conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
            active = conn.execute("SELECT active FROM targets WHERE id=? AND job_id=?", (target["id"], job["id"])).fetchone()
            published = (self._run_capable(current, run) and current["search_revision"] == run["search_revision"]
                         and active is not None and active["active"])
            conn.execute(
                """INSERT INTO check_results
                (id,run_id,job_id,target_id,practitioner_name,practice_name,booking_url,checked_at,status,
                 slot_count,earliest_slot,count_complete,error_code,error_message,search_revision,snapshot_known,published)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (result_id, run_id, job["id"], target["id"], target["practitioner_name"],
                 target["practice_name"], target["booking_url"], iso(), result.status, result.slot_count,
                 earliest_slot, int(result.count_complete),
                 result.error_code, result.error_message, run["search_revision"], 1, int(published)),
            )
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
                        WHERE target_id=? AND status IN ('pending','failed') AND dedupe_key!=?""",
                        (target["id"], current_key),
                    )
                    conn.execute(
                        """UPDATE alerts SET result_id=?,search_revision=? WHERE target_id=? AND dedupe_key=?
                        AND status IN ('pending','failed') AND claim_owner_token IS NULL""",
                        (result_id, run["search_revision"], target["id"], current_key),
                    )
        return result_id

    def insert_error_result(self, run_id, job, target, error_code, error_message, *, owner_token=None):
        class ErrorResult:
            status = "error"
            slot_count = 0
            earliest_slot = None
            count_complete = True

        result = ErrorResult()
        result.error_code = error_code
        result.error_message = error_message
        return self.insert_result(run_id, job, target, result, owner_token=owner_token)

    def result_id(self, run_id, target_id):
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM check_results WHERE run_id=? AND target_id=? ORDER BY checked_at DESC LIMIT 1",
                (run_id, target_id),
            ).fetchone()
            return row["id"] if row else None

    def create_alert(self, job, target, result_id, earliest_slot, *, owner_token=None):
        alert_id = new_id()
        slot_text = iso(earliest_slot)
        now = iso()
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = conn.execute("SELECT * FROM check_results WHERE id=? AND job_id=? AND target_id=?",
                                  (result_id, job["id"], target["id"])).fetchone()
            if (result is None or not result["published"] or result["earliest_slot"] != slot_text or
                    self._owned(conn, job["id"], result["run_id"], owner_token) is None):
                return None
            state = conn.execute(
                """SELECT s.episode,s.last_status,s.last_earliest_slot,t.active,
                j.status AS job_status,j.telegram_enabled,j.search_revision
                FROM target_alert_state s JOIN targets t ON t.id=s.target_id
                JOIN jobs j ON j.id=t.job_id WHERE t.id=? AND j.id=?""",
                (target["id"], job["id"]),
            ).fetchone()
            current_job = conn.execute("SELECT * FROM jobs WHERE id=?", (job['id'],)).fetchone()
            result_run = conn.execute("SELECT * FROM check_runs WHERE id=?", (result['run_id'],)).fetchone()
            if (state is None or not state["active"] or not self._run_capable(current_job, result_run) or
                    not state["telegram_enabled"] or state["last_status"] != "available" or
                    state["last_earliest_slot"] != slot_text or state["search_revision"] != result["search_revision"]):
                return None
            dedupe = alert_dedupe_key(target["id"], slot_text, state["episode"])
            inserted = conn.execute(
                """INSERT OR IGNORE INTO alerts(id,job_id,target_id,result_id,channel,event_type,dedupe_key,
                status,created_at,next_attempt_at,search_revision) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (alert_id, job["id"], target["id"], result_id, "telegram", "slot_found", dedupe,
                 "pending", now, now, result["search_revision"]),
            ).rowcount
            if inserted:
                return alert_id
            existing = conn.execute("SELECT id,status FROM alerts WHERE dedupe_key=?", (dedupe,)).fetchone()
            if existing is None:
                raise RuntimeError("Alert insert was ignored without a matching dedupe key")
            if existing["status"] == "sent":
                return None
            if existing["status"] == "cancelled":
                reactivated = conn.execute(
                    """UPDATE alerts SET status='pending',result_id=?,next_attempt_at=?,search_revision=?,
                    error_summary=NULL WHERE id=? AND claim_owner_token IS NULL AND delivery_state IN ('ready','retry') AND error_summary IN ('slot_expired','availability_disappeared','earliest_slot_changed','search_edited','job_paused','status_changed')""",
                    (result_id, now, result["search_revision"], existing["id"]),
                ).rowcount
                if not reactivated:
                    return None
            return existing["id"]

    @staticmethod
    def _delivery_rows(conn, alert_id=None):
        return conn.execute(
            """SELECT a.*,r.earliest_slot,r.slot_count,r.practitioner_name,r.practice_name,
            r.booking_url,r.checked_at,r.search_revision AS result_revision,r.snapshot_known,r.published,
            j.search_revision AS current_revision,j.time_zone,j.status AS job_status,j.telegram_enabled,
            j.interval_seconds,j.status_version AS current_status_version, cr.status_version AS run_status_version,
            cr.paused_manual,cr.intent_id,t.active AS target_active,s.last_status,s.last_earliest_slot
            FROM alerts a JOIN check_results r ON r.id=a.result_id JOIN check_runs cr ON cr.id=r.run_id
            JOIN jobs j ON j.id=a.job_id LEFT JOIN targets t ON t.id=a.target_id
            LEFT JOIN target_alert_state s ON s.target_id=a.target_id
            WHERE (? IS NULL OR a.id=?) ORDER BY COALESCE(a.next_attempt_at,a.created_at),a.created_at,a.rowid""",
            (alert_id, alert_id),
        ).fetchall()

    @staticmethod
    def _delivery_eligible(alert, now):
        return (alert['status'] in ('pending', 'failed') and (alert['job_status'] == 'active' or (alert['job_status'] == 'paused' and alert['paused_manual']))
                and alert['current_status_version'] == alert['run_status_version']
                and alert['telegram_enabled'] and alert['target_active']
                and alert['snapshot_known'] and alert['published']
                and alert['search_revision'] == alert['current_revision'] == alert['result_revision']
                and alert['earliest_slot'] and parse_time(alert['earliest_slot']) > now
                and alert['last_status'] == 'available' and alert['last_earliest_slot'] == alert['earliest_slot']
                and now - parse_time(alert['checked_at']) <= timedelta(seconds=alert['interval_seconds']))

    @staticmethod
    def _delivery_budget(alert, now):
        return (alert['delivery_epoch_attempts'] < 5 and
                now - parse_time(alert['delivery_epoch_at'] or alert['created_at']) < timedelta(hours=24))

    @staticmethod
    def _public_alert(alert):
        alert = dict(alert)
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
                             claim_search_revision=search_revision,attempt_started_at=NULL WHERE id=?""",
                             (token, precise_iso(now + timedelta(seconds=lease_seconds)), row['id']))
                alert = self._public_alert(row)
                alert['owner_token'] = token
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
                    or not self._delivery_eligible(row, now) or not self._delivery_budget(row, now)):
                self._release_delivery(conn, alert_id, 'exhausted' if not self._delivery_budget(row, now)
                                       else ('retry' if row['attempt_count'] else 'ready'))
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
                         next_attempt_at=?,error_summary=? WHERE id=?""", (next_attempt, error_code, alert_id))
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
            conn.execute("""UPDATE alerts SET status=?,sent_at=?,next_attempt_at=?,error_summary=? WHERE id=?""",
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
            row = rows[0]
            if row['status'] == 'sent' or row['delivery_state'] not in ('action_required', 'exhausted', 'uncertain'):
                raise ConflictError('Alert is not recoverable')
            if row['delivery_state'] == 'uncertain' and not acknowledge_duplicate_risk:
                raise ConflictError('Uncertain delivery requires duplicate-risk acknowledgement')
            if row['claim_owner_token']:
                raise ConflictError('Alert is still claimed')
            if not self._delivery_eligible(row, now):
                return False
            self._release_delivery(conn, alert_id, 'ready')
            conn.execute("""UPDATE alerts SET status='pending',delivery_epoch_attempts=0,delivery_epoch_at=?,next_attempt_at=?,
                         error_summary=NULL WHERE id=?""", (precise_iso(now), precise_iso(now), alert_id))
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

    def interrupt_stale_runs(self):
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = precise_iso()
            stale = conn.execute(
                """SELECT r.id,r.job_id,r.owner_token FROM check_runs r LEFT JOIN jobs j ON j.id=r.job_id
                WHERE r.outcome='running' AND (j.id IS NULL OR j.lock_until IS NULL
                OR j.lock_until<=? OR j.lock_run_id IS NOT r.id OR j.lock_owner_token IS NOT r.owner_token
                OR r.owner_token IS NULL)""", (now,),
            ).fetchall()
            for run in stale:
                conn.execute("UPDATE check_intents SET status='completed' WHERE run_id=? AND status='running'", (run['id'],))
                conn.execute(
                    """UPDATE check_runs SET outcome='interrupted',finished_at=?,
                    successful_targets=(SELECT COUNT(*) FROM check_results WHERE run_id=check_runs.id AND status!='error'),
                    failed_targets=(SELECT COUNT(*) FROM check_results WHERE run_id=check_runs.id AND status='error')
                    WHERE id=? AND outcome='running'""",
                    (now, run["id"]),
                )
                conn.execute(
                    """UPDATE jobs SET lock_until=NULL,lock_run_id=NULL,lock_owner_token=NULL
                    WHERE id=? AND lock_run_id=? AND lock_owner_token IS ?""",
                    (run["job_id"], run["id"], run["owner_token"]),
                )
            conn.execute("UPDATE jobs SET lock_until=NULL,lock_run_id=NULL,lock_owner_token=NULL WHERE lock_until<=?", (now,))

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
            counts = conn.execute(
                "SELECT SUM(status='active') active_jobs,SUM(status='paused') paused_jobs FROM jobs WHERE status!='deleted'"
            ).fetchone()
            last_run = conn.execute(
                "SELECT finished_at,outcome FROM check_runs WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
            next_check = conn.execute(
                "SELECT MIN(next_check_at) FROM jobs WHERE status='active'"
            ).fetchone()[0]
            return {"active_jobs": counts["active_jobs"] or 0, "paused_jobs": counts["paused_jobs"] or 0,
                    "worker": self.worker_status(), "dispatcher": self.dispatcher_status()["heartbeat"], "delivery_backlog": self.dispatcher_status()["backlog"], "last_completed_run": dict(last_run) if last_run else None,
                    "next_check_at": next_check}

    def checks(self, job_id, limit=50, offset=0):
        with self.database.connection() as conn:
            runs = [dict(row) for row in conn.execute(
                """SELECT id,job_id,job_name,started_at,finished_at,outcome,successful_targets,
                failed_targets,triggered_by,search_revision,search_snapshot,snapshot_known,intent_id,paused_manual,status_version FROM check_runs WHERE job_id=?
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
            return [self._public_alert(row) for row in conn.execute(
                """SELECT a.*,r.earliest_slot,r.slot_count,r.practitioner_name,r.practice_name,
                r.booking_url,j.name AS job_name,j.time_zone
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
            return dict(row)

    def update_settings(self, values, minimum_interval):
        current = self.settings(minimum_interval, 3.0)
        default_interval = int(values.get("default_interval_seconds", current["default_interval_seconds"]))
        spacing = float(values.get("request_spacing_seconds", current["request_spacing_seconds"]))
        if default_interval < minimum_interval:
            raise ValueError(f"default_interval_seconds must be at least {minimum_interval}")
        if spacing < 3:
            raise ValueError("request_spacing_seconds must be at least 3")
        with self.database.connection() as conn:
            conn.execute(
                "UPDATE settings SET default_interval_seconds=?,request_spacing_seconds=?,updated_at=? WHERE singleton_id=1",
                (default_interval, spacing, iso()),
            )
        return self.settings(minimum_interval, 3.0)

    def reserve_request_turn(self, spacing_seconds):
        now = utc_now()
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT next_allowed_at FROM request_gate WHERE singleton_id=1").fetchone()
            scheduled = max(now, parse_time(row[0])) if row else now
            following = scheduled + timedelta(seconds=spacing_seconds)
            conn.execute(
                """INSERT INTO request_gate(singleton_id,next_allowed_at) VALUES(1,?)
                ON CONFLICT(singleton_id) DO UPDATE SET next_allowed_at=excluded.next_allowed_at""",
                (precise_iso(following),),
            )
        wait_seconds = max(0.0, (scheduled - utc_now()).total_seconds())
        if wait_seconds:
            time.sleep(wait_seconds)
