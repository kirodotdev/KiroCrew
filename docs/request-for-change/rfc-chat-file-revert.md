---
title: Revert an agent's file edit from its card in dashboard chat
status: draft
author: RohanK6
created: 2026-11-11
last-audited: 2026-11-11
audited-at: 96a5a39460
doc-pr: null
implementation-prs: []
tracking-issues: [13694]
supersedes: []
superseded-by: []
---

# RFC: Revert an agent's file edit from its card in dashboard chat

- **Status:** Draft. Acceptance requested from a maintainer; the status flips to
  `accepted` when one records it on this line.
- **Verified at:** `96a5a39460`. Every code reference below was read at that commit.
- **Tracks:** [#13694](https://github.com/kirodotdev/KiroCrew/issues/13694).
- **Already decided there** (maintainer bolichen97, 2026-10-08): restore a
  snapshot, never apply a reverse patch; if the file changed after the agent's
  edit, refuse the revert and say why, so later work is never silently discarded.
  This document does not reopen that decision. It settles what the decision needs
  before code can land: where the snapshot lives, how long it is kept, what
  "changed after the edit" means, and who may press the button.

## 1. Problem

When the agent edits a file in a dashboard chat, the transcript shows a diff card
for it. If the edit is wrong, the person has to undo it by hand: open the file,
use git, or ask the agent again. Kiro IDE offers a one-click revert on the same
kind of card, and #13694 asks for the same in Crew, at whole-file and hunk level.

## 2. What exists today, and why it cannot be the restore source

`dashboard/chat_turn/file_changes.py` already takes a pre-write snapshot of every
file a turn writes, and the turn's `file_changes` meta carries a `before` and an
`after` per path. That text is built for display, not for restore:

| Property of the stored `before` | Where | Why it rules out a restore from it |
|---|---|---|
| Cut at 200,000 characters | `_MAX_SNAPSHOT`, `_truncate_snapshot` | A larger file would be restored cut off |
| Redacted | `_prepare_file_changes` runs `security.redact` | A file with a credential would be restored with the tag in its place |
| Windowed around the change | `_excerpt_pair`, `_turn_after_reads` | Only the changed region is kept for most edits |
| Spent from a per-turn budget | `_apply_turn_snapshot_budget` | A busy turn demotes or drops entries |
| Stored in the slot's message meta | `_attach_file_changes` | It is sent to the browser and persisted with the transcript |

So revert needs its own copy of the original bytes, kept on the server, never sent
to the browser and never redacted. The display `before` stays exactly as it is.

## 3. Proposal

### 3.1 The snapshot

- **When:** at the same pre-write point `_snapshot_write_target` already runs, for
  the agent's own file-write tools (create, overwrite, `str_replace`). Shell
  writes are not covered: the card for a shell command is not a file edit card
  today, and a shell command can touch any number of files.
- **What:** the exact bytes of the file before the write, or a marker that the
  file did not exist (so a revert of a create removes nothing; see 3.3), plus the
  sha256 of the bytes the write left on disk, read at the end-of-turn flush where
  `_read_turn_afters` already reads the after-content.
- **Where:** a per-session store under the data home,
  `~/.kiro/crew/edit_snapshots/<session id>/<sha256>.bin`, content-addressed, with
  a small index file mapping `(turn row id, path)` to `(before sha256, after
  sha256, created at)`. Owner-only like the other session stores. It is never
  inside the project directory, so a snapshot never shows up in git status.
- **Limits:** one file up to 2 MB (the same bound `_turn_after_reads` reads to);
  a larger file gets no snapshot and its card says why there is no revert. A
  session keeps at most 200 MB and 14 days of snapshots, oldest first evicted.
  These are the defaults; both are settings under `chat.edit_revert`.
- **Off by default** in the first release (`chat.edit_revert.enabled = false`),
  so nothing is written to disk for anyone who has not opted in.

### 3.2 The revert

`POST /api/chat/slots/{slot}/file-revert` with `{row, path}`:

1. Owner-only through the same gate the other chat write routes use, and audited
   in the SEL with the path and the outcome.
2. Reads the index for that row and path. No snapshot: `404 no_snapshot`, with
   the reason (off, too large, expired, shell write).
3. Hashes the file on disk now. If it is not the recorded after-hash, the file
   changed after the agent's edit: `409 changed_since_edit`, nothing written.
   This is the maintainer's refusal rule, made exact.
4. Otherwise writes the snapshot bytes back with the store's atomic writer
   (temp file and rename), keeping the file's mode.
5. Records the revert on the card's row, so the card shows "Reverted" and the
   button is gone after a reload.

A revert never deletes a file. Reverting a create is refused with
`409 revert_would_delete` in the first release, because the decision was to
never discard work silently, and a delete is the one step a snapshot cannot undo.

### 3.3 Hunk-level revert

Same route with `{row, path, hunk}`. The hunk is one opcode of the diff between
the snapshot and the recorded after bytes. It is applied only when the file on
disk still equals the recorded after-hash, the same rule as 3.2, so a hunk revert
is a snapshot-derived restore of one region, not a reverse patch on whatever the
file holds now. A file that changed since the edit refuses both kinds.

### 3.4 The card

A "Revert file" button on the edit card, and "Revert" on each hunk once 3.3 lands.
A refusal shows its reason inline, in plain words. Shown only in the dashboard
chat, never in Slack or other channels (#13694 scope).

## 4. Phases

Each phase is one PR and ships alone.

| Phase | Change | Visible |
|---|---|---|
| 1 | This document | no |
| 2 | Snapshot store and limits, behind the setting | no |
| 3 | The revert route (3.2) | no |
| 4 | "Revert file" on the card | yes |
| 5 | Hunk revert (3.3) | yes |

## 5. Relation to other work

- [#33998](https://github.com/kirodotdev/KiroCrew/pull/33998) (`rfc-chat-rewind`,
  open) rewinds a whole chat to an earlier turn. A rewind that also undoes file
  edits would read the same snapshot store; this document does not depend on it.
- #8067 (`/rewind` with side-effect undo) is the turn-level cousin.

## 6. Not decided here

- Reverting a create (3.2 refuses it).
- Shell-command writes (no snapshot).
- Undo of a revert. A revert is itself an ordinary write; the snapshot of what it
  replaced is not kept in the first release.

## 7. Questions for the maintainer

1. Are the limits in 3.1 (2 MB per file, 200 MB and 14 days per session) right?
2. Off by default for the first release, or on?
3. Should the snapshot store live under the data home as proposed, or beside the
   session's history?
