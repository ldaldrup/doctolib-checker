"""Validation and orchestration for job API operations."""

from copy import copy
import time

import requests
from curl_cffi import requests as curl_requests
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.doctolib import BookingUrlError, DoctolibClient, MetadataResolutionError, TargetBudgetExceeded, get_availability_session, parse_booking_url
from app.storage.repositories import ConflictError, NotFoundError, TARGET_FIELDS, VersionConflictError
from app.services.create_lease import MetadataLease


def resolve_target(url, doctolib):
    try:
        parsed = doctolib.resolve(url)
    except (BookingUrlError, MetadataResolutionError):
        raise
    return {
        "booking_url": url,
        "country": parsed.country,
        "profile_slug": parsed.profile_slug,
        "practice_id": parsed.practice_id,
        "motive_id": parsed.motive_id,
        "practitioner_id": parsed.practitioner_id,
        "agenda_ids_str": parsed.agenda_ids_str,
        "practice_name": parsed.practice_name,
        "practitioner_name": parsed.practitioner_name,
        "motive_name": parsed.motive_name,
    }


def validate_timezone(name):
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError):
        raise ValueError("time_zone must be a valid IANA time zone")


def normalize_targets(targets):
    normalized_urls = set()
    for target in targets:
        target["booking_url"] = parse_booking_url(target["booking_url"])["url"]
        if target["booking_url"] in normalized_urls:
            raise ValueError("Target URLs must be unique after URL normalization")
        normalized_urls.add(target["booking_url"])
    return targets


def create_job(repository, doctolib, settings, values, operation=None):
    validate_timezone(values["time_zone"])
    telegram_enabled = values.get("telegram_enabled")
    if telegram_enabled is None:
        telegram_enabled = bool(values.get("notification_channel_ids"))
    targets = []
    for url in values["target_urls"]:
        if operation is None:
            targets.append(resolve_target(url, doctolib))
            continue
        with MetadataLease(repository, operation) as lease:
            # A private copy keeps reservation guards separate from other
            # requests using the application's shared Doctolib client.
            guarded = copy(doctolib)
            request_gate = doctolib.before_request
            def guarded_request():
                lease.guard()
                request_gate()
                lease.guard()
            guarded.before_request = guarded_request
            targets.append(resolve_target(url, guarded))
            lease.guard()
    targets = normalize_targets(targets)
    clean = dict(values)
    clean["telegram_enabled"] = bool(telegram_enabled)
    clean["earliest_date"] = _date_text(values.get("earliest_date"))
    clean["latest_date"] = _date_text(values.get("latest_date"))
    return repository.create_job(clean, targets, operation=operation)


def update_job(repository, doctolib, settings, job_id, values, expected_version=None):
    existing = repository.get_job(job_id)
    if existing is None:
        return None
    if expected_version is not None and existing["edit_version"] != expected_version:
        raise VersionConflictError(existing["edit_version"])
    values = dict(values)
    target_urls = values.pop("target_urls", None)
    if "time_zone" in values:
        validate_timezone(values["time_zone"])
    new_mode = values.get("date_mode", existing["date_mode"])
    earliest = values.get("earliest_date", existing["earliest_date"])
    latest = values.get("latest_date", existing["latest_date"])
    if new_mode != "custom" and any(
        values.get(field) is not None for field in ("earliest_date", "latest_date")
    ):
        raise ValueError("Date range is only valid with custom date mode")
    if new_mode == "custom":
        if not earliest or not latest:
            raise ValueError("Custom date mode requires earliest_date and latest_date")
        if hasattr(earliest, "isoformat"):
            earliest = earliest.isoformat()
        if hasattr(latest, "isoformat"):
            latest = latest.isoformat()
        if earliest > latest:
            raise ValueError("earliest_date must be on or before latest_date")
        values["earliest_date"] = earliest
        values["latest_date"] = latest
    else:
        values["earliest_date"] = None
        values["latest_date"] = None
        if new_mode == "first_available":
            values["horizon_days"] = values.get("horizon_days", existing["horizon_days"] or 15)
    targets = normalize_targets([resolve_target(url, doctolib) for url in target_urls]) if target_urls is not None else None
    return repository.update_job(job_id, values, targets=targets, expected_version=expected_version)


def _date_text(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def revalidate_target(repository, doctolib, settings, job_id, target_id, booking_url, expected_version):
    """Resolve without a write lock, then apply through the existing edit CAS."""
    existing = repository.get_job(job_id)
    if existing is None:
        raise NotFoundError("Job not found")
    if existing['edit_version'] != expected_version:
        raise VersionConflictError(existing['edit_version'])
    canonical = parse_booking_url(booking_url)['url']
    before = next((target for target in existing['targets'] if target['id'] == target_id), None)
    if before is None or before['booking_url'] != canonical:
        raise ConflictError("target_conflict")
    guarded = copy(doctolib)
    guarded.deadline = time.monotonic() + settings.target_budget_seconds
    # requests' read timeout is an inactivity timeout. The installed curl
    # adapter enforces a total transfer timeout, including trickling metadata.
    if isinstance(getattr(guarded, 'metadata_session', None), requests.Session):
        guarded.metadata_session = get_availability_session(settings.doctolib_profile)

    def guard():
        if time.monotonic() >= guarded.deadline:
            raise TargetBudgetExceeded()
        current = repository.get_job(job_id)
        if current is None:
            raise NotFoundError("Job not found")
        if current['edit_version'] != expected_version:
            raise VersionConflictError(current['edit_version'])
        target = next((item for item in current['targets'] if item['id'] == target_id), None)
        if target is None or target['booking_url'] != canonical:
            raise ConflictError("target_conflict")

    def before_request():
        guard()
        values = repository.settings(settings.minimum_poll_interval_seconds, settings.request_spacing_seconds)
        repository.reserve_request_turn(float(values['request_spacing_seconds']), deadline=guarded.deadline)
        guard()

    guarded.before_request = before_request
    guarded.check_permission = guard
    candidate, state, reason = None, 'validated', None
    try:
        candidate = resolve_target(canonical, guarded)
        guard()
        if any(not isinstance(candidate.get(key), str) or not candidate[key].strip()
               for key in ('country', 'profile_slug', 'practice_id', 'motive_id', 'agenda_ids_str',
                           'practice_name', 'practitioner_name', 'motive_name')):
            raise MetadataResolutionError('Incomplete booking metadata')
        parts = parse_booking_url(canonical)
        if any(candidate[key] != str(parts[key]) for key in ('country', 'profile_slug', 'practice_id', 'motive_id')):
            raise MetadataResolutionError('Inconsistent booking metadata')
        if (parts['practitioner_id'] not in (None, 'NO_PREFERENCE')
                and candidate.get('practitioner_id') != parts['practitioner_id']):
            raise MetadataResolutionError('Inconsistent practitioner metadata')
    except TargetBudgetExceeded:
        candidate, state, reason = None, 'unavailable', 'budget_exceeded'
    except (requests.RequestException, curl_requests.exceptions.RequestException):
        candidate, state, reason = None, 'unavailable', 'upstream_unavailable'
    except BookingUrlError:
        # The submitted canonical URL was validated before outbound work.
        # A redirect failure says nothing definitive about that saved URL.
        candidate, state, reason = None, 'unavailable', 'upstream_unavailable'
    except MetadataResolutionError:
        candidate, state, reason = None, 'invalid', 'invalid_metadata'
    job = repository.update_job(job_id, {}, expected_version=expected_version,
        target_repair=(target_id, canonical, candidate, state, reason))
    after = next(target for target in job['targets'] if target['id'] == target_id)
    changed = job['search_revision'] != existing['search_revision']
    identity = lambda target: {key: target.get(key) for key in TARGET_FIELDS if key != 'booking_url'}
    return {'job': job, 'changed': changed, 'validation_state': state, 'validation_reason': reason,
            'before': identity(before), 'after': identity(after),
            'fresh_check_queued': changed and job['status'] == 'active'}
