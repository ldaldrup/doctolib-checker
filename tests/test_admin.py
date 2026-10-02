import json
import os
from pathlib import Path
import sqlite3
import threading
import zipfile

import pytest

from app import admin
from app.storage.db import Database
from test_backend_journey import create_job, FakeNotifier, setup_backend, drop_revision_columns, drop_delivery_columns, drop_intent_columns, Journey
from app.services.checks import CheckService


def seeded(tmp_path):
    client, repository, settings, doctolib = setup_backend(tmp_path)
    create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier()).run_due()
    create_job(client)
    Journey(repository, doctolib, settings, notifier=FakeNotifier(False)).run_due()
    repository.update_settings({"default_interval_seconds": 600}, 300)
    return Path(settings.database_path), repository


def rows(path):
    tables = ("jobs", "targets", "check_runs", "check_results", "alerts", "target_alert_state", "settings")
    with sqlite3.connect(path) as conn:
        return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}


def test_backup_restore_preserves_data_and_uses_offline_health(tmp_path, monkeypatch):
    source, repository = seeded(tmp_path)
    expected = rows(source)
    archive = tmp_path / "backup.zip"
    monkeypatch.setenv("DATABASE_PATH", str(source))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "private-fixture-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "private-fixture-chat")
    monkeypatch.setattr("requests.sessions.Session.request", lambda *a, **k: pytest.fail("network used"))
    monkeypatch.setattr("app.notifications.send_telegram_alert", lambda *a, **k: pytest.fail("send used"))
    assert admin.backup(source, archive)["schema_version"] == 6
    original = archive.read_bytes()
    work = tmp_path / "verify"
    assert admin.verify(archive, work) == {
        "status": "verified", "backup_schema_version": 6, "restored_schema_version": 6,
        "integrity": "ok", "foreign_keys": "ok", "health": "ok",
    }
    assert rows(work / "restored.sqlite3") == expected
    assert rows(source) == expected
    assert archive.read_bytes() == original
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
    assert manifest["schema_version"] == 6
    assert "private-fixture" not in json.dumps(manifest)
    assert manifest["created_at"].endswith("+00:00")
    for path in (archive, work / "checker.sqlite3", work / "restored.sqlite3"):
        assert path.stat().st_mode & 0o777 == 0o600
    assert work.stat().st_mode & 0o777 == 0o700


def test_wal_snapshot_is_consistent_with_concurrent_transactions(tmp_path):
    source, repository = seeded(tmp_path)
    keeper = sqlite3.connect(source)
    keeper.execute("PRAGMA wal_autocheckpoint=0")
    keeper.execute("CREATE TABLE backup_load(value BLOB)")
    keeper.execute("INSERT INTO backup_load VALUES(zeroblob(4194304))")
    keeper.commit()
    first_commit = threading.Event()
    stop = threading.Event()
    errors = []

    def writer():
        try:
            with sqlite3.connect(source, timeout=5) as conn:
                conn.execute("PRAGMA wal_autocheckpoint=0")
                for generation in range(601, 651):
                    conn.execute("UPDATE jobs SET interval_seconds=?", (generation,))
                    conn.execute("UPDATE settings SET default_interval_seconds=?", (generation,))
                    conn.commit()
                    first_commit.set()
                    if stop.wait(0.001):
                        break
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=writer)
    worker.start()
    try:
        assert first_commit.wait(5)
        assert Path(str(source) + "-wal").exists()
        archive = tmp_path / "wal.zip"
        admin.backup(source, archive)
    finally:
        stop.set()
        worker.join(5)
        keeper.close()
    assert not worker.is_alive() and not errors
    work = tmp_path / "wal-verify"
    admin.verify(archive, work)
    with sqlite3.connect(work / "restored.sqlite3") as conn:
        setting = conn.execute("SELECT default_interval_seconds FROM settings").fetchone()[0]
        assert 601 <= setting <= 650
        assert {row[0] for row in conn.execute("SELECT interval_seconds FROM jobs")} == {setting}


@pytest.mark.parametrize("alias", ["original", "symlink", "hardlink", "existing_archive"])
def test_refuses_existing_destination_and_source_aliases(tmp_path, alias):
    source, _ = seeded(tmp_path)
    destination = tmp_path / "existing"
    if alias == "original":
        destination = source
    elif alias == "symlink":
        destination.symlink_to(source)
    elif alias == "hardlink":
        os.link(source, destination)
    else:
        destination.write_bytes(b"existing backup")
    before = destination.read_bytes()
    with pytest.raises(admin.AdminError, match="destination_exists"):
        admin.backup(source, destination)
    assert destination.read_bytes() == before
    assert not list(tmp_path.glob(".checker-backup-*"))


def test_publish_race_cannot_overwrite_existing_output(tmp_path, monkeypatch):
    source, _ = seeded(tmp_path)
    destination = tmp_path / "racing.zip"
    link = os.link

    def competing_publish(src, dest):
        Path(dest).write_bytes(b"winner")
        link(src, dest)

    monkeypatch.setattr(admin.os, "link", competing_publish)
    with pytest.raises(admin.AdminError, match="destination_exists"):
        admin.backup(source, destination)
    assert destination.read_bytes() == b"winner"
    assert not list(tmp_path.glob(".checker-backup-*"))


@pytest.mark.parametrize("fault", ["missing", "invalid", "interrupted"])
def test_failed_backup_cleans_temporary_output(tmp_path, monkeypatch, fault):
    source = tmp_path / "source.sqlite3"
    if fault != "missing":
        source.write_bytes(b"not a database")
    if fault == "interrupted":
        def interrupt(_source, target, _timeout):
            target.write_bytes(b"partial destination")
            raise KeyboardInterrupt()
        monkeypatch.setattr(admin, "_snapshot", interrupt)
    destination = tmp_path / "new.zip"
    with pytest.raises((admin.AdminError, sqlite3.Error, KeyboardInterrupt)):
        admin.backup(source, destination)
    assert not destination.exists()
    assert source.exists() == (fault != "missing")
    if source.exists():
        assert source.read_bytes() == b"not a database"
    assert not list(tmp_path.glob(".checker-backup-*"))


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_legacy_archive_stays_immutable_while_copy_migrates(tmp_path, version):
    source, _ = seeded(tmp_path)
    expected = rows(source)
    with sqlite3.connect(source) as conn:
        if version < 4:
            drop_revision_columns(conn)
        elif version < 5:
            drop_delivery_columns(conn)
        else:
            drop_intent_columns(conn)
        if version < 3:
            conn.execute("DROP TABLE target_alert_state")
        if version == 1:
            conn.execute("ALTER TABLE jobs DROP COLUMN lock_run_id")
        conn.execute("UPDATE schema_version SET version=?", (version,))
    archive = tmp_path / "legacy.zip"
    admin.backup(source, archive)
    before = archive.read_bytes()
    work = tmp_path / "legacy-verify"
    report = admin.verify(archive, work)
    assert report["backup_schema_version"] == version
    assert report["restored_schema_version"] == 6 and report["health"] == "ok"
    assert archive.read_bytes() == before
    with sqlite3.connect(work / "checker.sqlite3") as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == version
    actual = rows(work / "restored.sqlite3")
    for table in ("targets", "settings"):
        assert actual[table] == expected[table]
    with sqlite3.connect(work / "restored.sqlite3") as conn:
        if version < 4:
            assert conn.execute("SELECT COUNT(*) FROM check_runs WHERE snapshot_known=1 OR search_revision IS NOT NULL OR search_snapshot IS NOT NULL").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM check_results WHERE snapshot_known=1 OR search_revision IS NOT NULL OR published=1").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM alerts WHERE search_revision IS NOT NULL").fetchone()[0] == 0
    assert len(actual["target_alert_state"]) == len(expected["target_alert_state"])


def test_newer_schema_only_verified_without_migration(tmp_path):
    source, _ = seeded(tmp_path)
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE schema_version SET version=99")
    archive = tmp_path / "future.zip"
    admin.backup(source, archive)
    with pytest.raises(admin.AdminError, match="unsupported_restore_schema"):
        admin.verify(archive, tmp_path / "not-supported")
    assert not (tmp_path / "not-supported").exists()
    assert admin.verify(archive, tmp_path / "integrity", integrity_only=True)["status"] == "integrity_verified"


def test_verification_refuses_existing_directory_and_cleans_failed_work(tmp_path):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(b"invalid zip")
    existing = tmp_path / "existing"
    existing.mkdir()
    sentinel = existing / "important"
    sentinel.write_text("keep")
    with pytest.raises(admin.AdminError, match="work_directory_exists"):
        admin.verify(archive, existing)
    assert sentinel.read_text() == "keep"
    with pytest.raises(zipfile.BadZipFile):
        admin.verify(archive, tmp_path / "failed")
    assert not (tmp_path / "failed").exists()


def test_cli_errors_do_not_expose_private_paths(tmp_path, capsys):
    source = tmp_path / "private-token-source"
    assert admin.main(["backup", "--source", str(source), "--destination", str(tmp_path / "out.zip")]) == 1
    output = capsys.readouterr()
    assert "private-token" not in output.err and str(tmp_path) not in output.err
    assert json.loads(output.err)["code"] == "source_missing_or_not_a_file"


@pytest.mark.parametrize("missing", ["all", "jobs", "settings", "alerts", "column"])
def test_incomplete_checker_cannot_be_blessed_by_initialization(tmp_path, missing):
    source = tmp_path / "incomplete.sqlite3"
    if missing == "all":
        with sqlite3.connect(source) as conn:
            conn.executescript("CREATE TABLE schema_version(version INTEGER); INSERT INTO schema_version VALUES(3);")
    else:
        Database(str(source)).initialize()
        with sqlite3.connect(source) as conn:
            if missing == "column":
                conn.execute("ALTER TABLE jobs DROP COLUMN lock_run_id")
            else:
                conn.execute(f"DROP TABLE {missing}")
    # Close/checkpoint this synthetic WAL fixture before deliberately assembling
    # a raw malformed bundle; copying only its main file would hide the damage.
    conn.close()
    before = source.read_bytes()
    with pytest.raises(admin.AdminError, match="incomplete_checker_schema"):
        admin.backup(source, tmp_path / "bad.zip")
    assert source.read_bytes() == before and not (tmp_path / "bad.zip").exists()
    # A manually assembled checksum-valid bundle must fail before initialize()
    # can synthesize empty replacement tables in its disposable restored copy.
    archive = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(source, admin.DATABASE_MEMBER)
        bundle.writestr(admin.MANIFEST_MEMBER, json.dumps({
            "format_version": 1, "schema_version": 3, "database_sha256": admin._digest(source),
        }))
    archive_before = archive.read_bytes()
    with pytest.raises(admin.AdminError, match="incomplete_checker_schema"):
        admin.verify(archive, tmp_path / "rejected")
    assert archive.read_bytes() == archive_before and not (tmp_path / "rejected").exists()


def test_source_connection_closes_if_target_cannot_open(tmp_path, monkeypatch):
    source = sqlite3.connect(":memory:")
    monkeypatch.setattr(admin, "_readonly", lambda path: source)
    with pytest.raises(sqlite3.Error):
        admin._snapshot(tmp_path / "source", tmp_path / "missing-parent" / "target", 30)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        source.execute("SELECT 1")
