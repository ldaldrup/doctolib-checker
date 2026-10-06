# Doctolib Checker

Checks appointment availability and sends alerts. It never books appointments.

## Web app

Requires Python 3.10+:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Run these in separate terminals, using the same code and database:

```bash
python -m app.api.main       # UI and API
python -m app.worker.main    # checks
python -m app.dispatcher     # alerts
```

Open <http://127.0.0.1:8000>. The default database is `./data/checker.sqlite3`; set `DATABASE_PATH` in each process to change it. Generate a Fernet key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and set the same `NOTIFICATION_SECRET_KEY` in all three processes before startup. `.env` is not loaded automatically; see [.env.example](.env.example).

## Searches and alerts

Enter a full HTTPS Doctolib `/booking/availabilities` URL. Configure and test Telegram, ntfy, HTTPS webhook or email channels in **Settings**, then select them per job. Email uses one SMTP transport and one recipient per named channel. A successful test means the provider accepted the message.

Webhooks require HTTPS. SMTP uses STARTTLS on 587 or implicit TLS on 465. Private destinations and nonstandard HTTPS/SMTP ports need an exact `WEBHOOK_PRIVATE_ALLOWLIST` entry. Checks run at least 5 minutes apart, with 3 seconds between Doctolib requests. **Check now** and search edits allow one extra check per job per minute, including while paused; a manual check can notify and leaves the job paused.

## Legacy CLI

The CLI uses a separate `config.json`. Copy `config.json.example`, add your Doctolib URL and optional Telegram credentials, then run:

```bash
cp config.json.example config.json
python checker.py --once --dry-run
```

Dry run suppresses alerts but still contacts Doctolib. Keep `config.json` private.

## Security and backup

See [retention, history export and recovery](OPERATIONS.md) for operator commands and key custody.

The web app has no authentication and binds to localhost; put it behind an authenticated reverse proxy before exposing it. `.env.example` sets `API_HOST=0.0.0.0` for containers. Back up the database and notification key separately:

```bash
python -m app.admin backup --source ./data/checker.sqlite3 --destination ./backup.zip
python -m app.admin verify --archive ./backup.zip --work-directory ./restore-check
```

## Development

Use the shared `feat/improvements` branch. Merge completed work into local `master`; create no other branches.
