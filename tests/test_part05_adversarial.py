"""Boundary regressions for validation privacy and transactional save races."""
from threading import Event, Thread
from fastapi.testclient import TestClient

from test_backend_journey import setup_backend, URL


def test_stale_metadata_editor_cannot_mutate_targets_or_queue(tmp_path):
    helper, repository, _, doctolib = setup_backend(tmp_path)
    client = TestClient(helper.app)
    job = client.post('/api/v1/jobs', headers={'Idempotency-Key':'editor-race'}, json={'name':'Original', 'target_urls':[URL]}).json()
    entered, release = Event(), Event()
    original = doctolib.resolve
    def blocked(url):
        entered.set()
        assert release.wait(5)
        return original(url)
    doctolib.resolve = blocked
    replies = []
    thread = Thread(target=lambda: replies.append(client.patch('/api/v1/jobs/'+job['id'], json={'expected_version':job['edit_version'], 'target_urls':[URL,URL+'&source=extra']})))
    thread.start()
    try:
        assert entered.wait(5)
        winner = client.patch('/api/v1/jobs/'+job['id'], json={'expected_version':job['edit_version'], 'name':'Winner'})
        assert winner.status_code == 200
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert replies[0].status_code == 409
    current = repository.get_job(job['id'])
    assert current['name'] == 'Winner' and len(current['targets']) == 1
    assert current['search_revision'] == job['search_revision'] and current['check_intent'] is None


def test_validation_errors_do_not_echo_private_input_or_context(tmp_path):
    helper, *_ = setup_backend(tmp_path)
    client = TestClient(helper.app)
    secret = 'private-fixture-value-never-echo'
    replies = [
        client.put('/api/v1/settings',json={'expected_version':1,'unsupported':secret}),
        client.put('/api/v1/settings',json={'expected_version':secret}),
        client.post('/api/v1/jobs',headers={'Idempotency-Key':secret+' invalid'},json={'name':'Job','target_urls':[URL]}),
        client.post('/api/v1/jobs',headers={'Idempotency-Key':'extra-field'},json={'name':'Job','target_urls':[URL],'token':secret}),
    ]
    for reply in replies:
        assert reply.status_code == 422
        assert secret not in reply.text
        detail = reply.json()['detail']
        if isinstance(detail,list):
            assert all('input' not in e and 'ctx' not in e for e in detail)
