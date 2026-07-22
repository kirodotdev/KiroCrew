# Live Slack Thread Sync

## Summary

Bidirectional message mirroring between dashboard chat sessions and Slack threads. One kiro-cli process, two surfaces.

## Architecture Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Session ownership | Dashboard always owns the kiro-cli process | Keeps `_ChatSlot` simple — always has its own `task` |
| Slack→Dashboard linking | Migrate Slack session to dashboard process | Update Slack handler's session map pointer, compress+inject history |
| Thread creation | Always create fresh thread on "Link to Slack" | Avoids session conflicts with existing threads |
| Message delivery | Best-effort with visible failure warning | No retry queue, but user sees "⚠️ Sync failed" |
| Unlink behavior | Farewell message + thread goes dead | No orphaned sessions |
| Mirror scope | User msgs + agent text + tool call summaries | Skip tool results, thinking, binary content |

## Data Model

### `_ChatSlot` additions (state.py)

```python
# New __slots__ entries:
'_slack_linked'     # bool — True when linked to a Slack thread
'_slack_channel'    # str — Slack channel ID
'_slack_thread_ts'  # str — Slack thread timestamp
```

Persisted in `to_dict()` as `slack_linked`, `slack_channel`, `slack_thread_ts`. Link persisted to SessionStore via `set_slack_link()`. Restored on session resume via `get_slack_link()`.

### `DashboardState` additions (state.py)

```python
# Reverse lookup: Slack thread_ts → dashboard slot key
_slack_to_slot: dict[str, str] = {}
```

Populated on link, cleared on unlink. Rebuilt from slot metadata on `restore_recent_sessions()`.

## Message Flow

### Dashboard → Slack (user sends message in dashboard)

```
ChatPage input → POST /api/chat → api_chat()
  → slot.append(user_msg)
  → if slot._linked_slack:
      slack.post_message(channel_id, text, thread_ts)  # fire-and-forget
  → _run_chat() → kiro-cli stream
  → on agent response:
      slot.append(agent_msg)
      if slot._linked_slack:
        slack.post_message(channel_id, mrkdwn_text, thread_ts)
```

### Slack → Dashboard (user sends message in Slack thread)

```
Slack event → handler.handle_message()
  → session_key = thread_ts
  → check state._slack_to_slot.get(thread_ts)
  → if linked:
      dashboard_slot = state.slots[slot_key]
      dashboard_slot.send(user_msg)  # inject into dashboard slot's queue
      # dashboard's _run_chat() picks it up, streams to both surfaces
  → else:
      normal Slack session flow
```

### Link operation (dashboard → Slack)

```
POST /api/chat/slots/{slot}/link-slack
  → slack.open_dm(owner_user_id) → channel_id
  → slack.post_message(channel_id, "🔗 Linked to dashboard: {title}") → thread_ts
  → slot._slack_linked = True; slot._slack_channel = channel_id; slot._slack_thread_ts = thread_ts
  → state._slack_to_slot[thread_ts] = slot.key
  → return {"channel_id", "thread_ts"}
```

### Link operation (Slack → Dashboard)

```
POST /api/chat/slots/{slot}/link-slack  (with body: {"thread_ts": "...", "channel_id": "..."})
  → existing_session = slack_handler.sessions.get(thread_ts)
  → if existing_session:
      compress history from existing session → inject into dashboard slot
      slack_handler.sessions.pop(thread_ts)  # release Slack-owned process
  → slot._slack_linked = True; slot._slack_channel = channel_id; slot._slack_thread_ts = thread_ts
  → state._slack_to_slot[thread_ts] = slot.key
```

### Unlink operation

```
DELETE /api/chat/slots/{slot}/link-slack
  → if slot._slack_linked:
      slack.post_message(slot._slack_channel, "🔗 Session unlinked", slot._slack_thread_ts)
      state._slack_to_slot.pop(slot._slack_thread_ts)
      slot._slack_linked = False; slot._slack_channel = ""; slot._slack_thread_ts = ""
```

## API Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/chat/slots/{slot}/link-slack` | Create Slack thread + link (or link to existing thread) |
| DELETE | `/api/chat/slots/{slot}/link-slack` | Unlink + farewell message |

## Frontend Changes (ChatPage.tsx)

- Header: "🔗" button between model selector and context ring
  - Unlinked: click → calls POST link-slack → shows Slack icon badge
  - Linked: click → shows popover with "Open in Slack" link + "Unlink" button
- Linked indicator: small Slack SVG icon in tab header (like history items already show)
- Sync failure: toast notification when `_slack_link_error` is set

## Files to Modify

| File | Changes |
|---|---|
| `dashboard/state.py` | `_linked_slack`, `_slack_link_error` on `_ChatSlot`; `_slack_to_slot` on `DashboardState`; persist/restore |
| `dashboard/chat.py` | `link-slack`/`unlink-slack` endpoints; mirror posts in `_run_chat()` and `api_chat()` |
| `slack/handler.py` | Check `_slack_to_slot` before creating new session; route to dashboard slot |
| `slack/gateway.py` | Helper to post mirrored messages with error handling |
| `frontend/src/pages/ChatPage.tsx` | Link button, Slack badge, unlink popover |
| `frontend/src/pages/ChatSidebar.tsx` | Slack link indicator on slot entries |

## Edge Cases

- **Gateway restart**: `restore_recent_sessions()` rebuilds `_slack_to_slot` from persisted `_linked_slack` on each slot
- **Slot deleted while linked**: `api_chat_slot_delete()` calls unlink first (farewell message)
- **Slack rate limits**: 1 msg/sec/channel — agent responses are typically slower than this; if burst needed, log warning but don't buffer
- **Slot closed (tab closed)**: Unlink automatically, post farewell
- **Multiple slots linked to different threads**: Each is independent, no conflicts
