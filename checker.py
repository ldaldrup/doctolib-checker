import os
import sys
import time
import json
import logging
import argparse
import urllib.parse
from datetime import date
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

# --- 2. Logging Setup with Color Support ---

class ColorFormatter(logging.Formatter):
    """Custom formatter to add colors to the console output"""
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
        logging.info(f"Please copy 'config.example.json' to '{CONFIG_FILE}' and fill in your details.")
        sys.exit(1)

    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        try:
            config = json.load(f)
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse '{CONFIG_FILE}'. Please check for JSON syntax errors: {e}")
            sys.exit(1)
            
    # Legacy config migration string replacements
    msg_template = config.get('message_template', '')
    if '{clinic}' in msg_template:
        config['message_template'] = msg_template.replace('{clinic}', '{practice}')
        config['message_template'] = config['message_template'].replace('Surgeon', 'Practitioner')

    # Fallback configuration variables
    config.setdefault('check_interval_seconds', 300)
    config.setdefault('delay_between_urls_seconds', 3)
    config.setdefault('upcoming_days', 15)
    config.setdefault('startup_message', '🚀 <b>Doctolib Checker Started!</b>\nMonitoring {doctor_count} practitioner(s):\n{practitioner_list}')
    config.setdefault('shutdown_message', '🛑 <b>Doctolib Checker Stopped!</b>\nNo longer monitoring availabilities.')
    config.setdefault('message_template', "🎉 <b>{total} slot(s) available!</b>\n👨‍⚕️ Practitioner: <b>{practitioner}</b>\n🏥 Practice: <b>{practice}</b>\n🔗 <a href='{booking_url}'>Click here to book now!</a>")
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

# --- 4. Doctolib API Fetching ---

def get_booking_metadata(booking_url, config):
    """Fetches practice and practitioner info from info.json API."""
    parsed = urllib.parse.urlparse(booking_url)
    path_parts = parsed.path.split('/')

    if 'availabilities' not in path_parts:
        logging.warning(f"URL might be incomplete (missing '/booking/availabilities')")

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

    info_url = f"https://www.doctolib.de/online_booking/api/slot_selection_funnel/v1/info.json?profile_slug={slug}"
    info_resp = requests.get(info_url, headers=headers, timeout=10)
    info_resp.raise_for_status()
    info_data = info_resp.json().get('data', {})

    agendas = info_data.get('agendas', [])
    practitioners = info_data.get('practitioners', [])
    profile_name = info_data.get('profile', {}).get('name') or slug

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
                    practitioner_name = f"Dr. {first_name} {last_name}".strip()
                break
        if not practitioner_name:
            practitioner_name = f"Practitioner (ID: {practitioner_id})"
    else:
        practitioner_name = "First Available (No Preference)"

    # Filter to find matching agendas
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

    state_key = f"{slug}_{practitioner_id}" if practitioner_id else slug
    agenda_ids_str = "-".join(valid_agenda_ids)

    return {
        'state_key': state_key,
        'practice_name': practice_name,
        'practitioner_name': practitioner_name,
        'motive_id': motive_id,
        'agenda_ids_str': agenda_ids_str,
        'practice_id': practice_id
    }


def fetch_slot_total(booking_url, config):
    """Fully polls availabilities using extracted metadata."""
    meta = get_booking_metadata(booking_url, config)
    headers = {'User-Agent': config['user_agent']}

    avail_url = "https://www.doctolib.de/availabilities.json"
    params = {
        'visit_motive_ids': meta['motive_id'],
        'agenda_ids': meta['agenda_ids_str'],
        'practice_ids': meta['practice_id'],
        'insurance_sector': 'public',
        'telehealth': 'false',
        'start_date': date.today().isoformat(),
        'limit': config['upcoming_days']
    }

    avail_resp = requests.get(avail_url, params=params, headers=headers, timeout=10)
    avail_resp.raise_for_status()
    avail_data = avail_resp.json()

    return meta['state_key'], meta['practitioner_name'], meta['practice_name'], avail_data.get('total', 0), booking_url

# --- 5. Notifications & Telegram ---

def create_session():
    """Create a requests.Session with retry strategy and connection pooling."""
    session = requests.Session()
    retry_strategy = Retry(
        total=5,                    # max retry attempts
        backoff_factor=2,           # exponential: 2, 4, 8, 16, 32 seconds
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=1, pool_maxsize=1)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# Create a module-level session for connection reuse
TELEGRAM_SESSION = None


def get_telegram_session():
    global TELEGRAM_SESSION
    if TELEGRAM_SESSION is None:
        TELEGRAM_SESSION = create_session()
    return TELEGRAM_SESSION


def should_notify(prev_state, new_total):
    prev_notified = prev_state.get('last_notified_total', 0)
    if new_total > 0 and (prev_notified == 0 or new_total > prev_notified):
        return True
    return False


def send_telegram(config, text, max_attempts=5):
    """Send a Telegram message with manual retry on connection errors."""
    token = config.get('telegram_bot_token')
    chat_id = config.get('telegram_chat_id')
    if not token or not chat_id:
        logging.warning("Telegram credentials missing. Skipping notification.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    headers = {
        "User-Agent": "Doctolib-Checker/1.0",
        "Connection": "keep-alive",
    }

    session = get_telegram_session()

    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.post(url, json=payload, headers=headers, timeout=30)

            # Check for Telegram API errors (e.g., bad token, bad chat_id)
            if resp.status_code != 200:
                try:
                    err_data = resp.json()
                    err_desc = err_data.get('description', 'Unknown error')
                except Exception:
                    err_desc = resp.text[:200]

                # 401/404 = permanent errors, no point retrying
                if resp.status_code in (401, 403, 404):
                    logging.error(f"Telegram API permanent error ({resp.status_code}): {err_desc}")
                    logging.error("Check your bot token and chat ID in config.json.")
                    return False

                logging.warning(f"Telegram API returned {resp.status_code}: {err_desc} (attempt {attempt}/{max_attempts})")
            else:
                return True  # success

        except (requests.exceptions.ConnectionError, ConnectionResetError) as e:
            logging.warning(f"Connection error on attempt {attempt}/{max_attempts}: {e}")
        except requests.exceptions.Timeout:
            logging.warning(f"Timeout on attempt {attempt}/{max_attempts}")
        except Exception as e:
            logging.error(f"Unexpected error sending Telegram message (attempt {attempt}/{max_attempts}): {e}")

        # If this wasn't the last attempt, wait before retrying
        if attempt < max_attempts:
            wait_time = min(2 ** attempt, 32)  # 2, 4, 8, 16, 32 (capped)
            logging.info(f"  Retrying in {wait_time}s...")
            time.sleep(wait_time)

    logging.error(f"Failed to send Telegram message after {max_attempts} attempts.")
    return False

# --- 6. Execution Cycle ---

def run_once(config, state):
    logging.info(f"{Fore.CYAN}--- Starting Check Cycle for {len(config['urls'])} Practitioner(s) ---{Style.RESET_ALL}")

    for i, url in enumerate(config['urls'], 1):
        try:
            state_key, practitioner, practice, total, booking_url = fetch_slot_total(url, config)
            
            if state_key not in state:
                state[state_key] = {'last_total': 0, 'last_notified_total': 0}
                
            prev_state = state[state_key]

            if total > 0:
                status_text = f"{Fore.GREEN}✔ {total} slot(s) available!{Style.RESET_ALL}"
            else:
                status_text = f"{Fore.LIGHTBLACK_EX}✘ No slots.{Style.RESET_ALL}"

            short_practice = practice if len(practice) <= 20 else practice[:17] + "..."
            logging.info(f"[{i}/{len(config['urls'])}] {Fore.LIGHTCYAN_EX}{short_practice:<20}{Style.RESET_ALL} | {Fore.LIGHTMAGENTA_EX}{practitioner:<32}{Style.RESET_ALL} -> {status_text}")

            if should_notify(prev_state, total):
                msg = config['message_template'].format(
                    total=total,
                    practitioner=practitioner,
                    practice=practice,
                    booking_url=booking_url
                )
                logging.info(f"    {Fore.YELLOW}🔔 Matches criteria! Dispatching Telegram notification.{Style.RESET_ALL}")
                if send_telegram(config, msg):
                    state[state_key]['last_notified_total'] = total
                else:
                    logging.warning(f"    {Fore.YELLOW}Notification failed — will retry next cycle.{Style.RESET_ALL}")
                
            elif total == 0 and prev_state.get('last_notified_total', 0) > 0:
                logging.info(f"    {Fore.LIGHTBLACK_EX}Slots dropped to 0. Resetting notifier.{Style.RESET_ALL}")
                state[state_key]['last_notified_total'] = 0

            state[state_key]['last_total'] = total
            
        except requests.exceptions.RequestException as e:
            logging.error(f"[{i}/{len(config['urls'])}] Connection error: {e}")
        except Exception as e:
            logging.error(f"[{i}/{len(config['urls'])}] Error processing URL: {e}")

        if i < len(config['urls']):
            delay = config['delay_between_urls_seconds']
            logging.info(f"    {Fore.LIGHTBLACK_EX}Pausing {delay}s before checking the next practitioner...{Style.RESET_ALL}")
            time.sleep(delay)

    save_state(state)
    logging.info(f"{Fore.CYAN}--- Check Cycle Complete ---{Style.RESET_ALL}")

# --- 7. Dynamic Timer & Loops ---

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
        logging.warning("No URLs are configured. Open config.json to add them.")
        return

    logging.info(f"{Style.BRIGHT}Initialized Doctolib Tracker Configuration:{Style.RESET_ALL}")
    logging.info(f"  • Interval:     {Fore.LIGHTBLUE_EX}{config['check_interval_seconds']} seconds{Style.RESET_ALL}")
    logging.info(f"  • Pacing delay: {Fore.LIGHTBLUE_EX}{config['delay_between_urls_seconds']} seconds{Style.RESET_ALL}")
    logging.info(f"  • Date window:  {Fore.LIGHTBLUE_EX}{config['upcoming_days']} days{Style.RESET_ALL}")
    logging.info(f"  • Targets:      {Fore.LIGHTBLUE_EX}{len(config['urls'])} practitioner(s){Style.RESET_ALL}")
    print()

    if args.once:
        state = load_state()
        run_once(config, state)
        return

    try:
        # Pre-flight check to build the practitioner list for the startup notification
        logging.info(f"{Style.BRIGHT}Fetching practice and practitioner details for startup message...{Style.RESET_ALL}")
        practitioner_list_text = ""
        for i, url in enumerate(config['urls']):
            try:
                meta = get_booking_metadata(url, config)
                practitioner_list_text += f"• {meta['practitioner_name']} ({meta['practice_name']})\n"
            except Exception as e:
                practitioner_list_text += f"• Unknown Practitioner (URL {i+1})\n"
                logging.debug(f"Error fetching metadata for URL {i+1}: {e}")
            
            if i < len(config['urls']) - 1:
                time.sleep(config['delay_between_urls_seconds'])

        startup_msg = config.get('startup_message')
        if startup_msg:
            try:
                formatted_msg = startup_msg.format(
                    doctor_count=len(config['urls']), 
                    practitioner_list=practitioner_list_text.strip()
                )
                if send_telegram(config, formatted_msg):
                    logging.info(f"{Fore.YELLOW}🔔 Startup notification sent to Telegram.{Style.RESET_ALL}\n")
                else:
                    logging.error(f"Failed to send startup notification.\n")
            except Exception as e:
                logging.error(f"Failed to format startup notification: {e}\n")

        # Primary polling loop
        while True:
            state = load_state()
            run_once(config, state)
            countdown_sleep(config['check_interval_seconds'])
            
    except KeyboardInterrupt:
        logging.info(f"\n{Fore.YELLOW}🛑 Shutting down gracefully...{Style.RESET_ALL}")
        shutdown_msg = config.get('shutdown_message')
        if shutdown_msg:
            logging.info(f"{Fore.YELLOW}Sending shutdown notification to Telegram...{Style.RESET_ALL}")
            send_telegram(config, shutdown_msg)

if __name__ == '__main__':
    main()