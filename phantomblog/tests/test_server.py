import http.client
import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from phantomblog import core, server, mcp
from fixtures import create


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.model=create(self.root)
        self.token='synthetic-test-token-never-production'
        self.httpd=server.make_server(self.root,self.token,0)
        self.thread=threading.Thread(target=self.httpd.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)
        self.port=self.httpd.server_port

    def stop(self):
        self.httpd.shutdown();self.httpd.server_close();self.thread.join(timeout=3)

    def request(self,path,body=None,auth=True,origin=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        headers={}
        if auth:headers['Authorization']='Bearer '+self.token
        if body is not None:headers['Content-Type']='application/json'
        if origin:headers['Origin']=origin
        connection.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,headers)
        response=connection.getresponse();data=response.read();status=response.status;headers=dict(response.getheaders());connection.close()
        return status,data,headers

    def test_authentication_and_origin_boundary(self):
        self.assertEqual(self.request('/api/state',auth=False)[0],401)
        self.assertEqual(self.request('/api/state')[0],200)
        self.assertEqual(self.request('/api/state',origin='https://evil.invalid')[0],403)
        self.assertEqual(self.request('/api/save',{},auth=False)[0],401)
        self.assertEqual(self.request('/api/login',{'token':'wrong'},auth=False)[0],401)

    def test_login_cookie_is_httponly(self):
        status,_,headers=self.request('/api/login',{'token':self.token},auth=False)
        self.assertEqual(status,200);self.assertIn('HttpOnly',headers['Set-Cookie']);self.assertIn('SameSite=Strict',headers['Set-Cookie'])

    def test_no_source_files_served_and_preview_is_sandboxable(self):
        self.assertEqual(self.request('/blog.json')[0],404)
        self.assertEqual(self.request('/../blog.json')[0],404)
        status,data,headers=self.request('/preview?page=blog-fixture-000-es.html')
        self.assertEqual(status,200);self.assertIn(b'<html lang="es">',data)
        self.assertIn("frame-ancestors 'self'",headers['Content-Security-Policy'])
        self.assertIn("default-src 'none'",headers['Content-Security-Policy'])

    def test_save_and_stale_edit(self):
        model,revision=core.load(self.root);model['site']['name']='Saved in dashboard'
        self.assertEqual(self.request('/api/save',{'model':model,'revision':revision})[0],200)
        self.assertEqual(self.request('/api/save',{'model':model,'revision':revision})[0],409)

    def test_invalid_template_preserves_file(self):
        model,revision=core.load(self.root);path=self.root/'templates/page.html';before=path.read_bytes()
        status,_,_=self.request('/api/template',{'revision':revision,'hash':core.digest(before),'text':'<script>oops</script>'})
        self.assertEqual(status,422);self.assertEqual(path.read_bytes(),before)

    def test_image_upload_validates_and_preserves_existing(self):
        source=(self.root/'assets/fixture.png').read_bytes()
        data={'name':'new-image.png','base64':base64.b64encode(source).decode()}
        self.assertEqual(self.request('/api/asset',data)[0],200)
        data['base64']=base64.b64encode(source+b'changed').decode()
        self.assertEqual(self.request('/api/asset',data)[0],409)
        self.assertEqual((self.root/'assets/new-image.png').read_bytes(),source)
        data['name']='../bad.png';self.assertEqual(self.request('/api/asset',data)[0],422)

    def test_live_verifier_checks_exact_revision(self):
        from phantomblog import connectors
        # Separate synthetic HTML server; no external network or credentials.
        from http.server import HTTPServer, BaseHTTPRequestHandler
        body=b'<html><body>verified fixture</body></html>'
        class PublicHandler(BaseHTTPRequestHandler):
            def do_GET(handler):
                if handler.path=='/redirect':
                    handler.send_response(302);handler.send_header('Location','/page');handler.end_headers();return
                handler.send_response(200);handler.send_header('Content-Type','text/html');handler.end_headers();handler.wfile.write(body)
            def log_message(handler,*args):pass
        public=HTTPServer(('127.0.0.1',0),PublicHandler)
        thread=threading.Thread(target=public.serve_forever,daemon=True);thread.start()
        try:
            url=f'http://127.0.0.1:{public.server_port}/page'
            self.assertTrue(connectors.verify_live(url,core.digest(body))['liveVerified'])
            with self.assertRaises(connectors.ExternalFailure):connectors.verify_live(url,'0'*64)
            with self.assertRaises(connectors.ExternalFailure):connectors.verify_live(url.replace('/page','/redirect'),core.digest(body))
        finally:
            public.shutdown();public.server_close();thread.join()

    def test_agent_proposal_review_and_apply(self):
        model,revision=core.load(self.root);model['site']['name']='Reviewed agent change'
        mcp.call(self.root,'phantomblog_propose',{'model':model,'revision':revision})
        _,data,_=self.request('/api/proposal');proposal=json.loads(data)
        self.assertEqual(core.load(self.root)[0]['site']['name'],'PhantomBlog test fixture')
        self.assertEqual(self.request('/api/apply-proposal',{'proposalHash':proposal['proposalHash']})[0],200)
        self.assertEqual(core.load(self.root)[0]['site']['name'],'Reviewed agent change')

    def test_external_execution_requires_reviewed_plan(self):
        self.assertEqual(self.request('/api/execute',{'approval':'invented'})[0],422)

    def test_readonly_commands_do_not_create_output(self):
        self.request('/api/build',{'mode':'dry-run'})
        self.request('/api/build',{'mode':'check'})
        self.assertFalse((self.root/'public').exists())

    def test_token_references_fail_closed(self):
        with patch.dict(os.environ,{'FIXTURE_ADMIN':'x'*32}):
            self.assertEqual(server.token_from_reference('env:FIXTURE_ADMIN',None),'x'*32)
        for reference in ('plaintext-token','env:UNSET_FIXTURE','vault:name'):
            with self.assertRaises(core.Invalid):
                server.token_from_reference(reference,None)


if __name__ == '__main__':
    unittest.main()
