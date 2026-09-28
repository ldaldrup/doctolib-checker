"""Transactional application operations over the SQLite schema."""

import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone


def utc_now():
    return datetime.now(timezone.utc)


def iso(value=None):
    value = value or utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def new_id():
    return str(uuid.uuid4())


class NotFoundError(LookupError):
    pass


class ConflictError(ValueError):
    pass


class Repository:
    def __init__(self, database):
        self.database = database

    def _job(self, row, conn=None):
        if row is None:
            return None
        item = dict(row)
        if conn is not None:
            item["targets"] = [dict(target) for target in conn.execute(
                "SELECT * FROM targets WHERE job_id=? AND active=1 ORDER BY rowid", (item["id"],)
            ).fetchall()]
        for key in ("telehealth", "telegram_enabled"):
            item[key] = bool(item[key])
        return item

    def create_job(self, values, targets):
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
                    """SELECT status,slot_count,earliest_slot,checked_at FROM check_results
                    WHERE job_id=? ORDER BY checked_at DESC LIMIT 1""", (job["id"],)
                ).fetchone()
                job["last_result"] = dict(latest) if latest else None
                result.append(job)
            return result

    def update_job(self, job_id, values, targets=None):
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id=? AND status != 'deleted'", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError("Job not found")
            fields = ["name", "interval_seconds", "date_mode", "horizon_days", "earliest_date", "latest_date",
                      "time_zone", "insurance_sector", "telehealth", "telegram_enabled"]
            update = {key: values[key] for key in fields if key in values}
            if update:
                assignments = ",".join(key + "=?" for key in update)
                encoded = [int(value) if key in ("telehealth", "telegram_enabled") else value
                           for key, value in update.items()]
                conn.execute("UPDATE jobs SET " + assignments + ",updated_at=? WHERE id=?",
                             encoded + [iso(), job_id])
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
            # Re-queue after an edit while preserving the 5 minute target floor.
            last = conn.execute("SELECT MAX(checked_at) FROM check_results WHERE job_id=?", (job_id,)).fetchone()[0]
            allowed = parse_time(last) + timedelta(seconds=300) if last else utc_now()
            due = max(utc_now(), allowed)
            # Keep an in-flight lease intact. The worker re-reads the changed
            # settings before its next target, and finish_run schedules using
            # the updated interval.
            conn.execute("UPDATE jobs SET next_check_at=? WHERE id=?", (iso(due), job_id))
            return self._job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(), conn)

    def set_status(self, job_id, status):
        if status not in ("active", "paused", "deleted"):
            raise ValueError("Invalid job status")
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id=? AND status != 'deleted'", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError("Job not found")
            due = row["next_check_at"]
            lock = row["lock_until"]
            if status == "active":
                last = conn.execute("SELECT MAX(checked_at) FROM check_results WHERE job_id=?", (job_id,)).fetchone()[0]
                allowed = parse_time(last) + timedelta(seconds=300) if last else utc_now()
                due = iso(max(utc_now(), allowed))
            if status == "deleted":
                due = iso(utc_now() + timedelta(days=36500))
                lock = None
            conn.execute("UPDATE jobs SET status=?,next_check_at=?,lock_until=?,updated_at=? WHERE id=?",
                         (status, due, lock, iso(), job_id))
            return self._job(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(), conn)

    def set_job_due(self, job_id, due_at):
        with self.database.connection() as conn:
            row = conn.execute("SELECT status,lock_until FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] == "deleted":
                raise NotFoundError("Job not found")
            if row["status"] == "paused":
                raise ConflictError("Paused jobs cannot be run; resume the job first")
            if row["lock_until"] and parse_time(row["lock_until"]) > utc_now():
                raise ConflictError("This job already has a check in progress")
            last = conn.execute("SELECT MAX(checked_at) FROM check_results WHERE job_id=?", (job_id,)).fetchone()[0]
            allowed = parse_time(last) + timedelta(seconds=300) if last else utc_now()
            due = max(due_at, allowed)
            conn.execute("UPDATE jobs SET next_check_at=?,updated_at=? WHERE id=?",
                         (iso(due), iso(), job_id))
            return due

    def claim_due_jobs(self, limit=10):
        now = utc_now()
        now_text = iso(now)
        claimed = []
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT * FROM jobs WHERE status='active' AND next_check_at<=?
                AND (lock_until IS NULL OR lock_until<=?) ORDER BY next_check_at LIMIT ?""",
                (now_text, now_text, limit),
            ).fetchall()
            for row in rows:
                run_id = new_id()
                changed = conn.execute(
                    """UPDATE jobs SET lock_until=?,last_started_at=? WHERE id=? AND status='active'
                    AND (lock_until IS NULL OR lock_until<=?)""",
                    (iso(now + timedelta(minutes=10)), now_text, row["id"], now_text),
                ).rowcount
                if not changed:
                    continue
                conn.execute(
                    "INSERT INTO check_runs(id,job_id,job_name,started_at,outcome) VALUES(?,?,?,?,?)",
                    (run_id, row["id"], row["name"], now_text, "running"),
                )
                claimed.append((run_id, dict(row)))
            conn.execute(
                """INSERT INTO worker_heartbeat(singleton_id,started_at,last_seen_at)
                VALUES(1,?,?) ON CONFLICT(singleton_id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                (now_text, now_text),
            )
        return claimed

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

    def insert_result(self, run_id, job, target, result):
        result_id = new_id()
        with self.database.connection() as conn:
            conn.execute(
                """INSERT INTO check_results
                (id,run_id,job_id,target_id,practitioner_name,practice_name,booking_url,checked_at,status,
                 slot_count,earliest_slot,count_complete,error_code,error_message)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (result_id, run_id, job["id"], target["id"], target["practitioner_name"],
                 target["practice_name"], target["booking_url"], iso(), result.status, result.slot_count,
                 iso(result.earliest_slot) if result.earliest_slot else None, int(result.count_complete),
                 result.error_code, result.error_message),
            )
        return result_id

    def insert_error_result(self, run_id, job, target, error_code, error_message):
        class ErrorResult:
            status = "error"
            slot_count = 0
            earliest_slot = None
            count_complete = True

        result = ErrorResult()
        result.error_code = error_code
        result.error_message = error_message
        return self.insert_result(run_id, job, target, result)

    def result_id(self, run_id, target_id):
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM check_results WHERE run_id=? AND target_id=? ORDER BY checked_at DESC LIMIT 1",
                (run_id, target_id),
            ).fetchone()
            return row["id"] if row else None

    def create_alert(self, job, target, result_id, earliest_slot):
        alert_id = new_id()
        dedupe = target["id"] + ":slot:" + iso(earliest_slot)
        now = iso()
        try:
            with self.database.connection() as conn:
                conn.execute(
                    """INSERT INTO alerts(id,job_id,target_id,result_id,channel,event_type,dedupe_key,
                    status,created_at,next_attempt_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (alert_id, job["id"], target["id"], result_id, "telegram", "slot_found", dedupe,
                     "pending", now, now),
                )
        except sqlite3.IntegrityError:
            return None
        return alert_id

    def get_pending_alerts(self, limit=20):
        with self.database.connection() as conn:
            return [dict(row) for row in conn.execute(
                """SELECT a.*,r.earliest_slot,r.slot_count,r.practitioner_name,r.practice_name,
                r.booking_url,j.time_zone
                FROM alerts a JOIN check_results r ON r.id=a.result_id
                LEFT JOIN jobs j ON j.id=a.job_id
                WHERE a.status IN ('pending','failed') AND (a.next_attempt_at IS NULL OR a.next_attempt_at<=?)
                ORDER BY a.created_at LIMIT ?""", (iso(), limit)
            ).fetchall()]

    def finish_alert(self, alert_id, sent, error_summary=None):
        with self.database.connection() as conn:
            row = conn.execute("SELECT attempt_count FROM alerts WHERE id=?", (alert_id,)).fetchone()
            if row is None:
                return
            attempt = row["attempt_count"] + 1
            delay = min(300, 2 ** min(attempt, 8))
            next_attempt = None if sent else iso(utc_now() + timedelta(seconds=delay))
            conn.execute(
                """UPDATE alerts SET status=?,attempt_count=?,sent_at=?,error_summary=?,next_attempt_at=? WHERE id=?""",
                ("sent" if sent else "failed", attempt, iso() if sent else None,
                 error_summary[:240] if error_summary else None, next_attempt, alert_id),
            )

    def finish_run(self, run_id, job_id, successful, failed, interval_seconds, error=None):
        now = utc_now()
        outcome = "error" if failed and not successful else "partial_error" if failed else "completed"
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job_row = conn.execute(
                "SELECT interval_seconds FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            effective_interval = job_row["interval_seconds"] if job_row else interval_seconds
            conn.execute(
                "UPDATE check_runs SET finished_at=?,outcome=?,successful_targets=?,failed_targets=? WHERE id=?",
                (iso(now), outcome, successful, failed, run_id),
            )
            conn.execute(
                """UPDATE jobs SET next_check_at=CASE WHEN status='active' THEN ? ELSE next_check_at END,
                last_finished_at=?,last_outcome=?,lock_until=NULL WHERE id=?""",
                (iso(now + timedelta(seconds=effective_interval)), iso(now), outcome, job_id),
            )
            conn.execute(
                """INSERT INTO worker_heartbeat(singleton_id,started_at,last_seen_at,last_completed_run_at,last_error)
                VALUES(1,?,?,?,?) ON CONFLICT(singleton_id) DO UPDATE SET last_seen_at=excluded.last_seen_at,
                last_completed_run_at=excluded.last_completed_run_at,last_error=excluded.last_error""",
                (iso(now), iso(now), iso(now), error[:240] if error else None),
            )

    def interrupt_stale_runs(self):
        now = iso()
        with self.database.connection() as conn:
            conn.execute(
                """UPDATE check_runs SET outcome='interrupted',finished_at=?
                WHERE outcome='running' AND started_at < ?""",
                (now, iso(utc_now() - timedelta(minutes=10))),
            )
            conn.execute("UPDATE jobs SET lock_until=NULL WHERE lock_until < ?", (now,))

    def worker_status(self):
        with self.database.connection() as conn:
            row = conn.execute("SELECT * FROM worker_heartbeat WHERE singleton_id=1").fetchone()
            return dict(row) if row else None

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
                    "worker": self.worker_status(), "last_completed_run": dict(last_run) if last_run else None,
                    "next_check_at": next_check}

    def checks(self, job_id, limit=50, offset=0):
        with self.database.connection() as conn:
            return [dict(row) for row in conn.execute(
                """SELECT r.*,c.job_name,c.started_at AS run_started_at,c.finished_at AS run_finished_at,
                c.outcome AS run_outcome,c.successful_targets,c.failed_targets,c.triggered_by
                FROM check_results r JOIN check_runs c ON c.id=r.run_id
                WHERE r.job_id=? ORDER BY r.checked_at DESC LIMIT ? OFFSET ?""",
                (job_id, limit, offset),
            ).fetchall()]

    def alerts(self, limit=50, offset=0):
        with self.database.connection() as conn:
            return [dict(row) for row in conn.execute(
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
            raise ValueError("default_interval_seconds must be at least 300")
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
                (iso(following),),
            )
        wait_seconds = max(0.0, (scheduled - utc_now()).total_seconds())
        if wait_seconds:
            time.sleep(wait_seconds)
