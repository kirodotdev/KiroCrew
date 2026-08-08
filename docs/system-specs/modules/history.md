# Conversation History Module

## Overview

Persistent conversation history with provenance tracking and LLM-driven consolidation. Conversations survive session expiry and gateway restarts.

## ConversationLog (`history.py`)

Per-thread JSONL files at `~/.kiro/crew/sessions/{safe_key}.jsonl`. First line is metadata, subsequent lines are messages with `role`, `content`, `ts`, `tools`, `source_thread`, `source_user`.

- Append-only for LLM cache efficiency
- Rotation at 2MB (keeps metadata + last 200 messages, atomic write)
- `recent(key)` — last 20 messages for context injection
- `recent_with_provenance(key)` — entries with source citations
- `list_sessions()` — lists all sessions with title (first user message or LLM-generated). Sort key uses ISO `created` string consistently (defaults to ISO from `st_mtime` if no metadata `created` field, ensuring string-only comparisons). Each returned session's meta dict also carries `folder_id` when present in the persisted metadata line, so sessions can be grouped by the folder they were filed in.
- `agent_usage()` — returns `{agent_name: (session_count, last_used_mtime)}`; built on `list_sessions()` so it inherits canonical-session dedup + symlink-skip (counts per logical conversation). Used by `GET /api/agents` to order the roster most-used-first, degrading to config order on failure.
- `search_sessions(query, limit=50)` — case-insensitive substring content search over the newest `_SEARCH_SCAN_WINDOW` session JSONL files. Counts all occurrences per session (length-normalized) to rank by relevance, then caps to `limit` results. Exposed via `GET /api/sessions/search?q=<q>&limit=<n>` (min 2 chars); used by the dashboard history filter to find sessions by content (CR ids, error messages, file paths) rather than title alone. Returns the same meta dicts as `list_sessions()`, so each search hit likewise carries `folder_id` (when present), letting the sidebar group results by folder.
- `delete_session(key)` — permanently removes a session JSONL file

### MCP chat-history tools (`mcp_core.py`)

These read-only tools expose the session store to the agent and are all
workspace-scoped by default (fail-closed via `_caller_workspace`/`_ws_bucket`,
`all_workspaces` opts out), exclude incognito/temporary sessions (canonical
`INCOGNITO_MEMORY_MODES` in `history.py`), and redact their output:

- `search_chat_history` — keyword lookup over past transcripts (ranked snippets).
- `get_chat_session` — read one full transcript by `session_key`.
- `list_sessions` — browse/overview counterpart to search: returns recent
  sessions newest-first (title, owning agent, message count, timestamps) built
  on `ConversationLog.list_sessions()`, with `limit` (default 20, max 100).
  Opt-in `summarize=true` calls `POST /api/sessions/summarize` to attach a fresh
  one-line LLM summary per session — MCP core has no LLM access, so the LLM leg
  runs gateway-side on an ephemeral background session (cheap Haiku model),
  bounded to 8 sessions and best-effort (falls back to the title on any failure).
  A generated summary is cached in a **sidecar file** (`sessions/.summaries/`),
  never in the session JSONL, keyed by the session file mtime — so summarizing an
  active session never rewrites (and cannot clobber a concurrently-appended
  message in) its log, and a repeat call for an unchanged session pays zero LLM
  cost. A new message advances the mtime and invalidates the cache. Because the
  session log is untouched, `list_sessions(summarize=true)` remains a true read of
  conversation history (`get_cached_summary` / `set_cached_summary` in
  `ConversationLog`). The gateway-side one-liner
  generation uses the shared `llm_helpers.run_bg_oneliner` helper (the same
  acquire→drive→destroy skeleton as title / link-label / folder-icon generation).

### Foreign-agent session import

The first-run importer accepts session history from Codex, Claude Code,
MeshClaw, OpenClaw, and Hermes. It projects each selected conversation to
**visible user and assistant text only**. Hidden reasoning, tool calls and tool
results, system messages, raw instructions, provider session identifiers,
approval state, and other runtime metadata are not copied.
Known non-text record/content envelopes are excluded as whole units even when a
foreign store labels them with a user/assistant role or places visible-looking
text in their content field.

Claude transcript records marked as metadata, sidechain activity, tool-use
results, or a non-external user type are excluded as whole records even when
they contain visible-looking text. Workspace discovery collects every valid
scalar cwd/project field from a record and every current Codex
`payload.workspace_roots[]` entry; one record is not reduced to its first path.

OpenClaw JSONL is considered only under `agents/<agentId>/sessions` and only
when the sibling `sessions.json` has one unambiguous entry resolving to that
file. The entry must have `createdVia` operator/channel/talk, a human
`createdActor`, no parent/spawn/runtime/plugin/fork ownership, and a key outside
the cron, subagent, ACP/bridge, hook, node, heartbeat, and internal-effects
namespaces. Trajectory/checkpoint artifacts and deleted/reset archives are
diagnosed and excluded. Canonical `agents/<agentId>/agent/openclaw-agent.sqlite`
stores are safety-checked and diagnosed as unsupported; their sessions are not
partially projected.

Hermes SQLite import requires both `sessions` and `messages`, joining
`messages.session_id` to `sessions.id`. Accepted sessions have a nonempty source
other than subagent/tool/cron and a null `parent_session_id`; parented/runtime
lineage is diagnosed, and only accepted sessions contribute workspaces. Message
projection remains visible user/assistant text only and honors the current
`active`/compacted marker. A legacy messages-only database has no sufficient
provenance and is diagnosed rather than guessed.

Imported conversations are persisted through `ConversationLog` under generated,
closed destination keys. They enter the normal History list but do not create
live dashboard slots, resume a foreign runtime, or reuse a foreign identifier as
an executable KiroCrew session key. The normal ConversationLog metadata/message
schema, rotation, path sanitization, and retention behavior therefore remain
authoritative.

Import is merge-only and idempotent. A durable provenance ledger binds the
foreign source and stable source-item identity to the generated destination key;
re-applying the same item is reported as already imported instead of appending a
duplicate conversation. The foreign session tree is read-only throughout scan
and apply and is never rewritten, moved, or deleted.
The existence check, interrupted-prefix repair, append, and rollback for one
destination session run under the same `ConversationLog._locked` critical
section, so concurrent imports cannot interleave transcripts or record a
partial session as complete.

Bounded JSONL parsing never emits a partial conversation: reaching a file line
or line-byte limit excludes every conversation projected from that file, and
reaching a per-session visible-message limit excludes that session while allowing
other complete sessions in the file. A malformed JSONL record likewise excludes
the whole file, including workspace paths observed in its otherwise valid prefix.
Each exclusion is reported by its limit reason. Within one source, mirrored
identical normalized visible transcripts collapse to one import candidate, but
the retained candidate keeps its stable source-item identity rather than deriving
identity from its transcript. A growing source session therefore remains tied to
the same provenance ledger entry.

## Dashboard History Persistence — Frozen Prefix + Live Window (`dashboard/chat_persistence.py`)

`_save_slot_to_history` persists dashboard chat slots. It models the session
file as a **frozen prefix + live window** so on-disk history is never
overwritten or truncated — a slot that restored only the last ~500 messages can
no longer destroy older turns.

- **Frozen prefix**: the first `slot._disk_older_count` on-disk message lines —
  the turns OLDER than the in-memory window (set at restore/resume/rehydrate
  from `len(disk) - window`). These bytes are read verbatim and NEVER rewritten.
  They are cached on the slot keyed by `(file-mtime, _disk_older_count)` so a
  steady 5s flush is O(window), not O(file size).
- **Live window**: all of `slot.messages` (small, bounded by the 10000-message
  cap). It is **re-serialized in full on every save**. Re-serializing the whole
  window is what makes in-place edits (stop-event resolution `stopping→stopped`,
  file-change chips, mcp_oauth banner completion) and any reordering done by
  `_flush_segment` (which moves a trailing `stop_event` to land AFTER the
  finalized assistant reply) persist correctly — there is no fragile position
  counter to drift.
- **Default save** (flush loop, close, folder/tag/title changes) writes
  `metadata + frozen_prefix + serialize(window)`. It is always a superset of
  what is on disk, so it archives nothing and skips the O(file) diff read.
- **`slot._disk_window_len`**: count of window messages the last save wrote to
  disk. Memory trimming (`_MAX_SLOT_MESSAGES`) may fold a leading window message
  into the frozen prefix (`_disk_older_count += …`) only for messages actually
  persisted (`min(excess, _disk_window_len)`); an unpersisted overflow is logged
  rather than silently counted as on-disk.
- **Single-file only**: the save touches `_path(history_key)` and never reads or
  writes sibling files. `tab_id` is 1:1 with a file (fork creates a fresh slot
  with its own file), so chaining is untouched and legacy no-tab_id sessions are
  never merged with unrelated sessions.
- **Tail-only fork** (`direction="tail"`): copies only `visible[at_index+1:]`
  into the new slot instead of the head `visible[:at_index+1]`. The head is
  always dropped -- there is no summarize option. Gated server-side by
  `dashboard.tail_fork_enabled`; if the gate is off, a `direction="tail"`
  request falls back to a normal head-fork instead of erroring. The source
  slot's history file is untouched, so the head stays archived in the parent.
- **Concurrency**: `_flush_dirty_slots` runs the save in an executor thread while
  `_run_chat` mutates `slot.messages` on the event loop. `slot._lock` is an
  asyncio lock (unusable from the thread), so the save instead takes a
  consistent snapshot: it reads `_disk_older_count`, snapshots
  `list(slot.messages)`, and re-checks `_disk_older_count` (bounded retry) so a
  concurrent trim cannot interleave with the read-serialize-write.
- **Cross-process lock (`_locked`)**: `_save_slot_to_history` holds the session's
  cross-process `_locked` (the SAME lock `append` / `append_off_loop` / rotate /
  rewrite / metadata edits take) across its metadata read, frozen-prefix read,
  archive diff, and `atomic_write`. Without it a concurrent `append_off_loop`
  (e.g. a workflow/cron result appended to the originating dashboard session)
  could land between the save's file snapshot and its file-replacing
  `atomic_write`, silently deleting the acknowledged append. On the event loop
  `_locked` makes ONE non-blocking acquire and raises `HistoryLockTimeout` under
  contention rather than blocking the loop — so **on-loop callers MUST offload**:
  `save_slot_off_loop(state, slot, …)` dispatches the save to a worker thread so
  it takes the patient off-loop acquire path. It is `best_effort=True` by default
  (a lock timeout / I/O error is logged, not raised — the in-memory slot is the
  source of truth and the periodic flush retries); archival paths that must
  confirm the durable write before removing the session (session close/cleanup)
  pass `best_effort=False` so the exception propagates and the caller rolls back.
  Off-loop callers (`_flush_dirty_slots`, `save_all_slots_to_history` at
  shutdown) call `_save_slot_to_history` inline — off the loop `_locked` polls
  patiently to a bounded deadline. The same discipline applies to every other
  session-JSONL writer: `clear_closed` (resume un-flags `closed` under `_locked`,
  offloaded via `asyncio.to_thread`) and all `history.py` mutators hold `_locked`.
- **Turn persistence is offloaded through ONE choke point**
  (`save_conversation_turn_off_loop`, `llm_helpers.py`): `save_conversation_turn`
  makes TWO `append` calls, so an on-loop caller pays ~24 ms of loop time per turn
  AND takes `_locked`'s single non-blocking acquire — dropping the durable copy
  exactly when another writer is active. Every async caller (the Slack handler,
  gateway, and transport dispatch) awaits the choke point rather than restating
  the offload, and `test_persist_off_loop.py` is an AST build gate that fails if
  any `async def` body calls `save_conversation_turn` directly. Unlike
  `append_off_loop`, the choke point **awaits** the write: its callers go on to
  refresh a dashboard tab or hand the session to consolidation, both of which read
  the transcript back.
- **A turn is an atomic PAIR, and offloading is what makes that need saying.**
  `append` locks per ROW, so two concurrent turn-writes for one session can land
  as `user_A, user_B, assistant_A, assistant_B` — turns that no longer pair up,
  and which no ordering pass can repair because every row's `ts` is individually
  correct. On the event loop this was impossible: a synchronous
  `save_conversation_turn` never yields between its two appends, so the
  single-threaded loop made the pair atomic *by accident*. Moving the write to a
  worker thread removes exactly that accidental guarantee. So
  `ConversationLog.atomic_appends(key)` is the required companion to the offload,
  not an optional extra: **any caller that offloads MULTIPLE appends for one
  session must hold it around the whole group.** `_locked` is reentrant for the
  same key on the same thread, so the per-row locks inside `append` reuse the
  held lock. Enter it off the loop only — it takes the same fail-fast-on-loop
  acquire path as `append`.
- **Row ordering has two writers with different floor sources.** Both
  `ConversationLog.append` and `_ChatSlot.append` stamp each row strictly after
  its predecessor via `monotonic_transcript_ts`, so a `ts` sort reproduces write
  order even on a host whose clock cannot separate two writes (Windows ticks in
  ~15.6 ms steps). They learn about that predecessor differently, and the
  asymmetry is deliberate:
  - `ConversationLog.append` reads the authoritative on-disk tail (`_last_row_ts`)
    under the cross-process flock, so it sees every committed row.
  - `_ChatSlot.append` runs on the event loop, where a `stat` plus a tail read per
    append would violate the no-blocking-call-on-event-loop rule. It floors on
    `latest_transcript_ts(window_tail, slot._disk_tail_ts)` — both in-process
    reads. `_disk_tail_ts` is refreshed at the save boundary, inside the `_locked`
    section where the foreign lines are already parsed, so it costs nothing.

  The window is NOT a superset of the file: a genuinely foreign on-disk row is
  preserved without being folded into `slot.messages`, so without the cached tail
  the slot's next row could TIE it. A foreign row arriving *between* two saves is
  still invisible until the next one — the reachable shape (a subagent/cron append
  observed at the following flush) is closed, the general case is not, and that
  bound is intentional rather than an oversight. The floor is monotone by
  construction: `latest_transcript_ts` only ever selects a *later* candidate, so it
  can move a row forward but never backward. It **skips** candidates it cannot
  parse, because `transcript_sort_key` deliberately buckets unparseable values
  AFTER every real instant (right for display order, backwards for a floor) — one
  corrupt row would otherwise win the comparison, be discarded by the stamper as
  unparseable, and switch the ordering guarantee off for that session.
- **On-loop offload discipline is enforced, not convention-only**: the offload
  invariant above was previously guaranteed only by convention — a future
  contributor calling a raw mutator (`append` / `update_metadata` / `set_title`
  / `delete_session` / `_save_slot_to_history`) from an async handler would get
  a write that works in every uncontended test yet silently drops under real
  contention (the on-loop `HistoryLockTimeout` swallowed by a best-effort
  `try/except`), invisible in CI. `_locked` now calls
  `_check_on_loop_persist_discipline(key)` on entry: if a running event loop is
  detected it either **raises `OnLoopPersistError`** (strict mode — on under
  `KIROCREW_STRICT_ON_LOOP_PERSIST=1` or `KIROCREW_DEV_MODE`) so an un-offloaded
  call-site fails tests rather than losing data, or emits a **loud throttled
  warning** and proceeds via the single non-blocking safety-net acquire
  (default / production gateway, strict off — never a new hard failure in the
  field). Strict is deliberately NOT auto-on under bare pytest (the suite's own
  async harness calls several mutators directly on the loop as a convenience, so
  auto-strict would flag harness code, not drift); the enforcement tests flip
  the env flag explicitly. Off the loop the check is a no-op (the sanctioned
  path). Tests that deliberately drive the low-level on-loop primitive wrap the
  call in `history.allow_on_loop_persist()` (a `ContextVar`-scoped bypass);
  production code must NEVER use it. **Considered-and-deferred alternative — a single-writer
  queue:** funnel every session-file mutation through one dedicated writer thread
  (or per-key `asyncio.Queue` drained off-loop) so the loop never touches
  `_locked` at all and no caller can bypass the discipline structurally. It was
  deferred because it reshapes every mutator into an async enqueue (touching the
  same ~15 call-sites plus the synchronous CLI/subagent/cron writers that must
  stay inline), serializes unrelated keys unless sharded, and complicates the
  close/cleanup paths that need a confirmed durable write (`best_effort=False`).
  The refcounted `_flock_state` + the strict on-loop guard give most of the
  safety at a fraction of the churn; the single-writer queue is the intended
  escape hatch if the guard's warn-and-proceed production fallback ever proves
  insufficient (e.g. a hot on-loop path that must not be lost).
- **Rewrite path** (`rewrite=True`, an explicit `messages` snapshot, or a slot
  left in `_pending_rewrite` — rewind/regenerate/fork): writes
  `metadata + frozen_prefix + serialize(snapshot)`. These INTENTIONALLY drop the
  post-edit window tail, so the dropped lines are archived first via
  `_archive_dropped_lines` → `_archive_lines` (the frozen prefix appears
  unchanged in both old and new, so it is never archived). `_pending_rewrite` is
  set by rewind/regenerate after they truncate the window and cleared only on a
  successful rewrite save, so a failed inline rewrite still gets retried as an
  archive-safe rewrite by the next flush (never silently overwritten).
- **Foreign-append merge & timestamp-first dedup** (`_frozen_prefix_and_foreign_appends`):
  a default save captures its `window` snapshot BEFORE taking `_locked`, so a
  cross-process writer (subagent / cron / CLI) can fully append + release the
  lock in that gap. A bare `meta + frozen + window` replace would then delete
  that acknowledged append, so the save first scans the on-disk WINDOW region
  (the bytes after the frozen prefix) for lines the in-memory window does not
  represent and carries them into the payload as `foreign_lines`. Matching is
  **count-bounded** (deques of window-entry indices; each disk line matches at
  most one window entry and each window entry absorbs at most one disk line) and
  runs in TWO passes so the outcome is independent of disk-line order:
  - **Pass 1 — exact `(ts, role, content)`** across all disk lines: an unchanged
    re-serialization, unambiguously **ours** (dropped — the window re-writes it).
    Resolving these first is what makes a burst of messages sharing ONE `ts`
    (coarse clocks — notably Windows' ~15 ms tick — stamp rapid appends with an
    identical `datetime.now().isoformat()`) match one-for-one instead of being
    mis-classified and duplicated on disk.
  - **Pass 2**, for each still-unmatched disk line, in order: (a) a **ts-only**
    match — an in-place edit keeps `ts` but changes content, so the window's
    version wins and the disk line is dropped — but applied ONLY when the `ts`
    group is an unambiguous 1:1 (exactly one unmatched window entry AND exactly
    one unmatched disk line share it); OR (b) a bounded `(role, content)`
    tiebreak against an as-yet-unconsumed window entry — covers a same-process
    `append_if_absent` durable copy persisted with a fresh `ts` (the workflow/
    cron-result injectors reflect the message in the slot AND write it via
    `append_if_absent_off_loop`, so the same message legitimately exists twice
    with different timestamps and must NOT be double-persisted). A line matching
    NEITHER is foreign and preserved.
  - **Count-bounded, exact-first identity (the fix for GPT 5.6's HIGH data-loss
    findings).** `(role, content)` is only a bounded tiebreak in which **each
    window entry absorbs at most ONE disk copy**. So if the on-disk window region
    holds two lines with identical `(role, content)` but distinct timestamps —
    the window's own persisted copy PLUS a *genuinely distinct* event from
    another process (e.g. a cron that reports the same status text twice) — the
    first is folded and the **second is preserved as a foreign append** (an
    earlier plain-`(role, content)`-set match collapsed both real events into
    one). Symmetrically, because colliding timestamps make a ts-only match
    AMBIGUOUS (a foreign append that happens to share the `ts` is
    indistinguishable from an edited window entry), ts-only matching is applied
    ONLY to unambiguous 1:1 `ts` groups; an ambiguous group preserves its disk
    lines as foreign — favouring a rare stale duplicate over irreversibly
    dropping an acknowledged cross-process append.
  - **Archive of ambiguous drops (no permanent loss).** A fresh-`ts` copy folded
    by tiebreak (b) is the genuinely ambiguous case (indistinguishable from a
    distinct same-content message without a stable id), so those drops are
    returned as `dedup_dropped` and routed through `_archive_lines`
    (`reason="foreign-dedup"`) by `_save_slot_to_history` before the atomic
    replace — the trade-off loses no data permanently. (A ts-less / ts-matched
    plain re-serialization is a normal window copy and is dropped silently to
    avoid archive spam.)
  - **Intended successor identity.** Timestamp is the closest thing to a stable
    per-message id available today. The intended successor is a **creation-time
    per-message uuid** (stamped when the message is created, carried through the
    slot and onto disk) so identity is *exact* rather than inferred; the bounded
    heuristic above is the bridge until that lands. This is a tracked, committed
    follow-up — see
    [issue #381](https://github.com/kirodotdev/KiroCrew/issues/381) — not an
    open-ended aspiration: when the uuid lands it **demotes this heuristic to a
    legacy fallback** used only for un-stamped (pre-uuid) lines, and both this
    paragraph and the `test_foreign_append_content_identity_dedup_semantics`
    contract test must be updated in the same commit.
  - **Residual window (rewrite saves).** The scan runs only for default saves
    (`collect_foreign = not rewrite`). Rewrite saves (rewind / regenerate / fork)
    intentionally truncate the window and are same-session/same-process, so they
    **skip** the foreign scan and can still clobber a concurrent cross-process
    append that lands between the pre-lock window snapshot and the lock — a known,
    narrow residual window (the dropped tail is handled by the rewrite's
    archive-diff, not the foreign scan).
- **Consolidation offset & rotation generation**: `last_consolidated` is an
  absolute message index the consolidator snapshots (as `total`) BEFORE its slow
  LLM call and writes back via `mark_consolidated`. A rotation firing during that
  await truncates the file and shifts every surviving index, so the stale offset
  can no longer be applied. Detection uses a monotonically-increasing
  `rotation_generation` counter in the metadata line (bumped by `_maybe_rotate`
  on every rotation, carried forward by compaction, absent field == 0 for legacy
  files): the consolidator snapshots it alongside the offset
  (`rotation_generation()`) and `mark_consolidated(key, total, generation=…)`
  resets `last_consolidated` to 0 whenever the generation changed — **regardless
  of how many messages the rotation retained**. This closes the gap a pure
  `offset > msg_count` heuristic misses (a rotation retaining ≥ the offset leaves
  `offset ≤ msg_count` true yet still shifted every index, silently marking
  never-consolidated retained messages as done); the `offset > msg_count` check
  remains as a defense-in-depth fallback for legacy callers that pass no
  generation. Reconsolidating a few already-processed messages is harmless and
  idempotent; dropping unprocessed ones is a persisted data-integrity failure.

## Session Archive (`history.py`)

Lines that ARE intentionally dropped (rotation, compaction, history edits) are
archived instead of being permanently deleted:

- **Archive location**: `~/.kiro/crew/sessions/archive/{key}__{YYYYMMDD-HHMMSS}.jsonl`,
  where the separator is `ARCHIVE_SEGMENT_DELIMITER`. It is `__` rather than a dot
  because session keys legitimately contain dots (a Slack `thread_ts`), which a
  right-most-dot parse would attribute to the wrong session.
- **Triggers**: `_rotate()` (>2MB), `rewrite_session()` (compact), and the
  dashboard rewrite path (`_save_slot_to_history` with a snapshot /
  `rewrite=True` / `_pending_rewrite` → `_archive_dropped_lines`). The default
  frozen-prefix dashboard save drops nothing, so it does not archive.
- **Atomic writes**: exclusive-create (`open mode 'x'`) avoids TOCTOU clobber
- **Retention**: configurable via `session.archive_retention_days` (default 30
  days; `-1` or `null` disables cleanup so the user manages deletion manually).
  `_cleanup_old_archives()` reads the value from config when called with no
  explicit `retention_days`, and is rate-limited to once per hour.
- **API**: `GET /api/session/archive` (list), `GET /api/session/archive/{name}` (read with path traversal protection)

### Pairing a session key with its files

`transcript_stem(key)` returns the filename stem a key's transcript and archive
segments share — the sanitized key (`dashboard:chat-1` → `dashboard_chat-1`). It is
public so callers that account for or reclaim a session's disk usage
([session-storage](session-storage.md)) resolve the pairing here instead of
re-deriving the sanitization. A second copy of that rule would drift the moment
this one changed, and the failure is silent and destructive: the pairing misses,
and a caller deleting "the session" removes one half and leaves the other behind.

- `set_title(key, title)` — persists a title into the session's metadata line (first line of JSONL)

### Session titling is independent of `memory_mode`

Auto-titling (`dashboard/chat_title.py:_maybe_auto_title`) runs for **every**
`memory_mode` — `persistent`, `incognito`, and `temporary` alike — and the
resulting title is persisted for all three. This is deliberate, not an
oversight:

- Titling reads only the slot's **own** messages and prompts the shared `_bg`
  session. It neither reads stored memory nor writes any, so neither of the two
  guarantees a non-persistent mode actually makes (`is_restricted` → no
  consolidation/lessons; `blocks_reads` → no memory-context injection) is
  engaged by it.
- Persisting the title discloses nothing new. `_save_slot_to_history` has no
  `memory_mode` gate, so an incognito/temporary slot already writes its **full
  transcript** to its session JSONL for tab recovery and gateway-restart
  restore. The title is a summary of content that is already on disk in the same
  file, and `restore_recent_sessions` skips only on `closed`, never on
  `memory_mode`.

Gating titling on `blocks_reads` (as an earlier revision did) therefore bought
no privacy while leaving temporary tabs permanently labelled "New Session…".
The manual `POST /api/chat/slots/{slot}/generate-title` endpoint never had such
a gate, so a temporary session could already be titled and persisted on demand.
Do not reintroduce a `memory_mode` condition here without first changing what
`_save_slot_to_history` writes.

## HistoryConsolidator (`history.py`)

Background task that fires when unconsolidated count ≥ 10 messages. Uses the
persistent background ACP session (kiro-cli long-running session, same as
cron/heartbeat/lesson extraction) to extract:
- `history_entry` → appended to today's daily history file
- `preferences_update` → overwrites `preferences.md` if changed
- `projects_update` → overwrites `projects.md` if changed

Non-blocking via `asyncio.create_task`. Requires `SessionManager` to be passed
at construction time; consolidation is silently skipped if no session manager
is available.

**Loop safety:** the task body runs on the event loop thread, so any blocking
work inside it must be offloaded. `_write_structured_memory` and `_save_lessons`
both embed items via blocking in-process llama.cpp inference calls
(`write_lesson` performs a rule embed plus up to `_MAX_BACKFILLS_PER_CALL` lazy
backfill embeds per lesson), so they are invoked through `asyncio.to_thread()` —
running them inline would freeze the gateway loop (heartbeats, Slack, dashboard)
for the duration of each embed, and can trip the faulthandler hard-kill. (The
model load itself never blocks the embed call — it runs on a background daemon
thread; embed returns `None` until the model is resident.) The same
applies to `TaskRunner._extract_lesson`, which calls `write_lesson` after a task
failure. Dashboard memory handlers that write semantic entries or embed a query
(`set_semantic`, `_try_embed`) offload the same way. Because these writes now run
on worker threads concurrently with loop-thread reads (`search_episodic` during
context assembly), `VectorMemoryStore` serializes the semantic UPSERT
read-modify-write and the FAISS add + id-map append with `_db_lock` (a `RLock`);
`write_lesson`'s dedup scan and backfill UPDATEs rely on sqlite's serialized-mode
statement atomicity (WAL + `busy_timeout`) rather than application-level locking
— the lock is never held across a blocking embed.

## Stop Events

Stop events are persisted to JSONL as `system` messages. The structured
stop-event data lives in the `cls` field as a JSON-encoded object (which
`parse_cls_meta` lifts into `meta` for frontend consumers via
`StopEventCard`). The `content` field mirrors the same JSON for
backward-compatible consumers that only read `content`.

```json
{
  "role": "system",
  "content": "{\"kind\":\"stop_event\",\"id\":\"stop-<uuid>\",\"state\":\"stopped\",\"outcome\":\"soft\",\"ts_start\":\"2026-04-27T00:07:40Z\",\"ts_end\":\"2026-04-27T00:07:40Z\"}",
  "cls": "{\"kind\":\"stop_event\",\"id\":\"stop-<uuid>\",\"state\":\"stopped\",\"outcome\":\"soft\",\"ts_start\":\"2026-04-27T00:07:40Z\",\"ts_end\":\"2026-04-27T00:07:40Z\"}",
  "ts": "2026-04-27T00:07:40Z",
  "source_thread": "dashboard",
  "source_user": "dashboard"
}
```

Possible `state` values:

| State | Meaning |
|-------|---------|
| `stopping` | Cooperative cancel in flight; waiting for agent ack |
| `stopped` | Agent acknowledged cancel; session preserved |
| `stop_failed_reset` | Agent did not ack within budget; session was hard-killed and reset |

The stop event is inserted at soft-start time with `state: "stopping"` and
updated in place (same `id`) when the outcome resolves. The updated message
is re-broadcast via `_on_message` so the frontend `StopEventCard` transitions
from `stopping` → `stopped`/`stop_failed_reset`.

After a cancelled turn, `context.build_cancelled_turn_preamble` reads the
cancelled user prompt and partial assistant output from this log and
prepends them to the next prompt as a bracketed preamble, because kiro-cli
discards cancelled turns from its own ACP conversation log. The flag
`_Session.prev_turn_cancelled` (set by `SessionManager.stop_turn` on
soft-cancel success) gates the one-shot re-injection.

## Session Lifecycle

1. New session → full context injected (memory + skills + lessons + last 20 messages)
2. Messages saved to JSONL with provenance after each response
3. Context ≥ configured threshold (`session.autocompact_pct`, default 90%) → compaction via kiro-cli `/compact` (fire-and-forget)
4. Session expires (30min idle) → provider killed
5. User returns → new session with history re-injected
6. After 10+ messages → background consolidation → structured memory updated

## Source Provenance

Messages include `source_thread` and `source_user` fields:
- **Slack**: `source_thread` = Slack thread_ts, `source_user` = Slack user ID
- **Dashboard**: `source_thread` = "dashboard", `source_user` = "dashboard"
- Session keys prefixed `dashboard:` for dashboard chat slots

Dashboard history list shows source icons: 🖥 (dashboard) / 💬 (Slack).
