---
name: self-tag-chat
description: >-
  Tag the current chat's own dashboard slot with an SDLC status (planned,
  todo, implementation, review, done) so the Chats sidebar and Board view
  reflect where the work stands. Use when this chat is doing
  software-development-lifecycle work — spec authoring, coding, PR review,
  deploy — and you cross a phase boundary. Safe to follow from any context:
  outside a dashboard chat it stops without tagging.
---

# self-tag-chat

Tag **this chat's own dashboard slot** with an SDLC status so the Chats
sidebar / Board view reflects where the work stands. Use it when this chat is
doing software-development-lifecycle work (spec authoring, coding, PR review,
deploy) and you cross a phase boundary.

Everything below goes through the `chat_status_tags_api` MCP tool — the
gateway holds the credential, so **no token is ever minted, printed, or
handled**, and the tool's allowlist means these steps can read the slot list
and the tag vocabulary and write ONE slot's tags, nothing else.

## Procedure

`<phase>` is one of: `planned` `todo` `implementation` `review` `done`.

1. **Resolve your own slot.** Read `$KIROCREW_SESSION_KEY` (the gateway
   injects it into every dashboard chat's agent process as
   `dashboard:<slot>`). If it is unset, or has no `dashboard:` prefix (IDE,
   plain CLI, messaging, cron, subagent), **stop silently — there is no slot
   to tag** and that is fine. Otherwise the slot key is the part after
   `dashboard:`.
2. **Read the tag vocabulary**: `chat_status_tags_api` `GET /tags`. Find the
   tag whose name equals `<phase>` case-insensitively.
3. **Create it only if missing**: `chat_status_tags_api` `POST /tags` with
   `{"name": "<phase>", "status": true, "color": "<color>"}` — colors:
   planned `purple`, todo `blue`, implementation `orange`, review `yellow`,
   done `green`. Creation is idempotent by case-insensitive name; use the id
   from the response either way. Ids are server-assigned — never assume one.
4. **Read your slot's current tags**: `chat_status_tags_api` `GET /slots`,
   find your slot by key, note its `tags` list.
5. **Never downgrade.** The order is planned → todo → implementation →
   review → done. If the slot already carries a status tag at or past
   `<phase>`, stop — a human can drag a tag back; automation must not.
6. **Write the merged list**: keep every non-status tag, drop any other
   status tag, add the `<phase>` tag id, de-duplicated —
   `chat_status_tags_api` `PUT /slots/{slot}/tags` with `slot_key` set to
   your slot key and body `{"tags": [...]}`. (Status tags are the five phase
   names; anything else in the vocabulary, including the `stuck`/`network`/
   `error` health tags, is non-status and must be preserved.)

## When to tag (phase mapping)

Set the tag at the moment the phase actually changes, not preemptively:

| Phase | Set it when |
|-------|-------------|
| `planned` | scoping / gathering requirements, nothing actionable yet |
| `todo` | plan agreed, work not started |
| `implementation` | actively writing code / building / editing files |
| `review` | change is committed and a pull request is open (awaiting review) |
| `done` | the pull request merged (or the task is otherwise complete) |

Rules:

- Only tag chats doing SDLC work. Skip pure Q&A, research, or triage chats.
- Never downgrade a phase (e.g. don't move `done` back to `review`).
- Tag once per transition — a handful of tool calls, so don't re-tag a phase
  you already set.

## Notes

- **Dashboard chats only.** Only dashboard chats have a slot; everywhere else
  step 1 stops the procedure cleanly.
- The Chat Status Tags app's hourly reconciler independently promotes slots
  to `done` when their referenced pull requests merge, so the final flip has
  a backstop — but tagging it yourself is preferred for immediacy.
- The app's own 60-second health sweep clears the `stuck`/`network`/`error`
  health tags automatically as soon as a chat recovers — no manual cleanup
  step exists or is needed.
