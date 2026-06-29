import urllib.parse
from datetime import date, datetime

import requests
from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.config import DOCTOLIB_AVAILABILITIES_API, DOCTOLIB_INFO_API
from app.models import BookingMeta

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


def parse_booking_url(booking_url: str):
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
        raise ValueError(f"Could not parse required IDs from URL parameters: {e}") from e

    return slug, practice_id, motive_id, practitioner_id


def get_booking_metadata(booking_url, config, session: Session):
    slug, practice_id, motive_id, practitioner_id = parse_booking_url(booking_url)
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
    profile_name = profile.get("name_with_title") or profile.get("name") or slug
    practice_name = profile_name

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


def format_doctolib_datetime(dt_str: str) -> str:
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


def fetch_slot_total(booking_url, config, session, meta=None):
    if meta is None:
        meta = get_booking_metadata(booking_url, config, session)

    headers = {"User-Agent": config["user_agent"]}
    upcoming_days = config["polling"]["upcoming_days"]

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
        DOCTOLIB_AVAILABILITIES_API, params=params, headers=headers, timeout=15
    )
    avail_resp.raise_for_status()
    avail_data = avail_resp.json()

    total = avail_data.get("total", 0)
    next_slot = avail_data.get("next_slot")

    first_date = "N/A"
    is_far_slot = False

    if total > 0:
        availabilities = avail_data.get("availabilities", [])
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
                    first_date = f"{date_str}{time_str}"
                    found = True
                    break

        if not found:
            first_date = format_doctolib_datetime(next_slot) if next_slot else "N/A"
    elif next_slot:
        try:
            next_dt = datetime.fromisoformat(next_slot.replace("Z", "+00:00"))
            days_away = (next_dt.date() - date.today()).days

            if 0 <= days_away <= upcoming_days:
                total = 1
                is_far_slot = True
                first_date = format_doctolib_datetime(next_slot)
            else:
                first_date = f"next in {days_away}d"
        except ValueError:
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
        is_far_slot,
    )
