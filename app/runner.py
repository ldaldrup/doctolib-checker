import argparse
import logging
import sys
import time
from datetime import datetime

from colorama import Fore, Style

from app.config import load_config
from app.doctolib import fetch_slot_total, get_booking_metadata, get_session
from app.logging_utils import setup_directories_and_logging, truncate
from app.loop import countdown_sleep, maybe_send_summary, render_startup_list, run_once
from app.models import SessionStats
from app.notifications import send_telegram
from app.state import load_state, save_state


def format_duration(seconds: int) -> str:
    if seconds >= 3600:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h" if m == 0 else f"{h}h {m}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Doctolib Availability Checker")
    parser.add_argument("--once", action="store_true", help="Run a single check cycle and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Telegram sends (overrides config)",
    )
    args = parser.parse_args(argv)

    setup_directories_and_logging()
    config = load_config()

    if args.dry_run:
        config["dry_run"] = True

    if not config["urls"]:
        return 0

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
    valid_urls = []

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
        return 1

    stats = SessionStats(session_start=datetime.now())
    state = load_state()
    for key in state:
        if isinstance(state[key], dict):
            state[key]["last_notified_total"] = 0
            state[key]["last_notified_far_date"] = None
    save_state(state)

    if args.once:
        _hits, errors = run_once(config, state, preflight_meta, stats)
        return 0 if errors == 0 else 1

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
            run_once(config, state, preflight_meta, stats)
            maybe_send_summary(config, stats)
            countdown_sleep(config["polling"]["check_interval_seconds"])
    except KeyboardInterrupt:
        logging.info(f"\n{Fore.YELLOW}🛑 Shutting down gracefully...{Style.RESET_ALL}")
        shutdown_msg = config["messages"]["shutdown"]["template"]
        if shutdown_msg:
            is_shutdown_silent = config["messages"]["shutdown"].get("silent", False)
            send_telegram(config, shutdown_msg, silent=is_shutdown_silent)

    return 0


if __name__ == "__main__":
    sys.exit(main())
