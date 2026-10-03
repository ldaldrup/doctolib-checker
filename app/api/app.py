"""FastAPI application factory."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import create_router
from app.doctolib import DoctolibClient
from app.settings import Settings
from app.notification_secrets import NotificationSecrets
from app.storage.db import Database
from app.storage.repositories import Repository


class WebFiles(StaticFiles):
    """Serve stable asset names with revalidation after application updates."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(settings=None, repository=None, doctolib=None):
    settings = settings or Settings.from_env()
    secrets = NotificationSecrets(settings.notification_secret_key)
    if repository is None:
        database = Database(settings.database_path)
        database.initialize()
        repository = Repository(database)
    repository.configure_minimum(settings.minimum_poll_interval_seconds)
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
    app.state.notification_secrets = secrets
    app.state.repository = repository
    app.state.doctolib = doctolib

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(_request, exc):
        # Pydantic inputs/contexts can echo private URLs, keys or future secrets.
        errors = [{key: error[key] for key in ("loc", "type", "msg")}
                  for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.middleware("http")
    async def uncached_api(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/") or request.url.path == "/healthz":
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(create_router())
    # Register last so API/health routes retain their normal responses. Hash
    # navigation only needs index.html; unknown paths must remain real 404s.
    web_directory = Path(__file__).resolve().parents[1] / "web"
    app.mount("/", WebFiles(directory=web_directory, html=True), name="web")
    return app
