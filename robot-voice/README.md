# Robot voice

Standalone local robot speech using espeak-ng, exposed through the existing
OpenAI-compatible audio API. No phantombot core changes. Incoming recordings are
forwarded unchanged to an OpenAI-compatible transcription provider; speech text
stays local. Transcription still has the upstream provider’s costs and privacy policy.
This is **not** an ElevenLabs transcription adapter.

## Run (Linux, Python 3.10+)

```sh
sudo apt-get install espeak-ng ffmpeg python3-venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Store `ROBOT_VOICE_TOKEN` (a new random bearer token), `ROBOT_STT_API_KEY`
(the existing transcription credential), and `ROBOT_STT_BASE_URL` (e.g.
`https://api.openai.com/v1`) in the phantombot vault. Inject them into the
service environment through your supervisor; do not put credentials in unit
files or shell history. The service requires all three. Local STT servers may
use HTTP on loopback and a nonempty placeholder key if authentication is unused.

```sh
.venv/bin/python server.py
```

Binds **127.0.0.1:8787** only; override the port with `ROBOT_VOICE_PORT`.
Run on the same host as phantombot. It reads no persona files, writes no runtime
config, and installs no daemon automatically. Stop with Ctrl-C or the supervisor.

In phantombot’s existing voice setup, select `openai-compatible`, set the base
URL to `http://127.0.0.1:8787/v1`, supply `ROBOT_VOICE_TOKEN` as that endpoint’s
API key, select `female` or `male` as voice, any TTS model name (ignored), and
your upstream’s supported STT model (forwarded unchanged). Preserve the existing
config and restore its endpoint/key to roll back. Replacing an ElevenLabs setup
requires choosing a compatible STT upstream first.

## API and language

Both endpoints require `Authorization: Bearer <ROBOT_VOICE_TOKEN>`:

- `POST /v1/audio/speech`: JSON `input` (1–10000 characters), `voice`
  (`female` default, or `male`), `speed` (0.25–4, default 1),
  `response_format` (`mp3` default, `opus`, or `wav`).
- `POST /v1/audio/transcriptions`: multipart bytes, model, filename, boundary,
  and optional language are forwarded unchanged. Only the configured upstream
  receives `ROBOT_STT_API_KEY`; the local bearer token is never forwarded.

Female preset: f3, 145 wpm, pitch 55. Male: m3, 130 wpm, pitch 30.
Automatic language detection runs locally and deterministically (`langdetect`).
Low-confidence detection falls back to English; unsupported espeak languages
retry in English. Short or mixed-language text can be misclassified. Clients may
supply a `language` code explicitly; phantombot currently sends no such field.
This service supplies detection for speech only, and does not guess STT language.
MP3 works with phantombot’s current speech requests; Opus is available to other
clients. Phantombot remains responsible for channel-specific voice-note conversion.

Requests are limited to 25 MiB, two concurrent processing operations, 30-second
socket/process timeouts and a 45-second upstream timeout. Busy requests get 503.
Upstream redirects are refused to prevent credential forwarding. Errors contain
no provider bodies or secrets. No request-content logging. Loopback plus bearer
auth protects the API; use a reverse proxy with appropriate limits if changing
this design to support remote clients. Only Linux is verified here.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
ROBOT_AUDIO_SMOKE=1 .venv/bin/python -m unittest discover -s tests -v
```

The smoke test makes actual HTTP speech requests for both voices in English,
Dutch and German, requests each supported format, and decodes every result with
ffmpeg. It never calls a paid provider. Transcription tests use a local mock
upstream to verify bytes, authentication, errors and redirect handling.

MIT; see the repository license. Supersedes phantombot issue #675 and PR #676.
