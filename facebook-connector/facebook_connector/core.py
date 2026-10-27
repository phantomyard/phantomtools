"""Page-only Graph API operations; no moderation, messages or automatic retries.

Graph operation structure adapted from Hagai Hen's MIT facebook-mcp-server.
See THIRD_PARTY_NOTICES.md. Transport, validation and transaction handling are new.
"""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import tempfile
from contextlib import contextmanager
from http.client import HTTPSConnection
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


class Invalid(ValueError):
    pass


class ExternalFailure(RuntimeError):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Invalid('Duplicate JSON key')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(Invalid('Invalid JSON number')))
    except (ValueError, UnicodeError) as exc:
        raise Invalid('Invalid JSON') from exc


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def safe_file(path):
    path = Path(path).absolute()
    if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in (path, *path.parents)):
        raise Invalid('Configuration and state paths cannot contain links')
    return path


def atomic(path, value):
    path = safe_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.facebook-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def origin(url):
    if not isinstance(url, str) or len(url) > 3000 or any(ord(c) < 33 for c in url) or '\\' in url:
        raise Invalid('Use a public HTTPS URL')
    try:
        parts = urlsplit(url)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.port not in (None, 443) or parts.fragment:
            raise Invalid('Use a public HTTPS URL without credentials or a fragment')
        host = parts.hostname.encode('idna').decode('ascii').lower()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            raise Invalid('Private destinations are not allowed')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and not address.is_global:
            raise Invalid('Private destinations are not allowed')
        return 'https://' + host
    except (ValueError, UnicodeError) as exc:
        raise Invalid('Invalid HTTPS URL') from exc


def validate(config):
    required = {'version', 'pageId', 'apiVersion', 'tokenEnv', 'allowPublishing', 'articleOrigins'}
    if not isinstance(config, dict) or set(config) != required or type(config['version']) is not int or config['version'] != 1:
        raise Invalid('Configuration needs exactly version, pageId, apiVersion, tokenEnv, allowPublishing and articleOrigins')
    if config['pageId'] is not None and (not isinstance(config['pageId'], str) or not re.fullmatch(r'[0-9]{5,30}', config['pageId'])):
        raise Invalid('Page ID must be numeric text')
    if config['apiVersion'] is not None and (not isinstance(config['apiVersion'], str) or not re.fullmatch(r'v[1-9][0-9]*\.0', config['apiVersion'])):
        raise Invalid('Select a supported Meta Graph API version, such as vNN.0')
    if not isinstance(config['tokenEnv'], str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{1,99}', config['tokenEnv']):
        raise Invalid('tokenEnv must be an environment variable name, never a credential')
    if type(config['allowPublishing']) is not bool or not isinstance(config['articleOrigins'], list):
        raise Invalid('Invalid publishing flag or article origins')
    if len(config['articleOrigins']) > 50 or len(set(map(str, config['articleOrigins']))) != len(config['articleOrigins']):
        raise Invalid('Article origins must be a unique list of at most 50 destinations')
    for value in config['articleOrigins']:
        if origin(value) != value:
            raise Invalid('Article origins must be normalized HTTPS origins without paths')
    return config


def load(path):
    path = safe_file(path)
    try:
        raw = path.read_bytes()
        if len(raw) > 50_000:
            raise Invalid('Configuration too large')
        return validate(decode(raw))
    except OSError as exc:
        raise Invalid('Cannot read connector configuration') from exc


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def pinned(base, address, secure):
    """A connection class that dials one pre-validated address only.

    HTTPS keeps the URL hostname for SNI and certificate verification, so a
    second DNS answer cannot move the request somewhere else. Proxies are off:
    verification must show what the public internet sees.
    """
    class Pinned(base):
        def connect(self):
            self.sock = socket.create_connection((address, self.port), self.timeout, self.source_address)
            if secure:
                self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
    return Pinned


class PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, address):
        super().__init__()
        self.address = address

    def https_open(self, req):
        return self.do_open(pinned(HTTPSConnection, self.address, True), req)


class Graph:
    def __init__(self, config, opener=None):
        self.config = config
        self.opener = opener or build_opener(NoRedirect())

    def request(self, method, endpoint, parameters=None):
        config = self.config
        if not config['pageId'] or not config['apiVersion']:
            raise Invalid('Configure the Page ID and a currently supported API version first')
        token = os.environ.get(config['tokenEnv'], '')
        if not token or len(token) > 20_000 or any(ord(c) < 33 for c in token):
            raise Invalid('Page token unavailable; inject it through Phantombot --env-secret')
        if not re.fullmatch(r'(?:me|[0-9_]+)(?:/feed)?', endpoint):
            raise Invalid('Unsupported Graph endpoint')
        url = 'https://graph.facebook.com/' + config['apiVersion'] + '/' + endpoint
        parameters = parameters or {}
        body = urlencode(parameters).encode('utf-8') if method == 'POST' else None
        if method == 'GET' and parameters:
            url += '?' + urlencode(parameters)
        request = Request(url, data=body, method=method, headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'PhantomFacebook/0.1'})
        try:
            with self.opener.open(request, timeout=20) as response:
                if response.status != 200:
                    raise ExternalFailure('Facebook returned an unsuccessful HTTP status')
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ExternalFailure('Facebook response too large')
                value = decode(raw)
        except (HTTPError, URLError, OSError, Invalid) as exc:
            # Provider bodies, exception strings and request objects can contain secrets.
            if isinstance(exc, HTTPError):
                details = 'HTTP ' + str(exc.code)
                try:
                    raw_error = exc.read(1_000_001)
                    if len(raw_error) <= 1_000_000:
                        error = decode(raw_error).get('error', {})
                        for field in ('code', 'error_subcode'):
                            number = error.get(field)
                            if type(number) is int and 0 <= number <= 2_147_483_647:
                                details += ', ' + field + ' ' + str(number)
                except (Invalid, OSError, AttributeError, TypeError):
                    pass
                finally:
                    exc.close()
                raise ExternalFailure('Facebook request failed (' + details + '); inspect token permissions privately') from None
            raise ExternalFailure('Facebook request failed; check permissions, API version and token privately') from None
        if not isinstance(value, dict) or 'error' in value:
            raise ExternalFailure('Facebook rejected the operation; inspect account permissions privately')
        return value


def verify_page(config, graph):
    value = graph.request('GET', config['pageId'] or '', {'fields': 'id,name'})
    if value.get('id') != config['pageId'] or not isinstance(value.get('name'), str) or not value['name'] or len(value['name']) > 500:
        raise ExternalFailure('Token did not verify the configured Page')
    # Provider text is untrusted data, never an instruction or publishing permission.
    return {'ok': True, 'pageId': value['id'], 'name': value['name'], 'publishingPermissionVerified': False}


def verify_live(url, content_hash):
    host = urlsplit(url).hostname
    try:
        addresses = [item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]
        if not addresses or any(not ipaddress.ip_address(item.split('%')[0]).is_global for item in addresses):
            raise ExternalFailure('Article hostname resolved to a nonpublic address')
        # Dial the address that was just validated: a second resolution must not
        # move this request somewhere else.
        opener = build_opener(NoRedirect(), PinnedHTTPSHandler(addresses[0]), ProxyHandler({}))
        with opener.open(Request(url, headers={'User-Agent': 'PhantomFacebook/0.1', 'Cache-Control': 'no-cache'}), timeout=20) as response:
            if response.status != 200 or 'text/html' not in response.headers.get('Content-Type', '').lower():
                raise ExternalFailure('Live article is not an HTML page')
            body = response.read(5_000_001)
            if len(body) > 5_000_000 or hashlib.sha256(body).hexdigest() != content_hash:
                raise ExternalFailure('Live article does not match the approved HTML hash')
    except (HTTPError, URLError, OSError, ValueError) as exc:
        if isinstance(exc, HTTPError):
            exc.close()
        raise ExternalFailure('Could not verify the live article') from None
    return True


def publication(config, url, message, content_hash, key):
    if origin(url) not in config['articleOrigins']:
        raise Invalid('Article origin is not configured for this connection')
    if not isinstance(message, str) or not message.strip() or len(message) > 5000 or any(ord(c) < 32 and c not in '\n\t' for c in message):
        raise Invalid('Message must contain 1–5000 characters of authored text')
    for name, value in [('contentHash', content_hash), ('idempotencyKey', key)]:
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
            raise Invalid(name + ' must be a SHA-256 hex value')
    if not config['pageId'] or not config['apiVersion']:
        raise Invalid('Page ID and supported API version are required')
    return {'pageId': config['pageId'], 'apiVersion': config['apiVersion'], 'url': url, 'message': message,
            'contentHash': content_hash, 'idempotencyKey': key}


class Connector:
    def __init__(self, path, graph_factory=Graph, verifier=verify_live):
        self.path = safe_file(path)
        # Only these two files, alongside the operator's config, are connector-owned.
        self.state_path = safe_file(self.path.with_name(self.path.name + '.operations.json'))
        self.lock_path = safe_file(self.path.with_name(self.path.name + '.lock'))
        self.graph_factory = graph_factory
        self.verifier = verifier

    def status(self):
        config = load(self.path)
        return {'ok': True, 'configured': bool(config['pageId'] and config['apiVersion']),
                'pageId': config['pageId'], 'apiVersion': config['apiVersion'], 'publishingEnabled': config['allowPublishing'],
                'credentialAvailable': bool(os.environ.get(config['tokenEnv'])), 'accountVerified': False}

    def page(self):
        config = load(self.path)
        return verify_page(config, self.graph_factory(config))

    def access(self):
        config = load(self.path)
        graph = self.graph_factory(config)
        page = verify_page(config, graph)
        identity = graph.request('GET', 'me', {'fields': 'id'})
        if not isinstance(identity.get('id'), str) or not re.fullmatch(r'[0-9]+', identity['id']):
            raise ExternalFailure('Invalid token identity response')
        result = {'ok': True, 'pageId': page['pageId'],
                  'tokenActsAsPage': identity['id'] == config['pageId'],
                  'feedReadable': False, 'publishingPermissionVerified': False}
        try:
            feed = graph.request('GET', config['pageId'] + '/feed', {'fields': 'id', 'limit': '1'})
            if not isinstance(feed.get('data'), list):
                raise ExternalFailure('Invalid Page feed response')
            result['feedReadable'] = True
        except ExternalFailure as exc:
            result['ok'] = False
            result['readError'] = str(exc)
        return result

    def plan(self, **args):
        config = load(self.path)
        value = publication(config, **args)
        return {'plan': value, 'approval': digest(value), 'liveVerified': False, 'sent': False}

    @contextmanager
    def lock(self):
        safe_file(self.lock_path)
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise Invalid('Connector busy or stale lock; check the process before removing its lock file') from None
        try:
            os.close(fd)
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)

    def state(self):
        safe_file(self.state_path)
        if not self.state_path.exists():
            return {'version': 1, 'operations': {}}
        try:
            if self.state_path.stat().st_size > 20_000_000:
                raise Invalid('Publishing ledger too large; archive it after reconciliation')
            state = decode(self.state_path.read_bytes())
        except OSError:
            raise Invalid('Cannot read publishing ledger') from None
        if not isinstance(state, dict) or set(state) != {'version', 'operations'} or state['version'] != 1 or not isinstance(state['operations'], dict):
            raise Invalid('Invalid publishing ledger; refuse publication')
        for key, value in state['operations'].items():
            if not re.fullmatch(r'[0-9a-f]{64}', key) or not isinstance(value, dict) or value.get('state') not in ('pending', 'uncertain', 'complete', 'retry-approved') or not re.fullmatch(r'[0-9a-f]{64}', str(value.get('fingerprint', ''))):
                raise Invalid('Malformed publishing operation; refuse publication')
            if value['state'] == 'complete' and (not isinstance(value.get('result'), dict) or value['result'].get('ok') is not True or not re.fullmatch(r'[0-9]+_[0-9]+', str(value['result'].get('postId', '')))):
                raise Invalid('Malformed completed receipt')
        return state

    def operation(self, key):
        if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key):
            raise Invalid('Invalid operation key')
        return {'ok': True, 'operation': self.state()['operations'].get(key)}

    def publish(self, url, message, content_hash, key, approval=None):
        config = load(self.path)
        value = publication(config, url, message, content_hash, key)
        fingerprint = digest(value)
        if approval is not None and approval != fingerprint:
            raise Invalid('Approval does not match this publication')
        if not config['allowPublishing']:
            raise Invalid('Publishing disabled; complete account setup and operator authorization first')
        with self.lock():
            state = self.state()
            previous = state['operations'].get(key)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise Invalid('Operation key reused with different publication content')
                if previous['state'] == 'complete':
                    return previous['result']
                if previous['state'] != 'retry-approved':
                    raise ExternalFailure('Previous result uncertain; reconcile with Facebook before retrying')
            graph = self.graph_factory(config)
            verify_page(config, graph)
            if self.verifier(url, content_hash) is not True:
                raise ExternalFailure('Live article verification failed')
            # Re-read configuration after network checks; edits invalidate this request.
            if load(self.path) != config:
                raise Invalid('Connection changed during verification; review again')
            if len(state['operations']) >= 10000 and key not in state['operations']:
                raise Invalid('Ledger capacity reached; reconcile and archive before publishing')
            state['operations'][key] = {'state': 'pending', 'fingerprint': fingerprint}
            atomic(self.state_path, state)
            try:
                response = graph.request('POST', config['pageId'] + '/feed', {'message': message, 'link': url})
                post_id = response.get('id')
                if not isinstance(post_id, str) or not re.fullmatch(re.escape(config['pageId']) + r'_[0-9]+', post_id):
                    raise ExternalFailure('Facebook did not return a valid Page post receipt')
                result = {'ok': True, 'postId': post_id, 'postUrl': 'https://www.facebook.com/' + post_id,
                          'pageId': config['pageId'], 'liveVerified': True}
                state['operations'][key].update(state='complete', result=result)
                atomic(self.state_path, state)
                return result
            except Exception:
                state['operations'][key].update(state='uncertain')
                atomic(self.state_path, state)
                raise ExternalFailure('Posting result uncertain; inspect Facebook before reconciliation. No automatic retry.') from None

    def reconcile(self, key, post_id=None, not_sent=False):
        if type(not_sent) is not bool or bool(post_id) == not_sent:
            raise Invalid('Choose a confirmed post ID or confirmed-not-sent')
        self.operation(key)
        config = load(self.path)
        with self.lock():
            state = self.state()
            previous = state['operations'].get(key)
            if not previous or previous['state'] not in ('pending', 'uncertain'):
                raise Invalid('Only pending or uncertain operations can be reconciled')
            if post_id:
                if not isinstance(post_id, str) or not re.fullmatch(re.escape(config['pageId'] or '') + r'_[0-9]+', post_id):
                    raise Invalid('Receipt must belong to the configured Page')
                response = self.graph_factory(config).request('GET', post_id, {'fields': 'id'})
                if response.get('id') != post_id:
                    raise ExternalFailure('Facebook did not confirm the receipt')
                previous.update(state='complete', result={'ok': True, 'postId': post_id, 'postUrl': 'https://www.facebook.com/' + post_id, 'pageId': config['pageId'], 'reconciled': True})
            else:
                # Operator-only assertion after inspecting Facebook history. Never MCP.
                previous.update(state='retry-approved')
            atomic(self.state_path, state)
        return {'ok': True, 'operation': previous}
