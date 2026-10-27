#!/usr/bin/env python3
"""Authenticated local OpenAI-compatible speech and transcription gateway."""
import hmac
import json
import math
import os
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from langdetect import DetectorFactory, LangDetectException, detect_langs

DetectorFactory.seed = 0
LANGUAGE_LOCK = threading.Lock()
MAX_BODY = 25 * 1024 * 1024
FORMATS = {"mp3": ("mp3", "audio/mpeg"), "opus": ("ogg", "audio/ogg"),
           "wav": ("wav", "audio/wav")}


class InvalidRequest(ValueError):
    """A validation failure whose message is safe to return to the caller.

    The message is authored here (never derived from request content), so it can
    be surfaced verbatim: this is how an OpenAI-compatible client discovers the
    supported voice names from a deliberately invalid probe.
    """


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the upstream credential to another URL.


def language(text):
    try:
        with LANGUAGE_LOCK:
            candidates = detect_langs(text)
        if candidates and candidates[0].prob >= 0.8:
            return candidates[0].lang
    except LangDetectException:
        pass
    return "en"


def synthesize(data):
    text = data.get("input")
    if not isinstance(text, str) or not text.strip() or len(text) > 10000:
        raise InvalidRequest("input must contain 1–10000 characters")
    voice = data.get("voice", "female")
    if voice not in ("female", "male"):
        # Clients read the supported list out of this rejection, so it must be
        # worded exactly like the OpenAI validation error it replaces.
        raise InvalidRequest("invalid voice. Supported values are: 'female', 'male'")
    fmt = data.get("response_format", "mp3")
    if fmt not in FORMATS:
        raise InvalidRequest("invalid response_format. Supported values are: 'mp3', 'opus', 'wav'")
    speed = data.get("speed", 1)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or not 0.25 <= speed <= 4:
        raise InvalidRequest("speed must be between 0.25 and 4")
    lang = data.get("language") or language(text)
    if not isinstance(lang, str) or not re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2,8})?", lang):
        raise InvalidRequest("invalid language")
    variant, rate, pitch = ("f3", 145, 55) if voice == "female" else ("m3", 130, 30)
    def speak(selected_language):
        # Keep the executable and option positions literal; text travels on stdin.
        return subprocess.run(
            ["espeak-ng", "--stdout", "-v", f"{selected_language}+{variant}",
             "-s", str(round(rate * speed)), "-p", str(pitch), "--stdin"],
            input=text.encode(), capture_output=True, timeout=30, check=True,
        ).stdout

    try:
        wav = speak(lang)
    except subprocess.CalledProcessError:
        if lang == "en":
            raise
        wav = speak("en")
    container, mime = FORMATS[fmt]
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0"]
    if fmt == "opus":
        args += ["-c:a", "libopus"]
    args += ["-f", container, "pipe:1"]
    audio = subprocess.run(args, input=wav, capture_output=True, timeout=30, check=True).stdout
    if not audio:
        raise RuntimeError("empty audio")
    return audio, mime


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, token, upstream, upstream_key):
        if not token or not upstream_key:
            raise ValueError("ROBOT_VOICE_TOKEN and ROBOT_STT_API_KEY are required")
        parsed = urlsplit(upstream)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"))):
            raise ValueError("STT URL must be HTTPS, or HTTP on loopback, without credentials/query/fragment")
        self.token, self.upstream_key = token, upstream_key
        self.upstream = upstream.rstrip("/") + "/audio/transcriptions"
        self.slots = threading.BoundedSemaphore(2)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.response_started = False
        self.connection.settimeout(30)

    def log_message(self, *_args):
        pass  # No request content, credentials or upstream error bodies in logs.

    def release_slot(self):
        if getattr(self, "processing", False):
            self.processing = False
            self.server.slots.release()

    def reply(self, status, body, mime="application/json"):
        # Processing is finished before the response becomes visible to a caller.
        self.release_slot()
        if self.response_started:
            # A write already began (e.g. the client disconnected mid-body): a
            # second status line would corrupt the response, so just hang up.
            self.close_connection = True
            return
        self.response_started = True
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def error(self, status, message):
        self.reply(status, json.dumps({"error": {"message": message}}).encode())

    def do_POST(self):
        expected = "Bearer " + self.server.token
        provided = self.headers.get_all("Authorization", [])
        if len(provided) != 1 or not hmac.compare_digest(provided[0].encode(), expected.encode()):
            return self.error(401, "invalid bearer token")
        if self.path not in ("/v1/audio/speech", "/v1/audio/transcriptions"):
            return self.error(404, "unknown endpoint")
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
            return self.error(400, "one Content-Length required; chunked requests unsupported")
        try:
            length = int(self.headers.get("Content-Length", ""))
            if not 0 < length <= MAX_BODY:
                return self.error(413, "body exceeds limit or is empty")
        except ValueError:
            return self.error(400, "invalid Content-Length")
        if not self.server.slots.acquire(blocking=False):
            return self.error(503, "service busy; retry later")
        self.processing = True
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                return self.error(400, "incomplete request body")
            if self.path.endswith("/speech"):
                if self.headers.get_content_type() != "application/json":
                    return self.error(415, "application/json required")
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise InvalidRequest("JSON object required")
                audio, mime = synthesize(data)
                self.reply(200, audio, mime)
            else:
                content_type = self.headers.get("Content-Type", "")
                if self.headers.get_content_type() != "multipart/form-data" or not self.headers.get_param("boundary"):
                    return self.error(415, "multipart/form-data with boundary required")
                request = Request(self.server.upstream, data=body, headers={
                    "Content-Type": content_type,
                    "Authorization": "Bearer " + self.server.upstream_key,
                }, method="POST")
                try:
                    with build_opener(NoRedirect).open(request, timeout=45) as response:
                        result = response.read(MAX_BODY + 1)
                        if len(result) > MAX_BODY:
                            return self.error(502, "upstream response too large")
                        self.reply(response.status, result, response.headers.get("Content-Type", "application/json"))
                except HTTPError as exc:
                    exc.close()
                    self.error(502, "transcription upstream rejected request")
                except (URLError, TimeoutError):
                    self.error(502, "transcription upstream unavailable")
        except InvalidRequest as exc:
            self.error(400, str(exc))
        except (ValueError, TypeError):
            self.error(400, "invalid speech request")
        except (subprocess.SubprocessError, OSError, RuntimeError):
            self.error(502, "audio processing failed; check dependencies and upstream availability")
        finally:
            self.release_slot()


def main():
    for binary in ("espeak-ng", "ffmpeg"):
        if not shutil.which(binary):
            raise SystemExit(f"Missing required binary: {binary}")
    try:
        server = Server(("127.0.0.1", int(os.environ.get("ROBOT_VOICE_PORT", "8787"))),
                        os.environ.get("ROBOT_VOICE_TOKEN", ""),
                        os.environ.get("ROBOT_STT_BASE_URL", ""),
                        os.environ.get("ROBOT_STT_API_KEY", ""))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print("Robot voice listening on loopback", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
