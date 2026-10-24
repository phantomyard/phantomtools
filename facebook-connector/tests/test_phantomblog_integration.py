"""Exercise both real tools with temporary source and a synthetic Graph transport."""
import sys
from pathlib import Path
import tempfile
import unittest

from facebook_connector import core as facebook, mcp

BLOG = Path(__file__).resolve().parents[2] / 'phantomblog'


@unittest.skipUnless(BLOG.is_dir(), 'Optional sibling PhantomBlog checkout not present')
class PhantomBlogIntegrationTests(unittest.TestCase):
    def test_both_languages_flow_through_mapping_and_return_public_receipts(self):
        sys.path.insert(0, str(BLOG)); sys.path.insert(0, str(BLOG / 'tests'))
        from phantomblog import core, connectors
        from fixtures import create
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); blog = root / 'blog'; blog.mkdir()
            model = create(blog, 1)
            config = root / 'facebook.json'
            facebook.atomic(config, {'version':1, 'pageId':'1234567890', 'apiVersion':'v22.0', 'tokenEnv':'UNUSED_FIXTURE_TOKEN',
                                     'allowPublishing':True, 'articleOrigins':['https://example.invalid']})
            sent, verified = [], []
            class Graph:
                def request(self, method, endpoint, parameters):
                    if method == 'GET': return {'id':'1234567890', 'name':'Fixture Page'}
                    sent.append(parameters)
                    return {'id':'1234567890_' + str(len(sent))}
            def verify(url, checksum):
                verified.append((url, checksum)); return True
            connector = facebook.Connector(config, lambda _: Graph(), verify)
            class Runner:
                def call(self, connection, spec, arguments):
                    return {'structuredContent':mcp.call(connector, spec['tool'], arguments)}
                def capture(self, persona, text): return True
            model['connections'] = {'facebook':{'provider':'facebook','persona':'fixture-editor','server':'fixture-facebook','capabilities':{'share':{
                'tool':'facebook_publish_article', 'arguments':{'url':'${url}','message':'${title}','contentHash':'${content_hash}','idempotencyKey':'${idempotency_key}'},
                'successField':'ok','receiptField':'postId'}}}}
            core.atomic(blog / 'blog.json', core.encoded(model))
            for language in ('en','es'):
                plan = connectors.prepare(blog,'fixture-000',language,None,['facebook'])
                result = connectors.execute(blog,plan,plan['approval'],Runner(),lambda *_:{'liveVerified':True})
                self.assertEqual(result['shares']['facebook']['state'],'complete')
                self.assertFalse(result['errors'])
                self.assertEqual(verified[-1],(plan['url'],plan['contentHash']))
                self.assertEqual(sent[-1],{'message':model['articles'][0][language]['title'],'link':plan['url']})
            self.assertEqual(len(sent),2)
            connectors.execute(blog,plan,plan['approval'],Runner(),lambda *_:{'liveVerified':True})
            self.assertEqual(len(sent),2)
