"""SMTP acceptance boundaries, MIME safety and pinned verified TLS."""
import smtplib
import socket
import ssl
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import pytest

from app.services.email_delivery import (PinnedSMTP, ReplyLimitError, email_message,
    send_email_alert, validate_smtp_host)


def configured():
    return {'type':'email','id':'channel-one','recipient':'recipient@example.org',
        'transport':{'host':'smtp.example.org','port':587,'tls_mode':'starttls',
            'username':'user','password':'secret','sender_email':'sender@example.org','sender_name':'Checker'}}


ALERT = {'event_id':'episode-one','practitioner_name':'Doctor <script>','practice_name':'A & B',
    'earliest_slot':'2030-01-02T10:00:00+00:00','time_zone':'Europe/Berlin',
    'booking_url':'https://www.doctolib.de/?a=1&b=2','slot_count':2}


def public_dns(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 587))]


class FakeSMTP:
    events = []
    recipient_code = 250
    data_code = 250
    def __init__(self, host, port, address, mode):
        self.events.append(('connect',host,port,address,mode))
    def ehlo_or_helo_if_needed(self):
        self.events.append('greeting')
    def starttls(self, context):
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        self.events.append('tls')
    def ehlo(self):
        self.events.append('ehlo-after-tls')
        return 250,b'ok'
    def login(self, user, password):
        self.events.append('auth')
    def mail(self, sender):
        self.events.append(('mail',sender))
        return 250,b'ok'
    def rcpt(self, recipient):
        self.events.append(('rcpt',recipient))
        return self.recipient_code,b'private transcript'
    def data(self, payload):
        self.events.append(('data',payload))
        return self.data_code,b'private acceptance transcript'
    def close(self):
        self.events.append('closed')


def send(connection=FakeSMTP, config=None, permission=None, resolver=public_dns):
    return send_email_alert(SimpleNamespace(webhook_allowlist=()),config or configured(),ALERT,
        before_send=permission,resolver=resolver,connection_factory=connection)


def test_one_recipient_verified_starttls_guard_and_stable_message_identity():
    FakeSMTP.events.clear()
    def permission(**kwargs):
        FakeSMTP.events.append('permission')
        return True
    assert send(permission=permission).category == 'sent'
    assert FakeSMTP.events[0] == ('connect','smtp.example.org',587,'1.1.1.1','starttls')
    assert FakeSMTP.events.index('tls') < FakeSMTP.events.index('ehlo-after-tls') < FakeSMTP.events.index('auth')
    index = FakeSMTP.events.index('permission')
    assert FakeSMTP.events[index-1] == ('rcpt','recipient@example.org')
    assert FakeSMTP.events[index+1][0] == 'data'
    payload = FakeSMTP.events[index+1][1]
    parsed = BytesParser(policy=policy.default).parsebytes(payload)
    assert len(parsed['To'].addresses) == 1 and parsed.is_multipart()
    assert parsed.get_body(preferencelist=('plain',)).get_content().find('11:00 CET') >= 0
    html = parsed.get_body(preferencelist=('html',)).get_content()
    assert '&lt;script&gt;' in html and 'A &amp; B' in html
    again = BytesParser(policy=policy.default).parsebytes(email_message(configured()['transport'],configured(),ALERT))
    assert parsed['Message-ID'] == again['Message-ID']
    other = configured() | {'recipient':'other@example.org'}
    assert parsed['Message-ID'] != BytesParser(policy=policy.default).parsebytes(email_message(other['transport'],other,ALERT))['Message-ID']
    FakeSMTP.events.clear()
    assert send(permission=lambda **kwargs: False).attempted is False
    assert not any(isinstance(event,tuple) and event[0]=='data' for event in FakeSMTP.events)


@pytest.mark.parametrize('field,value', [('recipient','One <one@example.org>'),
    ('recipient','one@example.org,two@example.org'),('recipient','one@example.org\r\nBcc: hidden@example.org'),
    ('sender_email','sender@example.org\nInjected: value'),('sender_name','Name\r\nInjected: value')])
def test_address_and_header_injection_fails_before_network(field,value):
    config = configured()
    if field == 'recipient': config[field] = value
    else: config['transport'][field] = value
    FakeSMTP.events.clear()
    outcome = send(config=config)
    assert outcome.category == 'action_required' and not outcome.attempted and not FakeSMTP.events
    assert 'Injected' not in repr(outcome)


@pytest.mark.parametrize('code,category', [(450,'retry'),(550,'action_required')])
def test_recipient_rejection_does_not_start_data(code,category):
    class Reject(FakeSMTP): recipient_code = code
    Reject.events.clear()
    result = send(connection=Reject)
    assert result.category == category and not result.attempted
    assert not any(isinstance(event,tuple) and event[0]=='data' for event in Reject.events)


@pytest.mark.parametrize('code,category', [(451,'retry'),(554,'action_required'),(299,'sent')])
def test_known_final_data_response_classification(code,category):
    class Reply(FakeSMTP): data_code = code
    result = send(connection=Reply)
    assert result.category == category and result.attempted
    assert 'transcript' not in repr(result)


def test_tls_downgrade_auth_and_lost_data_acknowledgement():
    class NoTLS(FakeSMTP):
        def starttls(self,context): raise smtplib.SMTPNotSupportedError('private transcript')
    class BadTLS(FakeSMTP):
        def starttls(self,context): raise ssl.SSLCertVerificationError('private hostname')
    class BadAuth(FakeSMTP):
        def login(self,*args): raise smtplib.SMTPAuthenticationError(535,b'private password')
    class Lost(FakeSMTP):
        def data(self,payload): raise smtplib.SMTPServerDisconnected('private transcript')
    for connection in (NoTLS,BadTLS,BadAuth):
        result = send(connection=connection)
        assert result.category == 'action_required' and not result.attempted
    result = send(connection=Lost)
    assert result.category == 'uncertain' and result.attempted
    assert 'private' not in repr(result)
    class CloseError(FakeSMTP):
        def close(self): raise OSError('socket already closed')
    assert send(connection=CloseError).category == 'sent'


def test_smtp_uses_same_mixed_dns_policy_before_connect():
    def dns(*args,**kwargs):
        return public_dns() + [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',587))]
    FakeSMTP.events.clear()
    result = send(resolver=dns)
    assert result.error_code == 'smtp_address_blocked' and not FakeSMTP.events


def test_multiline_smtp_reply_is_bounded():
    import io
    connection = PinnedSMTP.__new__(PinnedSMTP)
    connection.file = io.BytesIO(b'250-' + b'x'*8000 + b'\r\n')
    with pytest.raises(smtplib.SMTPServerDisconnected): connection.getreply()
    connection.file = io.BytesIO((b'250-' + b'x'*8000 + b'\r\n')*9)
    with pytest.raises(ReplyLimitError): connection.getreply()


def test_plaintext_is_rejected_and_overlong_event_fields_are_bounded():
    config = configured(); config['transport']['tls_mode'] = 'plaintext'
    assert send(config=config).error_code == 'smtp_invalid_transport'
    outcome = send_email_alert(SimpleNamespace(),configured(),ALERT | {'practice_name':'x'*70000},
        resolver=public_dns,connection_factory=FakeSMTP)
    assert outcome.category == 'sent' and outcome.attempted
    assert len(email_message(configured()['transport'], configured(), ALERT | {'practice_name': 'x'*70000})) < 65536


def test_spawned_email_adapter_rejects_invalid_mailbox_before_permission():
    config = configured(); config['recipient'] = 'one@example.org,two@example.org'
    called = []
    result = send_email_alert(SimpleNamespace(),config,ALERT,
        before_send=lambda **kwargs:called.append(True))
    assert result.error_code == 'smtp_invalid_address' and not result.attempted and not called


def test_approved_port_modes_and_exact_nonstandard_exception():
    assert validate_smtp_host('SMTP.example.org',587,'starttls') == 'smtp.example.org'
    assert validate_smtp_host('smtp.example.org',465,'implicit_tls') == 'smtp.example.org'
    for port,mode in [(465,'starttls'),(587,'implicit_tls'),(25,'starttls')]:
        with pytest.raises(ValueError): validate_smtp_host('smtp.example.org',port,mode)
    allowed = (('smtp.example.org',2525,'10.0.0.1'),)
    assert validate_smtp_host('smtp.example.org',2525,'starttls',allowed) == 'smtp.example.org'
    with pytest.raises(ValueError): validate_smtp_host('other.example.org',2525,'starttls',allowed)
    for host in ['smtp.example.org/path','smtp.example.org?query','smtp.example.org#fragment']:
        with pytest.raises(ValueError): validate_smtp_host(host,587,'starttls')


@pytest.mark.parametrize('mode',['implicit_tls','starttls'])
def test_real_smtp_tls_pins_numeric_socket_and_original_sni(tmp_path,monkeypatch,mode):
    import datetime
    import threading
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'smtp.example.org')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
        .not_valid_after(now+datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('smtp.example.org')]),critical=False)
        .sign(key,hashes.SHA256()))
    cert_file,key_file = tmp_path/'cert.pem',tmp_path/'key.pem'
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file,key_file)
    sni,messages,errors = [],[],[]
    context.set_servername_callback(lambda sock,host,ctx:sni.append(host))
    listener = socket.socket(); listener.bind(('127.0.0.1',0)); listener.listen(1); listener.settimeout(3)
    def serve():
        try:
            sock,_ = listener.accept(); sock.settimeout(3)
            if mode == 'implicit_tls': sock = context.wrap_socket(sock,server_side=True)
            file = sock.makefile('rb'); sock.sendall(b'220 Ready\r\n')
            while True:
                line = file.readline()
                if not line: break
                if line.lower().startswith(b'ehlo'):
                    sock.sendall(b'250-Example\r\n250 STARTTLS\r\n')
                elif line.lower().startswith(b'starttls'):
                    sock.sendall(b'220 Go TLS\r\n'); file.close()
                    sock = context.wrap_socket(sock,server_side=True); file = sock.makefile('rb')
                elif line.lower().startswith(b'data'):
                    sock.sendall(b'354 Send message\r\n'); data = []
                    while (line := file.readline()) != b'.\r\n':
                        if not line: raise RuntimeError('Incomplete DATA')
                        data.append(line)
                    messages.append(b''.join(data)); sock.sendall(b'250 Accepted\r\n')
                else: sock.sendall(b'250 OK\r\n')
            file.close(); sock.close()
        except Exception as exc: errors.append(type(exc).__name__)
    thread = threading.Thread(target=serve,daemon=True); thread.start()
    client_context = ssl.create_default_context(cafile=str(cert_file))
    def dns_again(*args,**kwargs): pytest.fail('Pinned SMTP must not resolve again')
    monkeypatch.setattr(socket,'getaddrinfo',dns_again)
    client = PinnedSMTP('smtp.example.org',listener.getsockname()[1],'127.0.0.1',mode,client_context)
    try:
        client.ehlo()
        if mode == 'starttls': client.starttls(context=client_context); client.ehlo()
        assert client.sock.context.check_hostname and client.sock.context.verify_mode == ssl.CERT_REQUIRED
        client.mail('sender@example.org'); client.rcpt('recipient@example.org')
        assert client.data(email_message(configured()['transport'],configured(),ALERT))[0] == 250
    finally:
        client.close(); thread.join(timeout=3); listener.close()
    assert not thread.is_alive() and not errors
    assert sni == ['smtp.example.org'] and len(messages) == 1
