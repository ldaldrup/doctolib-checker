import logging
import sys
import time
from collections import OrderedDict
from datetime import datetime

from colorama import Fore, Style

from app.doctolib import fetch_slot_total
from app.logging_utils import pad_right, strip_ansi, truncate
from app.models import SessionStats
from app.notifications import send_telegram
from app.state import save_state


def format_uptime(start: datetime) -> str:
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
    if seconds >= 3600:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h" if m == 0 else f"{h}h {m}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def maybe_send_summary(config: dict, stats: SessionStats) -> bool:
    summary_cfg = config.get("messages", {}).get("summary", {})
    if not summary_cfg.get("enabled", False):
        return False

    interval_secs = summary_cfg.get("interval_seconds", 0)
    cycle_target = summary_cfg.get("every_x_cycles", 0)

    if interval_secs <= 0 and cycle_target <= 0:
        return False

    now = datetime.now()
    time_trigger_met = False
    cycle_trigger_met = False

    if interval_secs > 0:
        if stats.last_summary_sent is None:
            time_trigger_met = True
        else:
            elapsed = (now - stats.last_summary_sent).total_seconds()
            if elapsed >= interval_secs:
                time_trigger_met = True

    if cycle_target > 0:
        if stats.total_cycles > 0 and stats.total_cycles % cycle_target == 0:
            cycle_trigger_met = True

    if not (time_trigger_met or cycle_trigger_met):
        return False

    if stats.last_slot_time:
        ago = format_ago(stats.last_slot_time)
        last_slot_line = (
            f"📅 Last slot: <b>{stats.last_slot_practitioner}</b> "
            f"@ {stats.last_slot_practice} · <code>{ago}</code>"
        )
    else:
        last_slot_line = "📅 No slots found yet this session"

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

    is_summary_silent = summary_cfg.get("silent", True)
    sent = send_telegram(config, text, silent=is_summary_silent)
    if sent:
        stats.last_summary_sent = now
        logging.info(
            f"{Fore.LIGHTBLACK_EX}📊 Summary dispatched "
            f"(cycle {stats.total_cycles}).{Style.RESET_ALL}"
        )
    return sent


def render_startup_list(preflight_meta, urls):
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


def run_once(config, state, preflight_meta, stats: SessionStats):
    from app.doctolib import get_session

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
                is_far_slot,
            ) = fetch_slot_total(url, config, session, meta)

            instance_key = f"{i}_{state_key}"

            if instance_key not in state:
                state[instance_key] = {
                    "last_total": 0,
                    "last_notified_total": 0,
                    "last_notified_far_date": None,
                }

            prev_state = state[instance_key]

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

            from app.notifications import should_notify

            if total > 0 and not is_far_slot:
                if should_notify(prev_state, total):
                    msg_cfg = config["messages"]["slot_found"]
                    msg = msg_cfg["template"].format(
                        total=total,
                        practitioner=practitioner,
                        practice=practice,
                        booking_url=booking_url,
                        first_date=first_date,
                    )
                    logging.info(
                        f"    {Fore.YELLOW}🔔 Imminent slot! "
                        f"Dispatching notification.{Style.RESET_ALL}"
                    )

                    is_slot_silent = msg_cfg.get("silent", False)
                    effect_cfg = msg_cfg.get("effect", {})
                    effect_id = effect_cfg.get("id") if effect_cfg.get("enabled") else None

                    if send_telegram(config, msg, silent=is_slot_silent, effect_id=effect_id):
                        state[instance_key]["last_notified_total"] = total

            elif total > 0 and is_far_slot:
                prev_far_date = prev_state.get("last_notified_far_date")
                if first_date != prev_far_date:
                    msg_cfg = config["messages"]["far_slot_found"]
                    msg = msg_cfg["template"].format(
                        total=total,
                        practitioner=practitioner,
                        practice=practice,
                        booking_url=booking_url,
                        first_date=first_date,
                    )
                    logging.info(
                        f"    {Fore.YELLOW}📆 Far calendar slot opened! "
                        f"Dispatching notification.{Style.RESET_ALL}"
                    )

                    is_slot_silent = msg_cfg.get("silent", False)
                    effect_cfg = msg_cfg.get("effect", {})
                    effect_id = effect_cfg.get("id") if effect_cfg.get("enabled") else None

                    if send_telegram(config, msg, silent=is_slot_silent, effect_id=effect_id):
                        state[instance_key]["last_notified_far_date"] = first_date
            else:
                if prev_state.get("last_notified_far_date"):
                    state[instance_key]["last_notified_far_date"] = None
                elif prev_state.get("last_notified_total", 0) > 0:
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
