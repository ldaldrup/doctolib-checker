"""Versioned job, result, and settings API."""

from datetime import datetime, timedelta
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request, Response, Header, Body, status
from requests import RequestException

from app.api.schemas import TargetRevalidationRequest, JobCreateRequest, JobUpdateRequest, SettingsUpdateRequest, TargetValidationRequest, VersionedRequest, ChannelCreateRequest, ChannelUpdateRequest, ChannelDeleteRequest, SmtpTransportUpdateRequest, StrictRequest, NotificationPreviewRequest, ChannelTestRequest, QuietHoursPreviewRequest
from app.notification_secrets import SecretUnavailable
from app.services.channel_tests import notification_preview
from app.services.email_delivery import mailbox, smtp_transport_usable, validate_smtp_host
from app.storage.channel_operations import SmtpImpactChangedError, channel_secret_columns
from app.webhooks import validate_endpoint
from app.doctolib import BookingUrlError, MetadataResolutionError, parse_booking_url
from app.services.jobs import create_job, resolve_target, update_job, revalidate_target
from app.services.jobs import validate_timezone
from app.quiet_hours import next_release, validate_quiet_hours
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
        try:
            result = request.app.state.repository.dashboard_status()
        except Exception:
            return {"api_alive": True, "database_ready": False, "api_checked_at": iso(utc_now()),
                    "worker": None, "dispatcher": None, "worker_alive": False,
                    "dispatcher_alive": False, "delivery_backlog": {}, "overdue_jobs": None,
                    "oldest_ready_delivery_at": None, "last_completed_run": None}
        result["api_alive"] = True
        result["database_ready"] = True
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
        result["telegram_configured"] = any(item['type']=='telegram' and channel_public(request, item)["usable"] for item in request.app.state.repository.list_channels()["items"])
        return result

    @router.post("/api/v1/quiet-hours/preview")
    def preview_quiet_hours(body: QuietHoursPreviewRequest):
        try:
            validate_timezone(body.time_zone)
            validate_quiet_hours(body.enabled, body.start, body.end)
            release = next_release(utc_now(), body.time_zone, body.enabled, body.start, body.end)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if release is None:
            return {"enabled": False, "time_zone": body.time_zone, "next_release_at": None,
                    "preview": "Quiet hours are off. Times use " + body.time_zone +
                               ". Monitoring continues; fresh alerts can be sent immediately. No message sent."}
        local = release.astimezone(ZoneInfo(body.time_zone))
        return {"enabled": True, "time_zone": body.time_zone, "next_release_at": iso(release),
                "preview": "Next eligible release: " + local.strftime("%d.%m.%Y %H:%M") +
                           " (" + body.time_zone + "), after a fresh confirmation. Monitoring continues. No message sent."}

    @router.get("/api/v1/jobs")
    def list_jobs(request: Request, job_status: str = Query(default=None, alias="status"),
                  limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0)):
        if job_status not in (None, "active", "paused"):
            raise HTTPException(status_code=422, detail="status must be active or paused")
        return [job_public(request,job) for job in request.app.state.repository.list_jobs(status=job_status, limit=limit, offset=offset)]

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
            values["telegram_enabled"] = bool(values.get("notification_channel_ids"))
        try:
            operation = repository.reserve_create(idempotency_key, fingerprint, values)
        except ConflictError:
            raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"})
        if operation["state"] != "owned":
            return creation_outcome(operation, response, request)
        try:
            canonical = operation["canonical_values"]
            if canonical["interval_seconds"] < settings.minimum_poll_interval_seconds:
                raise ValueError("interval below server minimum")
            job = create_job(repository, request.app.state.doctolib, settings, canonical, operation=operation)
            response.status_code = 201
            return job_public(request,job)
        except CreateReservationLostError as exc:
            return creation_outcome(exc.operation, response, request)
        except (BookingUrlError, MetadataResolutionError, ValueError):
            outcome = fail_creation(repository, operation, "invalid_job", retryable=False)
            return creation_outcome(outcome, response, request)
        except RequestException:
            outcome = fail_creation(repository, operation, "doctolib_unavailable", retryable=True)
            return creation_outcome(outcome, response, request)

    @router.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        job = request.app.state.repository.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        job["last_result"] = request.app.state.repository.latest_result(job_id)
        return job_public(request,job)

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
        try:
            validate_quiet_hours(values.get("quiet_hours_enabled", effective["quiet_hours_enabled"]),
                                 values.get("quiet_hours_start", effective["quiet_hours_start"]),
                                 values.get("quiet_hours_end", effective["quiet_hours_end"]))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
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
        return job_public(request,job)

    @router.post("/api/v1/jobs/{job_id}/targets/{target_id}/revalidate")
    def repair_target(job_id: str, target_id: str, body: TargetRevalidationRequest, request: Request):
        try:
            outcome = revalidate_target(request.app.state.repository, request.app.state.doctolib,
                request.app.state.settings, job_id, target_id, body.booking_url, body.expected_version)
        except VersionConflictError as exc:
            raise version_conflict(exc)
        except NotFoundError:
            raise HTTPException(status_code=404, detail="job_not_found")
        except ConflictError:
            raise HTTPException(status_code=409, detail={"code": "target_conflict"})
        except BookingUrlError:
            raise HTTPException(status_code=422, detail={"code": "invalid_target"})
        outcome['job'] = job_public(request, outcome['job'])
        return outcome

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

    @router.get("/api/v1/activity")
    def activity(request: Request, job_id: str = Query(default=None, min_length=1, max_length=128),
                 run_id: str = Query(default=None, min_length=1, max_length=128),
                 limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0),
                 before_run_id: str = Query(default=None, min_length=1, max_length=128)):
        if job_id and request.app.state.repository.get_job(job_id, include_deleted=True) is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        if run_id:
            run = request.app.state.repository.activity(job_id=job_id, run_id=run_id, limit=1)
            if not run["items"]:
                raise HTTPException(status_code=404, detail="run_not_found")
            return run
        return request.app.state.repository.activity(job_id=job_id, limit=limit, offset=offset,
                                                     before_run_id=before_run_id)

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
        return {"edit_version": values["edit_version"], "message_content":values["message_content"], "content_version":values["content_version"], "default_interval_seconds": values["default_interval_seconds"],
                "request_spacing_seconds": values["request_spacing_seconds"],
                "minimum_poll_interval_seconds": settings.minimum_poll_interval_seconds,
                "telegram_configured": any(item['type']=='telegram' and channel_public(request,item)["usable"] for item in request.app.state.repository.list_channels()["items"]),
                **notification_boundary(request),
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
        return {"edit_version": saved["edit_version"], "message_content":saved["message_content"], "content_version":saved["content_version"], "default_interval_seconds": saved["default_interval_seconds"],
                "request_spacing_seconds": saved["request_spacing_seconds"],
                "minimum_poll_interval_seconds": settings.minimum_poll_interval_seconds,
                "telegram_configured": any(item['type']=='telegram' and channel_public(request,item)["usable"] for item in request.app.state.repository.list_channels()["items"]),
                **notification_boundary(request),
                "time_zone": settings.default_timezone}

    @router.get('/api/v1/settings/smtp')
    def get_smtp_settings(request: Request):
        return smtp_public(request)

    @router.post('/api/v1/settings/smtp/impact')
    def preview_smtp_settings(body: SmtpTransportUpdateRequest, request: Request):
        repository = request.app.state.repository
        existing = repository.get_smtp_transport(private=True)
        if existing is None:
            raise HTTPException(503,detail={'code':'smtp_transport_unavailable'})
        if existing['edit_version'] != body.expected_version:
            raise version_conflict(VersionConflictError(existing['edit_version']))
        values = smtp_values(request,body.model_dump(),existing)
        destination_changed = (values['destination_identity'] != existing['destination_identity']
            or bool(values['enabled']) != bool(existing['enabled']))
        impact = repository.smtp_delivery_impact()
        if not destination_changed:
            impact['pending_email_deliveries'] = []
        return impact

    @router.put('/api/v1/settings/smtp')
    def put_smtp_settings(body: SmtpTransportUpdateRequest, request: Request):
        repository = request.app.state.repository
        existing = repository.get_smtp_transport(private=True)
        if existing is None:
            raise HTTPException(503,detail={'code':'smtp_transport_unavailable'})
        values = smtp_values(request,body.model_dump(),existing)
        try:
            repository.update_smtp_transport(values,body.expected_version,
                recover_failed=body.recover_failed,expected_impact_token=body.expected_impact_token)
        except VersionConflictError as exc:
            raise version_conflict(exc)
        except SmtpImpactChangedError as exc:
            raise HTTPException(409,detail={'code':'smtp_impact_changed',**exc.impact})
        except NotFoundError:
            raise HTTPException(503,detail={'code':'smtp_transport_unavailable'})
        return smtp_public(request)

    @router.get('/api/v1/channels')
    def channels(request: Request):
        request.app.state.repository.reconcile_channel_tests()
        result = request.app.state.repository.list_channels()
        result['items'] = [channel_public(request,item) for item in result['items']]
        return result

    @router.post('/api/v1/channels')
    def add_channel(body: ChannelCreateRequest,request: Request,response: Response,
                    idempotency_key: str = Header(alias='Idempotency-Key')):
        key_check(idempotency_key)
        supplied = body.model_dump(mode='json')
        repository = request.app.state.repository
        digest = fingerprint(['create',supplied])
        values = lambda: credential_values(request,supplied)
        try:
            channel,created = repository.create_channel(values,idempotency_key,digest)
        except ConflictError:
            raise HTTPException(409,detail={'code':'idempotency_conflict'})
        response.status_code = 201 if created else 200
        return channel_public(request,channel)

    @router.post('/api/v1/channels/import-legacy')
    def import_legacy(request: Request,response: Response,body: StrictRequest = Body(default=None),
                      idempotency_key: str = Header(alias='Idempotency-Key')):
        key_check(idempotency_key)
        settings = request.app.state.settings
        repository = request.app.state.repository
        if repository.legacy_imported():
            # Never re-encrypt/overwrite an already imported destination.
            try:
                channel,_ = repository.create_channel({},idempotency_key,fingerprint(['legacy-import']),legacy=True)
            except ConflictError:
                raise HTTPException(409,detail={'code':'idempotency_conflict'})
            response.status_code = 200
            return channel_public(request,channel)
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            raise HTTPException(409,detail={'code':'legacy_telegram_unavailable'})
        supplied = {'name':'Telegram1','enabled':True,'token_action':'replace','chat_action':'replace',
                    'bot_token':settings.telegram_bot_token,'chat_id':settings.telegram_chat_id}
        values = credential_values(request,supplied)
        try:
            channel,created = repository.create_channel(values,idempotency_key,fingerprint(['legacy-import']),legacy=True)
        except ConflictError:
            raise HTTPException(409,detail={'code':'idempotency_conflict'})
        response.status_code = 201 if created else 200
        return channel_public(request,channel)

    @router.get('/api/v1/channels/{channel_id}')
    def read_channel(channel_id: str,request: Request):
        request.app.state.repository.reconcile_channel_tests()
        channel = request.app.state.repository.get_channel(channel_id)
        if channel is None or channel['deleted']:
            raise HTTPException(404,detail='channel_not_found')
        return channel_public(request,channel)

    @router.patch('/api/v1/channels/{channel_id}')
    def edit_channel(channel_id: str,body: ChannelUpdateRequest,request: Request):
        repository = request.app.state.repository
        existing = repository.get_channel(channel_id,private=True)
        if existing is None or existing['deleted']:
            raise HTTPException(404,detail='channel_not_found')
        if existing['edit_version'] != body.expected_version:
            raise version_conflict(VersionConflictError(existing['edit_version']))
        values = credential_values(request,body.model_dump(exclude_unset=True),existing)
        try:
            channel = repository.update_channel(channel_id,values,body.expected_version,recover_failed=body.recover_failed)
        except VersionConflictError as exc:
            raise version_conflict(exc)
        except NotFoundError:
            raise HTTPException(404,detail='channel_not_found')
        except ValueError:
            raise HTTPException(422,detail={'code':'invalid_channel_recovery'})
        return channel_public(request,channel)

    @router.delete('/api/v1/channels/{channel_id}')
    def remove_channel(channel_id: str,body: ChannelDeleteRequest,request: Request):
        try:
            channel = request.app.state.repository.update_channel(channel_id,{},body.expected_version,delete=True)
        except VersionConflictError as exc:
            raise version_conflict(exc)
        except NotFoundError:
            raise HTTPException(404,detail='channel_not_found')
        return channel_public(request,channel)

    @router.post('/api/v1/notification-preview')
    def preview_notification(body: NotificationPreviewRequest, request: Request):
        settings = request.app.state.settings
        content = body.message_content.model_dump() if body.message_content else request.app.state.repository.settings(settings.minimum_poll_interval_seconds,settings.request_spacing_seconds)['message_content']
        try:
            return notification_preview({'type':body.channel_type},content)
        except ValueError as exc:
            raise HTTPException(422,detail=str(exc))

    @router.get('/api/v1/channels/{channel_id}/preview')
    def preview_channel(channel_id: str,request: Request):
        channel = request.app.state.repository.get_channel(channel_id)
        if channel is None or channel['deleted']:
            raise HTTPException(404,detail='channel_not_found')
        settings = request.app.state.settings
        content = request.app.state.repository.settings(settings.minimum_poll_interval_seconds,settings.request_spacing_seconds)['message_content']
        try:
            return notification_preview(channel, content)
        except ValueError as exc:
            raise HTTPException(422,detail=str(exc))

    @router.post('/api/v1/channels/{channel_id}/tests',status_code=202)
    def test_channel(channel_id: str,body: ChannelTestRequest,request: Request,
                     idempotency_key: str = Header(alias='Idempotency-Key')):
        key_check(idempotency_key)
        repository = request.app.state.repository
        def validate_usable(channel):
            try:
                secrets = request.app.state.notification_secrets
                for column in channel_secret_columns(channel):
                    secrets.decrypt(channel[column])
                if channel['type'] == 'email':
                    mailbox(secrets.decrypt(channel['email_recipient_ciphertext']))
                    if not smtp_public(request)['usable']:
                        raise HTTPException(409,detail={'code':'smtp_transport_unusable'})
            except SecretUnavailable:
                raise HTTPException(409,detail={'code':'channel_unusable'})
        try:
            return repository.reserve_channel_test(channel_id,body.expected_version,idempotency_key,
                fingerprint(['test',channel_id,body.expected_version] + ([body.expected_content_version] if body.expected_content_version is not None else [])),validate_usable=validate_usable,expected_content_version=body.expected_content_version)
        except VersionConflictError as exc:
            raise version_conflict(exc)
        except NotFoundError:
            raise HTTPException(404,detail='channel_not_found')
        except ConflictError as exc:
            raise HTTPException(409,detail={'code':str(exc)})
        except ValueError as exc:
            raise HTTPException(422,detail=str(exc))

    @router.get('/api/v1/channel-tests/{test_id}')
    def read_test(test_id: str,request: Request):
        request.app.state.repository.reconcile_channel_tests()
        result = request.app.state.repository.get_channel_test(test_id)
        if result is None:
            raise HTTPException(404,detail='test_not_found')
        return result

    return router


def version_conflict(exc):
    return HTTPException(status_code=409, detail={"code": "version_conflict",
                                                "current_version": exc.current_version})


def creation_outcome(operation, response, request):
    state = operation["state"]
    if state == "completed":
        response.status_code = 200
        return job_public(request,operation["job"])
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


def key_check(key):
    if not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}',key):
        raise HTTPException(422,detail={'code':'invalid_idempotency_key'})


def fingerprint(values):
    return hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def smtp_public(request):
    repository = request.app.state.repository
    result = repository.get_smtp_transport()
    raw = repository.get_smtp_transport(private=True)
    if result is None or raw is None:
        raise HTTPException(503,detail={'code':'smtp_transport_unavailable'})
    result['usable'] = result['usable'] and smtp_transport_usable(
        raw,request.app.state.notification_secrets,request.app.state.settings.webhook_allowlist)
    return result


def smtp_values(request,supplied,existing):
    secrets = request.app.state.notification_secrets
    host = supplied['host'].strip()
    port = supplied['port']
    tls_mode = supplied['tls_mode']
    transport_address_changed = (host != existing['host'] or port != existing['port'] or
        tls_mode != existing['tls_mode'])
    if host and (supplied['enabled'] or transport_address_changed):
        try:
            host = validate_smtp_host(host,port,tls_mode,request.app.state.settings.webhook_allowlist)
        except ValueError:
            raise HTTPException(422,detail={'code':'smtp_invalid_transport'}) from None
    sender_name = supplied['sender_name'].strip()
    if len(sender_name) > 120 or any(ord(char) < 32 or ord(char) == 127 for char in sender_name):
        raise HTTPException(422,detail={'code':'smtp_invalid_sender'})

    identity_changed = (host != existing['host'] or port != existing['port'] or
        tls_mode != existing['tls_mode'] or sender_name != (existing['sender_name'] or '') or
        supplied['sender_email_action'] != 'keep')
    need_plaintext = bool(supplied['enabled'])
    plaintext, encrypted = {}, {}
    for field,column,label in (
        ('sender_email','sender_email_ciphertext','sender_email'),
        ('username','username_ciphertext','username'),
        ('password','password_ciphertext','password'),
    ):
        action = supplied[label+'_action']
        value = supplied[label]
        if action == 'replace':
            if not secrets.available:
                raise HTTPException(503,detail={'code':'notification_secret_key_unavailable'})
            if label == 'sender_email':
                try:
                    value = mailbox(value.strip())
                except ValueError:
                    raise HTTPException(422,detail={'code':'smtp_invalid_sender'}) from None
            elif (not value or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise HTTPException(422,detail={'code':'smtp_invalid_auth'})
            plaintext[label] = value
            encrypted[column] = secrets.encrypt(value)
        elif action == 'clear':
            encrypted[column] = None
            plaintext[label] = None
        else:
            encrypted[column] = existing[column]
            required_for_identity = label == 'sender_email' and identity_changed and supplied['sender_email_action'] == 'keep'
            if existing[column] and (need_plaintext or required_for_identity):
                try:
                    plaintext[label] = secrets.decrypt(existing[column])
                except SecretUnavailable as exc:
                    raise HTTPException(503,detail={'code':str(exc)}) from None
            else:
                plaintext[label] = None
    if bool(encrypted['username_ciphertext']) != bool(encrypted['password_ciphertext']):
        raise HTTPException(422,detail={'code':'smtp_auth_pair_required'})

    identity = existing['destination_identity']
    if identity_changed:
        identity = None
    if identity_changed and host and plaintext['sender_email']:
        identity = secrets.identity(json.dumps(
            [host,port,tls_mode,sender_name,plaintext['sender_email']],
            ensure_ascii=True,separators=(',',':')))
    if supplied['enabled'] and (not host or not identity or
            bool(encrypted['username_ciphertext']) != bool(encrypted['password_ciphertext'])):
        raise HTTPException(422,detail={'code':'smtp_configuration_incomplete'})
    if supplied['recover_failed'] and identity != existing['destination_identity']:
        raise HTTPException(422,detail={'code':'recovery_requires_same_destination'})
    return {
        'enabled':int(supplied['enabled']),'host':host or None,'port':port,'tls_mode':tls_mode,
        'sender_name':sender_name or None,'destination_identity':identity,**encrypted,
    }


def notification_boundary(request):
    settings = request.app.state.settings
    return {'notification_secret_configured':request.app.state.notification_secrets.available,
            'legacy_telegram_available':bool(settings.telegram_bot_token and settings.telegram_chat_id),
            'legacy_telegram_imported':request.app.state.repository.legacy_imported()}


def channel_public(request,channel):
    public = dict(channel)
    raw = request.app.state.repository.get_channel(channel['id'],private=True)
    try:
        secrets = request.app.state.notification_secrets
        for column in channel_secret_columns(raw):
            secrets.decrypt(raw[column])
        if raw['type'] in ('ntfy','webhook') and raw['endpoint_ciphertext']:
            _,host = validate_endpoint(secrets.decrypt(raw['endpoint_ciphertext']),raw['type'],request.app.state.settings.webhook_allowlist)
            public['endpoint_host'] = host[:253]
        elif raw['type'] == 'email':
            mailbox(secrets.decrypt(raw['email_recipient_ciphertext']))
            public['smtp_usable'] = smtp_public(request)['usable']
            public['usable'] = public['usable'] and public['smtp_usable']
    except SecretUnavailable:
        public['usable'] = False
    except ValueError:
        public['usable'] = False
    public.setdefault('endpoint_host',None)
    return public


def email_values(request,supplied,existing,values):
    secrets = request.app.state.notification_secrets
    unsupported = ('bot_token','chat_id','endpoint','auth_token','auth_username','auth_password')
    if any(supplied.get(field) is not None for field in unsupported):
        raise HTTPException(422,detail={'code':'unsupported_channel_fields'})
    for field,expected in (('token_action','keep' if existing else 'replace'),
                           ('chat_action','keep' if existing else 'replace'),
                           ('endpoint_action','keep' if existing else 'replace'),
                           ('auth_action','keep')):
        if supplied.get(field,expected) != expected:
            raise HTTPException(422,detail={'code':'unsupported_channel_fields'})
    if supplied.get('auth_type','none') != 'none' or supplied.get('ntfy_priority',3) != 3:
        raise HTTPException(422,detail={'code':'unsupported_channel_fields'})
    action = supplied.get('recipient_action','keep' if existing else 'replace')
    address = supplied.get('recipient')
    if action == 'keep':
        if address is not None:
            raise HTTPException(422,detail={'code':'credential_action_required'})
        if supplied.get('recover_failed') and existing:
            values['destination_identity'] = existing['destination_identity']
        return values
    if address is not None and action == 'clear':
        raise HTTPException(422,detail={'code':'credential_action_required'})
    if action == 'clear':
        if not existing:
            raise HTTPException(422,detail={'code':'email_recipient_required'})
        values.update(email_recipient_ciphertext=None,destination_identity=None)
    else:
        if not secrets.available:
            raise HTTPException(503,detail={'code':'notification_secret_key_unavailable'})
        try:
            address = mailbox((address or '').strip())
        except ValueError:
            raise HTTPException(422,detail={'code':'smtp_invalid_recipient'}) from None
        values.update(email_recipient_ciphertext=secrets.encrypt(address),
            destination_identity=secrets.identity('email:'+address))
    if supplied.get('recover_failed') and (not existing or
            values['destination_identity'] != existing['destination_identity']):
        raise HTTPException(422,detail={'code':'recovery_requires_same_destination'})
    return values


def credential_values(request,supplied,existing=None):
    secrets = request.app.state.notification_secrets
    values = {key:supplied[key] for key in ('name','enabled') if key in supplied}
    if 'name' in values:
        values['name'] = values['name'].strip()
        if not values['name'] or any(ord(char) < 32 or ord(char) == 127 for char in values['name']):
            raise HTTPException(422,detail={'code':'invalid_channel_name'})
    kind = existing['type'] if existing else supplied.get('type','telegram')
    if not existing:
        values['type'] = kind
    if kind == 'email':
        return email_values(request,supplied,existing,values)
    if kind != 'telegram':
        return endpoint_values(request,supplied,existing,values,kind)
    if any(supplied.get(field) is not None for field in ('endpoint','auth_token','auth_username','auth_password','recipient')) or supplied.get('auth_type','none') != 'none':
        raise HTTPException(422,detail={'code':'unsupported_channel_fields'})
    if (supplied.get('endpoint_action','keep' if existing else 'replace') != ('keep' if existing else 'replace') or
            supplied.get('recipient_action','keep' if existing else 'replace') != ('keep' if existing else 'replace') or
            supplied.get('auth_action','keep') != 'keep' or supplied.get('ntfy_priority',3) != 3):
        raise HTTPException(422,detail={'code':'unsupported_channel_fields'})
    plaintext = {}
    changed = False
    for prefix,field,column in [('token','bot_token','token_ciphertext'),('chat','chat_id','chat_ciphertext')]:
        action = supplied.get(prefix+'_action','keep' if existing else 'replace')
        if action == 'keep':
            if supplied.get(field) is not None:
                raise HTTPException(422,detail={'code':'credential_action_required'})
            continue
        changed = True
        if not secrets.available:
            raise HTTPException(503,detail={'code':'notification_secret_key_unavailable'})
        if action == 'clear':
            if supplied.get(field) is not None:
                raise HTTPException(422,detail={'code':'credential_action_required'})
            values[column] = None
            plaintext[field] = None
        else:
            value = (supplied.get(field) or '').strip()
            pattern = r'[0-9]+:[A-Za-z0-9_-]{20,}' if field=='bot_token' else r'(?:-?[0-9]+|@[A-Za-z][A-Za-z0-9_]{4,})'
            if not re.fullmatch(pattern,value):
                raise HTTPException(422,detail={'code':'invalid_telegram_credentials'})
            plaintext[field] = value
            values[column] = secrets.encrypt(value)
    if changed:
        try:
            for field,column in [('bot_token','token_ciphertext'),('chat_id','chat_ciphertext')]:
                if field not in plaintext:
                    plaintext[field] = secrets.decrypt(existing[column]) if existing and existing[column] else None
        except SecretUnavailable as exc:
            raise HTTPException(503,detail={'code':str(exc)})
        token,chat = plaintext['bot_token'],plaintext['chat_id']
        values['destination_identity'] = secrets.identity(token.split(':',1)[0]+':'+chat) if token and chat else None
        if supplied.get('recover_failed') and (not existing or values['destination_identity']!=existing['destination_identity']):
            raise HTTPException(422,detail={'code':'recovery_requires_same_destination'})
    return values


def job_public(request,job):
    for channel in job.get('notification_channels',[]):
        raw = request.app.state.repository.get_channel(channel['id'],private=True)
        try:
            secrets = request.app.state.notification_secrets
            for column in channel_secret_columns(raw):
                secrets.decrypt(raw[column])
            if raw['type'] in ('ntfy','webhook'):
                validate_endpoint(secrets.decrypt(raw['endpoint_ciphertext']),raw['type'],request.app.state.settings.webhook_allowlist)
            elif raw['type'] == 'email':
                mailbox(secrets.decrypt(raw['email_recipient_ciphertext']))
                channel['usable'] = channel['usable'] and smtp_public(request)['usable']
        except (SecretUnavailable, ValueError):
            channel['usable'] = False
    return job


def endpoint_values(request,supplied,existing,values,kind):
    secrets = request.app.state.notification_secrets
    if any(supplied.get(field) is not None for field in ('bot_token','chat_id','recipient')):
        raise HTTPException(422,detail={'code':'unsupported_channel_fields'})
    if any(supplied.get(field,'keep' if existing else 'replace') != ('keep' if existing else 'replace') for field in ('token_action','chat_action')):
        raise HTTPException(422,detail={'code':'unsupported_channel_fields'})
    if supplied.get('recipient_action','keep' if existing else 'replace') != ('keep' if existing else 'replace'):
        raise HTTPException(422,detail={'code':'unsupported_channel_fields'})
    if kind == 'ntfy':
        if 'ntfy_priority' in supplied:
            values['ntfy_priority'] = supplied['ntfy_priority']
    elif supplied.get('ntfy_priority',3) != 3:
        raise HTTPException(422,detail={'code':'unsupported_channel_fields'})
    action = supplied.get('endpoint_action','keep' if existing else 'replace')
    endpoint = supplied.get('endpoint')
    if action == 'keep':
        if endpoint is not None:
            raise HTTPException(422,detail={'code':'credential_action_required'})
    else:
        if not secrets.available:
            raise HTTPException(503,detail={'code':'notification_secret_key_unavailable'})
        if action == 'clear':
            if endpoint is not None:
                raise HTTPException(422,detail={'code':'credential_action_required'})
            values.update(endpoint_ciphertext=None,destination_identity=None)
        else:
            try:
                endpoint,_ = validate_endpoint(endpoint or '',kind,request.app.state.settings.webhook_allowlist)
            except ValueError:
                raise HTTPException(422,detail={'code':'invalid_notification_endpoint'})
            values.update(endpoint_ciphertext=secrets.encrypt(endpoint),destination_identity=secrets.identity(kind+':'+endpoint))
        if supplied.get('recover_failed') and (not existing or values['destination_identity'] != existing['destination_identity']):
            raise HTTPException(422,detail={'code':'recovery_requires_same_destination'})
    auth_type = supplied.get('auth_type',existing['auth_type'] if existing else 'none')
    auth_action = supplied.get('auth_action','keep')
    auth_fields = ('auth_token','auth_username','auth_password')
    if auth_action == 'keep':
        if any(supplied.get(field) is not None for field in auth_fields) or (existing and auth_type != existing['auth_type']) or (not existing and auth_type != 'none'):
            raise HTTPException(422,detail={'code':'credential_action_required'})
    else:
        if not secrets.available:
            raise HTTPException(503,detail={'code':'notification_secret_key_unavailable'})
        required = {'none':(), 'bearer':('auth_token',),'basic':('auth_username','auth_password')}[auth_type]
        if auth_action == 'clear' and auth_type != 'none':
            raise HTTPException(422,detail={'code':'credential_action_required'})
        if any(supplied.get(field) is not None for field in set(auth_fields)-set(required)):
            raise HTTPException(422,detail={'code':'unsupported_channel_fields'})
        for field in auth_fields:
            value = supplied.get(field)
            if field in required and (not value or any(ord(c)<32 or ord(c)==127 for c in value) or (field=='auth_username' and ':' in value)):
                raise HTTPException(422,detail={'code':'invalid_notification_auth'})
            if field == 'auth_token' and field in required and not re.fullmatch(r'[A-Za-z0-9._~+/-]+=*',value):
                raise HTTPException(422,detail={'code':'invalid_notification_auth'})
            values[field+'_ciphertext'] = secrets.encrypt(value) if field in required else None
        values['auth_type'] = auth_type
    return values
