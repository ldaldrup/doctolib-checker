"""Transactional application operations over the SQLite schema."""

import time
import uuid
from datetime import datetime, timedelta, timezone


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
                    """UPDATE jobs SET interval_seconds=?,next_check_at=?,updated_at=? WHERE id=?""",
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
        if conn is not None:
            item["targets"] = [dict(target) for target in conn.execute(
                "SELECT * FROM targets WHERE job_id=? AND active=1 ORDER BY rowid", (item["id"],)
            ).fetchall()]
        for key in ("telehealth", "telegram_enabled"):
            item[key] = bool(item[key])
        return item

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
                    """SELECT status,slot_count,earliest_slot,checked_at FROM check_results
                    WHERE job_id=? ORDER BY checked_at DESC LIMIT 1""", (job["id"],)
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
                conn.execute(
                    """UPDATE alerts SET status='cancelled',next_attempt_at=NULL,error_summary='target_removed'
                    WHERE job_id=? AND status IN ('pending','failed') AND target_id IN
                    (SELECT id FROM targets WHERE job_id=? AND active=0)""",
                    (job_id, job_id),
                )
            due = self._minimum_due(row, utc_now())
            # Keep an in-flight lease intact. The worker re-reads the changed
            # settings before its next target, and finish_run schedules using
            # the updated interval.
            conn.execute("UPDATE jobs SET next_check_at=? WHERE id=?", (precise_iso(due), job_id))
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
                due = precise_iso(self._minimum_due(row, utc_now()))
            if status == "deleted":
                due = iso(utc_now() + timedelta(days=36500))
                lock = None
                conn.execute(
                    """UPDATE alerts SET status='cancelled',next_attempt_at=NULL,error_summary='job_deleted'
                    WHERE job_id=? AND status IN ('pending','failed')""", (job_id,),
                )
            lock_run_id = row["lock_run_id"] if status != "deleted" else None
            conn.execute("UPDATE jobs SET status=?,next_check_at=?,lock_until=?,lock_run_id=?,updated_at=? WHERE id=?",
                         (status, due, lock, lock_run_id, iso(), job_id))
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
        now = utc_now()
        now_text = precise_iso(now)
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
                    """UPDATE jobs SET lock_until=?,lock_run_id=?,last_started_at=? WHERE id=? AND status='active'
                    AND (lock_until IS NULL OR lock_until<=?)""",
                    (precise_iso(now + timedelta(minutes=10)), run_id, now_text, row["id"], now_text),
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

    def renew_job_lock(self, job_id, run_id, lease_minutes=10):
        """Extend this run's lease before another outbound request starts."""
        with self.database.connection() as conn:
            changed = conn.execute(
                """UPDATE jobs SET lock_until=? WHERE id=? AND status='active' AND lock_run_id=?""",
                (precise_iso(utc_now() + timedelta(minutes=lease_minutes)), job_id, run_id),
            ).rowcount
            return changed == 1

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
        earliest_slot = iso(result.earliest_slot) if result.earliest_slot else None
        if result.status == "available" and earliest_slot is None:
            raise ValueError("An available result requires an earliest slot")
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO check_results
                (id,run_id,job_id,target_id,practitioner_name,practice_name,booking_url,checked_at,status,
                 slot_count,earliest_slot,count_complete,error_code,error_message)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (result_id, run_id, job["id"], target["id"], target["practitioner_name"],
                 target["practice_name"], target["booking_url"], iso(), result.status, result.slot_count,
                 earliest_slot, int(result.count_complete),
                 result.error_code, result.error_message),
            )
            if result.status in ("available", "no_availability"):
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
                        """UPDATE alerts SET result_id=? WHERE target_id=? AND dedupe_key=?
                        AND status IN ('pending','failed')""",
                        (result_id, target["id"], current_key),
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
        slot_text = iso(earliest_slot)
        now = iso()
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute(
                """SELECT s.episode,s.last_status,s.last_earliest_slot,t.active,
                j.status AS job_status,j.telegram_enabled
                FROM target_alert_state s JOIN targets t ON t.id=s.target_id
                JOIN jobs j ON j.id=t.job_id WHERE t.id=? AND j.id=?""",
                (target["id"], job["id"]),
            ).fetchone()
            if (state is None or not state["active"] or state["job_status"] != "active" or
                    not state["telegram_enabled"] or state["last_status"] != "available" or
                    state["last_earliest_slot"] != slot_text):
                return None
            dedupe = alert_dedupe_key(target["id"], slot_text, state["episode"])
            inserted = conn.execute(
                """INSERT OR IGNORE INTO alerts(id,job_id,target_id,result_id,channel,event_type,dedupe_key,
                status,created_at,next_attempt_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (alert_id, job["id"], target["id"], result_id, "telegram", "slot_found", dedupe,
                 "pending", now, now),
            ).rowcount
            if inserted:
                return alert_id
            existing = conn.execute("SELECT id,status FROM alerts WHERE dedupe_key=?", (dedupe,)).fetchone()
            if existing is None:
                raise RuntimeError("Alert insert was ignored without a matching dedupe key")
            if existing["status"] == "sent":
                return None
            if existing["status"] == "cancelled":
                conn.execute(
                    """UPDATE alerts SET status='pending',result_id=?,next_attempt_at=?,
                    error_summary=NULL WHERE id=?""", (result_id, now, existing["id"]),
                )
            return existing["id"]

    def get_pending_alerts(self, limit=20, alert_id=None):
        now = utc_now()
        ready = []
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT a.*,r.earliest_slot,r.slot_count,r.practitioner_name,r.practice_name,
                r.booking_url,r.checked_at,j.time_zone,j.status AS job_status,j.telegram_enabled,
                j.interval_seconds,t.active AS target_active,s.last_status,s.last_earliest_slot
                FROM alerts a JOIN check_results r ON r.id=a.result_id
                JOIN jobs j ON j.id=a.job_id
                LEFT JOIN targets t ON t.id=a.target_id
                LEFT JOIN target_alert_state s ON s.target_id=a.target_id
                WHERE a.status IN ('pending','failed') AND (? IS NULL OR a.id=?)
                ORDER BY a.created_at""", (alert_id, alert_id),
            ).fetchall()
            for row in rows:
                alert = dict(row)
                reason = None
                if alert["job_status"] == "deleted":
                    reason = "job_deleted"
                elif not alert["target_active"]:
                    reason = "target_removed"
                elif not alert["earliest_slot"] or parse_time(alert["earliest_slot"]) <= now:
                    reason = "slot_expired"
                elif (alert["last_status"] != "available" or
                      alert["last_earliest_slot"] != alert["earliest_slot"]):
                    reason = "availability_changed"
                if reason:
                    conn.execute(
                        """UPDATE alerts SET status='cancelled',next_attempt_at=NULL,error_summary=?
                        WHERE id=? AND status IN ('pending','failed')""", (reason, alert["id"]),
                    )
                    continue
                if (alert["job_status"] != "active" or not alert["telegram_enabled"] or
                        now - parse_time(alert["checked_at"]) > timedelta(seconds=alert["interval_seconds"]) or
                        (alert["next_attempt_at"] and parse_time(alert["next_attempt_at"]) > now)):
                    continue
                ready.append(alert)
                if len(ready) >= limit:
                    break
        return ready

    def finish_alert(self, alert_id, sent, error_summary=None):
        with self.database.connection() as conn:
            row = conn.execute("SELECT attempt_count FROM alerts WHERE id=?", (alert_id,)).fetchone()
            if row is None:
                return
            attempt = row["attempt_count"] + 1
            delay = min(300, 2 ** min(attempt, 8))
            next_attempt = None if sent else iso(utc_now() + timedelta(seconds=delay))
            conn.execute(
                """UPDATE alerts SET status=?,attempt_count=?,sent_at=?,error_summary=?,next_attempt_at=?
                WHERE id=? AND status IN ('pending','failed')""",
                ("sent" if sent else "failed", attempt, iso() if sent else None,
                 error_summary[:240] if error_summary else None, next_attempt, alert_id),
            )

    def finish_run(self, run_id, job_id, successful, failed, interval_seconds, error=None):
        now = utc_now()
        outcome = "error" if failed and not successful else "partial_error" if failed else "completed"
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            owner = conn.execute(
                "SELECT lock_run_id FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            run = conn.execute(
                "SELECT outcome FROM check_runs WHERE id=? AND job_id=?", (run_id, job_id)
            ).fetchone()
            if run is None or run["outcome"] != "running":
                return
            if owner is None or owner["lock_run_id"] != run_id:
                conn.execute(
                    "UPDATE check_runs SET outcome='interrupted',finished_at=? WHERE id=?",
                    (iso(now), run_id),
                )
                return
            job_row = conn.execute(
                "SELECT interval_seconds FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            effective_interval = max(
                self.minimum_poll_interval_seconds,
                job_row["interval_seconds"] if job_row else interval_seconds,
            )
            conn.execute(
                "UPDATE check_runs SET finished_at=?,outcome=?,successful_targets=?,failed_targets=? WHERE id=?",
                (iso(now), outcome, successful, failed, run_id),
            )
            conn.execute(
                """UPDATE jobs SET next_check_at=CASE WHEN status='active' THEN ? ELSE next_check_at END,
                last_finished_at=?,last_outcome=?,lock_until=NULL,lock_run_id=NULL
                WHERE id=? AND lock_run_id=?""",
                (precise_iso(now + timedelta(seconds=effective_interval)), precise_iso(now), outcome, job_id, run_id),
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
            conn.execute("BEGIN IMMEDIATE")
            stale = conn.execute(
                """SELECT r.id,r.job_id FROM check_runs r LEFT JOIN jobs j ON j.id=r.job_id
                WHERE r.outcome='running' AND (j.id IS NULL OR j.lock_until IS NULL
                OR j.lock_until<=? OR j.lock_run_id!=r.id)""", (now,)
            ).fetchall()
            for run in stale:
                conn.execute(
                    "UPDATE check_runs SET outcome='interrupted',finished_at=? WHERE id=? AND outcome='running'",
                    (now, run["id"]),
                )
                conn.execute(
                    """UPDATE jobs SET lock_until=NULL,lock_run_id=NULL
                    WHERE id=? AND lock_run_id=?""", (run["job_id"], run["id"]),
                )
            conn.execute("UPDATE jobs SET lock_until=NULL,lock_run_id=NULL WHERE lock_until<=?", (now,))

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
                    "worker": self.worker_status(), "last_completed_run": dict(last_run) if last_run else None,
                    "next_check_at": next_check}

    def checks(self, job_id, limit=50, offset=0):
        with self.database.connection() as conn:
            runs = [dict(row) for row in conn.execute(
                """SELECT id,job_id,job_name,started_at,finished_at,outcome,successful_targets,
                failed_targets,triggered_by FROM check_runs WHERE job_id=?
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
