from app.settings import Settings


def test_settings_use_local_api_bind_and_enforce_minimums_by_default(monkeypatch):
    for name in (
        "API_HOST", "API_PORT", "LOG_LEVEL", "MINIMUM_POLL_INTERVAL_SECONDS",
        "REQUEST_SPACING_SECONDS", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "DOCTOLIB_PROFILE", "DOCTOLIB_PAGE_DAYS", "USER_AGENT",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8000
    assert settings.minimum_poll_interval_seconds == 300
    assert settings.request_spacing_seconds == 3
    assert settings.telegram_enabled is False
    assert settings.doctolib_profile == "safari2601"
    assert settings.doctolib_page_days == 15


def test_settings_validate_transport_profile_and_page_size(monkeypatch):
    monkeypatch.setenv("DOCTOLIB_PROFILE", "safari260")
    try:
        Settings.from_env()
    except ValueError as exc:
        assert "safari2601" in str(exc)
    else:
        raise AssertionError("unsupported profile was accepted")

    monkeypatch.setenv("DOCTOLIB_PROFILE", "safari2601")
    monkeypatch.setenv("DOCTOLIB_PAGE_DAYS", "16")
    try:
        Settings.from_env()
    except ValueError as exc:
        assert "between 1 and 15" in str(exc)
    else:
        raise AssertionError("oversized page size was accepted")
