# doctolib-checker

A lightweight Doctolib appointment poller for a fixed set of booking URLs (e.g., multiple surgeons at one clinic). It monitors availability and sends Telegram notifications when new slots appear. 

Designed to run locally as a continuous background process.

This project draws inspiration from [seh-len/doctolib](https://github.com/seh-len/doctolib) and [timoles/Doctolib-Userfriendly-Appointment-Tracker](https://github.com/timoles/Doctolib-Userfriendly-Appointment-Tracker).

## ⚠️ Disclaimer

**This tool is not officially endorsed or allowed by DoctoLib.** Using this tool to access DoctoLib's services may violate their terms of service. Use at your own risk. The author is not responsible for any consequences, account bans, IP blocks, or other issues that may result from using this tool. By using this tool, you assume full responsibility for any and all consequences of how it interacts with DoctoLib's API and services.

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
├── models.py          # Shared dataclasses (BookingMeta, SessionStats)
├── state.py           # State persistence (state.json)
├── doctolib.py        # Doctolib URL parsing and availability logic
├── notifications.py   # Telegram dispatch and HTML-to-terminal formatting
├── loop.py            # Per-cycle execution, summary dispatch, countdown
└── runner.py          # Main orchestration (CLI, preflight, polling loop)
```

### Architecture Overview

The application follows a layered architecture with clear module boundaries:

- **`checker.py`** — Thin entrypoint wrapper that delegates to the modular runtime
- **`runner.py`** — CLI argument parsing, config initialization, preflight verification, and loop orchestration
- **`loop.py`** — Per-cycle execution logic, summary dispatch, and countdown pacing
- **`doctolib.py`** — URL parsing, metadata resolution, and slot fetching via shared requests session
- **`notifications.py`** — Telegram message dispatch and HTML-to-terminal text conversion
- **`config.py`** — Centralized configuration with sensible defaults and validation
- **`state.py`** — Persistent state tracking across runs (notification history, cycle counts)

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
- `polling.upcoming_days` (Integer): How many days ahead to search for appointments (e.g., `15` = next 15 days).
- `polling.insurance_sector` (String): Filter by insurance type: `"public"` or `"private"`. Defaults to `"public"`.
- `polling.telehealth` (Boolean): Include remote/telehealth appointments. Defaults to `false`.
- `polling.slot_limit` (Integer): Maximum slots per API call. Defaults to `15`. Note: the script reports the total count from the API, not limited by this.

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
   - The URL should look similar to: `https://www.doctolib.de/praxis/berlin/hausarztrettungsstelle-adlershof/booking/availabilities?specialityId=1286&telehealth=false&placeId=practice-656116&insuranceSectorEnabled=true&insuranceSector=public&motiveIds%5B%5D=13620100&pid=practice-656116&insurance_sector=public&source=profile`

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

## Limitations & Considerations

- **Rate Limiting:** Doctolib may use anti-bot anti-ddos measures. Do not set your polling intervals too aggressively. Keep the interval to at least 5 minutes to minimize the risk of a temporary IP block.
- **URL Accuracy:** The URLs in your `config.json` must be exact and contain the correct query parameters (`specialityId`, `motiveIds`, `practitionerId`, etc.) for the script to locate availabilities. Copy them directly from the final booking step in your browser.
- **Always-On Requirement:** Because this runs locally, your computer must remain powered on, awake, and connected to the internet for the script to work.

## Project Status & Development

This tool is a personal utility and is provided entirely **"as-is"**. There is no planned roadmap, and active maintenance, feature requests, or bug fixes are not guaranteed. Feel free to fork the repository to modify it for your own needs.

The codebase has been refactored from a monolithic script into a modular Python package (`app/`) to improve maintainability and separation of concerns. The CLI interface and configuration schema remain unchanged, so existing setups continue to work without modification.

*Note: This project was developed with the assistance of AI tools. The modular architecture follows conventional Python packaging patterns for better long-term maintainability.*
