import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.doctolib import BookingUrlError, DoctolibClient, parse_booking_url


FIXTURES = Path(__file__).parent / "fixtures"
BOOKING_URL = (
    "https://www.doctolib.de/praxis/berlin/beispiel/booking/availabilities"
    "?placeId=practice-123&motiveIds%5B%5D=789&practitionerId=456"
)


class Response:
    def __init__(self, value, status=200, headers=None):
        self.value = value
        self.status_code = status
        self.headers = headers or {}
        self.is_redirect = 300 <= status < 400 and "Location" in self.headers

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP error")

    def json(self):
        return self.value


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses.pop(0)
        return value if isinstance(value, Response) else Response(value)


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def test_parse_booking_url_rejects_non_doctolib_and_incomplete_urls():
    with pytest.raises(BookingUrlError):
        parse_booking_url("https://example.org/praxis/a/booking/availabilities?placeId=1&motiveIds=2")
    with pytest.raises(BookingUrlError):
        parse_booking_url("http://www.doctolib.de/praxis/a/booking/availabilities?placeId=1&motiveIds=2")
    with pytest.raises(BookingUrlError):
        parse_booking_url("https://www.doctolib.de/praxis/a/booking/availabilities?placeId=1")
    with pytest.raises(BookingUrlError, match="malformed"):
        parse_booking_url("https://www.doctolib.de:invalid/praxis/a/booking/availabilities?placeId=1&motiveIds=2")


def test_metadata_validation_resolves_practitioner_practice_and_motive():
    session = FakeSession([fixture("info_de.json")])
    meta = DoctolibClient(session=session).resolve(BOOKING_URL)
    assert meta.country == "de"
    assert meta.practice_name == "Praxis Beispiel"
    assert meta.practitioner_name == "Dr. Ada Beispiel"
    assert meta.motive_name == "Erstuntersuchung"
    assert meta.agenda_ids_str == "1234"
    assert len(session.calls) == 1


def test_check_uses_request_preferences_and_filters_inclusive_date_window():
    session = FakeSession([fixture("availability_window.json")])
    client = DoctolibClient(session=session)
    meta = DoctolibClient(session=FakeSession([fixture("info_de.json")])).resolve(BOOKING_URL)
    now = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
    result = client.check(
        BOOKING_URL,
        {"date_mode": "custom", "earliest_date": "2026-10-10", "latest_date": "2026-10-15",
         "time_zone": "Europe/Berlin", "insurance_sector": "private", "telehealth": True},
        meta=meta,
        now=now,
    )
    assert result.status == "available"
    assert result.slot_count == 2
    assert result.earliest_slot.isoformat() == "2026-10-15T07:30:00+00:00"
    params = session.calls[0][1]["params"]
    assert params["start_date"] == "2026-10-10"
    assert params["insurance_sector"] == "private"
    assert params["telehealth"] == "true"


def test_custom_date_window_must_be_complete_and_ordered():
    client = DoctolibClient(session=FakeSession([]))
    with pytest.raises(ValueError, match="earliest_date must be on or before"):
        client._window({"date_mode": "custom", "earliest_date": "2026-10-20",
                       "latest_date": "2026-10-19", "time_zone": "Europe/Berlin"},
                      datetime(2026, 10, 1, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="requires valid"):
        client._window({"date_mode": "custom", "time_zone": "Europe/Berlin"},
                      datetime(2026, 10, 1, tzinfo=timezone.utc))


def test_each_retry_passes_through_shared_request_gate(monkeypatch):
    session = FakeSession([Response({}, status=429), Response({"data": {}})])
    gate_calls = []
    client = DoctolibClient(session=session, before_request=lambda: gate_calls.append(True))
    monkeypatch.setattr("app.doctolib.time_module.sleep", lambda _seconds: None)

    response = client._get("https://www.doctolib.de/test", timeout=1)

    assert response.json() == {"data": {}}
    assert len(session.calls) == 2
    assert len(gate_calls) == 2


def test_redirects_are_allowlisted_and_pass_through_shared_request_gate():
    session = FakeSession([
        Response({}, status=302, headers={"Location": "https://doctolib.de/other"}),
        Response({"ok": True}),
    ])
    gate_calls = []
    client = DoctolibClient(session=session, before_request=lambda: gate_calls.append(True))

    assert client._get("https://www.doctolib.de/test").json() == {"ok": True}
    assert [call[0] for call in session.calls] == [
        "https://www.doctolib.de/test", "https://doctolib.de/other"
    ]
    assert len(gate_calls) == 2


def test_redirect_to_untrusted_host_is_rejected():
    session = FakeSession([
        Response({}, status=302, headers={"Location": "https://example.org/collect"})
    ])
    client = DoctolibClient(session=session)

    with pytest.raises(BookingUrlError, match="unsupported host"):
        client._get("https://www.doctolib.de/test")
    assert len(session.calls) == 1


def test_only_slots_inside_inclusive_window_and_next_slot_are_counted():
    payload = {
        "total": 2,
        "next_slot": "2026-10-21T08:00:00+02:00",
        "availabilities": [
            {"date": "2026-10-09", "slots": [{"start_time": "2026-10-09T08:00:00+02:00"}]},
            {"date": "2026-10-21", "slots": [{"start_time": "2026-10-21T08:00:00+02:00"}]},
        ],
    }
    client = DoctolibClient(session=FakeSession([payload]))
    meta = DoctolibClient(session=FakeSession([fixture("info_de.json")])).resolve(BOOKING_URL)

    result = client.check(
        BOOKING_URL,
        {"date_mode": "custom", "earliest_date": "2026-10-10", "latest_date": "2026-10-20",
         "time_zone": "Europe/Berlin"},
        meta=meta,
        now=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )

    assert result.status == "no_availability"
    assert result.slot_count == 0
    assert result.earliest_slot is None


def test_incomplete_response_without_a_match_is_not_reported_as_empty():
    payload = {
        "total": 20,
        "availabilities": [
            {"date": "2026-10-09", "slots": [{"start_time": "2026-10-09T08:00:00+02:00"}]}
        ],
    }
    client = DoctolibClient(session=FakeSession([payload]))
    meta = DoctolibClient(session=FakeSession([fixture("info_de.json")])).resolve(BOOKING_URL)

    result = client.check(
        BOOKING_URL,
        {"date_mode": "custom", "earliest_date": "2026-10-10", "latest_date": "2026-10-20",
         "time_zone": "Europe/Berlin"},
        meta=meta,
        now=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )

    assert result.status == "error"
    assert result.error_code == "incomplete_availability_response"
    assert result.count_complete is False
