---
title: Revert an agent's file edit from its card in dashboard chat
status: draft
author: RohanK6
created: 2026-11-11
last-audited: 2026-11-24
audited-at: 3d36b459fd
doc-pr: null
implementation-prs: []
tracking-issues: [13694]
supersedes: []
superseded-by: []
---

# RFC: Revert an agent's file edit from its card in dashboard chat

- **Status:** Draft. Acceptance requested from a maintainer; the status flips to
  `accepted` when one records it on this line.
- **Verified at:** `3d36b459fd`. Every code reference below was read at that commit.
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

- **Not the display snapshot's capture point.** `_snapshot_write_target` runs (from
  `_snapshot_tool_writes` in `chat_turn/file_change_shapes.py`) when the dashboard
  handles the tool-call event (`chat_turn/tool_rows.py`), and its
  docstring says that on the auto-approved path the write has already landed by
  then. There the before is the ACP diff `oldText`, folded to LF by
  `_lf_newlines` and capped by `_truncate_snapshot`; for `str_replace` it is
  rebuilt from the file after the write by `_classify_str_replace_before`, which
  can come back undecidable (`pending_str_replace`). None of those is the bytes
  the file held, so none of them may feed a restore.
- **When:** only where the write has provably not run yet: in
  `_on_permission_request` (`chat_turn/tool_approval.py`), while the agent's
  `session/request_permission` for a file-write tool is still unanswered. The
  tools are kiro-cli's `create`, `strReplace` and `insert` commands
  (`_WRITE_COMMANDS` in `chat_turn/file_changes.py`) and its commandless
  `fs_write`, `fs_append` and `str_replace` (`_snapshot_commandless_write`, same
  file), so an insert or an append gets a snapshot like any other edit. Another
  harness's write shapes (`_HARNESS_WRITE_SHAPES` in
  `chat_turn/file_change_shapes.py`) get none in the first release: `source: none`,
  reason `unsupported_shape`. That holds whatever answers the request: the
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
  number of files. How many edits keep a revert depends on which tools the
  agent's config lets run without a permission request, and this RFC has not
  measured it. Phase 2 reports the count of write calls by `source` and reason,
  which the index already records, so phases 3 to 5 can be judged on real
  numbers (question 4).
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
- **Keyed by tool call, bound to a row at flush.** The reply row is not known
  when the bytes are read: `_flush_file_changes` attaches the turn's file changes
  to the turn's last assistant message at turn end. So each snapshot is keyed by
  its tool call id, the identity `_record_turn_snapshot` already takes as its
  writer key (`_tcid_identity_key` in `chat_turn/tool_rows.py`), and at flush the
  index gains `(turn row id, canonical path)` pointing at that path's call ids in
  order.
- **Two writes to one file in a turn.** The card's diff already runs from the
  turn's first before to its last after (`_record_turn_snapshot` keeps the first
  before). The revert follows the same rule: it restores the first call's before
  bytes and is checked against the last call's after record, so there is one
  record per row and path, never two competing ones. That holds only while the
  chain does: each later call's before sha256 (read at its permission request and
  hashed again before its grant) equals the previous call's after sha256, and
  every write call the display record saw for that path has a snapshot. A break
  (a change between the two writes, or a write the agent ran without asking)
  records `source: none` for that row and path with the reason `chain_broken`,
  and the route answers `404 no_snapshot`.
- **Where:** a per-session store under the data home,
  `~/.kiro/crew/edit_snapshots/<session id>/<sha256>.bin`, content-addressed, with
  a small index file mapping a tool call id to `(path, source, before sha256,
  after sha256, canonical path, after device, after inode, created at)`, where
  `source` is `permission_request` or `none`. Owner-only like the other session
  stores. It is never inside the project directory, so a snapshot never shows up
  in git status.
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
- **Eviction.** Two edits that start from the same bytes share one
  content-addressed blob, so eviction drops index rows, oldest first, and deletes
  a blob only when no remaining row refers to it. A row whose blob is gone, or
  whose bytes do not hash to the blob's name, answers `404 no_snapshot` with the
  reason `expired`; the route never writes anything else in its place.
- **Off by default** in the first release (`chat.edit_revert.enabled = false`),
  so nothing is written to disk for anyone who has not opted in.

### 3.2 The revert

`POST /api/chat/slots/{slot}/file-revert` with `{row, path}`:

1. Owner-only through the same gate the other chat write routes use, and audited
   in the SEL with the path and the outcome.
2. Reads the index for that row and path. No snapshot: `404 no_snapshot`, with
   the reason (off, too large, expired, shell write, written without a
   permission request, unsupported shape, changed before the grant, chain
   broken, no completed result).
3. Refuses while a turn can still write: `409 turn_running` when `slot.running`
   or `slot.linked_turn_running` (`dashboard/state_parts/chat_slot.py`).
   Otherwise it sets `slot._turn_admission_reserved` until the route ends, as
   `dashboard/chat_api/turn_stop.py` does around its own work, so a message sent
   meanwhile queues (`dashboard/chat_api/chat_send.py`) instead of starting a
   turn.
4. Refuses where the write cannot be pinned: `501 unsupported_platform` when
   `supports_pinned_walk()` (`pinned_fs_primitives.py`) or
   `pinned_parent_replace_supported()` (`atomic_write.py`) is false. The route
   does not fall back to writing by name, which is the fallback the dashboard's
   own file save takes (`_file_write_blocking` in
   `dashboard/file_api/pinned_io.py`): a revert is a write the person did not
   type, made outside the sandbox.
5. Gates the recorded canonical path with `is_sensitive_write_path`
   (`security/paths.py`): `403 path_refused` when it refuses. That is the check
   the file-edit tool gate applies, and it covers the write-protected list (for
   example `~/.kiro/agents`) that `validate_file_path`
   (`hook_runtime/safe_reads.py`) does not: that function canonicalizes with
   `realpath` and applies only `is_sensitive_path`. So a revert never writes to a
   path the file-edit tool gate refuses.
6. Pins the directory and checks the open file, with no step that resolves the
   path by name again. It walks the recorded canonical parent with `pin_parent`,
   one `O_NOFOLLOW` open per component, and opens the leaf relative to that
   descriptor (`open_access_control_source(path, dir_fd=...)`).
   - `409 target_moved` when a component is now a link, or when the open leaf's
     device or inode is not the after device and inode of the row's current
     after record. A folder swapped for a link, or a file replaced by rename,
     after the edit is caught here. A pinned descriptor cannot be re-pointed, so
     nothing later in the route follows a swapped folder.
   - `409 changed_since_edit` when the bytes read from the open descriptor do
     not hash to the recorded after-hash: the file changed after the agent's
     edit, nothing written. This is the maintainer's refusal rule, made exact.
7. Writes through the same pinned directory with
   `atomic_write(path, snapshot bytes, mode=<the open file's mode>,
   preserve_access_control_from=<the open leaf>, parent_dir_fd=<the pinned
   parent>, on_staged=<check>)`, the transaction `_file_write_blocking` already
   runs for the dashboard's file save, plus the hook. `on_staged` runs once the
   replacement is staged and before the rename (`atomic_write.py`), and an
   exception in it aborts the write with nothing replaced. The check hashes the
   open leaf again, which catches an in-place write, and compares the open
   descriptor's device and inode with `os.stat` of the leaf name relative to the
   pinned parent, which catches another file renamed over the name. Either one
   answers `409 changed_since_edit`. The staged descriptor also gives the device
   and inode of exactly the file that will appear, which a stat of the path
   afterwards could not (another writer may have replaced it by then).
8. Stores the hash of the bytes it wrote, with the staged file's device and
   inode, as the row's new after record, and records the revert on the card's
   row, so the card shows "Reverted" and the button is gone after a reload.

A revert never deletes a file. Reverting a create is refused with
`409 revert_would_delete` in the first release, because the decision was to
never discard work silently, and a delete is the one step a snapshot cannot undo.

### 3.3 Hunk-level revert

Two calls on the same route. `{row, path, list: true}` writes nothing and returns
the hunk list, once the file on disk still hashes to the row's after-hash
(`409 changed_since_edit` otherwise). `{row, path, hunk, after}` reverts one hunk. A hunk is one opcode of
the diff between the snapshot bytes and the bytes whose sha256 the request names
in `after`.

**What the hunk list carries.** The server builds it from the snapshot and the
current bytes, never from the persisted display meta: that diff is redacted,
folded to LF and windowed (`_prepare_file_changes`, `_excerpt_pair`), so its
ranges do not line up with the real bytes. The list holds, per hunk, a
server-side hunk id (bound to the after-hash and the opcode's index), the before
and after line ranges, and the text of each side after `security.redact`, cut
like the display text (`_MAX_SNAPSHOT`). The restore bytes never reach the
browser. Redaction runs on the whole before and the whole after first, and a
hunk's text is taken from the redacted lines at the same line numbers. If
redaction changes a side's line count (a multi-line secret), the file gets
whole-file revert only, with no hunk list and the reason
`redaction_changed_lines`, so a secret is never shown cut in half at a hunk
edge.

The route applies a hunk only when `after` is the row's current after-hash and
the file passes every check in 3.2 steps 3 to 7, so a hunk revert is a
snapshot-derived restore of one region, not a reverse patch on whatever the file
holds now. A request drawn from an older list gets `409 stale_hunk`; a file that
changed since the edit refuses both kinds with `409 changed_since_edit`.

A hunk revert is itself a write the gateway made, so once it lands the route
stores the hash, device and inode of the file it wrote as the row's new after
record (3.2 step 8), and returns the new hunk list. The next hunk, or a
whole-file revert, on the same card is then checked against that record and goes
through; a change anyone else made in between still refuses it.

### 3.4 The card

A "Revert file" button on the edit card, and "Revert" on each hunk once 3.3 lands,
drawn from the list the route returns when the person opens the hunk view.
A refusal shows its reason inline, in plain words. Shown only in the dashboard
chat, never in Slack or other channels (#13694 scope).

## 4. Phases

Each phase is one PR and ships alone.

| Phase | Change | Visible |
|---|---|---|
| 1 | This document | no |
| 2 | Snapshot store and limits, behind the setting; counts write calls by `source` | no |
| 3 | The revert route (3.2) | no |
| 4 | "Revert file" on the card | yes |
| 5 | Hunk revert (3.3) | yes |

## 5. Relation to other work

- [#33998](https://github.com/kirodotdev/KiroCrew/pull/33998) (`rfc-chat-rewind`,
  merged 2026-11-20 as a draft) rewinds a whole chat to an earlier turn. Its P2
  sketches an optional undo of the agent's own file edits and says it needs an
  undo-grade companion for each write: the exact prior bytes and a hash of what
  the agent wrote, kept outside the transcript. This store is that companion. If
  P2 is ever accepted it reads this store instead of adding a second one, and it
  inherits the rules here: exact bytes only, `404 no_snapshot` for every other
  write, refusal when the file changed since the edit. This document does not
  depend on P2.
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
- The window left in the revert itself (3.2 steps 6 and 7). The route refuses
  while the slot's own turn runs and holds its admission, so that writer is gone.
  A write by another process (another chat, a sub-agent session, the person's
  editor, a shell) that lands between the `on_staged` check and the rename is
  overwritten. Nothing runs between those two, so the window is the rename
  itself, but the gateway cannot lock out a writer it does not control. A turn
  that a channel or cron job starts on a linked session during the route is the
  same case. Closing it would need a lock every writer honours, which this RFC
  does not propose.
- Windows, and any platform without descriptor-relative open and rename: the
  revert route answers `501 unsupported_platform` (3.2 step 4). Question 5 asks
  whether that is acceptable for the first release.
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
   edit the agent runs without asking has no revert (3.1), and how many edits
   that is has not been measured. Phase 2 counts it, and that number decides
   whether phases 3 to 5 are worth building.
5. Is it acceptable that revert is unavailable, in the first release, on a
   platform where the write cannot be pinned to a directory (Windows)? The
   alternative is the by-name fallback the dashboard's file save uses, which
   reopens the swapped-folder window (3.2 step 4).
