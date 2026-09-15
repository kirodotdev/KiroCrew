# Attribution

The Meetings app was originally written by **adunuthu** as a standalone
KiroCrew-family app named **Meetings**, distributed as its own package with its
own backend server, UI bundle, and agent specs. This builtin is a port of that
work into the KiroCrew open-source tree.

`app.json`'s `author` field is unchanged — the original author remains the
author of record.

## What changed in the port

The original targeted an organization-internal environment. Four couplings had
no public equivalent and were replaced with generic seams; the rest of the app
(the meeting lifecycle, the batching agent dispatcher, the domain dictionary,
the task-review flow, the multi-agent panel UI) is a faithful port.

| Original | This port |
|---|---|
| Filed action items into one company-internal task system, named throughout the UI, the presets, and a dedicated agent prompt | A `TaskProvider` seam (`backend/providers/tasks.py`) with one shipped implementation: a local KiroCrew task ledger. An organization registers its own provider out of tree. |
| Read the calendar through a company-internal MCP server, with an internal-website scrape as a fallback | A `CalendarProvider` seam (`backend/providers/calendar.py`) with a stdlib iCalendar (`.ics`) reader — a local file or a published `https://` URL. |
| Two speech-to-text providers, one of which was a separate locally built daemon from an internal source repository | KiroCrew's own streaming speech-to-text (`/api/ws/stt`). The daemon and its setup script are removed. |
| A standalone `aiohttp` server on its own port, called back into by the gateway over authenticated loopback HTTP | In-gateway routes under `/api/apps/meetings/*`, and in-process agent dispatch through the shared session manager. |
| Two crons: a data-directory self-heal written as a shell blob in the cron message, and an update check that pulled from an internal git host | The self-heal is Python that runs at app startup. The update check is removed — a builtin app versions with the KiroCrew package, so there is nothing to check. |

## Added after the port

This section records what has been added since that port, so the line between the two
stays legible to anyone reading the app cold.

The design comes from **MeetNote**, Kai Mitsuzawa's native macOS meeting app — a
SwiftUI application that records a meeting, transcribes it on the machine, and
generates summaries and minutes from it. The design ports; the implementation does
not, because MeetNote rests on macOS frameworks a browser has no equivalent for.

| MeetNote (native macOS) | Here |
|---|---|
| Importing an existing recording as a new meeting record | Imported into a LIVE meeting instead: transcribed with the gateway's own batch speech-to-text and dispatched through the same admission transaction live speech uses, so every line is persisted to the meeting's transcript and reaches the agents exactly as if it had been spoken. |
