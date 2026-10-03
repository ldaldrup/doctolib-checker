# doctolib-checker

Monitor Doctolib appointment availability. The checker reports matching slots and can send Telegram alerts. It never books or reserves appointments.

The project has two modes:

- **Web app:** jobs, history, saved Telegram destinations and a background checker.
- **CLI:** a separate JSON-configured checker for one-off or continuous terminal use.

## Web app

Install Python 3.10+ and the dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Run these three processes in separate terminals from the repository directory. They must use the same database and compatible application version:

```bash
python -m app.api.main       # web app: http://127.0.0.1:8000/
python -m app.worker.main    # scheduled checks
python -m app.dispatcher     # notification delivery
```

The default database is `./data/checker.sqlite3`. Set `DATABASE_PATH` to use another path.

Create a job in **Jobs**, paste one or more complete availability URLs, choose its search options, and select saved notification channels. The worker must be running for scheduled checks. **Check now** also works for paused jobs and leaves them paused.

### Telegram setup

1. Create a bot with [BotFather](https://t.me/BotFather) and obtain its chat ID.
2. Set one shared `NOTIFICATION_SECRET_KEY` for the API and dispatcher. Generate one with:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

3. Open **Settings**, add a named Telegram destination, enter the credentials, and press **Save**.
4. Select one or more destinations on each job.

`NOTIFICATION_SECRET_KEY` is a Fernet key. Keep it outside SQLite and source control, and back it up separately from the database. Without it, existing credentials stay stored but cannot be used; new credential saves and sends are blocked. The UI never stores credentials in browser storage and masks them in reads.

For an existing environment-based setup, use **Import legacy Telegram** once. It creates `Telegram1` and maps it to existing opted-in jobs. Startup never imports or overwrites saved settings.

Test sends use a synthetic message. An uncertain result remains visible and is never retried automatically. A preview never sends anything.

### Important behavior

- The normal polling minimum is 300 seconds. Doctolib requests share a spacing gate.
- Manual checks and active-job search edits allow one extra run per job per 60 seconds.
- A saved availability event records its original channel selection. Attaching a channel later does not replay the same event.
- Each destination has its own delivery state and retry budget.
- Disabling, clearing, deleting, detaching, or changing a destination cancels unsent work. In-flight work records its actual outcome.
- Failed or uncertain delivery requires explicit recovery. Sent work is never replayed automatically.

## Booking URLs

Copy the complete availability URL from Doctolib, including its query string. Supported hosts are `doctolib.de` and `doctolib.fr`, including `www`, over HTTPS. The URL must contain `/booking/availabilities`, a practice ID, and an appointment motive ID.

Example shape:

```text
https://www.doctolib.de/example/booking/availabilities?placeId=practice-XXXXX&motiveIds%5B%5D=XXXXXXX
```

Use a URL from your own booking flow. Do not commit real URLs to the repository.

## CLI

The CLI uses `config.json`, separate from the web app database:

```bash
cp config.json.example config.json
python checker.py --once --dry-run
```

Useful commands:

```bash
python checker.py                  # continuous checks and Telegram sends
python checker.py --once           # one cycle
python checker.py --dry-run        # continuous checks without sends
python checker.py --once --dry-run # one cycle without sends
python quick_check.py              # inspect the first configured URL
```

Keep `config.json` and `quick_check.py` output private. Dry-run still contacts Doctolib.

## Configuration

The backend reads environment variables. It does not load `.env` automatically. See [.env.example](.env.example).

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_PATH` | `./data/checker.sqlite3` | Shared backend database. |
| `NOTIFICATION_SECRET_KEY` | empty | Fernet key for saved Telegram credentials. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | empty | Optional source for one-time legacy import. |
| `DEFAULT_TIMEZONE` | `Europe/Berlin` | Timezone for new jobs. |
| `MINIMUM_POLL_INTERVAL_SECONDS` | `300` | Polling floor. Values below 300 are clamped. |
| `REQUEST_SPACING_SECONDS` | `3` | Minimum Doctolib request spacing. Values below 3 are clamped. |
| `DOCTOLIB_PROFILE` | `safari2601` | Availability transport profile. No browser runs. |
| `DOCTOLIB_PAGE_DAYS` | `15` | Availability page size, 1–15 days. |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `8000` | API bind address and port. |

Use the same `DATABASE_PATH` and `NOTIFICATION_SECRET_KEY` for the API and dispatcher. The worker needs the database path too.

## Backup and restore

Back up before schema changes or maintenance:

```bash
python -m app.admin backup \
  --source ./data/checker.sqlite3 \
  --destination /protected-backups/checker-before-upgrade.zip
```

Verify into a new disposable directory:

```bash
python -m app.admin verify \
  --archive /protected-backups/checker-before-upgrade.zip \
  --work-directory /private/tmp/checker-restore-rehearsal
```

Verification checks the archive, SQLite integrity, foreign keys, schema migration, representative reads, and `/healthz` without starting a server or contacting Doctolib or Telegram. Backups contain private job and appointment data and are not encrypted. Protect them and keep the matching `NOTIFICATION_SECRET_KEY` separately.

For a real restore, stop the API, worker, and dispatcher first. Restore the database and matching application version together. Never downgrade a newer schema in place or combine a restored database with old `-wal` or `-shm` files.

## Deployment

The Docker image runs as UID/GID `10001:10001`. Mount a writable data directory and run the API, worker, and dispatcher as separate services using the same database. Protect the API and UI with an authenticated reverse proxy; the application does not provide authentication.

## Development

Run the offline suite:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

The browser fixture harness is disposable and local-only:

```bash
PYTHONPATH=. python tests/ui_harness.py --directory /tmp/checker-ui --port 9376
```

Stop the harness and remove its temporary directory after testing.

Implementation plans and completion handoffs live in [implementation-plans](implementation-plans/README.md). Parts 01–06 are complete locally; parts 07–14 remain planned. Continue work on the single `feat/improvements` branch. Remote publication and production rollout require separate authorization.

## Limits

Availability can change before you open the booking page. The project is unofficial and may be affected by Doctolib changes or access restrictions. It does not send email or webhooks yet.
