"""Run one job and persist its per-target results and alert outcomes."""

import logging
from copy import deepcopy

from app.models import BookingMeta
from app.notifications import send_telegram_alert
from app.doctolib import BookingUrlError, DoctolibHTTPError, MetadataResolutionError
from app.storage.repositories import LeaseLostError
from curl_cffi import requests as curl_requests
import requests


class _RunStopped(Exception):
    """A pause or deletion stops remaining requests without recording an error."""


def _safe_error(exc):
    if isinstance(exc, DoctolibHTTPError) or isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
        status = status if isinstance(status, int) and 100 <= status <= 599 else None
        if status is None:
            return "check_failed", "The availability check failed.", "unknown", None, None
        category = "throttled" if status == 429 else "upstream_rejected" if status and 400 <= status < 500 else "upstream_error"
        retry_at = getattr(exc, "retry_at", None)
        return "doctolib_http_error", "Doctolib rejected the availability request.", category, status, retry_at
    if isinstance(exc, (requests.Timeout, curl_requests.exceptions.Timeout)):
        return "availability_timeout", "The availability request timed out.", "timeout", None, None
    if isinstance(exc, (requests.ConnectionError, curl_requests.exceptions.ConnectionError)):
        return "availability_connection_error", "Doctolib could not be reached.", "connectivity", None, None
    if isinstance(exc, (BookingUrlError, MetadataResolutionError)):
        return "invalid_booking_metadata", "The booking target could not be resolved.", "invalid_metadata", None, None
    return "check_failed", "The availability check failed.", "unknown", None, None


def _result_error(result):
    categories = {"upstream_rejected", "throttled", "upstream_error", "timeout", "connectivity",
                  "invalid_metadata", "malformed_response", "incomplete_response"}
    category = result.error_category
    if category not in categories:
        category = {"invalid_availability_response": "malformed_response",
                    "incomplete_availability_response": "incomplete_response"}.get(result.error_code, "unknown")
    messages = {"upstream_rejected": "Doctolib rejected the availability request.",
                "throttled": "Doctolib is rate-limiting the availability request.",
                "upstream_error": "Doctolib returned a temporary error.",
                "timeout": "The availability request timed out.",
                "connectivity": "Doctolib could not be reached.",
                "invalid_metadata": "The booking target could not be resolved.",
                "malformed_response": "Doctolib returned invalid availability data.",
                "incomplete_response": "Doctolib returned only part of the availability list.",
                "unknown": "The availability check failed."}
    code = result.error_code
    if (not isinstance(code, str) or not code or len(code) > 80 or not code.isascii()
            or not all(char.isalnum() or char == "_" for char in code)):
        code = "check_failed"
    status = result.upstream_status
    if not isinstance(status, int) or not 100 <= status <= 599:
        status = None
    retry_at = result.retry_at if hasattr(result.retry_at, "tzinfo") else None
    return code, messages[category], category, status, retry_at


class CheckService:
    def __init__(self, repository, doctolib, settings, notifier=send_telegram_alert):
        self.repository = repository
        self.doctolib = doctolib
        self.settings = settings
        self.notifier = notifier
        self.repository.configure_notification_routing(settings)

    def _meta(self, target):
        return BookingMeta(
            state_key=target["profile_slug"] + "_" + str(target.get("practitioner_id") or "any"),
            practice_name=target["practice_name"],
            practitioner_name=target["practitioner_name"],
            motive_id=target["motive_id"],
            agenda_ids_str=target["agenda_ids"],
            practice_id=target["practice_id"],
            display_name=target["practitioner_name"] + " @ " + target["practice_name"],
            country=target["country"],
            profile_slug=target["profile_slug"],
            practitioner_id=target.get("practitioner_id"),
            motive_name=target.get("motive_name"),
        )

    def run_claim(self, run_id, job):
        successful = 0
        failed = 0
        last_error = None
        owner_token = job["owner_token"]
        snapshot = job["search_snapshot"]
        had_hook = hasattr(self.doctolib, "before_request")
        original_hook = getattr(self.doctolib, "before_request", None)

        def guard_request():
            # Only a manual intent created while paused grants one-run access;
            # status and revision changes revoke that capability in storage.
            if not self.repository.run_can_check(job["id"], run_id, owner_token):
                raise _RunStopped()
            if not self.repository.renew_job_lock(job["id"], run_id, owner_token=owner_token):
                raise LeaseLostError("The check claim is no longer owned")

        def before_request():
            guard_request()
            if original_hook is not None:
                original_hook()
            # A reserved request turn may wait beyond the lease or a pause.
            guard_request()

        try:
            self.doctolib.before_request = before_request
            for saved_target in snapshot["targets"]:
                guard_request()
                target = deepcopy(saved_target)
                search = deepcopy(snapshot["search"])
                try:
                    result = self.doctolib.check(target["booking_url"], search, meta=self._meta(target))
                    if result.status == "error":
                        code, safe_message, category, status, retry_at = _result_error(result)
                        result.error_code, result.error_message = code, safe_message
                        result.error_category, result.upstream_status, result.retry_at = category, status, retry_at
                    result_id = self.repository.insert_result(
                        run_id, job, target, result, owner_token=owner_token
                    )
                    if result.status == "error":
                        failed += 1
                        last_error = result.error_code
                    else:
                        successful += 1
                except (_RunStopped, LeaseLostError):
                    break
                except Exception as exc:
                    failed += 1
                    code, message, category, status, retry_at = _safe_error(exc)
                    last_error = code
                    try:
                        self.repository.insert_error_result(
                            run_id, job, target, code, message, error_category=category,
                            upstream_status=status, retry_at=retry_at, owner_token=owner_token
                        )
                    except LeaseLostError:
                        break
                    logging.warning("Availability check failed for target %s (%s)", target["id"], code)
        except (_RunStopped, LeaseLostError):
            pass
        except Exception as exc:
            failed += 1
            last_error = _safe_error(exc)[0]
            logging.error("Unable to load or process targets for job %s (%s)", job["id"], last_error)
        finally:
            if had_hook:
                self.doctolib.before_request = original_hook
            else:
                del self.doctolib.before_request
            self.repository.finish_run(
                run_id, job["id"], successful, failed, int(job["interval_seconds"]), last_error,
                owner_token=owner_token,
            )
        return {"successful_targets": successful, "failed_targets": failed}

    def run_due(self, limit=10):
        self.repository.interrupt_stale_runs()
        outcomes = []
        for _ in range(limit):
            claimed = self.repository.claim_due_jobs(limit=1)
            if not claimed:
                break
            run_id, job = claimed[0]
            try:
                outcomes.append(self.run_claim(run_id, job))
            except Exception as exc:
                code = _safe_error(exc)[0]
                logging.error("Worker failed while processing job %s (%s)", job["id"], code)
                outcomes.append({"successful_targets": 0, "failed_targets": 1})
        return outcomes
