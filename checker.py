import os
import sys
import time
import json
import logging
import argparse
import urllib.parse
import re
import warnings
from datetime import date, datetime
from dataclasses import dataclass
from collections import OrderedDict
from typing import Optional

import requests
from colorama import init, Fore, Style
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Silence urllib3 retry warnings (we handle retries ourselves)
warnings.filterwarnings("ignore")

init(autoreset=True)

# ── ANSI-aware helpers ──────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def pad_right(text: str, width: int) -> str:
    """Pad *text* on the right so its *visible* length equals *width*."""
    visible_len = len(strip_ansi(text))
    return text + " " * max(0, width - visible_len)


def truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


# ── 1. Constants & Paths ────────────────────────────────────────────

STATE_DIR = "state"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "checker.log")
CONFIG_FILE = "config.json"

DOCTOLIB_BASE_URL = "https://www.doctolib.de"
DOCTOLIB_INFO_API = f"{DOCTOLIB_BASE_URL}/online_booking/api/slot_selection_funnel/v1/info.json"
DOCTOLIB_AVAILABILITIES_API = f"{DOCTOLIB_BASE_URL}/availabilities.json"
TELEGRAM_API_BASE = "https://api.telegram.org"

# ── 2. Logging ──────────────────────────────────────────────────────


class ColorFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: Fore.CYAN,
        logging.INFO: Fore.CYAN,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Style.BRIGHT + Fore.RED,
    }

    def format(self, record):
        log_color = self.LEVEL_COLORS.get(record.levelno, Fore.WHITE)
        time_str = f"{Fore.LIGHTBLACK_EX}%(asctime)s{Style.RESET_ALL}"
        level_str = f"{log_color}[%(levelname)s]{Style.RESET_ALL}"
        msg_str = "%(message)s"
        formatter = logging.Formatter(
            f"{time_str} {level_str} {msg_str}", datefmt="%H:%M:%S"
        )
        return formatter.format(record)


def setup_directories_and_logging():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(ColorFormatter())

    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])
    logging.getLogger("urllib3").setLevel(logging.ERROR)


# ── Config ──────────────────────────────────────────────────────────


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

    # ── Telegram ──
    config.setdefault("telegram", {})
    config["telegram"].setdefault("bot_token", None)
    config["telegram"].setdefault("chat_id", None)
    config["telegram"].setdefault("silent", False)

    # ── Polling ──
    config.setdefault("polling", {})
    config["polling"].setdefault("check_interval_seconds", 300)
    config["polling"].setdefault("delay_between_urls_seconds", 3)
    config["polling"].setdefault("upcoming_days", 15)
    config["polling"].setdefault("insurance_sector", "public")
    config["polling"].setdefault("telehealth", False)
    config["polling"].setdefault("slot_limit", 15)

    # ── Messages ──
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

    # ── Misc ──
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


# ── 3. State ────────────────────────────────────────────────────────


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logging.error("Corrupted state.json. Starting fresh.")
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ── 4. Session & Dataclasses ────────────────────────────────────────

GLOBAL_SESSION = None


def get_session():
    global GLOBAL_SESSION
    if GLOBAL_SESSION is None:
        session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy, pool_connections=5, pool_maxsize=5
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        GLOBAL_SESSION = session
    return GLOBAL_SESSION


@dataclass
class BookingMeta:
    state_key: str
    practice_name: str
    practitioner_name: str
    motive_id: str
    agenda_ids_str: str
    practice_id: str
    display_name: str


@dataclass
class SessionStats:
    """In-memory stats for the current run session. Resets on restart."""

    session_start: datetime
    total_cycles: int = 0
    total_hits: int = 0
    total_errors: int = 0
    last_summary_sent: Optional[datetime] = None
    last_slot_time: Optional[datetime] = None
    last_slot_practitioner: Optional[str] = None
    last_slot_practice: Optional[str] = None
    last_slot_total: int = 0


def _practitioner_display_name(p):
    """Try every name field Doctolib might expose."""
    if not p:
        return None
    return (
        p.get("name")
        or p.get("full_name")
        or p.get("display_name")
        or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        or None
    )


# ── 5. Time formatting helpers ──────────────────────────────────────


def format_uptime(start: datetime) -> str:
    """Format a duration as a compact human-readable string."""
    delta = datetime.now() - start
    total_seconds = int(delta.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def format_ago(when: datetime) -> str:
    """Format a past timestamp as a relative human-readable string."""
    delta = datetime.now() - when
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return "just now"
    minutes, remainder = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago"


def format_duration(seconds: int) -> str:
    """Format a static duration in seconds to a readable string."""
    if seconds >= 3600:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h" if m == 0 else f"{h}h {m}m"
    elif seconds >= 60:
        return f"{seconds // 60}m"
    else:
        return f"{seconds}s"


def format_doctolib_datetime(dt_str: str) -> str:
    """Format a Doctolib datetime string to a clean 'YYYY-MM-DD HH:MM' format."""
    if not dt_str:
        return ""
    if "T" in dt_str:
        try:
            date_part = dt_str.split("T")[0]
            time_part = dt_str.split("T")[1][:5]
            return f"{date_part} {time_part}"
        except IndexError:
            return dt_str
    return dt_str


# ── 6. Doctolib API ─────────────────────────────────────────────────


def get_booking_metadata(booking_url, config, session):
    parsed = urllib.parse.urlparse(booking_url)
    path_parts = parsed.path.split("/")

    try:
        slug_idx = path_parts.index("booking") - 1
        slug = path_parts[slug_idx]
    except ValueError:
        slug = path_parts[3]

    query_params = urllib.parse.parse_qs(parsed.query)

    try:
        raw_place_id = query_params.get("placeId", [None])[0]
        practice_id = (
            raw_place_id.split("-")[1]
            if raw_place_id and "-" in raw_place_id
            else raw_place_id
        )
        motive_id = query_params.get("motiveIds[]", query_params.get("motiveIds", [None]))[0]
        practitioner_id = query_params.get(
            "practitionerId", query_params.get("practitioner_id", [None])
        )[0]
    except (TypeError, IndexError, AttributeError) as e:
        raise ValueError(f"Could not parse required IDs from URL parameters: {e}")

    headers = {"User-Agent": config["user_agent"]}

    info_resp = session.get(
        DOCTOLIB_INFO_API,
        params={"profile_slug": slug},
        headers=headers,
        timeout=10,
    )
    info_resp.raise_for_status()
    info_data = info_resp.json().get("data", {})

    agendas = info_data.get("agendas", [])
    practitioners = info_data.get("practitioners", [])

    profile = info_data.get("profile", {})
    profile_name = (
        profile.get("name_with_title") or profile.get("name") or slug
    )
    practice_name = profile_name

    # ── Smart practitioner-name resolution ──
    practitioner_name = None

    if practitioner_id and practitioner_id != "NO_PREFERENCE":
        for p in practitioners:
            if str(p.get("id")) == str(practitioner_id):
                practitioner_name = _practitioner_display_name(p)
                break
        if not practitioner_name:
            practitioner_name = f"Practitioner (ID: {practitioner_id})"
    else:
        unique_pids = []
        seen = set()
        for agenda in agendas:
            if practice_id and str(agenda.get("practice_id")) != str(practice_id):
                continue
            if motive_id:
                try:
                    if int(motive_id) not in agenda.get("visit_motive_ids", []):
                        continue
                except (TypeError, ValueError):
                    continue
            pid = agenda.get("practitioner_id")
            if pid and pid not in seen:
                seen.add(pid)
                unique_pids.append(str(pid))

        if len(unique_pids) == 1:
            resolved = None
            for p in practitioners:
                if str(p.get("id")) == unique_pids[0]:
                    resolved = _practitioner_display_name(p)
                    break
            practitioner_name = resolved or profile_name
        elif len(practitioners) == 1:
            practitioner_name = _practitioner_display_name(practitioners[0]) or profile_name
        elif len(unique_pids) > 1:
            practitioner_name = f"Any of {len(unique_pids)} practitioners"
        else:
            practitioner_name = "Any Practitioner"

    # ── Filter agendas ──
    valid_agenda_ids = []
    for agenda in agendas:
        if practice_id and str(agenda.get("practice_id")) != str(practice_id):
            continue
        if motive_id:
            try:
                if int(motive_id) not in agenda.get("visit_motive_ids", []):
                    continue
            except (TypeError, ValueError):
                continue
        if practitioner_id and practitioner_id != "NO_PREFERENCE":
            if str(agenda.get("practitioner_id")) != str(practitioner_id):
                continue
        valid_agenda_ids.append(str(agenda["id"]))

    if not valid_agenda_ids:
        raise ValueError(
            f"No specific agenda found for motive={motive_id}, "
            f"practitioner={practitioner_id}"
        )

    state_key = f"{slug}_{practitioner_id}" if practitioner_id else slug
    agenda_ids_str = "-".join(valid_agenda_ids)
    display_name = f"{practitioner_name} @ {practice_name}"

    return BookingMeta(
        state_key=state_key,
        practice_name=practice_name,
        practitioner_name=practitioner_name,
        motive_id=motive_id,
        agenda_ids_str=agenda_ids_str,
        practice_id=practice_id,
        display_name=display_name,
    )


def fetch_slot_total(booking_url, config, session, meta=None):
    if meta is None:
        meta = get_booking_metadata(booking_url, config, session)

    headers = {"User-Agent": config["user_agent"]}

    params = {
        "visit_motive_ids": meta.motive_id,
        "agenda_ids": meta.agenda_ids_str,
        "practice_ids": meta.practice_id,
        "insurance_sector": config["polling"]["insurance_sector"],
        "telehealth": str(config["polling"]["telehealth"]).lower(),
        "start_date": date.today().isoformat(),
        "limit": config["polling"]["slot_limit"],
    }

    avail_resp = session.get(
        DOCTOLIB_AVAILABILITIES_API, params=params, headers=headers, timeout=10
    )
    avail_resp.raise_for_status()
    avail_data = avail_resp.json()

    total = avail_data.get("total", 0)
    next_slot = avail_data.get("next_slot")

    first_date = "N/A"
    if total > 0:
        availabilities = avail_data.get("availabilities", [])
        found = False
        if availabilities:
            # Find the first date that actually contains a slot
            for day_info in availabilities:
                slots = day_info.get("slots", [])
                if slots:
                    date_str = day_info.get("date", "N/A")
                    
                    # Handle both string and dict slot formats from Doctolib
                    first_slot = slots[0]
                    if isinstance(first_slot, dict):
                        start_time_str = first_slot.get("start_time", "")
                    elif isinstance(first_slot, str):
                        start_time_str = first_slot
                    else:
                        start_time_str = ""
                        
                    time_str = ""
                    if start_time_str and "T" in start_time_str:
                        try:
                            time_str = " " + start_time_str.split("T")[1][:5]
                        except IndexError:
                            pass
                    first_date = f"{date_str}{time_str}"
                    found = True
                    break
        
        # Fallback if no slots were parsed from the list but total > 0
        if not found:
            first_date = format_doctolib_datetime(next_slot) if next_slot else "N/A"
    else:
        if next_slot:
            first_date = format_doctolib_datetime(next_slot)
        else:
            first_date = "no slots in window"

    return (
        meta.state_key,
        meta.practitioner_name,
        meta.practice_name,
        total,
        booking_url,
        first_date,
        next_slot,
    )


# ── 7. Notifications ────────────────────────────────────────────────


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


def send_telegram(config, text, silent=None, max_attempts=5):
    term_text = html_to_terminal_text(text)
    bar_color = Fore.LIGHTBLACK_EX
    bar = f"{bar_color}{'─' * 60}{Style.RESET_ALL}"
    print(f"\n{bar}")
    silent_flag = " (silent)" if (silent if silent is not None else config["telegram"]["silent"]) else ""
    print(f"{bar_color} outgoing telegram{silent_flag} {Style.RESET_ALL}")
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
    
    # Resolve silent flag: explicit override > message-specific > global default
    is_silent = silent if silent is not None else config["telegram"]["silent"]
    
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": is_silent,
    }
    headers = {"User-Agent": "Doctolib-Checker/1.0", "Connection": "keep-alive"}
    session = get_session()

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
            logging.warning(
                f"Telegram send failed: {err} (attempt {attempt}/{max_attempts})"
            )

        if attempt < max_attempts:
            time.sleep(min(2 ** attempt, 32))

    return False


# ── 8. Summary / Heartbeat ──────────────────────────────────────────


def maybe_send_summary(config: dict, stats: SessionStats) -> bool:
    """Send a periodic summary message if time or cycle interval has elapsed.

    Returns True if a message was sent, False otherwise.
    """
    summary_cfg = config.get("messages", {}).get("summary", {})
    if not summary_cfg.get("enabled", False):
        return False

    interval_secs = summary_cfg.get("interval_seconds", 0)
    cycle_target = summary_cfg.get("every_x_cycles", 0)

    # If both triggers are explicitly disabled/zero, do nothing
    if interval_secs <= 0 and cycle_target <= 0:
        return False

    now = datetime.now()
    time_trigger_met = False
    cycle_trigger_met = False

    # 1. Evaluate Time Trigger
    if interval_secs > 0:
        if stats.last_summary_sent is None:
            time_trigger_met = True  # Always send the first one if time-based is on
        else:
            elapsed = (now - stats.last_summary_sent).total_seconds()
            if elapsed >= interval_secs:
                time_trigger_met = True

    # 2. Evaluate Cycle Trigger
    if cycle_target > 0:
        if stats.total_cycles > 0 and stats.total_cycles % cycle_target == 0:
            cycle_trigger_met = True

    # 3. Fire if either condition is met
    if not (time_trigger_met or cycle_trigger_met):
        return False

    # ── Build the last_slot_line ──
    if stats.last_slot_time:
        ago = format_ago(stats.last_slot_time)
        last_slot_line = (
            f"📅 Last slot: <b>{stats.last_slot_practitioner}</b> "
            f"@ {stats.last_slot_practice} · <code>{ago}</code>"
        )
    else:
        last_slot_line = "📅 No slots found yet this session"

    # ── Format next_check_in ──
    check_secs = config["polling"]["check_interval_seconds"]
    if check_secs >= 3600:
        h, rem = divmod(check_secs, 3600)
        m = rem // 60
        next_check_in = f"{h}h" if m == 0 else f"{h}h {m}m"
    elif check_secs >= 60:
        next_check_in = f"{check_secs // 60}m"
    else:
        next_check_in = f"{check_secs}s"

    template = summary_cfg.get("template")

    text = template.format(
        uptime=format_uptime(stats.session_start),
        total_cycles=stats.total_cycles,
        total_hits=stats.total_hits,
        total_errors=stats.total_errors,
        last_slot_line=last_slot_line,
        next_check_in=next_check_in,
    )

    # Resolve silent flag for summary (defaults to True in config)
    is_summary_silent = summary_cfg.get("silent", True)
    
    sent = send_telegram(config, text, silent=is_summary_silent)
    if sent:
        stats.last_summary_sent = now
        logging.info(
            f"{Fore.LIGHTBLACK_EX}📊 Summary dispatched "
            f"(cycle {stats.total_cycles}).{Style.RESET_ALL}"
        )
    return sent


# ── 9. Startup list renderer (grouped, NO dedupe) ──────────────────


def render_startup_list(preflight_meta, urls):
    """Group targets by practice for readability — every URL is listed."""
    groups = OrderedDict()
    for idx, url in enumerate(urls, 1):
        meta = preflight_meta.get(url)
        if not meta:
            continue
        groups.setdefault(meta.practice_name, []).append((idx, meta.practitioner_name))

    lines = []
    for practice, items in groups.items():
        short = practice if len(practice) <= 48 else practice[:45] + "…"
        lines.append(f"🏥 <b>{short}</b>")
        for i, (_idx, p) in enumerate(items, 1):
            lines.append(f"   {i}. {p}")
        lines.append("")
    return "\n".join(lines).strip()


# ── 10. Execution Cycle ─────────────────────────────────────────────


def run_once(config, state, preflight_meta, stats: SessionStats):
    session = get_session()
    total_urls = len(config["urls"])
    logging.info(
        f"{Fore.CYAN}── starting check cycle ({total_urls} target(s)){Style.RESET_ALL}"
    )

    hits = 0
    errors = 0
    empty = 0

    stats.total_cycles += 1

    for i, url in enumerate(config["urls"], 1):
        try:
            meta = preflight_meta.get(url)
            (
                state_key,
                practitioner,
                practice,
                total,
                booking_url,
                first_date,
                next_slot,
            ) = fetch_slot_total(url, config, session, meta)

            # Instance key keeps duplicates intentional
            instance_key = f"{i}_{state_key}"

            if instance_key not in state:
                state[instance_key] = {"last_total": 0, "last_notified_total": 0}

            prev_state = state[instance_key]

            # ── Terminal row (ANSI-aware padding) ──
            pract_str = (
                practitioner
                if len(strip_ansi(practitioner)) <= 26
                else truncate(practitioner, 26)
            )
            prac_str = (
                practice
                if len(strip_ansi(practice)) <= 38
                else truncate(practice, 38)
            )

            name_cell = f"{Fore.MAGENTA}{pract_str}{Style.RESET_ALL}"
            clinic_cell = f"{Fore.CYAN}{prac_str}{Style.RESET_ALL}"

            if total > 0:
                status_text = f"{Fore.GREEN}✔ {total} slot(s) available{Style.RESET_ALL}"
                hits += 1

                # Update session stats with this slot hit
                stats.last_slot_time = datetime.now()
                stats.last_slot_practitioner = practitioner
                stats.last_slot_practice = practice
                stats.last_slot_total = total
            else:
                status_text = f"{Fore.LIGHTBLACK_EX}no slots in window{Style.RESET_ALL}"
                empty += 1

            logging.info(
                f"{Fore.LIGHTBLACK_EX}[{i}/{total_urls}]{Style.RESET_ALL} "
                f"{pad_right(name_cell, 28)}  "
                f"{pad_right(clinic_cell, 40)}  →  {status_text}"
            )

            if should_notify(prev_state, total):
                msg = config["messages"]["slot_found"]["template"].format(
                    total=total,
                    practitioner=practitioner,
                    practice=practice,
                    booking_url=booking_url,
                    first_date=first_date,
                )
                logging.info(
                    f"    {Fore.YELLOW}🔔 Matches criteria! "
                    f"Dispatching notification.{Style.RESET_ALL}"
                )
                is_slot_silent = config["messages"]["slot_found"].get("silent", False)
                if send_telegram(config, msg, silent=is_slot_silent):
                    state[instance_key]["last_notified_total"] = total
                else:
                    logging.warning(
                        f"    {Fore.YELLOW}Notification failed — "
                        f"will retry next cycle.{Style.RESET_ALL}"
                    )

            elif total == 0 and prev_state.get("last_notified_total", 0) > 0:
                logging.info(
                    f"    {Fore.LIGHTBLACK_EX}Slots dropped to 0. "
                    f"Resetting notifier.{Style.RESET_ALL}"
                )
                state[instance_key]["last_notified_total"] = 0

            state[instance_key]["last_total"] = total

        except Exception as e:
            errors += 1
            err = str(e)
            if len(err) > 100:
                err = err[:97] + "…"
            logging.error(
                f"{Fore.LIGHTBLACK_EX}[{i}/{total_urls}]{Style.RESET_ALL} "
                f"{Fore.RED}error:{Style.RESET_ALL} {err}"
            )

        if i < total_urls:
            time.sleep(config["polling"]["delay_between_urls_seconds"])

    # Accumulate into session stats
    if hits > 0:
        stats.total_hits += 1
    stats.total_errors += errors

    save_state(state)
    logging.info(
        f"{Fore.CYAN}── cycle complete{Style.RESET_ALL} · "
        f"{Fore.GREEN}{hits} hit(s){Style.RESET_ALL} · "
        f"{empty} empty · "
        f"{Fore.RED}{errors} error(s){Style.RESET_ALL} · "
        f"next in {config['polling']['check_interval_seconds']}s"
    )
    return hits, errors


# ── 11. Timer & Main ────────────────────────────────────────────────


def countdown_sleep(seconds):
    try:
        for remaining in range(seconds, 0, -1):
            sys.stdout.write(
                f"\r{Fore.LIGHTBLACK_EX}next cycle in {remaining:02d}s "
                f"— Ctrl+C to stop{Style.RESET_ALL}"
            )
            sys.stdout.flush()
            time.sleep(1)
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()
    except KeyboardInterrupt:
        sys.stdout.write(
            "\r" + " " * 80
            + f"\r{Fore.YELLOW}[WARN] Interrupted. Shutting down...{Style.RESET_ALL}\n"
        )
        sys.stdout.flush()
        raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description="Doctolib Availability Checker")
    parser.add_argument(
        "--once", action="store_true", help="Run a single check cycle and exit"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Telegram sends (overrides config)",
    )
    args = parser.parse_args()

    setup_directories_and_logging()
    config = load_config()

    if args.dry_run:
        config["dry_run"] = True

    if not config["urls"]:
        return

    logging.info(f"{Style.BRIGHT}Initialized Doctolib Tracker Configuration:{Style.RESET_ALL}")
    logging.info(f"  • Interval:     {Fore.LIGHTBLUE_EX}{config['polling']['check_interval_seconds']} seconds{Style.RESET_ALL}")
    logging.info(f"  • Date window:  {Fore.LIGHTBLUE_EX}{config['polling']['upcoming_days']} days{Style.RESET_ALL}")
    logging.info(f"  • Targets:      {Fore.LIGHTBLUE_EX}{len(config['urls'])} endpoint(s){Style.RESET_ALL}")
    logging.info(
        f"  • Parameters:   {Fore.LIGHTBLUE_EX}"
        f"Insurance: {config['polling']['insurance_sector']} | "
        f"Telehealth: {config['polling']['telehealth']}{Style.RESET_ALL}"
    )
    if config.get("dry_run"):
        logging.info(
            f"  • Mode:         {Fore.YELLOW}DRY RUN (no Telegram sends){Style.RESET_ALL}"
        )
    
    # Display summary trigger info
    summary_cfg = config.get("messages", {}).get("summary", {})
    if summary_cfg.get("enabled"):
        triggers = []
        interval_secs = summary_cfg.get("interval_seconds", 0)
        cycle_target = summary_cfg.get("every_x_cycles", 0)
        
        if interval_secs > 0:
            triggers.append(f"every {format_duration(interval_secs)}")
        if cycle_target > 0:
            triggers.append(f"every {cycle_target} cycles")
        
        if triggers:
            trigger_str = " or ".join(triggers)
            silent_str = " (silent)" if summary_cfg.get("silent", True) else ""
            logging.info(
                f"  • Summary:      {Fore.LIGHTBLUE_EX}{trigger_str}{silent_str}{Style.RESET_ALL}"
            )
    print()

    session = get_session()
    logging.info(f"{Style.BRIGHT}Running Pre-flight Verification on URLs...{Style.RESET_ALL}")

    preflight_meta = {}
    valid_urls = []  # URLs that passed verification — no dedupe

    for i, url in enumerate(config["urls"], 1):
        try:
            meta = get_booking_metadata(url, config, session)
            _ = fetch_slot_total(url, config, session, meta)
            preflight_meta[url] = meta
            valid_urls.append(url)
            logging.info(
                f"{Fore.LIGHTBLACK_EX}[{i}/{len(config['urls'])}]{Style.RESET_ALL} "
                f"{Fore.GREEN}[OK]{Style.RESET_ALL} "
                f"{Fore.MAGENTA}{meta.practitioner_name}{Style.RESET_ALL} @ "
                f"{Fore.CYAN}{meta.practice_name}{Style.RESET_ALL}"
            )
        except Exception as e:
            err = str(e)
            if len(err) > 100:
                err = err[:97] + "…"
            logging.error(
                f"{Fore.LIGHTBLACK_EX}[{i}/{len(config['urls'])}]{Style.RESET_ALL} "
                f"{Fore.RED}[FAIL]{Style.RESET_ALL} {err}"
            )

        if i < len(config["urls"]):
            time.sleep(config["polling"]["delay_between_urls_seconds"])

    config["urls"] = valid_urls
    print()

    if not config["urls"]:
        logging.error("❌ No valid URLs passed verification. Exiting.")
        return

    # Initialize session stats (before --once check so run_once always receives it)
    stats = SessionStats(session_start=datetime.now())

    if args.once:
        state = load_state()
        _hits, errors = run_once(config, state, preflight_meta, stats)
        sys.exit(0 if errors == 0 else 1)

    try:
        startup_msg = config["messages"]["startup"]["template"]
        if startup_msg:
            grouped_list = render_startup_list(preflight_meta, config["urls"])
            practice_count = len({m.practice_name for m in preflight_meta.values()})
            formatted_msg = startup_msg.format(
                doctor_count=len(config["urls"]),
                practice_count=practice_count,
                practitioner_list=grouped_list,
                interval_mins=config["polling"]["check_interval_seconds"] // 60,
                days=config["polling"]["upcoming_days"],
                insurance_sector=config["polling"]["insurance_sector"],
                start_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            is_startup_silent = config["messages"]["startup"].get("silent", False)
            if send_telegram(config, formatted_msg, silent=is_startup_silent):
                logging.info(f"{Fore.YELLOW}🔔 Startup notification dispatched.{Style.RESET_ALL}")

        while True:
            state = load_state()
            run_once(config, state, preflight_meta, stats)
            maybe_send_summary(config, stats)
            countdown_sleep(config["polling"]["check_interval_seconds"])

    except KeyboardInterrupt:
        logging.info(f"\n{Fore.YELLOW}🛑 Shutting down gracefully...{Style.RESET_ALL}")
        shutdown_msg = config["messages"]["shutdown"]["template"]
        if shutdown_msg:
            is_shutdown_silent = config["messages"]["shutdown"].get("silent", False)
            send_telegram(config, shutdown_msg, silent=is_shutdown_silent)


if __name__ == "__main__":
    main()