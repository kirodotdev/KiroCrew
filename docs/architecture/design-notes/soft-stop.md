# Cooperative stop, with a hard-kill fallback

A user-initiated Stop (the dashboard button, Slack `!stop`, `/kirocrew stop`)
first asks kiro-cli to cancel the turn cooperatively and escalates to a hard
process kill only when it does not acknowledge within a budget. This note records
why the escalation exists, why each surface behaves as it does, and the races the
implementation has to survive.

## Why cooperative first

A hard kill discards in-memory kiro-cli state, so conversation context has to be
re-injected from JSONL on every resume, and that re-injection is lossy:
mid-stream tokens not yet flushed to JSONL are gone, and in-flight tool results
are discarded. It also pays a process respawn (the warm pool cushions this but
does not eliminate it) and risks orphaning MCP child processes.

The ACP protocol gives a clean acknowledgement: `stopReason: "cancelled"` on the
`session/prompt` response. So the cooperative path is preferred, and the kill is
kept only as the fallback for a backend that demonstrably will not honor a
cancel.

## Why the kill is still reachable

Cooperative cancel can block indefinitely behind a long tool call, which is why
kill-first was the earlier policy. A stop that silently does nothing is worse
than a stop that costs a respawn, so the budget exists to bound the wait, and a
second press escalates immediately for a user who is not willing to wait it out.

## The budget

`agent.soft_stop_budget_secs` (`config/loader.py`), default `10.0`, clamped to
`[0.5, 60.0]`. `AgentConfig.__post_init__` clamps rather than raises, matching
what the dashboard PATCH path and the loader already do; an out-of-range value
logs a WARNING. `config/schema.py` picks the field up automatically by dataclass
introspection, so it is editable from the CLI and from Settings with no extra
plumbing.

The budget is also handed down into the ACP client, and that matters: the client's
read loop abandons a cancelled turn as unresponsive once its own grace window
elapses, so a grace shorter than the caller's budget would make the loop bail
first and force a session-losing hard kill even though the caller was still
willing to wait. `cancel_session(grace_secs=)` therefore sets
`_cancel_grace_secs = max(_CANCEL_GRACE_SECS, grace_secs)` (floor 10s), so a
configured budget above the floor genuinely extends the window.

## The layers

### ACP client

`AcpClient.cancel_session(grace_secs)` writes the `session/cancel` JSON-RPC
**notification** (no id, per the ACP spec) and returns. The acknowledgement does
not come back as a response to that message: it arrives as `stopReason` on the
in-flight `session/prompt` response. The client sets `_cancelled` and
`_cancel_ts` so `_read_message` can enforce the grace window, then writes to
stdin and drains.

The turn's completion is observable through two pieces of state:

- `_last_stop_reason`, set from the prompt response's `stopReason` when the read
  loop dispatches a completion, and cleared at the start of each turn. It is also
  set to `STOP_REASON_END_TURN` on the synthetic completion the client emits for a
  stale turn (kiro-cli finished but never sent `result`), so a stale turn
  finalizes normally instead of surfacing as a timeout.
- `_turn_done`, an `asyncio.Event` set when the turn reaches any done boundary.

`wait_turn_done(timeout)` awaits that event and returns the stop reason, or raises
`asyncio.TimeoutError`. `has_active_turn()` returns False as soon as
`cancel_session()` has been called, even before the agent acknowledges, so a
caller that needs to force a kill regardless of cancel state must skip that check.

`STOP_REASON_CANCELLED` and `STOP_REASON_END_TURN` live in `acp/types.py`
alongside the other reasons (`refusal`, `stale_recover`, a tool-stall marker).

### Provider

```python
CancelOutcome = Literal["acked", "timeout", "no_turn", "error"]

async def cancel(self, *, wait_ack_timeout: float = 0.0) -> CancelOutcome
```

The default of `0.0` is fire-and-forget, which is what internal programmatic
callers (the task runner, the subagent manager, `llm_helpers`) want: they are not
presenting a stop affordance to a user and must not block on an ack. A positive
timeout sends the cancel and then waits for the ack, returning `"acked"` only for
a `cancelled` or `end_turn` reason and `"timeout"` otherwise.

The API is a single keyword float rather than a request dataclass on purpose:
the user-facing escalation policy (double-press means kill now, the budget)
belongs at `SessionManager.stop_turn()`, not in the provider. Adding provider-level
knobs later is the trigger to reconsider the shape.

### `SessionManager.stop_turn`

```python
StopOutcome = Literal["soft", "hard", "idle"]

async def stop_turn(
    self, key, *, force=False, preserve_queue=False, on_soft=None, on_hard=None
) -> StopOutcome
```

The sequence:

1. Clear the queue, unless `preserve_queue=True` (the interrupt flow, which wants
   the next queued message to run).
2. If `force`, go straight to the hard kill.
3. Otherwise `provider.cancel(wait_ack_timeout=budget)`.
   - `"acked"`: set `session.prev_turn_cancelled = True`, await `on_soft`, return
     `"soft"`.
   - `"no_turn"`: return `"idle"`.
   - `"timeout"` or `"error"`: fall through and escalate.
4. Hard kill: push an abort frame to gatewayd for the session's runtime PIDs, then
   `reset(key)`, then fire a background respawn, then await `on_hard`, and return
   `"hard"`.

The hooks are awaited so a caller can settle its UI before the turn's own
async-generator handler observes the completion event. A hook that raises is logged
and swallowed: a UI-update failure must not abort a stop.

`prev_turn_cancelled` is set only on the soft path, because kiro-cli discards a
cancelled turn from its own conversation log, so the next prompt has to re-inject
the lost context (see [Context restore](#context-restore-after-a-soft-cancel)).

The abort push exists because in the pooled topology in-flight tool work runs in
backend processes that a local `reset()` does not reach. It is best-effort, and
deliberately loud when it cannot resolve a runtime PID or socket: it warns rather
than failing silently, because if provider internals are renamed the push stops
firing and the escape-hatch behavior regresses quietly. The initiation is
SEL-audited at the point of decision, since `schedule_abort` is fire-and-forget and
the downstream applied-audit only fires on success.

**Eager respawn is a UX optimization, not a correctness requirement.** After a hard
kill, `_eager_respawn` runs as a tracked background task (a strong reference is
kept, because the event loop holds only a weak one and the task could otherwise be
garbage-collected mid-respawn). It calls the ordinary `get_or_create(key)`, so the
warm pool and the persisted session-resume mapping are honored with no new fast
path, and then releases the per-session semaphore that `get_or_create` acquires on
every return path, so the next real user message can run. On failure it logs at
debug and does nothing more: the next user message calls `get_or_create` again, so
a transient pool exhaustion recovers on its own and a deeper problem surfaces on
that message exactly as it would have anyway.

### Handler response to a cancelled turn

`stop_reason` is now load-bearing at the surfaces, and a cancelled turn is
deliberately **not** treated as a failure:

- `record_success` is skipped, and the per-interaction telemetry event is not
  emitted.
- Memory consolidation is skipped for that turn.
- The empty-response retry is suppressed. A cancelled turn legitimately produced no
  visible output, and blind-retrying it would re-run work the user just stopped.
- The refusal-recovery continuation is suppressed
  (`dashboard/state.should_recover_from_refusal` gates on it), because a user stop
  is not a policy block and must not trigger an automatic retry.

## Surfaces

### Dashboard

`POST /api/chat/slots/{slot}/stop` (`dashboard/chat_handlers.py`), with an optional
`?force=true`.

**First press** sets `slot._stop_state = "soft_pending"`, turns off auto-run
(SEL-audited as `auto_run_stopped`), inserts a `stop_event` transcript message, and
calls `stop_turn(..., force=False, preserve_queue=True, on_soft=, on_hard=)`. The
queue is deliberately preserved here: a stop should cancel the running turn and
leave queued messages for the user to process or dismiss individually. If the
provider reports `"idle"`, the orphaned card is resolved immediately.

**Second press escalates on ANY second press**, not only when the client computed
`force=true`. The client derives `force` from the WS-echoed `stop_state`, which can
lag on a slow connection; the backend's own `_stop_state` is the authoritative
"already soft_pending" signal, so a second press always means kill it. Escalation
sets `_stop_state = "killing"`, clears the queue **and** the unconsumed steers (a
hard kill means discard everything, and the end-of-turn requeue would otherwise
resurrect them), records `_stop_escalated_card_id`, and calls
`stop_turn(force=True)`.

Both paths first call `_unblock_pending_waits`, because a chat runner suspended on a
tool approval or a pending question card would otherwise never observe the stop.

**Repeat presses are idempotent.** A press while `_stop_state` is not `idle`, or
while the slot is not running, logs and returns `{"ok": true, "info": ...}` with a
`noop` SEL outcome.

#### The transcript card and its precedence rules

The `stop_event` entry is a `system` message whose structured payload is
JSON-encoded into **both** `cls` (so `parse_cls_meta` populates `meta` on the wire,
which is what the frontend routes on) and `content` (for consumers that read only
content). The payload carries `kind`, `id`, `state`
(`stopping` / `stopped` / `stop_failed_reset`), `outcome`, `ts_start` and `ts_end`.
`_resolve_stop_event` rewrites it **in place** by id and re-broadcasts, so
`StopEventCard` transitions from its pulsing "Stopping" state to a settled one.

Two race rules are load-bearing, and both exist because a turn tearing down
concurrently drives `_stop_state` back to `idle`:

- **The resolver's guard keys on `_stop_event_id`, never on `_stop_state`.** The card
  id is already the idempotency token: `_resolve_stop_event` no-ops on `None` and
  clears the id once it has settled the card. Gating on the state instead meant that
  when teardown won the race, the hard callback bailed, the resolver never ran, and
  the card pulsed at "stopping" for the rest of the session.
- **Precedence has its own non-racy marker.** A cooperative ack arriving after the
  user escalated must not relabel a hard kill as a clean stop, and `_stop_state`
  cannot carry that fact because teardown resets it to `idle` from `killing` just as
  readily as from `soft_pending`. So the escalation path sets
  `slot._stop_escalated_card_id`, which teardown never touches, and only the **soft**
  callback defers on it. `hard` is terminal and nothing outranks it. The marker holds
  an **id** rather than a boolean, because a bare flag left set would make the NEXT
  card's cooperative ack defer to a hard callback that never fires, stranding that
  card at "stopping".

Each callback is also bound to the specific `card_id` it was created for, not to
whatever card happens to be in flight when it fires. `stop_turn` awaits these
callbacks, so one can still be pending when teardown resets the posture, a new turn
starts, and a second stop opens a NEW card; reading `slot._stop_event_id` at call
time would settle that newer card with the older outcome and clear its posture, so
the newer stop's own callback would find nothing left to settle. A `card_id` of
`None` is valid (a stop that escalated before any card existed): such a callback
still releases the posture, it just has no card to label.

#### The frontend Stop button

`_stop_state` is serialized to the slot as `stop_state`, and `_stopping` is a
property over it (`!= "idle"`) so existing callers keep working. The button has
three shapes:

- **`soft_pending`:** a clickable force-kill affordance.
  `utils/stopDebounce.decideStopAction` ignores a second press that lands within
  `FORCE_KILL_ARMING_MS` (400ms) of the soft press, so a frantic double-tap cannot
  immediately hard-kill. Nothing is lost by waiting: the backend auto-escalates on
  its own once the budget elapses. The arming timestamp is tracked **per slot**, so
  the window is measured against that slot's own soft press.
- **`killing`:** disabled with a spinner, because the kill is in flight.
- **`killing` past `useStopEscapeHatch`'s `KILLING_ESCAPE_MS` (15s):** re-enabled as
  a "Force reset" affordance with a "taking longer than expected" hint. The hard kill
  itself has stalled, so the press must re-dispatch `force: true`;
  `isEscalationState()` returns true for `killing` as well as `soft_pending` for
  exactly this reason, since a plain soft cancel would be ignored as redundant by the
  backend and the button would be a no-op.

`StopEventCard` renders the three payload states with `lucide-react` icons and the
danger design tokens, and the pulsing state uses a Framer Motion opacity loop rather
than new CSS keyframes.

### Slack and other channels

`!stop` posts an **ephemeral** Block Kit message with a `Kill Now` button
(`slack/blocks.build_stopping_blocks`), then calls `stop_turn` with `on_soft` and
`on_hard` callbacks that post a thread reply on resolution: a soft "Execution
stopped." or a hard "Execution stopped, session reset." If `stop_turn` returns
`"idle"` neither callback fires, so the caller explicitly dismisses the stale
"Stopping" ephemeral with "Nothing running."

The resolution reply is a normal thread message, not ephemeral, so the audit trail
is visible to other thread participants and to the context builder.

**The `Kill Now` button re-checks the allowlist.** `_handle_stop_kill_now` calls
`is_allowed_user()` even though `dispatch()` already enforces it, and audits a
denial. Slack's ephemeral scoping is a UI-level nicety, not a security boundary: an
attacker can craft a raw HTTP POST carrying `action_id: "stop_kill_now"`. On the
hard path it replaces the ephemeral in place via `response_url`
(`replace_original: true`) with `build_stop_failed_blocks()`, and posts the thread
reply against the ephemeral's own `thread_ts` (falling back to its message ts)
rather than the session key, because for a linked dashboard session those differ and
the session key is not a valid Slack thread.

## Context restore after a soft cancel

kiro-cli does not persist a cancelled turn to its ACP conversation log, so after a
soft cancel the model has no memory of what the user asked or what it had started
saying. Two mechanisms cover that, both reading the persisted transcript rather than
the backend's state.

**`context.build_cancelled_turn_preamble`** scans backwards for a `stop_event`
marker, takes the user message immediately before it plus any assistant text in
between, caps each at 2000 characters, and renders:

```
[PREVIOUS TURN WAS CANCELLED BY THE USER — context restore]
The following user request was interrupted mid-response. Do not emit any
standalone acknowledgment of the cancellation. Use this restored context
silently and respond only to the current user request, referencing the
interrupted work only when the current request depends on it.

Cancelled user request:
<user text>

Partial assistant response before cancel:
<assistant text>
[END PREVIOUS TURN]
```

It falls back to "the latest user entry" when no `stop_event` marker is present
(Slack writes no card), which is safe for two reasons: `prev_turn_cancelled` is a
one-shot flag consumed immediately before the preamble is built, and callers persist
the NEW user message to the conversation log only **after** the preamble is built,
so at that moment `recent()` holds only prior turns and the latest user entry is the
cancelled one. It is consumed one-shot on the first prompt after a cancel, so
subsequent prompts see only the normal ACP-held conversation.

**`context._build_stop_event_notes`** separately injects up to
`_STOP_EVENT_CAP` (3) short system notes for recent **resolved** `stop_event`
entries, so the model can see that a previous turn was halted rather than completed.
The scan is bounded because a stop from hundreds of turns ago is not actionable
context.

`history.py` preserves the `cls` JSON for `role == "system"` messages (other roles
rely on role-derived cls defaults), and session restore preserves it on reload, so
card rendering and both context mechanisms survive a gateway restart.

## Concurrency

`stop_turn` does **not** take the per-session semaphore, so a first call still
awaiting `wait_turn_done(budget)` and a second `force=True` call can run
concurrently. That is acceptable: the first call's cancel has already been written,
`reset()` is idempotent, and once the second call's reset kills the process the
first call's ack-wait fails and converts to a coherent `"timeout"` outcome. The
callbacks de-duplicate through the card-id and escalation-marker rules above, so
whichever order they land in, the card settles once and settles correctly.

## Telemetry

Each stop surface emits a SEL tool invocation (`!stop`, `dashboard_stop`,
`stop_kill_now`) carrying the `StopOutcome` and metadata, including whether the
backend escalated. `stop_turn` additionally logs the outcome and elapsed time at
INFO for each branch (`soft-acked`, `idle`, `escalated-to-hard`, `hard-done`), which
is what makes the soft-success rate and the distribution of ack latency observable
without extra instrumentation. That is the evidence for tuning the default budget:
raise it if soft-success is low, lower it if acks land well under the current value.
