"""Container entry point for the polling worker."""

import logging

from app.settings import Settings
from app.worker.scheduler import run_forever


def main():
    settings = Settings.from_env()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(message)s")
    run_forever(settings=settings)


if __name__ == "__main__":
    main()
