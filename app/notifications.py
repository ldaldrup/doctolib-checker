from __future__ import annotations

import logging
import json
from dataclasses import dataclass
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


@dataclass(frozen=True)
class DeliveryOutcome:
    category: str
    error_code: str | None = None
    retry_after: float | None = None
    attempted: bool = True


SEND_BUDGET_SECONDS = 15.0
CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 7.0
MAX_RESPONSE_BYTES = 65536


def _retry_after(response, body):
    value = body.get("parameters", {}).get("retry_after") if isinstance(body, dict) else None
    if value is None:
        value = response.headers.get("Retry-After")
    try:
        return min(900.0, max(5.0, float(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _known_presend_failure(exc):
    # A generic ConnectionError can mean the acknowledgement was lost after
    # acceptance. Only connection establishment / DNS failure is retryable.
    import urllib3.exceptions
    pending, seen = [exc], set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        if isinstance(error, urllib3.exceptions.NewConnectionError):
            return True
        pending.extend(value for value in getattr(error, "args", ()) if isinstance(value, BaseException))
        pending.extend(value for value in (getattr(error, "reason", None),
                       getattr(error, "__cause__", None)) if isinstance(value, BaseException))
    return False


def _telegram_payload(settings, alert):
    return {"chat_id": settings.telegram_chat_id, "text": format_slot_alert(alert),
            "parse_mode": "HTML", "disable_web_page_preview": True}


def _telegram_attempt(settings, alert, session, payload=None):
    endpoint = f"{TELEGRAM_API_BASE}/bot{settings.telegram_bot_token}/sendMessage"
    payload = payload if payload is not None else _telegram_payload(settings, alert)
    response = None
    try:
        response = session.post(endpoint, json=payload,
                                headers={"User-Agent": "DoctolibChecker/2.0"},
                                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS), stream=True,
                                allow_redirects=False)
        # Bound provider data even for an endless chunked body. The parent
        # process also bounds the full operation, including DNS/body trickling.
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=4096):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                return DeliveryOutcome("uncertain", "telegram_response_too_large")
            chunks.append(chunk)
        try:
            body = json.loads(b"".join(chunks))
        except (ValueError, UnicodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        status = response.status_code
        if status == 200 and body.get("ok") is True:
            return DeliveryOutcome("sent")
        provider_code = body.get("error_code") if body.get("ok") is False else status
        if provider_code in (400, 401, 403):
            return DeliveryOutcome("action_required", "telegram_rejected_" + str(provider_code))
        if provider_code == 429:
            return DeliveryOutcome("retry", "telegram_rate_limited", _retry_after(response, body))
        if provider_code in (500, 502, 503, 504):
            return DeliveryOutcome("retry", "telegram_temporary_rejection", _retry_after(response, body))
        return DeliveryOutcome("uncertain", "telegram_unconfirmed_response")
    except requests.exceptions.ConnectTimeout:
        return DeliveryOutcome("retry", "telegram_connect_timeout")
    except requests.exceptions.ConnectionError as exc:
        if _known_presend_failure(exc):
            return DeliveryOutcome("retry", "telegram_connect_failed")
        return DeliveryOutcome("uncertain", "telegram_connection_lost")
    except requests.exceptions.Timeout:
        return DeliveryOutcome("uncertain", "telegram_acknowledgement_timeout")
    except Exception:
        return DeliveryOutcome("uncertain", "telegram_delivery_error")
    finally:
        if response is not None:
            response.close()


def _telegram_child(connection, settings, alert):
    import os
    import threading
    # Independent watchdog remains effective if the dispatcher parent crashes.
    # os._exit closes sockets even if a provider trickles response bytes forever.
    watchdog = threading.Timer(SEND_BUDGET_SECONDS, lambda: os._exit(70))
    watchdog.daemon = True
    watchdog.start()
    permitted = False
    try:
        try:
            payload = _telegram_payload(settings, alert)
        except Exception:
            connection.send(DeliveryOutcome("action_required", "telegram_invalid_payload", attempted=False))
            return
        with requests.Session() as session:
            # All local preparation succeeds before SQLite records an attempt.
            connection.send("ready")
            permitted = connection.recv() is True
            if permitted:
                connection.send(_telegram_attempt(settings, alert, session, payload=payload))
    except BaseException:
        try:
            connection.send(DeliveryOutcome("uncertain" if permitted else "retry",
                                           "telegram_transport_failure", attempted=permitted))
        except BaseException:
            pass
    finally:
        watchdog.cancel()
        connection.close()


def send_telegram_alert(settings, alert, session=None, before_send=None):
    """One send attempt, with no retries or backoff sleeps.

    A prepared child waits for SQLite's persisted attempt permission before POST.
    Production transport has independent child and parent wall-clock watchdogs.
    Injected sessions are offline test transports.
    """
    if not settings.telegram_enabled or not settings.telegram_bot_token or not settings.telegram_chat_id:
        return DeliveryOutcome("action_required", "telegram_not_configured", attempted=False)
    if session is not None:
        try:
            payload = _telegram_payload(settings, alert)
        except Exception:
            return DeliveryOutcome("action_required", "telegram_invalid_payload", attempted=False)
        if before_send is not None and not before_send(remaining_seconds=SEND_BUDGET_SECONDS):
            return DeliveryOutcome("retry", "alert_no_longer_eligible", attempted=False)
        return _telegram_attempt(settings, alert, session, payload=payload)
    import multiprocessing
    import time as clock
    permitted = False
    process = None
    parent = child = None
    deadline = clock.monotonic() + SEND_BUDGET_SECONDS
    try:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(target=_telegram_child, args=(child, settings, alert), daemon=True)
        process.start()
        child.close()
        if parent.poll(max(0, deadline - clock.monotonic())):
            message = parent.recv()
            if isinstance(message, DeliveryOutcome):
                return message
            if message == "ready":
                if clock.monotonic() >= deadline:
                    return DeliveryOutcome("retry", "telegram_preparation_deadline", attempted=False)
                if before_send is not None and not before_send(
                        remaining_seconds=max(0, deadline - clock.monotonic())):
                    return DeliveryOutcome("retry", "alert_no_longer_eligible", attempted=False)
                # Persisted permission might already represent an attempted send
                # if acknowledgement is lost after this boundary.
                permitted = True
                if clock.monotonic() >= deadline:
                    return DeliveryOutcome("uncertain", "telegram_attempt_deadline", attempted=True)
                parent.send(True)
                if parent.poll(max(0, deadline - clock.monotonic())):
                    outcome = parent.recv()
                    if isinstance(outcome, DeliveryOutcome):
                        return outcome
        return DeliveryOutcome("uncertain" if permitted else "retry",
                               "telegram_attempt_deadline" if permitted else "telegram_preparation_deadline",
                               attempted=permitted)
    except Exception:
        return DeliveryOutcome("uncertain" if permitted else "retry",
                               "telegram_transport_failure", attempted=permitted)
    finally:
        if child is not None:
            child.close()
        if parent is not None:
            parent.close()
        if process is not None and process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
                if process.is_alive():
                    raise RuntimeError("telegram_transport_cleanup_failed")
            process.close()
