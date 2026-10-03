"""Fixed notification payloads and HTTPS transport with pinned, approved addresses."""
import base64
import http.client
import ipaddress
import json
import re
import socket
import ssl
from urllib.parse import urlsplit, urlunsplit

from app.notifications import (DeliveryOutcome, SEND_BUDGET_SECONDS,
    CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS, MAX_RESPONSE_BYTES,
    send_bounded_attempt)

MAX_PAYLOAD_BYTES = 16384
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in
    ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7'))
METADATA_ADDRESSES = {ipaddress.ip_address(value) for value in
    ('100.100.100.200', '168.63.129.16', 'fd00:ec2::254')}


def validate_endpoint(endpoint, type='webhook', allowlist=()):
    try:
        if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 4096:
            raise ValueError()
        if any(ord(char) <= 32 or ord(char) == 127 for char in endpoint) or '\\' in endpoint or re.search(r'%(?![0-9a-fA-F]{2})', endpoint):
            raise ValueError()
        parts = urlsplit(endpoint)
        host = parts.hostname
        port = parts.port if parts.port is not None else 443
        if (parts.scheme != 'https' or not host or parts.username is not None or
                parts.password is not None or parts.fragment or '%' in host or not 1 <= port <= 65535):
            raise ValueError()
        # HTTP request targets must be encoded by the caller; do not silently
        # rewrite a potentially signed endpoint path or query.
        (parts.path + parts.query).encode('ascii')
        host = host.encode('idna').decode('ascii').lower()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if (len(host) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                          for label in host.split('.'))):
                raise ValueError()
        if port != 443 and not any(item[0] == host and item[1] == port for item in allowlist):
            raise ValueError()
        if type not in ('ntfy', 'webhook'):
            raise ValueError()
        if type == 'ntfy' and (parts.query or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', parts.path.rsplit('/', 1)[-1])):
            raise ValueError()
        authority = '[' + host + ']' if ':' in host else host
        if port != 443:
            authority += ':' + str(port)
        return urlunsplit(('https', authority, parts.path or '/', parts.query, '')), host
    except (ValueError, UnicodeError):
        raise ValueError('webhook_invalid_endpoint') from None


def approved_addresses(host, port, allowlist=(), resolver=socket.getaddrinfo):
    """Validate every DNS answer; connect never resolves the hostname again."""
    addresses = []
    for family, _, _, _, address in resolver(host, port, type=socket.SOCK_STREAM):
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise ValueError('webhook_address_blocked')
        ip = ipaddress.ip_address(address[0])
        effective = getattr(ip, 'ipv4_mapped', None) or ip
        private_exception = ((host, port, str(ip)) in allowlist and
            any(effective in network for network in PRIVATE_NETWORKS))
        if (effective.is_reserved or effective.is_loopback or effective.is_link_local or effective.is_multicast or
                effective.is_unspecified or effective in METADATA_ADDRESSES or
                (not effective.is_global and not private_exception)):
            raise ValueError('webhook_address_blocked')
        if str(ip) not in addresses:
            addresses.append(str(ip))
    if not addresses:
        raise socket.gaierror()
    return addresses


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, address, context=None):
        super().__init__(host, port, timeout=CONNECT_TIMEOUT_SECONDS, context=context)
        self.address = address

    def connect(self):
        # Use a numeric address directly, bypassing DNS at the actual socket.
        family = socket.AF_INET6 if ':' in self.address else socket.AF_INET
        raw = socket.socket(family, socket.SOCK_STREAM)
        try:
            raw.settimeout(CONNECT_TIMEOUT_SECONDS)
            raw.connect((self.address, self.port))
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            self.sock.settimeout(READ_TIMEOUT_SECONDS)
        except BaseException:
            raw.close()
            raise


def webhook_payload(channel, alert):
    if channel['type'] == 'ntfy':
        topic = urlsplit(channel['endpoint']).path.rsplit('/', 1)[-1]
        return {'topic': topic, 'title': 'Matching appointment available',
            'message': f"{alert.get('slot_count', 1)} matching appointment slot(s)\n"
                f"{alert.get('practitioner_name', 'Practitioner')}\n{alert.get('practice_name', 'Practice')}\n"
                f"Earliest: {alert.get('earliest_slot', '')} ({alert.get('time_zone') or 'UTC'})\n"
                f"Event: {alert.get('event_id') or 'synthetic-test'}",
            'click': alert.get('booking_url', ''), 'priority': channel.get('ntfy_priority', 3)}
    return {'schema_version': 1, 'event_id': alert.get('event_id') or 'synthetic-test',
        'trigger': alert.get('triggered_by') or 'test', 'search_revision': alert.get('search_revision'),
        'job': {'id': alert.get('job_id'), 'name': alert.get('job_name', '')},
        'target': {'id': alert.get('target_id'), 'practitioner_name': alert.get('practitioner_name', ''),
            'practice_name': alert.get('practice_name', '')},
        'earliest_slot': alert.get('earliest_slot'), 'slot_count': alert.get('slot_count', 1),
        'checked_at': alert.get('checked_at'), 'time_zone': alert.get('time_zone') or 'UTC',
        'booking_url': alert.get('booking_url', '')}


def _prepare(channel, allowlist):
    endpoint, host = validate_endpoint(channel['endpoint'], channel['type'], allowlist)
    parts = urlsplit(endpoint)
    priority = channel.get('ntfy_priority', 3)
    if type(priority) is not int or not 1 <= priority <= 5:
        raise ValueError('webhook_invalid_payload')
    auth = channel.get('auth_type', 'none')
    headers = {'Content-Type': 'application/json', 'User-Agent': 'DoctolibChecker/2.0'}
    if auth == 'bearer':
        token = channel.get('auth_token', '')
        if not token or not re.fullmatch(r'[A-Za-z0-9._~+/-]+=*', token):
            raise ValueError('webhook_invalid_auth')
        headers['Authorization'] = 'Bearer ' + token
    elif auth == 'basic':
        username, password = channel.get('auth_username', ''), channel.get('auth_password', '')
        if not username or not password or ':' in username or any(ord(c) < 32 for c in username + password):
            raise ValueError('webhook_invalid_auth')
        headers['Authorization'] = 'Basic ' + base64.b64encode((username + ':' + password).encode()).decode()
    elif auth != 'none':
        raise ValueError('webhook_invalid_auth')
    path = parts.path or '/'
    if channel['type'] == 'ntfy':
        path = path.rsplit('/', 1)[0] + '/'
    elif parts.query:
        path += '?' + parts.query
    return host, parts.port or 443, path, headers


def _attempt(channel, alert, allowlist, before_send=None, resolver=socket.getaddrinfo,
             connection_factory=PinnedHTTPSConnection):
    connection = None
    attempted = False
    try:
        host, port, path, headers = _prepare(channel, allowlist)
        body = json.dumps(webhook_payload(channel, alert), ensure_ascii=False, separators=(',', ':')).encode()
        if len(body) > MAX_PAYLOAD_BYTES:
            return DeliveryOutcome('action_required', 'webhook_payload_too_large', attempted=False)
        addresses = approved_addresses(host, port, allowlist, resolver)
        connection = connection_factory(host, port, addresses[0])
        # TLS is established before the persisted send boundary. Failed DNS,
        # address policy and TLS verification cannot have sent a notification.
        connection.connect()
        if before_send is not None and not before_send(remaining_seconds=SEND_BUDGET_SECONDS):
            return DeliveryOutcome('retry', 'alert_no_longer_eligible', attempted=False)
        attempted = True
        connection.request('POST', path, body=body, headers=headers)
        response = connection.getresponse()
        content = response.read(MAX_RESPONSE_BYTES + 1)
        if len(content) > MAX_RESPONSE_BYTES:
            return DeliveryOutcome('uncertain', 'webhook_response_too_large')
        status = response.status
        if 200 <= status < 300:
            if channel['type'] == 'webhook':
                return DeliveryOutcome('sent')
            try:
                value = json.loads(content)
            except (ValueError, UnicodeError):
                value = None
            topic = urlsplit(channel['endpoint']).path.rsplit('/', 1)[-1]
            if isinstance(value, dict) and value.get('event') == 'message' and value.get('topic') == topic and isinstance(value.get('id'), str) and value['id']:
                return DeliveryOutcome('sent')
            return DeliveryOutcome('uncertain', 'ntfy_unconfirmed_response')
        if status == 429 or status in (408, 500, 502, 503, 504):
            try:
                delay = min(900.0, max(5.0, float(response.getheader('Retry-After'))))
            except (ValueError, TypeError, OverflowError):
                delay = None
            return DeliveryOutcome('retry', 'webhook_rate_limited' if status == 429 else 'webhook_temporary_rejection', delay)
        return DeliveryOutcome('action_required', 'webhook_redirect_blocked' if 300 <= status < 400 else 'webhook_rejected_' + str(status))
    except ValueError as exc:
        code = str(exc) if str(exc) in ('webhook_invalid_endpoint', 'webhook_address_blocked', 'webhook_invalid_payload', 'webhook_invalid_auth') else 'webhook_invalid_payload'
        return DeliveryOutcome('action_required', code, attempted=attempted)
    except ssl.SSLError:
        return DeliveryOutcome('uncertain' if attempted else 'action_required',
            'webhook_acknowledgement_lost' if attempted else 'webhook_tls_failed', attempted=attempted)
    except (OSError, http.client.HTTPException):
        return DeliveryOutcome('uncertain' if attempted else 'retry',
            'webhook_acknowledgement_lost' if attempted else 'webhook_connect_failed', attempted=attempted)
    except Exception:
        return DeliveryOutcome('uncertain' if attempted else 'action_required', 'webhook_transport_error', attempted=attempted)
    finally:
        if connection is not None:
            connection.close()


def send_webhook_alert(settings, channel, alert, before_send=None, *, resolver=None, connection_factory=None):
    """One bounded attempt; injected transports are reserved for offline checks."""
    allowlist = getattr(settings, 'webhook_allowlist', ())
    if resolver is not None or connection_factory is not None:
        return _attempt(channel, alert, allowlist, before_send, resolver or socket.getaddrinfo,
                        connection_factory or PinnedHTTPSConnection)
    return send_bounded_attempt(_attempt, (channel, alert, allowlist), before_send, 'webhook')
