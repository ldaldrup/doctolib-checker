"""Worker scheduler loop."""

import logging
import time

from app.doctolib import DoctolibClient
from app.services.checks import CheckService
from app.settings import Settings
from app.storage.db import Database
from app.storage.repositories import Repository


def create_check_service(settings=None, repository=None, doctolib=None, notifier=None):
    settings = settings or Settings.from_env()
    if repository is None:
        database = Database(settings.database_path)
        database.initialize()
        repository = Repository(database)
    repository.configure_minimum(settings.minimum_poll_interval_seconds)
    if doctolib is None:
        def before_request():
            repository.touch_worker()
            current = repository.settings(
                settings.minimum_poll_interval_seconds, settings.request_spacing_seconds
            )
            repository.reserve_request_turn(float(current["request_spacing_seconds"]), deadline=doctolib.deadline)
        doctolib = DoctolibClient(
            user_agent=settings.user_agent,
            before_request=before_request,
            profile=settings.doctolib_profile,
            page_days=settings.doctolib_page_days,
        )
    return CheckService(repository, doctolib, settings, notifier=notifier) if notifier else CheckService(repository, doctolib, settings)


def run_forever(settings=None, check_service=None):
    settings = settings or Settings.from_env()
    check_service = check_service or create_check_service(settings)
    repository = check_service.repository
    repository.interrupt_stale_runs()
    logging.info("Doctolib availability worker started")
    while True:
        repository.touch_worker()
        check_service.run_due()
        time.sleep(settings.check_interval_seconds)
