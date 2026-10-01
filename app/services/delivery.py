"""SQLite-owned, serial delivery turns independent of availability polling."""

from app.notifications import DeliveryOutcome, send_telegram_alert


class DeliveryService:
    def __init__(self, repository, settings, sender=send_telegram_alert):
        self.repository = repository
        self.settings = settings
        self.sender = sender

    def run_once(self):
        """Process at most one network attempt; retry scheduling lives in SQLite."""
        self.repository.reconcile_alert_claims()
        self.repository.touch_dispatcher()
        if (self.sender is send_telegram_alert and
                (not self.settings.telegram_enabled or not self.settings.telegram_bot_token
                 or not self.settings.telegram_chat_id)):
            # Configuration failure is not a network attempt. Leave work ready
            # and expose the operator action through the dispatcher heartbeat.
            self.repository.touch_dispatcher(last_error="telegram_not_configured")
            return False
        claimed = self.repository.claim_alert(lease_seconds=60)
        if claimed is None:
            return False
        token = claimed["owner_token"]
        started = False

        def before_send(remaining_seconds=None):
            nonlocal started
            started = self.repository.begin_alert_attempt(
                claimed["id"], token, wait_seconds=remaining_seconds) is not None
            return started

        try:
            if self.sender is send_telegram_alert:
                outcome = self.sender(self.settings, claimed, before_send=before_send)
            else:
                payload = self.repository.begin_alert_attempt(claimed["id"], token)
                if payload is None:
                    return False
                started = True
                outcome = self.sender(self.settings, payload)
            if not isinstance(outcome, DeliveryOutcome):
                outcome = DeliveryOutcome("uncertain" if started else "retry",
                                          "telegram_invalid_sender_outcome", attempted=started)
        except Exception:
            # Permission persisted before transport means provider acceptance is
            # possible. Preparation failures before permission are retryable.
            outcome = DeliveryOutcome("uncertain" if started else "retry",
                                      "telegram_delivery_error", attempted=started)
        finish = (self.repository.finish_delivery if outcome.attempted
                  else self.repository.finish_unstarted_delivery)
        finish(claimed["id"], token, outcome.category,
               error_code=outcome.error_code, retry_after=outcome.retry_after)
        self.repository.touch_dispatcher(last_error=outcome.error_code)
        return outcome.attempted
