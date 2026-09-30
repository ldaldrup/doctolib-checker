"""Uvicorn import target for the API container."""

import uvicorn

from app.api.app import create_app
from app.settings import Settings


settings = Settings.from_env()
app = create_app(settings=settings)


def main():
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
