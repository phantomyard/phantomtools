# Meeting Workflow — Human User Guide

_How a real meeting works, from the perspective of the people using it._
This guide is for **human users** (chair, members, guests). It complements
[SPEC.md](./SPEC.md) §6 (generic lifecycle) with a concrete, step-by-step
example. No technical knowledge required.

> Reference deployment: a self-hosted Jitsi at `meet.example.invalid`, private
> Nostr relay, persona bridge, local Whisper transcription + DeepSeek summary.
> Replace the placeholders with your own deployment values.

---

## Roles

| Role | Who | How they participate |
|---|---|---|
| **Chair / moderator** | A human (first to enter the room) | Video + audio + chat; controls recording |
| **Human participants** | Invited guests | Browser: video + audio + chat |
| **Responsible persona** | e.g. the secretary/CEO persona | Text-only via the bridge (`[room] text`) |
| **Support personas** | Invited via the room link (no prefix restriction) | Text-only via the bridge |

Personas **have no browser**: they attend meetings as text participants through
the bridge, exactly like a person sitting in the room with camera/mic off, who
reads the chat and writes in it.

---

## Example flow (end to end)

Imagine the chair wants a meeting tomorrow at 17:00, with two human guests and
one support persona present.

### 1. Ask the responsible persona to schedule

> "Please schedule an online meeting in the organization rooms for tomorrow
> at 17:00, invite Bea and Anna, and add Alma."

The responsible persona (e.g. the secretary):

- Creates a **calendar event** (topic, date/time, invitees).
- Builds the **room name** from the convention `{YYYY-MM-DD}-{HH-MM}_{topic}`
  (lowercase, hyphens only in the timestamp, user-entered spaces become `_`,
  no accents), e.g. `2026-08-10-18-06_asamblea_general`.
- Sends the invitations **with the direct room link**
  `https://meet.example.invalid/2026-08-10-18-06_asamblea_general`
  (email/Telegram/calendar invite).

**Room name = recording file name**: the MP4 (and its `.txt`/`.resumen.md`)
keeps the exact room name as its file name in Drive (the configured
`storage.drive_folder`, e.g. `Grabaciones/`).

**With the `meeting-invite.sh` tool** (installed in `tools/` of the personas
listed in `invite.roles`), steps two and three are one command:

```
tools/meeting-invite.sh --title "Junta directiva" \
  --type junta-directiva \
  --datetime 2026-08-08T17:00:00 \
  --recipients "@pepa,@paco,@roberto"
```

The tool derives the room name from the manifest naming convention and sends
the invitation (with the recipients line) as a **notification only**.
`--dry-run` previews everything without sending anything.

  Agent invitations carry a **recipients line with mentions** right after the
  title, so a human can see at a glance who the meeting is for:

  ```
  📅 Reunión: Junta directiva
  👥 Destinatarios: @pepa, @paco, @roberto
  🕐 2026-08-08T17:00:00
  🔗 https://meet.example.invalid/08-08-2026-17-00_junta_directiva
  ```

  The room is **self-creating**: nothing is provisioned on the server; it exists
  the moment someone opens the link.

> ⚠️ **An invitation is a notification, never an authorization.** A persona
> joins a meeting **only from its own scheduled task**, created in its own
> runtime (by the organizer's runtime or by the operator) — never by parsing
> the invitation text. The recipients line is informational: it tells humans
> who the meeting is for, but a persona must not treat its own mention there
> as a command to join. Joining is a **persona action driven by its scheduled
> state**, not a technician step: no SSH needed.

### 2. Join the meeting

The chair opens the room link in a browser. **There is no login/authentication**
— the chair just enters with their display name. Being the **first to enter**,
the chair automatically becomes **moderator** (controls recording, locking,
muting).

> 💡 **Moderator tip:** the human moderator should enter **first** (or be
> promoted). If the bridge entered first, it holds the moderator role; the
> bridge exposes a `/promote` endpoint to fix that. In normal use: people
> first, bridge joins later if personas are needed.

### 3. Wait for everyone, then record

Once everyone is present, the chair **announces the recording**
(transparency/legal) and starts it: **⋮ (More actions) → Record → Start
recording**. Only the moderator can start/stop recording.

- Human participants keep talking via **voice/video**.
- Everyone (humans and personas) writes in the **chat**; personas appear as
  `[name] text` (e.g. `[pepa]`, `[alma]`).

### 4. End the meeting

The chair stops the recording (stop button on the red recording indicator).
That's the last action the chair has to take:

- The **server automatically transcribes** the audio with local Whisper
  (`.txt`) and generates a **DeepSeek summary** (`.resumen.md`) next to the
  recording — no one has to ask for it.

### 5. Store the artifacts

The chair asks the responsible persona to store the artifacts:

> "Please upload the recording and transcription to folder X."

The responsible persona uploads the MP4 + transcription + summary to the
established storage location (e.g. Google Drive) and the artifacts are deleted
from the server after the upload is confirmed.

**How the upload works (Google Drive API):** the responsible persona downloads
the artifact from the meeting host and uploads it to Drive with a single tool:

```
workspace.py drive-upload <url-or-local-path> --folder <folder-name>
```

- Accepts either a **signed URL** served by the meeting host (token-protected,
  temporary) or a **local file** path.
- Creates the target Drive folder if it does not exist and uploads with a
  multipart request (Google Drive API).
- The Drive access model is the one the persona already uses (e.g. a service
  account with domain-wide delegation scoped to the org account, or OAuth2
  tokens).

> Storage locations are **decided by the responsible persona(s)**, never
> imposed by the tooling.

---

## Summary table

| # | Step | Actor | Action |
|---|---|---|---|
| 1 | Schedule | Responsible persona | Calendar event + room name + invitations with link |
| 2 | Join | Chair | Open link (no login), first one in = moderator |
| 3 | Record | Chair | Announce, ⋮ → Record → Start |
| 4 | Participate | Everyone | Humans: AV + chat. Personas: text via bridge (join via their own scheduled task → DM `join`) |
| 5 | End | Chair | Stop recording → server auto-transcribes + summarizes |
| 6 | Store | Responsible persona | Upload artifacts to agreed location, delete from server |

---

## Current autonomy note

Getting the recording out of the meeting host is now **fully autonomous**:

- The bridge serves artifacts over HTTPS with **token-protected temporary
  URLs** (signed, expiring) — no localhost-only limitation anymore.
- The responsible persona downloads those URLs and uploads them to the
  organization Drive with `workspace.py drive-upload`, using the **Google
  Drive API** credentials the persona already has (service account with
  domain-wide delegation, or OAuth2).

The technicians are not needed in day-to-day operation.
