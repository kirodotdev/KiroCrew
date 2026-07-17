# Session–Slack Thread Linking

## Problem

Sessions and Slack threads are loosely coupled through in-memory side-channels
(`_thread_map`, `_thread_to_key`, `_channel_map`). This causes:

1. **Duplicate sessions** — resuming a dashboard session from Slack creates a
   second ACP process instead of reusing the existing one.
2. **No cross-surface sync** — messages sent from Slack don't appear in the
   dashboard, and vice versa.
3. **Lost on restart** — thread mappings live only in memory; a gateway restart
   breaks all resumed threads.
4. **Key format mismatch** — filenames use `dashboard_` (underscore) while
   session keys use `dashboard:` (colon), causing lookup failures.

## Design

### Core Idea

Every session gets two first-class, **persisted** Slack fields:

```
slack_thread_ts:  str | None   # Slack thread parent timestamp
slack_channel_id: str | None   # Slack channel where the thread lives
```

Once set, the thread becomes the **exclusive** Slack surface for that session.

### Session Lifecycle

| Origin | On creation | On link |
|--------|-------------|---------|
| Slack DM | `slack_thread_ts = thread_ts or msg_ts`, `slack_channel_id = channel` | Already linked |
| Slack channel (@mention) | Same as DM | Already linked |
| Dashboard | Both `None` | Set when resumed via `/kirocrew sessions` or "Send to Slack" button |
| Cron / Subagent | Inherit from parent session, or `None` | N/A |

### Storage

Extend the **session map** (`~/.kirocrew/session_map.json`) from:

```json
{ "dashboard:chat-1-123": "acp-session-uuid" }
```

to:

```json
{
  "dashboard:chat-1-123": {
    "sid": "acp-session-uuid",
    "slack_thread_ts": "1775106508.539499",
    "slack_channel_id": "D0ABC123"
  }
}
```

Backward-compatible: if the value is a plain string, treat it as `{"sid": value}`.

### API Changes (SessionManager)

```python
# Replace _thread_map, _thread_to_key, _channel_map with:

def get_slack_link(self, key: str) -> tuple[str | None, str | None]:
    """Return (thread_ts, channel_id) for a session."""

def set_slack_link(self, key: str, thread_ts: str, channel_id: str) -> None:
    """Link a session to a Slack thread. Persists to session map."""

def get_session_for_thread(self, thread_ts: str) -> str | None:
    """Reverse lookup: thread_ts → session key."""
    # Backed by an in-memory index rebuilt from session map on load.
```

### Message Routing

#### Slack → Session (existing, simplified)

```
Message arrives in thread_ts
  → get_session_for_thread(thread_ts)
  → if found: route to that session's ACP process
  → if not found: create new session (normal flow)
```

No change to `_route_message` activation checks — `get_session_for_thread`
replaces the current `has_session(thread_ts)` + `get_session_for_thread(thread_ts)`
double-check.

#### Slack → Dashboard sync

When `handle_message` processes a message on a linked session:

1. Find the dashboard slot for the session key.
2. `slot.append("user", text, "msg msg-u")` — injects user message into dashboard.
3. As the ACP streams response chunks, they already go to Slack via `post_message`.
   Mirror each chunk to `slot.append("assistant-chunk", chunk, "msg msg-a")`.
4. `state.push_slots_update()` — triggers WebSocket push to dashboard.

#### Dashboard → Slack sync

When `_run_chat` processes a message on a linked session:

1. `get_slack_link(session_key)` → `(thread_ts, channel_id)`.
2. If linked: `post_message(channel_id, user_text, thread_ts)` — echo user msg.
3. As the ACP streams response, mirror final text to
   `post_message(channel_id, response_text, thread_ts)`.

#### Dashboard "Send to Slack" button

For unlinked dashboard sessions, add a button that:

1. Creates a new Slack thread: `post_message(dm_channel, "🧵 {title}")`.
2. Calls `set_slack_link(key, thread_ts, channel_id)`.
3. Posts last N messages as context in the thread.

Same flow as `/kirocrew sessions` resume, but triggered from the dashboard UI.

### What Gets Removed

- `SessionManager._thread_map`
- `SessionManager._thread_to_key`
- `SessionManager._channel_map`
- `SessionManager.set_thread()` / `get_thread()`
- `SessionManager.set_channel()` / `get_channel()`
- `SessionManager.link_thread_for_resume()`
- Key format conversion hacks in `_handle_sessions`

### Migration

On first load of the new session map format:
- Plain string values → `{"sid": value, "slack_thread_ts": null, "slack_channel_id": null}`
- Existing Slack sessions (numeric keys like `1775106508.539499`) get their
  thread/channel populated from the key itself + DM channel lookup.

### Implementation Order

1. **Extend session map** — new format, backward-compat loader, persist on set.
2. **New SessionManager API** — `get_slack_link`, `set_slack_link`, rebuild
   reverse index on load.
3. **Remove old side-channels** — delete `_thread_map`, `_thread_to_key`,
   `_channel_map` and all callers.
4. **Update resume handler** — use `set_slack_link` instead of
   `link_thread_for_resume`.
5. **Update message routing** — use `get_session_for_thread` backed by new
   index.
6. **Slack→Dashboard sync** — mirror messages to dashboard slot.
7. **Dashboard→Slack sync** — mirror messages to Slack thread.
8. **"Send to Slack" button** — dashboard UI for linking unlinked sessions.

### Risks

- **Semaphore contention**: Dashboard and Slack both acquire the session
  semaphore. If one is slow, the other blocks. Mitigation: the semaphore
  already handles this for concurrent Slack messages; same pattern applies.
- **Duplicate ACP processes**: If dashboard and Slack both call `get_or_create`
  for the same key simultaneously, the race check in `get_or_create` already
  handles this (loser shuts down its provider, uses winner's).
- **Message ordering**: Slack messages arrive asynchronously. Dashboard slot
  append is synchronous. No ordering guarantee across surfaces — acceptable
  for v1.
