"""Validated URL parsing and structured Doctolib availability checks."""

import urllib.parse
import time as time_module
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from requests import Session
from requests.adapters import HTTPAdapter

from app.models import AvailabilityResult, BookingMeta, Slot


SUPPORTED_HOSTS = {
    "www.doctolib.de": "de",
    "doctolib.de": "de",
    "www.doctolib.fr": "fr",
    "doctolib.fr": "fr",
}
GLOBAL_SESSION = None


class BookingUrlError(ValueError):
    """The supplied URL is not a supported booking availability URL."""


class MetadataResolutionError(ValueError):
    """Doctolib metadata did not identify a usable booking target."""


def get_session():
    global GLOBAL_SESSION
    if GLOBAL_SESSION is None:
        session = requests.Session()
        # Retries are handled by DoctolibClient._get so every outbound attempt
        # passes through the shared request gate.
        adapter = HTTPAdapter(max_retries=0, pool_connections=5, pool_maxsize=5)
        session.mount("https://", adapter)
        GLOBAL_SESSION = session
    return GLOBAL_SESSION


def parse_booking_url(booking_url: str):
    if not isinstance(booking_url, str):
        raise BookingUrlError("Booking URL must be text")
    try:
        parsed = urllib.parse.urlsplit(booking_url.strip())
        port = parsed.port
    except ValueError as exc:
        raise BookingUrlError("Booking URL is malformed") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or host not in SUPPORTED_HOSTS:
        raise BookingUrlError("Use an HTTPS Doctolib availability URL for a supported country")
    if parsed.username or parsed.password or port not in (None, 443):
        raise BookingUrlError("Booking URL must not contain credentials or a custom port")
    parts = [part for part in parsed.path.split("/") if part]
    try:
        booking_index = parts.index("booking")
    except ValueError as exc:
        raise BookingUrlError("URL path must contain /booking/availabilities") from exc
    if booking_index == 0 or booking_index + 1 >= len(parts) or parts[booking_index + 1] != "availabilities":
        raise BookingUrlError("URL path must contain /booking/availabilities")
    slug = parts[booking_index - 1]
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

    def first(*keys):
        for key in keys:
            values = query.get(key)
            if values and values[0]:
                return values[0]
        return None

    raw_place_id = first("placeId", "pid", "practice_id")
    practice_id = raw_place_id.split("-", 1)[1] if raw_place_id and "-" in raw_place_id else raw_place_id
    motive_id = first("motiveIds[]", "motiveIds", "visit_motive_ids")
    practitioner_id = first("practitionerId", "practitioner_id")
    if not practice_id:
        raise BookingUrlError("URL is missing placeId or practice ID")
    if not motive_id:
        raise BookingUrlError("URL is missing a visit motive ID")
    clean_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    return {
        "url": clean_url,
        "host": host,
        "country": SUPPORTED_HOSTS[host],
        "profile_slug": slug,
        "practice_id": practice_id,
        "motive_id": motive_id,
        "practitioner_id": practitioner_id,
    }


def _display_name(person):
    if not person:
        return None
    return (
        person.get("name")
        or person.get("full_name")
        or person.get("display_name")
        or " ".join(filter(None, [person.get("first_name"), person.get("last_name")]))
        or None
    )


def _motive_name(info_data, motive_id):
    collections = [info_data.get("visit_motives", []), info_data.get("motives", [])]
    for items in collections:
        if isinstance(items, list):
            for item in items:
                if str(item.get("id")) == str(motive_id):
                    name = item.get("name") or item.get("label") or item.get("visit_motive_name")
                    if name:
                        return str(name)
    return "Visit motive " + str(motive_id)


class DoctolibClient:
    def __init__(self, session=None, user_agent="DoctolibChecker/2.0", before_request=None):
        self.session = session or get_session()
        self.user_agent = user_agent
        self.before_request = before_request or (lambda: None)

    def _get(self, url, **kwargs):
        timeout = kwargs.pop("timeout", 15)
        retryable_statuses = {429, 500, 502, 503, 504}
        last_error = None
        for attempt in range(4):
            request_url = url
            request_kwargs = dict(kwargs)
            for redirect_count in range(6):
                self.before_request()
                try:
                    response = self.session.get(
                        request_url,
                        headers={"User-Agent": self.user_agent},
                        timeout=timeout,
                        allow_redirects=False,
                        **request_kwargs
                    )
                except (requests.Timeout, requests.ConnectionError) as exc:
                    last_error = exc
                    if attempt == 3:
                        raise
                    break

                if response.is_redirect:
                    location = response.headers.get("Location")
                    if not location or redirect_count == 5:
                        raise BookingUrlError("Doctolib returned an invalid redirect")
                    redirected_url = urllib.parse.urljoin(request_url, location)
                    try:
                        redirected = urllib.parse.urlsplit(redirected_url)
                        redirected_port = redirected.port
                    except ValueError as exc:
                        raise BookingUrlError("Doctolib returned an invalid redirect") from exc
                    if (redirected.scheme != "https" or (redirected.hostname or "").lower() not in SUPPORTED_HOSTS
                            or redirected.username or redirected.password or redirected_port not in (None, 443)):
                        raise BookingUrlError("Doctolib redirected to an unsupported host")
                    request_url = redirected_url
                    request_kwargs.pop("params", None)
                    continue

                if response.status_code in retryable_statuses and attempt < 3:
                    last_error = requests.HTTPError("Retryable Doctolib response")
                    break
                response.raise_for_status()
                return response
            if attempt < 3:
                time_module.sleep(min(2 ** attempt, 8))
        raise last_error

    def resolve(self, booking_url):
        parts = parse_booking_url(booking_url)
        origin = "https://" + parts["host"]
        info_url = origin + "/online_booking/api/slot_selection_funnel/v1/info.json"
        response = self._get(info_url, params={"profile_slug": parts["profile_slug"]}, timeout=10)
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise MetadataResolutionError("Doctolib returned invalid booking metadata")
        profile = data.get("profile", {})
        practice_name = profile.get("name_with_title") or profile.get("name") or parts["profile_slug"]
        practitioners = data.get("practitioners", [])
        practitioner_id = parts["practitioner_id"]
        practitioner_name = None

        if practitioner_id and practitioner_id != "NO_PREFERENCE":
            for practitioner in practitioners:
                if str(practitioner.get("id")) == str(practitioner_id):
                    practitioner_name = _display_name(practitioner)
                    break
            if not practitioner_name:
                practitioner_name = "Practitioner " + str(practitioner_id)
        else:
            practitioner_id = None
            matching_ids = []
            for agenda in data.get("agendas", []):
                if str(agenda.get("practice_id")) != str(parts["practice_id"]):
                    continue
                try:
                    if int(parts["motive_id"]) not in [int(x) for x in agenda.get("visit_motive_ids", [])]:
                        continue
                except (TypeError, ValueError):
                    continue
                agenda_practitioner = agenda.get("practitioner_id")
                if agenda_practitioner and str(agenda_practitioner) not in matching_ids:
                    matching_ids.append(str(agenda_practitioner))
            if len(matching_ids) == 1:
                practitioner_id = matching_ids[0]
                for practitioner in practitioners:
                    if str(practitioner.get("id")) == practitioner_id:
                        practitioner_name = _display_name(practitioner)
                        break
            if not practitioner_name:
                practitioner_name = "Any Practitioner"

        agenda_ids = []
        for agenda in data.get("agendas", []):
            if str(agenda.get("practice_id")) != str(parts["practice_id"]):
                continue
            try:
                if int(parts["motive_id"]) not in [int(x) for x in agenda.get("visit_motive_ids", [])]:
                    continue
            except (TypeError, ValueError):
                continue
            if practitioner_id and str(agenda.get("practitioner_id")) != str(practitioner_id):
                continue
            if agenda.get("id") is not None:
                agenda_ids.append(str(agenda["id"]))
        agenda_ids = list(dict.fromkeys(agenda_ids))
        if not agenda_ids:
            raise MetadataResolutionError("No agenda matches the practitioner, practice, and visit motive in this URL")

        motive_name = _motive_name(data, parts["motive_id"])
        return BookingMeta(
            state_key=parts["profile_slug"] + "_" + str(practitioner_id or "any"),
            practice_name=str(practice_name),
            practitioner_name=str(practitioner_name),
            motive_id=str(parts["motive_id"]),
            agenda_ids_str="-".join(agenda_ids),
            practice_id=str(parts["practice_id"]),
            display_name=str(practitioner_name) + " @ " + str(practice_name),
            country=parts["country"],
            profile_slug=parts["profile_slug"],
            practitioner_id=practitioner_id,
            motive_name=motive_name,
        )

    @staticmethod
    def _window(search, now):
        try:
            zone = ZoneInfo(search.get("time_zone", "Europe/Berlin"))
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown time zone") from exc
        today = now.astimezone(zone).date()
        mode = search.get("date_mode", "first_available")
        if mode == "custom":
            try:
                earliest = date.fromisoformat(search["earliest_date"])
                latest = date.fromisoformat(search["latest_date"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Custom date range requires valid earliest_date and latest_date") from exc
            if earliest > latest:
                raise ValueError("earliest_date must be on or before latest_date")
        elif mode == "first_available":
            earliest = today
            horizon = int(search.get("horizon_days", 15))
            if horizon < 1 or horizon > 365:
                raise ValueError("horizon_days must be between 1 and 365")
            latest = today + timedelta(days=horizon)
        else:
            raise ValueError("date_mode must be first_available or custom")
        return zone, earliest, latest

    @staticmethod
    def _slot_datetime(day_text, slot_value, zone):
        value = slot_value.get("start_time") if isinstance(slot_value, dict) else slot_value
        if value:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=zone)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                pass
            try:
                local_time = time.fromisoformat(str(value))
                return datetime.combine(date.fromisoformat(day_text), local_time, zone).astimezone(timezone.utc)
            except ValueError:
                return None
        try:
            return datetime.combine(date.fromisoformat(day_text), time.min, zone).astimezone(timezone.utc)
        except ValueError:
            return None

    def check(self, booking_url, search: Dict, meta: Optional[BookingMeta] = None, now=None):
        meta = meta or self.resolve(booking_url)
        current = now or datetime.now(timezone.utc)
        zone, earliest, latest = self._window(search, current)
        query = urllib.parse.urlsplit(booking_url)
        host = (query.hostname or "").lower()
        if host not in SUPPORTED_HOSTS:
            raise BookingUrlError("Unsupported Doctolib host")
        availability_url = "https://" + host + "/availabilities.json"
        response = self._get(
            availability_url,
            params={
                "visit_motive_ids": meta.motive_id,
                "agenda_ids": meta.agenda_ids_str,
                "practice_ids": meta.practice_id,
                "insurance_sector": search.get("insurance_sector", "public"),
                "telehealth": str(bool(search.get("telehealth", False))).lower(),
                "start_date": earliest.isoformat(),
                "limit": int(search.get("slot_limit", 100)),
            },
            timeout=15,
        )
        data = response.json()
        total = int(data.get("total", 0) or 0)
        matched = []
        returned_count = 0
        for day_info in data.get("availabilities", []) or []:
            day_text = str(day_info.get("date", ""))
            for slot_data in day_info.get("slots", []) or []:
                returned_count += 1
                starts_at = self._slot_datetime(day_text, slot_data, zone)
                if starts_at is None:
                    continue
                local_day = starts_at.astimezone(zone).date()
                if earliest <= local_day <= latest:
                    matched.append(Slot(starts_at=starts_at))
        matched.sort(key=lambda item: item.starts_at)

        next_slot = data.get("next_slot")
        if not matched and next_slot:
            fallback = self._slot_datetime(str(next_slot)[:10], next_slot, zone)
            if fallback:
                fallback_day = fallback.astimezone(zone).date()
                if earliest <= fallback_day <= latest:
                    matched.append(Slot(starts_at=fallback))

        if not matched:
            complete = returned_count >= total
            if not complete:
                return AvailabilityResult(
                    status="error",
                    slot_count=0,
                    earliest_slot=None,
                    count_complete=False,
                    error_code="incomplete_availability_response",
                    error_message="Doctolib returned only part of the availability list; retrying later.",
                )
            return AvailabilityResult(
                status="no_availability", slot_count=0, earliest_slot=None, count_complete=True
            )

        return AvailabilityResult(
            status="available",
            slot_count=len(matched),
            earliest_slot=matched[0].starts_at,
            slots=matched,
            count_complete=(returned_count >= total),
        )


# CLI compatibility wrappers. The web API and worker use DoctolibClient directly.
def get_booking_metadata(booking_url, config, session: Session):
    return DoctolibClient(session=session, user_agent=config.get("user_agent", "DoctolibChecker/2.0")).resolve(booking_url)


def format_doctolib_datetime(dt_str: str) -> str:
    if not dt_str:
        return ""
    try:
        parsed = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return dt_str


def fetch_slot_total(booking_url, config, session, meta=None):
    polling = config.get("polling", {})
    search = {
        "date_mode": "first_available",
        "horizon_days": polling.get("upcoming_days", 15),
        "time_zone": config.get("time_zone", "Europe/Berlin"),
        "insurance_sector": polling.get("insurance_sector", "public"),
        "telehealth": polling.get("telehealth", False),
        "slot_limit": polling.get("slot_limit", 100),
    }
    result = DoctolibClient(session=session, user_agent=config.get("user_agent", "DoctolibChecker/2.0")).check(
        booking_url, search, meta=meta
    )
    parsed = parse_booking_url(booking_url)
    meta = meta or DoctolibClient(session=session, user_agent=config.get("user_agent", "DoctolibChecker/2.0")).resolve(booking_url)
    first_date = result.earliest_slot.astimezone(ZoneInfo(search["time_zone"])).strftime("%Y-%m-%d %H:%M") if result.earliest_slot else "no slots in window"
    return (
        meta.state_key,
        meta.practitioner_name,
        meta.practice_name,
        result.slot_count,
        parsed["url"],
        first_date,
        result.earliest_slot.isoformat() if result.earliest_slot else None,
        False,
    )
