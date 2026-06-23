import os
import json
import re
import urllib.parse
from datetime import date, datetime

import requests

CONFIG_FILE = "config.json"


def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"Error: {CONFIG_FILE} not found.")
        exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def sanitize_filename(name):
    return re.sub(r"(?u)[^-\w.]", "_", name).strip("_")


def _practitioner_display_name(p):
    if not p:
        return None
    return (
        p.get("name")
        or p.get("full_name")
        or p.get("display_name")
        or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        or None
    )


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


def main():
    config = load_config()

    if not config.get("urls"):
        print("Error: No URLs found in config.json.")
        exit(1)

    target_url = config["urls"][0]
    print(f"Targeting URL: {target_url}\n")

    parsed_url = urllib.parse.urlparse(target_url)
    path_parts = parsed_url.path.split("/")

    try:
        slug_idx = path_parts.index("booking") - 1
        slug = path_parts[slug_idx]
    except ValueError:
        print("Error: Could not identify profile slug from URL.")
        exit(1)

    query_params = urllib.parse.parse_qs(parsed_url.query)
    raw_place_id = query_params.get("placeId", [None])[0]
    practice_id = (
        raw_place_id.split("-")[1] if raw_place_id and "-" in raw_place_id else raw_place_id
    )
    motive_id = query_params.get("motiveIds[]", query_params.get("motiveIds", [None]))[0]
    practitioner_id = query_params.get(
        "practitionerId", query_params.get("practitioner_id", [None])
    )[0]

    headers = {
        "User-Agent": config.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
        )
    }

    polling_cfg = config.get("polling", {})
    upcoming_days = polling_cfg.get("upcoming_days", 365)

    print(f"Fetching metadata for '{slug}'...")
    info_url = "https://www.doctolib.de/online_booking/api/slot_selection_funnel/v1/info.json"
    info_resp = requests.get(info_url, params={"profile_slug": slug}, headers=headers)
    info_resp.raise_for_status()
    info_data = info_resp.json()

    profile_data = info_data.get("data", {})
    profile = profile_data.get("profile", {})
    practice_name = profile.get("name_with_title") or profile.get("name") or slug

    agendas = profile_data.get("agendas", [])
    practitioners = profile_data.get("practitioners", [])

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
            for p in practitioners:
                if str(p.get("id")) == unique_pids[0]:
                    practitioner_name = _practitioner_display_name(p)
                    break
            practitioner_name = practitioner_name or practice_name
        elif len(practitioners) == 1:
            practitioner_name = _practitioner_display_name(practitioners[0]) or practice_name
        elif len(unique_pids) > 1:
            practitioner_name = f"Any of {len(unique_pids)} practitioners"
        else:
            practitioner_name = "Any Practitioner"

    valid_agendas = []
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
        valid_agendas.append(str(agenda["id"]))

    agenda_ids_str = "-".join(valid_agendas)

    print(f"Fetching availabilities using {len(valid_agendas)} agenda(s)...")
    avail_url = "https://www.doctolib.de/availabilities.json"
    current_time = datetime.now()

    avail_params = {
        "start_date": current_time.date().isoformat(),
        "visit_motive_ids": motive_id,
        "agenda_ids": agenda_ids_str,
        "practice_ids": practice_id,
        "insurance_sector": polling_cfg.get("insurance_sector", "public"),
        "telehealth": str(polling_cfg.get("telehealth", False)).lower(),
        "limit": polling_cfg.get("slot_limit", 15),
    }

    avail_resp = requests.get(avail_url, params=avail_params, headers=headers)
    avail_resp.raise_for_status()
    avail_json = avail_resp.json()

    # ── Parse & Display Results (mirrors checker.py robust logic) ──
    total = avail_json.get("total", 0)
    next_slot = avail_json.get("next_slot")
    
    print("\n" + "="*55)
    print(f"Clinic:       {practice_name}")
    print(f"Practitioner: {practitioner_name}")
    print(f"API Total:    {total} (within API limit window)")
    print(f"Config Window:{upcoming_days} days")
    
    if total > 0:
        availabilities = avail_json.get("availabilities", [])
        found = False
        if availabilities:
            for day_info in availabilities:
                slots = day_info.get("slots", [])
                if slots:
                    date_str = day_info.get("date", "N/A")
                    
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
                    
                    print("-" * 55)
                    print("👉 STATUS: IMMINENT SLOT (Trigger: slot_found)")
                    print(f"First Slot:   {date_str}{time_str}")
                    found = True
                    break
        if not found:
            print(f"First Slot:   {format_doctolib_datetime(next_slot) if next_slot else 'N/A'}")
            
    elif next_slot:
        try:
            next_dt = datetime.fromisoformat(next_slot.replace("Z", "+00:00"))
            days_away = (next_dt.date() - date.today()).days
            
            if 0 <= days_away <= upcoming_days:
                print("-" * 55)
                print("👉 STATUS: FAR SLOT (Trigger: far_slot_found)")
                print(f"Next Slot:    {format_doctolib_datetime(next_slot)}")
                print(f"Distance:     {days_away} days away (WITHIN {upcoming_days}d window)")
            else:
                print("-" * 55)
                print("👉 STATUS: OUT OF WINDOW (No trigger)")
                print(f"Next Slot:    {format_doctolib_datetime(next_slot)}")
                print(f"Distance:     {days_away} days away (OUTSIDE {upcoming_days}d window)")
        except ValueError:
            print(f"Next Slot:    {format_doctolib_datetime(next_slot)}")
    else:
        print("-" * 55)
        print("👉 STATUS: NO SLOTS (No trigger)")
        print(f"Next Slot:    None found on calendar")
        
    print("="*55 + "\n")

    final_output = {
        "metadata_header": {
            "checked_at": current_time.strftime("%Y-%m-%d %H:%M:%S"),
            "clinic": practice_name,
            "practitioner": practitioner_name,
            "target_url": target_url,
            "agenda_ids": valid_agendas,
        },
        "doctolib_data": avail_json,
    }

    temp_dir = "temp"
    os.makedirs(temp_dir, exist_ok=True)

    safe_clinic = sanitize_filename(practice_name)
    safe_practitioner = sanitize_filename(practitioner_name)
    timestamp = current_time.strftime("%Y%m%d_%H%M%S")

    filename = f"{timestamp}_{safe_clinic}_{safe_practitioner}.json"
    file_path = os.path.join(temp_dir, filename)

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(final_output, f, indent=2, ensure_ascii=False)

    print(f"JSON successfully saved to: {os.path.abspath(file_path)}")


if __name__ == "__main__":
    main()