"""SQLite connection and schema setup."""

import os
import sqlite3
from contextlib import contextmanager


SCHEMA_VERSION = 7


class Database:
    def __init__(self, path):
        self.path = path

    def initialize(self):
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        with self.connection() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone():
                versions = conn.execute("SELECT version FROM schema_version").fetchall()
                if len(versions) != 1 or versions[0][0] not in (1, 2, 3, 4, 5, 6, 7):
                    raise RuntimeError("Unsupported database schema version")
            conn.executescript("BEGIN IMMEDIATE;" +
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    default_interval_seconds INTEGER NOT NULL DEFAULT 300,
                    request_spacing_seconds REAL NOT NULL DEFAULT 3,
                    edit_version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS create_operations (
                    key TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    canonical_values TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','completed','failed')),
                    owner_token TEXT,
                    generation INTEGER NOT NULL DEFAULT 1,
                    lease_until TEXT,
                    job_id TEXT REFERENCES jobs(id),
                    error_code TEXT,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_create_operations_expiry ON create_operations(expires_at);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'deleted')),
                    interval_seconds INTEGER NOT NULL CHECK (interval_seconds >= 300),
                    date_mode TEXT NOT NULL CHECK (date_mode IN ('first_available', 'custom')),
                    horizon_days INTEGER,
                    earliest_date TEXT,
                    latest_date TEXT,
                    time_zone TEXT NOT NULL,
                    insurance_sector TEXT NOT NULL CHECK (insurance_sector IN ('public', 'private')),
                    telehealth INTEGER NOT NULL CHECK (telehealth IN (0, 1)),
                    telegram_enabled INTEGER NOT NULL CHECK (telegram_enabled IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    next_check_at TEXT NOT NULL,
                    last_started_at TEXT,
                    last_finished_at TEXT,
                    last_outcome TEXT,
                    lock_until TEXT,
                    lock_run_id TEXT,
                    lock_owner_token TEXT,
                    search_revision INTEGER NOT NULL DEFAULT 1,
                    edit_version INTEGER NOT NULL DEFAULT 1,
                    status_version INTEGER NOT NULL DEFAULT 1,
                    last_extra_started_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, next_check_at);
                CREATE TABLE IF NOT EXISTS targets (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    booking_url TEXT NOT NULL,
                    country TEXT NOT NULL,
                    profile_slug TEXT NOT NULL,
                    practice_id TEXT,
                    motive_id TEXT,
                    practitioner_id TEXT,
                    agenda_ids TEXT NOT NULL,
                    practice_name TEXT NOT NULL,
                    practitioner_name TEXT NOT NULL,
                    motive_name TEXT,
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    validation_state TEXT NOT NULL CHECK (validation_state IN ('ready', 'invalid')),
                    last_validated_at TEXT NOT NULL,
                    UNIQUE(job_id, booking_url)
                );
                CREATE INDEX IF NOT EXISTS idx_targets_job ON targets(job_id);
                CREATE TABLE IF NOT EXISTS check_runs (
                    id TEXT PRIMARY KEY,
                    job_id TEXT REFERENCES jobs(id),
                    job_name TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    outcome TEXT NOT NULL CHECK (outcome IN ('running', 'completed', 'partial_error', 'error', 'interrupted')),
                    successful_targets INTEGER NOT NULL DEFAULT 0,
                    failed_targets INTEGER NOT NULL DEFAULT 0,
                    triggered_by TEXT NOT NULL DEFAULT 'schedule',
                    search_revision INTEGER,
                    search_snapshot TEXT,
                    snapshot_known INTEGER NOT NULL DEFAULT 0,
                    owner_token TEXT,
                    intent_id TEXT,
                    paused_manual INTEGER NOT NULL DEFAULT 0,
                    status_version INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_runs_job_time ON check_runs(job_id, started_at DESC);
                CREATE TABLE IF NOT EXISTS check_results (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES check_runs(id),
                    job_id TEXT REFERENCES jobs(id),
                    target_id TEXT REFERENCES targets(id),
                    practitioner_name TEXT NOT NULL,
                    practice_name TEXT NOT NULL,
                    booking_url TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('available', 'no_availability', 'error')),
                    slot_count INTEGER NOT NULL DEFAULT 0,
                    earliest_slot TEXT,
                    count_complete INTEGER NOT NULL DEFAULT 1,
                    error_code TEXT,
                    error_message TEXT,
                    search_revision INTEGER,
                    snapshot_known INTEGER NOT NULL DEFAULT 0,
                    published INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_results_target_time ON check_results(target_id, checked_at DESC);
                CREATE TABLE IF NOT EXISTS target_alert_state (
                    target_id TEXT PRIMARY KEY REFERENCES targets(id),
                    last_status TEXT CHECK (last_status IN ('available', 'no_availability')),
                    last_earliest_slot TEXT,
                    episode INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY,
                    job_id TEXT REFERENCES jobs(id),
                    target_id TEXT REFERENCES targets(id),
                    result_id TEXT REFERENCES check_results(id),
                    channel TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'cancelled')),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    error_summary TEXT,
                    next_attempt_at TEXT,
                    search_revision INTEGER,
                    delivery_state TEXT NOT NULL DEFAULT 'ready',
                    claim_owner_token TEXT,
                    claim_until TEXT,
                    claim_result_id TEXT,
                    claim_search_revision INTEGER,
                    attempt_started_at TEXT,
                    last_attempt_at TEXT,
                    last_attempt_outcome TEXT,
                    delivery_epoch_at TEXT,
                    delivery_epoch_attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC);
                CREATE TABLE IF NOT EXISTS worker_heartbeat (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    started_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_completed_run_at TEXT,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS dispatcher_heartbeat (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    started_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS check_intents (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    search_revision INTEGER NOT NULL,
                    triggered_by TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    eligible_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','cancelled')),
                    paused_manual INTEGER NOT NULL DEFAULT 0,
                    status_version INTEGER NOT NULL,
                    run_id TEXT,
                    cancel_reason TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_intents_pending_job ON check_intents(job_id) WHERE status='queued';
                CREATE TABLE IF NOT EXISTS request_gate (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    next_allowed_at TEXT NOT NULL
                );
                """
            )
            version = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if version is None:
                conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            else:
                current_version = version[0]
                if current_version == 1:
                    conn.execute("ALTER TABLE jobs ADD COLUMN lock_run_id TEXT")
                    # Preserve an in-flight v1 lease across the schema upgrade.
                    conn.execute(
                        """UPDATE jobs SET lock_run_id=(SELECT id FROM check_runs
                        WHERE check_runs.job_id=jobs.id AND outcome='running'
                        ORDER BY started_at DESC LIMIT 1)
                        WHERE lock_until IS NOT NULL"""
                    )
                    current_version = 2
                if current_version == 2:
                    # SQLite cannot extend a CHECK constraint in place.
                    conn.execute("""CREATE TABLE alerts_v3 (
                        id TEXT PRIMARY KEY,
                        job_id TEXT REFERENCES jobs(id),
                        target_id TEXT REFERENCES targets(id),
                        result_id TEXT REFERENCES check_results(id),
                        channel TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        dedupe_key TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'cancelled')),
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        sent_at TEXT,
                        error_summary TEXT,
                        next_attempt_at TEXT
                    )""")
                    conn.execute("INSERT INTO alerts_v3 SELECT * FROM alerts")
                    conn.execute("DROP TABLE alerts")
                    conn.execute("ALTER TABLE alerts_v3 RENAME TO alerts")
                    conn.execute("CREATE INDEX idx_alerts_created ON alerts(created_at DESC)")
                    conn.execute("""INSERT INTO target_alert_state
                        (target_id,last_status,last_earliest_slot,episode)
                        SELECT t.id,r.status,r.earliest_slot,0 FROM targets t
                        LEFT JOIN check_results r ON r.rowid=(
                            SELECT previous.rowid FROM check_results previous
                            WHERE previous.target_id=t.id AND previous.status IN ('available','no_availability')
                            ORDER BY previous.rowid DESC LIMIT 1
                        )""")
                    current_version = 3
                if current_version == 3:
                    additions = {
                        "jobs": ["lock_owner_token TEXT", "search_revision INTEGER NOT NULL DEFAULT 1",
                                 "edit_version INTEGER NOT NULL DEFAULT 1"],
                        "check_runs": ["search_revision INTEGER", "search_snapshot TEXT",
                                       "snapshot_known INTEGER NOT NULL DEFAULT 0", "owner_token TEXT"],
                        "check_results": ["search_revision INTEGER", "snapshot_known INTEGER NOT NULL DEFAULT 0",
                                          "published INTEGER NOT NULL DEFAULT 0"],
                        "alerts": ["search_revision INTEGER"],
                    }
                    for table, columns in additions.items():
                        for column in columns:
                            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
                    # Legacy in-flight work has no provable snapshot/owner. Keep
                    # its evidence, but never let an older worker publish it.
                    conn.execute("""UPDATE check_runs SET outcome='interrupted',
                        successful_targets=(SELECT COUNT(*) FROM check_results WHERE run_id=check_runs.id AND status!='error'),
                        failed_targets=(SELECT COUNT(*) FROM check_results WHERE run_id=check_runs.id AND status='error')
                        WHERE outcome='running'""")
                    conn.execute("UPDATE jobs SET lock_until=NULL,lock_run_id=NULL,lock_owner_token=NULL")
                    current_version = 4
                if current_version == 4:
                    for column in ("delivery_state TEXT NOT NULL DEFAULT 'ready'", "claim_owner_token TEXT",
                                   "claim_until TEXT", "claim_result_id TEXT", "claim_search_revision INTEGER",
                                   "attempt_started_at TEXT", "last_attempt_at TEXT", "last_attempt_outcome TEXT",
                                   "delivery_epoch_at TEXT", "delivery_epoch_attempts INTEGER NOT NULL DEFAULT 0"):
                        conn.execute(f"ALTER TABLE alerts ADD COLUMN {column}")
                    # Any unsent inline alert may have reached Telegram before a
                    # crash, even with zero durable attempts. Do not
                    # silently replay an acknowledgement that was lost.
                    conn.execute("UPDATE alerts SET delivery_state='uncertain',last_attempt_outcome='legacy_unknown' WHERE status IN ('pending','failed')")
                    conn.execute("UPDATE alerts SET delivery_epoch_at=created_at,delivery_epoch_attempts=attempt_count")
                    current_version = 5
                if current_version == 5:
                    for table, columns in {
                        "jobs": ("status_version INTEGER NOT NULL DEFAULT 1", "last_extra_started_at TEXT"),
                        "check_runs": ("intent_id TEXT", "paused_manual INTEGER NOT NULL DEFAULT 0", "status_version INTEGER NOT NULL DEFAULT 1"),
                    }.items():
                        for column in columns:
                            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
                    current_version = 6
                if current_version == 6:
                    conn.execute("ALTER TABLE settings ADD COLUMN edit_version INTEGER NOT NULL DEFAULT 1")
                    conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
                elif current_version != SCHEMA_VERSION:
                    raise RuntimeError("Unsupported database schema version")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_results_known_run_target ON check_results(run_id,target_id) WHERE snapshot_known=1")
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level="DEFERRED")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
