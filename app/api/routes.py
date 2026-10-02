"""Versioned job, result, and settings API."""

from datetime import datetime, timedelta
import hashlib
import json
import re

from fastapi import APIRouter, HTTPException, Query, Request, Response, Header, status
from requests import RequestException

from app.api.schemas import JobCreateRequest, JobUpdateRequest, SettingsUpdateRequest, TargetValidationRequest, VersionedRequest
from app.doctolib import BookingUrlError, MetadataResolutionError, parse_booking_url
from app.services.jobs import create_job, resolve_target, update_job
from app.storage.repositories import ConflictError, NotFoundError, VersionConflictError, CreateReservationLostError, iso, utc_now


def create_router():
    router = APIRouter()

    @router.get("/healthz")
    def healthz(request: Request):
        repository = request.app.state.repository
        try:
            with repository.database.connection() as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception:
            raise HTTPException(status_code=503, detail="not_ready")
        return {"status": "ok"}

    @router.get("/api/v1/status")
    def status_view(request: Request):
        result = request.app.state.repository.dashboard_status()
        result["api_alive"] = True
        result["api_checked_at"] = iso(utc_now())
        heartbeat = result.get("worker")
        if heartbeat:
            last_seen = utc_now() - datetime.fromisoformat(
                heartbeat["last_seen_at"].replace("Z", "+00:00")
            )
            # Availability requests refresh the polling worker heartbeat.
            result["worker_alive"] = last_seen <= timedelta(seconds=90)
        else:
            result["worker_alive"] = False
        dispatcher = result.get("dispatcher")
        result["dispatcher_alive"] = bool(dispatcher and utc_now() - datetime.fromisoformat(
            dispatcher["last_seen_at"].replace("Z", "+00:00")
        ) <= timedelta(seconds=90))
        result["telegram_configured"] = request.app.state.settings.telegram_enabled
        return result

    @router.get("/api/v1/jobs")
    def list_jobs(request: Request, job_status: str = Query(default=None, alias="status"),
                  limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0)):
        if job_status not in (None, "active", "paused"):
            raise HTTPException(status_code=422, detail="status must be active or paused")
        return request.app.state.repository.list_jobs(status=job_status, limit=limit, offset=offset)

    @router.post("/api/v1/targets/validate")
    def validate_target(body: TargetValidationRequest, request: Request):
        doctolib = request.app.state.doctolib
        try:
            target = resolve_target(body.booking_url, doctolib)
            parts = parse_booking_url(target["booking_url"])
        except (BookingUrlError, MetadataResolutionError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except RequestException:
            raise HTTPException(status_code=502, detail="doctolib_unavailable")
        return {
            "valid": True,
            "booking_url": parts["url"],
            "country": target["country"],
            "profile_slug": target["profile_slug"],
            "practice_id": target["practice_id"],
            "motive_id": target["motive_id"],
            "motive_name": target["motive_name"],
            "practitioner_id": target["practitioner_id"],
            "practitioner_name": target["practitioner_name"],
            "practice_name": target["practice_name"],
        }

    @router.post("/api/v1/jobs", status_code=status.HTTP_201_CREATED)
    def add_job(body: JobCreateRequest, request: Request, response: Response,
                idempotency_key: str = Header(alias="Idempotency-Key")):
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
            raise HTTPException(status_code=422, detail={"code": "invalid_idempotency_key"})
        settings = request.app.state.settings
        repository = request.app.state.repository
        # Fingerprint only supplied validated fields. Omitted server defaults
        # remain omitted here and are frozen in the first owning reservation.
        supplied = body.model_dump(mode="json", exclude_unset=True)
        try:
            supplied["target_urls"] = [parse_booking_url(url)["url"] for url in body.target_urls]
            if len(set(supplied["target_urls"])) != len(supplied["target_urls"]):
                raise ValueError("duplicate targets")
        except (BookingUrlError, ValueError):
            raise HTTPException(status_code=422, detail={"code": "invalid_job", "retryable": False})
        fingerprint = hashlib.sha256(json.dumps(supplied, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        values = body.model_dump(mode="json")
        values["target_urls"] = supplied["target_urls"]
        db_settings = repository.settings(settings.minimum_poll_interval_seconds, settings.request_spacing_seconds)
        if "time_zone" not in body.model_fields_set:
            values["time_zone"] = settings.default_timezone
        if "interval_seconds" not in body.model_fields_set:
            values["interval_seconds"] = db_settings["default_interval_seconds"]
        if values["telegram_enabled"] is None:
            values["telegram_enabled"] = settings.telegram_enabled
        try:
            operation = repository.reserve_create(idempotency_key, fingerprint, values)
        except ConflictError:
            raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"})
        if operation["state"] != "owned":
            return creation_outcome(operation, response)
        try:
            canonical = operation["canonical_values"]
            if canonical["interval_seconds"] < settings.minimum_poll_interval_seconds:
                raise ValueError("interval below server minimum")
            job = create_job(repository, request.app.state.doctolib, settings, canonical, operation=operation)
            response.status_code = 201
            return job
        except CreateReservationLostError as exc:
            return creation_outcome(exc.operation, response)
        except (BookingUrlError, MetadataResolutionError, ValueError):
            outcome = fail_creation(repository, operation, "invalid_job", retryable=False)
            return creation_outcome(outcome, response)
        except RequestException:
            outcome = fail_creation(repository, operation, "doctolib_unavailable", retryable=True)
            return creation_outcome(outcome, response)

    @router.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        job = request.app.state.repository.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        job["last_result"] = request.app.state.repository.latest_result(job_id)
        return job

    @router.patch("/api/v1/jobs/{job_id}")
    def patch_job(job_id: str, body: JobUpdateRequest, request: Request):
        values = body.model_dump(exclude_unset=True)
        expected_version = values.pop("expected_version")
        if ("interval_seconds" in values and
                values["interval_seconds"] < request.app.state.settings.minimum_poll_interval_seconds):
            raise HTTPException(status_code=422, detail="interval_seconds is below the server minimum")
        if "earliest_date" in values and values["earliest_date"] is not None:
            values["earliest_date"] = values["earliest_date"].isoformat()
        if "latest_date" in values and values["latest_date"] is not None:
            values["latest_date"] = values["latest_date"].isoformat()
        effective = request.app.state.repository.get_job(job_id)
        if effective is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        # Validate a draft against the configuration it was actually based on.
        # Final repository CAS still closes races during metadata resolution.
        if effective["edit_version"] != expected_version:
            raise version_conflict(VersionConflictError(effective["edit_version"]))
        date_mode = values.get("date_mode", effective["date_mode"])
        if date_mode != "custom" and any(
            values.get(field) is not None for field in ("earliest_date", "latest_date")
        ):
            raise HTTPException(status_code=422, detail="date range is only valid with custom date mode")
        if date_mode == "custom":
            earliest = values.get("earliest_date", effective["earliest_date"])
            latest = values.get("latest_date", effective["latest_date"])
            if earliest is None or latest is None:
                raise HTTPException(status_code=422, detail="custom date mode requires an inclusive date range")
            if isinstance(earliest, str):
                earliest = datetime.fromisoformat(earliest).date()
            if isinstance(latest, str):
                latest = datetime.fromisoformat(latest).date()
            if (latest - earliest).days + 1 > 366:
                raise HTTPException(status_code=422, detail="custom date range must not exceed 366 calendar dates")
        try:
            job = update_job(request.app.state.repository, request.app.state.doctolib,
                             request.app.state.settings, job_id, values, expected_version=expected_version)
        except VersionConflictError as exc:
            raise version_conflict(exc)
        except NotFoundError:
            raise HTTPException(status_code=404, detail="job_not_found")
        except (BookingUrlError, MetadataResolutionError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except RequestException:
            raise HTTPException(status_code=502, detail="doctolib_unavailable")
        if job is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        return job

    @router.post("/api/v1/jobs/{job_id}/pause")
    def pause_job(job_id: str, body: VersionedRequest, request: Request):
        try:
            return request.app.state.repository.set_status(job_id, "paused", expected_version=body.expected_version)
        except VersionConflictError as exc:
            raise version_conflict(exc)
        except NotFoundError:
            raise HTTPException(status_code=404, detail="job_not_found")

    @router.post("/api/v1/jobs/{job_id}/resume")
    def resume_job(job_id: str, body: VersionedRequest, request: Request):
        try:
            return request.app.state.repository.set_status(job_id, "active", expected_version=body.expected_version)
        except VersionConflictError as exc:
            raise version_conflict(exc)
        except NotFoundError:
            raise HTTPException(status_code=404, detail="job_not_found")

    @router.delete("/api/v1/jobs/{job_id}")
    def delete_job(job_id: str, body: VersionedRequest, request: Request):
        try:
            return request.app.state.repository.set_status(job_id, "deleted", expected_version=body.expected_version)
        except VersionConflictError as exc:
            raise version_conflict(exc)
        except NotFoundError:
            raise HTTPException(status_code=404, detail="job_not_found")

    @router.post("/api/v1/jobs/{job_id}/check-now")
    def check_now(job_id: str, request: Request):
        try:
            intent = request.app.state.repository.request_check(job_id)
        except NotFoundError:
            raise HTTPException(status_code=404, detail="job_not_found")
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return intent

    @router.get("/api/v1/jobs/{job_id}/checks")
    def checks(job_id: str, request: Request, limit: int = Query(default=50, ge=1, le=100),
               offset: int = Query(default=0, ge=0)):
        # Jobs are soft-deleted so their check and alert history stays available.
        if request.app.state.repository.get_job(job_id, include_deleted=True) is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        return request.app.state.repository.checks(job_id, limit, offset)

    @router.get("/api/v1/alerts")
    def alerts(request: Request, limit: int = Query(default=50, ge=1, le=100),
               offset: int = Query(default=0, ge=0)):
        return request.app.state.repository.alerts(limit, offset)

    @router.get("/api/v1/settings")
    def get_settings(request: Request):
        settings = request.app.state.settings
        values = request.app.state.repository.settings(
            settings.minimum_poll_interval_seconds, settings.request_spacing_seconds
        )
        return {"edit_version": values["edit_version"], "default_interval_seconds": values["default_interval_seconds"],
                "request_spacing_seconds": values["request_spacing_seconds"],
                "minimum_poll_interval_seconds": settings.minimum_poll_interval_seconds,
                "telegram_configured": settings.telegram_enabled,
                "time_zone": settings.default_timezone}

    @router.put("/api/v1/settings")
    def put_settings(body: SettingsUpdateRequest, request: Request):
        settings = request.app.state.settings
        values = body.model_dump(exclude_unset=True)
        expected_version = values.pop("expected_version")
        try:
            saved = request.app.state.repository.update_settings(values, settings.minimum_poll_interval_seconds, expected_version=expected_version)
        except VersionConflictError as exc:
            raise version_conflict(exc)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"edit_version": saved["edit_version"], "default_interval_seconds": saved["default_interval_seconds"],
                "request_spacing_seconds": saved["request_spacing_seconds"],
                "minimum_poll_interval_seconds": settings.minimum_poll_interval_seconds,
                "telegram_configured": settings.telegram_enabled,
                "time_zone": settings.default_timezone}

    return router


def version_conflict(exc):
    return HTTPException(status_code=409, detail={"code": "version_conflict",
                                                "current_version": exc.current_version})


def creation_outcome(operation, response):
    state = operation["state"]
    if state == "completed":
        response.status_code = 200
        return operation["job"]
    if state == "failed":
        retryable = operation.get("retryable", False)
        raise HTTPException(status_code=502 if retryable else 422,
            detail={"code": operation.get("error_code", "invalid_job"), "retryable": retryable})
    response.status_code = 202
    response.headers["Retry-After"] = "2"
    return {"status": "in_progress", "retry_after_seconds": 2}


def fail_creation(repository, operation, code, retryable):
    try:
        return repository.fail_create(operation, code, retryable=retryable)
    except CreateReservationLostError as exc:
        return exc.operation
