"""Disposable local browser harness; never included in the application image.

Run from the repo root with .venv/bin/python -m tests.ui_harness
--directory /tmp/<fresh-test-owned-dir> --port <unused-port>.
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
from app.services.delivery import DeliveryService
from app.notifications import DeliveryOutcome
from cryptography.fernet import Fernet
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
    agenda_id = 1234
    metadata_failure = False

    def get(self, url, **kwargs):
        fixture = "info_de.json" if url.endswith("info.json") else "availability_window.json"
        if fixture == "info_de.json" and self.metadata_failure:
            response = FixtureResponse({})
            response.status_code = 403
            return response
        payload = json.loads((FIXTURES / fixture).read_text())
        if fixture == "info_de.json":
            payload["data"]["agendas"][0]["id"] = self.agenda_id
        return FixtureResponse(payload)


class FixtureDoctolib(DoctolibClient):
    def check(self, booking_url, search, meta=None, now=None):
        if "source=fail" in booking_url:
            raise requests.Timeout("test-owned upstream failure")
        return super().check(booking_url, search, meta=meta, now=now)


def create_harness(directory):
    directory.mkdir(parents=True, exist_ok=True)
    settings = Settings(database_path=str(directory / "checker.sqlite3"), notification_secret_key=Fernet.generate_key().decode(), telegram_bot_token="12345:synthetic_fixture_token_for_ui_only", telegram_chat_id="-100000001", telegram_enabled=True)
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
                            '''<script>
function reportFailure(message) {
  document.documentElement.dataset.contracts = 'failed';
  document.querySelector('#results').textContent += '\\nFAIL browser: ' + message + '\\n';
}
window.addEventListener('error', event => reportFailure(event.message || 'Script failed to load'));
window.addEventListener('unhandledrejection', event => reportFailure(event.reason?.message || String(event.reason)));
</script>'''
                            f'<script type="module">import("/__test/contracts.js?v={Path(__file__).with_name("ui_contracts.js").stat().st_mtime_ns}")'
                            '.catch(error => reportFailure(error.message));</script></html>')

    @app.get("/__test/contracts.js", include_in_schema=False)
    def contracts_script():
        return FileResponse(Path(__file__).with_name("ui_contracts.js"), media_type="text/javascript")

    @app.get("/__test/responsive", include_in_schema=False)
    def responsive():
        return HTMLResponse('''<!doctype html><html lang="en"><title>Responsive UI checks</title>
<style>body{margin:8px;font:14px system-ui}iframe{display:block;border:1px solid #999;margin-top:8px}</style>
<label>Width <select id="width"><option>1280</option><option>1092</option><option>768</option><option>390</option><option>320</option></select></label>
<label>Page <select id="page"><option value="jobs">Jobs</option><option value="activity">Activity</option><option value="settings">Settings</option></select></label>
<iframe title="Connected UI at selected width" width="1280" height="928" src="/#jobs"></iframe>
<script>const frame=document.querySelector('iframe');
document.querySelector('#width').onchange=e=>frame.width=e.target.value;
document.querySelector('#page').onchange=e=>frame.src='/#'+e.target.value;</script></html>''')

    @app.post("/__test/metadata", include_in_schema=False)
    def metadata(agenda_id: int = 1234, failure: bool = False):
        doctolib.metadata_session.agenda_id = agenda_id
        doctolib.metadata_session.metadata_failure = failure
        return {"configured": True}

    @app.post("/__test/reset-worker", include_in_schema=False)
    def reset_worker():
        with database.connection() as connection:
            connection.execute("DELETE FROM worker_heartbeat")
        return {"reset": True}

    @app.post("/__test/checks", include_in_schema=False)
    def run_checks():
        return CheckService(repository, doctolib, settings).run_due()

    @app.post("/__test/deliver", include_in_schema=False)
    def run_delivery(outcome: str = "sent"):
        def fixture_sender(settings, alert):
            return DeliveryOutcome("uncertain" if outcome == "uncertain" else "sent", attempted=True)
        return {"processed": DeliveryService(repository, settings, sender=fixture_sender).run_once()}

    app.router.routes.append(static_mount)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--port", type=int, default=9376)
    args = parser.parse_args()
    uvicorn.run(create_harness(args.directory), host="127.0.0.1", port=args.port, access_log=False)
