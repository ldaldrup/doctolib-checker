# doctolib-checker

A lightweight Doctolib appointment checker. It can run from the existing CLI or as a job-based backend with an HTTP API, a persistent SQLite database, and a separate polling worker. It reports matching appointment availability and can send Telegram alerts; it does not reserve or book appointments.

Designed to run locally as a continuous background process.

This project draws inspiration from [seh-len/doctolib](https://github.com/seh-len/doctolib) and [timoles/Doctolib-Userfriendly-Appointment-Tracker](https://github.com/timoles/Doctolib-Userfriendly-Appointment-Tracker).

## ⚠️ Disclaimer

**This tool is not officially endorsed or explicitly allowed by DoctoLib.** Using this tool to access DoctoLib's services may violate their terms of service. Use at your own risk. The author is not responsible for any consequences, account bans, IP blocks, or other issues that may result from using this tool. By using this tool, you assume full responsibility for any and all consequences of how it interacts with DoctoLib's API and services.

## Setup

1. **Install dependencies:** Ensure you have Python installed, then install the required packages:
  ```bash
   pip install -r requirements.txt
  ```
2. **Telegram Setup:** 
  - Create a bot with [BotFather](https://t.me/BotFather) to get your `bot_token`.
  - Start a conversation with your bot and get your `chat_id` (you can send any message to the bot, then use an API call or a service like [this](https://t.me/userinfobot) to find your ID).
3. **Configuration:** Copy `config.json.example` to `config.json` and populate it with your specific details:
  - **Telegram:** Add your `bot_token` and `chat_id` (found above).
  - **URLs:** Add the Doctolib appointment URLs you wish to monitor (see [Obtaining Doctolib URLs](#obtaining-doctolib-urls) below).
  - **Polling:** Adjust `polling.check_interval_seconds` (recommended: 300+ seconds) to avoid potential rate limits.

## Project Structure

The codebase is organized as a modular Python package under `app/`:

```
app/
├── __init__.py        # Package marker
├── config.py          # Config loading, defaults, and validation
├── logging_utils.py   # Logging setup and ANSI-aware formatting
├── models.py          # Shared booking and availability records
├── state.py           # CLI state persistence (state.json)
├── doctolib.py        # URL validation, metadata, and structured availability checks
├── notifications.py   # Telegram adapters and message formatting
├── loop.py            # Existing CLI polling cycle
├── runner.py          # Existing CLI orchestration
├── api/               # FastAPI app, routes, and request models
├── services/          # Job operations and check execution
├── storage/           # SQLite schema and repository operations
├── worker/            # Separate polling worker process
└── web/               # Native HTML/CSS/JavaScript Jobs and Settings interface
```

### Architecture Overview

The CLI and backend share the Doctolib checking code. The backend uses these boundaries:

- **`checker.py`** — Thin entrypoint wrapper that delegates to the modular runtime
- **`doctolib.py`** — URL validation, metadata resolution, date-window filtering, and slot fetching
- **`storage/`** — Durable jobs, targets, check runs/results, alert history, and shared request spacing
- **`worker/`** — Sequential due-job checks, result recording, and notification dispatch
- **`api/`** — Versioned job-control and reporting endpoints
- **`services/`** — Job validation and coordination between the checker and repository
- **`notifications.py`** — Telegram delivery with server-side credentials
- **`runner.py` and `loop.py`** — Existing command-line operation

This modular design improves maintainability while preserving the original CLI interface and behavior.

## Configuration (`config.json`)

The script relies on a `config.json` file in the root directory. Copy `config.json.example` as your starting point and adjust the parameters below:

### Telegram Settings

- `telegram.bot_token` (String): Your Telegram bot token from BotFather.
- `telegram.chat_id` (String): The numerical ID of the chat/user/group to receive notifications.
- `telegram.silent` (Boolean, optional): If `true`, all notifications are sent silently (no sound/vibration). Defaults to `false`. Can be overridden per message.

### Polling & Search

- `polling.check_interval_seconds` (Integer): Wait time in seconds between check cycles. **Recommended: 300+ seconds (5+ minutes)** to avoid potential rate-limiting or IP bans.
- `polling.delay_between_urls_seconds` (Integer): Pause between fetching URLs in a single cycle. **Recommended: 2–5 seconds.**
- `polling.upcoming_days` (Integer): Number of calendar dates to search, including today (e.g., `15` = today and the next 14 dates).
- `polling.insurance_sector` (String): Filter by insurance type: `"public"` or `"private"`. Defaults to `"public"`.
- `polling.telehealth` (Boolean): Include remote/telehealth appointments. Defaults to `false`.
- `polling.page_days` (Integer): Calendar days requested per availability page. Must be between `1` and `15`; defaults to `15`. Existing configs with `polling.slot_limit` use that value as a fallback.
- `doctolib_profile` (String): Browser profile used by the browserless availability transport. Currently supports `safari2601`.
- `user_agent` (String): Header used for booking metadata requests. Availability requests use the configured browser profile's matching headers and connection behavior.

### Messages

Message templates use placeholders and can be individually silenced:

#### Startup Message (`messages.startup`)
- `template` (String): Message on script start. Placeholders: `{start_time}`, `{doctor_count}`, `{practice_count}`, `{practitioner_list}`, `{interval_mins}`, `{days}`, `{insurance_sector}`.
- `silent` (Boolean, optional): If `true`, this specific message is silent. Defaults to `false`.

#### Shutdown Message (`messages.shutdown`)
- `template` (String): Message when script stops.
- `silent` (Boolean, optional): If `true`, this specific message is silent. Defaults to `false`.

#### Slot Found Message (`messages.slot_found`)
- `template` (String): Alert when slots are found. Placeholders: `{total}`, `{practitioner}`, `{practice}`, `{first_date}`, `{booking_url}`.
- `silent` (Boolean, optional): If `true`, this specific message is silent. Defaults to `false`.
- `effect` (Object, optional): Telegram notification effect.
  - `enabled` (Boolean): If `true`, plays a notification effect on Telegram. Defaults to `false`.
  - `id` (String): Telegram effect ID (e.g., `"5046509860389126442"` for fireworks. See [wiz0u/MessageEffectIds.txt](https://gist.github.com/wiz0u/2a6d40c8f635687be363d72251a264da) for a list of animated and non-animated message effects). 

#### Summary / Heartbeat Message (`messages.summary`)
- `enabled` (Boolean): If `true`, periodically sends a monitoring status update. Defaults to `false`.
- `interval_seconds` (Integer): Time-based interval in seconds (e.g., `3600` for hourly). Set to `0` to disable time-based sending. Defaults to `0`.
- `every_x_cycles` (Integer): Send summary every N polling cycles (e.g., `12` with 5-minute intervals ≈ hourly). Defaults to `0` (disabled).
- `template` (String): Message format. Placeholders: `{uptime}`, `{total_cycles}`, `{total_hits}`, `{total_errors}`, `{next_check_in}`, `{last_slot_line}`.
- `silent` (Boolean, optional): Summary messages are silent by default. Set to `false` to enable sound. Defaults to `true`.

### UI & Other

- `ui.terminal_table` (Boolean): Display results in a table format. Defaults to `false`.
- `ui.show_full_names` (Boolean): Show full practitioner names in terminal. Defaults to `true`.
- `ui.colorblind_friendly` (Boolean): Reserved for future use.
- `user_agent` (String): Browser User-Agent string. Generally do not change unless Doctolib blocks it.
- `dry_run` (Boolean): If `true`, runs checks but skips Telegram API calls. Useful for testing. Can also be set via `--dry-run` CLI flag. Defaults to `false`.

### Target URLs

- `urls` (Array of Strings): Doctolib booking page URLs to monitor.
  - Copy the URL from your browser's address bar when you're on the appointment availability page.

## Obtaining Doctolib URLs

To monitor appointments for a specific practitioner, follow these simple steps:

1. **Navigate to [doctolib.de](https://doctolib.de)** and search for your desired practitioner, specialty, or location.

2. **Select your practitioner and appointment type** from the search results.

3. **Navigate through the booking flow** until you reach the appointment availability view.

4. **Copy the URL from your browser's address bar** when you see the availability page (regardless of whether slots show "no appointments available" or not).
   - The URL should look similar to: `https://www.doctolib.de/EXAMPLE-PATH/booking/availabilities?placeId=practice-XXXXX&motiveIds%5B%5D=XXXXXXX`

5. **Paste the URL into your `config.json`** under the `urls` array.

**⚠️ Important:** The URL must contain the `/availabilities?` path and include query parameters such as:
- `specialityId` – The specialty ID
- `motiveIds[]` (or `motiveIds`) – The appointment type ID(s)
- `placeId` (or `pid`/`practice_id`) – The practice/clinic ID

If the URL is missing these parameters or doesn't contain `/availabilities?` in the path, the tool won't be able to fetch appointments correctly. If you're unsure, re-copy the URL from the address bar and verify it contains at least these three parameters.

That's it! The tool automatically parses the URL and monitors for available slots.

## Usage

- **Windows Shortcut:** Simply double-click `run.bat`. It will activate your virtual environment (if one exists) and start the polling loop.
- **Command Line:** Run the script manually from your terminal:
  ```bash
  python checker.py
  ```
  The CLI interface remains unchanged after the modular refactor — `checker.py` now delegates to the modular runtime under `app/`.
- **Single Check:** To run one check cycle without starting the continuous loop:
  ```bash
  python checker.py --once
  ```
- **Dry Run:** To test without sending Telegram messages:
  ```bash
  python checker.py --dry-run
  ```
- **Quick Check:** To verify the first URL in your config and save parsed Doctolib output to `temp/`:
  - parses the first configured booking URL
  - resolves metadata from Doctolib's `info.json`
  - fetches availability data from `availabilities.json`
  - prints an interpreted status such as IMMINENT SLOT, FAR SLOT, OUT OF WINDOW, or NO SLOTS
  - saves a JSON file containing both metadata and the Doctolib API response
  ```bash
  python quick_check.py
  ```

## Backend API and worker

The backend runs as two processes that share a SQLite database: the API controls jobs and reports status/history, and the worker checks due jobs and sends Telegram alerts. The API binds to `127.0.0.1` by default for local use. Set `API_HOST=0.0.0.0` only when it runs inside a container behind the configured authenticated reverse proxy.

Install the runtime dependencies, then start the API and worker in separate shells with the same environment and database path:

```bash
pip install -r requirements.txt
DATABASE_PATH=./data/checker.sqlite3 python -m app.api.main
DATABASE_PATH=./data/checker.sqlite3 python -m app.worker.main
```

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the environment of both processes to enable alerts. Keep those values out of job records and source control. The example container environment is in `.env.example`.

### Jobs and Settings interface

Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/) after starting the API. The API process serves the interface and its local assets, and the browser calls relative `/api/v1/` URLs on the same origin. No Node.js, frontend build, CDN, or separate static server is required. The worker must also run to check jobs; its heartbeat is shown in the interface.

Jobs and settings load exclusively from the configured database through the API. A new database starts empty. Create/edit/pause/resume/delete actions persist on the server, and API failures display errors rather than substitute records. Settings can save the default job interval and request spacing; the polling floor, default time zone, and Telegram configuration remain server configuration. Activity/Alerts pages and new notification channels are not part of this interface.

The served directory is only `app/web/`; repository files, configuration, tests, and database files are not public assets. Stable HTML, JavaScript, CSS, and font filenames are served with `Cache-Control: no-cache` and validators so browsers revalidate them after application updates. API reads use uncached requests. Unknown paths return 404, and hash navigation handles Jobs and Settings without a catch-all file rewrite.

For remote use, put the complete origin behind the authenticated reverse proxy: UI, assets, and API must share the same access gate. The application does not implement authentication itself. Keep API container ports unpublished, and use `/healthz` internally for health checks. The `deployment-repository` configuration prepares this routing; Komodo synchronization, builds, and deployments are deliberate user-owned operations.

Card detections are historical check results, not guaranteed live appointments. Partial failures remain visible alongside successful detections. The API has no search revision or creation idempotency key: results around external edits are marked uncertain, and an ambiguous creation response is reconciled before a deliberate retry is offered. Visible Jobs refresh every 30 seconds with bounded backoff; hidden pages stop polling.

For offline browser verification, install `requirements-dev.txt`, create a fresh temporary directory, and run `PYTHONPATH=. python tests/ui_harness.py --directory <temporary-directory> --port 9376`. Open `/` for the connected journey, `/__test/contracts` for native JavaScript contracts, or `/__test/responsive` for fixed-width layout checks. Only this harness substitutes fixture transports; its database is separate and its routes/files are absent from the production application/image. Stop the harness and remove only its temporary directory after testing. Fault switches in that directory's `control.json` are `reads_fail`, `writes_fail`, `auth`, and `lose_create_response` (boolean values); use `{}` to recover.

The versioned API is under `/api/v1`. It provides health and worker status, job and target management, URL validation, check history, alert history, and supported global settings. Poll intervals have a server-enforced minimum of at least 300 seconds, configurable through `MINIMUM_POLL_INTERVAL_SECONDS`. This minimum also applies to job edits, resume, and check-now requests; a check-now request may be queued for later. Outbound Doctolib requests share the configured spacing gate. A 15-day backend horizon includes today and the next 14 calendar dates. The API does not provide appointment booking, email, or webhook delivery.

An alert is sent once for an earliest slot while that slot remains the earliest available. A confirmed disappearance or change of earliest slot starts a new alert episode; an increased slot count with the same earliest slot does not. Failed Telegram sends remain in alert history for retry only while the target is active, the slot is in the future, and a confirming check is no older than one job interval. A newer no-availability result, changed earliest slot, removed target, or expired slot cancels a pending alert; errors do not reset an episode. Pausing a job or disabling Telegram suspends delivery until a fresh-enough check permits it. The `/api/v1/alerts` status can be `cancelled` for alerts that will not be retried. Telegram does not offer exactly-once delivery: if Telegram accepts a message and the worker stops before saving the success state, a later retry can send that alert again.

To run the offline API/worker and checker tests:

```bash
pip install -r requirements-dev.txt
python -m pytest
```

## Limitations & Considerations

- **Rate Limiting:** Doctolib may use anti-bot anti-ddos measures. Do not set your polling intervals too aggressively. Keep the interval to at least 5 minutes to minimize the risk of a temporary IP block.
- **URL Accuracy:** The URLs in your `config.json` must be exact and contain the correct query parameters (`specialityId`, `motiveIds`, `practitionerId`, etc.) for the script to locate availabilities. Copy them directly from the final booking step in your browser.
- **Always-On Requirement:** Because this runs locally, your computer must remain powered on, awake, and connected to the internet for the script to work.

## Project Status & Development

This tool is a personal utility and is provided entirely **"as-is"**. There is no planned roadmap, and active maintenance, feature requests, or bug fixes are not guaranteed. Feel free to fork the repository to modify it for your own needs.

The codebase has been refactored from a monolithic script into a modular Python package (`app/`) to improve maintainability and separation of concerns. The CLI interface and configuration schema remain unchanged, so existing setups continue to work without modification.

*Note: This project was developed with the assistance of AI tools. The modular architecture follows conventional Python packaging patterns for better long-term maintainability.*
