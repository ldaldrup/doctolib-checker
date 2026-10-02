"""Long gate waits retain ownership; stuck metadata has a finite lease budget."""
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app.services import create_lease
from app.storage.repositories import CreateReservationLostError
from test_backend_journey import setup_backend, URL


def test_create_lease_renews_during_request_gate_wait(tmp_path, monkeypatch):
    now=[datetime(2026,10,2,12,tzinfo=timezone.utc)]
    monkeypatch.setattr('app.storage.repositories.utc_now',lambda:now[0])
    monkeypatch.setattr(create_lease,'CREATE_HEARTBEAT_SECONDS',0.005)
    helper,repository,_,doctolib=setup_backend(tmp_path)
    client=TestClient(helper.app)
    repository.update_settings({'request_spacing_seconds':120},300)
    renewed=Event()
    original_renew=repository.renew_create
    renewals=[]
    def renew(operation):
        result=original_renew(operation)
        renewals.append(now[0])
        renewed.set()
        return result
    repository.renew_create=renew
    def long_gate():
        # Four waiting slices exceed the original 120-second lease, while
        # real renewal turns retain the live owner throughout the wait.
        for _ in range(4):
            renewed.clear()
            now[0]+=timedelta(seconds=40)
            assert renewed.wait(1)
    doctolib.before_request=long_gate
    response=client.post('/api/v1/jobs',headers={'Idempotency-Key':'long-gate'},json={'name':'Long wait','target_urls':[URL]})
    assert response.status_code==201,response.text
    assert len(repository.list_jobs())==1 and len(renewals)>=4
    assert doctolib.before_request is long_gate


def test_target_deadline_releases_retryable_reservation_without_reviving_owner(tmp_path):
    _,repository,_,_=setup_backend(tmp_path)
    operation=repository.reserve_create('deadline','fingerprint',{'name':'Reserved'})
    with create_lease.MetadataLease(repository,operation) as lease:
        lease.deadline=create_lease.monotonic()-1
        with pytest.raises(CreateReservationLostError) as rejected:
            lease.guard()
        assert rejected.value.operation['state']=='failed'
        assert rejected.value.operation['retryable']
        assert rejected.value.operation['error_code']=='metadata_unavailable'
    assert not lease.thread.is_alive()
    replacement=repository.reserve_create('deadline','fingerprint',{'name':'Changed default'})
    assert replacement['canonical_values']==operation['canonical_values']
    assert replacement['generation']==operation['generation']+1
    with pytest.raises(CreateReservationLostError):
        repository.renew_create(operation)
