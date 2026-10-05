"""DST-safe quiet-hour windows in each job's IANA time zone."""

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def validate_quiet_hours(enabled, start, end):
    if type(enabled) is not bool:
        raise ValueError("quiet_hours_enabled must be boolean")
    if not isinstance(start, str) or not _TIME.fullmatch(start):
        raise ValueError("quiet_hours_start must use HH:MM")
    if not isinstance(end, str) or not _TIME.fullmatch(end):
        raise ValueError("quiet_hours_end must use HH:MM")
    if enabled and start == end:
        raise ValueError("quiet_hours_start and quiet_hours_end must differ")
    return time.fromisoformat(start), time.fromisoformat(end)


def _instants(wall, zone):
    values = []
    for fold in (0, 1):
        instant = wall.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if instant.astimezone(zone).replace(tzinfo=None) == wall and instant not in values:
            values.append(instant)
    return sorted(values)


def _boundary(day, clock, zone, edge):
    wall = datetime.combine(day, clock)
    values = _instants(wall, zone)
    if values:
        return values[0] if edge == "start" else values[-1]
    # A boundary inside a forward clock jump moves to the first valid wall minute.
    for minute in range(1, 2881):
        values = _instants(wall + timedelta(minutes=minute), zone)
        if values:
            return values[0]
    raise ValueError("quiet_hours_boundary_unresolvable")


def _window(day, zone, start, end):
    start_at = _boundary(day, start, zone, "start")
    end_day = day + timedelta(days=1) if start >= end else day
    end_at = _boundary(end_day, end, zone, "end")
    return (start_at, end_at) if end_at > start_at else None


def quiet_window(now, time_zone, start, end):
    """Return current quiet-window end in UTC, or None when outside quiet hours."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    start_time, end_time = validate_quiet_hours(True, start, end)
    zone = ZoneInfo(time_zone)
    current = now.astimezone(timezone.utc)
    local_day = current.astimezone(zone).date()
    for offset in range(-2, 3):
        window = _window(local_day + timedelta(days=offset), zone, start_time, end_time)
        if window and window[0] <= current < window[1]:
            return window[1]
    return None


def next_release(now, time_zone, enabled, start, end):
    """Return the next eligible UTC instant for a preview; None when disabled."""
    start_time, end_time = validate_quiet_hours(enabled, start, end)
    if not enabled:
        return None
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = ZoneInfo(time_zone)
    current = now.astimezone(timezone.utc)
    local_day = current.astimezone(zone).date()
    current_window = quiet_window(current, time_zone, start, end)
    if current_window is not None:
        return current_window
    candidates = []
    for offset in range(-1, 5):
        window = _window(local_day + timedelta(days=offset), zone, start_time, end_time)
        if window and window[0] > current:
            candidates.append(window[1])
    return min(candidates) if candidates else None
