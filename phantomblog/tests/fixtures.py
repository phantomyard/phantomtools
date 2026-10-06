"""Clearly labelled synthetic content; never installed into a live publication."""
from pathlib import Path
import struct
import zlib

from phantomblog import core


def png():
    def chunk(name, data):
        return struct.pack("!I", len(data)) + name + data + struct.pack("!I", zlib.crc32(name + data))
    width, height = 1200, 630
    raw = b"".join(b"\x00" + bytes([114, 83, 204]) * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def create(root, count=1):
    core.initialize(root)
    core.atomic(root / "assets" / "fixture.png", png())
    model, _ = core.load(root)
    model["site"]["name"] = "PhantomBlog test fixture"
    model["site"]["url"] = "https://example.invalid/fixture/"
    for i in range(count):
        def translation(lang):
            return {"title": f"{'Test fixture' if lang == 'en' else 'Artículo de prueba'} {i + 1}", "summary": "Synthetic content for verification only." if lang == 'en' else "Contenido sintético solo para verificación.", "description": "Private test fixture, not a published claim." if lang == 'en' else "Prueba privada, no es una afirmación publicada.", "imageAlt": "Purple fixture image" if lang == 'en' else "Imagen morada de prueba", "body": '<h2 id="fixture">' + ("Verification fixture" if lang == "en" else "Prueba de verificación") + '</h2><p>HTML &amp; CSS test only.</p><table><caption>Fixture table</caption><thead><tr><th scope="col">Column A</th><th scope="col">Column B</th><th scope="col">Column C</th><th scope="col">Column D</th></tr></thead><tbody><tr><td>One</td><td>Two</td><td>Three</td><td>Four</td></tr></tbody></table>'}
        model["articles"].append({"slug": f"fixture-{i:03}", "category": "guides", "status": "published", "mode": "generated", "publishedAt": "2026-10-06", "featured": i == 2, "image": "fixture.png", "socialImage": "fixture.png", "en": translation("en"), "es": translation("es"), "design": {}})
    core.atomic(root / "blog.json", core.encoded(model))
    return model


def adapter(capability="share", provider="facebook"):
    return {"provider": provider, "persona": "synthetic-editor", "server": "synthetic-mcp", "capabilities": {capability: {"tool": "fixture_" + capability, "arguments": {"text": "${text}", "url": "${url}", "request": "${request}", "idempotency_key": "${idempotency_key}"}, "successField": "ok", "receiptField": "id"}}}
