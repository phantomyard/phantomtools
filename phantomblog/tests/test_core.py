from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from phantomblog import core, mcp
from fixtures import create


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = create(self.root)

    def write(self, model=None):
        core.atomic(self.root / "blog.json", core.encoded(model or self.model))

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_determinism_and_readonly(self):
        before = self.snapshot()
        core.build(self.root, "check")
        core.build(self.root, "dry-run")
        self.assertEqual(before, self.snapshot())
        core.build(self.root)
        before = self.snapshot()
        self.assertEqual(core.build(self.root)["changed"], [])
        self.assertEqual(core.build(self.root, "check")["changed"], [])
        self.assertEqual(before, self.snapshot())

    def test_invalid_preserves_every_output(self):
        core.build(self.root)
        self.model["articles"][0]["es"]["title"] = ""
        self.write()
        before = self.snapshot()
        with self.assertRaises(core.Invalid):
            core.build(self.root)
        self.assertEqual(before, self.snapshot())

    def test_catalogue_validation(self):
        changes = [lambda m: m["articles"].append(deepcopy(m["articles"][0])), lambda m: m["articles"][0].update(slug="../bad"), lambda m: m["articles"][0].update(publishedAt="2026-02-30"), lambda m: m["articles"][0].update(updatedAt="2025-01-01"), lambda m: m["articles"][0].update(category="missing"), lambda m: m["articles"][0].update(featured="yes"), lambda m: m["site"].update(url="javascript:bad"), lambda m: m["articles"][0].update(image="../secret.png"), lambda m: m["articles"][0]["es"].update(body=""), lambda m: m["theme"].update(sections=["body", "body", "lead"])]
        for mutate in changes:
            with self.subTest(mutate=mutate):
                model = deepcopy(self.model)
                mutate(model)
                with self.assertRaises(core.Invalid):
                    core.render(self.root, model)

    def test_fragments_reject_active_content_and_bad_structure(self):
        for body in ('<script>alert(1)</script>', '<p onclick="evil()">bad</p>', '<a href="javascript:evil()">bad</a>', '<p>open', '<img src="fixture.png">', '<a href="#missing">bad</a>', '<a href="https://example.invalid" target="_blank">bad</a>', '<p id="x" id="x">bad</p>'):
            with self.subTest(body=body), self.assertRaises(core.Invalid):
                core.Fragment(self.root, set()).validate(body)

    def test_metadata_escaping(self):
        self.model["articles"][0]["en"]["title"] = '<img src=x> & "quoted"'
        outputs = core.render(self.root, self.model)
        page = outputs["blog-fixture-000.html"].decode()
        self.assertIn('&lt;img src=x&gt; &amp; &quot;quoted&quot;', page)
        self.assertNotIn('<img src=x>', page)

    def test_asset_and_body_sources(self):
        a = self.model["articles"][0]
        core.atomic(self.root / "bodies/en/fixture.html", b'<p>Supplied English</p><img src="extra.png" alt="Extra image">')
        core.atomic(self.root / "assets/extra.png", (self.root / "assets/fixture.png").read_bytes())
        a["en"].pop("body")
        a["en"]["bodyFile"] = "en/fixture.html"
        outputs = core.render(self.root, self.model)
        self.assertIn(b"Supplied English", outputs["blog-fixture-000.html"])
        self.assertIn("extra.png", outputs)

    def test_social_raster_required(self):
        core.atomic(self.root / "assets/not.png", b"not a raster")
        self.model["articles"][0]["socialImage"] = "not.png"
        with self.assertRaises(core.Invalid):
            core.render(self.root, self.model)

    def test_output_ownership(self):
        core.atomic(self.root / "public/blog.html", b"unrelated page")
        with self.assertRaises(core.Invalid):
            core.build(self.root)
        self.assertEqual((self.root / "public/blog.html").read_bytes(), b"unrelated page")

    def test_edited_generated_file_refused(self):
        core.build(self.root)
        core.atomic(self.root / "public/blog.html", b"manual edits")
        with self.assertRaises(core.Invalid):
            core.build(self.root)

    def test_recover_interrupted_build_requires_exact_rendered_bytes(self):
        core.build(self.root);self.model['site']['name']='Updated after initial build';self.write()
        outputs=core.render(self.root,self.model)
        core.atomic(self.root/'public/blog.html',outputs['blog.html'])
        with self.assertRaises(core.Invalid):core.build(self.root)
        core.build(self.root,recover=True)
        self.assertEqual(core.build(self.root,'check')['changed'],[])
        core.atomic(self.root/'public/blog.html',b'unknown manual edits')
        with self.assertRaises(core.Invalid):core.build(self.root,recover=True)

    def test_draft_assets_are_not_public(self):
        self.model['articles'][0]['status']='draft'
        self.assertNotIn('fixture.png',core.render(self.root,self.model))
        self.assertIn('fixture.png',core.render(self.root,self.model,True))

    def test_manifest_cannot_claim_legacy_page_even_with_correct_hash(self):
        data=b'Legacy page bytes';core.atomic(self.root/'public/original.html',data)
        core.atomic(self.root/'public/phantomblog-manifest.json',core.encoded({'generator':'phantomblog','version':1,'files':{'original.html':core.digest(data)}}))
        with self.assertRaises(core.Invalid):core.build(self.root)
        self.assertEqual((self.root/'public/original.html').read_bytes(),data)

    def test_manifest_malformed_and_unsafe_paths(self):
        for value in ({"generator": "other", "version": 1, "files": {}}, {"generator": "phantomblog", "version": 1, "files": {"../bad": "a" * 64}}):
            core.atomic(self.root / "public/phantomblog-manifest.json", core.encoded(value))
            with self.assertRaises(core.Invalid):
                core.build(self.root)

    def test_legacy_preserved_and_urls_retained(self):
        core.atomic(self.root / "public/original.html", b"exact original bytes\r\n")
        a = self.model["articles"][0]
        a["mode"] = "legacy"
        a["en"]["url"] = "original.html"
        a["es"]["url"] = "original.html?lang=es"
        self.write()
        core.build(self.root)
        self.assertEqual((self.root / "public/original.html").read_bytes(), b"exact original bytes\r\n")
        self.assertIn(b"original.html?lang=es", (self.root / "public/blog-es.html").read_bytes())
        self.assertFalse((self.root / "public/blog-fixture-000.html").exists())

    def test_pagination_boundaries_and_order(self):
        for count in (0, 1, 12, 13, 25):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                model = create(root, count)
                model["articles"].reverse()
                core.atomic(root / "blog.json", core.encoded(model))
                outputs = core.render(root, model)
                pages = max(1, (count + 11) // 12)
                for lang in ("en", "es"):
                    self.assertEqual(len([n for n in outputs if n == core.archive_url(lang) or n.startswith('blog' + ('-es' if lang=='es' else '') + '-page-')]), pages)
                if count > 2:
                    archive = outputs["blog.html"].decode()
                    self.assertLess(archive.index('href="blog-fixture-001.html"'), archive.index('href="blog-fixture-002.html"'))
                    self.assertEqual(archive.count('class="blog-card featured"'), 1)

    def test_date_order_before_slug(self):
        b = deepcopy(self.model["articles"][0]); b.update(slug="aaa-newer", publishedAt="2026-10-07")
        self.model["articles"].append(b)
        archive = core.render(self.root, self.model)["blog.html"].decode()
        self.assertLess(archive.index('href="blog-aaa-newer.html"'), archive.index('href="blog-fixture-000.html"'))

    def test_prune_only_numbered_archives(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); model = create(root,13)
            core.build(root)
            core.atomic(root / "public/unrelated.html", b"unrelated")
            model["articles"] = model["articles"][:1]
            core.atomic(root / "blog.json", core.encoded(model))
            result = core.build(root)
            self.assertEqual(result["deleted"], ["blog-es-page-2.html", "blog-page-2.html"])
            self.assertTrue((root / "public/blog-fixture-012.html").exists())
            self.assertEqual((root / "public/unrelated.html").read_bytes(), b"unrelated")

    def test_draft_not_public(self):
        self.model["articles"][0]["status"] = "draft"
        self.assertNotIn("blog-fixture-000.html", core.render(self.root, self.model))
        self.assertIn("blog-fixture-000.html", core.render(self.root, self.model, True))

    def test_body_links_cannot_address_draft_only_outputs(self):
        draft=deepcopy(self.model['articles'][0]);draft.update(slug='draft-only',status='draft');self.model['articles'].append(draft)
        self.model['articles'][0]['en']['body']='<p><a href="blog.html">Archive</a></p>'
        core.render(self.root,self.model)
        self.model['articles'][0]['en']['body']='<p><a href="blog-draft-only.html">Not published</a></p>'
        with self.assertRaises(core.Invalid):core.render(self.root,self.model)

    def test_symlink_component_is_not_owned(self):
        with tempfile.TemporaryDirectory() as outside:
            target=self.root/'linked'
            try:target.symlink_to(outside,target_is_directory=True)
            except OSError:self.skipTest('This host does not permit creating symlink fixtures')
            with self.assertRaises(core.Invalid):core.contained(self.root,'linked/file.html')

    def test_save_conflict_and_public_slug_protection(self):
        _, revision = core.load(self.root)
        self.model["site"]["name"] = "New name"
        core.save(self.root, self.model, revision)
        with self.assertRaises(core.Conflict):
            core.save(self.root, self.model, revision)
        model, current = core.load(self.root); model["articles"] = []
        with self.assertRaises(core.Invalid):
            core.save(self.root, model, current)

    def test_css_scope_and_rejections(self):
        self.assertEqual(core.scoped_css("h2, p { color: #abc; }", ".article"), ".article h2, .article p { color: #abc; }")
        for css in ('body {color:red}', '@import "bad";', 'p { background:url(https://bad); }', 'p{color:red', 'h2{color:red}</style>'):
            with self.subTest(css=css), self.assertRaises(core.Invalid):
                core.scoped_css(css, '.article')

    def test_template_validation(self):
        core.validate_template((core.RESOURCES / "page.html").read_text())
        for template in ('<html>$head$content</html>', '$lang $head $header $content $footer <script>evil</script>'):
            with self.assertRaises(core.Invalid):
                core.validate_template(template)

    def test_per_article_template_override(self):
        source=(core.RESOURCES/'page.html').read_text().replace('$content','<div class="special-structure">$content</div>')
        core.atomic(self.root/'templates/special.html',source.encode())
        self.model['articles'][0]['design']['templateFile']='special.html'
        outputs=core.render(self.root,self.model)
        self.assertIn(b'special-structure',outputs['blog-fixture-000.html'])
        self.assertNotIn(b'special-structure',outputs['blog.html'])
        self.model['articles'][0]['design']['templateFile']='../outside.html'
        with self.assertRaises(core.Invalid):core.render(self.root,self.model)

    def test_reciprocal_language_and_share_metadata(self):
        outputs = core.render(self.root,self.model)
        en, es = outputs["blog-fixture-000.html"].decode(), outputs["blog-fixture-000-es.html"].decode()
        self.assertIn('hreflang="es" href="https://example.invalid/fixture/blog-fixture-000-es.html"',en)
        self.assertIn('hreflang="en" href="https://example.invalid/fixture/blog-fixture-000.html"',es)
        self.assertIn('blog-fixture-000-es.html',es.split('facebook.com')[1])
        self.assertIn('og:image',es)

    def test_duplicate_json_key(self):
        with self.assertRaises(core.Invalid):
            core.decode('{"x":1,"x":2}')

    def test_mcp_proposal_does_not_apply(self):
        _, revision = core.load(self.root)
        self.model["site"]["name"] = "Agent proposal"
        result = mcp.call(self.root,"phantomblog_propose",{"model":self.model,"revision":revision})
        self.assertFalse(result["applied"])
        self.assertNotEqual(core.load(self.root)[0]["site"]["name"],"Agent proposal")

    def test_mcp_protocol_and_no_external_publish_tool(self):
        requests = [{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}},{"jsonrpc":"2.0","method":"notifications/initialized"},{"jsonrpc":"2.0","id":2,"method":"tools/list"},{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"phantomblog_snapshot","arguments":{}}}]
        out = io.StringIO();mcp.serve(self.root,io.StringIO('\n'.join(json.dumps(r) for r in requests)+'\n'),out)
        responses=[json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(len(responses),3)
        self.assertEqual(responses[0]["result"]["protocolVersion"],"2025-03-26")
        self.assertFalse(any('execute' in t['name'] for t in responses[1]['result']['tools']))
        invalid = io.StringIO()
        mcp.serve(self.root, io.StringIO('{"jsonrpc":"2.0","id":4,"method":"initialize","params":[]}\n'), invalid)
        self.assertEqual(json.loads(invalid.getvalue())['error']['code'], -32600)

    def test_cli_from_different_directory_and_exit_codes(self):
        wrapper=Path(__file__).resolve().parents[1]/"bin/phantomblog"
        def run(command):
            return subprocess.run([sys.executable,str(wrapper),'--root',str(self.root),'--json',command],cwd=self.root.parent,capture_output=True,text=True)
        self.assertEqual(run('check').returncode,1)
        self.assertEqual(run('dry-run').returncode,0)
        self.assertEqual(run('build').returncode,0)
        self.assertEqual(run('check').returncode,0)
        self.model['articles'][0]['publishedAt']='bad';self.write()
        result=run('build');self.assertEqual(result.returncode,2);self.assertIn('error',json.loads(result.stdout))


if __name__ == '__main__':
    unittest.main()
