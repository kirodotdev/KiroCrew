---
title: Append-Only Session Transcript — revision records instead of window rewrites
status: draft
author: zezhexu
created: 2026-08-16
last-audited: 2026-08-16
audited-at: 2a665e735
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Append-Only Session Transcript — revision records instead of window rewrites

- Status: draft — nothing implemented. All four phases are proposals.
- Author: zezhexu
- Created: 2026-08-16
- Related: `docs/system-specs/modules/history.md`,
  `docs/system-specs/modules/session.md`,
  `rfc-resumable-subagent-sessions.md` (whose Phase 0 redirected the record-store
  ladder; this RFC deliberately does not re-open model-context ownership)

## 1. Problem statement

Kiro Crew's session transcript is a JSONL file that reads like an append-only log and
is not one. The hot write path rewrites a bounded window on every flush, and two
paths can leave an already-recorded row reachable only from the archive directory.
Verified on main `2a665e735` (2026-08-16); symbols are cited rather than line
numbers, because every line reference taken from a checkout 962 commits behind had
moved.

- **The hot path re-serializes the whole window to record a one-row state change.**
  `src/kiro_crew/dashboard/chat_persistence.py::_save_slot_to_history` keeps a frozen
  prefix of `slot._disk_older_count` lines verbatim and rewrites the in-memory window.
  The module says so itself, twice, in capitals — "re-serializes the WHOLE in-memory
  window on every flush" — and sets
  `slot._disk_older_count = max(0, len(messages) - 500)`, so the window is up to 500
  rows. The changes that trigger a flush are single-row state transitions: a stop
  event moving `stopping → stopped`, a file-change chip attaching to a row, an
  mcp_oauth banner. `_flush_segment` may also reorder already-written rows.
- **Two paths can strand a recorded row in the archive.**
  `_frozen_prefix_and_foreign_appends` can drop lines that are already on disk,
  archived with `reason="foreign-dedup"`; rewrite mode (`rewrite=True`, explicit
  `messages`, or `slot._pending_rewrite`) truncates the window tail, archiving the
  dropped lines through `_archive_dropped_lines`. Archives are then hard-deleted by
  `src/kiro_crew/history.py::_cleanup_old_archives` after
  `session.archive_retention_days` (default 30), so a row can become silently
  unrecoverable after a month.
- **Other rewriting paths, for completeness.** `history.py::_maybe_rotate` drops the
  oldest rows past `_SESSION_MAX_BYTES` / `_SESSION_KEEP_LINES` and archives them with
  `reason="rotate"`. Metadata edits rewrite the whole file through `os.replace` with
  mtime restored (`update_metadata`, `set_title`, `mark_consolidated`, `clear_closed`).
  `delete_session` unlinks. `channel_transcript_migration._write_merged` merges an
  orphan transcript and atomic-writes the result.
- **Dead code advertises a capability nothing uses.** `history.py::rewrite_session`
  (with `_rewrite_session_locked`) and `history.py::sliding_window` have no production
  caller anywhere under `src/` — the only call sites are in
  `test/test_ephemeral_sessions.py`, plus a docstring reference in
  `chat_persistence.py` noting that the dashboard rewrite path and `rewrite_session`
  share one definition.

The cost is threefold: write amplification proportional to window size rather than to
the change, a data-loss class that only manifests as a missing archive, and a
transcript whose semantics cannot be stated in one sentence.

## 2. Explicit non-goal: this RFC does not move model-context ownership

This must be stated before the design, because the obvious comparison invites the
wrong conclusion.

DeepSeek Harness makes its session log the single source of truth and *derives* the
model-visible message list from it on every turn. That is why resume, fork, replay,
and compaction all reduce to one read there.

Kiro Crew cannot do this, and this RFC does not attempt it. Each turn sends **one
string**, built by `src/kiro_crew/context.py::ContextBuilder.build_message` (via
`build_session_context`) and handed to `client.stream(full_message)` in
`dashboard/chat_runner.py`. The model's actual conversation lives in kiro-cli's own
native ACP session, reached through `session/load`. Kiro Crew's JSONL is a *parallel
durable transcript*, injected back only at session start, replay, and provider switch.
Compaction likewise happens inside the provider —
`src/kiro_crew/session.py::check_context_usage` → `_compact_session` sends kiro-cli an
in-place `/compact` and watches its status; it mutates no Kiro Crew record.

Adopting DSH's derivation model would mean Kiro Crew holding the message array and
sending it in full every turn — giving up the incremental semantics of `session/load`
and creating two authorities writing the same history. That is an architecture
inversion, not a refactor.

**What is portable is one local technique: DSH's `surfaceOp` revision record.** That
is the entire scope of this RFC.

## 3. Design

### 3.1 The revision record

Instead of editing a row in place, append a revision that targets it:

```json
{"_type":"revision","target_ts":"<ts of the target row>","patch":{"cls":"…"},"ts":"…"}
```

`target_ts` is the addressing key because `history.py::monotonic_transcript_ts`
already guarantees a strictly increasing `ts` per file, making it a stable identity
that survives rotation bookkeeping better than a line index. The read path folds
revisions over base rows in file order; the last revision for a target wins.

`patch` is a shallow field merge, not a full row replacement, so a revision states
only what changed. Reordering — today done by rewriting — becomes a revision carrying
an explicit sequence hint rather than moved bytes.

A file with no revision records folds to exactly itself, so **existing transcripts
need no migration** and a new reader handles them unchanged.

### 3.2 What must not be lost

`history.py::_redact_at_write_boundary` runs on the append path for non-user roles. It
is the most valuable property of this subsystem and precisely what DSH lacks — DSH's
session-telemetry plugin ships zero record rules and its own README concedes records
leave the process "including any credentials embedded in file contents or command
output". Every revision record must pass the same boundary as a base row. This is a
hard requirement, not a follow-up.

The existing concurrency discipline is unchanged: the reentrant per-key lock (RLock +
cross-process flock) and the on-loop guard (`_check_on_loop_persist_discipline` /
`OnLoopPersistError`) continue to wrap every write.

### 3.3 Rotation stays the one rewriting path

`_maybe_rotate` remains, and becomes the *only* code that rewrites a transcript. Two
consequences:

- Rotation is the natural place to **fold revisions into their base rows**, bounding
  the read-path cost by revisions accumulated since the last rotation rather than by
  session age.
- Metadata edits still rewrite, because line 0 is a fixed-position record. That is
  acceptable — metadata is one line, transcript rows are untouched, mtime is already
  restored — and this RFC does not change it.

### 3.4 Fork, as an optional later phase

`dashboard/chat_fork.py::api_chat_slot_fork` copies messages into a new slot file. DSH
instead records a parent session id plus a seed length and reads through. Adopting
that would save the copy and make the fork tree traceable, but it only solves the
Kiro Crew half — kiro-cli's own session still has to be forked for real. Phased last
and separable; it is not required by the append-only goal.

## 4. Phases

| Phase | Change | Risk |
|---|---|---|
| **S1** | Add the revision record type and the folding read path. Writers unchanged, so the fold is a no-op on every existing file. Tests assert fold-identity on today's fixtures. | low — read path only |
| **S2** | Switch `_save_slot_to_history`'s state edits (stop transitions, chips, banners) to appended revisions. Retire `rewrite=True` for state changes; keep it only for explicit user truncation (`chat_rewind`), which continues to archive. | medium — the hot path |
| **S3** | Fold revisions at rotation. Delete the dead `rewrite_session` / `_rewrite_session_locked` / `sliding_window` (and migrate the tests that are their only callers). | low |
| **S4** | *Optional.* Reference-style fork. | medium, independent |

S1 and S3 are independently shippable. S2 is the one that pays.

## 5. Success criteria

Measurable, and to be asserted in tests rather than claimed in prose:

1. Bytes written per flush for a single-row state change drop from O(window) — up to
   500 rows — to O(1).
2. `reason="foreign-dedup"` archives are no longer produced under normal operation.
3. No row is reachable only from `sessions/archive/` outside rotation and explicit
   user rewind.
4. Fold-identity holds: for every existing fixture transcript, folding produces
   output identical to today's reader.
5. Every revision record passes `_redact_at_write_boundary`, asserted directly.

## 6. Risks

- **Read-path cost.** Folding is O(rows + revisions), bounded by §3.3's
  fold-at-rotation. Worst case before the first rotation is one revision per streaming
  state transition, the same order as today's row count.
- **Addressing by `ts`.** Relies on `monotonic_transcript_ts` holding; if two rows ever
  share a `ts`, a revision becomes ambiguous. S1 should add an invariant test that `ts`
  is strictly increasing within a file, which is worth having regardless.
- **Reordering semantics.** `_flush_segment`'s reordering is currently implicit in a
  rewrite. Expressing it as a revision makes it explicit and reviewable, but the exact
  ordering rule must be written down during S2 rather than inferred.
- **Two readers.** `history.py` and `dashboard/chat_persistence.py` both read
  transcripts. The fold must live in one place both call, or they will drift.

## 7. Alternatives considered

1. **Adopt DSH's full model** — session log as sole truth, model history derived.
   Rejected in §2: Kiro Crew does not own the model context, kiro-cli does.
2. **Leave the rewrite path and only fix the dedup drop.** Cheaper, and it removes one
   data-loss class, but it leaves the write amplification and leaves the transcript's
   semantics unstatable. Available as a fallback if S2 proves too invasive.
3. **Move the transcript to SQLite.** Solves in-place edits by making them legal rather
   than unnecessary, and DSH offers exactly this as a swappable provider (JSONL ↔
   SQLite). Rejected for now: it trades a greppable, tail-able, human-readable artifact
   — load-bearing for support and for log-style debugging — for a property that
   appended revisions already deliver.
4. **Event-source the whole subsystem** with a typed event enum in the style of DSH's
   ~48 known session event types. Larger than the problem. Kiro Crew already has
   role-typed rows (`history.py::_TOOL_ROLES` covering `tool`/`tool_call`/`tool_result`,
   alongside `user`, `assistant`, `system`, `error`, `notice`, `inject`, `subagent`) plus
   `chat_persistence.py::_TRANSIENT_ROLES` that are never persisted. The gap is
   mutability, not vocabulary.
