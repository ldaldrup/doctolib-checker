"""Runtime settings shared by the API and worker processes."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_path: str = "./data/checker.sqlite3"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = False
    user_agent: str = "DoctolibChecker/2.0"
    default_timezone: str = "Europe/Berlin"
    minimum_poll_interval_seconds: int = 300
    request_spacing_seconds: float = 3.0
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: str = "INFO"
    check_interval_seconds: float = 2.0

    @classmethod
    def from_env(cls):
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("LOG_LEVEL must be CRITICAL, ERROR, WARNING, INFO, or DEBUG")
        return cls(
            database_path=os.getenv("DATABASE_PATH", "./data/checker.sqlite3"),
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
            telegram_enabled=bool(token and chat_id),
            user_agent=os.getenv("USER_AGENT", "DoctolibChecker/2.0"),
            default_timezone=os.getenv("DEFAULT_TIMEZONE", "Europe/Berlin"),
            minimum_poll_interval_seconds=max(
                300, int(os.getenv("MINIMUM_POLL_INTERVAL_SECONDS", "300"))
            ),
            request_spacing_seconds=max(
                3.0, float(os.getenv("REQUEST_SPACING_SECONDS", "3"))
            ),
            api_host=os.getenv("API_HOST", "127.0.0.1"),
            api_port=int(os.getenv("API_PORT", "8000")),
            log_level=log_level,
            check_interval_seconds=max(
                0.5, float(os.getenv("WORKER_TICK_SECONDS", "2"))
            ),
        )
