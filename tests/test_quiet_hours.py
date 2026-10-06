from datetime import datetime, timezone

import pytest

from app.quiet_hours import next_release, quiet_window, validate_quiet_hours
from app.storage.db import Database, SCHEMA_VERSION


def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_daytime_midnight_and_exact_boundaries():
    zone = "Europe/Berlin"
    assert quiet_window(utc("2026-10-05T18:00:00Z"), zone, "22:00", "07:00") is None
    assert quiet_window(utc("2026-10-05T20:00:00Z"), zone, "22:00", "07:00") == utc("2026-10-06T05:00:00Z")
    assert quiet_window(utc("2026-10-06T05:00:00Z"), zone, "22:00", "07:00") is None
    assert quiet_window(utc("2026-10-06T08:30:00Z"), zone, "09:00", "17:00") == utc("2026-10-06T15:00:00Z")
    assert next_release(utc("2026-10-05T18:00:00Z"), zone, True, "22:00", "07:00") == utc("2026-10-06T05:00:00Z")


def test_spring_gap_moves_missing_boundary_to_first_valid_minute():
    zone = "Europe/Berlin"
    assert quiet_window(utc("2026-03-29T00:50:00Z"), zone, "01:30", "02:30") == utc("2026-03-29T01:00:00Z")
    assert quiet_window(utc("2026-03-29T01:00:00Z"), zone, "01:30", "02:30") is None
    assert quiet_window(utc("2026-03-29T01:15:00Z"), zone, "02:30", "04:00") == utc("2026-03-29T02:00:00Z")


def test_fall_fold_uses_earliest_start_and_latest_end():
    # 02:15 occurs twice; start uses CEST and end uses CET.
    until = utc("2026-10-25T01:45:00Z")
    assert quiet_window(utc("2026-10-25T01:00:00Z"), "Europe/Berlin", "02:15", "02:45") == until
    assert quiet_window(until, "Europe/Berlin", "02:15", "02:45") is None


def test_equal_enabled_bounds_are_rejected_but_disabled_is_explicit():
    with pytest.raises(ValueError, match="must differ"):
        validate_quiet_hours(True, "07:00", "07:00")
    assert validate_quiet_hours(False, "07:00", "07:00")
    assert next_release(utc("2026-10-05T18:00:00Z"), "Europe/Berlin", False, "07:00", "07:00") is None


def test_schema_11_migrates_to_disabled_quiet_hours(tmp_path):
    database = Database(str(tmp_path / "schema.sqlite3"))
    database.initialize()
    with database.connection() as conn:
        for column in ("quiet_hours_enabled", "quiet_hours_start", "quiet_hours_end"):
            conn.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
        for column in ("quiet_state", "quiet_until"):
            conn.execute(f"ALTER TABLE alerts DROP COLUMN {column}")
        conn.execute("UPDATE schema_version SET version=11")

    database.initialize()
    with database.connection() as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        defaults = {row[1]: row[4] for row in conn.execute("PRAGMA table_info(jobs)")}
        assert defaults["quiet_hours_enabled"] == "0"
        assert defaults["quiet_hours_start"] == "'22:00'"
        assert defaults["quiet_hours_end"] == "'07:00'"
