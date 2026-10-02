"""Versioned job, result, and settings API."""

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request, status
from requests import RequestException

from app.api.schemas import JobCreateRequest, JobUpdateRequest, SettingsUpdateRequest, TargetValidationRequest
from app.doctolib import BookingUrlError, MetadataResolutionError, parse_booking_url
from app.services.jobs import create_job, resolve_target, update_job
from app.storage.repositories import ConflictError, NotFoundError, iso, utc_now


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
    def add_job(body: JobCreateRequest, request: Request):
        settings = request.app.state.settings
        values = body.model_dump()
        supplied = body.model_fields_set
        db_settings = request.app.state.repository.settings(
            settings.minimum_poll_interval_seconds, settings.request_spacing_seconds
        )
        if "time_zone" not in supplied:
            values["time_zone"] = settings.default_timezone
        if "interval_seconds" not in supplied:
            values["interval_seconds"] = db_settings["default_interval_seconds"]
        if values["interval_seconds"] < settings.minimum_poll_interval_seconds:
            raise HTTPException(status_code=422, detail="interval_seconds is below the server minimum")
        try:
            return create_job(request.app.state.repository, request.app.state.doctolib, settings, values)
        except (BookingUrlError, MetadataResolutionError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except RequestException:
            raise HTTPException(status_code=502, detail="doctolib_unavailable")

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
                             request.app.state.settings, job_id, values)
        except (BookingUrlError, MetadataResolutionError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except RequestException:
            raise HTTPException(status_code=502, detail="doctolib_unavailable")
        if job is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        return job

    @router.post("/api/v1/jobs/{job_id}/pause")
    def pause_job(job_id: str, request: Request):
        try:
            return request.app.state.repository.set_status(job_id, "paused")
        except NotFoundError:
            raise HTTPException(status_code=404, detail="job_not_found")

    @router.post("/api/v1/jobs/{job_id}/resume")
    def resume_job(job_id: str, request: Request):
        try:
            return request.app.state.repository.set_status(job_id, "active")
        except NotFoundError:
            raise HTTPException(status_code=404, detail="job_not_found")

    @router.delete("/api/v1/jobs/{job_id}")
    def delete_job(job_id: str, request: Request):
        try:
            return request.app.state.repository.set_status(job_id, "deleted")
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
        return {"default_interval_seconds": values["default_interval_seconds"],
                "request_spacing_seconds": values["request_spacing_seconds"],
                "minimum_poll_interval_seconds": settings.minimum_poll_interval_seconds,
                "telegram_configured": settings.telegram_enabled,
                "time_zone": settings.default_timezone}

    @router.put("/api/v1/settings")
    def put_settings(body: SettingsUpdateRequest, request: Request):
        settings = request.app.state.settings
        values = body.model_dump(exclude_unset=True, exclude_none=True)
        try:
            saved = request.app.state.repository.update_settings(values, settings.minimum_poll_interval_seconds)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"default_interval_seconds": saved["default_interval_seconds"],
                "request_spacing_seconds": saved["request_spacing_seconds"],
                "minimum_poll_interval_seconds": settings.minimum_poll_interval_seconds,
                "telegram_configured": settings.telegram_enabled,
                "time_zone": settings.default_timezone}

    return router
