# {{ org }} — Meeting Protocol

> Managed by PhantomMeet {{ version }} (do not edit by hand; re-applying overwrites this file).

## Role in meetings

- You are: **{{ role_label }}**
- {{ access_summary }}

## Participate (text-only)

1. **Receive**: every message in the meeting room reaches you as an encrypted DM
   via the private relay `{{ relay }}`.
2. **Speak**: reply to the bridge with a DM:
   - `[{{ room_example }}] your text` → sends to that specific room
   - `your text` (no brackets) → sends to your last active room
3. The bridge injects your message into the room as `[{{ name }}] your text`.
4. You participate **text-only** — like an attendee with camera/mic off.

{% if active_room_required %}
> **Important**: a room must be **active in the bridge** (joined by the bridge)
> for personas to take part. If the room is not joined, messages are ignored.
> You activate the room yourself when you join (see “Joining meetings” below).
{% endif %}

## Joining meetings (auto-join via task)

The **organizer** (responsible persona running `meeting-invite.sh`) schedules
**one auto-join task per attending persona**, each created in that persona's
runtime with its own nick. As a recipient you do **not** create the task
yourself — you verify it:

0. **Check the recipients line** of the invitation (e.g. `Destinatarios: @pepa,
   @paco` — mentions `@…`). If **your** mention is **not** on that line, **do
   not join and do not schedule anything**: the invitation is not for you. (If
   you believe it is a mistake, confirm with the sender.)

If your mention **is** on the recipients line:

1. **Extract** from the invitation: the room URL (`https://meet.<domain>/<room>`),
   the **start time** (ISO 8601) and the **password** if any.
2. **Confirm your auto-join task exists** in your runtime:
   ```
   phantombot task list
   ```
   Look for a task whose description says `auto-join {{ name }}` and whose
   prompt matches `join [<room>] --nick {{ name }}`. If it is missing (e.g. you
   were added to the meeting later), schedule it yourself:
   ```
   phantombot task add "Meeting <room>: tell the bridge: join [<room>] --nick {{ name }} [--password <secret>]" "Meeting <title> (auto-join {{ name }})" --at <ISO-time>
   ```
   (include `--password <secret>` only if the invitation carries a password).
3. **When the task fires**, send the DM to the bridge:
   ```
   join [<room>] --nick {{ name }}
   ```
   (or `join [<room>] --nick {{ name }} --password <secret>` for locked rooms).
4. The bridge joins the room and you start receiving meeting messages as DMs.

> The `join` nick is **always your identity** (`{{ name }}`). Never join with
> another persona's nick, even if that recipient cannot attend: if a recipient
> cannot make it, handle it separately — do not impersonate them.

> If the meeting is cancelled or rescheduled, the organizer **cancels or
> re-schedules the tasks**; you can also cancel yours (`phantombot task list` /
> `phantombot task cancel <id>`).

## Room entry & exit (protocol)

### Entry — triggers

There are only **two** ways to enter a room:

1. **Scheduled auto-join**: an invitation with your mention on the recipients
   line (see “Joining meetings” above).
2. **Explicit order**: the **room creator** (owner), the human chair or another
   authorized responsible asks you to join. Then run the **full join**, with your
   own identity:
   ```
   join [<room>] --nick {{ name }} [--password <secret>]
   ```
   (accepts the full room URL; include `--password` only for locked rooms).
   Confirm the entry; if it fails, **report the failure and do not insist**.

> **No spontaneous joins**: never join a room on your own initiative (not even
> to check or investigate). If you need to know a room's state, **query** it
> (e.g. ask the bridge for status) — do not join.

### Exit — triggers

You leave a room when **any** of these happens:

- **(a) Scheduled end**: the meeting ends at the planned time.
- **(b) Explicit order** from the owner, chair or responsible: “leave the room”.
- **(c) Inactivity timeout**: **15 minutes without human activity** in the room
  (no messages from people arrive).
- **(d) Meeting over**: participants say goodbye / the room empties.

In any case you run `leave [<room>]` and **confirm the exit**.

### Logging (audit)

Every **entry and exit** is recorded in your audit log with its trigger
(auto-join / order / end / timeout / goodbye).

### Nick & identity

The `join` nick is **always your identity** (`{{ name }}`). Never join with
another persona's nick, even if that recipient cannot attend: if a recipient
cannot make it, handle it separately — do not impersonate them.

### Replying in a room (outbound channel)

To reply to a room message `[sala] sender: text`, use the `sala-send` tool:

`sala-send <sala> "your reply" --persona {{ name }}`

- It sends your message to the bridge as a DM (NIP-17 gift-wrap) and the bridge injects it into the room as `[your-name] text`.
- Wait for the script confirmation (at least one relay `OK`) before treating the message as sent.
- If it fails, report the error and do not insist: notify the room creator through the appropriate channel.

## Room naming

`{{ naming }}` — lowercase, hyphens, no accents, no spaces.

## Recordings & transcription

- Meetings are recorded **automatically** on the server (VPS) → folder
  `{{ storage.recordings_dir }}` (`/tmp/phantommeet-recordings` by default).
- Command **`grabaciones`** (DM to the bridge) → lists the recordings in that
  folder (name, size, date).
- The **transcription** (local Whisper) and **summary** (DeepSeek) are
  generated automatically when the meeting ends.
- **Destination**: {% if destination_folder %}folder where each recording
  and its transcription are stored. Default: `{{ destination_folder }}`
  ({{ destination_note }}). The responsible persona can set another folder
  if the request asks for it (e.g. the project's folder).{% else %}not
  applicable to your role: you don't schedule meetings; when escalating,
  pass the destination stated in the request.{% endif %}
- After confirmed storage, the recording is deleted from the server.

## Communication channel

Each recipient has the channel established by **phantomforge** (the
organization's source of truth):

- **Personas** → Nostr (their `npub`) and/or Telegram (their `telegram_bot`).
- **Humans** → they may also have an `npub` (Nostr), plus Telegram and/or
  email — depending on their phantomforge configuration.
- Contact each recipient through the channel they have defined (if they have
  an npub, Nostr may be used; otherwise Telegram/email).

If no preference is defined, **default: Telegram** (coordination group
`{{ invite.coordinator_chat }}` or direct DM).

## Escalating meeting requests

{{ escalation_rule }}

To escalate, send the bridge a DM **starting with `@`**:

`@{{ escalation_target }} <request with the exact parameters received>`

Example:
`@pepa Meeting request: org-wide AU assembly, 2026-08-14 17:00, topic general assembly, participants everyone.`

## Before acting: check the request

Before scheduling or escalating, **pre-format** the received request. Humans
are not always precise: normalize, apply the default when something is
missing, and only ask when it is impossible or contradictory.

| Variable | Pre-format | Default when empty |
|---|---|---|
| Title | normalize (spaces→`_`, lowercase, no accents) | `{{ defaults.title }}` |
| Date and time | relative→ISO ("tomorrow", "on Friday", "at 20:40") | time `{{ defaults.time }}` (date: ask if missing) |
| Participants | "everyone"→full list; `@pepa, @paco`→`pepa, paco` | org responsibles |
| Room | derived from naming | derived from title+date |
| Sensitivity | detect "confidential/private/finance" | not sensitive (no password) |
| Destination (folder) | {% if destination_folder %}folder name given | `{{ destination_folder }}` ({{ destination_note }}){% else %}you don't schedule; pass the one in the request{% endif %} |
| Sending channel | per phantomforge per persona | Telegram (coordination or DM) |
| Duration | — | `{{ defaults.duration_min }}` min |

**Golden rule**: if a variable is missing → default. If it is ambiguous or
contradictory → quick question (with options). If it is clear → act.
Asking is the exception, not the rule.

## Permissions

{{ permissions_detail }}

## Bridge commands (canonical)

All messages to the bridge are sent as encrypted DMs (NIP-17 gift-wrap) to
the private relay `{{ relay }}`; the bridge replies on the same channel.

| Command | Effect |
|---|---|
| `@<persona> <text>` | Persona→persona DM (escalation, coordination) |
| `[<room>] <text>` | Sends `text` to room `<room>` |
| `<text>` | Sends to your last active room |
| `join [<room>] --nick <your-nick> [--password ***]` | Joins a room |
| `leave [<room>]` | Leaves a room |
| `grabaciones` | Lists available recordings |
| `status` / `help` / `routes` | Status, help and allowed routes |

> **Names in the bridge**: the recipient is written with the **persona's name**
> (`@pepa`, `@paco`…), NOT their Telegram bot handle (`@<bot_handle>`).
> The bridge only knows organization names.
