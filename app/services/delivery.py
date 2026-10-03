"""SQLite-owned, serial delivery turns independent of availability polling."""

from app.notifications import DeliveryOutcome
from app.notification_secrets import SecretUnavailable
from app.services.channel_tests import channel_settings, run_test_once, send_configured


class DeliveryService:
    def __init__(self, repository, settings, sender=None):
        self.repository = repository
        self.settings = settings
        self.sender = sender

    def run_once(self):
        """Process at most one network attempt; retry scheduling lives in SQLite."""
        self.repository.reconcile_alert_claims()
        self.repository.touch_dispatcher()
        if run_test_once(self.repository, self.settings, self.sender):
            return True
        claimed = self.repository.claim_alert(lease_seconds=60)
        if claimed is None:
            return False
        token = claimed["owner_token"]
        started = False
        configured = None
        try:
            channel = self.repository.get_channel(claimed['channel_config_id'], private=True)
            if (channel is None or channel['destination_version'] != claimed['destination_version']
                    or channel['credential_version'] != claimed['credential_version']):
                raise SecretUnavailable('notification_channel_changed')
            smtp_transport = (self.repository.get_smtp_transport(private=True)
                              if channel['type'] == 'email' else None)
            configured = channel_settings(self.settings, channel, smtp_transport)
        except SecretUnavailable as exc:
            self.repository.finish_unstarted_delivery(claimed['id'], token,
                'retry' if str(exc)=='notification_channel_changed' else 'action_required', error_code=str(exc))
            self.repository.touch_dispatcher(last_error=str(exc))
            return False

        def before_send(remaining_seconds=None):
            nonlocal started
            started = self.repository.begin_alert_attempt(
                claimed["id"], token, wait_seconds=remaining_seconds) is not None
            return started

        try:
            if self.sender is None:
                outcome = send_configured(self.settings, configured, claimed, before_send)
            else:
                payload = self.repository.begin_alert_attempt(claimed["id"], token)
                if payload is None:
                    return False
                started = True
                outcome = self.sender(configured, payload)
            if not isinstance(outcome, DeliveryOutcome):
                outcome = DeliveryOutcome("uncertain" if started else "retry",
                                          "notification_invalid_sender_outcome", attempted=started)
        except Exception:
            # Permission persisted before transport means provider acceptance is
            # possible. Preparation failures before permission are retryable.
            outcome = DeliveryOutcome("uncertain" if started else "retry",
                                      "notification_delivery_error", attempted=started)
        finish = (self.repository.finish_delivery if outcome.attempted
                  else self.repository.finish_unstarted_delivery)
        finish(claimed["id"], token, outcome.category,
               error_code=outcome.error_code, retry_after=outcome.retry_after)
        self.repository.touch_dispatcher(last_error=outcome.error_code)
        return outcome.attempted
