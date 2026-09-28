# Channel History Buffer Module

## Overview

`channel_history.py` — per-channel rolling history for Slack group context.
Normal channels use an ephemeral in-memory window. Channels in `observe` mode
use a deeper window persisted as JSONL so it survives gateway restarts. Only
messages admitted by the sender, interceptor, activation, and channel-governance
gates are eligible; thread context is isolated to the current thread rather than
mixed with other threads.

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
- **Observe mode**: defaults to 200 entries and one week, configured by `slack.observe_max_messages` and `slack.observe_ttl_hours`; entries are appended to owner-local JSONL under `<data-home>/history`, loaded on startup, and compacted lazily
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
