---
title: Revert an agent's file edit from its card in dashboard chat
status: draft
author: RohanK6
created: 2026-11-11
last-audited: 2026-11-20
audited-at: f8a805aaeb
doc-pr: null
implementation-prs: []
tracking-issues: [13694]
supersedes: []
superseded-by: []
---

# RFC: Revert an agent's file edit from its card in dashboard chat

- **Status:** Draft. Acceptance requested from a maintainer; the status flips to
  `accepted` when one records it on this line.
- **Verified at:** `f8a805aaeb`. Every code reference below was read at that commit.
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

- **Not the display snapshot's capture point.** `_snapshot_write_target` runs when
  the dashboard handles the tool-call event (`chat_turn/tool_rows.py`), and its
  docstring says that on the auto-approved path the write has already landed by
  then. There the before is the ACP diff `oldText`, folded to LF by
  `_lf_newlines` and capped by `_truncate_snapshot`; for `str_replace` it is
  rebuilt from the file after the write by `_classify_str_replace_before`, which
  can come back undecidable (`pending_str_replace`). None of those is the bytes
  the file held, so none of them may feed a restore.
- **When:** only where the write has provably not run yet: in
  `_on_permission_request` (`chat_turn/tool_approval.py`), while the agent's
  `session/request_permission` for a file-write tool (create, overwrite,
  `str_replace`) is still unanswered. That holds whatever answers the request: the
  person, session trust, a trusted pattern or YOLO. The bytes are read from disk
  there, with no decoding, no newline folding and no cap below the per-file limit,
  together with their sha256 and the file's canonical path.
- **Re-hashed just before the grant.** For a request a person answers, the grant
  is not sent where the bytes were read. `_on_permission_prompt`
  (`chat_turn/permission_prompt.py`) waits for the card's answer
  (`asyncio.wait_for`, up to the approval window) and only then calls
  `client.approve_tool`, so an edit made while the card is open (the person's
  editor, a formatter) is not in the stored before. Immediately before each
  `client.approve_tool` that grants a file write, the file is hashed again. If
  the hash is not the one read, the snapshot is dropped and the index records
  `source: none` with the reason `changed_before_grant`. A limit stays: a change
  between that last hash and the agent's own write cannot be seen from the
  gateway (section 6).
- **Which writes get a snapshot, and which do not.** A write that reaches a
  permission request gets one. A write the agent runs without asking (a tool its
  own config trusts, the auto-approved path above), a shell write, and a write
  whose path `validate_file_path` or `is_sensitive_write_path` refuses get none:
  the index records the write
  with `source: none` and the reason, and the route answers `404 no_snapshot`
  with that reason. Nothing derived from `oldText`, a rebuild or the display
  snapshot is ever stored. Shell writes are left out because the card for a
  shell command is not a file edit card today, and one command can touch any
  number of files.
- **What:** the exact before bytes, or a marker that the file did not exist (a
  create; see 3.2), plus the sha256 of the bytes the write left on disk, taken
  when that call's completed result arrives, not at the end-of-turn flush. A
  later change to the same file in the same turn (another agent write, the
  person's editor, a formatter) therefore no longer matches the after-hash, and
  a revert refuses it instead of discarding it. A call whose completed result
  never arrives gets no after-hash and no revert. The file's device and inode are
  read at that same moment, from the file the write left, not from the pre-write
  read: an agent write tool may replace the file by renaming a new one over it,
  which gives it a new inode. The after-hash, device and inode are one record and
  are always replaced together.
- **Where:** a per-session store under the data home,
  `~/.kiro/crew/edit_snapshots/<session id>/<sha256>.bin`, content-addressed, with
  a small index file mapping `(turn row id, path)` to `(source, before sha256,
  after sha256, canonical path, after device, after inode, created at)`, where
  `source` is `permission_request` or `none`.
  Owner-only like the other session stores. It is never inside the project
  directory, so a snapshot never shows up in git status.
- **Sandbox disposition: `HIDDEN`.** Only the gateway reads and writes the store;
  nothing inside the agent's sandbox needs it. If the agent could write the index
  or a `.bin`, it could plant both the after-hash and the bytes the gateway later
  writes back outside the sandbox. Phase 2 adds the leaf to
  `sandbox_leaves.py` as `HIDDEN`, and to the crew-home half of
  `security.sensitive_home_dirs()`, in the same commit, so
  `test_sandbox_governance_mask.py` keeps them equal.
- **Limits:** one file up to 2 MB (the same bound `_turn_after_reads` reads to);
  a larger file gets no snapshot and its card says why there is no revert. A
  session keeps at most 200 MB and 14 days of snapshots, oldest first evicted.
  These are the defaults; all three are settings under `chat.edit_revert`.
- **Off by default** in the first release (`chat.edit_revert.enabled = false`),
  so nothing is written to disk for anyone who has not opted in.

### 3.2 The revert

`POST /api/chat/slots/{slot}/file-revert` with `{row, path}`:

1. Owner-only through the same gate the other chat write routes use, and audited
   in the SEL with the path and the outcome.
2. Reads the index for that row and path. No snapshot: `404 no_snapshot`, with
   the reason (off, too large, expired, shell write, written without a
   permission request, no completed result).
3. Re-checks the target before any hash or write:
   - `409 target_moved` when the canonical path now differs from the one recorded
     at snapshot time, when the device or inode differs from the after device and
     inode recorded with the current after-hash, or when any component
     of the path is a symbolic link. A folder the agent swapped for a link after
     its edit would otherwise send the write somewhere the snapshot never named.
   - `403 path_refused` when `is_sensitive_write_path` (`security/paths.py`)
     refuses the path. That is the check the file-edit tool gate applies, and it
     covers the write-protected list (for example `~/.kiro/agents`) that
     `validate_file_path` (`hook_runtime/safe_reads.py`) does not: that function
     canonicalizes with `realpath` and applies only `is_sensitive_path`. So a
     revert never writes to a path the file-edit tool gate refuses.
4. Hashes the file on disk now. If it is not the recorded after-hash, the file
   changed after the agent's edit: `409 changed_since_edit`, nothing written.
   This is the maintainer's refusal rule, made exact.
5. Otherwise writes the snapshot bytes back with the store's atomic writer
   (temp file and rename), keeping the file's mode. `atomic_write`
   (`atomic_write.py`) renames a new temp file over the target, so the file has a
   new inode afterwards. The route therefore reads the hash, device and inode of
   the file it wrote and stores them as the row's new after record before it
   answers.
6. Records the revert on the card's row, so the card shows "Reverted" and the
   button is gone after a reload.

A revert never deletes a file. Reverting a create is refused with
`409 revert_would_delete` in the first release, because the decision was to
never discard work silently, and a delete is the one step a snapshot cannot undo.

### 3.3 Hunk-level revert

Same route with `{row, path, hunk, after}`. The hunk is one opcode of the diff
between the snapshot and the bytes whose sha256 the request names in `after`;
the card draws its hunk list from that pair and sends the hash with every hunk.
The route applies it only when `after` is the index's current after-hash and the
file on disk still hashes to it, the same rule as 3.2, so a hunk revert is a
snapshot-derived restore of one region, not a reverse patch on whatever the file
holds now. A request drawn from an older pair gets `409 stale_hunk`; a file that
changed since the edit refuses both kinds with `409 changed_since_edit`.

A hunk revert is itself a write the gateway made, so once it lands the route
stores the hash, device and inode of the file it wrote as the row's new after
record (3.2 step 5), and returns the new hunk list. The next hunk, or a
whole-file revert, on the same card is then checked against that record and goes
through; a change anyone else made in between still refuses it.

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
- Shell-command writes, and writes the agent runs without a permission request
  (no snapshot; see 3.1).
- A change made between the last hash before the grant and the agent's own write.
  The gateway does not run that write and cannot see it, so such a change is
  folded into the after-hash and a revert discards it. The window is the time the
  agent takes to act on the grant; closing it needs the agent's write to go
  through the gateway, which this RFC does not propose.
- Undo of a revert. A revert is itself an ordinary write; the snapshot of what it
  replaced is not kept in the first release.

## 7. Questions for the maintainer

1. Are the limits in 3.1 (2 MB per file, 200 MB and 14 days per session) right?
2. Off by default for the first release, or on?
3. Should the snapshot store live under the data home as proposed, or beside the
   session's history?
4. With revert on, should the agent's file-write tools always go through a
   permission request (answered by trust as today), so every agent file edit gets
   an exact snapshot? This RFC does not change which tools ask; without it, an
   edit the agent runs without asking has no revert (3.1).
