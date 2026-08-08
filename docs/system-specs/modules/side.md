# Side Conversation Module

## Overview

The side conversation module adds an ephemeral Q&A thread to a parent chat
slot. Users invoke it via the `/side` command or the "Side" tab in the
Activity panel. The `/side` command is intercepted client-side regardless of
the parent turn's state: while a turn is running, the composer's steer path
checks `isInterceptedSlashCommand` before steering, so the command opens the
side chat instead of being injected into the running turn as literal text.
Paste tokens are expanded before the command is delegated, and a rejected
command (side turn already in flight, question over the byte limit) is
merged back into the originating slot's composer or persisted draft so the
question is never silently lost.
The side runs against the same parent slot identity but
spawns its own isolated LLM session, reads parent context as a frozen
snapshot, and never persists messages to JSONL or memory stores.

Design strategy: Option C (sidecar storage on parent slot) with a native
implementation lifting wire format and system prompt strings from the
upstream OpenClaw `/btw` protocol.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Frontend (KiroCrewWebsite)                                     │
│  ┌────────────┐  ┌────────────────┐  ┌───────────────────────┐ │
│  │ SideChat   │→ │ chatSlice      │← │ useWebSocket          │ │
│  │ .tsx       │  │ slotSide state │  │ chat.side_result case │ │
│  └────────────┘  └────────────────┘  └───────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
         │ HTTP                              ▲ WS
         ▼                                  │
┌─────────────────────────────────────────────────────────────────┐
│  Backend (KiroCrew)                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐ │
│  │ handlers/    │→ │ side_state   │  │ ws.py                 │ │
│  │ side.py      │  │ .py          │  │ broadcast_side_result │ │
│  ├──────────────┤  ├──────────────┤  └───────────────────────┘ │
│  │ side_context │  │ side_prompts │                             │
│  │ .py          │  │ .py          │                             │
│  └──────────────┘  └──────────────┘                             │
└─────────────────────────────────────────────────────────────────┘
```

### Key Invariants

1. **No new slot identity** — side lives as `slot._side: SideState | None`.
2. **Main path byte-frozen** — `context.py.build_message()` and
   `dashboard/chat.py` main-thread paths are never modified.
3. **Birth-only memory mode** — side never calls memory/learn/save; the
   sidecar buffer is discarded on close with no persistence.
4. **Isolated LLM session** — keyed on `f"side:{slot.key}"`, separate from
   the parent's session, so turns don't pollute parent context.
5. **Non-blocking** — `api_side_turn` returns immediately; streaming runs
   as a background task.

## Lifecycle

```
 open ──→ turn ──→ turn ──→ ... ──→ close
  │         │         │                │
  │ SideState created │                │ slot._side = None
  │ (open=True)       │                │ side session destroyed
  │                   │
  │        background task spawns,
  │        broadcasts chat.side_result chunks
```

| Endpoint | Method | Path | Effect |
|----------|--------|------|--------|
| open | POST | `/api/chat/slots/{slot}/side/open` | Initialise sidecar (idempotent) |
| turn | POST | `/api/chat/slots/{slot}/side/turn` | Submit question, get run_id back |
| close | POST | `/api/chat/slots/{slot}/side/close` | Drop buffer + destroy LLM session |

## Wire Protocol

Event name: `chat.side_result`  
Kind field: `"side"` (translated from upstream `"btw"`)

Payload shape (broadcast per chunk and per final response):

```json
{
  "type": "chat.side_result",
  "data": {
    "slot": "<parent-slot-key>",
    "run_id": "<hex-uuid>",
    "role": "user" | "assistant",
    "content": "<text>",
    "kind": "side",
    "ts": <unix-float | null>,
    "is_error": false
  }
}
```

Run-ID isolation: the frontend routes `chat.side_result` frames to
`chatSlice.slotSide` via a dedicated reducer (`sseSideResult`). The main
chat assembler never sees these frames — isolation is structural (separate
event type → separate reducer → separate redux slice), not filter-based.

## Backend Modules

### `dashboard/side_state.py`

`SideState` dataclass: `open`, `messages`, `last_run_id`, `created_at`.
Helpers: `append_user`, `append_assistant`, `clear`.

### `dashboard/side_prompts.py`

Two prompt constants lifted from the upstream protocol:

- `SIDE_BOUNDARY_PROMPT` — establishes ephemeral context, tool prohibition.
- `SIDE_DEVELOPER_INSTRUCTIONS` — marks the main-thread/side-thread boundary.
- `build_side_system_prompt()` — concatenates both into the first-turn envelope.

### `dashboard/side_context.py`

- `build_side_message(slot, question, is_first_turn=...)` — first turn
  includes developer instructions + parent snapshot + boundary prompt +
  question; subsequent turns return bare question (session retains framing).
- `_format_parent_snapshot(slot)` — renders parent user/assistant turns as
  read-only text block (max 32K chars, 500 chars/line truncation).
- `_format_side_history(slot)` — renders prior side turns for session
  cold-start recovery.

### `dashboard/handlers/side.py`

Three aiohttp handlers + `_run_side_turn` background driver.
`_run_side_turn` acquires an isolated session via `state.sessions.get_or_create`,
streams with `ToolApprovalPolicy.REJECT_ALL` (defense-in-depth against tool
calls), broadcasts chunks over `broadcast_side_result`, and appends the
final assembled text to `slot._side.messages`.

### `dashboard/ws.py` — `broadcast_side_result`

Module-level helper emitting `chat.side_result` frames to all WS clients.
Deliberately separate from `broadcast_ws` main-channel events.

## Frontend Modules

### `ActivityViewer.tsx`

5th tab: `{key: 'side', label: 'Side', icon: MessageSquare}`. Renders
`<SideChat slot={slot} />` when active.

### `SideChat.tsx`

Reads from `state.chat.slotSide[slot]`. Calls `api.sideOpen` on mount,
`api.sideTurn` on submit. Local optimistic buffer for pre-redux rendering.

### `chatSlice.ts` — Side State

- `slotSide: Record<string, SideState>` on ChatState.
- `sseSideResult` reducer: user frames append, assistant frames accumulate
  (delta-append within same run_id), error frames always start new entry.
- `sideClose` action drops per-slot side state.
- Cleaned up in `deleteSlot.fulfilled`.

### `useWebSocket.ts`

`case 'chat.side_result':` dispatches `sseSideResult` — single line addition
alongside existing subagent/tool dispatch cases.

## Security & Isolation

| Concern | Mitigation |
|---------|-----------|
| Tool execution | System prompt prohibition + REJECT_ALL approval policy |
| Memory pollution | No calls to memory/learn/save; sidecar never serialised |
| Context leak to main | `build_message` byte-frozen; side uses separate module |
| Slot visibility | No new `_ChatSlot` created; sidebar doesn't show phantom entries |
| App isolation | `_check_slot_ownership` mirrors main chat ownership checks |
| Session cleanup | `api_side_close` destroys kiro-cli session files |

## Testing

Backend invariants are covered by `test/test_side.py`:

| Invariant | Test |
|-----------|------|
| Memory isolation | `test_memory_isolation_byte_equal_after_round_trip` |
| Same-session reuse | `test_side_path_never_creates_a_new_slot` |
| Non-blocking stream | `test_side_turn_returns_before_run_finishes` |
| Channel separation | `test_side_run_id_never_leaks_to_main_channels` |
| Tool-rejection fallback | `test_empty_llm_output_produces_visible_fallback` |

Frontend invariants are covered in KiroCrewWebsite under `src/test/`:
`SideChat.close.test.tsx`, `SideChat.multiturn.test.tsx`,
`SideChat.refresh.test.tsx`, `SideChat.thinking.test.tsx`,
`SideSlashCommand.test.tsx`, `SideSlashCommand.steer.test.tsx` (command
interception wins over mid-turn steer routing), and the `sseSideResult`
block in `chatSlice.test.ts`.

Structural greps enforce compile-time invariants:

- `main_path_baseline.sh` — context.py + chat.py SHA-equal to baseline
- `no_main_context_pollution.sh` — no side symbols in main path files
- `no_memory_writes_from_side.sh` — no learn/save/memory calls from side
- `no_new_slot_in_side_path.sh` — side handlers never call get_or_create_slot
- `exactly_five_activity_tabs.sh` — tab count matches spec
- `side_tab_registered.sh` — "Side" tab present in ActivityViewer

## Design

- Context injection: sidecar storage on the parent ``_ChatSlot``
  (per-slot ``_side: SideState | None``) — keeps side messages reachable
  to the side handler without touching the main agent's context builder.
- Build strategy: native KiroCrew implementation reusing only the
  upstream wire format for the ``chat.side_result`` event.
- Memory mode: side conversations are born ephemeral and never write to
  vector store, learn store, KiroCrew session JSONL, or the consolidation
  pipeline. Tool execution is rejected via `ToolApprovalPolicy.REJECT_ALL`
  so the side LLM cannot call `learn_add` or any other write tool. The
  `side:` prefix is registered in `session._STATELESS_PREFIXES`, so the
  isolated kiro-cli ACP session is never resumed across gateway restarts;
  its on-disk transcript at `~/.kiro/sessions/cli/<sid>.jsonl` exists
  only while the side is open and is destroyed by `/side close` via
  `state.sessions.destroy(side_key)`.
