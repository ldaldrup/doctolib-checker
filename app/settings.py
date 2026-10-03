"""Runtime settings shared by the API and worker processes."""

import os
import ipaddress
import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_path: str = "./data/checker.sqlite3"
    notification_secret_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = False
    webhook_allowlist: tuple = ()
    # Metadata requests keep using requests and this configurable header.
    # Availability requests use the selected curl_cffi browser profile instead.
    user_agent: str = "DoctolibChecker/2.0"
    doctolib_profile: str = "safari2601"
    doctolib_page_days: int = 15
    default_timezone: str = "Europe/Berlin"
    minimum_poll_interval_seconds: int = 300
    request_spacing_seconds: float = 3.0
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: str = "INFO"
    check_interval_seconds: float = 2.0

    @classmethod
    def from_env(cls):
        from app.notification_secrets import NotificationSecrets
        secret_key = os.getenv("NOTIFICATION_SECRET_KEY", "").strip()
        NotificationSecrets(secret_key)
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("LOG_LEVEL must be CRITICAL, ERROR, WARNING, INFO, or DEBUG")
        profile = os.getenv("DOCTOLIB_PROFILE", "safari2601").strip()
        if profile != "safari2601":
            raise ValueError("DOCTOLIB_PROFILE currently supports only safari2601")
        page_days = int(os.getenv("DOCTOLIB_PAGE_DAYS", "15"))
        if not 1 <= page_days <= 15:
            raise ValueError("DOCTOLIB_PAGE_DAYS must be between 1 and 15")
        return cls(
            webhook_allowlist=_webhook_allowlist(os.getenv("WEBHOOK_PRIVATE_ALLOWLIST", "[]")),
            notification_secret_key=secret_key,
            database_path=os.getenv("DATABASE_PATH", "./data/checker.sqlite3"),
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
            telegram_enabled=bool(token and chat_id),
            user_agent=os.getenv("USER_AGENT", "DoctolibChecker/2.0"),
            doctolib_profile=profile,
            doctolib_page_days=page_days,
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


def _webhook_allowlist(value):
    """Exact operator exceptions; never allow a private network or wildcard."""
    try:
        entries = json.loads(value)
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValueError
        allowed = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"host", "port", "addresses"}:
                raise ValueError
            host, port, addresses = entry["host"], entry["port"], entry["addresses"]
            if (not isinstance(host, str) or not host or len(host) > 253
                    or any(char in host for char in "/@*[]\\ \t\n\r")
                    or type(port) is not int or not 1 <= port <= 65535
                    or not isinstance(addresses, list) or not 1 <= len(addresses) <= 32):
                raise ValueError
            host = host.encode("idna").decode("ascii").lower().rstrip(".")
            try:
                host = str(ipaddress.ip_address(host))
            except ValueError:
                if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                           for label in host.split(".")):
                    raise ValueError
            for address in addresses:
                allowed.append((host, port, str(ipaddress.ip_address(address))))
        return tuple(allowed)
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("WEBHOOK_PRIVATE_ALLOWLIST must contain exact host, port and IP addresses") from None
