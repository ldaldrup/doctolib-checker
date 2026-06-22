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
  - **URLs:** Add the exact Doctolib booking URLs you wish to monitor.
  - **Polling:** Adjust `polling.check_interval_seconds` (recommended: 300+ seconds) to avoid rate limits.

## Configuration (`config.json`)

The script relies on a `config.json` file in the root directory. Copy `config.json.example` as your starting point and adjust the parameters below:

### Telegram Settings

- `telegram.bot_token` (String): Your Telegram bot token from BotFather.
- `telegram.chat_id` (String): The numerical ID of the chat/user/group to receive notifications.
- `telegram.silent` (Boolean, optional): If `true`, all notifications are sent silently (no sound/vibration). Defaults to `false`. Can be overridden per message.

### Polling & Search

- `polling.check_interval_seconds` (Integer): Wait time in seconds between check cycles. **Recommended: 300+ seconds (5+ minutes)** to avoid Doctolib's rate-limiting and IP bans.
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

- `urls` (Array of Strings): Exact Doctolib booking URLs to monitor.
  - **Important:** Must include all query parameters (`specialityId`, `motiveIds`, `practitionerId`, etc.). Copy from the final booking step in your browser. A profile page link alone will not work.

## Usage

- **Windows Shortcut:** Simply double-click `run.bat`. It will activate your virtual environment (if one exists) and start the polling loop.
- **Command Line:** Run the script manually from your terminal:
  ```bash
  python checker.py
  ```
- **Single Check:** To run one check cycle without starting the continuous loop:
  ```bash
  python checker.py --once
  ```
- **Dry Run:** To test without sending Telegram messages:
  ```bash
  python checker.py --dry-run
  ```
- **Quick Check:** To fetch raw API data for the first URL in your config (saved to `temp/`):
  ```bash
  python quick_check.py
  ```

## Limitations & Considerations

- **Rate Limiting:** Doctolib uses anti-bot measures. Do not set your polling intervals too aggressively. Keep the interval to at least 5 minutes to minimize the risk of a temporary IP block.
- **URL Accuracy:** The URLs in your `config.json` must be exact and contain the correct query parameters (`specialityId`, `motiveIds`, `practitionerId`, etc.) for the script to locate availabilities. Copy them directly from the final booking step in your browser.
- **Always-On Requirement:** Because this runs locally, your computer must remain powered on, awake, and connected to the internet for the script to work.

## Project Status & Development

This tool is a personal utility and is provided entirely **"as-is"**. There is no planned roadmap, and active maintenance, feature requests, or bug fixes are not guaranteed. Feel free to fork the repository to modify it for your own needs.

*Note: This project was primarily developed with the assistance of AI tools. While it gets the job done for its specific use case, the internal logic and structure may bypass traditional software engineering patterns.*
