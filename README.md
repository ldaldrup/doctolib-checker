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

Start the API and worker in **two separate terminals**, with the virtual environment activated in each:

```bash
# Terminal 1: API and web interface
python -m app.api.main
```

```bash
# Terminal 2: background checks and notifications
python -m app.worker.main
```

Run both from the repository directory so they use the same default database, `./data/checker.sqlite3`. For a custom location, set the same `DATABASE_PATH` in both terminals.

1. Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/).
2. Create a job with one or more [booking URLs](#booking-urls) and your search filters.
3. Check the worker heartbeat and the job's latest results.
4. Use Settings to adjust the default job interval and request spacing.

A new database starts empty. The worker must remain running to perform checks. Results are historical observations: a detected appointment may no longer be available when you open its booking link. Partial failures remain visible alongside successful results.

### Enable Telegram alerts

Create a bot with [BotFather](https://t.me/BotFather), start a conversation with it, and obtain the destination chat ID. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the environment of **both processes**, then restart them. Enable Telegram for the jobs you want to notify.

The timezone, polling minimum, and Telegram credentials are server configuration; they cannot be edited in the web interface. Keep credentials out of job records and source control.

### API

The API lives under `/api/v1` and supports job/target management, URL validation, worker status, check history, alert history, and global settings. `/healthz` provides an internal health check.

Polling intervals have a server-enforced minimum of at least **300 seconds**. The same minimum applies to edits, resume, and check-now requests, so a requested check may be queued for later. Outbound Doctolib requests share a spacing gate. A 15-day horizon includes today and the next 14 calendar dates.

<details>
<summary>Alert delivery behavior</summary>

An alert is sent once for an earliest slot while that slot remains the earliest available. A confirmed disappearance or change of earliest slot starts a new alert episode; a higher slot count with the same earliest slot does not.

Failed Telegram sends can be retried while the target is active, the slot is in the future, and a confirming check is no older than one job interval. A newer no-availability result, changed earliest slot, removed target, or expired slot cancels a pending alert. Errors do not reset an episode. Pausing a job or disabling Telegram suspends delivery until a fresh-enough check permits it.

Cancelled alerts remain visible in `/api/v1/alerts`. Telegram does not provide exactly-once delivery: if it accepts a message and the worker stops before recording success, a retry may send a duplicate.

</details>

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

The API and worker read process environment variables. `.env.example` documents the container settings; the application does **not** automatically load a `.env` file. Its container-oriented `API_HOST=0.0.0.0` and `DATABASE_PATH=/data/checker.sqlite3` differ from the local defaults below.

| Variable | Local default | Purpose |
| --- | --- | --- |
| `DATABASE_PATH` | `./data/checker.sqlite3` | Shared SQLite file; use the same path for API and worker. |
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

Keep the process or host awake and connected to the internet. For the backend, run the API and worker with the same environment and a persistent, writable SQLite location. The Dockerfile runs as UID/GID `10001:10001`; a mounted data directory must be writable by that user.

**The application does not implement authentication.** For remote access, protect the complete origin with an authenticated reverse proxy: UI, assets, and API must share the same gate. Keep API container ports unpublished and use `/healthz` internally. Set `API_HOST=0.0.0.0` inside a container when the proxy needs to reach it.

Only `app/web/` is served as static content. Repository files, configuration, tests, and databases are outside the served directory. UI and API share an origin, with no separate frontend server or CDN. Static assets revalidate after updates; API reads are uncached.

## Troubleshooting and limitations

| Symptom | What to check |
| --- | --- |
| Jobs do not run | Start the worker; verify its heartbeat and that both processes use the same database. |
| No Telegram alerts | Set both credentials for both processes, enable Telegram on the job, and check alert history. CLI dry-run suppresses sends. |
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
