# Twilio Voice Agent

A live phone agent built on **Twilio ConversationRelay**. Twilio handles
all audio (STT via Deepgram, TTS via ElevenLabs, barge-in, DTMF,
language switching). This server only sees text in and text out over a
WebSocket — no Media Streams, no audio frames in process. Cheap to run
and easy to reason about.

## What it does

- **Inbound:** whitelisted callers dial one of your Twilio numbers and
  have a real-time spoken conversation with the LLM.
- **Outbound:** any agent / script / cron job POSTs to `/initiate-call`
  with a bearer token, providing `to`, `purpose`, and optional
  `context` / `greeting` / `language`. The agent calls the recipient
  and conducts the conversation on your behalf.

LLM is whatever you point it at via OpenRouter. The default is
`inception/mercury-2` — picked for low first-token latency on phone
calls — with `anthropic/claude-haiku-4.5` as fallback when the primary
returns a 5xx.

## Tools the LLM can call

- `ask_assistant(message)` — proxy a hard question to a back-end agent
  via the optional `phantombot` CLI integration. Use for things outside
  the voice loop's injected context (older history, web search, home
  automation, sending messages, calendar lookups). When `phantombot`
  isn't on PATH the tool is still exposed but every call returns an
  error string the LLM can recover from.
- `end_call(reason)` — hangs up cleanly when the conversation is done.

When `ask_assistant` is invoked the agent emits a language-aware filler
("Hang on, let me check" / "Even kijken" / "Un momento, déjame
verificar" / etc.) so the caller doesn't sit in dead air while the
back-end runs.

## Architecture

```
                                      ┌────────────────────────┐
   PSTN / SIP caller ──────► Twilio ──┤  ConversationRelay     │
                                      │  (STT/TTS/barge-in)    │
                                      └───────────┬────────────┘
                                                  │ WebSocket text frames
                                                  ▼
   nginx :443 (your domain)
     /twiml          ──► 127.0.0.1:8080  (TwiML + caller whitelist)
     /ws             ──► 127.0.0.1:8080  (ConversationRelay WS)
     /initiate-call  ──► 127.0.0.1:8080  (bearer-token-protected)
     /call-status    ──► 127.0.0.1:8080  (Twilio callback, signature-validated)
                                                  │
                                                  ▼
                                  ┌─────────────────────────────┐
                                  │   server.js (Fastify)       │
                                  │   - LLM:  OpenRouter        │
                                  │   - Tool: ask_assistant ─┐  │
                                  │   - Tool: end_call       │  │
                                  └──────────────────────────┼──┘
                                                             │
                                                             ▼
                                          (optional) phantombot CLI
```

Single Node 20+ ES-module Fastify process listening on
`127.0.0.1:8080`. No DB, no Redis. Per-call state lives in two `Map`s
(`sessions`, `pendingOutboundCalls`) — restart clears them, which is
fine because calls are ephemeral.

## Quickstart

```bash
git clone https://github.com/phantomyard/phantomtools.git
cd phantomtools/twilio-voice-agent
npm install
cp .env.example .env
$EDITOR .env             # fill in TWILIO_*, OPENROUTER_API_KEY, ALLOWED_CALLERS, etc.
node server.js
```

You'll need to put nginx (or any TLS-terminating reverse proxy) in
front of port 8080 and point your Twilio voice webhooks at
`https://<your-domain>/twiml`. Twilio ConversationRelay requires a
publicly reachable HTTPS / WSS endpoint — `localhost` won't work; use
ngrok or similar for development.

## Tuning the agent's turn-taking

The TwiML blocks (in `server.js`, around the `/twiml` handler) ship
with these ConversationRelay settings:

| Attribute               | Value   | What it does |
|-------------------------|---------|--------------|
| `speechModel`           | `flux`  | Deepgram's conversational turn-detection model (better than nova-3 at not interrupting). |
| `eotThreshold`          | `0.9`   | End-of-turn confidence required before the model can start speaking. Range 0.5–0.9, default 0.8. |
| `ignoreBackchannel`     | `true`  | Filters "yeah/uh-huh/right" so they don't trip turn-end early. |
| `interruptible`         | `speech`| The agent stops speaking when the human interrupts. |
| `interruptSensitivity`  | `low`   | Don't get triggered by tiny noises. |
| `dtmfDetection`         | `true`  | Pass DTMF digits through to the WebSocket. |

This combo (flux + eotThreshold 0.9 + ignoreBackchannel) is what we
landed on after a lot of tuning. If the agent is interrupting humans
mid-sentence, raise `eotThreshold` toward 0.9. If it feels too patient
(awkward silences), lower it.

## Environment variables

See [`.env.example`](./.env.example) for the complete list with
descriptions. Required:

- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`
- `OPENROUTER_API_KEY`
- `SERVER_DOMAIN` (matches your TLS cert + Twilio webhook hosts)
- `VOICE_AGENT_API_TOKEN` (bearer for `/initiate-call` — never deploy
  without it; the endpoint will be open if unset)
- `ALLOWED_CALLERS` and/or `ALLOWED_SIP_USERS` (whitelist; otherwise
  inbound is rejected with a polite hangup)

## HTTP / WebSocket endpoints

| Path             | Method  | Auth            | Purpose                                    |
|------------------|---------|-----------------|--------------------------------------------|
| `/health`        | GET     | none            | Liveness + flag summary.                   |
| `/twiml`         | ALL     | Twilio sig      | TwiML for inbound + outbound, with whitelist. |
| `/ws`            | WS      | (Twilio relay)  | ConversationRelay text frames.             |
| `/initiate-call` | POST    | Bearer token    | Trigger outbound call.                     |
| `/call-status`   | POST    | Twilio sig      | Status callback (failed, completed, etc.). |

### Trigger an outbound call

```bash
curl -X POST https://your-domain/initiate-call \
  -H "Authorization: Bearer $VOICE_AGENT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "to": "+15551234567",
    "purpose": "Confirming the appointment for tomorrow at 10",
    "context": "Caller is the dentist office, my name is on file",
    "language": "en"
  }'
```

The agent picks the from-number based on destination prefix (`+31...`
gets `TWILIO_PHONE_NL` if set, otherwise `TWILIO_PHONE_US`).

## Voices

The default voice IDs in `VOICE_MAP` (top of `server.js`) are
ElevenLabs voices we happened to like. Swap them for your own — the
voice IDs are public identifiers; you'll just need an ElevenLabs
account that ConversationRelay is configured against.

## Security

- All inbound webhooks are validated against `TWILIO_AUTH_TOKEN` via
  HMAC-SHA1 (Twilio's standard signature).
- `/initiate-call` requires `Authorization: Bearer <VOICE_AGENT_API_TOKEN>`.
- Inbound calls from numbers / SIP users not in `ALLOWED_CALLERS` /
  `ALLOWED_SIP_USERS` get a polite "this number is not accepting calls"
  TwiML response and hangup.
- Auth failures are logged in a structured `AUTH_FAILURE ip=… path=… reason=…`
  format that `fail2ban` can match against.

## License

MIT.
