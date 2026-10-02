"""Bounded metadata ownership while a create waits on shared request spacing."""
from threading import Event, Thread
from time import monotonic

from app.storage.repositories import CreateReservationLostError


CREATE_HEARTBEAT_SECONDS = 30
CREATE_TARGET_DEADLINE_SECONDS = 300


class MetadataLease:
    """Keep a live reservation, never resurrect an expired or replaced owner.

    Renewal covers long shared-gate waits, but stops after five minutes per
    target. A stuck resolver may still return later; its final write is fenced.
    """
    def __init__(self, repository, operation):
        self.repository = repository
        self.operation = operation
        self.deadline = monotonic() + CREATE_TARGET_DEADLINE_SECONDS
        self.stopped = Event()
        self.error = None
        self.thread = Thread(target=self._heartbeat, name='create-metadata-lease', daemon=True)

    def guard(self):
        if self.error is not None:
            raise self.error
        if monotonic() >= self.deadline:
            try:
                outcome = self.repository.fail_create(self.operation, 'metadata_unavailable', retryable=True)
            except CreateReservationLostError as exc:
                self.error = exc
            else:
                self.error = CreateReservationLostError(outcome)
            raise self.error
        self.repository.renew_create(self.operation)

    def _heartbeat(self):
        while not self.stopped.wait(CREATE_HEARTBEAT_SECONDS):
            try:
                self.guard()
            except CreateReservationLostError as exc:
                self.error = exc
                return
            except Exception:
                # A failed renewal cannot authorize a write. Stop maintaining
                # the lease; foreground guards/final transaction recheck it.
                return

    def __enter__(self):
        self.guard()
        self.thread.start()
        return self

    def __exit__(self, _kind, _value, _traceback):
        self.stopped.set()
        # A connection waiting on SQLite's bounded lock timeout must not hold
        # the HTTP response open. It cannot renew a completed/replaced owner.
        self.thread.join(timeout=1)
