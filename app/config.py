import json
import logging
import os
import sys

STATE_DIR = "state"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "checker.log")
CONFIG_FILE = "config.json"

DOCTOLIB_BASE_URL = "https://www.doctolib.de"
DOCTOLIB_INFO_API = f"{DOCTOLIB_BASE_URL}/online_booking/api/slot_selection_funnel/v1/info.json"
DOCTOLIB_AVAILABILITIES_API = f"{DOCTOLIB_BASE_URL}/availabilities.json"
TELEGRAM_API_BASE = "https://api.telegram.org"


def load_config():
    if not os.path.exists(CONFIG_FILE):
        logging.error(f"Configuration file '{CONFIG_FILE}' not found!")
        sys.exit(1)

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        try:
            config = json.load(f)
        except json.JSONDecodeError as e:
            logging.error(
                f"Failed to parse '{CONFIG_FILE}'. Please check for JSON syntax errors: {e}"
            )
            sys.exit(1)

    config.setdefault("telegram", {})
    config["telegram"].setdefault("bot_token", None)
    config["telegram"].setdefault("chat_id", None)
    config["telegram"].setdefault("silent", False)

    config.setdefault("polling", {})
    config["polling"].setdefault("check_interval_seconds", 300)
    config["polling"].setdefault("delay_between_urls_seconds", 3)
    config["polling"].setdefault("upcoming_days", 15)
    config["polling"].setdefault("insurance_sector", "public")
    config["polling"].setdefault("telehealth", False)
    config["polling"].setdefault("slot_limit", 15)

    config.setdefault("messages", {})
    config["messages"].setdefault("startup", {})
    config["messages"]["startup"].setdefault("silent", False)
    config["messages"]["startup"].setdefault(
        "template",
        "🚀 <b>Doctolib Checker Started</b>\n\n"
        "🗓 {start_time}\n"
        "🎯 Monitoring <b>{doctor_count}</b> target(s) across "
        "<b>{practice_count}</b> practice(s)\n\n"
        "{practitioner_list}\n\n"
        "⏱ Poll: every <code>{interval_mins}m</code>\n"
        "📅 Window: next <code>{days}d</code>\n"
        "🏥 Insurance: <code>{insurance_sector}</code>",
    )

    config["messages"].setdefault("shutdown", {})
    config["messages"]["shutdown"].setdefault("silent", False)
    config["messages"]["shutdown"].setdefault(
        "template",
        "🛑 <b>Doctolib Checker Stopped</b>\n\nMonitoring has been disabled.",
    )

    config["messages"].setdefault("slot_found", {})
    config["messages"]["slot_found"].setdefault("silent", False)
    config["messages"]["slot_found"].setdefault(
        "template",
        "🎉 <b>{total} slot(s) available</b>\n\n"
        "👨‍⚕️ {practitioner}\n"
        "🏥 {practice}\n"
        "📅 Earliest: <b>{first_date}</b>\n\n"
        '👉 <a href="{booking_url}">Open booking</a>',
    )
    config["messages"]["slot_found"].setdefault("effect", {})
    config["messages"]["slot_found"]["effect"].setdefault("enabled", False)
    config["messages"]["slot_found"]["effect"].setdefault(
        "id", "5046509860389126442"
    )

    config["messages"].setdefault("far_slot_found", {})
    config["messages"]["far_slot_found"].setdefault("silent", False)
    config["messages"]["far_slot_found"].setdefault(
        "template",
        "📆 <b>Slot opened on calendar</b>\n\n"
        "👨‍⚕️ {practitioner}\n"
        "🏥 {practice}\n"
        "📅 Date: <b>{first_date}</b>\n\n"
        '👉 <a href="{booking_url}">Open booking</a>',
    )
    config["messages"]["far_slot_found"].setdefault("effect", {})
    config["messages"]["far_slot_found"]["effect"].setdefault("enabled", False)

    config["messages"].setdefault("summary", {})
    config["messages"]["summary"].setdefault("enabled", False)
    config["messages"]["summary"].setdefault("interval_seconds", 0)
    config["messages"]["summary"].setdefault("every_x_cycles", 0)
    config["messages"]["summary"].setdefault("silent", True)
    config["messages"]["summary"].setdefault(
        "template",
        "📊 <b>Heartbeat</b> · uptime <code>{uptime}</code>\n\n"
        "🔄 <b>{total_cycles}</b> checks · "
        "🎯 <b>{total_hits}</b> hits · "
        "⚠️ <b>{total_errors}</b> errors\n"
        "{last_slot_line}\n\n"
        "⏭ Next check in ~<code>{next_check_in}</code>",
    )

    config.setdefault(
        "ui",
        {"terminal_table": False, "show_full_names": True, "colorblind_friendly": False},
    )
    config.setdefault("dry_run", False)
    config.setdefault(
        "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    config.setdefault("urls", [])

    if not config["telegram"]["bot_token"] or not config["telegram"]["chat_id"]:
        logging.warning("Telegram credentials missing in config. Alerts will fail.")

    if not config["urls"]:
        logging.warning("No URLs configured in config.json.")

    return config
