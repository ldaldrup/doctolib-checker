# doctolib-checker

Monitor Doctolib appointment availability and receive optional Telegram alerts. Run a local web interface with a background worker, or use the command-line checker with a JSON configuration file.

The checker reports matching slots; booking happens on Doctolib. It does not reserve appointments.

- Monitor multiple booking URLs with insurance, telehealth, and date-window filters.
- Create, edit, pause, and resume jobs in the Jobs interface.
- Keep check results and alert history in SQLite when using the backend.
- Run checks without Telegram using the CLI's dry-run mode.
- Serve the web interface directly from Python, with no Node.js or frontend build required.

[Installation](#installation) · [Web interface](#web-interface) · [Command line](#command-line) · [Booking URLs](#booking-urls) · [Configuration](#configuration) · [Deployment](#deployment) · [Development](#development)

## Installation

Use Python 3.10 or newer; the Docker image uses Python 3.12. From the repository directory:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows, activate the environment with `.venv\Scripts\activate.bat` in Command Prompt or `.venv\Scripts\Activate.ps1` in PowerShell.

Choose a workflow below. The web interface uses environment variables and SQLite; the CLI uses `config.json`. They share the checking code, but do not share job configuration or stored state.

## Web interface

Start the API, availability worker and notification dispatcher in **three separate terminals**, with the virtual environment activated in each:

```bash
# Terminal 1: API and web interface
python -m app.api.main
```

```bash
# Terminal 2: background availability checks
python -m app.worker.main
```

```bash
# Terminal 3: independent Telegram delivery
python -m app.dispatcher
```

Run all three from the repository directory so they use the same default database, `./data/checker.sqlite3`. For a custom location, set the same `DATABASE_PATH` in all three terminals. Use compatible application versions for every writer.

1. Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/).
2. Create a job with one or more [booking URLs](#booking-urls) and your search filters.
3. Check the worker heartbeat and the job's latest results.
4. Use Settings to adjust the default job interval and request spacing.

A new database starts empty. The worker must remain running to perform checks. Results are historical observations: a detected appointment may no longer be available when you open its booking link. Partial failures remain visible alongside successful results.

### Enable Telegram alerts

Create a bot with [BotFather](https://t.me/BotFather), start a conversation with it, and obtain the destination chat ID. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the environment of **all three processes**, then restart them. The dispatcher performs sends; the API and worker use the configuration to expose and enqueue Telegram preferences. Enable Telegram for the jobs you want to notify.

The timezone, polling minimum, and Telegram credentials are server configuration; they cannot be edited in the web interface. Keep credentials out of job records and source control.

### API

The API lives under `/api/v1` and supports job/target management, URL validation, worker status, check history, alert history, and global settings. `/healthz` provides an internal health check.

Polling intervals have a server-enforced minimum of at least **300 seconds**. The same minimum applies to edits, resume, and check-now requests, so a requested check may be queued for later. Outbound Doctolib requests share a spacing gate. A 15-day horizon includes today and the next 14 calendar dates.

<details>
<summary>Alert delivery behavior</summary>

An alert is sent once for an earliest slot while that slot remains the earliest available. A confirmed disappearance or change of earliest slot starts a new alert episode; a higher slot count with the same earliest slot does not.

Failed Telegram sends can be retried while the target is active, the slot is in the future, and a confirming check is no older than one job interval. A newer no-availability result, changed earliest slot, removed target, or expired slot cancels a pending alert. Errors do not reset an episode. Pausing a job or disabling Telegram suspends delivery until a fresh-enough check permits it.

Availability checks queue alerts without sending. The independent serial dispatcher makes one bounded attempt per turn, with no in-request retry sleeps. It retries known connection-establishment failures, temporary rejections and rate limits with at least 5 seconds of exponential backoff, capped at 15 minutes. Retry-After is bounded by the same cap. Each recovery epoch allows at most 5 network attempts over 24 hours; confirmation freshness and slot expiry can stop delivery sooner. `attempt_count` is cumulative; `delivery_epoch_attempts` tracks the current recovery budget.

`/api/v1/alerts` preserves pending/failed/sent/cancelled status and adds `delivery_state`: ready, retry, action_required, exhausted or uncertain (sent delivery has state sent). `/api/v1/status` and Jobs/Settings notices show dispatcher health and backlog separately from availability worker health. Credential/recipient rejection needs repair and explicit recovery. Lost acknowledgements, read timeouts, interrupted started sends and unknown responses become uncertain and cannot retry automatically. Cancelled alerts remain visible. Telegram acceptance followed by a crash cannot be made exactly-once.

</details>

### Dispatcher operation and recovery

Run a single dispatch turn with `python -m app.dispatcher --once`. Inspect alert IDs and delivery states using `/api/v1/alerts`; the CLI prints safe recovery outcomes and never provider response bodies or credentials.

After repairing credentials or the recipient, explicitly reevaluate an action-required/exhausted alert:

```bash
python -m app.dispatcher --recover ALERT_UUID
```

An uncertain send may already have reached Telegram. Only deliberately accepting possible duplicate delivery allows its recovery:

```bash
python -m app.dispatcher --recover ALERT_UUID --acknowledge-duplicate-risk
```

Recovery never replays sent work. It requires an active job/target, enabled Telegram, matching current revision, future slot and fresh successful confirmation. If evidence is stale, let the availability worker reconfirm it before recovery. Recovery preserves lifetime attempt counts and starts a new bounded epoch. It does not send directly.

For an upgrade from inline delivery, stop every old API/worker writer, create and rehearse a protected backup, migrate with compatible schema-5 code, and start the API, availability worker and one dispatcher against the same DB. Do not run old inline senders alongside the dispatcher. Legacy pending and failed alerts become uncertain because an earlier send may have been accepted without a stored acknowledgement; fresh confirmation alone cannot resolve that ambiguity. Review them before acknowledged recovery. Migration preserves their status, IDs and history.

The default sender uses connect/read timeouts of 3/7 seconds and a 15-second overall transport deadline. A short-lived network child enforces that deadline independently, with bounded cleanup; the dispatcher remains serial. Shut down every process for maintenance or restore. Rollback restores the pre-upgrade database and matching earlier code after stopping writers, never an in-place schema downgrade.


## Command line

Copy the example configuration:

```bash
cp config.json.example config.json
```

On Windows, use `copy config.json.example config.json`.

Edit `config.json`:

1. Replace the `urls` example with your own [booking URLs](#booking-urls).
2. Choose your polling interval and search horizon. Start with at least 300 seconds between cycles and a short horizon such as 15 days; the example currently searches 365 days.
3. Add `telegram.bot_token` and `telegram.chat_id` for alerts, or use `--dry-run` to skip Telegram sends.

Try one cycle first:

```bash
python checker.py --once --dry-run
```

| Command | Behavior |
| --- | --- |
| `python checker.py` | Check continuously and send configured notifications. |
| `python checker.py --once` | Run one check cycle, then exit. |
| `python checker.py --dry-run` | Check continuously without sending Telegram messages. |
| `python checker.py --once --dry-run` | Run one cycle without Telegram messages. |
| `python quick_check.py` | Inspect the first configured URL and save metadata/API output under `temp/`. |

Stop the continuous checker with **Ctrl+C**. Dry-run still contacts Doctolib. The diagnostic output from `quick_check.py` may contain practice and practitioner details; keep it local.

## Booking URLs

1. Open Doctolib and choose a practitioner or practice.
2. Select the appointment type and continue to the availability view.
3. Copy the complete URL from the browser, including its query parameters.
4. Add it to a web job or the CLI's `urls` array.

Supported hosts are `doctolib.de`, `www.doctolib.de`, `doctolib.fr`, and `www.doctolib.fr`, over HTTPS. The URL must contain `/booking/availabilities`, a practice ID (`placeId`, `pid`, or `practice_id`), and an appointment motive ID (`motiveIds[]`, `motiveIds`, or `visit_motive_ids`). Preserve other parameters copied from the booking flow, such as `specialityId` and `practitionerId`.

Example shape, using placeholders rather than a real practice:

```text
https://www.doctolib.de/EXAMPLE-PATH/booking/availabilities?placeId=practice-XXXXX&motiveIds%5B%5D=XXXXXXX
```

Replace the entire example with a URL from your own booking flow. A profile page or search-results URL is not an availability URL.

## Configuration

### Backend environment

The API, worker and dispatcher read process environment variables. `.env.example` documents the container settings; the application does **not** automatically load a `.env` file. Its container-oriented `API_HOST=0.0.0.0` and `DATABASE_PATH=/data/checker.sqlite3` differ from the local defaults below.

| Variable | Local default | Purpose |
| --- | --- | --- |
| `DATABASE_PATH` | `./data/checker.sqlite3` | Shared SQLite file; use the same path for API, worker and dispatcher. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Empty | Set both to enable Telegram delivery. |
| `DEFAULT_TIMEZONE` | `Europe/Berlin` | Default timezone for new jobs. |
| `MINIMUM_POLL_INTERVAL_SECONDS` | `300` | Polling floor; values below 300 are clamped. |
| `REQUEST_SPACING_SECONDS` | `3` | Minimum spacing between requests; values below 3 are clamped. |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `8000` | API listening address and port. |
| `DOCTOLIB_PROFILE` | `safari2601` | Supported availability transport profile; no browser runs. |
| `DOCTOLIB_PAGE_DAYS` | `15` | Days per availability page, from 1 to 15. |
| `USER_AGENT` | `DoctolibChecker/2.0` | Metadata request header; does not override the availability profile. |
| `LOG_LEVEL` | `INFO` | Logging level. |
| `WORKER_TICK_SECONDS` | `2` | Worker scheduler tick, with a minimum of 0.5 seconds. |

### CLI configuration

`config.json.example` is the starting point for the CLI. `config.json`, runtime data, logs, and `.env` files are ignored by Git.

| Setting | Purpose |
| --- | --- |
| `urls` | Booking availability URLs to monitor. |
| `telegram.bot_token`, `telegram.chat_id` | Notification credentials. |
| `telegram.silent` | Send notifications without sound/vibration. |
| `polling.check_interval_seconds` | Time between cycles; at least 300 seconds. |
| `polling.delay_between_urls_seconds` | Request spacing; at least 3 seconds. |
| `polling.upcoming_days` | Calendar dates to search, including today. |
| `polling.insurance_sector` | `public` or `private`. |
| `polling.telehealth` | Include telehealth appointments. |
| `polling.page_days` | Days per availability page, from 1 to 15; older `slot_limit` is a fallback. |
| `doctolib_profile` | Availability transport profile; currently `safari2601`. |
| `user_agent` | Metadata request header. |
| `dry_run` | Skip Telegram API calls; also available as `--dry-run`. |
| `ui.terminal_table`, `ui.show_full_names` | Terminal presentation options. |

<details>
<summary>CLI message templates and heartbeat settings</summary>

Templates live under `messages`. Each message supports `silent`; slot messages can also specify `effect.enabled` and `effect.id`. The example includes a disabled Telegram effect. See the [Telegram effect ID list](https://gist.github.com/wiz0u/2a6d40c8f635687be363d72251a264da) for alternatives.

| Message | Template placeholders |
| --- | --- |
| `startup` | `{start_time}`, `{doctor_count}`, `{practice_count}`, `{practitioner_list}`, `{interval_mins}`, `{days}`, `{insurance_sector}` |
| `shutdown` | Static shutdown text. |
| `slot_found`, `far_slot_found` | `{total}`, `{practitioner}`, `{practice}`, `{first_date}`, `{booking_url}` |
| `summary` | `{uptime}`, `{total_cycles}`, `{total_hits}`, `{total_errors}`, `{next_check_in}`, `{last_slot_line}` |

Set `messages.summary.enabled` to enable heartbeat messages. `interval_seconds` sends by elapsed time; `every_x_cycles` sends by cycle count. Set either to `0` to disable that trigger. Summary messages are silent by default.

`ui.colorblind_friendly` is reserved for future use.

</details>

## Deployment

Keep the process or host awake and connected to the internet. For the backend, run the API, availability worker and dispatcher with the same environment and a persistent, writable SQLite location. The Dockerfile runs as UID/GID `10001:10001`; a mounted data directory must be writable by that user.

The same built image supports three commands: `python -m app.api.main`, `python -m app.worker.main`, and `python -m app.dispatcher`. Set those as separate service/container commands with the same writable `/data` mount and `DATABASE_PATH`; only the API needs proxy ingress. No dispatcher port is needed. The Dockerfile API default remains unchanged. Configure the supervisor to restart each process independently and stop all three before restore.

**The application does not implement authentication.** For remote access, protect the complete origin with an authenticated reverse proxy: UI, assets, and API must share the same gate. Keep API container ports unpublished and use `/healthz` internally. Set `API_HOST=0.0.0.0` inside a container when the proxy needs to reach it.

Only `app/web/` is served as static content. Repository files, configuration, tests, and databases are outside the served directory. UI and API share an origin, with no separate frontend server or CDN. Static assets revalidate after updates; API reads are uncached.

## Troubleshooting and limitations

| Symptom | What to check |
| --- | --- |
| Jobs do not run | Start the worker; verify its heartbeat and that all three processes use the same database. |
| No Telegram alerts | Start the dispatcher, set both credentials for all three processes, enable Telegram on the job, and inspect delivery state in alert history. CLI dry-run suppresses sends. |
| URL rejected | Copy the final availability URL, preserving practice and motive parameters. |
| Database permission error | Use a writable directory; for Docker bind mounts, account for UID/GID `10001:10001`. |
| Blocked requests or transient failures | Keep conservative polling/request spacing and inspect reported errors. Doctolib can change its API or anti-bot behavior. |

The API does not book appointments or deliver email/webhooks. Availability can change between a check and opening the booking page. The web interface currently exposes Jobs and Settings; check and alert history are also available through the API.

This is an unofficial personal utility, provided as-is. It is not endorsed by Doctolib, and use may violate its terms of service or lead to access restrictions. Maintenance and compatibility are not guaranteed.

## Development

Install development dependencies and run the offline tests:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

| Path | Responsibility |
| --- | --- |
| `checker.py`, `app/runner.py`, `app/loop.py` | CLI entrypoint and polling loop. |
| `app/doctolib.py`, `app/models.py` | URL validation, metadata, and availability checks. |
| `app/notifications.py` | Telegram delivery and formatting. |
| `app/api/`, `app/services/` | API routes, validation, and job operations. |
| `app/storage/`, `app/worker/` | SQLite persistence and scheduled checks. |
| `app/dispatcher.py`, `app/services/delivery.py` | Independent durable notification dispatch and explicit recovery. |
| `app/web/` | Native HTML/CSS/JavaScript interface. |
| `tests/` | Offline checker, backend, and UI verification. |

<details>
<summary>Offline browser verification</summary>

Create a fresh temporary directory, then start the fixture harness:

```bash
PYTHONPATH=. python tests/ui_harness.py --directory <temporary-directory> --port 9376
```

Open `http://127.0.0.1:9376/` for the connected journey, `/__test/contracts` for JavaScript contracts, or `/__test/responsive` for fixed-width layout checks. Only this harness substitutes fixture transports; its database and test routes are separate from production.

The directory's `control.json` accepts boolean fault switches: `reads_fail`, `writes_fail`, `auth`, and `lose_create_response`. Use `{}` to recover. Stop the harness and remove its temporary directory after testing.

</details>

## Acknowledgements

Inspired by [seh-len/doctolib](https://github.com/seh-len/doctolib) and [timoles/Doctolib-Userfriendly-Appointment-Tracker](https://github.com/timoles/Doctolib-Userfriendly-Appointment-Tracker). Developed with assistance from AI tools.

## SQLite backup and restore verification

Create a backup of an **existing backend SQLite database** with the online backup command. The API/worker/dispatcher may keep writing during backup; SQLite supplies a consistent snapshot including committed WAL data. The destination parent must already exist in a trusted, writable location. The command never replaces an existing destination, including a symlink or hard link, and fails safely if the source is missing or invalid.

```bash
python -m app.admin backup \
  --source ./data/checker.sqlite3 \
  --destination /protected-backups/checker-before-upgrade.zip
```

The single ZIP bundle contains `checker.sqlite3` and `manifest.json`. It is published atomically with permissions `0600`; temporary files are private and removed on failure. The manifest records UTC creation time, the snapshot's schema version, a SHA-256 checksum and the backup tool's checkout revision when available and application files are clean (`null` otherwise). That revision does **not** establish the version of the running database writer. The checksum detects accidental corruption; it is not a signature. Backups contain private appointment/job/history data and are not encrypted: protect their directory and copies. Environment Telegram credentials, service configuration and the legacy CLI's separate JSON state are not included.

Backup has a 30-second online-copy deadline; use `--timeout 120` for a deliberately longer deadline. Validation/archive creation takes additional time. A constantly busy database may need a longer deadline or a maintenance window. This command requires a filesystem supporting local hard links for no-overwrite publication; it does not fall back to an overwriting rename or upload to remote storage.

Verify and rehearse restore in a **new disposable directory**, leaving the archive untouched:

```bash
python -m app.admin verify \
  --archive /protected-backups/checker-before-upgrade.zip \
  --work-directory /private/tmp/checker-restore-rehearsal
```

The parent directory must exist; the work directory must not. Verification checks the checksum, SQLite integrity, foreign keys, manifest/schema agreement and required checker tables/columns for supported versions **before** initialization can create tables. It retains an extracted original snapshot as `checker.sqlite3`, copies it to `restored.sqlite3`, migrates only that second copy with the current application, and exercises representative job/target/history/alert/status repository reads plus the real `/healthz` ASGI endpoint without opening a server/socket or starting a worker. Explicit offline settings disable Telegram and upstream access; ambient `DATABASE_PATH`/Telegram environment values are not used. Success prints safe aggregate JSON with backup/restored schema versions and health status. Failure removes only the disposable directory newly created by this invocation; it never removes an existing directory. Keep the successful rehearsal directory private or remove it deliberately after inspection.

Current code can rehearse schema versions 1, 2, 3, 4 and 5. A newer unsupported schema is refused before attempting migration. `verify --integrity-only` checks an archive without migration or API health, allowing archival integrity checks independently of application compatibility. Re-run rehearsal with the intended application revision before each upgrade; passing health proves DB access, not upstream availability or delivery.

For an **actual offline restore**, stop every API/worker writer first. Preserve the current database using a separate backup, choose an application version compatible with the archive, and rehearse verification into a new private location. With writers stopped, promote the verified `restored.sqlite3` to a **new database path**, update all process configurations to that same path, and start the compatible application. Never overwrite a live database or combine restored data with old `-wal`/`-shm` files; do not start the worker during rehearsal. Check jobs/settings/targets/history and internal health before intentionally enabling real checks/notifications. Restored in-flight work and pending alerts require review because provider acceptance may have occurred after the snapshot; replay can duplicate an external notification. Record the cutover and keep the prior path available until rollback is no longer needed.

Rollback restores the matching earlier database **and** application version after stopping writers. It cannot retain changes made after that backup and must not downgrade a newer schema in place. These instructions do not create a production backup schedule or authorize a live restore/deployment. Future stored notification secrets will also require their external encryption key; current backend credentials remain separate environment configuration.

## Improvement work and branch policy

Completed improvements are consolidated into `master`. Continue the numbered [implementation plans](implementation-plans/README.md) sequentially on the single shared `feat/improvements` branch, starting from the latest `master`. Reuse that branch for every remaining part; do not create per-part, agent, review or auxiliary branches. Merge completed, reviewed work into `master`, then bring `feat/improvements` forward before continuing.

Parts 01–03 are complete; the [part 03 handoff](implementation-plans/03-completion-handoff.md) records verification and rollout requirements. Old feature branches are retired after their work is verified as included in `master`. Commits and branch merges do not deploy or authorize production migrations.
