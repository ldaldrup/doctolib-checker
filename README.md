# doctolib-checker

Monitor Doctolib appointment availability through a web app or CLI. The web app sends Telegram, ntfy and HTTPS webhook alerts. The checker never books appointments.

## Run the web app

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Run each process in a separate terminal with the virtual environment activated:

```bash
python -m app.api.main       # UI and API
python -m app.worker.main    # appointment checks
python -m app.dispatcher     # Telegram delivery
```

Open **http://127.0.0.1:8000**. In **Jobs**, add an availability URL and choose your search options.

All three processes must use the same database and compatible application version. The database defaults to `./data/checker.sqlite3`; override it with `DATABASE_PATH`.

## Telegram alerts

1. Create a bot with [BotFather](https://t.me/BotFather) and obtain the destination chat ID.
2. Generate an encryption key:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

3. Set that key as `NOTIFICATION_SECRET_KEY` in both API and dispatcher environments, then restart those processes.
4. In **Settings**, save a named Telegram destination and send a test. Select destinations on each job.

Keep the key outside source control and back it up separately from SQLite. Losing it makes saved credentials unusable. Tests send synthetic messages; previews send nothing.

Existing environment credentials can be imported once through **Import legacy Telegram**. Startup does not import them automatically.

For ntfy or generic webhooks, choose the channel type in **Settings**, save its HTTPS URL and optional bearer/basic credentials, then select it on a job. ntfy uses a full topic URL and priority; generic webhooks POST fixed versioned JSON. A successful send means endpoint acceptance, not device receipt.

Private destinations require exact server-side `WEBHOOK_PRIVATE_ALLOWLIST` entries; see [.env.example](.env.example). Loopback and metadata addresses remain blocked. Endpoint URLs and auth use the same encryption key as Telegram.

## Availability URLs

Copy the complete `/booking/availabilities` URL from your Doctolib booking flow, including its query string. It must use HTTPS on `doctolib.de` or `doctolib.fr` (including `www`) and include practice and appointment motive IDs.

```text
https://www.doctolib.de/example/booking/availabilities?placeId=practice-XXXXX&motiveIds%5B%5D=XXXXXXX
```

Keep real booking URLs and credentials out of source control.

## Run the CLI

The CLI uses a separate JSON configuration, not the web app database:

```bash
cp config.json.example config.json
# Edit config.json: replace the example URL and Telegram credentials.
python checker.py --once --dry-run  # check once without sending
python checker.py                  # continuous checks and alerts
```

Use `--once` for one cycle or `--dry-run` to suppress sends. Dry-run still contacts Doctolib. Keep `config.json` and `quick_check.py` output out of source control.

## Operating notes

- Scheduled polling has a 300-second minimum; Doctolib requests are spaced at least 3 seconds apart.
- **Check now** and active-job search edits allow one extra check per job per 60 seconds. Checking a paused job can notify its selected channels and leaves it paused.
- Each availability event keeps its original destinations. Adding a destination later does not replay it. Destinations retry independently.
- Disabling, removing or changing the delivery destination cancels unsent work. Temporary failures retry automatically within limits; action-required, exhausted or uncertain delivery needs explicit recovery. Uncertain sends may have arrived already.

Recover an eligible delivery with `python -m app.dispatcher --recover ALERT_ID`. For an uncertain send, also pass `--acknowledge-duplicate-risk`; this may send a duplicate.

The backend reads environment variables; it does **not** load `.env` automatically. See [.env.example](.env.example) for settings. Its `/data` path and `API_HOST=0.0.0.0` are container examples, not local defaults.

For Docker, run API, worker and dispatcher as separate services sharing a writable data directory. The image uses UID/GID `10001:10001`; set `API_HOST=0.0.0.0` for container access. Protect exposed UI and API routes with an authenticated reverse proxy—the app has no built-in authentication.

Create a consistent database backup and verify it in a new disposable directory:

```bash
python -m app.admin backup --source ./data/checker.sqlite3 --destination ./backup.zip
python -m app.admin verify --archive ./backup.zip --work-directory ./restore-check
```

Backups exclude the encryption key. Back up before upgrading; database migrations may prevent older versions from opening the upgraded database.

Availability can change before you book. This unofficial project may be affected by Doctolib changes or access restrictions. Email is not supported yet.
