import logging
import os
import re

from colorama import Fore, Style, init

from app.config import LOG_DIR, LOG_FILE, STATE_DIR

init(autoreset=True)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def pad_right(text: str, width: int) -> str:
    visible_len = len(strip_ansi(text))
    return text + " " * max(0, width - visible_len)


def truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


class ColorFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: Fore.CYAN,
        logging.INFO: Fore.CYAN,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Style.BRIGHT + Fore.RED,
    }

    def format(self, record):
        log_color = self.LEVEL_COLORS.get(record.levelno, Fore.WHITE)
        time_str = f"{Fore.LIGHTBLACK_EX}%(asctime)s{Style.RESET_ALL}"
        level_str = f"{log_color}[%(levelname)s]{Style.RESET_ALL}"
        msg_str = "%(message)s"
        formatter = logging.Formatter(
            f"{time_str} {level_str} {msg_str}", datefmt="%H:%M:%S"
        )
        return formatter.format(record)


def setup_directories_and_logging():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(ColorFormatter())

    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, console_handler],
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.ERROR)
