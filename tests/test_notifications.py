from app.notifications import format_slot_alert


def test_slot_alert_escapes_booking_data_and_formats_configured_timezone():
    message = format_slot_alert({
        "practitioner_name": "<Dr. Ada>",
        "practice_name": "Praxis & Partner",
        "earliest_slot": "2026-10-15T07:30:00+00:00",
        "booking_url": "https://www.doctolib.de/booking?a=1&b=2",
        "slot_count": 3,
        "time_zone": "Europe/Berlin",
    })

    assert "&lt;Dr. Ada&gt;" in message
    assert "Praxis &amp; Partner" in message
    assert "2026-10-15 09:30 CEST" in message
    assert "a=1&amp;b=2" in message
