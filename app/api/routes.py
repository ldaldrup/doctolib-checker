"""Versioned job, result, and settings API."""

from datetime import datetime, timedelta
import hashlib
import json
import re

from fastapi import APIRouter, HTTPException, Query, Request, Response, Header, Body, status
from requests import RequestException

from app.api.schemas import JobCreateRequest, JobUpdateRequest, SettingsUpdateRequest, TargetValidationRequest, VersionedRequest, ChannelCreateRequest, ChannelUpdateRequest, ChannelDeleteRequest, StrictRequest
from app.notification_secrets import SecretUnavailable
from app.notifications import format_slot_alert
from app.services.channel_tests import synthetic_alert
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
        result["telegram_configured"] = any(channel_public(request, item)["usable"] for item in request.app.state.repository.list_channels()["items"])
        return result

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
                "telegram_configured": any(channel_public(request,item)["usable"] for item in request.app.state.repository.list_channels()["items"]),
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
        return {"edit_version": saved["edit_version"], "default_interval_seconds": saved["default_interval_seconds"],
                "request_spacing_seconds": saved["request_spacing_seconds"],
                "minimum_poll_interval_seconds": settings.minimum_poll_interval_seconds,
                "telegram_configured": any(channel_public(request,item)["usable"] for item in request.app.state.repository.list_channels()["items"]),
                **notification_boundary(request),
                "time_zone": settings.default_timezone}

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

    @router.get('/api/v1/channels/{channel_id}/preview')
    def preview_channel(channel_id: str,request: Request):
        if request.app.state.repository.get_channel(channel_id) is None:
            raise HTTPException(404,detail='channel_not_found')
        return {'html':format_slot_alert(synthetic_alert())}

    @router.post('/api/v1/channels/{channel_id}/tests',status_code=202)
    def test_channel(channel_id: str,body: VersionedRequest,request: Request,
                     idempotency_key: str = Header(alias='Idempotency-Key')):
        key_check(idempotency_key)
        repository = request.app.state.repository
        def validate_usable(channel):
            try:
                secrets = request.app.state.notification_secrets
                secrets.decrypt(channel['token_ciphertext'])
                secrets.decrypt(channel['chat_ciphertext'])
            except SecretUnavailable:
                raise HTTPException(409,detail={'code':'channel_unusable'})
        try:
            return repository.reserve_channel_test(channel_id,body.expected_version,idempotency_key,
                fingerprint(['test',channel_id,body.expected_version]),validate_usable=validate_usable)
        except VersionConflictError as exc:
            raise version_conflict(exc)
        except NotFoundError:
            raise HTTPException(404,detail='channel_not_found')
        except ConflictError as exc:
            raise HTTPException(409,detail={'code':str(exc)})

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
        secrets.decrypt(raw['token_ciphertext'])
        secrets.decrypt(raw['chat_ciphertext'])
    except SecretUnavailable:
        public['usable'] = False
    return public


def credential_values(request,supplied,existing=None):
    secrets = request.app.state.notification_secrets
    values = {key:supplied[key] for key in ('name','enabled') if key in supplied}
    if 'name' in values:
        values['name'] = values['name'].strip()
        if not values['name']:
            raise HTTPException(422,detail={'code':'invalid_channel_name'})
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
            secrets.decrypt(raw['token_ciphertext'])
            secrets.decrypt(raw['chat_ciphertext'])
        except SecretUnavailable:
            channel['usable'] = False
    return job
