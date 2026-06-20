# doctolib-checker

A lightweight Doctolib appointment poller for a fixed set of booking URLs (e.g., multiple surgeons at one clinic). It monitors availability and sends Telegram notifications when new slots appear. 

Designed to run locally as a continuous background process.

## Setup

1. **Install dependencies:** Ensure you have Python installed, then install the required packages:
  ```bash
   pip install -r requirements.txt
  ```
2. **Configuration:** Copy `config.example.json` to `config.json` and populate it with your specific details:
  - **Telegram:** Add your Bot Token and Chat ID.
  - **URLs:** Add the exact Doctolib booking URLs you wish to monitor.
  - **Timing:** Adjust `check_interval_seconds` (recommended: 300+ seconds) to avoid rate limits.

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