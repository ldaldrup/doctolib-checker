"""Disposable local browser harness; never included in the application image.

Run with PYTHONPATH=. python tests/ui_harness.py --directory /tmp/<test-owned-dir>.
Only a loopback server is opened. Upstream metadata/checks use existing fixtures.
Control faults with control.json in the test-owned directory. Stop the process
and remove only that directory after testing.
"""

import argparse
import json
from pathlib import Path

import requests
import uvicorn
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

from app.api.app import create_app
from app.doctolib import DoctolibClient
from app.models import AvailabilityResult
from app.services.checks import CheckService
from app.settings import Settings
from app.storage.db import Database
from app.storage.repositories import Repository


FIXTURES = Path(__file__).parent / "fixtures"


class FixtureResponse:
    status_code = 200
    headers = {}
    is_redirect = False

    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value

    def raise_for_status(self):
        pass


class FixtureSession:
    def get(self, url, **kwargs):
        fixture = "info_de.json" if url.endswith("info.json") else "availability_window.json"
        return FixtureResponse(json.loads((FIXTURES / fixture).read_text()))


class FixtureDoctolib(DoctolibClient):
    def check(self, booking_url, search, meta=None, now=None):
        if "source=fail" in booking_url:
            raise requests.Timeout("test-owned upstream failure")
        return super().check(booking_url, search, meta=meta, now=now)


def create_harness(directory):
    directory.mkdir(parents=True, exist_ok=True)
    settings = Settings(database_path=str(directory / "checker.sqlite3"))
    database = Database(settings.database_path)
    database.initialize()
    repository = Repository(database)
    doctolib = FixtureDoctolib(session=FixtureSession())
    app = create_app(settings, repository, doctolib)

    @app.middleware("http")
    async def test_faults(request, call_next):
        control_path = directory / "control.json"
        control = json.loads(control_path.read_text()) if control_path.exists() else {}
        if request.url.path.startswith("/api/"):
            if control.get("auth"):
                return HTMLResponse("Test session expired", status_code=401)
            if request.method == "GET" and control.get("reads_fail"):
                return JSONResponse({"detail": "Test-owned read failure"}, status_code=503)
            if request.method in {"POST", "PATCH", "PUT", "DELETE"} and control.get("writes_fail"):
                return JSONResponse({"detail": "Test-owned rejected write"}, status_code=422)
        response = await call_next(request)
        if request.method == "POST" and request.url.path == "/api/v1/jobs" and control.get("lose_create_response"):
            return JSONResponse({"detail": "Test-owned lost creation response"}, status_code=503)
        return response

    # Insert test-owned routes before the static mount. They are never added
    # to create_app and cannot exist in the production image.
    static_mount = app.router.routes.pop()

    @app.get("/__test/contracts", include_in_schema=False)
    def contracts():
        return HTMLResponse('<!doctype html><html lang="en"><title>UI contracts</title>'
                            '<h1>Native browser contracts</h1><pre id="results">Running…</pre>'
                            '<script type="module" src="/__test/contracts.js"></script></html>')

    @app.get("/__test/contracts.js", include_in_schema=False)
    def contracts_script():
        return FileResponse(Path(__file__).with_name("ui_contracts.js"), media_type="text/javascript")

    @app.get("/__test/responsive", include_in_schema=False)
    def responsive():
        return HTMLResponse('''<!doctype html><html lang="en"><title>Responsive UI checks</title>
<style>body{margin:8px;font:14px system-ui}iframe{display:block;border:1px solid #999;margin-top:8px}</style>
<label>Width <select id="width"><option>1280</option><option>1092</option><option>768</option><option>390</option></select></label>
<label>Page <select id="page"><option value="jobs">Jobs</option><option value="settings">Settings</option></select></label>
<iframe title="Connected UI at selected width" width="1280" height="928" src="/#jobs"></iframe>
<script>const frame=document.querySelector('iframe');
document.querySelector('#width').onchange=e=>frame.width=e.target.value;
document.querySelector('#page').onchange=e=>frame.src='/#'+e.target.value;</script></html>''')

    @app.post("/__test/checks", include_in_schema=False)
    def run_checks():
        return CheckService(repository, doctolib, settings, notifier=lambda *args: (True, None)).run_due()

    app.router.routes.append(static_mount)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--port", type=int, default=9376)
    args = parser.parse_args()
    uvicorn.run(create_harness(args.directory), host="127.0.0.1", port=args.port, access_log=False)
