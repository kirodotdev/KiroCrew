# Channel History Buffer Module

## Overview

`channel_history.py` — per-channel rolling history for Slack group context.
Normal channels use an ephemeral in-memory window. Channels in `observe` mode
use a deeper window persisted as a capped JSONL file so it survives gateway
restarts. That file is the sole restart persistence for the observe-mode window;
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
  every message. The stronger alternative is a coalesced rewrite-only design:
  one rewrite scheduled per push, coalesced last-writer-wins on the lane the
  way terminal ops already coalesce, so a busy channel pays one rewrite per
  lane turn, not per message. The reason to keep the append path over it is
  the crash-loss window: an append persists each message as soon as the lane
  reaches it, while a coalesced rewrite persists at most one batch per lane
  turn, so everything behind the newest completed rewrite is lost on a crash --
  and that durability is itself soft (a forced exit abandons queued lane
  jobs). Whether that smaller loss window is worth the append-log
  reconciliation machinery is the design-family trade bound to the maintainer
  (this append-log vs rewrite-only family choice is left to the maintainer's
  decision on PR #6557).
  The file is bounded by `observe_max_entries`: every append counts toward a
  count-triggered compaction that rewrites the file from the in-memory window,
  the rewrite publishes via `atomic_write` temp+rename (never an in-place
  truncate), and after compaction under the current cap the file remains at
  or below twice that cap between compactions. Construction stores the RAW
  history path without touching the filesystem. The first `set_observe` load
  runs on the single-worker history lane, resolves that root exactly once,
  captures its filesystem identity at the same first-trust point, and parses
  the file; a gateway-loop caller returns while the result is pending, then
  merges it into the deque on that loop. Every later operation copies the
  canonical root and identity without re-resolving and pins that same identity
  at use time. Synchronous non-loop callers wait for the same lane operation.
  All disk mutations — appends, compaction rewrites, unlinks — share the lane.
  While a deferred load is pending, compaction is suppressed so the post-boot
  deque cannot replace history that has not been merged yet; a compaction
  requested in that window is remembered and republished after that load is
  safely applied, without adding an unconditional boot rewrite. A failed load
  suppresses every disk mutation for that channel — appends included — until a
  later load of that observe session succeeds: either it folds the complete
  file into memory, or it finds no file at all (nothing persisted is nothing
  unseen); new messages stay
  in the deque meanwhile and reach disk at the next compaction. This covers
  count and queue-saturation compaction plus live cap reduction; the
  user-requested `unset_observe` unlink still proceeds and
  clears the suppression because it intentionally removes the protected file.
  A live cap reduction republishes only the window's persistable (`ts`-bearing)
  entries and is skipped when it holds none, so messages received before
  observe was enabled can never turn that rewrite into an unlink of the file.
  `set_observe` first cancels any unlink still queued from a prior
  observe-off (the file must survive re-enable even when the load fails),
  then publishes the reloaded window as a rewrite so an unlink the worker
  already popped is superseded. `unset_observe` keeps the newest
  `max_entries` window in memory for ordinary channel context; the
  disk/memory merge on load dedupes by message identity, so a re-enable
  can never duplicate entries. An explicit orderly executor shutdown
  cancels queued-not-started lane jobs but cannot stop an in-flight job, and
  CPython joins the worker before process exit (so a blocked write can delay
  exit); bare interpreter shutdown may run queued jobs before the executor's
  ordinary `atexit` cleanup, while forced exit (`os._exit`) abandons everything.
  The history file is the sole restart persistence for the observe-mode window:
  `_load_observe` rebuilds memory only from what that file holds (skipping any
  torn trailing line), and no messaging-platform backfill exists. A forced exit
  abandons queued lane jobs, so appends since the last successful file
  publication are permanently absent from Kiro Crew's history after a crash.
  It can also abandon a queued `UNLINK`. For a disabled channel that is never
  observed again, the residue file is permanent; it is bounded at twice the cap
  per channel, best-effort owner-only (`0o600`) on POSIX (on Windows it carries the history
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

## APIs

| Method | Purpose |
|--------|---------|
| `push(channel_id, user, text, thread_ts=None, msg_ts=None)` | Record a message with optional thread and its own timestamp |
| `context_for(channel_id, thread_ts=None)` | Format messages, split by thread if provided |
| `clear(channel_id)` | Clear a specific channel buffer |
| `set_observe(channel_id)` | Enable observe mode: deeper buffer, loaded from the channel's persisted history file if one exists |
| `unset_observe(channel_id)` | Leave observe mode and remove the persisted history file |
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
