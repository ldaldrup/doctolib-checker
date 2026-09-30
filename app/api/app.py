"""FastAPI application factory."""

from fastapi import FastAPI

from app.api.routes import create_router
from app.doctolib import DoctolibClient
from app.settings import Settings
from app.storage.db import Database
from app.storage.repositories import Repository


def create_app(settings=None, repository=None, doctolib=None):
    settings = settings or Settings.from_env()
    if repository is None:
        database = Database(settings.database_path)
        database.initialize()
        repository = Repository(database)
    if doctolib is None:
        def before_request():
            values = repository.settings(
                settings.minimum_poll_interval_seconds, settings.request_spacing_seconds
            )
            repository.reserve_request_turn(float(values["request_spacing_seconds"]))
        doctolib = DoctolibClient(
            user_agent=settings.user_agent,
            before_request=before_request,
            profile=settings.doctolib_profile,
            page_days=settings.doctolib_page_days,
        )

    app = FastAPI(title="Doctolib Checker API", version="1.0.0")
    app.state.settings = settings
    app.state.repository = repository
    app.state.doctolib = doctolib
    app.include_router(create_router())
    return app
