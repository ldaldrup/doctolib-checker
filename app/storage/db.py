"""SQLite connection and schema setup."""

import os
import sqlite3
from contextlib import contextmanager


SCHEMA_VERSION = 2


class Database:
    def __init__(self, path):
        self.path = path

    def initialize(self):
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    default_interval_seconds INTEGER NOT NULL DEFAULT 300,
                    request_spacing_seconds REAL NOT NULL DEFAULT 3,
                    updated_at TEXT NOT NULL
                );
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
                    lock_run_id TEXT
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
                    triggered_by TEXT NOT NULL DEFAULT 'schedule'
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
                    error_message TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_results_target_time ON check_results(target_id, checked_at DESC);
                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY,
                    job_id TEXT REFERENCES jobs(id),
                    target_id TEXT REFERENCES targets(id),
                    result_id TEXT REFERENCES check_results(id),
                    channel TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    error_summary TEXT,
                    next_attempt_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC);
                CREATE TABLE IF NOT EXISTS worker_heartbeat (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    started_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_completed_run_at TEXT,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS request_gate (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    next_allowed_at TEXT NOT NULL
                );
                """
            )
            version = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if version is None:
                conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            elif version[0] == 1:
                conn.execute("ALTER TABLE jobs ADD COLUMN lock_run_id TEXT")
                # Preserve an in-flight v1 lease across the schema upgrade.
                conn.execute(
                    """UPDATE jobs SET lock_run_id=(SELECT id FROM check_runs
                    WHERE check_runs.job_id=jobs.id AND outcome='running'
                    ORDER BY started_at DESC LIMIT 1)
                    WHERE lock_until IS NOT NULL"""
                )
                conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
            elif version[0] != SCHEMA_VERSION:
                raise RuntimeError("Unsupported database schema version")
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
