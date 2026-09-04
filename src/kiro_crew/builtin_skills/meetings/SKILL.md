---
name: meetings
description: Context for working with the Meetings app's data — where a meeting's notes, diagram, transcript-derived tasks, and calendar cache live, what the lifecycle states mean, and how the app's agents are driven. Load when the user asks about a meeting's notes or action items, or when writing/reading files under the meetings app data dir.
triggers: meeting, meeting notes, action items, meeting tasks, transcript, standup, retro, calendar, meetings app
---

# Meetings app

The Meetings app transcribes a live meeting and fans each line out to a small
crew of background agents. Each agent owns exactly ONE output file and rewrites
it in full after every batch.

## Where the data lives

All paths are under `~/.kiro/crew/apps/meetings/data/`:

| Path | What it is |
|---|---|
| `config.json` | app config: the agent roster, the task/calendar provider ids, presets |
| `dictionary.toml` | speech-to-text correction terms (`[[term]]` blocks) |
| `calendar-cache.json` | the last calendar sync's events |
| `task-ledger.json` | tasks filed through the local task provider |
| `meetings/<id>/session.json` | one meeting's metadata (title, status, attendees, attachments, outputs map) |
| `meetings/<id>/tasks.json` | that meeting's extracted action items |
| `meetings/<id>/<agent-id>.md` | a markdown agent's output (e.g. `note-taker.md`) |
| `meetings/<id>/<agent-id>.html` | an HTML agent's output (e.g. `sketch-artist.html`) |

`<id>` is the meeting id with `:` replaced by `_`. Only `[A-Za-z0-9._-]` is
legal in it — the backend rejects anything else, so do not construct a path from
a raw calendar UID.

## Lifecycle

1. `idle` — the meeting folder exists, nothing is running.
2. `active` — transcription is flowing; agents receive batched lines every ~30s.
3. `paused` — transcription stopped, the session and its queues are intact.
4. `reviewing` — the meeting is over and the user is triaging extracted tasks.
5. `ended` — finished; outputs are final.

## The agents

| Agent | Writes | Notes |
|---|---|---|
| `meetings-note-taker` | `note-taker.md` | structured notes: topics, decisions, action items, open questions |
| `meetings-sketch-artist` | `sketch-artist.html` | one self-contained HTML/Mermaid diagram, revised in place |
| `meetings-task-extractor` | `tasks.json` | always runs; the app's core output |

Each agent's first message carries an `OUTPUT_FILE:` line. Write to that exact
path, character for character, and rewrite the FULL file after every batch —
never accumulate content in memory, because a context limit would lose it.

Lines prefixed `[chat]` are typed by the user, not transcribed: treat them as
corrections or added context and act on them immediately.

## Providers

Two things are pluggable, and which implementation is active comes from
`config.json`:

- **`task_provider`** — where a reviewed action item is filed. The shipped
  provider is `local`, which appends to `task-ledger.json`.
- **`calendar.provider`** — where upcoming meetings come from. `none` (default,
  meetings are created by hand) or `ics`, which reads the iCalendar document at
  `calendar.source` (a local `.ics` file path, or a published `https://` URL).

To sync the calendar, call `POST /api/apps/meetings/calendar/sync` — do not try
to fetch or parse the calendar yourself. With a provider configured the app also
syncs on its own every `calendar.poll_interval_secs` (default 300) and creates
the meeting directory for an event `calendar.precreate_lead_minutes` (default 15)
before it starts, so an imminent meeting usually already exists as `idle`;
`calendar.auto_sync: false` turns the background poll off, and a lead of `0`
keeps the sync but stops pre-creation.

## Speech-to-text

Transcription uses KiroCrew's own streaming endpoint (`/api/ws/stt`), driven from
the browser. Cloud transcription is optional
(`pip install 'boto3>=1.34,<2' 'amazon-transcribe>=0.6,<1'`); when it is not
installed the app says so and the user can still type into the broadcast bar to
feed the agents.

## Correcting recurring mistranscriptions

When the user complains that a project noun keeps coming through wrong, add a
dictionary term rather than correcting it in the notes:

```
POST /api/apps/meetings/dictionary
{"correct": "DynamoDB", "aliases": ["dynamo db", "dynamo d.b."]}
```

Matching is case-insensitive with word boundaries and the longest alias wins, so
every later meeting gets it right at the point of transcription.
