"""Local SQLite backup and disposable verification; no live restore or sends."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
import zipfile
from contextlib import closing

from app.storage.db import Database, SCHEMA_VERSION


DATABASE_MEMBER = "checker.sqlite3"
MANIFEST_MEMBER = "manifest.json"
FORMAT_VERSION = 1
# Structure checks for the formats this release can rehearse. Future migrations
# must extend these requirements before advertising a new restore format.
SUPPORTED_SCHEMAS = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10}
REQUIRED_COLUMNS = {
    "settings": "singleton_id default_interval_seconds request_spacing_seconds updated_at",
    "jobs": "id name status interval_seconds date_mode horizon_days earliest_date latest_date "
            "time_zone insurance_sector telehealth telegram_enabled created_at updated_at next_check_at "
            "last_started_at last_finished_at last_outcome lock_until",
    "targets": "id job_id booking_url country profile_slug practice_id motive_id practitioner_id "
               "agenda_ids practice_name practitioner_name motive_name active validation_state last_validated_at",
    "check_runs": "id job_id job_name started_at finished_at outcome successful_targets failed_targets triggered_by",
    "check_results": "id run_id job_id target_id practitioner_name practice_name booking_url checked_at "
                     "status slot_count earliest_slot count_complete error_code error_message",
    "alerts": "id job_id target_id result_id channel event_type dedupe_key status attempt_count "
              "created_at sent_at error_summary next_attempt_at",
    "worker_heartbeat": "singleton_id started_at last_seen_at last_completed_run_at last_error",
    "request_gate": "singleton_id next_allowed_at",
}


class AdminError(Exception):
    """A safe user-facing failure, without database contents or input paths."""


def _readonly(path):
    if not path.is_file():
        raise AdminError("source_missing_or_not_a_file")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def _inspect(path):
    with closing(_readonly(path)) as conn:
        try:
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise AdminError("database_integrity_failed")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise AdminError("database_foreign_keys_failed")
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
            if len(rows) != 1 or type(rows[0][0]) is not int or rows[0][0] < 1:
                raise AdminError("invalid_schema_version")
            version = rows[0][0]
            if version in SUPPORTED_SCHEMAS:
                requirements = dict(REQUIRED_COLUMNS)
                if version >= 2:
                    requirements["jobs"] += " lock_run_id"
                if version >= 3:
                    requirements["target_alert_state"] = "target_id last_status last_earliest_slot episode"
                if version >= 4:
                    requirements["jobs"] += " lock_owner_token search_revision edit_version"
                    requirements["check_runs"] += " search_revision search_snapshot snapshot_known owner_token"
                    requirements["check_results"] += " search_revision snapshot_known published"
                    requirements["alerts"] += " search_revision"
                if version >= 5:
                    requirements["alerts"] += " delivery_state claim_owner_token claim_until claim_result_id claim_search_revision attempt_started_at last_attempt_at last_attempt_outcome delivery_epoch_at delivery_epoch_attempts"
                    requirements["dispatcher_heartbeat"] = "singleton_id started_at last_seen_at last_error"
                if version >= 6:
                    requirements["jobs"] += " status_version last_extra_started_at"
                    requirements["check_runs"] += " intent_id paused_manual status_version"
                    requirements["check_intents"] = "id job_id search_revision triggered_by requested_at eligible_at status paused_manual status_version run_id cancel_reason"
                if version >= 7:
                    requirements["settings"] += " edit_version"
                    requirements["create_operations"] = "key fingerprint canonical_values state owner_token generation lease_until job_id error_code retryable created_at expires_at"
                if version >= 8:
                    requirements["alerts"] += " event_id channel_config_id destination_version credential_version channel_name"
                    requirements["notification_channels"] = "id type name enabled deleted edit_version destination_version credential_version token_ciphertext chat_ciphertext destination_identity created_at updated_at"
                    requirements["job_channels"] = "job_id channel_config_id"
                    requirements["availability_events"] = "id job_id target_id result_id dedupe_key search_revision observed_at routed_at routing_snapshot"
                    requirements["channel_mutations"] = "key fingerprint channel_id created_at expires_at"
                    requirements["notification_onboarding"] = "singleton_id channel_id"
                    requirements["channel_tests"] = "id key fingerprint channel_config_id destination_version credential_version status owner_token claim_until attempt_started_at attempt_count error_code created_at updated_at expires_at"
                if version >= 9:
                    requirements["notification_channels"] += " endpoint_ciphertext auth_type auth_token_ciphertext auth_username_ciphertext auth_password_ciphertext ntfy_priority"
                if version >= 10:
                    requirements["notification_channels"] += " email_recipient_ciphertext"
                    requirements["smtp_transport"] = "singleton_id enabled host port tls_mode sender_name sender_email_ciphertext username_ciphertext password_ciphertext destination_identity edit_version destination_version credential_version created_at updated_at"
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table, fields in requirements.items():
                    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                    if table not in tables or not set(fields.split()) <= columns:
                        raise AdminError("incomplete_checker_schema")
        except sqlite3.Error:
            raise AdminError("invalid_checker_database") from None
    return version


def _revision():
    """Identify the backup tool checkout, never infer the deployed writer's build."""
    try:
        root = Path(__file__).resolve().parent.parent
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal", "--", "app", "requirements.txt"],
            cwd=root, capture_output=True, text=True, timeout=2, check=True,
        )
        if status.stdout:
            return None  # A base commit cannot identify modified/untracked application code.
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=2, check=True,
        )
        revision = result.stdout.strip()
        if len(revision) == 40 and all(char in "0123456789abcdef" for char in revision):
            return revision
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _snapshot(source, target, timeout):
    deadline = time.monotonic() + timeout

    def progress(_status, _remaining, _total):
        if time.monotonic() >= deadline:
            raise AdminError("backup_timeout")

    # TemporaryDirectory protects the initially created database and its sidecars.
    with closing(_readonly(source)) as source_conn, closing(sqlite3.connect(target)) as target_conn:
        source_conn.backup(target_conn, pages=256, progress=progress, sleep=0.05)
        target_conn.execute("PRAGMA journal_mode=DELETE")
    os.chmod(target, 0o600)


def backup(source, destination, timeout=30):
    """Publish one complete bundle with an atomic, no-overwrite hard link."""
    source, destination = Path(source), Path(destination)
    if timeout <= 0 or not timeout < float("inf"):
        raise AdminError("timeout_must_be_positive_and_finite")
    if os.path.lexists(destination):
        raise AdminError("destination_exists")
    if not destination.parent.is_dir():
        raise AdminError("destination_parent_missing")
    with tempfile.TemporaryDirectory(prefix=".checker-backup-", dir=destination.parent) as temporary:
        directory = Path(temporary)
        snapshot = directory / DATABASE_MEMBER
        _snapshot(source, snapshot, timeout)
        version = _inspect(snapshot)
        manifest = {
            "format_version": FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": version,
            "application_revision": _revision(),
            "revision_scope": "backup_tool_checkout",
            "database_sha256": _digest(snapshot),
        }
        archive = directory / "backup.zip"
        with archive.open("xb") as stream:
            os.chmod(archive, 0o600)
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as bundle:
                bundle.write(snapshot, DATABASE_MEMBER)
                bundle.writestr(MANIFEST_MEMBER, json.dumps(manifest, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Unlike rename(), link() cannot replace an existing path, even in a race.
        try:
            os.link(archive, destination)
        except FileExistsError:
            raise AdminError("destination_exists") from None
        if os.name == "posix":
            descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    return {"status": "backed_up", "schema_version": version,
            "application_revision": manifest["application_revision"]}


def _unpack(archive, directory):
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if (len(entries) != 2 or {entry.filename for entry in entries} !=
                {DATABASE_MEMBER, MANIFEST_MEMBER} or
                any(entry.compress_type != zipfile.ZIP_STORED for entry in entries)):
            raise AdminError("invalid_backup_members")
        if bundle.getinfo(MANIFEST_MEMBER).file_size > 16384:
            raise AdminError("invalid_backup_manifest")
        manifest = json.loads(bundle.read(MANIFEST_MEMBER))
        if (not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION or
                type(manifest.get("schema_version")) is not int or
                not isinstance(manifest.get("database_sha256"), str)):
            raise AdminError("invalid_backup_manifest")
        snapshot = directory / DATABASE_MEMBER
        with bundle.open(DATABASE_MEMBER) as source, snapshot.open("xb") as target:
            os.chmod(snapshot, 0o600)
            shutil.copyfileobj(source, target)
    if _digest(snapshot) != manifest["database_sha256"]:
        raise AdminError("backup_checksum_failed")
    version = _inspect(snapshot)
    if version != manifest["schema_version"]:
        raise AdminError("backup_schema_mismatch")
    return manifest


class _OfflineDoctolib:
    def resolve(self, *args, **kwargs):
        raise AdminError("verification_upstream_disabled")

    def check(self, *args, **kwargs):
        raise AdminError("verification_upstream_disabled")


async def _health(path):
    # Runtime dependencies only: no httpx/TestClient, socket, server or worker.
    from app.api.app import create_app
    from app.settings import Settings

    app = create_app(settings=Settings(database_path=str(path)), doctolib=_OfflineDoctolib())
    repository = app.state.repository
    jobs = repository.list_jobs(limit=1)
    if jobs:
        job_id = jobs[0]["id"]
        repository.get_job(job_id)
        repository.get_targets(job_id)
        repository.checks(job_id, limit=1)
    channels = repository.list_channels()["items"]
    if channels:
        repository.get_channel(channels[0]["id"])
    repository.alerts(limit=1)
    repository.dashboard_status()
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
               "method": "GET", "scheme": "http", "path": "/healthz", "raw_path": b"/healthz",
               "query_string": b"", "headers": [], "server": ("offline", 80),
               "client": ("offline", 0), "root_path": ""}, receive, send)
    starts = [message for message in messages if message["type"] == "http.response.start"]
    body = b"".join(message.get("body", b"") for message in messages
                    if message["type"] == "http.response.body")
    if len(starts) != 1 or starts[0]["status"] != 200 or json.loads(body) != {"status": "ok"}:
        raise AdminError("restored_health_failed")


def verify(archive, work_directory, integrity_only=False):
    """Verify an archive in a new private directory; migrate only a second copy."""
    archive, directory = Path(archive), Path(work_directory)
    if not archive.is_file():
        raise AdminError("archive_missing_or_not_a_file")
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        raise AdminError("work_directory_exists") from None
    try:
        manifest = _unpack(archive, directory)
        original_version = manifest["schema_version"]
        report = {"status": "integrity_verified", "backup_schema_version": original_version,
                  "integrity": "ok", "foreign_keys": "ok"}
        if not integrity_only:
            if original_version not in SUPPORTED_SCHEMAS or SCHEMA_VERSION not in SUPPORTED_SCHEMAS:
                raise AdminError("unsupported_restore_schema")
            restored = directory / "restored.sqlite3"
            shutil.copyfile(directory / DATABASE_MEMBER, restored)
            os.chmod(restored, 0o600)
            Database(str(restored)).initialize()
            asyncio.run(_health(restored))
            report.update(status="verified", restored_schema_version=_inspect(restored), health="ok")
        return report
    except BaseException:
        # Only this invocation's newly created disposable directory is removed.
        shutil.rmtree(directory)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    save = commands.add_parser("backup", help="snapshot an existing DB into a new private archive")
    save.add_argument("--source", required=True)
    save.add_argument("--destination", required=True)
    save.add_argument("--timeout", type=float, default=30)
    check = commands.add_parser("verify", help="verify/restore only into a new disposable directory")
    check.add_argument("--archive", required=True)
    check.add_argument("--work-directory", required=True)
    check.add_argument("--integrity-only", action="store_true", help="skip migration and API health")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            result = backup(args.source, args.destination, args.timeout)
        else:
            result = verify(args.archive, args.work_directory, args.integrity_only)
        print(json.dumps(result, sort_keys=True))
        return 0
    except AdminError as exc:
        print(json.dumps({"status": "error", "code": str(exc)}), file=sys.stderr)
    except (OSError, sqlite3.Error, zipfile.BadZipFile, ValueError, RuntimeError):
        # SQLite/OS/ZIP exceptions can contain private paths or schema contents.
        print(json.dumps({"status": "error", "code": "admin_operation_failed"}), file=sys.stderr)
    except KeyboardInterrupt:
        print(json.dumps({"status": "error", "code": "admin_operation_interrupted"}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
