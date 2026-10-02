"""Validation and orchestration for job API operations."""

from copy import copy
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.doctolib import BookingUrlError, DoctolibClient, MetadataResolutionError, parse_booking_url
from app.storage.repositories import VersionConflictError
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
        telegram_enabled = settings.telegram_enabled
    if telegram_enabled and not settings.telegram_enabled:
        raise ValueError("Telegram is not configured on the server")
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
    if values.get("telegram_enabled") and not settings.telegram_enabled:
        raise ValueError("Telegram is not configured on the server")
    targets = normalize_targets([resolve_target(url, doctolib) for url in target_urls]) if target_urls is not None else None
    return repository.update_job(job_id, values, targets=targets, expected_version=expected_version)


def _date_text(value):
    return value.isoformat() if hasattr(value, "isoformat") else value
