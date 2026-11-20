---
title: Rewind a dashboard chat in place — cut back to an earlier turn and stop
status: draft
author: RohanK6
created: 2026-11-09
last-audited: 2026-11-09
audited-at: d0e911b617
doc-pr: 33998
implementation-prs: []
tracking-issues: [8067, 9509, 33962]
supersedes: []
superseded-by: []
---

# RFC: Rewind a dashboard chat in place

**Status:** draft. This is a proposal for the team to accept, change or
reject. On 2026-10-08 KiroCrewCaptain wrote on
[#8067](https://github.com/kirodotdev/KiroCrew/issues/8067) that a rewind is a
new concept the team designs itself, and asked for a request for change rather
than an outside pull request. Nothing here is built, and no implementation PR
opens until a maintainer records a decision in this document. Every main-side
reference was read at `d0e911b617`.

Credit: the ask is #8067 (rewind N turns, with an optional undo of side
effects), with TheAksh's
[#9509](https://github.com/kirodotdev/KiroCrew/issues/9509) (the
conversation-only half) folded into it, and TomeRozen's
[#33962](https://github.com/kirodotdev/KiroCrew/issues/33962) ("Rewind to here"
on a message, with a confirm step and a list of what stays done). buluoray's
closed [#17774](https://github.com/kirodotdev/KiroCrew/pull/17774) built
`/rewind [N]` as a fork; it closed unmerged when T3/T4 work paused, and the
review concerns it drew are answered in §5.

## 1. Summary

A person whose chat went the wrong way has no way to cut it back to an earlier
turn and simply stop there. Main can re-run an edited earlier prompt in place,
or copy the chat up to a point into a new chat. Neither leaves the person at
the earlier point, in the same chat, with nothing re-sent.

This document proposes:

- **P1 (the core):** a default-off **Rewind** that cuts the chat back to before
  one of the person's own turns and stops. In place, same chat. The cut rows
  are archived, not dropped, and stay recoverable for the configured archive
  retention period. File edits and every other side effect stay
  done, and the person is told so before they confirm.
- **P2 (a later, separate decision):** an optional undo of the agent's own file
  edits, behind its own flag, with a dry-run preview first. This document does
  not ask for P2 to be accepted with P1.
- **P3:** the dashboard surface for P1 (and for P2 if it is accepted).

## 2. What main has today

| Piece | Where | What it does |
|---|---|---|
| Rewind (edit and re-run) | `src/kiro_crew/dashboard/chat_rewind.py`, `POST /api/chat/slots/{slot}/rewind` (`api_chat_slot_rewind`) | Cuts at a message, swaps the ACP session for a fresh one, deletes the orphaned kiro-cli session file, and re-runs an edited prompt. `content` is required (`api_chat_slot_rewind` answers 400 without it), so it always re-sends. |
| Edit & Resend | `POST /api/chat/slots/{slot}/edit-resend` (`api_chat_slot_edit_resend`) | Cuts the in-memory window and re-sends. The kiro-cli session file keeps its forward turns (the `chat_rewind.py` module docstring). |
| Fork | `src/kiro_crew/dashboard/chat_fork.py`, `POST /api/chat/slots/{slot}/fork` (`api_chat_slot_fork`) | Copies the chat up to a point into a new chat. The original stays as it was. |
| Merge back | `src/kiro_crew/dashboard/chat_merge_back.py` | Brings a fork's new rows back into its parent. |
| Archive before rewrite | `docs/system-specs/modules/history.md` ("Fail closed") | A rewind, regenerate or fork save archives the rows it drops before it replaces the transcript. If the archive fails, nothing is written and a rewind answers `503 rewind_save_failed`. |
| KAS rewind | `src/kiro_crew/acp/session_handle_kas.py` | Rolls KAS history back to before the handle's last prompt only. |
| kiro-cli `/rewind` | `docs/reference/kiro-cli/reference/slash-commands.md` | Native; forks the conversation. On KAS it is in `KAS_KIRO_CLI_ONLY_COMMANDS` (`dashboard/kas_local_commands.py`). |
| Per-turn file record | `src/kiro_crew/dashboard/chat_turn/file_changes.py` (`_snapshot_write_target`, `_record_turn_snapshot`, `_flush_file_changes`) | Before an agent write, the file's prior content is read; at turn end the before/after pair is stored on the reply as `meta.file_changes` for the diff cards. It is bounded for display (`_MAX_SNAPSHOT` per file, `_MAX_TURN_SNAPSHOT_ENTRIES` per turn), redacted, and a capped file is marked truncated. |
| File undo | none | Nothing restores a file. #9366 (automatic checkpoints) is closed and #13694 (revert a file or hunk) is parked. |

So a "context only" rewind already exists as a copy (Fork). What is missing is
the in-place cut that stops without re-sending. That was the finding on #8067
on 2026-09-28: the rewind endpoint has no truncate-only mode, so a
frontend-only `/rewind N` would only repeat Edit & Resend.

## 3. Goals

- Cut a chat back to before one of the person's own turns, in place, and stop.
- Keep the cut rows recoverable on disk for the archive retention period
  (`session.archive_retention_days`, 30 days by default).
- Say plainly, before the person confirms, what the cut does not undo.
- Behave the same on every harness where it is offered, and refuse plainly
  where it cannot.

## 4. Non-goals

- Undoing file edits, shell commands, git state, uploads, posts, tickets or any
  other side effect in P1.
- Changing Fork, Edit & Resend or the existing rewind-and-re-run.
- Taking over kiro-cli's own `/rewind` inside a kiro-cli terminal.

## 5. Answers to the #17774 review concerns

1. **One word for one act.** The act is **Rewind**, everywhere: the slash
   command, the message action, the confirm dialog and the result notice.
   "Fork" stays the word for a copy. Rewind never makes a new chat, so it never
   shows a "Fork of…" entry, and its text never says "copy".
2. **Count only the person's turns.** "N turns" means N of the person's own
   prompts. Rows a machine wrote into the chat (cron notifications,
   `session_send` deliveries, workflow follow-ups, app wakes) are not turns to
   count and are never a cut point. P1 needs a positive marker that a row is
   the person's prompt. Some machine writers already use their own row kind:
   a workflow follow-up is appended as an `inject` row with
   `injectKind: workflow` (`enqueue_or_run_prompt` in `state_parts/chat_slot.py`), not a `user` row.
   Phase 0 lists every writer of `user` rows and confirms each machine writer
   is either its own kind or marked. P1 is blocked on that list.
3. **No routing from a cached menu.** Whether `/rewind` is answered by the
   dashboard is decided on the server from the slot's harness, by a positive
   identity check (`is_kiro_backend` or a named `ACP_BACKENDS_*` set), never from
   what the client's slash-menu cache happens to hold. Where the harness cannot
   rewind in place, the server answers a typed refusal and the text is not sent
   to the model.

## 6. Design

### P1: Rewind in place (default off)

- **Flag:** a new `chat.rewind_in_place` setting, `false` by default.
- **Request:** a truncate-only mode on the rewind path, for example
  `POST /api/chat/slots/{slot}/rewind` with `{at_message_index, stop: true}` and
  no `content`. The existing body shape keeps its current meaning.
- **Cut:** the cut point is just before a person's turn (§5.2). The chat then
  ends at the reply before it. Nothing is re-sent.
- **Archive, not delete:** the cut rows go through the same archive-before-
  rewrite path the rewind already uses. If the archive fails, nothing changes
  and the request answers `503 rewind_save_failed`. The fix for repeated
  archives on a failed replace (#32757) applies to this writer too. The
  archive is kept for `session.archive_retention_days`, not forever.
- **Model context:** the next turn runs on a session whose history ends at the
  cut. The rewind already swaps in a fresh ACP session; P1 reuses that swap and
  only skips the re-run.
- **Composer:** the first cut prompt is put back in the composer, not sent
  (#33962).
- **`/rewind N`:** the slash command resolves N against the person's turns and
  calls the same request. With no N it rewinds one turn.

### P2: Undo the agent's own file edits (separate decision, own flag)

Only sketched here, so the team can decide on P1 alone.

- **Flag:** `chat.rewind_undo_files`, `false` by default, and only when P1's flag
  is on.
- **Record:** start from the existing `meta.file_changes` capture, not a second
  one. It cannot restore a file as it stands, because it is capped, redacted
  and may be truncated. P2 needs an undo-grade companion for the same write:
  the exact prior bytes and a hash of what the agent wrote, kept outside the
  transcript. A file whose capture was truncated or redacted, or that has no
  undo-grade record, is listed as "stays done", never restored from the display
  copy.
- **Preview first:** a dry run lists every file the undo would restore, and
  refuses any file whose current bytes differ from what the agent wrote (the
  person or another tool changed it since).
- **Scope:** only files written through Kiro Crew's own write tools. Shell
  writes, git operations and anything outside the workspace are out of scope
  and listed as "stays done".

### P3: Dashboard surface

- A **Rewind** action on the person's own messages, beside Edit & Resend.
- A confirm dialog that names the turns being cut and lists the tool actions
  in them that stay done (#33962).
- Strings through the i18n catalog. Screenshots attached to the PR.

## 7. What cannot be undone

Say this in the confirm dialog, the docs and the result notice:

- **P1 undoes nothing outside the chat.** Every file edit, shell command, git
  commit or push, upload, message, post or ticket made in the cut turns stays
  done.
- A reply already shown in another place (a channel, a notification, an email)
  stays there.
- With P2 on, only files the agent wrote through Kiro Crew's own write tools,
  inside the workspace, and unchanged since, come back. Everything else stays
  done.

## 8. Migration plan

| Phase | PR-sized change | Exit criteria |
|---|---|---|
| 0 | Audit every writer of `role: user` transcript rows; write the list into this document. | Each machine writer is named with its marker, or the gap is filed. |
| 1 | Truncate-only rewind behind `chat.rewind_in_place`, and `/rewind N` on the server. | With the flag off, nothing changes. With it on, a rewind leaves the chat ending at the reply before the cut, sends nothing, archives the cut rows, and a failed archive changes nothing. Machine rows are never counted. An unsupported harness gets a typed refusal. |
| 2 | Per-turn file record, dry-run preview, undo, behind `chat.rewind_undo_files`. Blocked on its own acceptance. | The preview lists exactly the files the undo restores; a file changed since the agent's write is refused, not overwritten. |
| 3 | Message action, confirm dialog, composer refill. | The dialog lists what stays done; every string is in the catalog. |

Each phase can be dropped without the later ones.

## 9. Backward compatibility

The existing rewind body (`{at_message_index, content}`) keeps its meaning,
and Fork and Edit & Resend are unchanged. Both flags default off. With them off
nothing new is reachable.

## 10. Security and data-loss considerations

- **Data loss:** the cut is a transcript rewrite, so it rides the existing
  archive-before-rewrite rule: no archive, no cut. Archives follow
  `session.archive_retention_days` (30 days by default), after which the
  archive cleanup removes them like any other archive. A rewind deletes no row
  at the moment it runs; it does not keep one forever either.
- **P2 overwrites files on disk.** It only restores files the agent wrote, only
  inside the workspace, only when unchanged since, and only after a preview.
  The stored prior bytes are file contents and may hold secrets, so they need
  the same storage and masking as the transcript.
- **Who may rewind:** the same owner and app-token gates as the existing rewind
  route; a relay archive stays read-only.

## 11. Alternatives considered

- **Fork only (#17774).** Already possible from a message. It leaves the person
  in a new chat and keeps the wrong turns in the old one, which is what the
  issue asks to avoid.
- **Frontend-only `/rewind N` over Edit & Resend.** It re-sends a prompt, which
  re-enters the turn the person wanted to drop.
- **Hand `/rewind` to kiro-cli.** That forks natively, and it does not exist on
  every harness.

## 12. Open questions

1. Is Rewind in place the act the team wants, or should the in-place cut stay
   out and Fork be the only way back?
2. Should a rewind be allowed while a turn is running, or wait for it to end?
3. Is P2 wanted at all, given #9366 is closed and #13694 is parked?

## 13. Decision

No decision recorded yet.
