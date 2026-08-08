# Side Conversation Module

## Overview

The side conversation module adds an ephemeral Q&A thread to a parent chat
slot. Users invoke it via the `/side` command or the "Side" tab in the
Activity panel. The side runs against the same parent slot identity but
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
6. **A submit is never dropped** — while a turn is in flight the message is
   steered into it or queued behind it; there is no rejection path.

## Busy-send: steer and queue

A side turn is a real LLM turn, so a second question can arrive while one is
streaming. The sidecar handles it with the same two-mode contract as the main
composer, and the frontend reuses the main chat's split send button
(`components/BusySendButton.tsx`) and queue cards (`components/QueueStack.tsx`)
so the two surfaces cannot drift.

| Mode | Effect |
|------|--------|
| `steer` (default) | `POST .../side/turn` with `{"steer": true}` injects the text into the RUNNING turn via the isolated session's `steer()` RPC. |
| `queue` | The text is held on `SideState.queue` and dispatched as the next turn when the current one ends. |

Which mode Enter takes is ONE user preference (`mc-busy-send-mode` in
localStorage), shared live between the main composer and the side panel.

Fall-through, not rejection: a steer is attempted only when the isolated session
exists, reports `supports_steer`, and `has_active_turn()` is true. Any of those
failing — or the RPC returning False — falls through to the queue. kiro-cli
silently swallows a steer aimed at a prompt that already ended, so without the
liveness probe the text would vanish with no turn and no queue entry.

The commit is bound to the sidecar **object** and the `run_id` captured BEFORE the
RPC suspends. Re-reading them afterwards would misattribute the steer: a
close+reopen swaps in a fresh `SideState` that is also `open`, and a finished
turn's drain can already have started the next run. So after the await —

- sidecar replaced (or closed) → 409; the replacement never asked for the text.
- same sidecar, run advanced or completed → the turn's `finally` owns the entry;
  the response reports `queued` + `demoted` rather than adding a second copy.
- same sidecar, same run, still incomplete → committed as a steer.

**A steer is never proven delivered by the RPC.** `steer()` returning True only
proves the bytes left the process; the backend's `steering_consumed` echo is the
authoritative signal, surfaced through `stream_and_collect`'s
`on_steer_consumed` hook.

Delivery state lives in an explicit **ledger**, `SideState.steers`, holding
`{id, text, state}` with state `pending` | `consumed` | `requeued`. The state is
explicit rather than encoded as presence-in-a-list because a submitter has to tell
three outcomes apart — *delivered and answered*, *never injected*, and *already
turned into a queue card* — and an absence cannot distinguish the first from the
third.

- registered BEFORE the RPC suspends, so a turn that ends during it already sees
  the entry;
- the echo **marks** `consumed` (never erases — an erased entry cannot tell a
  waiting submitter that its steer landed);
- the turn's `finally` marks whatever is still `pending` as `requeued` and puts it
  at the HEAD of the queue as an ordinary, cancellable card;
- the submitter reads back **its own id**, and `consumed` **outranks turn
  completion**: a question the backend injected is committed to the transcript even
  if the turn has since ended, because reporting a demotion there would leave it
  delivered and invisible;
- terminal entries are pruned when the next turn starts — the one point at which no
  submitter can still be mid-read.

A demoted steer is reported to the panel (`demoted: true` → a notice), so the
user who pressed "Steer" is not left to infer the mode change from a card
appearing.

Cancel and edit are **server-authoritative**: the card changes only once the
server has confirmed, never optimistically. A drain can dequeue an entry between
render and click, and an optimistic update would then show the text as cancelled
while the turn it started is already running. While a mutation is in flight the
card's controls are disabled (`QueueStack`'s `pendingIds`), so a second click
cannot fire a duplicate that races the first and 404s.

Confirmation arrives by **two independent paths** — the HTTP response and the
`chat.side_queue` frame — both dispatching the same replay-safe `sseSideQueue`
reducer, so losing either one cannot desynchronise the panel. A `cancel` releases
its text through `SideState.releasedText`, stashed in the reducer (which both
paths funnel through) and drained + cleared by the panel, so the release happens
exactly once whichever path lands first.

A head-insert broadcast carries `front: true` (a requeued steer, a failed drain's
entry) and the reducer prepends it — appending would show a different next
question than the backend will actually run.

Released text is **merged** into the composer, never chosen over it: a cancelled
entry's text and an in-progress draft are both typed work, and the released text
has no other home (its card or its request is already gone), so neither may be
the one discarded. The same rule covers a rejected submit's text.

Drain: `_run_side_turn`'s `finally` releases the session and then calls
`_drain_side_queue`, which pops one entry and dispatches it. The drain is
identity-checked on `run_id`, so a task belonging to a superseded run cannot
dispatch onto a sidecar that a close/reopen replaced. The queue path in
`api_side_turn` kicks the drain itself when the turn finished during the steer
attempt — the `finally` has already run by then, so the entry would otherwise sit
forever.

Bound: `MAX_SIDE_QUEUE` (20). The sidecar lives in memory on the parent slot, so
an unbounded queue is a client-driven memory sink; past the bound the endpoint
returns 429 and the pressure stays visible to the user.

Placement of a steer bubble: the terminal `chat.side_result` frame carries the
WHOLE turn's text and replaces the last assistant row, so a steer bubble is
inserted ABOVE the streaming answer rather than appended after it. Appending
would strand the reply and make the terminal frame concatenate the full text a
second time.

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
| turn | POST | `/api/chat/slots/{slot}/side/turn` | Submit question; starts a turn, steers the running one, or queues |
| queue cancel | DELETE | `/api/chat/slots/{slot}/side/queue/{queue_id}` | Drop a queued entry; echoes its text back for the composer |
| queue edit | PATCH | `/api/chat/slots/{slot}/side/queue/{queue_id}` | Rewrite a queued entry in place |
| close | POST | `/api/chat/slots/{slot}/side/close` | Drop buffer + queue + destroy LLM session |

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

### `dashboard/ws.py` — `broadcast_side_queue`

Emits `chat.side_queue` frames — `{slot, action, queue_id, content?, depth, ts}`
where `action` is `push` | `edit` | `cancel` | `drain`. Held apart from
`chat.side_result` so a queue mutation never enters the transcript reducer, and
apart from the main chat's `queue_push` so a side entry can never be mistaken for
a parent-slot turn.

### `dashboard/side_state.py`

`SideState` dataclass: `open`, `messages`, `last_run_id`, `created_at`,
`is_complete`, `queue`, `steers`.
Helpers: `append_user` (with a `steer` marker), `append_assistant`, `clear`,
`queue_append` / `queue_insert_front` / `queue_pop` / `queue_remove` /
`queue_edit`, and the ledger's `steer_register` / `steer_state` /
`steer_mark` / `steer_pending` / `steer_settle` / `steer_prune_terminal`.

### `dashboard/steer_settle.py`

`settle_consumed_steers(pending, snapshot)` — pure, and shared with the main chat
(`chat_runner._settle_consumed_steers` delegates to it). Matches by EQUALITY and
is count-aware: containment would false-positive a short steer against a longer
one, and a falsely-settled steer is never requeued, so the question is lost.

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
`api.sideTurn` on submit. Local optimistic buffer for pre-redux rendering — for
a turn this submit STARTS only: a steer's bubble must land above the streaming
answer and a queued one is a card, so both are placed by the server frame.
While a turn is in flight the composer stays editable and swaps its send button
for the shared `BusySendButton`; queued entries render as `QueueStack` cards whose
cancel and edit wait for the server's own frame before changing what the user
sees.

### `chatSlice.ts` — Side State

- `slotSide: Record<string, SideState>` on ChatState, each with `messages` and
  `queue`.
- `sseSideResult` reducer: user frames append, assistant frames accumulate
  (delta-append within same run_id), error frames always start new entry, and a
  `steer` user frame is spliced in ABOVE a streaming assistant row of the same
  run.
- `sseSideQueue` reducer: `push` appends (replay-safe — a redelivered id updates
  in place), `edit` rewrites, `cancel`/`drain` remove. Never resurrects a closed
  side.
- `sideClose` action drops per-slot side state.
- Cleaned up in `deleteSlot.fulfilled`.

### `useWebSocket.ts`

`case 'chat.side_result':` dispatches `sseSideResult` and
`case 'chat.side_queue':` dispatches `sseSideQueue` — two lines alongside the
existing subagent/tool dispatch cases.

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

Busy-send invariants live in `test/test_side_steer_queue.py`:

| Invariant | Test |
|-----------|------|
| Steer reaches the live turn | `test_in_flight_steer_injects_into_the_running_turn` |
| Unavailable steer never drops text | `test_steer_unavailable_falls_through_to_the_queue` |
| Queue mode leaves the session alone | `test_queue_mode_defers_even_when_steer_is_available` |
| FIFO, one entry per turn | `test_queue_drains_fifo_one_entry_per_turn` |
| Cancel / edit | `test_queue_cancel_removes_the_entry_and_echoes_content`, `test_queue_edit_rewrites_in_place_preserving_order` |
| Bounded queue | `test_queue_refuses_past_its_bound` |
| No stranding past the finally | `test_entry_queued_after_completion_is_not_stranded` |
| Steer identity binding | `test_a_steer_that_lands_after_its_run_ended_is_queued_not_claimed`, `test_a_steer_cannot_land_on_a_sidecar_that_was_replaced` |
| Unproven delivery is recovered | `test_an_unconsumed_steer_becomes_a_queue_card_instead_of_vanishing` |
| A proven delivery is not duplicated | `test_a_consumed_steer_is_settled_and_not_requeued` |
| A failed drain keeps the text | `test_a_failed_drain_puts_the_entry_back_instead_of_dropping_it` |

The settlement rules themselves are in `test/test_steer_settle.py` (equality not
containment, count-awareness, settle-all on an unusable echo), and
`test/test_llm_helpers_steer_echo.py` covers the REAL `stream_and_collect`
dispatch — every steering caller fakes that helper, so without it the hook could
be dead at runtime while all of them stayed green.
| Close wins over a late drain | `test_close_drops_the_queue_and_a_late_drain_cannot_resurrect_it`, `test_a_stale_task_cannot_drain_a_newer_sides_queue` |

Frontend invariants are covered in KiroCrewWebsite under `src/test/`:
`SideChat.close.test.tsx`, `SideChat.multiturn.test.tsx`,
`SideChat.refresh.test.tsx`, `SideChat.thinking.test.tsx`,
`SideChat.steerQueue.test.tsx`, `SideSlashCommand.test.tsx`, and the
`sseSideResult` block in `chatSlice.test.ts`.

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
