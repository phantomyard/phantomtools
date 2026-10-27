import http.client
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class Upstream(BaseHTTPRequestHandler):
    received = None
    status = 200
    paths = []

    def log_message(self, *_):
        pass

    def do_GET(self):
        type(self).paths.append(self.path)
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        type(self).paths.append(self.path)
        type(self).received = (self.path, dict(self.headers), self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(type(self).status)
        if type(self).status == 302:
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/leak")
        self.end_headers()
        self.wfile.write(b'{"text":"transcribed"}')


class ApiTests(unittest.TestCase):
    def setUp(self):
        Upstream.status = 200
        Upstream.received = None
        Upstream.paths = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.gateway = server.Server(("127.0.0.1", 0), "local-token",
                                     f"http://127.0.0.1:{self.upstream.server_port}/v1", "upstream-token")
        for svc in (self.upstream, self.gateway):
            threading.Thread(target=svc.serve_forever, daemon=True).start()

    def tearDown(self):
        for svc in (self.gateway, self.upstream):
            svc.shutdown()
            svc.server_close()

    def request(self, path="speech", body=None, token="local-token", content_type="application/json"):
        if body is None:
            body = json.dumps({"input": "Hello there", "voice": "female"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=10)
        conn.request("POST", "/v1/audio/" + path, body, {
            "Authorization": "Bearer " + token, "Content-Type": content_type})
        response = conn.getresponse()
        result = response.status, response.getheader("Content-Type"), response.read()
        conn.close()
        return result

    def test_auth_and_routes(self):
        self.assertEqual(self.request(token="wrong")[0], 401)
        self.assertEqual(self.request(path="other")[0], 404)
        self.assertIsNone(Upstream.received)

    def test_transcription_preserves_bytes_and_replaces_credentials(self):
        body = b'--xyz\r\nContent-Disposition: form-data; name="model"\r\n\r\nwhisper-1\r\n--xyz--\r\n'
        self.assertEqual(self.request("transcriptions", body, content_type="multipart/form-data; boundary=xyz")[0], 200)
        path, headers, forwarded = Upstream.received
        self.assertEqual(path, "/v1/audio/transcriptions")
        self.assertEqual(headers["Authorization"], "Bearer upstream-token")
        self.assertEqual(headers["Content-Type"], "multipart/form-data; boundary=xyz")
        self.assertEqual(forwarded, body)

    def test_transcription_failure_and_redirect_are_not_exposed(self):
        for status in (401, 302, 500):
            Upstream.status = status
            code, _, body = self.request("transcriptions", b"--xyz--", content_type="multipart/form-data; boundary=xyz")
            self.assertEqual(code, 502)
            self.assertNotIn(b"transcribed", body)
        self.assertEqual(Upstream.paths, ["/v1/audio/transcriptions"] * 3)

    def test_validation(self):
        for data in ([], {"input": ""}, {"input": "hi", "voice": "../evil"},
                     {"input": "hi", "speed": True}, {"input": "hi", "speed": float("nan")},
                     {"input": "hi", "response_format": "pcm"}, {"input": "hi", "language": "--help"}):
            with self.subTest(data=data):
                self.assertEqual(self.request(body=json.dumps(data).encode())[0], 400)
        self.assertEqual(self.request(body=b"{")[0], 400)
        self.assertEqual(self.request(content_type="text/plain")[0], 415)
        self.assertEqual(self.request("transcriptions")[0], 415)

    def test_busy(self):
        self.gateway.slots.acquire()
        self.gateway.slots.acquire()
        try:
            self.assertEqual(self.request()[0], 503)
        finally:
            self.gateway.slots.release()
            self.gateway.slots.release()

    def test_oversized_body_rejected_before_read(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=5)
        conn.request("POST", "/v1/audio/speech", headers={"Authorization": "Bearer local-token", "Content-Length": str(server.MAX_BODY + 1)})
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()

    @patch("server.synthesize", side_effect=subprocess.TimeoutExpired("espeak-ng", 30))
    def test_process_timeout(self, _mock):
        self.assertEqual(self.request()[0], 502)

    @patch("server.synthesize", return_value=(b"audio", "audio/mpeg"))
    def test_speech_does_not_use_upstream(self, _mock):
        self.assertEqual(self.request(), (200, "audio/mpeg", b"audio"))
        self.assertIsNone(Upstream.received)

    @unittest.skipUnless(os.environ.get("ROBOT_AUDIO_SMOKE") == "1", "enable real audio dependencies")
    def test_real_multilingual_audio(self):
        texts = {"en": "Hello Andrew, the local voice service is ready for your message.",
                 "nl": "Goedemorgen, dit is een Nederlands bericht over het mooie weer vandaag.",
                 "de": "Guten Morgen, dies ist eine deutsche Nachricht über das schöne Wetter heute."}
        for lang, text in texts.items():
            self.assertEqual(server.language(text), lang)
            for voice in ("male", "female"):
                for fmt, magic in (("mp3", b"ID3"), ("opus", b"OggS"), ("wav", b"RIFF")):
                    with self.subTest(lang=lang, voice=voice, fmt=fmt):
                        status, _, audio = self.request(body=json.dumps({"input": text, "voice": voice, "response_format": fmt}).encode())
                        self.assertEqual(status, 200)
                        self.assertTrue(audio.startswith(magic))
                        probe = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "null", "-"], input=audio, capture_output=True)
                        self.assertEqual(probe.returncode, 0, probe.stderr)


class SynthesisTests(unittest.TestCase):
    def test_unsupported_language_falls_back_without_shell(self):
        with patch("server.subprocess.run") as run:
            run.side_effect = [subprocess.CalledProcessError(1, "espeak-ng"),
                               subprocess.CompletedProcess([], 0, b"wav"),
                               subprocess.CompletedProcess([], 0, b"audio")]
            self.assertEqual(server.synthesize({"input": "$(touch /bad)", "language": "zz"})[0], b"audio")
            self.assertEqual(run.call_args_list[1].args[0][3], "en+f3")
            self.assertEqual(run.call_args_list[0].kwargs["input"], b"$(touch /bad)")

    def test_invalid_upstream_and_missing_secrets(self):
        for url in ("http://example.com/v1", "https://u:p@example.com/v1", "https://example.com/v1?q=x", ""):
            with self.assertRaises(ValueError):
                server.Server(("127.0.0.1", 0), "token", url, "key")
        with self.assertRaises(ValueError):
            server.Server(("127.0.0.1", 0), "", "https://example.com/v1", "key")


if __name__ == "__main__":
    unittest.main()
