import json
import socket
import ssl
from types import SimpleNamespace

import pytest

from app.webhooks import (PinnedHTTPSConnection, approved_addresses, send_webhook_alert,
                         validate_endpoint, webhook_payload, MAX_RESPONSE_BYTES)


def answers(*ips):
    return lambda *args, **kwargs: [(socket.AF_INET6 if ':' in ip else socket.AF_INET,
        socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in ips]


@pytest.mark.parametrize('ip', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '192.168.1.1',
    '::1', 'fe80::1', 'fc00::1', 'fd00:ec2::254', '4000::1', '::ffff:127.0.0.1', '224.0.0.1', '::'])
def test_addresses_blocked_even_with_public_answer(ip):
    with pytest.raises(ValueError, match='webhook_address_blocked'):
        approved_addresses('receiver.example', 443, resolver=answers('1.1.1.1', ip))


def test_exact_private_allowlist_and_nonstandard_ports():
    allowed = (('receiver.example', 8443, '10.0.0.8'),)
    assert validate_endpoint('https://receiver.example:8443/path', allowlist=allowed)[1] == 'receiver.example'
    assert approved_addresses('receiver.example', 8443, allowed, answers('10.0.0.8')) == ['10.0.0.8']
    for host, port, ip in [('other.example', 8443, '10.0.0.8'),
                         ('receiver.example', 443, '10.0.0.8'), ('receiver.example', 8443, '10.0.0.9')]:
        with pytest.raises(ValueError):
            approved_addresses(host, port, allowed, answers(ip))


@pytest.mark.parametrize('ip', ['127.0.0.1', '::1', '169.254.169.254', 'fe80::1',
    '100.100.100.200', '168.63.129.16', 'fd00:ec2::254', '4000::1', '192.0.2.1', '198.18.0.1',
    '::ffff:127.0.0.1', '::ffff:168.63.129.16'])
def test_operator_allowlist_never_overrides_special_addresses(ip):
    import ipaddress
    allowed = (('receiver.example', 443, str(ipaddress.ip_address(ip))),)
    with pytest.raises(ValueError, match='webhook_address_blocked'):
        approved_addresses('receiver.example', 443, allowed, answers(ip))


@pytest.mark.parametrize('url', ['http://receiver.example/a', 'https://u:p@receiver.example/a',
    'https://receiver.example:8443/a', 'https://receiver.example/a#fragment',
    'https://receiver.example/a\nX: v', 'https://receiver.example/a%zz',
    'https://receiver.example\\evil/a', 'https://[fe80::1%25eth0]/a',
    'https://receiver.example:0/a', 'https://receiver.example/ü'])
def test_invalid_endpoints_have_safe_errors(url):
    with pytest.raises(ValueError, match='^webhook_invalid_endpoint$'):
        validate_endpoint(url)


class FakeConnection:
    status = 204
    content = b''
    headers = {}
    sent = []
    def __init__(self, host, port, address):
        self.destination = (host, port, address)
    def connect(self):
        pass
    def request(self, method, path, body, headers):
        self.sent.append((self.destination, method, path, json.loads(body), headers))
    def getresponse(self):
        return self
    def read(self, size):
        return self.content[:size]
    def getheader(self, name):
        return self.headers.get(name)
    def close(self):
        pass


def send(channel=None, alert=None, permission=None, connection=FakeConnection, resolver=None):
    return send_webhook_alert(SimpleNamespace(webhook_allowlist=()),
        channel or {'type': 'webhook', 'endpoint': 'https://receiver.example/secret?key=hidden'},
        alert or {'event_id': 'episode-1', 'triggered_by': 'manual', 'search_revision': 4},
        before_send=permission, resolver=resolver or answers('1.1.1.1'), connection_factory=connection)


def test_fixed_payload_auth_and_guard_no_rebinding(monkeypatch):
    FakeConnection.sent.clear()
    # A transport that re-resolves after the policy check would hit loopback.
    monkeypatch.setattr(socket, 'getaddrinfo', answers('127.0.0.1'))
    channel = {'type': 'webhook', 'endpoint': 'https://receiver.example/path?key=hidden',
               'auth_type': 'basic', 'auth_username': 'user', 'auth_password': 'secret'}
    assert send(channel, permission=lambda **kwargs: False).attempted is False
    assert not FakeConnection.sent
    assert send(channel, permission=lambda **kwargs: True).category == 'sent'
    destination, method, path, payload, headers = FakeConnection.sent[-1]
    assert destination == ('receiver.example', 443, '1.1.1.1')
    assert (method, path) == ('POST', '/path?key=hidden')
    assert payload['schema_version'] == 1 and payload['event_id'] == 'episode-1'
    assert payload['trigger'] == 'manual' and payload['search_revision'] == 4
    assert headers['Authorization'] == 'Basic dXNlcjpzZWNyZXQ='


@pytest.mark.parametrize('status,category', [(202, 'sent'), (302, 'action_required'),
    (401, 'action_required'), (403, 'action_required'), (408, 'retry'), (429, 'retry'), (503, 'retry')])
def test_response_classification_without_raw_body(status, category):
    class Response(FakeConnection):
        content = b'secret endpoint token provider body'
        headers = {'Retry-After': '999999'}
    Response.status = status
    outcome = send(connection=Response)
    assert outcome.category == category
    assert 'secret' not in repr(outcome)
    if category == 'retry':
        assert outcome.retry_after == 900


def test_ntfy_json_root_topic_confirmation_and_payload_bound():
    channel = {'type': 'ntfy', 'endpoint': 'https://receiver.example/ntfy/private_topic', 'ntfy_priority': 4}
    class Response(FakeConnection):
        status = 200
        content = b'{"id":"remote-id","event":"message","topic":"private_topic"}'
    assert send(channel, connection=Response).category == 'sent'
    _, _, path, payload, _ = Response.sent[-1]
    assert path == '/ntfy/' and payload['topic'] == 'private_topic' and payload['priority'] == 4
    assert 'Event: episode-1' in payload['message']
    Response.content = b'{"event":"message","topic":"wrong","id":"remote-id"}'
    assert send(channel, connection=Response).category == 'uncertain'
    Response.content = b'x' * (MAX_RESPONSE_BYTES + 1)
    assert send(channel, connection=Response).error_code == 'webhook_response_too_large'
    assert send(alert={'practitioner_name': 'x' * 20000}).attempted is False


def test_lost_acknowledgement_is_uncertain_but_connect_failure_retries():
    class Lost(FakeConnection):
        def getresponse(self):
            raise socket.timeout('private response')
    class Failed(FakeConnection):
        def connect(self):
            raise ConnectionRefusedError('private endpoint')
    assert send(connection=Lost).category == 'uncertain'
    outcome = send(connection=Failed)
    assert outcome.category == 'retry' and outcome.attempted is False


def test_real_tls_verifies_original_hostname_and_sni_without_resolving_again(tmp_path, monkeypatch):
    import datetime
    import threading
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'receiver.example')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('receiver.example')]), critical=False)
        .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_file, key_file)
    seen = []
    server_context.set_servername_callback(lambda sock, hostname, context: seen.append(hostname))
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(2)
    listener.settimeout(3)
    def serve():
        for _ in range(2):
            raw, _ = listener.accept()
            try:
                with server_context.wrap_socket(raw, server_side=True):
                    pass
            except ssl.SSLError:
                raw.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    context = ssl.create_default_context(cafile=str(cert_file))
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    def rebinding(*args, **kwargs):
        pytest.fail('Pinned numeric connect must not resolve DNS again')
    monkeypatch.setattr(socket, 'getaddrinfo', rebinding)
    correct = PinnedHTTPSConnection('receiver.example', listener.getsockname()[1], '127.0.0.1', context)
    try:
        correct.connect()
        correct.close()
        wrong = PinnedHTTPSConnection('wrong.example', listener.getsockname()[1], '127.0.0.1', context)
        with pytest.raises(ssl.SSLCertVerificationError):
            wrong.connect()
    finally:
        listener.close()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert seen == ['receiver.example', 'wrong.example']


def _hanging_child(pipe, attempt, args, prefix):
    import time
    pipe.send('ready')
    pipe.recv()
    time.sleep(10)


def test_production_parent_bounds_lost_acknowledgement_and_denied_permission(monkeypatch):
    import multiprocessing
    import time
    import app.notifications as notifications
    context = multiprocessing.get_context('fork')
    monkeypatch.setattr(multiprocessing, 'get_context', lambda name: context)
    monkeypatch.setattr(notifications, '_bounded_attempt_child', _hanging_child)
    monkeypatch.setattr(notifications, 'SEND_BUDGET_SECONDS', 0.1)
    start = time.monotonic()
    channel = {'type': 'webhook', 'endpoint': 'https://receiver.example/'}
    result = send_webhook_alert(SimpleNamespace(), channel, {}, before_send=lambda **kw: True)
    assert result.category == 'uncertain' and result.attempted
    assert time.monotonic() - start < 2
    result = send_webhook_alert(SimpleNamespace(), channel, {}, before_send=lambda **kw: False)
    assert result.attempted is False and result.category == 'retry'


def test_spawn_child_rejects_invalid_endpoint_before_permission():
    called = []
    result = send_webhook_alert(SimpleNamespace(), {'type': 'webhook', 'endpoint': 'http://private/'}, {},
        before_send=lambda **kw: called.append(True))
    assert result.error_code == 'webhook_invalid_endpoint' and result.attempted is False
    assert not called


def test_child_watchdog_bounds_transport_without_parent(monkeypatch):
    import multiprocessing
    import time
    import app.notifications as notifications
    monkeypatch.setattr(notifications, 'SEND_BUDGET_SECONDS', 0.1)
    context = multiprocessing.get_context('fork')
    parent, child = context.Pipe()
    process = context.Process(target=notifications._bounded_attempt_child,
                              args=(child, lambda *args, **kwargs: time.sleep(10), (), 'webhook'))
    try:
        process.start()
        child.close()
        process.join(timeout=2)
        assert not process.is_alive() and process.exitcode == 70
    finally:
        parent.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
        process.close()
