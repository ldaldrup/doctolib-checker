import logging
import re
import time
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

import requests
from colorama import Fore, Style

from app.config import TELEGRAM_API_BASE
from app.logging_utils import strip_ansi
from app.state import save_state


def should_notify(prev_state, new_total):
    prev_notified = prev_state.get("last_notified_total", 0)
    if new_total > 0 and (prev_notified == 0 or new_total > prev_notified):
        return True
    return False


def html_to_terminal_text(html_str):
    def replace_link(match):
        url = match.group(1)
        text = match.group(2)
        return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"

    text = re.sub(r"<a\s+href=[\'\"]([^\'\"]+)[\'\"]>(.*?)</a>", replace_link, html_str)
    text = re.sub(r"</?(b|i|strong|em|code)>", "", text)
    return text


def send_telegram(config, text, silent=None, effect_id=None, max_attempts=5):
    term_text = html_to_terminal_text(text)
    bar_color = Fore.LIGHTBLACK_EX
    bar = f"{bar_color}{'─' * 60}{Style.RESET_ALL}"
    print(f"\n{bar}")

    flags = []
    if silent if silent is not None else config["telegram"]["silent"]:
        flags.append("silent")
    if effect_id:
        flags.append("🎆 effect")
    flag_str = f" ({', '.join(flags)})" if flags else ""

    print(f"{bar_color} outgoing telegram{flag_str} {Style.RESET_ALL}")
    print(bar)
    for line in term_text.split("\n"):
        print(f"  {line}")
    print(f"{bar}\n")

    if config.get("dry_run"):
        logging.info(f"{Fore.YELLOW}[dry-run] Telegram send skipped.{Style.RESET_ALL}")
        return True

    token = config["telegram"]["bot_token"]
    chat_id = config["telegram"]["chat_id"]
    if not token or not chat_id:
        logging.warning("Telegram credentials missing. Skipping API call.")
        return False

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    is_silent = silent if silent is not None else config["telegram"]["silent"]

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": is_silent,
    }

    if effect_id:
        payload["message_effect_id"] = effect_id

    headers = {"User-Agent": "Doctolib-Checker/1.0", "Connection": "keep-alive"}
    session = requests.Session()

    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.post(url, json=payload, headers=headers, timeout=30)
            if resp.status_code == 200:
                return True
            logging.warning(
                f"Telegram API returned {resp.status_code} "
                f"(attempt {attempt}/{max_attempts})"
            )
        except requests.exceptions.ConnectionError:
            logging.warning(
                f"Telegram send failed: connection error "
                f"(attempt {attempt}/{max_attempts})"
            )
        except Exception as e:
            err = str(e)
            if len(err) > 80:
                err = err[:77] + "…"
            logging.warning(f"Telegram send failed: {err} (attempt {attempt}/{max_attempts})")

        if attempt < max_attempts:
            time.sleep(min(2 ** attempt, 32))

    return False


def format_slot_alert(alert):
    practitioner = escape(str(alert.get("practitioner_name", "Practitioner")))
    practice = escape(str(alert.get("practice_name", "Practice")))
    slot_value = str(alert.get("earliest_slot", ""))
    try:
        slot_time = datetime.fromisoformat(slot_value.replace("Z", "+00:00"))
        slot = slot_time.astimezone(ZoneInfo(alert.get("time_zone") or "UTC")).strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, KeyError):
        slot = slot_value
    slot = escape(slot)
    booking_url = escape(str(alert.get("booking_url", "")), quote=True)
    count = int(alert.get("slot_count", 1))
    return (
        f"<b>{count} matching appointment slot(s)</b>\n\n"
        f"👨‍⚕️ {practitioner}\n"
        f"🏥 {practice}\n"
        f"📅 Earliest: <b>{slot}</b>\n\n"
        f'<a href="{booking_url}">Open booking on Doctolib</a>'
    )


def send_telegram_alert(settings, alert, session=None, before_send=None):
    """Send one structured slot alert without logging its URL or credentials."""
    if not settings.telegram_enabled or not settings.telegram_bot_token or not settings.telegram_chat_id:
        return False, "telegram_not_configured"

    request_session = session or requests.Session()
    endpoint = f"{TELEGRAM_API_BASE}/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": format_slot_alert(alert),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    last_error = "telegram_delivery_failed"
    for attempt in range(1, 4):
        # Recheck eligibility after retry backoff; edits can invalidate evidence.
        if before_send is not None and not before_send():
            return False, "alert_no_longer_eligible"
        try:
            response = request_session.post(
                endpoint,
                json=payload,
                headers={"User-Agent": "DoctolibChecker/2.0"},
                timeout=20,
            )
            if response.status_code == 200 and response.json().get("ok") is True:
                return True, None
            last_error = "telegram_http_" + str(response.status_code)
        except requests.exceptions.Timeout:
            last_error = "telegram_timeout"
        except requests.exceptions.ConnectionError:
            last_error = "telegram_connection_error"
        except Exception:
            # Never include requests' URL-bearing exception text: the URL contains the bot token.
            last_error = "telegram_delivery_error"
        if attempt < 3:
            time.sleep(2 ** attempt)
    logging.warning("Telegram delivery failed (%s)", last_error)
    return False, last_error
