import logging
import re
import time

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
