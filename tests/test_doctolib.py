import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from app.doctolib import BookingUrlError, DoctolibClient, DoctolibHTTPError, fetch_slot_total, get_booking_metadata, parse_booking_url
from app.models import BookingMeta


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


def test_http_failures_keep_only_safe_status_and_retry_time(monkeypatch):
    monkeypatch.setattr("app.doctolib.time_module.sleep", lambda _seconds: None)
    before = datetime.now(timezone.utc)
    session = FakeSession([Response({"provider": "private body"}, status=429, headers={"Retry-After": "30"}) for _ in range(4)])
    client = DoctolibClient(session=session)
    with pytest.raises(DoctolibHTTPError) as caught:
        client._get("https://www.doctolib.de/private-path?token=secret")
    assert caught.value.status_code == 429
    assert caught.value.retry_at.tzinfo is not None
    assert timedelta(seconds=28) <= caught.value.retry_at - before <= timedelta(seconds=31)
    assert "secret" not in str(caught.value) and "private body" not in str(caught.value)
    assert len(session.calls) == 4

    rejected = FakeSession([Response({}, status=403)])
    with pytest.raises(DoctolibHTTPError) as caught:
        DoctolibClient(session=rejected)._get("https://www.doctolib.de/test")
    assert caught.value.status_code == 403 and len(rejected.calls) == 1


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


def _test_meta():
    return BookingMeta(
        state_key="example_any", practice_name="Example", practitioner_name="Any",
        motive_id="789", agenda_ids_str="1234", practice_id="123",
        display_name="Any @ Example", profile_slug="beispiel",
    )


@pytest.mark.parametrize(
    ("latest_date", "expected_limits"),
    [
        ("2026-10-01", [1]),
        ("2026-10-15", [15]),
        ("2026-10-16", [15, 1]),
    ],
)
def test_check_pages_inclusive_window_and_uses_short_final_page(latest_date, expected_limits):
    session = FakeSession([{"total": 0, "availabilities": []} for _ in expected_limits])
    client = DoctolibClient(session=session)
    result = client.check(
        BOOKING_URL,
        {"date_mode": "custom", "earliest_date": "2026-10-01", "latest_date": latest_date,
         "time_zone": "Europe/Berlin"},
        meta=_test_meta(),
        now=datetime(2026, 10, 1, 10, tzinfo=timezone.utc),
    )

    assert result.status == "no_availability"
    assert [call[1]["params"]["limit"] for call in session.calls] == expected_limits
    starts = [call[1]["params"]["start_date"] for call in session.calls]
    expected_starts = ["2026-10-01"] if len(starts) == 1 else ["2026-10-01", "2026-10-16"]
    assert starts == expected_starts


def test_366_date_window_uses_25_pages_with_six_day_final_page():
    session = FakeSession([{"total": 0, "availabilities": []} for _ in range(25)])
    client = DoctolibClient(session=session)
    result = client.check(
        BOOKING_URL,
        {"date_mode": "custom", "earliest_date": "2026-01-01", "latest_date": "2027-01-01",
         "time_zone": "Europe/Berlin"},
        meta=_test_meta(),
        now=datetime(2025, 12, 1, tzinfo=timezone.utc),
    )

    assert result.status == "no_availability"
    assert len(session.calls) == 25
    assert [call[1]["params"]["limit"] for call in session.calls] == [15] * 24 + [6]
    assert session.calls[-1][1]["params"]["start_date"] == "2026-12-27"


def test_custom_window_over_366_dates_is_rejected():
    client = DoctolibClient(session=FakeSession([]))
    with pytest.raises(ValueError, match="must not exceed 366"):
        client.check(
            BOOKING_URL,
            {"date_mode": "custom", "earliest_date": "2026-01-01", "latest_date": "2027-01-02",
             "time_zone": "Europe/Berlin"},
            meta=_test_meta(),
            now=datetime(2025, 12, 1, tzinfo=timezone.utc),
        )


def test_later_incomplete_page_discards_earlier_slots():
    payloads = [
        {"total": 1, "availabilities": [{"date": "2026-10-01", "slots": [
            {"start_time": "2026-10-01T10:00:00+02:00"}
        ]}]},
        {"total": 2, "availabilities": [{"date": "2026-10-16", "slots": [
            {"start_time": "2026-10-16T10:00:00+02:00"}
        ]}]},
    ]
    session = FakeSession(payloads)
    client = DoctolibClient(session=session)
    result = client.check(
        BOOKING_URL,
        {"date_mode": "custom", "earliest_date": "2026-10-01", "latest_date": "2026-10-16",
         "time_zone": "Europe/Berlin"},
        meta=_test_meta(),
        now=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )

    assert result.status == "error"
    assert result.slot_count == 0
    assert result.error_code == "incomplete_availability_response"
    assert result.count_complete is False


def test_availability_transport_uses_profile_without_overriding_user_agent(monkeypatch):
    session = FakeSession([{"total": 0, "availabilities": []}])
    observed = {}

    def session_factory(profile):
        observed["profile"] = profile
        return session

    monkeypatch.setattr("app.doctolib.get_availability_session", session_factory)
    client = DoctolibClient(user_agent="legacy-agent", page_days=15)
    result = client.check(
        BOOKING_URL,
        {"date_mode": "custom", "earliest_date": "2026-10-01", "latest_date": "2026-10-01",
         "time_zone": "Europe/Berlin"},
        meta=_test_meta(),
        now=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )

    assert result.status == "no_availability"
    assert observed["profile"] == "safari2601"
    assert "headers" not in session.calls[0][1]
    assert session.calls[0][1]["allow_redirects"] is False


def test_cli_compatibility_wrappers_keep_metadata_and_availability_transports_separate(monkeypatch):
    metadata_session = FakeSession([fixture("info_de.json")])
    availability_session = FakeSession([{"total": 0, "availabilities": []}])
    observed = {}
    gate_calls = []

    def session_factory(profile):
        observed["profile"] = profile
        return availability_session

    monkeypatch.setattr("app.doctolib.get_availability_session", session_factory)
    config = {
        "user_agent": "metadata-agent",
        "doctolib_profile": "safari2601",
        "time_zone": "Europe/Berlin",
        "polling": {"upcoming_days": 1, "page_days": 15, "insurance_sector": "public"},
    }

    meta = get_booking_metadata(
        BOOKING_URL, config, metadata_session, before_request=lambda: gate_calls.append("metadata")
    )
    result = fetch_slot_total(
        BOOKING_URL, config, metadata_session, meta,
        before_request=lambda: gate_calls.append("availability"),
    )

    assert observed["profile"] == "safari2601"
    assert metadata_session.calls[0][1]["headers"] == {"User-Agent": "metadata-agent"}
    assert "headers" not in availability_session.calls[0][1]
    assert availability_session.calls[0][1]["params"]["limit"] == 1
    assert gate_calls == ["metadata", "availability"]
    assert result[3] == 0


def test_duplicate_availability_slots_are_counted_once():
    payload = {"total": 2, "availabilities": [{"date": "2026-10-01", "slots": [
        {"start_time": "2026-10-01T10:00:00+02:00"},
        {"start_time": "2026-10-01T10:00:00+02:00"},
    ]}]}
    client = DoctolibClient(session=FakeSession([payload]))
    result = client.check(
        BOOKING_URL,
        {"date_mode": "custom", "earliest_date": "2026-10-01", "latest_date": "2026-10-01",
         "time_zone": "Europe/Berlin"},
        meta=_test_meta(),
        now=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )

    assert result.status == "available"
    assert result.slot_count == 1


def test_string_encoded_slot_times_are_valid_availability_evidence():
    client = DoctolibClient(session=FakeSession([{
        "total": 1, "availabilities": [{"date": "2026-10-01", "slots": ["2026-10-01T10:00:00+02:00"]}]
    }]))
    result = client.check(
        BOOKING_URL,
        {"date_mode": "custom", "earliest_date": "2026-10-01", "latest_date": "2026-10-01",
         "time_zone": "Europe/Berlin"},
        meta=_test_meta(),
        now=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    assert result.status == "available" and result.count_complete
    assert result.earliest_slot == datetime(2026, 10, 1, 8, tzinfo=timezone.utc)


def test_invalid_total_cannot_become_complete_negative_evidence():
    responses = [
        {"availabilities": []},
        {"total": 0.5, "availabilities": []},
        {"total": True, "availabilities": []},
        {"total": "0", "availabilities": []},
    ]
    for payload in responses:
        result = DoctolibClient(session=FakeSession([payload])).check(
            BOOKING_URL,
            {"date_mode": "custom", "earliest_date": "2026-10-01", "latest_date": "2026-10-01",
             "time_zone": "Europe/Berlin"},
            meta=_test_meta(),
            now=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        assert result.status == "error" and not result.count_complete
        assert result.error_category == "malformed_response"


def test_invalid_total_cannot_become_complete_negative_evidence():
    responses = [
        {"availabilities": []},
        {"total": 0.5, "availabilities": []},
        {"total": True, "availabilities": []},
        {"total": "0", "availabilities": []},
    ]
    for payload in responses:
        result = DoctolibClient(session=FakeSession([payload])).check(
            BOOKING_URL,
            {"date_mode": "custom", "earliest_date": "2026-10-01", "latest_date": "2026-10-01",
             "time_zone": "Europe/Berlin"},
            meta=_test_meta(),
            now=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        assert result.status == "error" and not result.count_complete
        assert result.error_category == "malformed_response"


def test_malformed_slot_is_an_error_instead_of_a_midnight_appointment():
    client = DoctolibClient(session=FakeSession([
        {"total": 1, "availabilities": [{"date": "2026-10-01", "slots": [{}]}]}
    ]))
    result = client.check(
        BOOKING_URL,
        {"date_mode": "custom", "earliest_date": "2026-10-01", "latest_date": "2026-10-01",
         "time_zone": "Europe/Berlin"},
        meta=_test_meta(), now=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )

    assert result.status == "error"
    assert result.error_code == "invalid_availability_response"
    assert result.earliest_slot is None


def test_past_slots_are_not_reported_as_available():
    client = DoctolibClient(session=FakeSession([
        {"total": 2, "availabilities": [{"date": "2026-10-01", "slots": [
            {"start_time": "2026-10-01T09:00:00+02:00"},
            {"start_time": "2026-10-01T13:00:00+02:00"},
        ]}]}
    ]))
    result = client.check(
        BOOKING_URL,
        {"date_mode": "custom", "earliest_date": "2026-10-01", "latest_date": "2026-10-01",
         "time_zone": "Europe/Berlin"},
        meta=_test_meta(), now=datetime(2026, 10, 1, 9, tzinfo=timezone.utc),
    )

    assert result.status == "available"
    assert result.slot_count == 1
    assert result.earliest_slot.isoformat() == "2026-10-01T11:00:00+00:00"


def test_first_available_horizon_counts_calendar_dates_including_today():
    _zone, first, last = DoctolibClient._window(
        {"date_mode": "first_available", "horizon_days": 15, "time_zone": "Europe/Berlin"},
        datetime(2026, 10, 1, 10, tzinfo=timezone.utc),
    )
    assert first.isoformat() == "2026-10-01"
    assert last.isoformat() == "2026-10-15"


def test_availability_redirects_are_gated_and_allowlisted():
    session = FakeSession([
        Response({}, status=302, headers={"Location": "https://doctolib.de/availability"}),
        Response({"total": 0, "availabilities": []}),
    ])
    gate_calls = []
    client = DoctolibClient(
        availability_session=session,
        before_request=lambda: gate_calls.append(True),
    )

    assert client._get("https://www.doctolib.de/availability", availability=True).json()["total"] == 0
    assert len(session.calls) == len(gate_calls) == 2
    assert all("headers" not in call[1] for call in session.calls)

    untrusted = FakeSession([Response({}, status=302, headers={"Location": "https://example.org/collect"})])
    client = DoctolibClient(availability_session=untrusted)
    with pytest.raises(BookingUrlError, match="unsupported host"):
        client._get("https://www.doctolib.de/availability", availability=True)


def test_failed_later_page_raises_without_returning_partial_result():
    class SecondPageFails(FakeSession):
        def get(self, url, **kwargs):
            if self.calls:
                raise requests.ConnectionError("simulated failure")
            return super().get(url, **kwargs)

    session = SecondPageFails([{"total": 1, "availabilities": [{"date": "2026-10-01", "slots": [
        {"start_time": "2026-10-01T10:00:00+02:00"}
    ]}]}])
    client = DoctolibClient(session=session)
    with pytest.raises(requests.ConnectionError):
        client.check(
            BOOKING_URL,
            {"date_mode": "custom", "earliest_date": "2026-10-01", "latest_date": "2026-10-16",
             "time_zone": "Europe/Berlin"},
            meta=_test_meta(),
            now=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
