# doctolib-checker

A lightweight Doctolib appointment poller for a fixed set of booking URLs (e.g., multiple surgeons at one clinic). It monitors availability and sends Telegram notifications when new slots appear. 

Designed to run locally as a continuous background process.

## Inspired By

This project draws inspiration from the following repositories:

- [seh-len/doctolib](https://github.com/seh-len/doctolib)
- [timoles/Doctolib-Userfriendly-Appointment-Tracker](https://github.com/timoles/Doctolib-Userfriendly-Appointment-Tracker)

## ⚠️ Disclaimer

**This tool is not officially endorsed or allowed by DoctoLib.** Using this tool to access DoctoLib's services may violate their terms of service. Use at your own risk. The author is not responsible for any consequences, account bans, IP blocks, or other issues that may result from using this tool. By using this tool, you assume full responsibility for any and all consequences of how it interacts with DoctoLib's API and services.

## Setup

1. **Install dependencies:** Ensure you have Python installed, then install the required packages:
  ```bash
   pip install -r requirements.txt
  ```
2. **Configuration:** Copy `config.example.json` to `config.json` and populate it with your specific details:
  - **Telegram:** Add your Bot Token and Chat ID.
  - **URLs:** Add the exact Doctolib booking URLs you wish to monitor.
  - **Timing:** Adjust `check_interval_seconds` (recommended: 300+ seconds) to avoid rate limits.
## Configuration (`config.json`)

The script relies on a `config.json` file in the root directory to manage its behavior. Below is a breakdown of all available parameters and how to adjust them:

### Telegram Settings
* **`telegram_bot_token`** (String): The token provided by Telegram's BotFather when you create your bot.
* **`telegram_chat_id`** (String): The numerical ID of the chat, user, or group where the bot should send notifications.

### Timing & Polling (Anti-Ban)
* **`check_interval_seconds`** (Integer): The wait time in seconds between full check cycles. 
    * *Recommendation:* Set to `300` (5 minutes) or higher. Doctolib aggressively rate-limits; setting this too low will result in a temporary IP ban.
* **`delay_between_urls_seconds`** (Integer): The pause in seconds between fetching individual URLs *during* a single cycle. Prevents the script from hammering the server with concurrent requests.
    * *Recommendation:* Keep between `2` and `5` seconds.

### Search Criteria
* **`upcoming_days`** (Integer): How many days into the future the script should check for available slots. For example, `15` will look for appointments within the next 15 days.

### Notifications & Messages
* **`startup_message`** (String): The Telegram message sent at script startup. You can customize this using the following dynamic placeholders:
    * `{doctor_count}`: Number of practitioners being monitored.
    * `{practitioner_list}`: List of practitioner names.
    * `{interval_mins}`: Check interval in minutes.
    * `{days}`: Upcoming days window.
* **`shutdown_message`** (String): The Telegram message sent when the script stops.
* **`message_template`** (String): The layout of the Telegram message sent when slots are found. You can customize this using the following dynamic placeholders:
    * `{total}`: Number of available slots found.
    * `{practitioner}`: The doctor's name.
    * `{practice}`: The practice/clinic name.
    * `{first_date}`: The earliest available appointment date.
    * `{booking_url}`: The direct link to book the appointment.
* **`user_agent`** (String): The browser User-Agent string used to mimic a real web browser. You generally do not need to change this unless Doctolib blocks the default one.

### Target Doctors/Clinics
* **`urls`** (Array of Strings): A list of exact Doctolib booking URLs to monitor. 
    * *Important:* These must be the final URLs from the booking process, containing all query parameters (e.g., `specialityId`, `motiveIds`, `practitionerId`). Simply linking to a doctor's profile page will not work.

## Usage

- **Windows Shortcut:** Simply double-click `run.bat`. It will activate your virtual environment (if one exists) and start the polling loop.
- **Command Line:** Run the script manually from your terminal:
  ```bash
  python checker.py
  ```
- **Single Execution:** To run one check cycle without starting the continuous loop, use the `--once` flag:
  ```bash
  python checker.py --once
  ```

## Limitations & Considerations

- **Rate Limiting:** Doctolib uses anti-bot measures. Do not set your polling intervals too aggressively. Keep the interval to at least 5 minutes to minimize the risk of a temporary IP block.
- **URL Accuracy:** The URLs in your `config.json` must be exact and contain the correct query parameters (`specialityId`, `motiveIds`, `practitionerId`, etc.) for the script to locate availabilities. Copy them directly from the final booking step in your browser.
- **Always-On Requirement:** Because this runs locally, your computer must remain powered on, awake, and connected to the internet for the script to work.

## Project Status & Development

This tool is a personal utility and is provided entirely **"as-is"**. There is no planned roadmap, and active maintenance, feature requests, or bug fixes are not guaranteed. Feel free to fork the repository to modify it for your own needs.

*Note: This project was primarily developed with the assistance of AI tools. While it gets the job done for its specific use case, the internal logic and structure may bypass traditional software engineering patterns.*