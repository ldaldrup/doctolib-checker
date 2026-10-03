# Doctolib Checker

Check Doctolib appointment availability and get notified when a search matches. The checker never books appointments.

## Run

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Open three terminals, activate `.venv` in each, then run one process per terminal:

- `python -m app.api.main` — web app and API
- `python -m app.worker.main` — appointment checks
- `python -m app.dispatcher` — notifications

Open <http://127.0.0.1:8000> and create a search in **Jobs**. All processes must use the same database and app version. The default database is `./data/checker.sqlite3`; set `DATABASE_PATH` to change it.

## Notifications

Add Telegram, ntfy, HTTPS webhook or email destinations in **Settings**, test them, then select them on a job. Email uses one shared SMTP transport and one recipient per named channel. A successful test means the service accepted the message; it does not confirm delivery to a device or inbox.

Set the same `NOTIFICATION_SECRET_KEY` in the API, worker and dispatcher environments. Generate one with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Back up the key separately; saved credentials cannot be read without it. The app does not load `.env` files. Legacy Telegram environment credentials require an explicit import in Settings.

Webhooks require HTTPS. SMTP requires verified TLS on port 587 (STARTTLS) or 465 (implicit TLS). Private destinations and other SMTP ports require an exact `WEBHOOK_PRIVATE_ALLOWLIST` entry; see [.env.example](.env.example).

## Searches

Use the full HTTPS `/booking/availabilities` URL, including its query string, practice ID and motive ID. Supported hosts are `doctolib.de` and `doctolib.fr`, including `www`.

Scheduled checks run no faster than every 300 seconds, with at least 3 seconds between Doctolib requests. **Check now** and edits to active search conditions allow one extra check per job per 60 seconds. A manual check can run on a paused job, notify its selected channels and leave it paused.

## CLI

The CLI uses a separate `config.json`. Copy `config.json.example` to `config.json` and add your booking URL and Telegram credentials, then run:

```bash
python checker.py
python checker.py --once --dry-run
```

`python checker.py` checks continuously. `--once` checks one cycle; `--dry-run` suppresses notifications but still contacts Doctolib. Keep `config.json` private.

## Backups and access

The app has no built-in authentication. Protect the UI and API with an authenticated reverse proxy before exposing them.

Back up the database and `NOTIFICATION_SECRET_KEY` before upgrading. Backups exclude the key, and migrations may prevent an older app version from opening the database.

```bash
python -m app.admin backup --source ./data/checker.sqlite3 --destination ./backup.zip
python -m app.admin verify --archive ./backup.zip --work-directory ./restore-check
```

## Development

Keep development on the shared `feat/improvements` branch. Merge completed work into `master`; do not create additional branches.
