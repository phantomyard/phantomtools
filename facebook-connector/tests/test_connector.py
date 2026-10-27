import hashlib
import io
import json
import os
from http.client import HTTPSConnection
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from facebook_connector import core, mcp

PAGE = '1234567890'
HASH = 'a' * 64
KEY = 'b' * 64


def config():
    return {'version': 1, 'pageId': PAGE, 'apiVersion': 'v22.0', 'tokenEnv': 'FIXTURE_TOKEN',
            'allowPublishing': True, 'articleOrigins': ['https://example.com']}


class FakeGraph:
    def __init__(self):
        self.calls = []; self.fail = False; self.page_id = PAGE; self.receipt = PAGE + '_123'

    def request(self, method, endpoint, parameters=None):
        self.calls.append((method, endpoint, parameters))
        if method == 'POST':
            if self.fail:
                raise core.ExternalFailure('Synthetic timeout')
            return {'id': self.receipt}
        if '_' in endpoint:
            return {'id': endpoint}
        return {'id': self.page_id, 'name': 'Página de prueba <untrusted>'}


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.json'
        self.model = config(); core.atomic(self.path, self.model)
        self.graph = FakeGraph(); self.live_calls = []
        self.connector = core.Connector(self.path, lambda _: self.graph, self.verify)

    def verify(self, url, checksum):
        self.live_calls.append((url, checksum)); return True

    def publish(self, **changes):
        args = {'url': 'https://example.com/article-es.html', 'message': 'Artículo aprobado', 'content_hash': HASH, 'key': KEY}
        args.update(changes); return self.connector.publish(**args)

    def files(self):
        return {p.name: p.read_bytes() for p in self.path.parent.iterdir() if p.is_file()}

    def test_setup_and_review_commands_are_readonly(self):
        before = self.files()
        self.assertFalse(self.connector.status()['accountVerified'])
        page = self.connector.page(); self.assertEqual(page['name'], 'Página de prueba <untrusted>')
        self.assertFalse(page['publishingPermissionVerified'])
        plan = self.connector.plan(url='https://example.com/a', message='Hola', content_hash=HASH, key=KEY)
        self.assertFalse(plan['sent']); self.assertFalse(plan['liveVerified'])
        self.assertIsNone(self.connector.operation(KEY)['operation'])
        self.assertEqual(before, self.files()); self.assertFalse(self.live_calls)

    def test_access_check_is_readonly_and_distinguishes_user_tokens(self):
        before = self.files()
        def response(method, endpoint, parameters=None):
            self.assertEqual(method, 'GET')
            if endpoint == 'me': return {'id': '999999'}
            if endpoint.endswith('/feed'): return {'data': []}
            return {'id': PAGE, 'name': 'Fixture Page'}
        with patch.object(self.graph, 'request', side_effect=response):
            result = mcp.call(self.connector, 'facebook_check_page_access', {})
        self.assertFalse(result['tokenActsAsPage'])
        self.assertTrue(result['feedReadable'])
        self.assertFalse(result['publishingPermissionVerified'])
        self.assertEqual(before, self.files())

    def test_access_check_reports_read_failure_without_posting(self):
        before = self.files()
        def response(method, endpoint, parameters=None):
            self.assertEqual(method, 'GET')
            if endpoint.endswith('/feed'): raise core.ExternalFailure('Facebook request failed (HTTP 400, code 10)')
            return {'id': PAGE, 'name': 'Fixture Page'}
        with patch.object(self.graph, 'request', side_effect=response):
            result = self.connector.access()
        self.assertTrue(result['tokenActsAsPage'])
        self.assertFalse(result['ok'])
        self.assertFalse(result['feedReadable'])
        self.assertIn('code 10', result['readError'])
        self.assertEqual(before, self.files())

    def test_disabled_publication_has_no_external_calls_or_output(self):
        self.model['allowPublishing'] = False; core.atomic(self.path, self.model); before = self.files()
        with self.assertRaises(core.Invalid): self.publish()
        self.assertEqual(before, self.files()); self.assertFalse(self.graph.calls)

    def test_exact_page_and_live_article_checked_before_post(self):
        result = self.publish()
        self.assertIs(result['ok'], True); self.assertEqual(result['postId'], PAGE + '_123')
        self.assertEqual(self.graph.calls[0][0], 'GET')
        self.assertEqual(self.graph.calls[1], ('POST', PAGE + '/feed', {'message': 'Artículo aprobado', 'link': 'https://example.com/article-es.html'}))
        self.assertEqual(self.live_calls, [('https://example.com/article-es.html', HASH)])
        self.assertFalse(self.connector.lock_path.exists())

    def test_wrong_page_or_not_live_never_posts(self):
        self.graph.page_id = '9999999999'
        with self.assertRaises(core.ExternalFailure): self.publish()
        self.assertFalse(self.connector.state_path.exists())
        self.graph.page_id = PAGE; self.connector.verifier = lambda *_: False
        with self.assertRaises(core.ExternalFailure): self.publish()
        self.assertFalse(any(c[0] == 'POST' for c in self.graph.calls))

    def test_deduplication_and_key_collision(self):
        first = self.publish(); count = len(self.graph.calls)
        self.assertEqual(self.publish(), first); self.assertEqual(len(self.graph.calls), count)
        with self.assertRaises(core.Invalid): self.publish(message='Different')

    def test_timeout_is_durable_and_not_retried(self):
        self.graph.fail = True
        with self.assertRaises(core.ExternalFailure): self.publish()
        self.assertEqual(self.connector.operation(KEY)['operation']['state'], 'uncertain')
        count = len(self.graph.calls); self.graph.fail = False
        with self.assertRaises(core.ExternalFailure): self.publish()
        self.assertEqual(count, len(self.graph.calls))

    def test_receipt_must_belong_to_selected_page(self):
        self.graph.receipt = '9999999999_123'
        with self.assertRaises(core.ExternalFailure): self.publish()
        self.assertEqual(self.connector.operation(KEY)['operation']['state'], 'uncertain')

    def test_pending_after_crash_blocks_retry(self):
        value = core.publication(self.model, 'https://example.com/article-es.html', 'Artículo aprobado', HASH, KEY)
        core.atomic(self.connector.state_path, {'version': 1, 'operations': {KEY: {'state': 'pending', 'fingerprint': core.digest(value)}}})
        with self.assertRaises(core.ExternalFailure): self.publish()
        self.assertFalse(self.graph.calls)

    def test_operator_reconciliation_requires_checked_receipt(self):
        self.graph.fail = True
        with self.assertRaises(core.ExternalFailure): self.publish()
        result = self.connector.reconcile(KEY, post_id=PAGE + '_123')
        self.assertEqual(result['operation']['state'], 'complete')
        self.assertTrue(self.publish()['reconciled'])
        self.assertEqual(sum(c[0] == 'POST' for c in self.graph.calls), 1)

    def test_confirmed_not_sent_enables_one_explicit_retry(self):
        self.graph.fail = True
        with self.assertRaises(core.ExternalFailure): self.publish()
        self.connector.reconcile(KEY, not_sent=True); self.graph.fail = False
        self.assertTrue(self.publish()['ok'])
        with self.assertRaises(core.Invalid): self.connector.reconcile(KEY, not_sent=True)

    def test_configuration_change_during_verification_prevents_post(self):
        def changed(*_):
            self.model['allowPublishing'] = False; core.atomic(self.path, self.model); return True
        self.connector.verifier = changed
        with self.assertRaises(core.Invalid): self.publish()
        self.assertFalse(self.connector.state_path.exists())

    def test_concurrent_calls_refused(self):
        with self.connector.lock():
            with self.assertRaises(core.Invalid): self.publish()
        self.assertFalse(self.graph.calls)

    def test_bad_inputs_preserve_every_file(self):
        before = self.files()
        for changes in ({'url': 'http://example.com/a'}, {'url': 'https://evil.example/a'}, {'url': 'https://example.com/a#frag'}, {'message': ''}, {'content_hash': 'bad'}, {'key': True}, {'approval': 'bad'}):
            with self.subTest(changes=changes), self.assertRaises(core.Invalid): self.publish(**changes)
        self.assertEqual(before, self.files()); self.assertFalse(self.graph.calls)

    def test_malformed_ledger_cannot_be_adopted(self):
        core.atomic(self.connector.state_path, {'version': 1, 'operations': {'bad': {'state': 'complete'}}})
        before = self.files()
        with self.assertRaises(core.Invalid): self.publish()
        self.assertEqual(before, self.files()); self.assertFalse(self.graph.calls)

    def test_secret_and_unknown_config_fields_rejected(self):
        for key, value in [('accessToken', 'fixture'), ('allowPublishing', 1), ('pageId', '../me'), ('apiVersion', 'latest'), ('articleOrigins', ['https://example.com/a']), ('articleOrigins', ['https://127.0.0.1'])]:
            model = config(); model[key] = value
            with self.subTest(key=key), self.assertRaises(core.Invalid): core.validate(model)
        with self.assertRaises(core.Invalid): core.decode('{"a":1,"a":2}')

    def test_links_are_not_followed_for_state_ownership(self):
        target = self.path.parent / 'elsewhere'; target.write_text('preserve')
        try: self.connector.state_path.symlink_to(target)
        except OSError: self.skipTest('Symlink creation unavailable on this installation')
        with self.assertRaises(core.Invalid): self.publish()
        self.assertEqual(target.read_text(), 'preserve')

    def test_mcp_has_six_tools_and_no_reconciliation_or_delete(self):
        source = io.StringIO('\n'.join(json.dumps(v) for v in [
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26'}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'facebook_verify_page'}},
            {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'delete_post'}},
        ]) + '\n')
        output = io.StringIO(); mcp.serve(self.connector, source, output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 4); self.assertEqual(len(responses[1]['result']['tools']), 6)
        self.assertIn('Página', responses[2]['result']['content'][0]['text'])
        self.assertTrue(responses[3]['result']['isError']); self.assertFalse(any(c[0]=='POST' for c in self.graph.calls))

    def test_mcp_publication_obeys_disabled_flag_and_complete_receipt(self):
        args = {'url':'https://example.com/a', 'message':'Prueba aprobada', 'contentHash':HASH, 'idempotencyKey':KEY}
        self.model['allowPublishing'] = False; core.atomic(self.path, self.model)
        with self.assertRaises(core.Invalid): mcp.call(self.connector, 'facebook_publish_article', args)
        self.model['allowPublishing'] = True; core.atomic(self.path, self.model)
        result = mcp.call(self.connector, 'facebook_publish_article', args)
        self.assertTrue(result['ok']); self.assertEqual(result['postId'], PAGE + '_123')
        with self.assertRaises(core.Invalid): mcp.call(self.connector, 'facebook_publish_article', {**args, 'accessToken':'synthetic'})

    def test_mcp_rejects_malformed_and_oversized_frames(self):
        for source in ('{not json}\n', '{"jsonrpc":"2.0","id":true,"method":"initialize"}\n', 'x' * 100_001 + '\n'):
            output = io.StringIO(); mcp.serve(self.connector,io.StringIO(source),output)
            self.assertEqual(json.loads(output.getvalue())['error']['code'], -32600)
        self.assertFalse(self.graph.calls)

    def test_cli_works_outside_repository_and_has_exit_codes(self):
        wrapper = Path(__file__).resolve().parents[1] / 'bin/phantom-facebook'
        result = subprocess.run([sys.executable, str(wrapper), '--config', str(self.path), 'check'], cwd=self.temp.name, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0); self.assertTrue(json.loads(result.stdout)['ok'])
        request = self.path.parent / 'request.json'; core.atomic(request, {'url':'https://example.com/a','message':'fixture','contentHash':HASH,'idempotencyKey':KEY})
        result = subprocess.run([sys.executable, str(wrapper), '--config', str(self.path), 'publish', '--request', str(request), '--approve', 'wrong'], cwd=self.temp.name, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2); self.assertFalse(self.connector.state_path.exists())

    def test_lock_release_tolerates_a_removed_file(self):
        with self.connector.lock():
            self.connector.lock_path.unlink()
        self.assertFalse(self.connector.lock_path.exists())


class TransportTests(unittest.TestCase):
    def response(self, payload, status=200):
        result = unittest.mock.MagicMock(); result.__enter__.return_value = result
        result.status = status; result.read.return_value = core.encoded(payload)
        return result

    def test_token_is_header_only_and_post_link_is_explicit(self):
        opener = unittest.mock.Mock(); opener.open.return_value = self.response({'id': PAGE + '_123'})
        with patch.dict(os.environ, {'FIXTURE_TOKEN':'synthetic-private-token'}):
            core.Graph(config(), opener).request('POST', PAGE + '/feed', {'message':'Hola','link':'https://example.com/a'})
        req = opener.open.call_args.args[0]
        self.assertNotIn('synthetic-private-token', req.full_url); self.assertNotIn(b'synthetic-private-token', req.data)
        self.assertEqual(req.get_header('Authorization'), 'Bearer synthetic-private-token')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 20)

    def test_provider_failures_do_not_leak_secrets(self):
        for failure in (URLError('synthetic-private-token'), HTTPError('https://x/?secret=synthetic-private-token', 403, 'synthetic-private-token', {}, None)):
            opener = unittest.mock.Mock(); opener.open.side_effect = failure
            with patch.dict(os.environ, {'FIXTURE_TOKEN':'synthetic-private-token'}), self.assertRaises(core.ExternalFailure) as exc:
                core.Graph(config(), opener).request('GET', PAGE)
            self.assertNotIn('synthetic-private-token', str(exc.exception))

    def test_http_diagnostics_allow_only_numeric_codes(self):
        payload = {'error': {'code': 10, 'error_subcode': 42, 'message': 'synthetic-private-token', 'fbtrace_id': 'private-trace'}}
        failure = HTTPError('https://x/?secret=synthetic-private-token', 400, 'private', {}, io.BytesIO(core.encoded(payload)))
        opener = unittest.mock.Mock(); opener.open.side_effect = failure
        with patch.dict(os.environ, {'FIXTURE_TOKEN': 'synthetic-private-token'}), self.assertRaises(core.ExternalFailure) as exc:
            core.Graph(config(), opener).request('GET', PAGE)
        self.assertIn('HTTP 400, code 10, error_subcode 42', str(exc.exception))
        self.assertNotIn('synthetic-private-token', str(exc.exception))
        self.assertNotIn('private-trace', str(exc.exception))

    def test_graph_errors_and_non_json_are_rejected(self):
        for payload in ({'error':{'message':'private'}}, [], {'id':'okay'}):
            opener = unittest.mock.Mock(); opener.open.return_value = self.response(payload)
            with patch.dict(os.environ, {'FIXTURE_TOKEN':'fixture'}):
                if payload == {'id':'okay'}: self.assertEqual(core.Graph(config(), opener).request('GET',PAGE), payload)
                else:
                    with self.assertRaises(core.ExternalFailure): core.Graph(config(),opener).request('GET',PAGE)

    def test_missing_credentials_do_not_contact_network(self):
        opener = unittest.mock.Mock()
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(core.Invalid): core.Graph(config(), opener).request('GET', PAGE)
        opener.open.assert_not_called()

    def test_redirects_invalid_json_and_oversized_responses_fail_closed(self):
        for payload in (b'not JSON', b'x' * 1_000_001):
            opener = unittest.mock.Mock(); response = self.response({}); response.read.return_value = payload
            opener.open.return_value = response
            with patch.dict(os.environ, {'FIXTURE_TOKEN':'fixture'}), self.assertRaises(core.ExternalFailure):
                core.Graph(config(),opener).request('GET', PAGE)
        response = self.response({},302); opener.open.return_value = response
        with patch.dict(os.environ, {'FIXTURE_TOKEN':'fixture'}), self.assertRaises(core.ExternalFailure):
            core.Graph(config(),opener).request('GET', PAGE)

    def test_live_verification_rejects_private_dns(self):
        with patch('facebook_connector.core.socket.getaddrinfo', return_value=[(2,1,6,'',('127.0.0.1',443))]), patch('facebook_connector.core.build_opener') as opener:
            with self.assertRaises(core.ExternalFailure): core.verify_live('https://example.com/a',HASH)
            opener.assert_not_called()

    def test_live_verification_checks_html_hash_and_redirect_policy(self):
        body = b'<html>fixture</html>'; response = self.response({})
        response.read.return_value = body; response.headers = {'Content-Type':'text/html; charset=utf-8'}
        opener = unittest.mock.Mock(); opener.open.return_value = response
        with patch('facebook_connector.core.socket.getaddrinfo', return_value=[(2,1,6,'',('93.184.216.34',443))]), patch('facebook_connector.core.build_opener',return_value=opener):
            self.assertTrue(core.verify_live('https://example.com/a',hashlib.sha256(body).hexdigest()))
            with self.assertRaises(core.ExternalFailure): core.verify_live('https://example.com/a',HASH)
        self.assertIsNone(core.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example'))

    def test_live_verification_dials_the_validated_address(self):
        body = b'<html>fixture</html>'; response = self.response({})
        response.read.return_value = body; response.headers = {'Content-Type':'text/html'}
        opener = unittest.mock.Mock(); opener.open.return_value = response
        with patch('facebook_connector.core.socket.getaddrinfo', return_value=[(2,1,6,'',('93.184.216.34',443))]), patch('facebook_connector.core.build_opener', return_value=opener) as build:
            self.assertTrue(core.verify_live('https://example.com/a', hashlib.sha256(body).hexdigest()))
        handler = build.call_args.args[1]
        self.assertIsInstance(handler, core.PinnedHTTPSHandler)
        self.assertEqual(handler.address, '93.184.216.34')

    def test_pinned_connection_dials_the_validated_address(self):
        with patch('facebook_connector.core.socket.create_connection') as create:
            create.return_value = unittest.mock.Mock()
            connection = core.pinned(HTTPSConnection, '93.184.216.34', True)('example.com')
            connection._context = unittest.mock.Mock()
            connection.connect()
            create.assert_called_once_with(('93.184.216.34', 443), connection.timeout, None)
            connection._context.wrap_socket.assert_called_once_with(create.return_value, server_hostname='example.com')


if __name__ == '__main__': unittest.main()
