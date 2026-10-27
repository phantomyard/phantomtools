import http.client
import io
import json
import os
import socket
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
    oversized = False

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
        if type(self).oversized:
            self.wfile.write(b"x" * (server.MAX_BODY + 2))
        else:
            self.wfile.write(b'{"text":"transcribed"}')


class ApiTests(unittest.TestCase):
    def setUp(self):
        Upstream.status = 200
        Upstream.received = None
        Upstream.paths = []
        Upstream.oversized = False
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

    def test_voice_rejection_enumerates_supported_voices(self):
        # phantombot's fetchOpenAIVoiceOptions() probes with an invalid voice and
        # parses `Supported values are: 'a', 'b'` out of the 400 body, so the
        # enumeration is the contract that makes the voice picker work.
        for voice in ("__phantombot_probe__", "nova"):
            with self.subTest(voice=voice):
                code, _, body = self.request(body=json.dumps({"input": ".", "voice": voice}).encode())
                self.assertEqual(code, 400)
                message = json.loads(body)["error"]["message"]
                self.assertRegex(message, r"Supported values are: 'female', 'male'$")

    def test_duplicate_authorization_header_rejected(self):
        # Two Authorization headers must not authenticate: reading only the
        # first (or joining them) would let a proxy smuggle a second value.
        with socket.create_connection(("127.0.0.1", self.gateway.server_port), timeout=5) as sock:
            sock.sendall(
                b"POST /v1/audio/speech HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\nContent-Length: 2\r\n"
                b"Authorization: Bearer local-token\r\n"
                b"Authorization: Bearer local-token\r\n\r\n{}"
            )
            status_line = sock.recv(4096)
        self.assertIn(b" 401 ", status_line)
        self.assertIsNone(Upstream.received)

    def test_oversized_upstream_response_is_rejected(self):
        Upstream.oversized = True
        with patch("server.MAX_BODY", 4096):
            code, _, body = self.request("transcriptions", b"--xyz--", content_type="multipart/form-data; boundary=xyz")
        self.assertEqual(code, 502)
        self.assertIn(b"upstream response too large", body)
        self.assertNotIn(b"xxxx", body)

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


class ReplyGuardTests(unittest.TestCase):
    def test_second_response_write_is_ignored(self):
        # A write that fails mid-body (client hung up) must not emit a second
        # status line into an already-open response.
        handler = server.Handler.__new__(server.Handler)
        handler.response_started = False
        handler._headers_buffer = []
        handler.send_response = lambda status: handler._headers_buffer.append(status)
        handler.send_header = lambda *_args: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        handler.close_connection = False
        handler.reply(200, b"first")
        handler.reply(502, b"second")
        self.assertEqual(handler._headers_buffer, [200])
        self.assertEqual(handler.wfile.getvalue(), b"first")
        self.assertTrue(handler.close_connection)


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
