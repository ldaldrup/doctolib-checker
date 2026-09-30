"""Run one job and persist its per-target results and alert outcomes."""

import logging

from app.models import BookingMeta
from app.notifications import send_telegram_alert
from app.storage.repositories import iso


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

    def dispatch_pending(self):
        for alert in self.repository.get_pending_alerts():
            # A job or target may have changed after the initial batch query.
            current = self.repository.get_pending_alerts(limit=1, alert_id=alert["id"])
            if not current:
                continue
            try:
                sent, error = self.notifier(self.settings, current[0])
            except Exception:
                # Do not persist exception text; request URLs may contain
                # credentials supplied by an upstream notification service.
                sent, error = False, "telegram_delivery_error"
            self.repository.finish_alert(alert["id"], sent, error)

    def run_claim(self, run_id, job):
        successful = 0
        failed = 0
        last_error = None
        try:
            targets = self.repository.get_targets(job["id"])
            for target_ref in targets:
                current_job = self.repository.get_job(job["id"])
                if current_job is None or current_job["status"] != "active":
                    break
                if not self.repository.renew_job_lock(job["id"], run_id):
                    break
                target = self.repository.get_target(job["id"], target_ref["id"])
                if target is None:
                    continue
                # Edits made while a previous request was in flight apply to
                # this target request. Never change options during one request.
                search = {
                    "date_mode": current_job["date_mode"],
                    "horizon_days": current_job["horizon_days"],
                    "earliest_date": current_job["earliest_date"],
                    "latest_date": current_job["latest_date"],
                    "time_zone": current_job["time_zone"],
                    "insurance_sector": current_job["insurance_sector"],
                    "telehealth": bool(current_job["telehealth"]),
                }
                try:
                    result = self.doctolib.check(target["booking_url"], search, meta=self._meta(target))
                    result_id = self.repository.insert_result(run_id, current_job, target, result)
                    if result.status == "error":
                        failed += 1
                        last_error = result.error_code or "doctolib_incomplete_result"
                    else:
                        successful += 1
                    if result.status == "available" and result.earliest_slot:
                        # Re-read state after the HTTP request so a pause or
                        # notification edit suppresses delivery for this result.
                        current_job = self.repository.get_job(job["id"])
                        if (current_job and current_job["status"] == "active"
                                and current_job["telegram_enabled"]):
                            self.repository.create_alert(current_job, target, result_id, result.earliest_slot)
                            self.dispatch_pending()
                except Exception as exc:
                    failed += 1
                    code, message = _safe_error(exc)
                    last_error = code
                    self.repository.insert_error_result(run_id, current_job, target, code, message)
                    logging.warning("Availability check failed for target %s (%s)", target["id"], code)
        except Exception as exc:
            failed += 1
            last_error, _message = _safe_error(exc)
            logging.error("Unable to load or process targets for job %s (%s)", job["id"], last_error)
        finally:
            self.repository.finish_run(
                run_id, job["id"], successful, failed, int(job["interval_seconds"]), last_error
            )
        return {"successful_targets": successful, "failed_targets": failed}

    def run_due(self, limit=10):
        self.dispatch_pending()
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
