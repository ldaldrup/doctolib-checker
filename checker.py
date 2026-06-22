import os
import sys
import time
import json
import logging
import argparse
import urllib.parse
import re
from datetime import date
from dataclasses import dataclass
import requests
from colorama import init, Fore, Style
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Initialize colorama
init(autoreset=True)

# --- 1. Constants & Paths ---

STATE_DIR = 'state'
STATE_FILE = os.path.join(STATE_DIR, 'state.json')
LOG_DIR = 'logs'
LOG_FILE = os.path.join(LOG_DIR, 'checker.log')
CONFIG_FILE = 'config.json'

DOCTOLIB_BASE_URL = "https://www.doctolib.de"
DOCTOLIB_INFO_API = f"{DOCTOLIB_BASE_URL}/online_booking/api/slot_selection_funnel/v1/info.json"
DOCTOLIB_AVAILABILITIES_API = f"{DOCTOLIB_BASE_URL}/availabilities.json"
TELEGRAM_API_BASE = "https://api.telegram.org"

# --- 2. Logging Setup with Color Support ---

class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: Style.DIM + Fore.CYAN,
        logging.INFO: Fore.WHITE,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Style.BRIGHT + Fore.RED,
    }

    def format(self, record):
        log_color = self.COLORS.get(record.levelno, Fore.WHITE)
        time_str = f"{Fore.LIGHTBLACK_EX}%(asctime)s{Style.RESET_ALL}"
        level_str = f"{log_color}[%(levelname)s]{Style.RESET_ALL}"
        msg_str = f"{log_color}%(message)s{Style.RESET_ALL}"
        formatter = logging.Formatter(f"{time_str} {level_str} {msg_str}", datefmt="%H:%M:%S")
        return formatter.format(record)

def setup_directories_and_logging():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(ColorFormatter())

    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])

def load_config():
    if not os.path.exists(CONFIG_FILE):
        logging.error(f"Configuration file '{CONFIG_FILE}' not found!")
        sys.exit(1)

    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        try:
            config = json.load(f)
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse '{CONFIG_FILE}'. Please check for JSON syntax errors: {e}")
            sys.exit(1)
            
    # Parameterization Defaults
    config.setdefault('check_interval_seconds', 300)
    config.setdefault('delay_between_urls_seconds', 3)
    config.setdefault('upcoming_days', 15)
    config.setdefault('insurance_sector', 'public')
    config.setdefault('telehealth', False)
    config.setdefault('slot_limit', 15)
    
    config.setdefault('startup_message', 
        "🚀 <b>Doctolib Checker Started</b>\n\n"
        "👥 Monitoring <b>{doctor_count}</b> target(s):\n"
        "{practitioner_list}\n\n"
        "⏱ Interval: <code>{interval_mins}m</code>\n"
        "📅 Window: <code>{days}d</code>"
    )
    config.setdefault('shutdown_message', "🛑 <b>Doctolib Checker Stopped</b>\n\nMonitoring has been disabled.")
    config.setdefault('message_template', 
        "🎉 <b>{total} Slot(s) Found!</b>\n\n"
        "👨‍⚕️ Practitioner: <b>{practitioner}</b>\n"
        "🏥 Practice: <b>{practice}</b>\n"
        "📅 First Date: <b>{first_date}</b>\n\n"
        "👉 <a href='{booking_url}'>Click here to book now!</a>"
    )
    config.setdefault('user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

    if not config.get('telegram_bot_token') or not config.get('telegram_chat_id'):
        logging.warning("Telegram credentials missing in config. Alerts will fail.")
        
    if not config.get('urls'):
        logging.warning("No URLs configured in config.json.")
        
    return config

# --- 3. State Management ---

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logging.error("Corrupted state.json. Starting fresh.")
            return {}
    return {}

def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)

# --- 4. Session & Dataclasses ---

GLOBAL_SESSION = None

def get_session():
    global GLOBAL_SESSION
    if GLOBAL_SESSION is None:
        session = requests.Session()
        retry_strategy = Retry(
            total=5,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=5, pool_maxsize=5)
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

# --- 5. Doctolib API Fetching ---

def get_booking_metadata(booking_url, config, session):
    parsed = urllib.parse.urlparse(booking_url)
    path_parts = parsed.path.split('/')

    try:
        slug_idx = path_parts.index('booking') - 1
        slug = path_parts[slug_idx]
    except ValueError:
        slug = path_parts[3]

    query_params = urllib.parse.parse_qs(parsed.query)

    try:
        raw_place_id = query_params.get('placeId', [None])[0]
        practice_id = raw_place_id.split('-')[1] if raw_place_id and '-' in raw_place_id else raw_place_id
        motive_id = query_params.get('motiveIds[]', [None])[0]
        practitioner_id = query_params.get('practitionerId', [None])[0]
    except (TypeError, IndexError, AttributeError) as e:
        raise ValueError(f"Could not parse required IDs from URL parameters: {e}")

    headers = {'User-Agent': config['user_agent']}

    info_resp = session.get(DOCTOLIB_INFO_API, params={'profile_slug': slug}, headers=headers, timeout=10)
    info_resp.raise_for_status()
    info_data = info_resp.json().get('data', {})

    agendas = info_data.get('agendas', [])
    practitioners = info_data.get('practitioners', [])
    
    # Better extraction of the entity name instead of the raw slug
    profile = info_data.get('profile', {})
    profile_name = profile.get('name_with_title') or profile.get('name') or slug
    practice_name = profile_name

    practitioner_name = None

    if practitioner_id and practitioner_id != 'NO_PREFERENCE':
        for p in practitioners:
            if str(p.get('id')) == str(practitioner_id):
                p_name = p.get('name') or p.get('full_name') or p.get('display_name')
                if p_name:
                    practitioner_name = p_name
                else:
                    first_name = p.get('first_name', '')
                    last_name = p.get('last_name', '')
                    practitioner_name = f"{first_name} {last_name}".strip()
                break
        if not practitioner_name:
            practitioner_name = f"Practitioner (ID: {practitioner_id})"
    else:
        # If no practitioner ID is provided, the target is whoever the profile resolves to.
        practitioner_name = "Any Practitioner"

    valid_agenda_ids = []
    for agenda in agendas:
        if practice_id and str(agenda.get('practice_id')) != str(practice_id):
            continue
        if motive_id and int(motive_id) not in agenda.get('visit_motive_ids', []):
            continue
        if practitioner_id and practitioner_id != 'NO_PREFERENCE':
            if str(agenda.get('practitioner_id')) != str(practitioner_id):
                continue
        valid_agenda_ids.append(str(agenda['id']))

    if not valid_agenda_ids:
        raise ValueError(f"No specific agenda found for motive={motive_id}, practitioner={practitioner_id}")

    # The state key still uses slug so it remains unique across configurations
    state_key = f"{slug}_{practitioner_id}" if practitioner_id else slug
    agenda_ids_str = "-".join(valid_agenda_ids)

    return BookingMeta(
        state_key=state_key,
        practice_name=practice_name,
        practitioner_name=practitioner_name,
        motive_id=motive_id,
        agenda_ids_str=agenda_ids_str,
        practice_id=practice_id
    )

def fetch_slot_total(booking_url, config, session, meta=None):
    if meta is None:
        meta = get_booking_metadata(booking_url, config, session)
        
    headers = {'User-Agent': config['user_agent']}

    params = {
        'visit_motive_ids': meta.motive_id,
        'agenda_ids': meta.agenda_ids_str,
        'practice_ids': meta.practice_id,
        'insurance_sector': config['insurance_sector'],
        'telehealth': str(config['telehealth']).lower(),
        'start_date': date.today().isoformat(),
        'limit': config['slot_limit']
    }

    avail_resp = session.get(DOCTOLIB_AVAILABILITIES_API, params=params, headers=headers, timeout=10)
    avail_resp.raise_for_status()
    avail_data = avail_resp.json()

    total = avail_data.get('total', 0)
    next_slot = avail_data.get('next_slot') or 'None listed'
    
    first_date = "N/A"
    if total > 0:
        availabilities = avail_data.get('availabilities', [])
        if availabilities:
            first_date = availabilities[0].get('date', 'N/A')
    else:
        first_date = next_slot

    return meta.state_key, meta.practitioner_name, meta.practice_name, total, booking_url, first_date, next_slot

# --- 6. Notifications & Telegram ---

def should_notify(prev_state, new_total):
    prev_notified = prev_state.get('last_notified_total', 0)
    if new_total > 0 and (prev_notified == 0 or new_total > prev_notified):
        return True
    return False

def html_to_terminal_text(html_str):
    def replace_link(match):
        url = match.group(1)
        text = match.group(2)
        return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"

    text = re.sub(r'<a\s+href=[\'"]([^\'"]+)[\'"]>(.*?)</a>', replace_link, html_str)
    text = re.sub(r'</?(b|i|strong|em|code)>', '', text)
    return text

def send_telegram(config, text, max_attempts=5):
    # Lightweight terminal echo without rigid box borders
    term_text = html_to_terminal_text(text)
    print(f"\n{Fore.BLUE}--- Outgoing Telegram Message ---{Style.RESET_ALL}")
    for line in term_text.split('\n'):
        print(f"{Fore.BLUE}  {line}{Style.RESET_ALL}")
    print(f"{Fore.BLUE}---------------------------------{Style.RESET_ALL}\n")

    token = config.get('telegram_bot_token')
    chat_id = config.get('telegram_chat_id')
    if not token or not chat_id:
        logging.warning("Telegram credentials missing. Skipping API call.")
        return False

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    headers = {"User-Agent": "Doctolib-Checker/1.0", "Connection": "keep-alive"}
    session = get_session()

    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.post(url, json=payload, headers=headers, timeout=30)
            if resp.status_code == 200:
                return True
            logging.warning(f"Telegram API returned {resp.status_code} (attempt {attempt}/{max_attempts})")
        except Exception as e:
            logging.error(f"Telegram message error (attempt {attempt}/{max_attempts}): {e}")

        if attempt < max_attempts:
            time.sleep(min(2 ** attempt, 32))

    return False

# --- 7. Execution Cycle ---

def run_once(config, state, preflight_meta):
    session = get_session()
    logging.info(f"{Fore.CYAN}--- Starting Check Cycle for {len(config['urls'])} Target(s) ---{Style.RESET_ALL}")

    for i, url in enumerate(config['urls'], 1):
        try:
            meta = preflight_meta.get(url)
            state_key, practitioner, practice, total, booking_url, first_date, next_slot = fetch_slot_total(url, config, session, meta)
            
            # Using the instance identifier since user config may have intentional duplicates
            instance_key = f"{i}_{state_key}"
            
            if instance_key not in state:
                state[instance_key] = {'last_total': 0, 'last_notified_total': 0}
                
            prev_state = state[instance_key]

            # Better Layout Output
            pract_str = practitioner if len(practitioner) <= 24 else practitioner[:21] + "..."
            prac_str = practice if len(practice) <= 24 else practice[:21] + "..."
            
            if total > 0:
                status_text = f"{Fore.GREEN}✔ {total} slot(s) available!{Style.RESET_ALL}"
            else:
                status_text = f"{Fore.LIGHTBLACK_EX}✘ 0 slots (in {config['upcoming_days']}d). Next: {next_slot}{Style.RESET_ALL}"

            logging.info(f"[{i}/{len(config['urls'])}] {Fore.LIGHTMAGENTA_EX}{pract_str:<25}{Style.RESET_ALL} | {Fore.LIGHTCYAN_EX}{prac_str:<25}{Style.RESET_ALL} -> {status_text}")

            if should_notify(prev_state, total):
                msg = config['message_template'].format(
                    total=total,
                    practitioner=practitioner,
                    practice=practice,
                    booking_url=booking_url,
                    first_date=first_date
                )
                logging.info(f"    {Fore.YELLOW}🔔 Matches criteria! Dispatching notification.{Style.RESET_ALL}")
                if send_telegram(config, msg):
                    state[instance_key]['last_notified_total'] = total
                else:
                    logging.warning(f"    {Fore.YELLOW}Notification failed — will retry next cycle.{Style.RESET_ALL}")
                
            elif total == 0 and prev_state.get('last_notified_total', 0) > 0:
                logging.info(f"    {Fore.LIGHTBLACK_EX}Slots dropped to 0. Resetting notifier.{Style.RESET_ALL}")
                state[instance_key]['last_notified_total'] = 0

            state[instance_key]['last_total'] = total
            
        except Exception as e:
            logging.error(f"[{i}/{len(config['urls'])}] Error processing URL: {e}")

        if i < len(config['urls']):
            time.sleep(config['delay_between_urls_seconds'])

    save_state(state)
    logging.info(f"{Fore.CYAN}--- Check Cycle Complete ---{Style.RESET_ALL}")

# --- 8. Dynamic Timer & Loops ---

def countdown_sleep(seconds):
    try:
        for remaining in range(seconds, 0, -1):
            sys.stdout.write(f"\r{Fore.LIGHTBLACK_EX}[INFO] Next check cycle in {remaining:02d}s... Press Ctrl+C to stop.{Style.RESET_ALL}")
            sys.stdout.flush()
            time.sleep(1)
        sys.stdout.write("\r" + " " * 75 + "\r")
        sys.stdout.flush()
    except KeyboardInterrupt:
        sys.stdout.write("\r" + " " * 75 + f"\r{Fore.YELLOW}[WARN] Interrupted. Shutting down...{Style.RESET_ALL}\n")
        sys.stdout.flush()
        raise KeyboardInterrupt

def main():
    parser = argparse.ArgumentParser(description="Doctolib Availability Checker")
    parser.add_argument('--once', action='store_true', help="Run a single check and exit")
    args = parser.parse_args()

    setup_directories_and_logging()
    config = load_config()

    if not config['urls']:
        return

    logging.info(f"{Style.BRIGHT}Initialized Doctolib Tracker Configuration:{Style.RESET_ALL}")
    logging.info(f"  • Interval:     {Fore.LIGHTBLUE_EX}{config['check_interval_seconds']} seconds{Style.RESET_ALL}")
    logging.info(f"  • Date window:  {Fore.LIGHTBLUE_EX}{config['upcoming_days']} days{Style.RESET_ALL}")
    logging.info(f"  • Targets:      {Fore.LIGHTBLUE_EX}{len(config['urls'])} endpoint(s){Style.RESET_ALL}")
    logging.info(f"  • Parameters:   {Fore.LIGHTBLUE_EX}Insurance: {config['insurance_sector']} | Telehealth: {config['telehealth']}{Style.RESET_ALL}")
    print()

    session = get_session()
    logging.info(f"{Style.BRIGHT}Running Pre-flight Verification on URLs...{Style.RESET_ALL}")
    
    practitioner_list_text = ""
    active_urls = []
    preflight_meta = {}
    
    for i, url in enumerate(config['urls'], 1):
        try:
            meta = get_booking_metadata(url, config, session)
            _ = fetch_slot_total(url, config, session, meta)
            
            # Using clean names resolved from the API
            practitioner_list_text += f"• {meta.practitioner_name} ({meta.practice_name})\n"
            active_urls.append(url)
            preflight_meta[url] = meta
            logging.info(f"[{i}/{len(config['urls'])}] {Fore.GREEN}[OK]{Style.RESET_ALL} JSON verified for {meta.practitioner_name}")
        except Exception as e:
            logging.error(f"[{i}/{len(config['urls'])}] {Fore.RED}[FAIL]{Style.RESET_ALL} Verification failed: {e}")
        
        if i < len(config['urls']):
            time.sleep(config['delay_between_urls_seconds'])

    config['urls'] = active_urls
    print()
    
    if not config['urls']:
        logging.error("❌ No valid URLs passed verification. Exiting.")
        return

    if args.once:
        state = load_state()
        run_once(config, state, preflight_meta)
        return

    try:
        startup_msg = config.get('startup_message')
        if startup_msg:
            formatted_msg = startup_msg.format(
                doctor_count=len(config['urls']), 
                practitioner_list=practitioner_list_text.strip(),
                interval_mins=config['check_interval_seconds'] // 60,
                days=config['upcoming_days']
            )
            if send_telegram(config, formatted_msg):
                logging.info(f"{Fore.YELLOW}🔔 Startup notification dispatched.{Style.RESET_ALL}\n")

        while True:
            state = load_state()
            run_once(config, state, preflight_meta)
            countdown_sleep(config['check_interval_seconds'])
            
    except KeyboardInterrupt:
        logging.info(f"\n{Fore.YELLOW}🛑 Shutting down gracefully...{Style.RESET_ALL}")
        shutdown_msg = config.get('shutdown_message')
        if shutdown_msg:
            send_telegram(config, shutdown_msg)

if __name__ == '__main__':
    main()