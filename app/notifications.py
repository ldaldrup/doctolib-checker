from __future__ import annotations

import logging
import json
from dataclasses import dataclass
import re
import time
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

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


CONTENT_FIELDS = ('job_name', 'practitioner', 'practice', 'earliest_appointment',
                  'check_time', 'time_zone', 'booking_link')
STANDARD_FIELDS = ('practitioner', 'practice', 'earliest_appointment', 'booking_link')
COMPACT_FIELDS = ('practitioner', 'earliest_appointment', 'booking_link')


def normalize_content(value=None):
    """Validate the structured vocabulary and resolve preset fields."""
    options = {'preset': 'standard', 'silent': False} if value is None else value
    if (not isinstance(options, dict) or set(options) - {'preset', 'fields', 'silent'} or
            options.get('preset', 'standard') not in ('standard', 'compact', 'custom') or
            type(options.get('silent', False)) is not bool):
        raise ValueError('invalid_message_content')
    fields = options.get('fields', [])
    preset = options.get('preset', 'standard')
    if (not isinstance(fields, list) or any(not isinstance(field, str) or field not in CONTENT_FIELDS for field in fields) or
            len(set(fields)) != len(fields) or (preset == 'custom' and not fields)):
        raise ValueError('invalid_message_content')
    chosen = STANDARD_FIELDS if preset == 'standard' else COMPACT_FIELDS if preset == 'compact' else fields
    return {'preset': preset, 'fields': list(chosen), 'silent': options.get('silent', False)}


def render_notification(channel_type, alert, content=None):
    """Pure structured renderer shared by previews and saved event deliveries."""
    options = normalize_content(content if content is not None else alert.get('message_content'))
    fields, preset, silent = options['fields'], options['preset'], options['silent']
    if channel_type not in ('telegram', 'ntfy', 'email', 'webhook'):
        raise ValueError('unsupported_notification_channel')
    if silent and channel_type not in ('telegram', 'ntfy'):
        raise ValueError('unsupported_silent_option')
    if channel_type == 'webhook':
        return {'schema_version': 1, 'event_id': alert.get('event_id') or 'synthetic-test',
            'trigger': alert.get('triggered_by') or 'test', 'search_revision': alert.get('search_revision'),
            'job': {'id': alert.get('job_id'), 'name': alert.get('job_name', '')},
            'target': {'id': alert.get('target_id'), 'practitioner_name': alert.get('practitioner_name', ''),
                'practice_name': alert.get('practice_name', '')},
            'earliest_slot': alert.get('earliest_slot'), 'slot_count': alert.get('slot_count', 1),
            'checked_at': alert.get('checked_at'), 'time_zone': alert.get('time_zone') or 'UTC',
            'booking_url': alert.get('booking_url', '')}
    warnings = []

    def clean(value, fallback='Unavailable'):
        raw = str(value or fallback)
        text = ''.join(c for c in raw if ord(c) >= 32 and ord(c) != 127)
        if text != raw:
            warnings.append('Control characters removed.')
        if len(text) > 160:
            warnings.append('Long fields truncated to 160 characters.')
            text = text[:159] + '…'
        return text

    def html(value):
        # Escape whole characters, never split an HTML entity or tag.
        if len(escape(value)) > 384:
            warnings.append('Escaped fields truncated to fit the channel.')
            while len(escape(value + '…')) > 384:
                value = value[:-1]
            value += '…'
        return escape(value)

    zone = clean(alert.get('time_zone'), 'UTC')
    try:
        tz = ZoneInfo(zone)
    except (ValueError, KeyError):
        zone, tz = 'UTC', timezone.utc
        warnings.append('Invalid time zone replaced with UTC.')

    def stamp(value):
        if not value:
            return 'Unavailable'
        try:
            moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
                warnings.append('Timestamp without a time zone treated as UTC.')
            return moment.astimezone(tz).strftime('%Y-%m-%d %H:%M %Z')
        except (ValueError, OverflowError):
            warnings.append('Invalid timestamp shown as unavailable.')
            return 'Unavailable'

    booking = str(alert.get('booking_url') or '')
    try:
        url = urlsplit(booking)
        if (url.scheme != 'https' or not url.hostname or url.username is not None or
                url.password is not None or any(ord(c) <= 32 or ord(c) == 127 for c in booking) or
                '\\' in booking or len(escape(booking, quote=True)) > 1024):
            booking = ''
    except ValueError:
        booking = ''
    if not booking:
        warnings.append('Booking link unavailable or unsafe; omitted.')
    values = {'job_name': clean(alert.get('job_name')),
        'practitioner': clean(alert.get('practitioner_name'), 'Practitioner'),
        'practice': clean(alert.get('practice_name'), 'Practice'),
        'earliest_appointment': stamp(alert.get('earliest_slot')),
        'check_time': stamp(alert.get('checked_at')), 'time_zone': zone}
    labels = {'job_name': 'Job', 'practitioner': 'Practitioner', 'practice': 'Practice',
        'earliest_appointment': 'Earliest', 'check_time': 'Checked', 'time_zone': 'Time zone'}
    chosen = STANDARD_FIELDS if preset == 'standard' else COMPACT_FIELDS if preset == 'compact' else fields
    try:
        count = max(1, min(999999, int(alert.get('slot_count', 1))))
    except (ValueError, TypeError, OverflowError):
        count = 1
    heading = f'{count} matching appointment slot(s)'
    lines, html_lines = [heading], ['<b>' + heading + '</b>']
    for field in chosen:
        if field == 'booking_link':
            continue
        value = values[field]
        if preset == 'standard' and field in ('practitioner', 'practice'):
            lines.append(value)
            html_lines.append(('👨‍⚕️ ' if field == 'practitioner' else '🏥 ') + html(value))
        else:
            lines.append(labels[field] + ': ' + value)
            html_lines.append(('📅 ' if field == 'earliest_appointment' else '') + labels[field] + ': ' +
                ('<b>' + html(value) + '</b>' if field == 'earliest_appointment' else html(value)))
    link = booking if 'booking_link' in chosen else ''
    text = '\n'.join(lines) + ('\n\nOpen booking on Doctolib: ' + link if link else '')
    if channel_type == 'telegram':
        rendered = (html_lines[0] + '\n\n' + '\n'.join(html_lines[1:])
                    if preset == 'standard' else '\n'.join(html_lines))
        if link:
            rendered += '\n\n<a href="' + escape(link, quote=True) + '">Open booking on Doctolib</a>'
        return {'text': rendered, 'parse_mode': 'HTML', 'disable_notification': silent,
                'warnings': list(dict.fromkeys(warnings))}
    if channel_type == 'ntfy':
        # Standard preserves the event marker and uses the configured local time.
        if preset == 'standard':
            lines = [heading, values['practitioner'], values['practice'],
                'Earliest: ' + values['earliest_appointment'] + ' (' + zone + ')',
                'Event: ' + clean(alert.get('event_id'), 'synthetic-test')]
        return {'title': 'Matching appointment available', 'message': '\n'.join(lines),
                'click': link, 'silent': silent, 'priority': 1 if silent else alert.get('ntfy_priority', 3),
                'warnings': list(dict.fromkeys(warnings))}
    rendered = '<p>' + '</p><p>'.join(escape(line) for line in lines) + '</p>'
    if link:
        rendered += '<p><a href="' + escape(link, quote=True) + '">Open booking on Doctolib</a></p>'
    return {'subject': 'Matching appointment available', 'text': text, 'html': rendered,
            'warnings': list(dict.fromkeys(warnings))}


def format_slot_alert(alert):
    return render_notification('telegram', alert)['text']


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


def _bounded_attempt_child(pipe, attempt, args, prefix):
    import os
    import threading

    watchdog = threading.Timer(SEND_BUDGET_SECONDS, lambda: os._exit(70))
    watchdog.daemon = True
    watchdog.start()
    permitted = False

    def permission(**kwargs):
        nonlocal permitted
        pipe.send("ready")
        permitted = pipe.recv() is True
        return permitted

    try:
        try:
            pipe.send(attempt(*args, before_send=permission))
        except BaseException:
            pipe.send(DeliveryOutcome("uncertain" if permitted else "retry",
                                      f"{prefix}_transport_failure", attempted=permitted))
    finally:
        watchdog.cancel()
        pipe.close()


def send_bounded_attempt(attempt, args, before_send, prefix):
    """Run one prepared send with a hard deadline and persisted send gate."""
    import multiprocessing
    import time

    parent = child = process = None
    permitted = False
    deadline = time.monotonic() + SEND_BUDGET_SECONDS
    try:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(target=_bounded_attempt_child,
                                  args=(child, attempt, args, prefix), daemon=True)
        process.start()
        child.close()
        if parent.poll(max(0, deadline - time.monotonic())):
            result = parent.recv()
            if isinstance(result, DeliveryOutcome):
                return result
            if result == "ready":
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (before_send is not None and
                        not before_send(remaining_seconds=remaining)):
                    return DeliveryOutcome("retry", "alert_no_longer_eligible", attempted=False)
                permitted = True
                if time.monotonic() < deadline:
                    parent.send(True)
                    if parent.poll(max(0, deadline - time.monotonic())):
                        result = parent.recv()
                        if isinstance(result, DeliveryOutcome):
                            return result
        return DeliveryOutcome("uncertain" if permitted else "retry",
                               f"{prefix}_{'attempt' if permitted else 'preparation'}_deadline",
                               attempted=permitted)
    except Exception:
        return DeliveryOutcome("uncertain" if permitted else "retry",
                               f"{prefix}_transport_failure", attempted=permitted)
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
                    raise RuntimeError(f"{prefix}_transport_cleanup_failed")
            process.close()


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
    rendered = render_notification('telegram', alert)
    return {'chat_id': settings.telegram_chat_id, 'text': rendered['text'],
            'parse_mode': 'HTML', 'disable_notification': rendered['disable_notification'],
            'disable_web_page_preview': True}


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
