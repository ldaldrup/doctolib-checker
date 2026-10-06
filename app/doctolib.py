"""Validated URL parsing and structured Doctolib availability checks."""

import urllib.parse
import time as time_module
from email.utils import parsedate_to_datetime
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from curl_cffi import requests as curl_requests
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
GLOBAL_AVAILABILITY_SESSIONS = {}


class BookingUrlError(ValueError):
    """The supplied URL is not a supported booking availability URL."""


class MetadataResolutionError(ValueError):
    """Doctolib metadata did not identify a usable booking target."""


class TargetBudgetExceeded(TimeoutError):
    """The target's total checking deadline expired; coverage is incomplete."""


class DoctolibHTTPError(requests.RequestException):
    """Safe HTTP failure details for activity history."""

    def __init__(self, status_code, retry_at=None):
        super().__init__("Doctolib returned an HTTP error")
        self.status_code = status_code
        self.retry_at = retry_at


def _retry_at(headers):
    value = headers.get("Retry-After")
    if not value:
        return None
    try:
        seconds = int(value)
        if seconds < 0:
            return None
        return datetime.now(timezone.utc) + timedelta(seconds=seconds)
    except (TypeError, ValueError, OverflowError):
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return retry_at.astimezone(timezone.utc)


def _availability_error(category, code, message):
    return AvailabilityResult(status="error", slot_count=0, earliest_slot=None,
                             count_complete=False, error_code=code,
                             error_message=message, error_category=category)


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


def get_availability_session(profile="safari2601"):
    """Return a reusable browser-profile session for availability requests."""
    if profile != "safari2601":
        raise ValueError("Unsupported Doctolib availability profile")
    if profile not in GLOBAL_AVAILABILITY_SESSIONS:
        GLOBAL_AVAILABILITY_SESSIONS[profile] = curl_requests.Session(impersonate=profile)
    return GLOBAL_AVAILABILITY_SESSIONS[profile]


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
    def __init__(self, session=None, user_agent="DoctolibChecker/2.0", before_request=None,
                 *, metadata_session=None, availability_session=None, profile="safari2601", page_days=15):
        # `session` remains as a compatibility injection point for callers and
        # tests that intentionally use one fake transport for both request types.
        self.metadata_session = metadata_session if metadata_session is not None else (
            session if session is not None else get_session()
        )
        self.availability_session = availability_session if availability_session is not None else session
        self.user_agent = user_agent
        self.before_request = before_request or (lambda: None)
        if profile != "safari2601":
            raise ValueError("Unsupported Doctolib availability profile")
        if not isinstance(page_days, int) or isinstance(page_days, bool) or not 1 <= page_days <= 15:
            raise ValueError("Doctolib page size must be between 1 and 15 days")
        self.profile = profile
        self.page_days = page_days
        self.deadline = None
        self.check_permission = lambda: None

    def remaining_budget(self):
        self.check_permission()
        if self.deadline is None:
            return None
        remaining = self.deadline - time_module.monotonic()
        # libcurl converts timeout to integer milliseconds; zero disables it.
        if remaining < 0.001:
            raise TargetBudgetExceeded("The target checking budget was exceeded.")
        return remaining

    def _get(self, url, **kwargs):
        timeout = kwargs.pop("timeout", 15)
        availability = kwargs.pop("availability", False)
        session = self.availability_session if availability else self.metadata_session
        if availability and session is None:
            session = get_availability_session(self.profile)
            self.availability_session = session
        retryable_statuses = {429, 500, 502, 503, 504}
        last_error = None
        for attempt in range(4):
            request_url = url
            request_kwargs = dict(kwargs)
            for redirect_count in range(6):
                self.remaining_budget()
                self.before_request()
                remaining = self.remaining_budget()
                try:
                    call_kwargs = {
                        "timeout": min(timeout, remaining) if remaining is not None else timeout,
                        "allow_redirects": False,
                        **request_kwargs,
                    }
                    if not availability:
                        call_kwargs["headers"] = {"User-Agent": self.user_agent}
                    response = session.get(request_url, **call_kwargs)
                    self.remaining_budget()
                except (requests.Timeout, requests.ConnectionError,
                        curl_requests.exceptions.Timeout, curl_requests.exceptions.ConnectionError) as exc:
                    self.remaining_budget()
                    last_error = exc
                    if attempt == 3:
                        raise
                    break

                if response.status_code in {301, 302, 303, 307, 308} and "Location" in response.headers:
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
                    last_error = None
                    break
                if response.status_code >= 400:
                    raise DoctolibHTTPError(response.status_code, _retry_at(response.headers))
                response.raise_for_status()
                return response
            if attempt < 3:
                remaining = self.remaining_budget()
                delay = min(2 ** attempt, 8)
                time_module.sleep(min(delay, remaining) if remaining is not None else delay)
                self.remaining_budget()
        raise last_error

    def resolve(self, booking_url):
        parts = parse_booking_url(booking_url)
        origin = "https://" + parts["host"]
        info_url = origin + "/online_booking/api/slot_selection_funnel/v1/info.json"
        response = self._get(info_url, params={"profile_slug": parts["profile_slug"]}, timeout=10)
        try:
            payload = response.json()
        except (TypeError, ValueError):
            raise MetadataResolutionError("Doctolib returned invalid booking metadata") from None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("profile", {}), dict):
            raise MetadataResolutionError("Doctolib returned invalid booking metadata")
        # Provider metadata is untrusted. Reject malformed identity/name fields
        # before matching or formatting; never stringify containers into IDs.
        for collection, id_fields, name_fields in (
                ('practitioners', ('id',), ('name', 'full_name', 'display_name', 'first_name', 'last_name')),
                ('agendas', ('id', 'practice_id', 'practitioner_id'), ()),
                ('visit_motives', ('id',), ('name', 'label', 'visit_motive_name')),
                ('motives', ('id',), ('name', 'label', 'visit_motive_name'))):
            items = data.get(collection, [])
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise MetadataResolutionError("Doctolib returned invalid booking metadata")
            for item in items:
                if (any(item.get(key) is not None and type(item[key]) not in (str, int) for key in id_fields)
                        or any(item.get(key) is not None and not isinstance(item[key], str) for key in name_fields)):
                    raise MetadataResolutionError("Doctolib returned invalid booking metadata")
                if collection == 'agendas':
                    motives = item.get('visit_motive_ids', [])
                    if not isinstance(motives, list) or any(type(value) not in (str, int) for value in motives):
                        raise MetadataResolutionError("Doctolib returned invalid booking metadata")
        profile = data.get("profile", {})
        if any(profile.get(key) is not None and not isinstance(profile[key], str) for key in ('name_with_title', 'name')):
            raise MetadataResolutionError("Doctolib returned invalid booking metadata")
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
            if (latest - earliest).days + 1 > 366:
                raise ValueError("Custom date range must not exceed 366 calendar dates")
        elif mode == "first_available":
            earliest = today
            horizon = int(search.get("horizon_days", 15))
            if horizon < 1 or horizon > 365:
                raise ValueError("horizon_days must be between 1 and 365")
            latest = today + timedelta(days=horizon - 1)
        else:
            raise ValueError("date_mode must be first_available or custom")
        if "effective_earliest_date" in search or "effective_latest_date" in search:
            # Claims freeze calendar dates, while check() still filters slots
            # against the current clock rather than a historical claim time.
            try:
                earliest = date.fromisoformat(search["effective_earliest_date"])
                latest = date.fromisoformat(search["effective_latest_date"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Frozen search requires both effective calendar dates") from exc
            if earliest > latest or (latest - earliest).days + 1 > 366:
                raise ValueError("Frozen search has an invalid date range")
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
        all_slots = {}
        page_start = earliest
        base_params = {
            "visit_motive_ids": meta.motive_id,
            "agenda_ids": meta.agenda_ids_str,
            "practice_ids": meta.practice_id,
            "insurance_sector": search.get("insurance_sector", "public"),
            "telehealth": str(bool(search.get("telehealth", False))).lower(),
        }
        while page_start <= latest:
            self.remaining_budget()
            page_end = min(page_start + timedelta(days=self.page_days - 1), latest)
            response = self._get(
                availability_url,
                params={
                    **base_params,
                    "start_date": page_start.isoformat(),
                    "limit": (page_end - page_start).days + 1,
                },
                timeout=15,
                availability=True,
            )
            try:
                data = response.json()
            except (TypeError, ValueError):
                return _availability_error("malformed_response", "invalid_availability_response",
                                           "Doctolib returned invalid availability data.")
            self.remaining_budget()
            if not isinstance(data, dict) or not isinstance(data.get("availabilities"), list):
                return _availability_error("malformed_response", "invalid_availability_response",
                                           "Doctolib returned invalid availability data.")
            total = data.get("total")
            if type(total) is not int or total < 0:
                return _availability_error("malformed_response", "invalid_availability_response",
                                           "Doctolib returned invalid availability data.")
            returned_count = 0
            page_slots = {}
            for day_info in data.get("availabilities", []) or []:
                self.remaining_budget()
                if not isinstance(day_info, dict) or not isinstance(day_info.get("slots", []), list):
                    return _availability_error("malformed_response", "invalid_availability_response",
                                               "Doctolib returned invalid availability data.")
                day_text = str(day_info.get("date", ""))
                for slot_data in day_info.get("slots", []) or []:
                    if not isinstance(slot_data, (dict, str)):
                        return _availability_error("malformed_response", "invalid_availability_response",
                                                   "Doctolib returned invalid availability data.")
                    returned_count += 1
                    starts_at = self._slot_datetime(day_text, slot_data, zone)
                    if starts_at is None:
                        return _availability_error("malformed_response", "invalid_availability_response",
                                                   "Doctolib returned a slot without a valid start time.")
                    local_day = starts_at.astimezone(zone).date()
                    if starts_at > current and page_start <= local_day <= page_end:
                        page_slots[starts_at] = Slot(starts_at=starts_at)

            next_slot = data.get("next_slot")
            if not page_slots and next_slot:
                fallback = self._slot_datetime(str(next_slot)[:10], next_slot, zone)
                if fallback is None:
                    return _availability_error("malformed_response", "invalid_availability_response",
                                               "Doctolib returned a slot without a valid start time.")
                fallback_day = fallback.astimezone(zone).date()
                if fallback > current and page_start <= fallback_day <= page_end:
                    page_slots[fallback] = Slot(starts_at=fallback)
                    returned_count += 1

            # Never report a partial count or trigger an alert when any page is truncated.
            if returned_count < total:
                return _availability_error("incomplete_response", "incomplete_availability_response",
                                           "Doctolib returned only part of the availability list.")
            all_slots.update(page_slots)
            page_start = page_end + timedelta(days=1)
            self.remaining_budget()

        self.remaining_budget()
        matched = sorted(all_slots.values(), key=lambda item: item.starts_at)
        if not matched:
            return AvailabilityResult(
                status="no_availability", slot_count=0, earliest_slot=None, count_complete=True
            )
        return AvailabilityResult(
            status="available",
            slot_count=len(matched),
            earliest_slot=matched[0].starts_at,
            slots=matched,
            count_complete=True,
        )


# CLI compatibility wrappers. The web API and worker use DoctolibClient directly.
def get_booking_metadata(booking_url, config, session: Session, before_request=None):
    return DoctolibClient(
        metadata_session=session,
        user_agent=config.get("user_agent", "DoctolibChecker/2.0"),
        before_request=before_request,
        profile=config.get("doctolib_profile", "safari2601"),
        page_days=config.get("polling", {}).get("page_days", config.get("polling", {}).get("slot_limit", 15)),
    ).resolve(booking_url)


def format_doctolib_datetime(dt_str: str) -> str:
    if not dt_str:
        return ""
    try:
        parsed = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return dt_str


def fetch_slot_total(booking_url, config, session, meta=None, before_request=None):
    polling = config.get("polling", {})
    search = {
        "date_mode": "first_available",
        "horizon_days": polling.get("upcoming_days", 15),
        "time_zone": config.get("time_zone", "Europe/Berlin"),
        "insurance_sector": polling.get("insurance_sector", "public"),
        "telehealth": polling.get("telehealth", False),
    }
    client = DoctolibClient(
        metadata_session=session,
        user_agent=config.get("user_agent", "DoctolibChecker/2.0"),
        before_request=before_request,
        profile=config.get("doctolib_profile", "safari2601"),
        page_days=polling.get("page_days", polling.get("slot_limit", 15)),
    )
    result = client.check(booking_url, search, meta=meta)
    if result.status == "error":
        raise RuntimeError(result.error_code or "availability_check_incomplete")
    parsed = parse_booking_url(booking_url)
    meta = meta or client.resolve(booking_url)
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
