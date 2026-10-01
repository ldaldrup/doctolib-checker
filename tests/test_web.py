"""Connected UI hosting boundaries, using a disposable empty backend."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.app import WebFiles, create_app
from app.settings import Settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Hosting must resolve from the package location, not process cwd. No
    # Doctolib transport is needed for these empty-database/read-only requests.
    monkeypatch.chdir(tmp_path)
    settings = Settings(database_path=str(tmp_path / "checker.sqlite3"))
    with TestClient(create_app(settings=settings, doctolib=object())) as client:
        yield client


def test_root_and_native_assets(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<main' in response.text
    assert response.headers["cache-control"] == "no-cache"

    for path, mime_types in (
        ("/assets/js/main.js", ("text/javascript", "application/javascript")),
        ("/assets/css/tokens.css", ("text/css",)),
        ("/assets/fonts/InterVariable.woff2", ("font/woff2",)),
        ("/assets/favicon.svg", ("image/svg+xml",)),
    ):
        asset = client.get(path)
        assert asset.status_code == 200
        assert asset.headers["content-type"].split(";")[0] in mime_types
        assert asset.headers["cache-control"] == "no-cache"
        assert asset.headers.get("etag")
        revalidated = client.get(path, headers={"If-None-Match": asset.headers["etag"]})
        assert revalidated.status_code == 304
        assert revalidated.headers["cache-control"] == "no-cache"


def test_api_and_health_routes_still_take_precedence(client):
    assert client.get("/healthz").status_code == 200
    jobs = client.get("/api/v1/jobs")
    assert jobs.status_code == 200
    assert jobs.headers["content-type"].startswith("application/json")
    assert jobs.headers["cache-control"] == "no-store"
    assert jobs.json() == []
    settings = client.get("/api/v1/settings")
    assert settings.status_code == 200
    assert settings.headers["cache-control"] == "no-store"
    assert settings.json()["minimum_poll_interval_seconds"] == 300
    updated = client.put("/api/v1/settings", json={
        "default_interval_seconds": 600, "request_spacing_seconds": 3.5,
    })
    assert updated.status_code == 200
    assert updated.json()["default_interval_seconds"] == 600
    assert updated.json()["request_spacing_seconds"] == 3.5


@pytest.mark.parametrize("path", [
    "/unknown-page", "/assets/unknown.js", "/api/v1/unknown",
    "/.env", "/requirements.txt", "/README.md", "/config.json",
    "/app/settings.py", "/tests/fixtures/info_de.json", "/checker.sqlite3",
    "/__test/contracts", "/__test/responsive",
    "/%2e%2e/settings.py", "/assets/%2e%2e/%2e%2e/%2e%2e/settings.py",
])
def test_unknown_and_private_paths_are_not_spa_fallbacks(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert not response.headers["content-type"].startswith("text/html")


def test_static_mount_does_not_follow_symlinks_outside_web(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    private_file = tmp_path / "private.txt"
    private_file.write_text("test-owned-private-content")
    (web / "outside.txt").symlink_to(private_file)
    app = FastAPI()
    app.mount("/", WebFiles(directory=web, html=True))
    with TestClient(app) as client:
        assert client.get("/outside.txt").status_code == 404


def test_static_files_reject_write_methods(client):
    response = client.post("/assets/js/main.js", content="changed")
    assert response.status_code == 405


def test_same_origin_hosting_does_not_enable_cross_origin_api(client):
    response = client.get("/api/v1/jobs", headers={"Origin": "https://other.example"})
    assert "access-control-allow-origin" not in response.headers
    preflight = client.options("/api/v1/jobs", headers={
        "Origin": "https://other.example", "Access-Control-Request-Method": "POST",
    })
    assert "access-control-allow-origin" not in preflight.headers
