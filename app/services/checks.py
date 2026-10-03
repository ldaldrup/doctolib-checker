"""Run one job and persist its per-target results and alert outcomes."""

import logging
from copy import deepcopy

from app.models import BookingMeta
from app.notifications import send_telegram_alert
from app.storage.repositories import LeaseLostError


class _RunStopped(Exception):
    """A pause or deletion stops remaining requests without recording an error."""


def _safe_error(exc):
    name = type(exc).__name__
    code = "doctolib_request_error" if name.startswith("HTTP") or name.startswith("Connection") else "check_failed"
    return code, "The availability check failed. Review the target URL or retry later."


class CheckService:
    def __init__(self, repository, doctolib, settings, notifier=send_telegram_alert):
        self.repository = repository
        self.doctolib = doctolib
        self.settings = settings
        self.notifier = notifier

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
                    result_id = self.repository.insert_result(
                        run_id, job, target, result, owner_token=owner_token
                    )
                    if result.status == "error":
                        failed += 1
                        last_error = result.error_code or "doctolib_incomplete_result"
                    else:
                        successful += 1
                except (_RunStopped, LeaseLostError):
                    break
                except Exception as exc:
                    failed += 1
                    code, message = _safe_error(exc)
                    last_error = code
                    try:
                        self.repository.insert_error_result(
                            run_id, job, target, code, message, owner_token=owner_token
                        )
                    except LeaseLostError:
                        break
                    logging.warning("Availability check failed for target %s (%s)", target["id"], code)
        except (_RunStopped, LeaseLostError):
            pass
        except Exception as exc:
            failed += 1
            last_error, _message = _safe_error(exc)
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
            except Exception:
                logging.exception("Worker failed while processing job %s", job["id"])
                outcomes.append({"successful_targets": 0, "failed_targets": 1})
        return outcomes
