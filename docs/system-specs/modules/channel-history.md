# Channel History Buffer Module

## Overview

`channel_history.py` — ephemeral in-memory rolling window that captures
ALL messages per Slack channel. Provides conversational context when the
agent is @mentioned in group channels. Thread-aware: separates current
thread messages from other threads for clear LLM context.

## Problem

In a DM, the agent sees every message. In a group channel like #team-oncall,
multiple people are talking. When someone @mentions KiroCrew, the agent only
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
- **Thread-aware**: entries carry optional `thread_ts`; `context_for()` splits by thread
- **Max entries**: 50 per channel (configurable)
- **TTL**: 5 minutes — stale messages from old topics are evicted
- **In-memory only**: no disk persistence (ephemeral context)
- **Push on EVERY message**: before owner lock, captures all channel members
- **Inject on every message**: not just new sessions, since conversation changes

## Thread Context (Trust ACP)

Follow-up messages (non-new sessions) inject **no transcript context**.
ACP/kiro-cli maintains native conversation history — injecting a parallel
copy from ConversationLog creates dual sources of truth that contradict
each other (especially after compaction or rotation). This follows the
Only inject transcript on new sessions (via
`build_session_context`), never on follow-ups.

Episodic memory is also restricted to new sessions only, to avoid
cross-thread contamination on follow-up messages.

## Wiring

### Gateway (`slack/gateway.py`)

1. `ChannelHistory()` created at startup
2. `ctx_builder.channel_history = channel_history` — attached to context builder
3. `channel_history.push(channel, sender_id, text, thread_ts=thread_ts)` — before owner lock

### Context Builder (`context.py`)

`build_message(text, is_new, session_key, channel_id=channel, thread_ts=thread_ts)` —
calls `context_for(channel_id, thread_ts=thread_ts)` and injects result.
Also injects lightweight thread reminder on non-new sessions.

### Handler (`slack/handler.py`)

Passes `channel_id=channel` and `thread_ts=thread_ts or msg_ts` to `build_message()`.

## Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `_DEFAULT_MAX_ENTRIES` | 50 | Max messages per channel buffer |
| `_DEFAULT_TTL_SECS` | 300 | 5 min TTL for message expiry |

## APIs

| Method | Purpose |
|--------|---------|
| `push(channel_id, user, text, thread_ts=None)` | Record a message with optional thread |
| `context_for(channel_id, thread_ts=None)` | Format messages, split by thread if provided |
| `clear(channel_id)` | Clear a specific channel buffer |
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

When the bot is @mentioned in a channel thread, prior messages may be
invisible (the in-memory buffer only contains messages observed via Socket
Mode). The handler falls back to `conversations.replies(limit=1)` to fetch
the thread parent text and reply count. This metadata is injected via a
dedicated `thread_meta` parameter on `build_message`. Graceful degradation:
if the API call fails (e.g. missing `groups:history` scope), thread context
is simply skipped.

`HistoryEntry` includes `msg_ts` for correct thread parent identification.

## Per-Channel thread_follow

`ChannelConfig.thread_follow` (boolean, default: `true`) controls whether
the bot auto-responds in threads where it has an active session. When set
to `false`, the bot requires an explicit @-mention for every message, even
in threads it previously responded in. Useful for helpline/support channels
where continued thread engagement is undesirable.

## Related: A2A Exchange Budget (implemented in `channel.py`)

> **Note**: The behavior below is implemented in `channel.py` (the persistent
> multi-agent channel orchestration module), NOT in `channel_history.py`.
> It is documented here for cross-reference since both modules deal with
> channel message flow, but `channel_history.py` is the ephemeral Slack
> message buffer only.

Agent-to-agent (A2A) conversations in persistent agent channels are gated
by an exchange budget (`max_exchanges`, default: 3, configurable per channel).
Previously, the budget accumulated over the channel's lifetime and permanently
blocked agent pairs. Now the budget resets whenever a human sends a message,
giving agents a fresh round of autonomous discussion.
