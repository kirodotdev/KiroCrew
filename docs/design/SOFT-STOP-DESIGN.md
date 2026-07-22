# Soft Stop with Kill Fallback

**Status:** Draft
**Author:** bgrubin
**Date:** 2026-04-25
**Branch:** `beta-braveheart`

## 1. Problem

User-initiated Stop (dashboard button, Slack `!stop`, `/kirocrew stop`) always hard-kills the kiro-cli process. The choice was made in commit `45defbb` (2026-02-24) because cooperative `session/cancel` was observed to block indefinitely behind long tool calls — but the kill-first policy has real costs:

- Loss of in-memory kiro-cli state — conversation context must be re-injected from JSONL on every resume.
- Process respawn penalty (~2–30s cold start; warm pool cushions but does not eliminate).
- Orphan-MCP-process risk — entire cleanup subsystems (`cron.py` reaper, subagent reaper, `_cleanup_orphaned_mcp_servers`) exist partly because of kill-first.
- History re-injection is lossy: mid-stream tokens that had not yet been flushed to JSONL are gone; in-flight tool results are discarded.
- No user-visible signal about *why* a stop took a moment (cancel honored vs. tool was mid-execution), and no confirmation the stop actually succeeded — a silent return-to-idle is ambiguous.

The ACP spec provides a clean acknowledgement for cancellation (`stopReason: "cancelled"` on the `session/prompt` response, per agentclientprotocol.com), but KiroCrew never wired up observation of this field. `cancel_session()` is fire-and-forget, the `stop_reason` field exists on `AcpEvent` (line 101 of `acp/types.py`) but is never populated, and the `_cancelled` flag shortcircuits the read loop before the ack can arrive.

## 2. Goals

1. Prefer the spec-correct cooperative cancel; fall back to hard-kill only when kiro-cli demonstrably will not honor cancel within a user-controlled budget.
2. Give the user **visible, unambiguous confirmation** that stop was actioned — not a silent return-to-idle.
3. Preserve the ability to force immediate kill via a second stop press.
4. Keep cancellation outcomes in the transcript so later troubleshooting (and the LLM's next turn) can see whether a prior turn was cancelled, completed, or failed.

## 3. Non-goals

- Changing Slack message queue cancellation semantics beyond "first press clears queue."
- Removing the hard-kill code path (it remains the fallback).
- User-configurable soft-stop policy beyond the budget duration.
- Per-session / per-agent / per-tool budget overrides (global only for now).
- Changing `cancel_current` semantics for internal programmatic callers (taskrunner, subagent, `llm_helpers`). Their fire-and-forget behavior is preserved via the `wait_ack_timeout=0.0` default.

## 4. User-visible behavior

### 4.1 Dashboard

**Stop button states.** A single button in the chat footer:

| State | Appearance | Transitions |
|---|---|---|
| `idle` | hidden or disabled | — |
| `armed` | normal Stop icon, clickable | press → `pulsing` (soft stop initiated) |
| `pulsing` | continuous pulse animation (opacity 0.6↔1.0, ~1.2s period, Framer Motion `animate` loop) | soft ack → `idle`; second click → `killing`; budget timeout → `killing` |
| `killing` | solid red, brief (<500ms) | hard reset done → `idle` |

Implementation: Framer Motion `animate={{ opacity: [0.6, 1, 0.6] }} transition={{ duration: 1.2, repeat: Infinity }}` on `pulsing`. No new CSS keyframes (per AGENTS.md).

**Inline "Stopping" card.** When stop is pressed, a tool-call-style card is inserted at the bottom of the message list as its own JSONL entry. Red color tones (use `--diff-del` or `text-danger` tokens, not a new color). The card has three life-cycle states:

1. **In flight**: `⏹ Stopping…` with subtle red pulse (same Framer Motion pattern), matching tool-call card layout so users recognize it.
2. **Soft ack resolved**: replaced in place with `[Stopped]` — single-line, red text, static.
3. **Hard kill resolved**: replaced with `[Stop Failed, Session Reset]` — single-line, red text, static.

Each state is persisted to the slot's JSONL transcript as a `system` message with `kind: "stop_event"`, so:

- Reading back the conversation later shows exactly where the cancel happened.
- LLM cross-session history / context builder sees it, so the next turn's context includes "the previous turn was stopped by the user."
- No ambiguity between "the turn silently completed" vs "the user halted it."

### 4.2 Slack

**Ephemeral Block Kit message** posted via `chat.postEphemeral` in the same thread as the in-flight turn, visible only to the stop requester:

```
┌────────────────────────────────────────┐
│ ⏹  Stopping…                           │
│    Cooperative cancel in progress.     │
│    [ Kill Now ]                        │
└────────────────────────────────────────┘
```

- Soft ack → update via `response_url` → `⏹ [Stopped]` (buttons removed).
- Hard kill (button pressed or budget expired) → `⛔ [Stop Failed, Session Reset]` (buttons removed).

The ephemeral message is always updated in place, never deleted, so the user sees the outcome of their action even after a brief thread reload (within the Slack ephemeral 1-hour window).

A short summary line is also posted as a non-ephemeral thread reply on resolution (current behavior) so the audit trail is visible to other thread participants and the LLM's context builder: `⏹ Execution stopped.` (soft) or `⛔ Execution stopped — session reset.` (hard).

### 4.3 Configuration

Global config: `agent.soft_stop_budget_secs` (float, default `10.0`, clamped to `[0.5, 60]`).

- Lives in the `AgentConfig` dataclass in `src/kiro_crew/config/loader.py`. Automatically exposed via `config/schema.py`'s JSON Schema generation.
- Editable via `kirocrew config set agent.soft_stop_budget_secs 10` CLI.
- Settings UI: number input in `frontend settings ChatPanel` labeled "Soft-stop budget (seconds)" with an `InfoTip` explaining the trade-off.

Config reload: existing SIGHUP / `apply_config` path picks up the new value without restart.

## 5. Core mechanism

### 5.1 ACP client — observe the ack

Update `src/kiro_crew/acp/types.py`:

- Keep `stop_reason` field on `AcpEvent` (already exists, line 101).
- Add `STOP_REASON_CANCELLED = "cancelled"`, `STOP_REASON_END_TURN = "end_turn"` constants.

Update `src/kiro_crew/acp/client.py`:

1. **Populate `stop_reason`** from the JSON-RPC prompt response. In `_dispatch_events`, when `action == "complete"`, read `msg.result.get("stopReason", "")` if result is a dict; set it on the emitted `AcpEvent(kind=EVENT_COMPLETE, stop_reason=...)`.
2. **Remove the `_cancelled` short-circuit from `_read_message`** at line 730. Keep the flag itself (used by internal callers) but stop using it to raise `AcpError("Operation cancelled")` — that behavior currently prevents the client from observing the `stopReason: "cancelled"` response. Replace with a milder check: if `_cancelled` is set and a configurable grace window (default 10s) passes without a response, raise `AcpError`. This preserves the escape hatch for broken agents without sabotaging the common case.
3. **Add a `turn_done: asyncio.Event`** per in-flight prompt, set when `_dispatch_events` yields `EVENT_COMPLETE` or raises. Expose as `AcpClient.wait_turn_done(timeout) -> str` (returns the `stop_reason` or raises `asyncio.TimeoutError`).
4. `cancel_session()` becomes a true notification — no ID awaited, just write to stdin and return. The `_cancelled` flag setting stays for internal callers.

### 5.2 Provider API

`src/kiro_crew/providers/base.py`:

```python
CancelOutcome = Literal["acked", "timeout", "no_turn", "error"]

async def cancel(self, *, wait_ack_timeout: float = 0.0) -> CancelOutcome:
    """Cancel in-flight operation.

    wait_ack_timeout=0.0 → fire-and-forget (legacy; preserves behavior for
        internal callers: taskrunner, subagent, llm_helpers).
    wait_ack_timeout>0.0 → send session/cancel, wait for stopReason ack
        or timeout. Returns "acked" if ack arrived, "timeout" otherwise.
    """
```

`src/kiro_crew/providers/acp.py`:

```python
async def cancel(self, *, wait_ack_timeout: float = 0.0) -> CancelOutcome:
    if not self._client.has_active_turn():
        return "no_turn"
    try:
        await self._client.cancel_session()
    except AcpError:
        return "error"
    if wait_ack_timeout <= 0:
        return "acked"  # fire-and-forget semantics
    try:
        reason = await self._client.wait_turn_done(timeout=wait_ack_timeout)
        return "acked" if reason in (STOP_REASON_CANCELLED, STOP_REASON_END_TURN) else "timeout"
    except asyncio.TimeoutError:
        return "timeout"
```

Bedrock provider: unchanged — returns `"no_turn"` since there is no in-flight turn state.

Design choice: the provider API uses a single keyword float (`wait_ack_timeout`) rather than a `CancelRequest` dataclass. User-facing escalation policy ("double-click = hard kill now", "5s budget") lives at `SessionManager.stop_turn()`, not in the provider. If more provider-level knobs appear later, refactor to dataclass then.

### 5.3 SessionManager.stop_turn

The orchestration layer both stop surfaces (dashboard + Slack) share:

```python
# src/kiro_crew/session.py

StopOutcome = Literal["soft", "hard", "idle"]

async def stop_turn(
    self,
    key: str,
    *,
    force: bool = False,
    on_soft: Callable[[], Awaitable[None]] | None = None,
    on_hard: Callable[[], Awaitable[None]] | None = None,
) -> StopOutcome:
    """Cooperative stop with kill fallback + eager respawn.

    Sequence:
      1. clear_queue(key)                # queue drop is unconditional, first-press
      2. if force: go straight to hard kill
      3. else: send session/cancel, wait up to budget
         - acked → call on_soft hook → return "soft"
         - timeout → fall through to hard kill
      4. hard kill: reset(key) → fire-and-forget respawn task → on_hard hook → "hard"
    """
    session = self._sessions.get(key)
    if not session:
        return "idle"

    self.clear_queue(key)
    budget = self._cfg.agent.soft_stop_budget_secs

    if not force:
        outcome = await session.provider.cancel(wait_ack_timeout=budget)
        if outcome == "acked":
            if on_soft:
                await on_soft()
            return "soft"
        if outcome == "no_turn":
            return "idle"
        # timeout or error → escalate

    await self.reset(key)
    # Eager respawn — fire-and-forget, do not block stop_turn
    asyncio.create_task(self._eager_respawn(key))
    if on_hard:
        await on_hard()
    return "hard"

async def _eager_respawn(self, key: str) -> None:
    try:
        await self.get_or_create(key)  # preserves all existing semantics
    except Exception:
        logger.debug("Eager respawn failed for %s", key, exc_info=True)
```

`on_soft` / `on_hard` callbacks let callers update their UI before the async-generator turn handler sees `EVENT_COMPLETE` fly by.

### 5.4 Handler response to `stopReason="cancelled"`

Handler code (`slack/handler.py`, `dashboard/chat.py`) that iterates `async for event in provider.stream(...)` already exits on `EVENT_COMPLETE`. After this change, it also checks `event.stop_reason`:

- `"end_turn"` → existing normal completion path.
- `"cancelled"` → **do not** call `record_success` or `record_failure`; flush whatever partial text streamed; skip any "agent turn failed" UI paths; skip memory consolidation for this turn.
- Anything else ≠ `"end_turn"` → log at warn, treat as normal completion for now (max_tokens / refusal / etc are out of scope).

## 6. Stop flow orchestration

The stop flow is driven by the caller. Both dashboard and Slack orchestrate the same abstract sequence; only the UI surface differs.

### 6.1 Dashboard

```
user clicks Stop
├─ button → state='pulsing'
├─ POST /api/chat/slots/{slot}/stop
│  ├─ slot._stopping = True
│  ├─ slot._stop_state = 'soft_pending'
│  ├─ insert StopEvent('stopping', timestamp) into slot messages
│  ├─ push_slots_update()  → SSE → frontend re-renders pulsing + card
│  │
│  ├─ if slot._stopping_second_press (within debounce window): go to FORCE
│  │    (set via separate POST /api/chat/slots/{slot}/stop?force=true)
│  │
│  └─ await sessions.stop_turn(
│         _history_key_for(name),
│         on_soft=lambda: _resolve_stop(slot, 'soft'),
│         on_hard=lambda: _resolve_stop(slot, 'hard'),
│     )
│
└─ _resolve_stop(slot, outcome):
   ├─ update StopEvent → '[Stopped]' or '[Stop Failed, Session Reset]'
   ├─ slot._stopping = False
   ├─ slot._stop_state = 'idle'
   ├─ if outcome == 'hard': do not cancel slot.task (eager respawn handles it)
   ├─ if outcome == 'soft': do not cancel slot.task (will finish on EVENT_COMPLETE cancelled)
   └─ push_slots_update()
```

**Second-press path (hard override):**

Frontend tracks `pulsing` state. A second click within the pulse window (not the first 150ms — debounce) fires `POST /api/chat/slots/{slot}/stop?force=true`, which routes to a distinct branch that:

1. Checks `slot._stop_state == 'soft_pending'` (must be in flight).
2. Calls `sessions.stop_turn(key, force=True, ...)` on a fresh task — or signals the existing in-flight `stop_turn()` to bail early (see §6.3).

### 6.2 Slack

```
user types !stop
├─ events.py:1422 intercept (unchanged entry point)
├─ post ephemeral block-kit message with "Kill Now" button
├─ await sessions.stop_turn(
│      session_key,
│      on_soft=lambda: _update_ephemeral('[Stopped]'),
│      on_hard=lambda: _update_ephemeral('[Stop Failed, Session Reset]'),
│   )
│
└─ on Kill Now button click (interactions.py):
   └─ await sessions.stop_turn(session_key, force=True, on_hard=...)
```

The ephemeral message update uses `response_url` with `replace_original: true`.

### 6.3 Second-press force flag — race handling

Race: the first `stop_turn()` call is mid-await on `wait_turn_done(budget)`. The second press arrives and calls `stop_turn(force=True)`. Two concerns:

1. **Semaphore contention**: `stop_turn()` does not take the per-session semaphore. Both calls can run concurrently. The first's cancel already fired; the second can proceed directly to `reset()`. Acceptable race — `reset()` is idempotent.
2. **First call's ack-wait**: after the second call's `reset()` kills the process, the first call's `wait_turn_done(budget)` will raise because the client disconnects. Catch and convert to `"timeout"` or `"hard"` — either way the first call returns a coherent outcome, and callbacks de-dup by checking `slot._stop_state` before firing.

Implementation:

```python
if slot._stop_state != 'soft_pending':
    return  # someone else resolved the stop already
```

## 7. State on `_ChatSlot`

Extend `_ChatSlot` in `src/kiro_crew/dashboard/state.py`:

```python
# Replace scalar _stopping with enum (keep _stopping as a property for backward compat)
_stop_state: Literal['idle', 'soft_pending', 'killing'] = 'idle'
_stop_event_id: str | None = None  # transcript message id for the in-flight stop card
```

Serialization (`to_dict`): emit `stop_state` as a top-level field. Frontend Redux slice adds it to `ChatSlot` type in `frontend types/index.ts`.

`slot._stopping` becomes a property: `return self._stop_state != 'idle'`. Existing callers continue to work.

## 8. Transcript: StopEvent

A `system` message with structured stop-event data JSON-encoded into the
`cls` field (for `parse_cls_meta` → frontend `meta.kind`-based routing)
and mirrored into `content` for consumers that only read content.

```json
{
  "role": "system",
  "content": "{\"kind\":\"stop_event\",\"id\":\"stop-<uuid>\",\"state\":\"stopping|stopped|stop_failed_reset\",\"outcome\":\"soft|hard|null\",\"ts_start\":\"<iso8601>\",\"ts_end\":\"<iso8601>\"}",
  "cls": "<same JSON as content>",
  "ts": "<iso8601>",
  "source_thread": "dashboard",
  "source_user": "dashboard"
}
```

- `chat.py` inserts this at soft-start time via `slot.append("system", stop_msg, stop_msg)` where `stop_msg` is the JSON string; updates in place (same `id`) in `_resolve_stop_event` when the outcome resolves, and re-broadcasts the updated message so the frontend card transitions.
- `history.py` persists it via `_save_slot_to_history`, which preserves the `cls` JSON for `role=='system'` messages (other roles rely on role-derived cls defaults).
- `restore_recent_sessions` preserves the persisted `cls` on reload, so StopEventCard rendering survives gateway restarts.
- `context.build_cancelled_turn_preamble` re-injects the cancelled user message + partial assistant as a bracketed preamble on the next prompt. This is gated by `_Session.prev_turn_cancelled` (one-shot flag set by `SessionManager.stop_turn` on soft-cancel success).
- Message-list renderer detects `m.kind === 'stop_event' || m.meta?.kind === 'stop_event'` and renders `StopEventCard`; the reducer replaces the in-flight card in place by `meta.id` when the updated broadcast arrives.

**Role / kind convention:** role `system`, with `kind` held inside the JSON payload of `cls`/`content`. This keeps `history.py` persistence generic (any role/content/cls triple) while making entries discoverable via structural parsing.

**LLM context restore after cancel:** kiro-cli does not persist cancelled turns to its ACP conversation log, so the LLM has no memory of the interrupted prompt on the next turn. KiroCrew re-injects the cancelled turn as a short preamble:

```
[PREVIOUS TURN WAS CANCELLED BY THE USER — context restore]
Cancelled user request: <user text, capped ~2000 chars>
Partial assistant response before cancel: <assistant text, capped ~2000 chars>
[END PREVIOUS TURN]
```

The preamble is built once on the first prompt after a cancel and consumed one-shot — subsequent prompts see only the normal ACP-held conversation.

## 9. Slack block-kit message

Ephemeral message structure (`src/kiro_crew/slack/blocks.py`):

```python
def build_stopping_blocks(session_key: str) -> list[dict]:
    return [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": "⏹  *Stopping…*\nCooperative cancel in progress."}},
        {"type": "actions",
         "block_id": f"stop-actions-{session_key}",
         "elements": [{
             "type": "button",
             "action_id": "stop_kill_now",
             "style": "danger",
             "text": {"type": "plain_text", "text": "Kill Now"},
             "value": session_key,
         }]},
    ]
```

On resolve, update via `response_url`:

```python
def build_stopped_blocks() -> list[dict]:
    return [{"type": "section",
             "text": {"type": "mrkdwn", "text": "⏹  *[Stopped]*"}}]

def build_stop_failed_blocks() -> list[dict]:
    return [{"type": "section",
             "text": {"type": "mrkdwn", "text": "⛔  *[Stop Failed, Session Reset]*"}}]
```

Action handler in `src/kiro_crew/slack/interactions.py`: new `stop_kill_now` action routes to `sessions.stop_turn(session_key, force=True, ...)` with the same `on_hard` callback.

**Authorization:** the `[Kill Now]` button enforces the standard
`is_allowed_user()` allowlist check (deny-by-default). While the
ephemeral message is scoped to the requester at the Slack UI level, an
attacker can still craft a raw HTTP POST with `action_id:
"stop_kill_now"` — so client-side scoping is not a security boundary.

## 10. Debounce

**Dashboard-side**: Redux slice tracks `stopPressedAt` per slot. A second press within `stop_state === 'soft_pending'` while `(now - stopPressedAt) < 150ms` is ignored. A second press after that gap fires the force path.

**Slack-side**: Slack delivers button clicks via webhook round-trip, no debounce needed.

150ms is chosen empirically — short enough to not feel laggy, long enough to absorb accidental double-clicks. Exposed as a frontend constant `SOFT_STOP_DEBOUNCE_MS` in the chat types file.

## 11. Config

`src/kiro_crew/config/loader.py` — add to `AgentConfig`:

```python
@dataclass
class AgentConfig:
    # ... existing fields ...
    soft_stop_budget_secs: float = 10.0

    def __post_init__(self) -> None:
        # ... existing validation ...
        if not 0.5 <= self.soft_stop_budget_secs <= 60.0:
            raise ValueError(
                f"soft_stop_budget_secs must be in [0.5, 60.0], got {self.soft_stop_budget_secs}"
            )
```

`config/schema.py` picks up the new field automatically via dataclass introspection.

Settings UI (`frontend settings ChatPanel`):

```tsx
<div className="...">
  <label>Soft-stop budget (seconds)</label>
  <Input
    type="number"
    min={0.5}
    max={60}
    step={0.5}
    value={config.agent.soft_stop_budget_secs}
    onChange={...}
  />
  <InfoTip text="How long to wait for the agent to honor a Stop press before forcefully killing the session. Longer budgets preserve session state more often but make stops feel laggy when agents are stuck in long tool calls." />
</div>
```

## 12. Eager respawn behavior

After a hard kill, `stop_turn` fires `asyncio.create_task(self._eager_respawn(key))` and returns immediately. The respawn:

1. Uses existing `get_or_create(key)` semantics — pulls from warm pool if eligible, cold-spawns otherwise. No new fast-path.
2. Honors existing session-resume mapping (`SessionMap`), so if the key had a persistent kiro-cli session-id, resume is attempted.
3. On failure: logs at debug, does nothing more. The next user message triggers `get_or_create` again; if the warm pool was exhausted transiently it recovers naturally, if something more fundamental is wrong the user sees the error on their next message — same UX as today.

Rationale: eager respawn is a UX optimization (by the time the user types their next message, the session is likely ready), not a correctness requirement. If it fails, the existing error path handles it.

## 13. Telemetry

SEL audit events gain `outcome` and `elapsed_ms` fields:

```python
sel().log_tool_invocation(
    tool_name="!stop" | "dashboard_stop" | "stop_kill_now",
    outcome="soft" | "hard" | "idle",
    metadata={
        "elapsed_ms": round((t_end - t_start) * 1000),
        "budget_secs": cfg.agent.soft_stop_budget_secs,
        "tool_at_stop": last_tool_name,  # if available
    },
)
```

Over time, the distribution of `elapsed_ms` per `outcome` and `tool_at_stop` tells us which tools block cancel and whether the default budget is tuned correctly.

## 14. Tests

### Backend

- `test/test_acp_client.py`: stop_reason extraction from response, `turn_done` event semantics, `_cancelled` flag no longer short-circuits reads, `wait_turn_done(timeout)` contract.
- `test/test_session.py`: `stop_turn()` soft / hard / idle / force paths; eager respawn fires once; first-press queue clear; second-press override; race between concurrent `stop_turn` calls.
- `test/test_config_loader.py`: `soft_stop_budget_secs` validation (range + default).
- `test/test_slack_handler.py`: `!stop` posts ephemeral blocks, updates on outcome.
- `test/test_dashboard_chat.py`: slot stop-state transitions, StopEvent transcript persistence, second-press `force=true` routing.
- `test/test_slack_interactions.py`: `stop_kill_now` action handler, permission skip for ephemeral.

### Frontend

- `integration/ChatFooter.integration.test.tsx`: button state transitions (armed → pulsing → killing → idle); debounce; second-press force.
- `integration/StopEvent.integration.test.tsx`: card renders in 3 states; red tones; replace-in-place.
- `integration/ChatPanel.ChatSettings.integration.test.tsx`: budget input validates range.

### E2E

- `playwright/chat.spec.ts`: stop mid-tool-call triggers pulsing, resolves to `[Stopped]` or `[Stop Failed, Session Reset]` depending on mocked latency.

## 15. Migration & rollout

Single CR against `beta-braveheart`. Lands atomically (backend + frontend + spec) to avoid UX regressions.

Dark-launch rollout:

1. Ship with default `soft_stop_budget_secs=10.0`. Observe SEL event `outcome` distribution for 1–2 weeks.
2. If soft-success rate at 10s is near 100% (current kiro-cli 2.1+ acks cancel within <1s), consider lowering default back to 5s to tighten hard-kill fallback. If soft-success rate drops (<80%), keep 10s or raise further.
3. Power users can tune via config or Settings UI immediately.

Rollback path: revert the three caller changes (`dashboard/chat.py`, `slack/events.py`, `slack/interactions.py`) back to direct `reset()`. The new plumbing in `acp/client.py`, `session.py`, `providers/acp.py` is additive — leaving it in place does no harm.

## 16. Spec updates

Matching updates in the same CR:

- `docs/system-specs/modules/acp-client.md` — `stopReason` parsing, `turn_done` event, removal of `_cancelled` short-circuit.
- `docs/system-specs/modules/session.md` — `stop_turn(force=, on_soft=, on_hard=)` API, eager respawn, first-press queue clear.
- `docs/system-specs/modules/providers.md` — `cancel(wait_ack_timeout=)` signature and return type.
- `docs/system-specs/modules/slack-gateway.md` — `!stop` ephemeral block flow, `stop_kill_now` action, removal of "clears queue on second press" (now first press).
- `docs/system-specs/modules/config.md` — `agent.soft_stop_budget_secs` field.
- `docs/system-specs/modules/history.md` — `stop_event` message kind.

## 17. Estimated diff size

- Backend: ~400 lines (acp/client 40, providers 25, session 80, config 10, dashboard/chat 60, dashboard/state 15, slack/events 40, slack/handler 20, slack/interactions 60, slack/blocks 30, spec updates 20)
- Frontend: ~350 lines (ChatFooter Stop button 80, Redux slice 40, stop-event card component 100, ChatPanel settings 50, types 20, integration tests 60)
- Tests (backend + frontend): ~400 lines
- Spec updates: ~80 lines
- **Total: ~1,230 lines added, ~50 removed. One CR.**

## 18. Implementation plan (phased in a single CR)

Phase boundaries are internal to implementation — all land in one CR but can be developed and self-tested in order:

1. **Protocol observation (backend, isolated).** Populate `stop_reason` in `acp/client.py`, add `turn_done`, remove `_cancelled` short-circuit from `_read_message`. New tests in `test/test_acp_client.py`.
2. **Provider + SessionManager API.** Add `cancel(wait_ack_timeout=)`, `stop_turn(force=, on_soft=, on_hard=)`, `_eager_respawn`. New tests in `test/test_session.py`.
3. **Config.** Add `soft_stop_budget_secs` to `AgentConfig`, validator, tests.
4. **Handler wiring (backend).** Route handlers check `event.stop_reason == "cancelled"` and skip error / consolidation paths.
5. **Dashboard backend.** Replace `sessions.reset()` call in `api_chat_slot_stop` with `sessions.stop_turn(...)`. Add `_stop_state` on `_ChatSlot`, StopEvent insertion and resolution, `?force=true` query parameter.
6. **Slack backend.** Replace `sessions.reset()` calls in `slack/events.py:1422`, `slack/handler.py:990`, `slack/interactions.py:1416` (`_handle_stop_confirm`). Add `build_stopping_blocks` / `build_stopped_blocks` / `build_stop_failed_blocks` in `slack/blocks.py`. Add `stop_kill_now` action handler.
7. **Frontend (dashboard).** `ChatFooter` Stop button state machine, Framer Motion pulse animation, debounce, `?force=true` fetch on second press. `MessageList` / `TurnBlock` rendering for `stop_event` card.
8. **Frontend (settings).** `ChatPanel` budget number input + `InfoTip`.
9. **Spec updates.** Update all six spec files listed in §16.
10. **Changelog entry** in `CHANGELOG.md` under next unreleased version.

---

## References

- Commit `45defbb` (2026-02-24) — "fix: hard-stop chat generation and preserve history on update." Rationale for the current hard-kill policy.
- Commit `76eb780` (2026-04-02) — "feat: add !stop command to force-halt agent execution mid-stream." Introduced `!stop`.
- ACP spec — `https://agentclientprotocol.com/protocol/prompt-turn#cancellation`. Defines `stopReason: "cancelled"` as the acknowledgement protocol.
- `docs/system-specs/modules/acp-client.md` — current ACP client contract.
- `docs/system-specs/modules/session.md` — current `SessionManager` API.
- `docs/system-specs/modules/slack-gateway.md:171` — current `!stop` documentation.
