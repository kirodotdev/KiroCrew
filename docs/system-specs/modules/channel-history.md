# Channel History Buffer Module

## Overview

`channel_history.py` — per-channel rolling history for Slack group context.
Normal channels use an ephemeral in-memory window. Channels in `observe` mode
use a deeper window persisted as a rotated pair of JSONL files (live and `.1`)
so it survives gateway restarts. That file pair is the sole restart persistence
for the observe-mode window;
the messaging platform remains the copy of record a human can re-read, but
Kiro Crew never backfills the window from it automatically. Only messages
admitted by the sender, interceptor, activation, and channel-governance gates
are eligible; thread context is isolated to the current thread rather than mixed
with other threads.

## Problem

In a DM, the agent sees every message. In a group channel like #team-oncall,
multiple people are talking. When someone @mentions Kiro Crew, the agent only
sees that single message — zero context about the surrounding conversation.

Additionally, when multiple threads are active in the same channel, messages
from different threads were mixed together with no separation, causing the
LLM to confuse context across threads.

## Solution

A per-channel deque buffer with TTL expiry and thread-aware formatting:

```
channel_history.push(channel_id, user, text, thread_ts=thread_ts)  ← every message event
channel_history.context_for(channel_id, thread_ts=thread_ts)       ← when @mentioned
```

### Flow

When `thread_ts` is provided, output includes only messages from the current thread:
```
[Recent channel messages for context:]
[Current thread:]
  alice (2m ago): The pipeline is broken again
  bob (1m ago): Yeah I see 5xx errors
[End of channel context]
```

## Design

- **Per-channel**: each channel gets its own independent deque
- **Thread-aware**: entries carry optional `thread_ts` and `msg_ts`; `context_for()` returns only the current thread when given `thread_ts`, or only top-level messages otherwise
- **Normal-mode capacity**: 50 entries per channel
- **Normal-mode TTL**: 5 minutes — stale messages from old topics are evicted
- **Observe mode**: defaults to 200 entries and one week, configured by
  `slack.observe_max_messages` and `slack.observe_ttl_hours`; entries are
  appended to owner-local JSONL under `<data-home>/history`, loaded on startup.
  Per-message appends are deliberate: observe mode exists to preserve channel
  context across gateway restarts, and a restart is most likely exactly when
  the gateway is unhealthy mid-conversation, so waiting for a periodic
  coalesced rewrite would lose the newest messages every time it matters most.
  Rewriting the whole file on every message (a per-message rewrite) is
  rejected: it turns each message into an O(cap)-byte serialize-and-rename on
  the single disk lane, paying write amplification proportional to the cap on
  every message. A coalesced rewrite-only design (one rewrite per lane turn)
  is rejected for its crash-loss window: everything behind the newest
  completed rewrite is lost on a crash, where an append persists each message
  as soon as the lane reaches it.
  **Bounding by rotation.** The persisted history is a pair of generations,
  `<channel>.jsonl` (live) and `<channel>.jsonl.1` (older), bounded the way
  `jsonl_util.rotate_jsonl_at` bounds the other append-only logs in the tree.
  That helper is not reused here: it rotates on a byte size, best-effort, so
  records written between its size check and the rename overshoot the bound,
  and its `os.replace` resolves both paths from the root, so a parent
  directory swapped for a link would redirect the rename. This bound is a
  record count enforced before each write, and its rename runs inside the
  pinned parent. The lane counts the lines in the live file; an append whose
  lines would take it past `observe_max_entries` first renames the live file
  over the older generation (one `renameat` inside the pinned parent on POSIX,
  `os.replace` under the held pin on Windows) and then writes to a fresh
  live file. Once any inherited oversized generation has been replaced
  by a later rotation, the file pair holds at most about twice
  `observe_max_entries` records and never fewer than the newest full generation.
  Rotation rather than a bounding rewrite because a rewrite
  recomputes the file from an in-memory window that can be stale, partial or
  mixed with off-mode context, and so can publish a subset of the records on
  disk; a rename moves records without re-serializing any, so every byte on
  disk is exactly what an append wrote and no reconciliation state is needed
  between the window and the file. No sibling lock file is taken: the
  single-worker lane is the only writer, so two rotations cannot race.
  **Why no cross-process rotation lock.** `jsonl_util.rotate_jsonl_at` guards
  its rename with a non-blocking sibling `.lock` because its callers are, or
  must be treated as, multi-process writers. This writer is exempt from that
  treatment because no second process ever writes these files. `gateway_lock`
  takes an exclusive advisory `flock` on `<home>/gateway.lock` (and anchors
  the home directory) when `kirocrew gateway` starts and holds it for the
  process lifetime, so a second gateway on the same home is refused at
  startup rather than admitted as a second writer. Within that one gateway
  the only `ChannelHistory` instance is the one `slack/gateway.py` constructs
  over `<data-home>/history`, and every operation that appends to, renames,
  reads or unlinks a generation runs as a job on the single-worker history
  lane (`executors.channel_history_executor`), so a rotation has exactly one
  writer and the generation-loss race the sibling lock exists to stop cannot
  arise. The only other code that opens the directory is the `kirocrew
  security audit` scan, which reads. A tool that starts writing these files
  from another process has to take the same lock, or the exemption falls.
  **Load.** Enabling observe reads the older generation and then the live
  file on the lane, through the same no-follow, single-link regular-file,
  pinned-identity opens the writes use, into a deque bounded to the cap in
  force when the read begins (memory stays O(cap) on an oversized inherited
  file; its oldest records fall off the window and stay on disk). Each line
  is validated on its own — undecodable bytes, malformed JSON, non-object
  records, wrong field types, missing or non-finite timestamps are skipped
  with a warning, oversized fields are truncated to the same caps `push`
  applies — and entries past the TTL are dropped from the window. A load
  never writes a record; its only disk changes are whole-file, judged
  against the TTL in force once both generations are read — and loads are
  the only source of
  TTL-driven removals from disk; nothing expires records between loads. At a
  load, a generation whose every record is
  past the TTL is unlinked inside the pinned parent; a generation that still
  holds one record inside the TTL is kept whole. Then, when no older
  generation exists (absent, or just unlinked), the live file is rotated
  aside to `.1` if its oldest record is past the TTL: the rename replaces
  nothing, the window keeps the fresh records, the next append starts a
  fresh live file, and the moved-aside generation is removed at a later
  load once its newest record has expired, or when the next rotation
  replaces it. A live file is never rotated over an existing `.1` for TTL
  reasons — a `.1` that still holds a fresh record beside a live file with
  an expired head can only arise from a clock anomaly, and nothing is moved
  or removed then. A generation whose read fails is left exactly where it
  is (no unlink, no rotation); an unreadable older generation pauses the
  channel's disk appends, whatever the live file holds, so no rotation can
  replace it, while an unreadable live file beside an absent `.1` seeds the
  lane's record count at the cap, so the next append rotates it aside before
  it writes and the bound still holds, while an unreadable live file beside a
  readable `.1` pauses the channel's disk appends instead — that rotation
  would rename it over the only generation a load can still read — with one
  warning at the load and the in-memory window unaffected. **Paused appends
  retry on a schedule.** A paused channel's append returns at once, leaving
  the entry in memory and both files untouched, until
  `_PAUSE_RETRY_INITIAL_SECS` (60 s) have passed since the pause on the
  lane's monotonic clock; the first append after that re-reads both
  generations through the same guarded read a load uses — a read only, never
  a rename or an unlink. When every generation that exists reads, the pause
  lifts: the live measurement seeds the record count and torn state as a
  load would, one INFO line records the resume, and that append rotates or
  writes as normal. When a generation is still unreadable the channel stays
  paused, the wait doubles (to at most `_PAUSE_RETRY_MAX_SECS`, one hour),
  and nothing above DEBUG is logged, so a file unreadable for weeks costs
  one warning at the pause and one INFO line at the resume. A load or an
  observe-off clears the pause and its schedule outright. The interval
  starts at a minute because what clears a pause is a repair of the file
  (a planted link removed, a permission or mount restored), minutes to
  hours later, not the next message; the doubling bounds what a
  permanently unreadable file costs the lane while keeping the resume
  within one interval of the repair. **Retention bound.** An expired record
  leaves disk once its generation's newest record is past the TTL at a load
  or a rotation replaces that generation; all of it happens by whole-file
  rename or unlink, never a rewrite. Once any inherited oversized generation
  has been replaced, retention between loads is bounded by the cap, not the
  TTL: rotation keeps at most two generations of about
  `observe_max_entries` records each. The load also seeds the lane's record
  count from the live
  file so the bound holds across restarts, and the same rotation moves aside
  an inherited single file already over the cap with no older generation, so
  the next rotation replaces it with a cap-sized generation (at most one
  rotation per load). An inherited file whose records are all expired is
  removed at the load; one whose oldest record is expired is rotated aside;
  otherwise the code only renames it aside when it is over the cap — it
  never adds to it and never trims it — and a channel that stays quiet keeps
  it at the size it had when it was inherited until a later load or rotation
  removes it. A torn last line (a
  crash mid-append) is skipped on load and remembered: the next append
  starts on a fresh line, so the torn tail can never swallow the record
  after it. Off-mode (`wall_ts=None`) entries never reach disk because only
  `push` on an observe channel appends, so nothing off-mode can displace a
  persisted record.
  **Live cap changes** resize every observe deque; the disk bound follows
  without a rewrite because the lane compares the live file's record count
  against the cap in force at each append. A lowered cap therefore takes
  effect at the next successful rotation; once any inherited oversized
  generation has been replaced, the pair is bounded by about twice the cap in
  force, and a raised cap lets the live file grow to the new cap. A cap
  raised while a load is still queued loses nothing: the read is bounded by
  the cap in force when it begins, and the file is untouched either way.
  **Failure behaviour.** Rotation never raises: when the live file is full
  and the rename fails, the disk append is skipped, so the live file never
  exceeds the cap while rotation keeps failing; the entry stays in the deque,
  every later append retries the rotation first, and persistence resumes
  with the first rename that succeeds. The pause is logged once per run of
  consecutive failures, never per message. A failed read costs only
  the window; disk appends continue unless preserving an unreadable generation
  requires a pause, which lifts at the next load or at the first scheduled
  re-read that finds every generation readable (see above). A failed append is
  logged and the entry
  stays in the deque. After an attempted write fails, the lane reopens the
  live file through the guarded load path and replaces its count and torn
  flag with the measured end. If that read fails too, disk appends pause and
  retry the measurement before any later rotation or write; a load also
  reseeds the state. A failure before the write (a refused pin or open) moves
  neither the count nor the torn flag. The
  append queue is admission-bounded (64 slots, non-blocking): on a stalled
  disk an append past the bound is dropped with a warning while the deque
  keeps the entry, so a stalled disk cannot grow the queue one closure per
  message. Turning observe off removes both generations on the lane, after
  every append already queued for the channel — each independently, so a
  failure to unlink one is logged and never leaves the other behind — and
  keeps the newest
  `max_entries` window in memory; turning it back on finds no file, so the
  retained window stays in memory only and the next push starts a fresh live
  file (the same outcome as an inline unlink on main). An explicit orderly
  executor shutdown cancels queued-not-started lane jobs but cannot stop an
  in-flight job, and CPython joins the worker before process exit (so a
  blocked write can delay exit); bare interpreter shutdown may run queued
  jobs before the executor's ordinary `atexit` cleanup, while forced exit
  (`os._exit`) abandons everything.
  **Root trust and containment.** Construction stores the RAW history path
  without touching the filesystem. The first lane operation resolves that
  root exactly once, creates it, and captures its filesystem identity; every
  later operation — append, rotate, read, unlink — pins the parent directory
  (`platform_compat.pin_directory`, refusing a link, file or junction at its
  name) and refuses to proceed when the pinned identity differs from the
  trusted one. The leaf stays lexical so a link planted at either
  generation's name is refused by the no-follow open (or, for a rotation,
  replaced by the rename without being followed), and every open is then
  judged on its descriptor: a FIFO, device, directory, or a hard link to a
  file outside the root (another name for the same inode, which no path
  guard can see) is refused before any read, write or chmod. A rename or
  unlink acts on the directory entry alone, so a refused entry's target is
  never touched. Synchronous non-loop callers wait for the same lane
  operation.
  The file pair is the sole restart persistence for the observe-mode window:
  the load rebuilds memory only from what the two generations hold, and no
  messaging-platform backfill exists. A forced exit abandons queued lane
  jobs, so appends still queued are permanently absent from Kiro Crew's
  history after a crash; it can also abandon a queued unlink, and for a
  disabled channel that is never observed again the residue files are
  permanent. They are bounded at about twice the cap per channel, best-effort
  owner-only (`0o600`) on POSIX (on Windows they carry the history
  directory's inherited ACL), and accepted because the messaging platform
  remains the copy of record in the human sense that someone can re-read the
  channel.
- **Push after gates**: unauthorized, intercepted, activation-off, and channel-governance-denied content is never recorded; observe mode records admitted messages before the mention/active-thread routing decision, while other modes record only messages accepted for processing
- **Inject on every built message**: `ContextBuilder.build_message()` reads current channel history on both new and follow-up turns and neutralizes structural prompt markers

## Thread Context (Trust ACP)

Follow-up messages (non-new sessions) inject **no transcript context**.
ACP/kiro-cli maintains native conversation history — injecting a parallel
copy from ConversationLog creates dual sources of truth that contradict
each other (especially after compaction or rotation). The transcript is
therefore injected only on new sessions (via `build_session_context`), never on
follow-ups.

Episodic memory is also restricted to new sessions only, to avoid
cross-thread contamination on follow-up messages.

## Wiring

### Gateway and event routing (`slack/gateway.py`, `slack/events.py`)

1. `ChannelHistory()` is created at startup with `history_dir=<data-home>/history` and the configured observe-mode limits.
2. `ctx_builder.channel_history = channel_history` attaches it to the context builder; configured `observe` channels call `set_observe()` and load persisted entries.
3. `slack/events.py` applies sender authorization, interception, activation, and channel-governance gates before recording content. Observe mode records admitted messages before mention routing; other activation modes push after deduplication and attachment/transcription processing.

### Context Builder (`context.py`)

`build_message(text, is_new_session, session_key, channel_id=channel, thread_ts=thread_ts)` —
calls `context_for(channel_id, thread_ts=thread_ts)` and injects result.
Also injects lightweight thread reminder on non-new sessions.

### Handler (`slack/handler.py`)

Passes `channel_id=channel` and `thread_ts=thread_ts or msg_ts` to `build_message()`.

## Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `_DEFAULT_MAX_ENTRIES` | 50 | Max messages per channel buffer |
| `_DEFAULT_TTL_SECS` | 300 | 5 min TTL for normal-mode message expiry |
| `OBSERVE_MAX_ENTRIES` | 200 | Observe-mode default; gateway config may override it |
| `OBSERVE_TTL_SECS` | 604800 | Observe-mode one-week default; gateway config may override it |
| `_PAUSE_RETRY_INITIAL_SECS` | 60 | Wait before a paused channel's first scheduled re-read of its generations |
| `_PAUSE_RETRY_MAX_SECS` | 3600 | Ceiling the wait doubles toward while a generation stays unreadable |

## APIs

| Method | Purpose |
|--------|---------|
| `push(channel_id, user, text, thread_ts=None, msg_ts=None)` | Record a message with optional thread and its own timestamp |
| `context_for(channel_id, thread_ts=None)` | Format messages, split by thread if provided |
| `clear(channel_id)` | Clear a specific channel buffer |
| `set_observe(channel_id)` | Enable observe mode: deeper buffer, loaded from the channel's persisted history generations if any exist |
| `unset_observe(channel_id)` | Leave observe mode and remove both persisted history generations |
| `channel_count` | Property: number of channels with history |
| `entry_count(channel_id)` | Message count for a specific channel |
| `set_user_name(user_id, name)` | Cache a display name for a user ID |

## Display Name Resolution

`ChannelHistory` maintains a `_user_names` cache (`user_id → display name`).
When `context_for()` formats messages, it replaces raw Slack user IDs with
cached display names so the LLM sees human-readable names. The cache is
populated by `slack/events.py` which resolves sender display names via
`users_info()` on each message event.

## Thread Metadata Injection

On a new, non-resumed, non-compressed thread session, the handler first uses
`fetch_message(channel, thread_ts)` to retrieve the thread parent. If that is
unavailable, it falls back to `fetch_thread_replies(limit=1)` for parent text
and reply count; missing `channels:history` or `groups:history` scope degrades to
bare thread identifiers. Parent text and fallback metadata are treated as
untrusted input: prompt-injection matches are withheld and audited, and accepted
text is structurally neutralized before injection. `HistoryEntry.msg_ts` lets the
in-memory window identify a top-level message as the parent of a later thread.

## Per-Channel thread_follow

`ChannelConfig.thread_follow` (boolean, default: `true`) controls whether
the bot auto-responds in threads where it has an active session. When set
to `false`, the bot requires an explicit @-mention for every message, even
in threads it previously responded in. Useful for helpline/support channels
where continued thread engagement is undesirable.

## Related: A2A exchange budget

Agent-to-agent delivery in persistent agent channels is gated by an exchange
budget that a human message resets. That contract lives in `channel.py`, not
`channel_history.py`, and is specified in
[persistent-agent-channels.md](persistent-agent-channels.md).
