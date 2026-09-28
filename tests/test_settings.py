from app.settings import Settings


def test_settings_use_local_api_bind_and_enforce_minimums_by_default(monkeypatch):
    for name in (
        "API_HOST", "API_PORT", "LOG_LEVEL", "MINIMUM_POLL_INTERVAL_SECONDS",
        "REQUEST_SPACING_SECONDS", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8000
    assert settings.minimum_poll_interval_seconds == 300
    assert settings.request_spacing_seconds == 3
    assert settings.telegram_enabled is False
