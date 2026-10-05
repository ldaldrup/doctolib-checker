"""One-recipient SMTP delivery with verified pinned TLS and guarded DATA."""
import hashlib
import re
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.headerregistry import Address
from email.policy import SMTP as SMTP_POLICY

from app.notifications import (DeliveryOutcome, SEND_BUDGET_SECONDS,
    CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS, MAX_RESPONSE_BYTES, render_notification)
from app.notification_secrets import SecretUnavailable
from app.webhooks import approved_addresses, validate_endpoint

MAX_MESSAGE_BYTES = 65536


def validate_smtp_host(host, port, tls_mode, allowlist=()):
    try:
        if (not isinstance(host, str) or len(host) > 253 or any(c in host for c in '/?#\\') or
                type(port) is not int or not 1 <= port <= 65535):
            raise ValueError()
        _, normalized = validate_endpoint('https://' + ('[' + host + ']' if ':' in host else host) + '/')
        if tls_mode not in ('implicit_tls', 'starttls'):
            raise ValueError()
        if port in (465, 587):
            if (port == 465) != (tls_mode == 'implicit_tls'):
                raise ValueError()
        elif not any(item[0] == normalized and item[1] == port for item in allowlist):
            raise ValueError()
        return normalized
    except (ValueError, TypeError):
        raise ValueError('smtp_invalid_transport') from None


def smtp_transport_usable(transport, secrets, allowlist=()):
    if not transport or not transport.get('enabled') or not transport.get('configured'):
        return False
    try:
        validate_smtp_host(transport['host'],transport['port'],transport['tls_mode'],allowlist)
        mailbox(secrets.decrypt(transport['sender_email_ciphertext']))
        for column in ('username_ciphertext','password_ciphertext'):
            if transport[column]:
                value = secrets.decrypt(transport[column])
                if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
                    return False
    except (SecretUnavailable,ValueError,TypeError,KeyError):
        return False
    return True


def mailbox(value):
    """Support a single bare ASCII mailbox; no display names or SMTPUTF8."""
    if not isinstance(value, str) or len(value) > 254:
        raise ValueError('smtp_invalid_address')
    if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value):
        raise ValueError('smtp_invalid_address')
    local, domain = value.rsplit('@', 1)
    if len(local) > 64 or len(domain) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in domain.split('.')):
        raise ValueError('smtp_invalid_address')
    return local + '@' + domain.lower()


def email_preview(alert):
    rendered = render_notification('email', alert)
    return {'text': rendered['text'], 'html': rendered['html']}


def email_message(transport, channel, alert):
    message = EmailMessage(policy=SMTP_POLICY)
    sender = mailbox(transport['sender_email'])
    sender_name = transport.get('sender_name', '')
    if not isinstance(sender_name, str) or len(sender_name) > 120 or any(ord(c) < 32 or ord(c) == 127 for c in sender_name):
        raise ValueError('smtp_invalid_address')
    message['From'] = Address(display_name=sender_name, addr_spec=sender)
    message['To'] = mailbox(channel['recipient'])
    message['Subject'] = 'Matching appointment available'
    identity = '\0'.join(str(value) for value in (alert.get('event_id', 'synthetic-preview'), channel.get('id', ''), message['To']))
    message['Message-ID'] = '<' + hashlib.sha256(identity.encode()).hexdigest() + '@doctolib-checker.invalid>'
    preview = email_preview(alert)
    message.set_content(preview['text'], cte='quoted-printable')
    message.add_alternative(preview['html'], subtype='html', cte='quoted-printable')
    payload = message.as_bytes()
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError('smtp_message_too_large')
    return payload


class ReplyLimitError(Exception):
    pass


class SMTPProtocolError(Exception):
    pass


class PinnedSMTP(smtplib.SMTP):
    def __init__(self, host, port, address, tls_mode, context=None):
        self.address = address
        self.tls_mode = tls_mode
        self.context = context or ssl.create_default_context()
        try:
            super().__init__(host, port, local_hostname='doctolib-checker', timeout=CONNECT_TIMEOUT_SECONDS)
        except BaseException:
            self.close()
            raise
        self.sock.settimeout(READ_TIMEOUT_SECONDS)

    def _get_socket(self, host, port, timeout):
        raw = socket.socket(socket.AF_INET6 if ':' in self.address else socket.AF_INET, socket.SOCK_STREAM)
        try:
            raw.settimeout(timeout)
            raw.connect((self.address, port))
            return self.context.wrap_socket(raw, server_hostname=host) if self.tls_mode == 'implicit_tls' else raw
        except BaseException:
            raw.close()
            raise

    def getreply(self):
        # smtplib bounds one line, but not an endless multiline response.
        if self.file is None:
            self.file = self.sock.makefile('rb')
        lines, size, previous = [], 0, None
        while True:
            line = self.file.readline(8193)
            if not line:
                raise smtplib.SMTPServerDisconnected()
            size += len(line)
            if len(line) > 8192 or size > MAX_RESPONSE_BYTES:
                raise ReplyLimitError()
            if len(line) < 4 or not line[:3].isdigit() or line[3:4] not in (b' ', b'-'):
                raise SMTPProtocolError()
            code = int(line[:3])
            if not 200 <= code <= 599 or (previous is not None and previous != code):
                raise SMTPProtocolError()
            lines.append(line[4:].strip())
            if line[3:4] == b' ':
                return code, b'\n'.join(lines)
            previous = code


def _rejected(code, phase, attempted):
    if 400 <= code < 500:
        return DeliveryOutcome('retry', 'smtp_' + phase + '_temporary', attempted=attempted)
    if 500 <= code < 600:
        return DeliveryOutcome('action_required', 'smtp_' + phase + '_rejected', attempted=attempted)
    return DeliveryOutcome('uncertain' if attempted else 'action_required', 'smtp_unconfirmed_response', attempted=attempted)


def _attempt(transport, channel, alert, allowlist, before_send=None,
             resolver=socket.getaddrinfo, connection_factory=PinnedSMTP):
    connection = None
    attempted = False
    phase = 'connection'
    try:
        payload = email_message(transport, channel, alert)
        host, port, mode = transport['host'], transport['port'], transport['tls_mode']
        host = validate_smtp_host(host, port, mode, allowlist)
        username, password = transport.get('username', ''), transport.get('password', '')
        if bool(username) != bool(password) or any(ord(c) < 32 or ord(c) == 127 for c in username + password):
            raise ValueError('smtp_invalid_transport')
        addresses = approved_addresses(host, port, allowlist, resolver)
        connection = connection_factory(host, port, addresses[0], mode)
        connection.ehlo_or_helo_if_needed()
        if mode == 'starttls':
            phase = 'tls'
            connection.starttls(context=ssl.create_default_context())
            code, _ = connection.ehlo()
            if code != 250:
                return _rejected(code, 'greeting', False)
        phase = 'auth'
        if transport.get('username'):
            connection.login(transport['username'], transport.get('password', ''))
        phase = 'sender'
        code, _ = connection.mail(mailbox(transport['sender_email']))
        if code != 250:
            return _rejected(code, phase, False)
        phase = 'recipient'
        code, _ = connection.rcpt(mailbox(channel['recipient']))
        if code not in (250, 251):
            return _rejected(code, phase, False)
        if before_send is not None and not before_send(remaining_seconds=SEND_BUDGET_SECONDS):
            return DeliveryOutcome('retry', 'alert_no_longer_eligible', attempted=False)
        attempted = True
        phase = 'data'
        code, _ = connection.data(payload)
        return DeliveryOutcome('sent') if 200 <= code < 300 else _rejected(code, phase, True)
    except ValueError as exc:
        code = str(exc) if str(exc) in ('smtp_invalid_address', 'smtp_message_too_large', 'smtp_invalid_transport') else 'smtp_invalid_transport'
        if str(exc) == 'webhook_address_blocked':
            code = 'smtp_address_blocked'
        return DeliveryOutcome('uncertain' if attempted else 'action_required', code, attempted=attempted)
    except ssl.SSLError:
        return DeliveryOutcome('uncertain' if attempted else 'action_required', 'smtp_acknowledgement_lost' if attempted else 'smtp_tls_failed', attempted=attempted)
    except smtplib.SMTPResponseException as exc:
        return _rejected(exc.smtp_code, phase, attempted)
    except (smtplib.SMTPNotSupportedError, ReplyLimitError, SMTPProtocolError) as exc:
        return DeliveryOutcome('uncertain' if attempted else 'action_required',
            'smtp_response_too_large' if isinstance(exc, ReplyLimitError) else 'smtp_unsupported_or_invalid_response', attempted=attempted)
    except smtplib.SMTPServerDisconnected:
        return DeliveryOutcome('uncertain' if attempted else 'retry', 'smtp_acknowledgement_lost' if attempted else 'smtp_connection_failed', attempted=attempted)
    except smtplib.SMTPException:
        return DeliveryOutcome('uncertain' if attempted else 'action_required', 'smtp_acknowledgement_lost' if attempted else 'smtp_unsupported_or_invalid_response', attempted=attempted)
    except OSError:
        return DeliveryOutcome('uncertain' if attempted else 'retry', 'smtp_acknowledgement_lost' if attempted else 'smtp_connection_failed', attempted=attempted)
    except Exception:
        return DeliveryOutcome('uncertain' if attempted else 'action_required', 'smtp_transport_error', attempted=attempted)
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass  # Closing cannot change an already received DATA acknowledgement.


def send_email_alert(settings, configured, alert, before_send=None, *, resolver=None, connection_factory=None):
    transport = configured['transport']
    allowlist = getattr(settings, 'webhook_allowlist', ())
    if resolver is not None or connection_factory is not None:
        return _attempt(transport, configured, alert, allowlist, before_send,
            resolver or socket.getaddrinfo, connection_factory or PinnedSMTP)
    from app.notifications import send_bounded_attempt
    return send_bounded_attempt(_attempt, (transport, configured, alert, allowlist), before_send, 'smtp')
